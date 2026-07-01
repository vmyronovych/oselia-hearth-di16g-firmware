"""Per-MCP health bookkeeping + recovery policy (PURE -- host-testable).

No `machine`/`utime` imports: the clock (`now_ms`) is injected, exactly like
`press_detector`/`debounce`/`mcp_select`. `input_task` owns the hardware (I2C reads,
the `/RESET` pulse, the bus-clock recovery) and drives these objects; this module
only tracks state and decides *whether* to escalate recovery -- so the decision
logic is unit-tested off-board.

What lives here:
  * the stable error-code taxonomy (greppable to firmware locations from an export),
  * `classify_oserror` -- map a raised exception to (code, detail),
  * `BoardStatus` -- per-board health record (-> one entry in the diag `mcp[]` list),
  * `FaultRing` -- bounded ring of recent fault records (the diag `recent[]`),
  * `RecoveryPolicy` -- rate-limited L1(bus)->L2(reset) escalation decision,
  * `health_summary` -- the single `health` string for the diag blob / HA.
See docs/spec.md sec.12 (robustness) and the diag/state schema in INTEGRATION_SPEC.md.
"""

# ---- stable error-code taxonomy (the diag `code` field) -------------------
CODE_OK = ""
CODE_I2C_EIO = "i2c_eio"            # generic I2C NACK / EIO on read or write
CODE_I2C_TIMEOUT = "i2c_timeout"    # ETIMEDOUT (clock-stretch / wedged bus)
CODE_MCP_ABSENT = "mcp_absent"      # chip does not ACK on a bus scan
CODE_MCP_INIT_FAIL = "mcp_init_fail"  # register-config sequence raised
CODE_INT_STUCK = "int_stuck"        # shared INT held asserted despite healthy reads
CODE_BUS_RECOVERED = "bus_recovered"  # L1 recovery ran (SCL clocking)
CODE_MCP_RESET = "mcp_reset"        # L2 recovery ran (/RESET pulse)
CODE_ETH_LINK_LOST = "eth_link_lost"
CODE_MQTT_DISCONNECT = "mqtt_disconnect"
CODE_MQTT_CONNACK_REFUSED = "mqtt_connack_refused"

# errno values we special-case (MicroPython exposes them as OSError.args[0]).
_ETIMEDOUT = 110


def classify_oserror(err):
    """Map a raised exception to (code, detail). Pure; `detail` keeps the raw text
    (errno/type) so an exported blob still carries the original error."""
    name = type(err).__name__
    text = str(err)
    errno = None
    try:
        errno = err.args[0]
    except (AttributeError, IndexError, TypeError):
        errno = None
    detail = ("%s %s" % (name, text)).strip()
    if errno == _ETIMEDOUT:
        return CODE_I2C_TIMEOUT, detail
    return CODE_I2C_EIO, detail


