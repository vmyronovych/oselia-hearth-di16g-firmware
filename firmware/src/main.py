"""Entry point: dual-core orchestration.

  Core 1 (spawned thread): net_task.run  -- CH9120 link, MQTT, discovery, LED,
      drains the event queue.
  Core 0 (this thread):    input_task.run -- MCP23017 IRQ, debounce, gesture
      detection, pushes events to the queue, owns the watchdog.

They communicate only through a thread-safe EventQueue (gestures) and a lock-guarded
SharedState (health + heartbeat). See SPEC.md sec.3a (concurrency) and sec.12
(robustness).
"""
import _thread

import config as cfg
from event_queue import EventQueue
from shared_state import SharedState
import input_task
import net_task
import mcp_select
import metrics as metrics_mod
import metrics_store
import clock
import log

try:
    import ujson as json
except ImportError:
    import json


def _device_id():
    if cfg.DEVICE_ID:
        return cfg.DEVICE_ID
    import machine
    import ubinascii
    return ubinascii.hexlify(machine.unique_id()).decode()[-6:].upper()


def _validate_addresses(addrs, what="MCP_ADDRESSES"):
    assert 1 <= len(addrs) <= mcp_select.MAX_BOARDS, \
        "%s: 1..%d chips" % (what, mcp_select.MAX_BOARDS)
    assert len(set(addrs)) == len(addrs), what + " must be distinct"
    for a in addrs:
        assert mcp_select.MCP_ADDR_MIN <= a <= mcp_select.MCP_ADDR_MAX, \
            "MCP address 0x%02x out of 0x20..0x27" % a


def _validate_config():
    assert len(cfg.BROKER_IP) == 4, "BROKER_IP must be a 4-tuple (numeric, no DNS)"
    assert 1 <= cfg.BROKER_PORT <= 65535, "BROKER_PORT out of range"
    for name in ("LOCAL_IP", "GATEWAY", "SUBNET_MASK"):
        assert len(getattr(cfg, name)) == 4, name + " must be a 4-tuple"
    assert cfg.WDT_TIMEOUT_MS <= 8388, "RP2040 WDT max is ~8388 ms"
    assert cfg.CORE1_STALL_MS < cfg.WDT_TIMEOUT_MS, \
        "CORE1_STALL_MS must be < WDT_TIMEOUT_MS"
    assert cfg.EVENT_QUEUE_SIZE >= 1
    _validate_addresses(cfg.MCP_ADDRESSES)   # the explicit / fallback list


def _reset_cause():
    """Why we last booted, as a name for the diag blob ("power_on"|"wdt"|"soft"|...).
    A `wdt` value is the direct answer to "did the watchdog reboot the Hearth?".
    Best-effort: ports that don't implement machine.reset_cause() -> "unknown".
    HW-VERIFY: confirm the rp2 build reports WDT_RESET after a forced stall."""
    try:
        import machine
        cause = machine.reset_cause()
    except Exception:
        return "unknown"
    names = {}
    for attr, name in (("PWRON_RESET", "power_on"), ("WDT_RESET", "wdt"),
                       ("HARD_RESET", "hard"), ("SOFT_RESET", "soft"),
                       ("DEEPSLEEP_RESET", "deepsleep")):
        v = getattr(machine, attr, None)
        if v is not None:
            names[v] = name
    return names.get(cause, "unknown")


_OTA_STATE = getattr(cfg, "OTA_STATE_PATH", "/ota/state")


def _build_metrics(mono):
    """Construct the metrics registry + its persistent store. The store touches hardware
    (watchdog scratch + flash); if anything is unavailable we degrade to an in-RAM registry
    rather than fail boot -- metrics must never break the product."""
    store = None
    try:
        scratch = None
        try:
            scratch = metrics_store.ScratchStore()
        except Exception:
            scratch = None
        flash = metrics_store.FlashStore(
            getattr(cfg, "METRICS_STATE_PATH", "/metrics_state.json"))
        store = metrics_store.PersistentStore(
            scratch=scratch, flash=flash, clock_ms=mono.ms,
            flush_interval_ms=getattr(cfg, "METRICS_FLUSH_INTERVAL_MS", 300000))
    except Exception as e:
        log.warn("metrics store unavailable (%s); telemetry not persisted" % e)
        store = None
    mtr = metrics_mod.Metrics(store=store,
                              ring_size=getattr(cfg, "DIAG_FAULT_RING", 16),
                              lock=_thread.allocate_lock())
    try:
        mtr.load()                       # restore counters/boot_count/ring; bumps boot_count
    except Exception as e:
        log.warn("metrics load failed: %s" % e)
    return mtr


