"""Pure helpers that assemble the machine-owned site.json from the installer's answers.

No I/O here -- these are unit-tested on the host. The firmware overlays site.json on top
of its fixed hardware defaults (firmware/src/config.py), so only the per-install kernel
lives here. board.write_site_atomic() does the actual write.
"""
import ipaddress

from .constants import MAX_BOARDS, MCP_BASE_ADDR


def is_valid_ipv4(s):
    try:
        return isinstance(ipaddress.ip_address(s), ipaddress.IPv4Address)
    except ValueError:
        return False


def board_count_to_addrs(n):
    """1..MAX_BOARDS -> list of MCP I2C addresses [0x20, 0x21, ...]."""
    if not 1 <= n <= MAX_BOARDS:
        raise ValueError("board count must be 1..%d" % MAX_BOARDS)
    return [MCP_BASE_ADDR + i for i in range(n)]


def build_site_dict(broker_ip, broker_port, user, password,
                    board_count=None, use_dhcp=True, static=None, diag=True,
                    acceptance_hooks=False):
    """Assemble site.json. `board_count` None -> firmware auto-discovers the I2C boards
    (key omitted). `static` (if given) = {"ip","gateway","mask"} and forces DHCP off.
    `diag` only written when False (default on), to keep the file minimal.

    `ha_integration` is ALWAYS written as "oselia". Current firmware no longer reads this
    key (the legacy MQTT-discovery path was removed -- the OSELIA custom integration is the
    only supported HA path), but we still write it so a board carrying OLDER firmware --
    which defaulted to "mqtt" and still honours the override -- is forced out of publishing
    HA discovery. A newer unit simply ignores the unknown key."""
    if not is_valid_ipv4(broker_ip):
        raise ValueError("broker_ip must be numeric IPv4, got %r" % broker_ip)
    site = {
        "broker_ip": broker_ip,
        "broker_port": int(broker_port),
        "mqtt_user": user or None,
        "mqtt_pass": password or None,
        "use_dhcp": bool(use_dhcp) and static is None,
        "ha_integration": "oselia",
    }
    if board_count is not None:
        site["board_count"] = int(board_count)
    if static is not None:
        for k in ("ip", "gateway", "mask"):
            if not is_valid_ipv4(static[k]):
                raise ValueError("static %s must be IPv4, got %r" % (k, static[k]))
        site["static"] = {k: static[k] for k in ("ip", "gateway", "mask")}
        site["use_dhcp"] = False
    if not diag:
        site["diag"] = False                 # default on; only record the opt-out
    if acceptance_hooks:
        site["acceptance_hooks"] = True      # bench-only; production never sets this
    return site
