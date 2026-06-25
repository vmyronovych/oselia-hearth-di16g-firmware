"""Push HA assets (the OSELIA Hearth dashboard + the switch blueprint) over the Home
Assistant WebSocket API, authenticated with a long-lived access token.

Why WebSocket and not REST: HA's REST `/api/` cannot install blueprints or create
Lovelace dashboards -- those are WebSocket-only operations (config flows, by contrast,
ARE REST-only -- see `ensure_oselia`). Auth is the long-lived token throughout. A
minimal stdlib-only WS client lives here so the wizard keeps no heavy dependencies
(same ethos as the optional zeroconf import).

The dashboard is the multi-gateway `/oselia-hearth` (one Sections view per gateway,
`gw-<id>`). It is (re)built from the devices the OSELIA custom integration has created,
reusing the standalone generator in `homeassistant/dashboards/generate.py`
(via `ensure_oselia_dashboard`) so the wizard and that tool stay identical -- the
freshly provisioned gateway simply joins the existing views.
"""
import base64
import json
import os
import socket
import struct
import urllib.request

class HAWSError(OSError):
    def __init__(self, type_, error):
        self.code = (error or {}).get("code") if isinstance(error, dict) else None
        super().__init__("HA WS %s failed: %s" % (type_, error))


BLUEPRINT_DOMAIN = "automation"
BLUEPRINT_PATH = "oselia/dib_switch.yaml"
DASHBOARD_URL_PATH = "oselia-hearth"    # HA requires a dash in dashboard url_path