def _surface_boot_crash(mtr, reset_cause):
    """If boot.py recorded a crash (traceback excerpt in /ota/state), surface it as the
    registry's last_crash so it rides out in telemetry. boot.py stays dependency-free; the
    app side reads its file here. Best-effort -- never raises."""
    try:
        with open(_OTA_STATE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return
    lc = st.get("last_crash")
    if not lc:
        return
    exc = lc if isinstance(lc, str) else lc.get("exc", "")
    mtr.note_crash(0, 0, reset_cause, exc)      # crash boot/up unknown from boot.py
    # Surface it only on the recovered boot: clear it so a stale crash isn't reported forever.
    try:
        import os
        st.pop("last_crash", None)
        with open(_OTA_STATE + ".tmp", "w") as f:
            json.dump(st, f)
        os.rename(_OTA_STATE + ".tmp", _OTA_STATE)
    except OSError:
        pass


def main():
    try:
        import micropython
        micropython.alloc_emergency_exception_buf(100)   # usable traceback from ISRs
    except Exception:
        pass
    log.set_level(cfg.LOG_LEVEL)
    _validate_config()
    device_id = _device_id()
    log.info("OSELIA Hearth %s id=%s" % (cfg.SW_VERSION, device_id))

    lock = _thread.allocate_lock()
    shared = SharedState(_thread.allocate_lock())
    # Seed live-tunable timings from config BEFORE core1 spawns, so both cores agree
    # on the initial values with no startup race (core1 may publish/handle commands).
    shared.init_tunables(cfg.LONG_MS, cfg.DOUBLE_GAP_MS, cfg.DEBOUNCE_MS)
    shared.set_reset_cause(_reset_cause())
    queue = EventQueue(cfg.EVENT_QUEUE_SIZE, lock)

    # Warm up modules that are otherwise imported lazily at runtime, BEFORE
    # starting core1. The RP2040 import lock + GIL deadlock if both cores `import`
    # at the same instant; after spawning, input_task's clock.from_utime -> import
    # utime would race net_task's status_led `import neopixel`. Importing them here
    # on the main thread removes the race.
    # HW-VERIFY: confirmed on hardware -- without this, boot hangs (core1 never
    # reaches "configuring CH9120...").
    import utime                          # noqa: F401  (clock.from_utime, both cores)
    try:
        import neopixel                   # noqa: F401  (status_led lazy import, core1)
    except ImportError:
        pass

    # The single metrics registry both cores share (own namespace; persists across reboot;
    # never freezes the loop -- see metrics.py). Built after the import warm-up so its
    # clock/scratch/flash use of utime/machine doesn't race core1's spawn.
    #
    # CONTAINED: metrics must NEVER be able to crash boot. An uncaught error here would fall
    # through to boot.py -> machine.reset(), and on THIS board a reset can DROP USB-CDC until a
    # cold BOOTSEL reflash (OTA_SPEC.md). So on ANY failure we fall back to a pure in-RAM
    # registry (no persistence) and keep booting -- telemetry degrades, the product does not.
    try:
        mono = clock.from_utime()
        mtr = _build_metrics(mono)
        _surface_boot_crash(mtr, shared.reset_cause)
    except Exception as e:
        try:
            log.error("metrics setup failed; running in-RAM, no persistence: %s" % e)
        except Exception:
            pass
        mtr = metrics_mod.Metrics()      # allocation-only; never touches hardware

    # Network FIRST: spawn core1 before any I2C work so the CH9120/MQTT bring-up is
    # never gated on the bus. core0 (input_task) then builds I2C, resolves the board
    # set, and publishes it via SharedState; core1 waits (bounded) for that before
    # its first discovery/diag publish. An MCP fault can no longer delay the network.
    def _net():
        try:
            net_task.run(shared, queue, device_id, mtr)
        except Exception as e:           # keep the failure visible; core0/WDT react
            log.error("net_task crashed: %s" % e)

    try:
        _thread.stack_size(16 * 1024)    # core1 does MQTT/JSON; default is tight
    except Exception:
        pass
    _thread.start_new_thread(_net, ())

    # Run the real-time input loop on core 0 (this thread) forever. It owns I2C +
    # board resolution + MCP recovery now.
    input_task.run(shared, queue, mtr)


if __name__ == "__main__":
    main()
