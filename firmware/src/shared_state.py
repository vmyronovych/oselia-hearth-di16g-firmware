"""Cross-core shared health/status, guarded by a lock.

core0 (input) writes: mcp_ok.
core1 (network) writes: eth_ok, mqtt_ok, ready, and a periodic heartbeat tick.
core0 reads the heartbeat to gate the watchdog; core1 reads mcp_ok to drive the LED.

Single-word reads/writes are effectively atomic on RP2040, but grouped updates go
through the lock for correctness. The lock is injected for host testing.
"""


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
        # input_task and SPEC.md sec.5.4.
        self.tune_version = 0
        self.tune_long_ms = 0
        self.tune_double_gap_ms = 0
        self.tune_debounce_ms = 0

    def set_mcp(self, ok):
        with self._lock:
            self.mcp_ok = ok

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