# ===========================================================================
# Minimal WebSocket client (stdlib only)
# ===========================================================================
class HAWebSocket:
    def __init__(self, host, port, token, use_tls=False, timeout=10):
        self.host, self.port, self.token = host, port, token
        self.use_tls, self.timeout = use_tls, timeout
        self.sock = None
        self._id = 0

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *a):
        self.close()

    def connect(self):
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.use_tls:
            import ssl
            raw = ssl.create_default_context().wrap_socket(
                raw, server_hostname=self.host)
        self.sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET /api/websocket HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n") % (self.host, self.port, key)
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(1)
            if not chunk:
                raise OSError("WS handshake failed (no data)")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n")[0]:
            raise OSError("WS handshake not upgraded: %r" % buf.split(b"\r\n")[0])
        if self._recv().get("type") != "auth_required":
            raise OSError("expected auth_required")
        self._send({"type": "auth", "access_token": self.token})
        if self._recv().get("type") != "auth_ok":
            raise OSError("HA rejected the token (auth failed)")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    # ---- framing ----
    def _send(self, obj):
        payload = json.dumps(obj).encode()
        hdr = bytearray([0x81])                 # FIN + text
        n = len(payload)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(hdr) + mask + masked)

    def _recv(self):
        b0 = self.sock.recv(1)
        if not b0:
            raise OSError("WS closed")
        b1 = self.sock.recv(1)[0]
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._recvn(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._recvn(8))[0]
        return json.loads(self._recvn(n).decode())

    def _recvn(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise OSError("WS closed mid-frame")
            data += chunk
        return data

    def call(self, type_, **kw):
        """Send a command and return its result, raising on failure."""
        self._id += 1
        msg = {"id": self._id, "type": type_}
        msg.update(kw)
        self._send(msg)
        while True:
            r = self._recv()
            if r.get("id") == self._id and r.get("type") == "result":
                if not r.get("success"):
                    raise HAWSError(type_, r.get("error"))
                return r.get("result")
            # ignore events / other ids


# ===========================================================================
# Asset builders / installers (pure-ish; the WS calls are thin)
# ===========================================================================
def _load_dashboard_generator(here):
    """Import the standalone dashboard generator
    (homeassistant/dashboards/generate.py) by path -- it lives outside the
    provisioning package, so it is loaded as a one-off module rather than imported.
    Reusing it keeps the wizard's dashboard identical to the manual `generate.py` run."""
    import importlib.util
    path = os.path.normpath(os.path.join(here, "..", "homeassistant",
                                         "dashboards", "generate.py"))
    spec = importlib.util.spec_from_file_location("oselia_dashboard_generate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ensure_oselia_dashboard(ha_url, token, here):
    """(Re)build the multi-gateway OSELIA Hearth dashboard (/oselia-hearth) from the
    devices the OSELIA integration has created, so the freshly provisioned gateway gets
    its `gw-<id>` view. Reuses `dashboards/generate.py` so the wizard and that tool
    produce the same dashboard. Returns (summary_list, [gw_id, ...]).

    A soft skip (empty gw list) when no OSELIA gateways are visible yet -- the unit may
    still be registering when HA setup runs; re-running `--ha-setup` (or `generate.py`)
    picks it up."""
    gen = _load_dashboard_generator(here)
    host, port, use_tls = _parse_url(ha_url)
    with HAWebSocket(host, port, token, use_tls=use_tls) as ws:
        config, gw_ids = gen.build_config(ws)
        if not gw_ids:
            return (["dashboard skipped (no OSELIA gateways visible in HA yet -- "
                     "re-run --ha-setup once the unit registers)"], [])
        gen.push_config(ws, config)
        return (["dashboard '/%s' (%d gateway view(s): %s)"
                 % (gen.URL_PATH, len(gw_ids), ", ".join(gw_ids))], gw_ids)


def _rest(ha_url, token, path, data=None, method=None):
    """Minimal authenticated REST call (config flows aren't exposed over the WS API).
    Default GET (or POST when data is given); pass method= for DELETE etc."""
    url = ha_url.rstrip("/") + path
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body, method=(method or ("POST" if data is not None else "GET")),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def teardown(ha_url, token, here, remove_mqtt=False):
    """Remove this unit's HA presence and the blueprint, optionally the MQTT integration.
    The device itself disappears once its retained discovery is cleared on the broker
    (the caller does that MQTT-side). Because `/oselia-hearth` is SHARED across gateways,
    it is REBUILT from whoever remains (dropping the gone unit's `gw-<id>` view) rather
    than deleted -- and deleted only when no gateways are left. Returns a summary list."""
    host, port, use_tls = _parse_url(ha_url)
    done = []
    gen = _load_dashboard_generator(here)
    with HAWebSocket(host, port, token, use_tls=use_tls) as ws:
        existing = {d["url_path"]: d["id"] for d in ws.call("lovelace/dashboards/list")}
        if DASHBOARD_URL_PATH in existing:
            config, gw_ids = gen.build_config(ws)
            if gw_ids:
                gen.push_config(ws, config)
                done.append("dashboard /%s rebuilt (%d gateway(s) remain)"
                            % (DASHBOARD_URL_PATH, len(gw_ids)))
            else:
                ws.call("lovelace/dashboards/delete",
                        dashboard_id=existing[DASHBOARD_URL_PATH])
                done.append("dashboard /%s (no gateways left)" % DASHBOARD_URL_PATH)
        try:
            ws.call("blueprint/delete", domain=BLUEPRINT_DOMAIN, path=BLUEPRINT_PATH)
            done.append("blueprint %s" % BLUEPRINT_PATH)
        except HAWSError as e:
            if e.code not in ("not_found", None):
                done.append("blueprint kept (%s)" % e.code)   # e.g. in use
        if remove_mqtt:
            for e in ws.call("config_entries/get"):
                if e.get("domain") == "mqtt":
                    _rest(ha_url, token,
                          "/api/config/config_entries/entry/" + e["entry_id"],
                          method="DELETE")
                    done.append("MQTT integration")
    return done


def ensure_mqtt(ws, ha_url, token, broker_ip, broker_port, user, password):
    """If HA has no MQTT integration, add one (config flow) pointed at the same broker
    the unit uses, so a fresh HA picks up the device's retained discovery. Returns
    True if it was added, False if already present."""
    if any(e.get("domain") == "mqtt" for e in ws.call("config_entries/get")):
        return False
    flow = _rest(ha_url, token, "/api/config/config_entries/flow",
                 {"handler": "mqtt", "show_advanced_options": False})
    if flow.get("step_id") != "broker":
        raise OSError("unexpected MQTT config-flow start: %s" % json.dumps(flow)[:200])
    res = _rest(ha_url, token,
                "/api/config/config_entries/flow/" + flow["flow_id"],
                {"broker": broker_ip, "port": int(broker_port),
                 "username": user or "", "password": password or ""})
    if res.get("type") != "create_entry":
        raise OSError("MQTT setup didn't complete (check broker reachable from HA): %s"
                      % json.dumps(res.get("errors") or res)[:200])
    return True


OSELIA_DOMAIN = "oselia"


def ensure_oselia(ha_url, token, broker_ip, broker_port, user, password,
                  base_topic, release_url, gh_token):
    """Ensure the OSELIA custom integration is set up in HA against this broker, and set
    its firmware release-feed options (so the user never opens Configure by hand). The
    GitHub token is stored HA-side (the gateway never sees it). Idempotent: an existing
    entry's options are just updated. Returns a summary list.

    Uses the config-flow + options-flow REST endpoints (WebSocket can't drive flows)."""
    done = []
    entries = _rest(ha_url, token, "/api/config/config_entries/entry")
    entry = next((e for e in entries if e.get("domain") == OSELIA_DOMAIN), None)

    if entry is None:
        flow = _rest(ha_url, token, "/api/config/config_entries/flow",
                     {"handler": OSELIA_DOMAIN, "show_advanced_options": True})
        if flow.get("type") != "form" or flow.get("step_id") != "user":
            raise OSError("unexpected OSELIA config-flow start: %s"
                          % json.dumps(flow)[:200])
        res = _rest(ha_url, token,
                    "/api/config/config_entries/flow/" + flow["flow_id"],
                    {"broker": broker_ip, "port": int(broker_port),
                     "username": user or "", "password": password or "",
                     "base_topic": base_topic})
        if res.get("type") != "create_entry":
            raise OSError("OSELIA setup didn't complete (broker reachable from HA?): %s"
                          % json.dumps(res.get("errors") or res)[:200])
        entry_id = res["result"]["entry_id"]
        done.append("OSELIA integration (added, broker %s)" % broker_ip)
    else:
        entry_id = entry["entry_id"]
        done.append("OSELIA integration (already present)")

    # Set the firmware release feed + token via the options flow.
    oflow = _rest(ha_url, token, "/api/config/config_entries/options/flow",
                  {"handler": entry_id})
    if oflow.get("type") == "form":
        ores = _rest(ha_url, token,
                     "/api/config/config_entries/options/flow/" + oflow["flow_id"],
                     {"release_url": release_url or "", "github_token": gh_token or ""})
        if ores.get("type") == "create_entry":
            done.append("OSELIA release feed configured%s"
                        % (" (+ GitHub token)" if gh_token else ""))
        else:
            done.append("OSELIA options not set (%s)"
                        % json.dumps(ores.get("errors") or ores)[:120])
    return done


def install_blueprint(ws, yaml_text, source_url):
    """Install the blueprint; idempotent. If a copy exists, try to replace it
    (delete+save). If the delete is refused because an automation already uses it
    ('Blueprint in use'), leave the existing copy in place -- it can't be removed
    without deleting the user's automation, and re-provisions ship the same content.
    Returns True if (re)written, False if left in place."""
    try:
        ws.call("blueprint/save", domain=BLUEPRINT_DOMAIN, path=BLUEPRINT_PATH,
                yaml=yaml_text, source_url=source_url)
        return True
    except HAWSError as e:
        if e.code != "already_exists":
            raise
    try:
        ws.call("blueprint/delete", domain=BLUEPRINT_DOMAIN, path=BLUEPRINT_PATH)
        ws.call("blueprint/save", domain=BLUEPRINT_DOMAIN, path=BLUEPRINT_PATH,
                yaml=yaml_text, source_url=source_url)
        return True
    except HAWSError:
        return False        # in use by an automation -> keep the existing blueprint


def run_setup(ha_url, token, here, broker_ip=None, broker_port=1883,
              mqtt_user=None, mqtt_pass=None, do_blueprint=True):
    """Legacy MQTT-discovery HA setup: ensure HA has the MQTT integration (if `broker_ip`
    is given) and install the switch blueprint. `here` = provisioning dir (to find the
    blueprint file). The curated dashboard is OSELIA-mode only now (`/oselia-hearth` via
    `ensure_oselia_dashboard`); MQTT-discovery devices appear under the MQTT integration
    with their auto-created entities. Returns a summary list."""
    host, port, use_tls = _parse_url(ha_url)
    done = []
    with HAWebSocket(host, port, token, use_tls=use_tls) as ws:
        if broker_ip and ensure_mqtt(ws, ha_url, token, broker_ip, broker_port,
                                     mqtt_user, mqtt_pass):
            done.append("MQTT integration (added, broker %s:%d)"
                        % (broker_ip, int(broker_port)))
        if do_blueprint:
            bp_path = os.path.normpath(os.path.join(
                here, "..", "homeassistant", "blueprints", "automation",
                "oselia", "dib_switch.yaml"))
            with open(bp_path) as f:
                yaml_text = f.read()
            source_url = _blueprint_source_url(yaml_text)
            wrote = install_blueprint(ws, yaml_text, source_url)
            done.append("blueprint %s%s" % (BLUEPRINT_PATH,
                                            "" if wrote else " (kept; in use)"))
    return done


def _parse_url(ha_url):
    u = ha_url.rstrip("/")
    use_tls = u.startswith("https://")
    u = u.split("://", 1)[-1]
    host, _, port = u.partition(":")
    return host, int(port) if port else (443 if use_tls else 8123), use_tls


def _blueprint_source_url(yaml_text):
    for line in yaml_text.splitlines():
        if line.strip().startswith("source_url:"):
            return line.split("source_url:", 1)[1].strip()
    return None
