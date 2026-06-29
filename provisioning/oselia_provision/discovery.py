"""Broker discovery: mDNS (`_mqtt._tcp`) first, then a verified LAN port scan, then
validated manual entry. zeroconf is optional -- without it the wizard falls back to the
LAN scan + manual entry. Also hostname->IPv4 resolution (the board's CH9120 does no DNS,
so whatever we write to site.json must be numeric)."""
import socket

from . import console, mqtt
from .constants import DEFAULT_BROKER_PORT
from .siteconfig import is_valid_ipv4


def resolve_to_ipv4(host):
    """A typed hostname is resolved on the laptop; the numeric result is what we write to
    the board. -> ip string or None."""
    if is_valid_ipv4(host):
        return host
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


# ---- mDNS -----------------------------------------------------------------
def _have_zeroconf():
    try:
        import zeroconf  # noqa: F401
        return True
    except ImportError:
        return False


def _browse_mdns(services, timeout, default_port):
    """Browse the whole timeout window for the given mDNS service type(s) -> a deduped,
    ordered list of (ip, port). Returns None when zeroconf is unavailable (callers
    distinguish that from [] = none answered)."""
    if not _have_zeroconf():
        return None
    from zeroconf import Zeroconf, ServiceBrowser
    order, seen = [], set()

    class _L:
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=2000)
            if info and info.parsed_addresses():
                port = info.port or default_port
                for a in info.parsed_addresses():
                    if is_valid_ipv4(a) and (a, port) not in seen:
                        seen.add((a, port))
                        order.append((a, port))

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    import time
    zc = Zeroconf()
    try:
        ServiceBrowser(zc, list(services), _L())
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(0.2)
    finally:
        zc.close()
    return order


def discover_brokers_mdns(timeout=4.0):
    """MQTT brokers advertised on the LAN (`_mqtt._tcp`). -> list[(ip,port)] | None
    (None = zeroconf unavailable, distinct from [] = none answered)."""
    return _browse_mdns(["_mqtt._tcp.local."], timeout, DEFAULT_BROKER_PORT)


def discover_ha_instances_mdns(timeout=4.0):
    """Home Assistant instances on the LAN (`_home-assistant._tcp`). -> list[(ip,port)]
    | None."""
    return _browse_mdns(["_home-assistant._tcp.local."], timeout, 8123)


# ---- LAN port scan (fallback when nothing advertises) ---------------------
def probe_ha(host):
    """(host, 8123) if it looks like Home Assistant (HTTP on 8123; `GET /api/` -> 401/200/
    403 -- a token-protected API, not a random web server), else None."""
    try:
        sock = socket.create_connection((host, 8123), timeout=0.5)
    except OSError:
        return None
    try:
        sock.settimeout(1.5)
        sock.sendall(("GET /api/ HTTP/1.0\r\nHost: %s\r\n\r\n" % host).encode())
        line = sock.recv(128).split(b"\r\n", 1)[0]
        if any(code in line for code in (b" 401", b" 200", b" 403")):
            return (host, 8123)
    except OSError:
        pass
    finally:
        sock.close()
    return None



def _primary_ipv4():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def scan_lan(probe, workers=128):
    """Probe every host in the laptop's /24 with `probe(host)->(ip,port)|None`. Sorted
    matches; [] if the subnet can't be determined."""
    ip = _primary_ipv4()
    if not ip or ip.startswith("127."):
        return []
    base = ip.rsplit(".", 1)[0] + "."
    import concurrent.futures as cf
    hosts = [base + str(i) for i in range(1, 255)]
    found = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(probe, hosts):
            if r:
                found.append(r)
    return sorted(found)


# ---- the broker question --------------------------------------------------
def prompt_broker(broker_arg, existing):
    """Resolve the broker: --broker wins; else mDNS -> LAN scan -> manual entry. Returns
    (ip, port). Honours console interactivity for the menu/manual paths."""
    if broker_arg:
        host, _, port = broker_arg.partition(":")
        ip = resolve_to_ipv4(host)
        if not ip:
            console.die("could not resolve --broker host %r" % host)
        return ip, int(port or DEFAULT_BROKER_PORT)

    default_ip = (existing or {}).get("broker_ip")
    default_port = (existing or {}).get("broker_port", DEFAULT_BROKER_PORT)

    console.step("Searching the network for MQTT brokers (mDNS) ...")
    brokers = discover_brokers_mdns()
    if brokers is None:
        console.warn("  zeroconf not installed -- skipping mDNS (enable with: pipx inject "
                     "oselia-provision zeroconf, or pip install zeroconf in a venv).")
        brokers = []
    if not brokers:
        console.info("  None advertised -- scanning the local network for MQTT (port 1883) ...")
        brokers = scan_lan(mqtt.probe_broker)
    hit = console.pick_one(brokers, "MQTT broker", lambda b: "%s:%d" % b)
    if hit:
        return hit

    console.info("  No broker found automatically -- enter it manually.")
    while True:
        host = console.ask("Broker IP or hostname", default_ip, required=True)
        ip = resolve_to_ipv4(host)
        if not ip:
            console.warn("  Not a valid IP / resolvable hostname; try again.")
            continue
        port = console.ask("Broker port", str(default_port))
        if not str(port).isdigit() or not 1 <= int(port) <= 65535:
            console.warn("  Port must be 1..65535; try again.")
            continue
        return ip, int(port)
