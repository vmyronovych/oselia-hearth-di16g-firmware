"""OTA application updates -- pure core + on-device install/state helpers.

See OTA_SPEC.md for the contract. Scope: replace the **app .py files** (not the
MicroPython interpreter) via an A/B slot layout with a boot-confirm / auto-revert
safety gate. Transport is MQTT-chunked (no CH9120 retarget) -- the receiver lives in
net_task and feeds bytes here; see Phase 2.

This module is split so the decision logic and bundle (de)serialisation are **pure**
(json/hashlib only) and run under CPython for host tests (tests/test_ota.py); the file
I/O uses only stdlib `os`/`json` so it is host-testable too. Only `reset()` touches
`machine`, imported lazily.

Layout (OTA_SPEC.md "On-device slot layout"):
    /boot.py            loader (installed once; never in a bundle)
    /site.json          machine-owned; never touched by OTA
    /slots/a , /slots/b one full copy of the app each
    /ota/active         text "a"|"b"  (kept inside /ota/state too)
    /ota/state          JSON: {active, previous, pending, tries}
"""
try:
    import ujson as json
except ImportError:
    import json

try:
    import uhashlib as _hashlib
except ImportError:
    import hashlib as _hashlib

try:
    import ubinascii as _binascii
except ImportError:
    import binascii as _binascii


# ===========================================================================
# pure helpers
# ===========================================================================
def sha256_hex(data):
    """Hex sha256 of bytes (works under uhashlib and CPython hashlib)."""
    return _binascii.hexlify(_hashlib.sha256(data).digest()).decode()


def other_slot(slot):
    return "b" if slot == "a" else "a"


def default_state():
    return {"active": "a", "previous": "a", "pending": False, "tries": 0}


# ---- boot-confirm / auto-revert state machine (the safety core) ----
def boot_decision(state, max_tries):
    """Pure: decide which slot to boot from `state` and the state to persist first.

    Returns (slot, new_state, reverted). On a `pending` build, increment tries; if it
    exceeds max_tries the build never proved itself -> revert to `previous`. The
    running app calls confirm() once healthy to clear `pending`.
    """
    active = state.get("active", "a")
    if not state.get("pending"):
        return active, state, False
    tries = int(state.get("tries", 0)) + 1
    if tries > max_tries:
        prev = state.get("previous", active)
        new = dict(state)
        new.update(active=prev, pending=False, tries=0)
        return prev, new, True
    new = dict(state)
    new["tries"] = tries
    return active, new, False


def confirm_state(state):
    """State after the new build proves itself healthy: clear the pending gate."""
    new = dict(state)
    new.update(pending=False, tries=0)
    return new


def staged_state(state, new_slot):
    """State after a verified download into `new_slot`: boot it next, pending-confirm."""
    new = dict(state)
    new.update(previous=state.get("active", "a"), active=new_slot,
               pending=True, tries=0)
    return new


# ---- bundle format: manifest line + concatenated file bytes ----
def build_bundle(files):
    """`files`: iterable of (name, bytes). -> bundle bytes.

    Layout: a JSON manifest `[[name,size,sha256hex],...]` then a newline then the
    files' bytes concatenated in manifest order. No tar lib needed on the device.
    """
    manifest = []
    blob = b""
    for name, content in files:
        manifest.append([name, len(content), sha256_hex(content)])
        blob += content
    header = json.dumps(manifest).encode() + b"\n"
    return header + blob


def parse_bundle(blob):
    """Reverse build_bundle with per-file sha256 verification.

    -> list of (name, bytes). Raises ValueError on a malformed bundle or any hash /
    length mismatch (the caller then aborts the swap and keeps the current version).
    """
    nl = blob.find(b"\n")
    if nl < 0:
        raise ValueError("bundle: no manifest terminator")
    manifest = json.loads(blob[:nl].decode())
    data = blob[nl + 1:]
    out = []
    off = 0
    for entry in manifest:
        name, size, sha = entry[0], int(entry[1]), entry[2]
        chunk = data[off:off + size]
        off += size
        if len(chunk) != size:
            raise ValueError("bundle: short file %s" % name)
        if sha256_hex(chunk) != sha:
            raise ValueError("bundle: sha mismatch %s" % name)
        out.append((name, chunk))
    if off != len(data):
        raise ValueError("bundle: trailing bytes")
    return out


# ===========================================================================
# on-device file I/O (stdlib os/json only -> host-testable with a temp dir)
# ===========================================================================
def read_state(path):
    """Read /ota/state JSON, or the safe default if missing/corrupt."""
    try:
        with open(path) as f:
            s = json.load(f)
        if isinstance(s, dict) and "active" in s:
            return s
    except (OSError, ValueError):
        pass
    return default_state()