class BoardStatus:
    """Health record for one board (= one MCP chip position). Serialises to one
    entry in the diag `mcp[]` array."""

    def __init__(self, board, addr):
        self.board = board
        self.addr = addr
        self.ok = False
        self.code = CODE_OK
        self.detail = ""
        self.fails = 0             # consecutive failed health checks (0 when ok)
        self.last_ok_ms = None     # ms of the last successful read/init (None=never)
        self.recoveries = 0        # times brought back after a recovery action

    def mark_ok(self, now_ms):
        """Record a healthy read/init. Returns True if this is a down->up edge.
        A down->up edge after the board has been healthy before counts as a
        recovery (the first-ever init is not a recovery)."""
        edge = not self.ok
        if edge and self.last_ok_ms is not None:
            self.recoveries += 1
        self.ok = True
        self.code = CODE_OK
        self.detail = ""
        self.fails = 0
        self.last_ok_ms = now_ms
        return edge

    def mark_fail(self, code, detail):
        """Record a failure. Returns True if this is an up->down edge."""
        edge = self.ok
        self.ok = False
        self.code = code
        self.detail = detail
        self.fails += 1
        return edge

    def last_ok_s(self, now_ms):
        if self.last_ok_ms is None:
            return None
        return max(0, (now_ms - self.last_ok_ms) // 1000)

    def as_dict(self, now_ms):
        return {
            "board": self.board,
            "addr": "0x%02x" % self.addr,
            "ok": self.ok,
            "code": self.code,
            "detail": self.detail,
            "fails": self.fails,
            "last_ok_s": self.last_ok_s(now_ms),
            "recoveries": self.recoveries,
        }


class FaultRing:
    """Bounded ring of recent fault records -> the diag blob's `recent[]`. Newest
    last. A single retained export then carries the *sequence* of what happened."""

    def __init__(self, size):
        self.size = max(1, int(size))
        self._items = []

    def add(self, ts, component, code, detail, board=None):
        rec = {"ts": ts, "component": component, "code": code, "detail": detail}
        if board is not None:
            rec["board"] = board
        self._items.append(rec)
        if len(self._items) > self.size:
            self._items = self._items[-self.size:]
        return rec

    def last(self):
        return dict(self._items[-1]) if self._items else None

    def recent(self):
        return [dict(r) for r in self._items]


def health_summary(eth_ok, mqtt_ok, boards_total, boards_ok):
    """Single health string for the diag blob / HA Diagnostics sensor state.

    net_fault if the link/broker is down (root cause); mcp_fault if every board is
    down; degraded if some-but-not-all boards are down; else ok.
    """
    if not eth_ok or not mqtt_ok:
        return "net_fault"
    if boards_total and boards_ok == 0:
        return "mcp_fault"
    if boards_ok < boards_total:
        return "degraded"
    return "ok"


# Reset-cause: the int->name map is pure; the read of machine.reset_cause() is done
# in main.py (hardware) and passed through this map.
def reset_cause_name(cause, names):
    """`names` is a dict {machine.<X>_RESET: "wdt"/...}; unknown -> "unknown"."""
    if cause is None:
        return "unknown"
    return names.get(cause, "unknown")


class RecoveryPolicy:
    """Decide whether to run an MCP recovery action this tick, and at what level.

    Rate-limited with EXPONENTIAL BACKOFF so we never thrash the bus/reset line.
    Escalates L1 (I2C bus clocking) -> L2 (/RESET pulse) across attempts: the first
    eligible attempt is L1; if a chip is still failing at the next eligible attempt,
    L2; thereafter L2. A stuck-INT observation bypasses the consecutive-fail gate (it
    demands prompt action), but is still rate-limited.

    The interval starts at min_interval_ms and DOUBLES after each action up to
    max_interval_ms, then resets when a board comes back healthy. This matters on a
    multi-board rig: L2 pulses the SHARED /RESET line, so a persistently-absent chip
    must not keep resetting the HEALTHY boards every interval -- the passive 2 s
    health-check still re-inits a returning chip regardless, so backing off loses
    nothing. See docs/spec.md sec.12 and input_task.
    """

    def __init__(self, after_fails, min_interval_ms, max_interval_ms=None):
        self.after_fails = after_fails
        self.min_interval_ms = min_interval_ms
        self.max_interval_ms = max(min_interval_ms, max_interval_ms or min_interval_ms)
        self._last_ms = None       # ms of the last recovery action (None=never)
        self._last_level = 0       # 0/1/2 -- last level run while the fault persists
        self._interval = min_interval_ms   # current backoff interval

    def decide(self, now_ms, failing, fail_streak, int_stuck):
        """failing: any board not ok. fail_streak: max consecutive health-check
        fails across boards. int_stuck: a stuck-INT was just observed this tick.
        Returns 0 (do nothing), 1 (bus recovery) or 2 (hardware reset)."""
        if not failing and not int_stuck:
            self._last_level = 0           # healthy again -> reset the ladder
            self._interval = self.min_interval_ms
            return 0
        if not int_stuck and fail_streak < self.after_fails:
            return 0                       # not bad enough yet (and no stuck INT)
        if self._last_ms is not None and \
                (now_ms - self._last_ms) < self._interval:
            return 0                       # rate-limited (with backoff)
        level = 1 if self._last_level < 1 else 2
        self._last_ms = now_ms
        self._last_level = level
        self._interval = min(self._interval * 2, self.max_interval_ms)  # back off
        return level

    def note_recovered(self):
        """Call when the bus is healthy again so the next fault starts at L1 and the
        backoff interval is reset."""
        self._last_level = 0
        self._interval = self.min_interval_ms
