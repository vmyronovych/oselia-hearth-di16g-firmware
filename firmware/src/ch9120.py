"""CH9120 UART-to-Ethernet bridge driver.

The CH9120 holds the TCP/IP stack. We configure it once (serial config mode) as a
TCP client pointed at the MQTT broker, then switch to transparent mode where the
UART byte stream == the TCP byte stream to/from the broker. See SPEC.md sec.4.

Control pins (from config.py):
  CFG0  LOW  -> serial configuration mode   (GP18)
  CFG0  HIGH -> transparent transmission mode
  RST   LOW  -> reset (active low)           (GP19)
  TCPCS LOW  -> TCP connected (read-only)    (GP17, not used by the POC)

Command framing and the config/exit sequence below are CONFIRMED working, taken
from the reference POC (dib/app: ch9120.py + RP2040-ETH-MQTT.py). The POC performs
configuration at 9600 baud, then re-opens the UART at 115200 for transparent mode.
"""
from machine import UART, Pin
import time

# Operating modes
TCP_SERVER = 0
TCP_CLIENT = 1
UDP_SERVER = 2
UDP_CLIENT = 3

# Config command framing (POC-confirmed)
_HEAD = b"\x57\xab"
_OP_MODE = 0x10
_OP_LOCAL_IP = 0x11
_OP_SUBNET = 0x12
_OP_GATEWAY = 0x13
_OP_LOCAL_PORT = 0x14
_OP_TARGET_IP = 0x15
_OP_TARGET_PORT = 0x16
_OP_BAUD = 0x21
_OP_DHCP = 0x33   # DHCP on/off. Param 0x01 = obtain IP via DHCP, 0x00 = static.
                  # Opcode + param per the Waveshare "CH9120 Serial Control
                  # Instruction Set"; CONFIRMED on hardware (board leased an IP and
                  # reached the broker online with USE_DHCP=True).
_OP_GET_IP = 0x61  # READ current IP (the DHCP lease when DHCP is on). Replies with
                   # the 4 IP bytes (e.g. C0 A8 01 10 = 192.168.1.16). From the
                   # Waveshare instruction set; CONFIRMED on hardware 2026-06 (DHCP
                   # board read back its lease 192.168.1.134; trailing 4 bytes = IP).
# exit/save sequence (written in config mode, then CFG0 -> HIGH). Per the
# instruction set: 0x0D = save params to EEPROM, 0x0E = execute config + reset the
# chip (this is what applies the DHCP setting), 0x5E = leave serial-config mode.
_EXIT_SEQ = (b"\x0D", b"\x0E", b"\x5E")


