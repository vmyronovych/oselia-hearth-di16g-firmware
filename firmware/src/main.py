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


def _resolve_mcp(i2c):
    """Scan the bus (unless pinned) and return the MCP address list to drive.

    Bounded retry because satellite chips may power up just after the MCU. Falls
    back to cfg.MCP_ADDRESSES inside select_addresses if a scan finds nothing.
    """
    import utime
    if not cfg.MCP_AUTODISCOVER:
        return list(cfg.MCP_ADDRESSES)
    scanned = []
    for _ in range(5):
        try:
            scanned = list(i2c.scan())
        except Exception:
            scanned = []
        if any(mcp_select.MCP_ADDR_MIN <= a <= mcp_select.MCP_ADDR_MAX
               for a in scanned):
            break
        utime.sleep_ms(200)
    return mcp_select.select_addresses(scanned, cfg.MCP_ADDRESSES,
                                       cfg.MCP_AUTODISCOVER)


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
    queue = EventQueue(cfg.EVENT_QUEUE_SIZE, lock)

    # Resolve the MCP boards once, here, before spawning core1 -- so both cores
    # agree on the count (core1 needs it for HA discovery) with no cross-core race.
    i2c = input_task.build_i2c()
    mcp_addrs = _resolve_mcp(i2c)
    _validate_addresses(mcp_addrs, "resolved MCP boards")
    n_boards = len(mcp_addrs)
    log.info("MCP boards: %d (%s)%s" % (
        n_boards, ",".join("0x%02x" % a for a in mcp_addrs),
        " autodiscover" if cfg.MCP_AUTODISCOVER else " pinned"))

    # Warm up modules that are otherwise imported lazily at runtime, BEFORE
    # starting core1. The RP2040 import lock + GIL deadlock if both cores `import`
    # at the same instant; after spawning, input_task's first line
    # (clock.from_utime -> import utime) would race net_task's status_led
    # `import neopixel`. Importing them here on the main thread removes the race.
    # HW-VERIFY: confirmed on hardware -- without this, boot hangs right after the
    # "MCP boards" log (core1 never reaches "configuring CH9120...").
    import utime                          # noqa: F401  (clock.from_utime, both cores)
    try:
        import neopixel                   # noqa: F401  (status_led lazy import, core1)
    except ImportError:
        pass

    # Spawn the networking task on core 1.
    def _net():
        try:
            net_task.run(shared, queue, device_id, mcp_addrs)
        except Exception as e:           # keep the failure visible; core0/WDT react
            log.error("net_task crashed: %s" % e)

    try:
        _thread.stack_size(16 * 1024)    # core1 does MQTT/JSON; default is tight
    except Exception:
        pass
    _thread.start_new_thread(_net, ())

    # Run the real-time input loop on core 0 (this thread) forever.
    input_task.run(shared, queue, i2c, mcp_addrs)


if __name__ == "__main__":
    main()
