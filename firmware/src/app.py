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
import log


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
    """Why we last booted, as a name for the diag blob. On RP2040 only "power_on" and "wdt"
    occur (SOFT_RESET/HARD_RESET aren't defined on this build). NOTE: machine.reset() is itself
    implemented via the watchdog on RP2040, so "wdt" means EITHER a real watchdog stall OR any
    deliberate machine.reset() (Restart / OTA / maintenance / crash-recovery) -- it is NOT a
    reliable "did the watchdog catch a hang?" signal. Ports without reset_cause() -> "unknown"."""
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


def main():
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

    # Network FIRST: spawn core1 before any I2C work so the CH9120/MQTT bring-up is
    # never gated on the bus. core0 (input_task) then builds I2C, resolves the board
    # set, and publishes it via SharedState; core1 waits (bounded) for that before
    # its first discovery/diag publish. An MCP fault can no longer delay the network.
    def _net():
        try:
            net_task.run(shared, queue, device_id)
        except Exception as e:           # keep the failure visible; core0/WDT react
            log.error("net_task crashed: %s" % e)

    try:
        _thread.stack_size(16 * 1024)    # core1 does MQTT/JSON; default is tight
    except Exception:
        pass
    _thread.start_new_thread(_net, ())

    # Run the real-time input loop on core 0 (this thread) forever. It owns I2C +
    # board resolution + MCP recovery now.
    input_task.run(shared, queue)


if __name__ == "__main__":
    main()
