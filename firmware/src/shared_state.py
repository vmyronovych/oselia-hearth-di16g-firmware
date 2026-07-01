"""Cross-core shared health/status, guarded by a lock.

core0 (input) writes: mcp_ok, the resolved board set, the per-board MCP diagnostic
snapshot (built from mcp_health records), and fault records.
core1 (network) writes: eth_ok, mqtt_ok, ready, and a periodic heartbeat tick; it
reads the MCP snapshot to publish diag/state + diag/event.
core0 reads the heartbeat to gate the watchdog; core1 reads mcp_ok to drive the LED.

Single-word reads/writes are effectively atomic on RP2040, but grouped updates go
through the lock for correctness. The lock is injected for host testing.

Observability handshake (publish-on-change): core0 bumps `mcp_change_version` on any
board health edge so core1 publishes diag/state immediately, and bumps
`event_version` + stows `last_fault` on each NEW fault so core1 publishes diag/event.
Both are plain ints (atomic read on RP2040); core1 compares them each pass.
"""


def _empty_mcp_diag():
    return {
        "mcp": [],
        "boards_total": 0,
        "boards_ok": 0,
        "counters": {"int_stuck": 0, "bus_recoveries": 0, "mcp_resets": 0},
        "last_fault": None,
        "recent": [],
    }


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class SharedState:
    def __init__(self, lock=None):
        self._lock = lock if lock is not None else _NullLock()
        self.mcp_ok = False
        self.eth_ok = False
        self.mqtt_ok = False
        self.ready = False           # network init finished -> ok to arm WDT
        self.core1_heartbeat = 0     # ticks_ms of core1's last loop pass
        # Live-tunable gesture timings: core1 writes (on an HA command) and bumps
        # tune_version; core0 reads tune_version each pass (plain int -> atomic) and
        # only takes the lock to re-read the set when it changed. See net_task /
        # input_task and docs/spec.md sec.5.4.
        self.tune_version = 0
        self.tune_long_ms = 0
        self.tune_double_gap_ms = 0
        self.tune_debounce_ms = 0
        # Resolved MCP topology (core0 writes after the boot scan; core1 waits on
        # boards_resolved before its first HA discovery / diag publish).
        self.n_boards = 0
        self.board_addrs = []            # ["0x20", ...] in board order
        self.boards_resolved = False
        # Per-board MCP diagnostic snapshot (built by core0; serialised by core1).
        self.mcp_diag = _empty_mcp_diag()
        self.mcp_change_version = 0       # bumped on a board health edge -> publish now
        self.event_version = 0            # bumped on each new fault -> publish diag/event
        self.last_fault = None            # the latest fault record (for diag/event)
        self.reset_cause = "unknown"      # set once at boot from machine.reset_cause()

    def set_mcp(self, ok):
        with self._lock:
            self.mcp_ok = ok

    # ---- MCP topology + per-board diagnostics (core0 writes, core1 reads) ----
    def set_boards(self, n, addrs):
        """Publish the resolved board count/addresses; unblocks core1's discovery."""
        with self._lock:
            self.n_boards = n
            self.board_addrs = list(addrs)
            self.boards_resolved = True

    def boards_info(self):
        with self._lock:
            return self.n_boards, list(self.board_addrs), self.boards_resolved

    def set_reset_cause(self, name):
        self.reset_cause = name           # single write at boot; plain assignment

    def set_mcp_diag(self, snapshot, changed=False):
        """Store the per-board diagnostic snapshot built by core0. `changed=True`
        (a board health edge) bumps the change version so core1 publishes now."""
        with self._lock:
            self.mcp_diag = snapshot
            if changed:
                self.mcp_change_version += 1

    def mcp_diag_snapshot(self):
        with self._lock:
            return self.mcp_diag

    def note_fault(self, record):
        """Stow the latest fault record + bump the event version (core1 publishes
        diag/event when it sees the bump)."""
        with self._lock:
            self.last_fault = record
            self.event_version += 1

    def take_fault(self):
        with self._lock:
            return self.event_version, self.last_fault

    def set_net(self, eth_ok=None, mqtt_ok=None):
        with self._lock:
            if eth_ok is not None:
                self.eth_ok = eth_ok
            if mqtt_ok is not None:
                self.mqtt_ok = mqtt_ok

    def beat(self, ticks):
        # plain write; single word
        self.core1_heartbeat = ticks

    def init_tunables(self, long_ms, double_gap_ms, debounce_ms):
        """Seed from config at boot (version 1) before core1 is spawned."""
        with self._lock:
            self.tune_long_ms = long_ms
            self.tune_double_gap_ms = double_gap_ms
            self.tune_debounce_ms = debounce_ms
            self.tune_version = 1

    def set_tunables(self, long_ms=None, double_gap_ms=None, debounce_ms=None):
        """Update one/more timings and bump the version so core0 re-applies them."""
        with self._lock:
            if long_ms is not None:
                self.tune_long_ms = long_ms
            if double_gap_ms is not None:
                self.tune_double_gap_ms = double_gap_ms
            if debounce_ms is not None:
                self.tune_debounce_ms = debounce_ms
            self.tune_version += 1
            return self.tune_version

    def tunables(self):
        """Locked snapshot -> (version, long_ms, double_gap_ms, debounce_ms)."""
        with self._lock:
            return (self.tune_version, self.tune_long_ms,
                    self.tune_double_gap_ms, self.tune_debounce_ms)

    def health(self):
        """Snapshot dict for the LED renderer."""
        with self._lock:
            return {
                "ethernet": self.eth_ok,
                "mqtt": self.mqtt_ok,
                "mcp": self.mcp_ok,
            }
