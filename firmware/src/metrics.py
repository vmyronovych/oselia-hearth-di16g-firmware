"""Metrics framework -- the firmware's single telemetry namespace.

ONE place owns collection, the fault ring, snapshotting, and serialization; the rest of the
firmware only calls this API. Replaces the scattered counters dict (input_task), the
`_empty_mcp_diag` seed (shared_state) and the ~20-arg `diag.build_state`.

Design contract (see stability-metrics-design.md):
  * REQ self-contained: this module + metrics_schema (pure) + metrics_store (hardware, injected).
  * REQ never-freeze: mutators (`inc`/`set_gauge`/`add_fault`) are cheap and allocation-light and
    called from either core under a single-writer-core discipline (fault ring is lock-guarded);
    `serialize()` writes into a REUSED buffer (no `json.dumps` transient on a fragmented heap);
    everything is wrapped so a metrics failure degrades to "no metrics", never raising into the
    network loop.
  * REQ tiny wire: short nested keys via metrics_schema; `serialize()` emits compact JSON.

PURE: no top-level `import machine`/`os` -- the hardware store is injected, so the whole core
runs under CPython for host tests.
"""
import metrics_schema as S


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---- never-freeze serializer: index-based writer over a reused buffer ----
class _Writer:
    """Append bytes into a bytearray by index so the buffer's capacity is reused across
    publishes (grows once to the high-water size, then stays). Avoids a large transient
    string from json.dumps on a fragmented heap."""

    def __init__(self, buf):
        self.buf = buf
        self.n = 0

    def w(self, b):
        end = self.n + len(b)
        if end > len(self.buf):
            self.buf.extend(b"\x00" * (end - len(self.buf)))
        self.buf[self.n:end] = b
        self.n = end

    def result(self):
        return bytes(self.buf[:self.n])


_ESCAPES = {'"': b'\\"', "\\": b"\\\\", "\n": b"\\n", "\r": b"\\r", "\t": b"\\t"}


def _enc_str(wr, s):
    wr.w(b'"')
    for ch in s:
        e = _ESCAPES.get(ch)
        if e is not None:
            wr.w(e)
        elif ch < " ":
            wr.w(("\\u%04x" % ord(ch)).encode())
        else:
            wr.w(ch.encode("utf-8"))
    wr.w(b'"')


def _enc(wr, obj):
    # bool before int: isinstance(True, int) is True
    if obj is True:
        wr.w(b"true")
    elif obj is False:
        wr.w(b"false")
    elif obj is None:
        wr.w(b"null")
    elif isinstance(obj, str):
        _enc_str(wr, obj)
    elif isinstance(obj, int):
        wr.w(str(obj).encode())
    elif isinstance(obj, float):
        wr.w(str(obj).encode())
    elif isinstance(obj, dict):
        wr.w(b"{")
        first = True
        for k, v in obj.items():
            if not first:
                wr.w(b",")
            first = False
            _enc_str(wr, k)
            wr.w(b":")
            _enc(wr, v)
        wr.w(b"}")
    elif isinstance(obj, (list, tuple)):
        wr.w(b"[")
        first = True
        for v in obj:
            if not first:
                wr.w(b",")
            first = False
            _enc(wr, v)
        wr.w(b"]")
    else:
        _enc_str(wr, str(obj))


def dumps(obj, buf=None):
    """Compact JSON bytes for `obj` using the reused-buffer writer. Pure/host-testable."""
    wr = _Writer(buf if buf is not None else bytearray(256))
    _enc(wr, obj)
    return wr.result()


# ---- fault ring (semantic records; anchored by uptime + boot_count) ----
class FaultRing:
    """Bounded ring of recent fault records, newest last. Records are
    {up, boot, component, code, detail[, board]} -- the `boot` field anchors a record to the
    reboot it happened in, so a persisted ring stays interpretable across reboots."""

    def __init__(self, size):
        self.size = max(1, int(size))
        self._items = []

    def add(self, up, boot, component, code, detail, board=None):
        rec = {"up": up, "boot": boot, "component": component,
               "code": code, "detail": S.cap(detail)}
        if board is not None:
            rec["board"] = board
        self._items.append(rec)
        if len(self._items) > self.size:
            self._items = self._items[-self.size:]
        return rec

    def restore(self, items):
        for r in items or []:
            self._items.append(dict(r))
        if len(self._items) > self.size:
            self._items = self._items[-self.size:]

    def last(self):
        return dict(self._items[-1]) if self._items else None

    def items(self):
        return [dict(r) for r in self._items]