class CH9120:
    def __init__(self, pin_cfg0, pin_rst, pin_tcpcs=None):
        self.cfg0 = Pin(pin_cfg0, Pin.OUT, Pin.PULL_UP)
        self.rst = Pin(pin_rst, Pin.OUT, Pin.PULL_UP)
        self.tcpcs = (Pin(pin_tcpcs, Pin.IN, Pin.PULL_UP)
                      if pin_tcpcs is not None else None)

    def _cmd(self, uart, opcode, payload=b""):
        uart.write(_HEAD + bytes([opcode]) + payload)
        time.sleep_ms(100)

    def enter_config(self):
        self.rst.value(1)
        self.cfg0.value(0)
        time.sleep_ms(500)

    def exit_config(self, uart):
        for b in _EXIT_SEQ:
            uart.write(_HEAD + b)
            time.sleep_ms(100)
        self.cfg0.value(1)
        time.sleep_ms(100)

    def configure(self, uart, cfg):
        """Send all settings as TCP client. `uart` must be at UART_CONFIG_BAUD."""
        self.enter_config()
        self._cmd(uart, _OP_MODE, bytes([TCP_CLIENT]))
        # DHCP vs static identity. With DHCP on, the CH9120 leases its own address,
        # so the static IP/subnet/gateway are not programmed (it ignores them). This
        # is what lets the installer skip IP planning (PROVISIONING_SPEC.md sec.2).
        self._cmd(uart, _OP_DHCP, bytes([1 if cfg.USE_DHCP else 0]))
        if not cfg.USE_DHCP:
            self._cmd(uart, _OP_LOCAL_IP, bytes(cfg.LOCAL_IP))
            self._cmd(uart, _OP_SUBNET, bytes(cfg.SUBNET_MASK))
            self._cmd(uart, _OP_GATEWAY, bytes(cfg.GATEWAY))
        self._cmd(uart, _OP_LOCAL_PORT, cfg.CH9120_LOCAL_PORT.to_bytes(2, "little"))
        self._cmd(uart, _OP_TARGET_IP, bytes(cfg.BROKER_IP))
        self._cmd(uart, _OP_TARGET_PORT, cfg.BROKER_PORT.to_bytes(2, "little"))
        self._cmd(uart, _OP_BAUD, cfg.UART_BAUD.to_bytes(4, "little"))
        self.exit_config(uart)

    def is_connected(self):
        """True if TCPCS reports connected. If TCPCS isn't wired, assume True."""
        if self.tcpcs is None:
            return True
        return self.tcpcs.value() == 0

    def leave_config(self, uart):
        """Leave serial-config mode WITHOUT the save (0x0D) / execute-reset (0x0E)
        steps -- so a read-only round-trip doesn't reset the chip and trigger a new
        DHCP lease. Just 0x5E (exit) then CFG0 high."""
        uart.write(_HEAD + b"\x5e")
        time.sleep_ms(100)
        self.cfg0.value(1)
        time.sleep_ms(100)

    def read_ip(self, uart):
        """Read the CH9120's current IP via 0x61 -> 4-tuple, or None on failure.

        `uart` must be open at UART_CONFIG_BAUD. Enters config mode, queries, and
        leaves WITHOUT save/execute (no reset/re-lease). Tolerates an echo prefix by
        taking the trailing 4 bytes; 0.0.0.0 (lease not ready) -> None.
        CONFIRMED on hardware 2026-06: a DHCP board read back its lease via 0x61
        (trailing 4 bytes = the IP). Costs one boot-time reconnect because this
        config-mode dip interrupts the CH9120's first transparent TCP attempt."""
        self.enter_config()
        try:
            uart.read()                      # flush any pending bytes
        except Exception:
            pass
        uart.write(_HEAD + bytes([_OP_GET_IP]))
        time.sleep_ms(150)
        data = uart.read()
        self.leave_config(uart)
        if data and len(data) >= 4:
            ip = tuple(data[-4:])
            if ip != (0, 0, 0, 0):
                return ip
        return None


def bring_up(cfg, read_ip=False):
    """Full sequence -> returns (transparent_uart, ch9120, leased_ip).

    Mirrors the POC: open UART at config baud, push settings, flush, then re-open
    the UART at the transparent baud. The returned UART is what net_stream wraps.
    `leased_ip` is a 4-tuple (or None) -- only populated when `read_ip` is set.

    DHCP note: when USE_DHCP is set, after the 0x0E reset the CH9120 must obtain a
    lease before it can open the TCP client to the broker -- that can take a couple
    of seconds beyond the 500 ms settle here. We don't block for it: net_task's
    connect retry/backoff loop simply tries again until the link comes up, so a
    slow lease just delays the first connect rather than failing bring-up.

    IP read-back (`read_ip=True`, diagnostics): with DHCP the MCU doesn't know the
    leased address. We optionally read it ONCE here -- after a settle for the lease
    -- in a read-only config round-trip (no reset). This runs only on the initial
    bring-up (never on reconnect, so it never slows a recovery) and is best-effort:
    any failure leaves leased_ip None and diagnostics falls back to "dhcp".
    """
    ch = CH9120(cfg.PIN_CH9120_CFG0, cfg.PIN_CH9120_RST, cfg.PIN_CH9120_TCPCS)
    uart = UART(cfg.PIN_CH9120_UART_ID, baudrate=cfg.UART_CONFIG_BAUD,
                tx=Pin(cfg.PIN_CH9120_TX), rx=Pin(cfg.PIN_CH9120_RX))
    ch.configure(uart, cfg)
    time.sleep_ms(500)
    try:
        uart.read()              # flush any config-mode echo
    except Exception:
        pass

    leased_ip = None
    if read_ip and cfg.USE_DHCP:
        time.sleep_ms(getattr(cfg, "DHCP_LEASE_SETTLE_MS", 4000))  # let DHCP settle
        try:
            cfg_uart = UART(cfg.PIN_CH9120_UART_ID, baudrate=cfg.UART_CONFIG_BAUD,
                            tx=Pin(cfg.PIN_CH9120_TX), rx=Pin(cfg.PIN_CH9120_RX))
            leased_ip = ch.read_ip(cfg_uart)
        except Exception:
            leased_ip = None

    uart = UART(cfg.PIN_CH9120_UART_ID, baudrate=cfg.UART_BAUD,
                tx=Pin(cfg.PIN_CH9120_TX), rx=Pin(cfg.PIN_CH9120_RX))
    return uart, ch, leased_ip
