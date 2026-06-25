"""Robust minimal MQTT 3.1.1 client over a byte stream (not usocket).

Designed for the CH9120 transparent UART link (net_stream.UartStream): CONNECT
(+auth +LWT), PUBLISH QoS0, PINGREQ keepalive with PINGRESP liveness tracking,
DISCONNECT, plus SUBSCRIBE + inbound-PUBLISH dispatch to a message handler (for
two-way control). The wire format lives in mqtt_packets (unit-tested).

Liveness model: we ping when the link has been idle for ~70% of keepalive; if no
PINGRESP arrives within ping_timeout_ms we declare the session dead so the network
task can tear down and reconnect. All methods are non-blocking except connect(),
which waits (bounded) for CONNACK.
"""
import mqtt_packets as pkt

try:
    import utime as _t
except ImportError:                      # host
    import time as _t
    if not hasattr(_t, "ticks_ms"):
        _t.ticks_ms = lambda: int(_t.time() * 1000)
        _t.ticks_diff = lambda a, b: a - b
        _t.sleep_ms = lambda ms: _t.sleep(ms / 1000.0)


class MQTTError(Exception):
    pass


class MQTTClient:
    def __init__(self, client_id, stream, user=None, password=None,
                 keepalive=30, lwt_topic=None, lwt_msg=None, lwt_retain=True,
                 connect_timeout_ms=4000, ping_timeout_ms=5000):
        self.client_id = client_id
        self.s = stream
        self.user = user
        self.password = password
        self.keepalive = keepalive
        self.lwt_topic = lwt_topic
        self.lwt_msg = lwt_msg
        self.lwt_retain = lwt_retain
        self.connect_timeout_ms = connect_timeout_ms
        self.ping_timeout_ms = ping_timeout_ms

        self.connected = False
        self._last_tx = 0
        self._last_ping = 0          # when we last SENT a PINGREQ (drives the ping cycle)
        self._ping_outstanding = False
        self._ping_sent = 0
        self._packet_id = 0
        self._on_message = None      # fn(topic, payload_bytes) for inbound PUBLISH

    # ---- low-level receive ----
    def _read_byte_blocking(self, start, timeout_ms):
        while True:
            b = self.s.read_byte()
            if b is not None:
                return b
            if _t.ticks_diff(_t.ticks_ms(), start) > timeout_ms:
                return None
            _t.sleep_ms(1)

    def _recv_packet(self, timeout_ms):
        """Return (header_byte, body_bytes) or None on timeout/partial."""
        start = _t.ticks_ms()
        hdr = self._read_byte_blocking(start, timeout_ms)
        if hdr is None:
            return None
        rl = 0
        mult = 1
        while True:
            b = self._read_byte_blocking(start, timeout_ms)
            if b is None:
                return None
            rl += (b & 0x7F) * mult
            if (b & 0x80) == 0:
                break
            mult *= 128
        body = b""
        while len(body) < rl:
            chunk = self.s.read(rl - len(body))
            if chunk:
                body += chunk
            elif _t.ticks_diff(_t.ticks_ms(), start) > timeout_ms:
                return None
            else:
                _t.sleep_ms(1)
        return (hdr, body)

    def _drain(self):
        """Discard any bytes currently buffered on the link. Run before CONNECT so leftovers
        from a prior session (e.g. bytes the CH9120 still held from a connection that wasn't
        cleanly closed) can't be misframed as our CONNACK and desync the parser."""
        try:
            while self.s.any():
                if not self.s.read(256):
                    break
        except Exception:
            pass

    # ---- public API ----
    def connect(self, clean_session=True):
        self.connected = False
        self._ping_outstanding = False
        self._drain()                        # clear any stale bytes before we CONNECT
        frame = pkt.build_connect(
            self.client_id, keepalive=self.keepalive, clean=clean_session,
            user=self.user, password=self.password,
            lwt_topic=self.lwt_topic, lwt_msg=self.lwt_msg,
            lwt_qos=0, lwt_retain=self.lwt_retain)
        self.s.write(frame)
        self._last_tx = _t.ticks_ms()

        resp = self._recv_packet(self.connect_timeout_ms)
        if resp is None:
            raise MQTTError("no CONNACK")
        hdr, body = resp
        if (hdr & 0xF0) != pkt.CONNACK:
            raise MQTTError("expected CONNACK, got 0x%02x" % hdr)
        try:
            rc = pkt.connack_return_code(body)   # raises ValueError on a short CONNACK
        except ValueError as e:
            raise MQTTError("bad CONNACK: %s" % e)
        if rc != 0:
            raise MQTTError("CONNACK refused rc=%d" % rc)
        self.connected = True
        self._last_ping = _t.ticks_ms()      # first liveness ping ~70% of keepalive later
        return True

    def publish(self, topic, msg, retain=False, qos=0):
        if not self.connected:
            raise MQTTError("not connected")
        self.s.write(pkt.build_publish(topic, msg, retain=retain, qos=qos))
        self._last_tx = _t.ticks_ms()

    def set_message_handler(self, fn):
        """fn(topic, payload_bytes) is called for each inbound PUBLISH drained in
        service(). Runs on the network core, after the gesture queue is drained, so
        command handling never delays a button publish."""
        self._on_message = fn

    def subscribe(self, topic, qos=0):
        """Subscribe to a topic filter (QoS0). Clean-session means subscriptions are
        dropped on disconnect, so the caller re-subscribes on every (re)connect."""
        if not self.connected:
            raise MQTTError("not connected")
        self._packet_id = (self._packet_id + 1) & 0xFFFF or 1
        self.s.write(pkt.build_subscribe(self._packet_id, topic, qos))
        self._last_tx = _t.ticks_ms()

    def ping(self):
        self.s.write(pkt.PINGREQ_PKT)
        now = _t.ticks_ms()
        self._last_tx = now
        self._last_ping = now
        self._ping_sent = now
        self._ping_outstanding = True

    def service(self):
        """Drain inbound packets + manage keepalive. Returns True if link healthy,
        False if it should be considered dead (caller reconnects)."""
        if not self.connected:
            return False

        # drain whatever is available (small bounded read)
        while self.s.any():
            p = self._recv_packet(50)
            if p is None:
                break
            hdr = p[0] & 0xF0
            if hdr == pkt.PINGRESP:
                self._ping_outstanding = False
            elif hdr == pkt.DISCONNECT:
                self.connected = False
                return False
            elif hdr == pkt.PUBLISH:
                if self._on_message is not None:
                    try:
                        topic, payload = pkt.parse_publish(p[0], p[1])
                        self._on_message(topic, payload)
                    except Exception:
                        pass          # a malformed command must not kill the link
            # SUBACK / others: ignore

        now = _t.ticks_ms()
        # Keepalive + liveness: send a PINGREQ every ~70% of keepalive on a fixed CYCLE
        # (time since the last ping), NOT merely when idle since the last TX. This matters
        # because writes into a dead CH9120 TCP socket still "succeed" at the UART, so TX
        # activity (e.g. the 10 s diag publish) can't prove the link is alive -- and we no
        # longer trust the CH9120 TCPCS hardware pin. A periodic ping + PINGRESP check is the
        # transport-independent way to detect a dead link even while actively publishing.
        if not self._ping_outstanding and \
                _t.ticks_diff(now, self._last_ping) >= int(self.keepalive * 1000 * 0.7):
            try:
                self.ping()
            except Exception:
                self.connected = False
                return False
        # liveness: outstanding ping not answered in time -> dead
        if self._ping_outstanding and \
                _t.ticks_diff(now, self._ping_sent) > self.ping_timeout_ms:
            self.connected = False
            return False
        return True

    def disconnect(self):
        try:
            if self.connected:
                self.s.write(pkt.DISCONNECT_PKT)
        except Exception:
            pass
        self.connected = False
