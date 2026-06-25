"""Tiny leveled logger over USB serial (print), with rate-limiting.

Levels: 0=ERROR 1=WARN 2=INFO 3=DEBUG. Set the threshold once via set_level().
Rate-limiting collapses repeated identical keys (e.g. a reconnect loop) so the
serial console isn't flooded.
"""
try:
    import utime as _t
except ImportError:
    import time as _t
    if not hasattr(_t, "ticks_ms"):
        _t.ticks_ms = lambda: int(_t.time() * 1000)
        _t.ticks_diff = lambda a, b: a - b

ERROR, WARN, INFO, DEBUG = 0, 1, 2, 3
_NAMES = ("E", "W", "I", "D")

_level = INFO
_last = {}          # key -> last-emitted ticks_ms
_sink = None        # optional fn(lvl, msg) mirror (e.g. -> MQTT/HA), set by net_task


def set_level(level):
    global _level
    _level = level


def get_level():
    return _level


def level_name(lvl):
    """Single-letter name for a level int (E/W/I/D); '?' if out of range."""
    return _NAMES[lvl] if 0 <= lvl < len(_NAMES) else "?"


def set_sink(fn):
    """Register an optional mirror called for every emitted line (after the level
    gate), as fn(lvl, msg). Used to surface logs in Home Assistant. It may be
    called from EITHER core, so it must be cheap and non-blocking -- just stash the
    line; the actual MQTT publish happens on core1 in net_task's queue-gated loop."""
    global _sink
    _sink = fn


def _emit(lvl, msg):
    if lvl <= _level:
        try:
            print("[{}] {}".format(_NAMES[lvl], msg))
        except Exception:
            pass
        if _sink is not None:
            try:
                _sink(lvl, msg)
            except Exception:
                pass


def log(lvl, msg, every_ms=0, key=None):
    """Log msg at level lvl. If every_ms>0, suppress repeats of `key` within the
    window (key defaults to msg)."""
    if lvl > _level:
        return
    if every_ms > 0:
        k = key if key is not None else msg
        now = _t.ticks_ms()
        prev = _last.get(k)
        if prev is not None and _t.ticks_diff(now, prev) < every_ms:
            return
        _last[k] = now
    _emit(lvl, msg)


def error(msg, **kw):
    log(ERROR, msg, **kw)


def warn(msg, **kw):
    log(WARN, msg, **kw)


def info(msg, **kw):
    log(INFO, msg, **kw)


def debug(msg, **kw):
    log(DEBUG, msg, **kw)