def write_state(path, state):
    """Atomic write of /ota/state (tmp + rename) so a power cut can't truncate it."""
    import os
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.rename(tmp, path)


def _ensure_empty_dir(path):
    import os
    try:
        for name in os.listdir(path):
            os.remove(path + "/" + name)
    except OSError:
        try:
            os.mkdir(path)
        except OSError:
            pass


def apply_bundle(blob, slot_dir):
    """Verify `blob` and write every file into `slot_dir` (replacing its contents).

    Returns the list of file names written. Raises (via parse_bundle) before touching
    the slot if the bundle is bad, so a failed verify never corrupts a slot.
    """
    files = parse_bundle(blob)              # verifies first -> raises before any write
    _ensure_empty_dir(slot_dir)
    for name, content in files:
        with open(slot_dir + "/" + name, "wb") as f:
            f.write(content)
    return [name for name, _ in files]


def file_sha256(path, bufsize=1024):
    """Hex sha256 of a file, read in chunks (low RAM)."""
    h = _hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return _binascii.hexlify(h.digest()).decode()


class OtaReceiver:
    """Loss-tolerant chunk receiver: writes each chunk at its byte offset and tracks
    which indices arrived, so chunks may come out of order or be dropped (the board
    subscribes at QoS0). The caller NAKs `missing()` indices for the publisher to
    resend, and only `finish()` (whole-bundle sha) when `complete`. Staging is
    pre-allocated on flash so out-of-order seek-writes are safe and RAM stays free.
    """

    def __init__(self, staging_path, total_chunks, total_size, whole_sha,
                 chunk_size, beat=None):
        self.staging = staging_path
        self.total_chunks = total_chunks
        self.total_size = total_size
        self.whole_sha = whole_sha
        self.chunk_size = chunk_size
        self.received = set()
        # Pre-create the file at full size (zeros) so seek-writes never hit a hole;
        # beat() between blocks keeps the watchdog fed during this ~N-KB write.
        z = b"\x00" * 512
        with open(staging_path, "wb") as f:
            remaining = total_size
            while remaining > 0:
                n = 512 if remaining >= 512 else remaining
                f.write(z[:n])
                remaining -= n
                if beat and (remaining & 0x1FFF) == 0:
                    beat()
        self._f = open(staging_path, "r+b")

    def add_chunk(self, index, data):
        if index >= self.total_chunks or index in self.received:
            return                          # out-of-range or duplicate -> ignore
        self._f.seek(index * self.chunk_size)
        self._f.write(data)
        self.received.add(index)

    @property
    def complete(self):
        return len(self.received) >= self.total_chunks

    def percent(self):
        if not self.total_chunks:
            return 100
        return len(self.received) * 100 // self.total_chunks

    def missing(self, limit=48):
        out = []
        for i in range(self.total_chunks):
            if i not in self.received:
                out.append(i)
                if len(out) >= limit:
                    break
        return out

    def finish(self):
        """Flush + verify the whole-bundle sha256. Raises on mismatch."""
        self._f.flush()
        self._f.close()
        if file_sha256(self.staging) != self.whole_sha:
            raise ValueError("ota: whole-bundle sha mismatch")

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass


def apply_bundle_file(staging_path, slot_dir, bufsize=1024, beat=None):
    """Stream-install a (already whole-sha-verified) staging bundle into slot_dir.

    Reads the manifest from the head, then copies each file's bytes with a per-file
    sha re-check. Writes the INACTIVE slot only, so a failure here can't corrupt the
    running slot -- the caller flips the active pointer only on success. `beat()` is
    called between writes to keep the watchdog fed during the ~N-KB copy.
    """
    with open(staging_path, "rb") as f:
        header = b""
        while True:
            c = f.read(1)
            if not c:
                raise ValueError("bundle: no manifest")
            if c == b"\n":
                break
            header += c
        manifest = json.loads(header.decode())
        _ensure_empty_dir(slot_dir)
        names = []
        for entry in manifest:
            name, size, sha = entry[0], int(entry[1]), entry[2]
            h = _hashlib.sha256()
            remaining = size
            with open(slot_dir + "/" + name, "wb") as out:
                while remaining > 0:
                    chunk = f.read(min(bufsize, remaining))
                    if not chunk:
                        raise ValueError("bundle: short file %s" % name)
                    h.update(chunk)
                    out.write(chunk)
                    remaining -= len(chunk)
            if _binascii.hexlify(h.digest()).decode() != sha:
                raise ValueError("bundle: sha mismatch %s" % name)
            names.append(name)
            if beat:
                beat()
    return names


def reset():
    """Soft-reset into the loader (board only)."""
    import machine
    machine.reset()