class Metrics:
    """The telemetry registry. See module docstring for the never-freeze/cross-core contract."""

    def __init__(self, store=None, ring_size=16, lock=None, buf_size=512):
        self._lock = lock if lock is not None else _NullLock()
        self._store = store
        self._counters = {k: 0 for k in S.COUNTER_KEYS}
        self._gauges = {}                 # short top-level key -> value
        self._ring = FaultRing(ring_size)
        self._boards = []                 # semantic BoardStatus.as_dict() list
        self._boards_total = 0
        self._boards_ok = 0
        self._crash = None                # short-keyed crash record or None
        self._boot_count = 0
        self._seq = 0
        self._buf = bytearray(buf_size)   # reused serialize buffer

    # ---- mutate (cheap; single-writer-core per the design's mutation map) ----
    def inc(self, key, n=1):
        try:
            self._counters[key] = self._counters.get(key, 0) + n
        except Exception:
            pass

    def set_gauge(self, key, value):
        try:
            self._gauges[key] = value
        except Exception:
            pass

    def add_fault(self, up, boot, component, code, detail, board=None):
        """Record a fault (any component: mcp|net|mqtt|sys). Lock-guarded -- two cores write it."""
        try:
            with self._lock:
                return self._ring.add(up, boot, component, code, detail, board)
        except Exception:
            return None

    def note_crash(self, boot, up, cause, exc):
        self._crash = {S.CR_BOOT: boot, S.CR_UP: up,
                       S.CR_CAUSE: cause, S.CR_EXC: S.cap(exc, 160)}

    def set_boards(self, board_dicts, boards_total, boards_ok):
        """core0 publishes the per-board snapshot (semantic dicts) + counts."""
        try:
            with self._lock:
                self._boards = list(board_dicts)
                self._boards_total = boards_total
                self._boards_ok = boards_ok
        except Exception:
            pass

    def counter(self, key):
        return self._counters.get(key, 0)

    def gauge(self, key, default=None):
        return self._gauges.get(key, default)

    def ring_items(self):
        """Semantic fault records {up,boot,component,code,detail[,board]} (newest last)."""
        with self._lock:
            return self._ring.items()

    def last_fault(self):
        with self._lock:
            return self._ring.last()

    def crash(self):
        return self._crash

    @property
    def boot_count(self):
        return self._boot_count

    # ---- read (core1) ----
    def snapshot(self):
        """Stable wire dict (short keys) under lock. Pure data -- caller serializes."""
        with self._lock:
            snap = {
                S.K_V: S.SCHEMA_VERSION,
                S.K_SEQ: self._seq,
                S.K_BOOT: self._boot_count,
                S.K_COUNTERS: dict(self._counters),
                S.K_BTOTAL: self._boards_total,
                S.K_BOK: self._boards_ok,
                S.K_BOARDS_ARR: [S.board_to_wire(b) for b in self._boards],
                S.K_RING: [S.fault_to_wire(r) for r in self._ring.items()],
            }
            last = self._ring.last()
            snap[S.K_LASTFAULT] = S.fault_to_wire(last) if last else None
            for k, v in self._gauges.items():
                snap[k] = v
            if self._crash is not None:
                snap[S.K_CRASH] = self._crash
            return snap

    def serialize(self):
        """Compact JSON bytes of the current snapshot into the reused buffer. Bumps `seq`.
        Never raises -- on any failure returns None (caller skips this publish)."""
        try:
            self._seq += 1
            return dumps(self.snapshot(), self._buf)
        except Exception:
            return None

    # ---- lifecycle / persistence (core1; store injected & hardware) ----
    def load(self):
        """Restore persisted state on boot and increment boot_count. Reconciles by taking the
        MAX of each monotonic counter so a count never regresses. Never raises."""
        data = None
        if self._store is not None:
            try:
                data = self._store.load()
            except Exception:
                data = None
        if not data:
            self._boot_count += 1
            return
        try:
            self._boot_count = int(data.get("bc", 0)) + 1
            saved = data.get("c", {}) or {}
            for k in S.COUNTER_KEYS:
                self._counters[k] = max(self._counters.get(k, 0), int(saved.get(k, 0)))
            self._crash = data.get("cr")
            self._ring.restore(data.get("r", []))
        except Exception:
            # corrupt payload -> start clean but don't crash boot
            self._boot_count = self._boot_count or 1

    def _persist_payload(self):
        return {"bc": self._boot_count, "c": dict(self._counters),
                "cr": self._crash, "r": self._ring.items()}

    def checkpoint(self):
        """Cheap, frequent -> fast (scratch) tier. Never raises."""
        if self._store is None:
            return
        try:
            self._store.checkpoint(self._persist_payload())
        except Exception:
            pass

    def flush(self):
        """Rate-limited -> durable (flash) tier. Never raises."""
        if self._store is None:
            return
        try:
            self._store.flush(self._persist_payload())
        except Exception:
            pass
