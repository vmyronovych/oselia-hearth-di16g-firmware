"""Core 1 -- networking task.

Owns the CH9120 UART link, the MQTT session, HA discovery, the event-queue drain,
and the status LED. Allowed to block (reconnect waits etc.) because it runs on the
second core; the input task on core 0 is unaffected.

Robustness: exponential reconnect backoff, LWT + availability, discovery republish
on reconnect, keepalive/PINGRESP liveness, and a heartbeat written every pass (and
during backoff waits) so core 0's watchdog gate sees this core as alive.
"""
import gc
import utime
try:
    import ujson as json
except ImportError:
    import json

import config as cfg
import ch9120 as ch9120mod
from net_stream import UartStream
from mqtt_client import MQTTClient, MQTTError
import ha_discovery as ha
import diag
import mcp_health
import clock
import log

try:
    import status_led
except Exception:
    status_led = None

# The hardware watchdog lives on CORE 1 (the network task), not core 0. Rationale
# (SPEC.md sec.12 + user requirement): an MCP/I2C stall on core 0 must NEVER reboot
# the board -- a dead bus is reported and recovered, not reset. The WDT therefore
# guards only the *network* core: it is fed from _beat() (called every core1 pass and
# during every chunked blocking wait), so it trips only if net_task itself wedges.
_wdt = None


def _beat(shared, led, mono):
    """Feed the watchdog + sync LED subsystem states from shared + render once.
    Called every core1 loop pass and inside every blocking wait, so the WDT stays fed
    as long as the network core is alive."""
    shared.beat(utime.ticks_ms())
    if _wdt is not None:
        _wdt.feed()
    if led is not None:
        if shared.ready:
            led.boot_done()
        h = shared.health()
        led.set_state("ethernet", h["ethernet"])
        led.set_state("mqtt", h["mqtt"])
        led.set_state("mcp", h["mcp"])
        led.update(mono.ms())


def _sleep_beat(shared, led, mono, ms):
    """Sleep `ms` in small chunks, keeping heartbeat + LED alive throughout."""
    waited = 0
    while waited < ms:
        _beat(shared, led, mono)
        utime.sleep_ms(50)
        waited += 50


def run(shared, queue, device_id):
    # MCP topology is resolved by core0 (network-first boot); read it below after
    # the CH9120 link is up. Placeholders until then.
    n_boards = 0
    board_addrs = []
    mono = clock.from_utime()
    led = None
    if status_led and cfg.STATUS_LED_ENABLE:
        led = status_led.StatusLed(cfg.PIN_STATUS_LED,
                                   cfg.STATUS_LED_BRIGHTNESS, cfg.STATUS_LED_ORDER)

    try:
        import machine                      # for the die-temp ADC and the reboot cmd
    except Exception:
        machine = None

    # RP2040 internal die-temperature sensor (ADC channel 4), read only inside the
    # gated diag block. Optional: if the build/board can't provide it, temp is None.
    temp_adc = None
    if cfg.DIAG_ENABLE and machine is not None:
        try:
            temp_adc = machine.ADC(4)
        except Exception:
            temp_adc = None

    avail_topic = ha.availability_topic(cfg, device_id)

    # OTA topics + state (OTA_SPEC.md). The bundle streams as chunks over the live
    # broker session (no CH9120 retarget); slots/boot-confirm logic lives in ota.py.
    _ota_base = ha.base(cfg, device_id)
    ota_cmd_topic = _ota_base + "/ota/cmd"
    ota_data_topic = _ota_base + "/ota/data"
    ota_state_topic = _ota_base + "/ota/state"
    ota_nak_topic = _ota_base + "/ota/nak"
    ota_rx = [None]            # active ota.OtaReceiver during a download, else None
    ota_target = [None]        # target version string being downloaded
    ota_last_rx = [0]          # ms of the last REAL chunk received (stall -> NAK/abort)
    ota_nak_at = [0]           # ms of the last NAK sent (back-off timer, separate)
    ota_healthy_since = [0]    # ms the running build has been online+healthy
    ota_confirmed = [False]    # boot-confirm already done this boot

    # Bring up the CH9120 link (blocking, multi-second). Heartbeat isn't gated yet
    # because shared.ready is still False, so core0 hasn't armed the WDT.
    log.info("configuring CH9120...")
    uart, ch, leased_ip = ch9120mod.bring_up(cfg, read_ip=cfg.DIAG_ENABLE)
    stream = UartStream(uart, ch)
    # Resolve the address to report in diagnostics once: the DHCP lease read back
    # from the CH9120, else the static/"dhcp" fallback.
    ip_display = diag.format_ip(leased_ip, cfg.USE_DHCP, cfg.LOCAL_IP)
    if leased_ip:
        log.info("CH9120 DHCP lease: %s" % ip_display)
    _beat(shared, led, mono)

    # Network-first boot: core0 resolves the MCP board set on its own thread. Wait
    # (bounded, heartbeat-friendly) for it before HA discovery / diag need the count;
    # if it never resolves (all chips absent), fall back to the config list so the
    # network/entities still come up. The bus is never allowed to gate the network.
    waited = 0
    while not shared.boards_resolved and waited < cfg.NET_BOARD_WAIT_MS:
        _beat(shared, led, mono)
        utime.sleep_ms(50)
        waited += 50
    n_boards, board_addrs, _resolved = shared.boards_info()
    if not _resolved:
        n_boards = len(cfg.MCP_ADDRESSES)
        board_addrs = ["0x%02x" % a for a in cfg.MCP_ADDRESSES]
        log.warn("boards not resolved in %dms; assuming %d from config"
                 % (cfg.NET_BOARD_WAIT_MS, n_boards))
    else:
        log.info("boards from core0: %d (%s)" % (n_boards, ",".join(board_addrs)))

    client = MQTTClient(
        cfg.BASE_TOPIC + "_" + device_id,    # e.g. hearth_<id>
        stream, user=cfg.MQTT_USER, password=cfg.MQTT_PASS,
        keepalive=cfg.MQTT_KEEPALIVE_S,
        lwt_topic=avail_topic, lwt_msg="offline", lwt_retain=True,
        connect_timeout_ms=cfg.MQTT_CONNECT_TIMEOUT_MS,
        ping_timeout_ms=cfg.PING_RESPONSE_TIMEOUT_MS)

    backoff = cfg.RECONNECT_BACKOFF_MIN_MS
    first_connect = True

    # Two-way control. The handler runs inside client.service() -- i.e. on core1
    # AFTER the gesture queue is drained each pass -- so a command never delays an
    # action publish. identify_until is a holder so the closure can update it.
    identify_until = [0]
    identify_last = [0]

    def _publish_cfg():
        """Publish the current live-tunable values (retained) so the HA number/select
        entities reflect them."""
        _v, lm, dg, db = shared.tunables()
        payload = ha.cfg_state_payload(lm, dg, db, log.get_level())
        try:
            client.publish(ha.cfg_state_topic(cfg, device_id),
                           json.dumps(payload), retain=True)
        except Exception as e:
            log.error("cfg publish failed: %s" % e, every_ms=5000, key="cfg")

    def _persist_site(updates):
        """Merge updates into the board's site.json (atomic) so a re-tune survives a
        reboot. Best-effort; a failure is logged, not fatal."""
        try:
            try:
                with open("site.json") as f:
                    site = json.load(f)
            except OSError:
                site = {}
            site.update(updates)
            with open("site.json.tmp", "w") as f:
                json.dump(site, f)
            import os
            os.rename("site.json.tmp", "site.json")
        except Exception as e:
            log.error("persist failed: %s" % e, every_ms=5000, key="persist")

    def _decode(payload):
        if isinstance(payload, (bytes, bytearray)):
            return payload.decode().strip()
        return str(payload).strip()

    def _on_cmd(topic, payload):
        name = topic.rsplit("/", 1)[-1] if topic else ""
        if name == "reboot":
            log.warn("reboot command -> resetting")
            try:
                client.publish(avail_topic, "offline", retain=True)
            except Exception:
                pass
            utime.sleep_ms(150)            # let the offline LWT flush
            if machine is not None:
                machine.reset()
        elif name == "maintenance":
            # Provisioning quiesce (PROVISIONING_SPEC.md): park the auto-run loader and reset
            # to a BARE REPL so the host can re-provision over USB WITHOUT breaking into a
            # running REPL. Doing it host-side fights the hardware watchdog -- the WDT
            # hard-resets the board mid-break-in. Here the FIRMWARE renames the loader +
            # resets ITSELF (no host interrupt, no WDT race); the board boots bare (no app
            # -> no WDT), and provisioning restores the loader afterwards. Uses the same
            # `.provbak` suffix the host's _disable_app/_restore_app expect.
            log.warn("maintenance command -> parking loader + resetting to a bare REPL")
            try:
                client.publish(avail_topic, "offline", retain=True)
            except Exception:
                pass
            # Close the broker session CLEANLY (MQTT DISCONNECT) before resetting, so the
            # broker frees this client-id immediately and the CH9120 socket closes -- nothing
            # lingers to leave stale bytes that would desync the next boot's CONNECT parse.
            try:
                client.disconnect()
            except Exception:
                pass
            utime.sleep_ms(150)            # let the offline status + DISCONNECT flush
            try:
                import os
                _l = os.listdir()
                entry = ("boot.py" if "boot.py" in _l
                         else ("main.py" if "main.py" in _l else None))
                if entry:
                    bak = entry + ".provbak"
                    try:
                        os.remove(bak)            # drop a stale backup if present
                    except OSError:
                        pass
                    os.rename(entry, bak)
            except Exception as e:
                log.error("maintenance park failed: %s" % e)
            utime.sleep_ms(50)
            if machine is not None:
                machine.reset()
        elif name == "identify":
            log.info("identify command")
            identify_until[0] = mono.ms() + 3000
        elif name in ("long_ms", "double_gap_ms", "debounce_ms"):
            try:
                val = int(float(_decode(payload)))
            except Exception:
                return
            lo, hi = ha.TUNABLE_LIMITS[name]
            val = lo if val < lo else (hi if val > hi else val)
            shared.set_tunables(**{name: val})   # core0 re-applies on version bump
            _persist_site({name: val})
            _publish_cfg()
            log.info("set %s=%d" % (name, val))
        elif name == "log_level":
            s = _decode(payload)
            opts = ha.LOG_LEVEL_OPTIONS
            lvl = opts.index(s) if s in opts else None
            if lvl is None:
                try:
                    n = int(s)
                    lvl = n if 0 <= n < len(opts) else None
                except Exception:
                    lvl = None
            if lvl is not None:
                log.set_level(lvl)
                _persist_site({"log_level": lvl})
                _publish_cfg()
                log.info("set log_level=%d" % lvl)
        else:
            log.warn("unknown command: %s" % name, every_ms=5000, key="cmd")

    # ---- OTA over MQTT (see OTA_SPEC.md) ----
    def _ota_publish_state(stage, target=None, percent=None, error=None):
        st = {"stage": stage, "running_version": cfg.SW_VERSION}
        if target is not None:
            st["target_version"] = target
        if percent is not None:
            st["percent"] = percent
        if error is not None:
            st["error"] = error
        try:
            client.publish(ota_state_topic, json.dumps(st), retain=True)
        except Exception:
            pass

    def _ota_abort(reason):
        if ota_rx[0] is not None:
            ota_rx[0].close()
            ota_rx[0] = None
        try:
            import os
            os.remove(cfg.OTA_STAGING_PATH)
        except Exception:
            pass
        log.error("OTA aborted: %s" % reason)
        _ota_publish_state("error", target=ota_target[0], error=reason)
        ota_target[0] = None

    def _ota_command(payload):
        if not cfg.OTA_ENABLE or ota_rx[0] is not None:
            return                        # disabled, or a download already in flight
        try:
            cmd = json.loads(payload)
        except Exception:
            return
        version = cmd.get("version")
        if version == cfg.SW_VERSION:     # version guard: re-delivery is a no-op
            _ota_publish_state("idle", target=version, percent=100)
            return
        import ota
        try:
            try:
                import os
                os.mkdir("/ota")
            except OSError:
                pass
            ota_rx[0] = ota.OtaReceiver(
                cfg.OTA_STAGING_PATH, int(cmd["chunks"]), int(cmd["size"]),
                cmd["sha256"], int(cmd.get("chunk_size", cfg.OTA_CHUNK_SIZE)),
                beat=lambda: _beat(shared, led, mono))
            ota_target[0] = version
            ota_last_rx[0] = mono.ms()
            ota_nak_at[0] = mono.ms()
            _ota_publish_state("downloading", target=version, percent=0)
            log.warn("OTA start -> %s (%d chunks, %d bytes)" %
                     (version, cmd["chunks"], cmd["size"]))
        except Exception as e:
            ota_rx[0] = None
            _ota_abort("start:%s" % e)

    def _ota_data(payload):
        rx = ota_rx[0]
        if rx is None or len(payload) < 4:
            return
        index = (payload[0] << 24) | (payload[1] << 16) | (payload[2] << 8) | payload[3]
        try:
            rx.add_chunk(index, payload[4:])
        except Exception as e:
            _ota_abort("chunk:%s" % e)
            return
        ota_last_rx[0] = mono.ms()
        _beat(shared, led, mono)          # keep the WDT fed through the chunk stream
        n = len(rx.received)
        if n % 16 == 0 or rx.complete:
            gc.collect()
            _ota_publish_state("downloading", target=ota_target[0], percent=rx.percent())
        if rx.complete:
            _ota_finish()

    def _ota_progress_check(now_ms):
        """Drive a download in flight: abort if no real chunk has arrived for
        OTA_DOWNLOAD_TIMEOUT_MS (a dead publisher -> free the receiver for a retry),
        else NAK the still-missing chunks (QoS0 drops). `ota_last_rx` tracks only REAL
        chunks; NAK back-off uses its own timer so NAKs never mask a true stall."""
        rx = ota_rx[0]
        if rx is None or rx.complete:
            return
        since_chunk = now_ms - ota_last_rx[0]
        if since_chunk >= cfg.OTA_DOWNLOAD_TIMEOUT_MS:
            _ota_abort("download stalled %ds (no chunks)"
                       % (cfg.OTA_DOWNLOAD_TIMEOUT_MS // 1000))
            return
        if (since_chunk >= cfg.OTA_NAK_STALL_MS
                and now_ms - ota_nak_at[0] >= cfg.OTA_NAK_STALL_MS):
            miss = rx.missing()
            if miss:
                try:
                    client.publish(ota_nak_topic, json.dumps(miss))
                except Exception:
                    pass
                log.warn("OTA NAK: %d chunks still missing" % len(miss),
                         every_ms=3000, key="nak")
            ota_nak_at[0] = now_ms        # back off the NAK rate (not the stall timer)

    def _ota_finish():
        import ota
        rx = ota_rx[0]
        ota_rx[0] = None
        try:
            rx.finish()                   # verify size + whole-bundle sha256
            state = ota.read_state(cfg.OTA_STATE_PATH)
            inactive = ota.other_slot(state.get("active", "a"))
            ota.apply_bundle_file(cfg.OTA_STAGING_PATH, cfg.SLOTS_DIR + "/" + inactive,
                                  beat=lambda: _beat(shared, led, mono))
            ota.write_state(cfg.OTA_STATE_PATH, ota.staged_state(state, inactive))
            _ota_publish_state("applying", target=ota_target[0], percent=100)
            log.warn("OTA verified -> slot %s; resetting to boot it" % inactive)
            utime.sleep_ms(400)           # let the retained state publish flush
            ota.reset()
        except Exception as e:
            _ota_abort("apply:%s" % e)

    def _ota_confirm_if_healthy(now_ms):
        """Once the running build has been NETWORK-online for OTA_BOOT_CONFIRM_MS, clear
        the boot-confirm pending flag so the main.py loader won't auto-revert it.

        Requires only mqtt+ethernet -- deliberately NOT mcp. As of 0.7.x a degraded MCP
        is a normal, reported, recoverable running state (the firmware is built to keep
        running, reporting, and recovering with a dead/absent chip -- never rebooting for
        it; the watchdog likewise guards only the network core). Gating boot-confirm on
        mcp would auto-revert a perfectly good build on exactly the units that have an MCP
        fault -- the ones that most need the upgrade. Network health is the real "did this
        build come up?" signal."""
        if ota_confirmed[0] or not cfg.OTA_ENABLE:
            return
        h = shared.health()
        if not (h["mqtt"] and h["ethernet"]):
            ota_healthy_since[0] = 0
            return
        if ota_healthy_since[0] == 0:
            ota_healthy_since[0] = now_ms
        elif now_ms - ota_healthy_since[0] >= cfg.OTA_BOOT_CONFIRM_MS:
            import ota
            state = ota.read_state(cfg.OTA_STATE_PATH)
            if state.get("pending"):
                ota.write_state(cfg.OTA_STATE_PATH, ota.confirm_state(state))
                log.warn("OTA build %s confirmed healthy" % cfg.SW_VERSION)
                _ota_publish_state("idle", target=cfg.SW_VERSION, percent=100)
            ota_confirmed[0] = True

    def _on_message(topic, payload):
        if cfg.OTA_ENABLE and topic == ota_cmd_topic:
            _ota_command(payload)
        elif cfg.OTA_ENABLE and topic == ota_data_topic:
            _ota_data(payload)
        else:
            _on_cmd(topic, payload)

    if cfg.CONTROL_ENABLE or cfg.OTA_ENABLE:
        client.set_message_handler(_on_message)

    # Diagnostics telemetry state (all local to this core; no cross-core surface).
    reconnects = 0
    last_gesture = ""
    diag_last_ms = 0
    # Publish-on-change tracking so a fault surfaces in HA immediately (not up to
    # DIAG_INTERVAL_S later): core0 bumps these versions; we compare each pass.
    applied_change_ver = shared.mcp_change_version
    applied_event_ver = shared.event_version
    last_eth_pub = None
    last_mqtt_pub = None

    # Mirror WARN/ERROR log lines to HA. The sink may fire on EITHER core, so it
    # only stashes the line (a bare list-slot write -- atomic under the GIL); the
    # actual publish happens below, queue-gated, so it never delays an action.
    log_stash = [None]
    last_log_pub = None
    if cfg.DIAG_ENABLE:
        def _log_sink(lvl, msg):
            if lvl <= log.WARN:
                log_stash[0] = (log.level_name(lvl), msg)
        log.set_sink(_log_sink)

    def _disc_settle():
        # Heartbeat + LED between discovery publishes: a multi-board republish can
        # take seconds (8 boards ~= 384 msgs), and it must not look like a core1
        # stall to core0's WDT gate.
        _beat(shared, led, mono)
        utime.sleep_ms(20)

    while True:
        # Arm the watchdog once the network is up (not before -- the multi-second
        # CH9120/MQTT bring-up must not trip it). From here _beat() feeds it.
        global _wdt
        if _wdt is None and cfg.WDT_ENABLE and shared.ready and machine is not None:
            _wdt = machine.WDT(timeout=cfg.WDT_TIMEOUT_MS)
            log.info("watchdog armed (core1)")

        # ---- (re)connect ----
        if not client.connected:
            shared.set_net(mqtt_ok=False)
            shared.set_net(eth_ok=stream.link_up())
            try:
                log.info("MQTT connect...")
                client.connect()
                shared.set_net(eth_ok=True, mqtt_ok=True)
                client.publish(avail_topic, "online", retain=True)
                if first_connect and cfg.OTA_ENABLE:
                    # Reached the network -> this boot is good; clear the main.py loader's
                    # consecutive-failure counter so its safe-mode gate resets.
                    try:
                        import ota as _ota
                        _st = _ota.read_state(cfg.OTA_STATE_PATH)
                        if _st.get("crashes"):
                            _st["crashes"] = 0
                            _ota.write_state(cfg.OTA_STATE_PATH, _st)
                        # Publish a clean idle ota/state when nothing is pending, so a
                        # STALE retained state (e.g. an old "applying" from a pre-0.7.2
                        # boot that never confirmed because its MCP was faulted) can't
                        # leave the HA update entity stuck "installation in progress". A
                        # genuinely pending build still confirms via the boot-confirm path.
                        if not _st.get("pending"):
                            _ota_publish_state("idle", target=cfg.SW_VERSION, percent=100)
                    except Exception:
                        pass
                if first_connect or cfg.DISCOVERY_REPUBLISH_ON_RECONNECT:
                    # In "oselia" mode the first-party custom integration owns the HA
                    # entities, so the firmware skips publishing MQTT-discovery configs.
                    # The data + command topics are identical either way; only the
                    # homeassistant/.../config publishing is gated. The command
                    # subscribe and the cfg seed are NOT gated -- commands and the HA
                    # number/select state work in both modes. See INTEGRATION_SPEC.md.
                    publish_disc = getattr(cfg, "HA_INTEGRATION", "oselia") == "mqtt"
                    if publish_disc:
                        mode = getattr(cfg, "INPUT_DISCOVERY", "both")
                        if mode in ("trigger", "both"):
                            ha.publish_discovery(client, cfg, device_id, n_boards,
                                                 settle_ms=_disc_settle)
                        if mode in ("event", "both"):
                            ha.publish_event_discovery(client, cfg, device_id, n_boards,
                                                       settle_ms=_disc_settle)
                        log.info("HA discovery published (%d inputs, %s)" % (
                            n_boards * 16, mode))
                    else:
                        log.info("HA discovery skipped (oselia integration mode)")
                    if cfg.CONTROL_ENABLE:
                        # clean-session drops subs on disconnect -> re-subscribe here
                        # (needed in BOTH modes so commands keep working).
                        client.subscribe(ha.command_sub_topic(cfg, device_id))
                        if publish_disc:
                            ha.publish_command_discovery(client, cfg, device_id,
                                                         settle_ms=_disc_settle)
                            ha.publish_tunable_discovery(client, cfg, device_id,
                                                         settle_ms=_disc_settle)
                        _publish_cfg()       # seed number/select values (both modes:
                                             # the oselia entities also read <base>/<id>/cfg)
                    if cfg.OTA_ENABLE:
                        # clean-session: re-subscribe to the OTA command + data topics
                        # on every (re)connect.
                        client.subscribe(ota_cmd_topic)
                        client.subscribe(ota_data_topic)
                    if cfg.DIAG_ENABLE and publish_disc:
                        diag.publish_diag_discovery(
                            client, cfg, device_id, settle_ms=_disc_settle)
                if not first_connect:
                    reconnects += 1
                first_connect = False
                backoff = cfg.RECONNECT_BACKOFF_MIN_MS
                shared.ready = True          # lets core0 arm the WDT
                gc.collect()
            except Exception as e:           # noqa: BLE001 -- net_task must self-heal, never
                # die. ANY failure on the (re)connect path -> back off + retry, NOT crash the
                # thread (a crashed net_task freezes the LED on solid blue and never recovers).
                # Broadened from (MQTTError, OSError): a malformed CONNACK from stale link
                # bytes can surface as ValueError etc., which previously killed the task.
                log.error("connect failed: %s (retry %dms)" % (e, backoff),
                          every_ms=3000, key="conn")
                _beat(shared, led, mono)     # connect() just blocked up to
                                             # MQTT_CONNECT_TIMEOUT_MS without beating;
                                             # tick before the (also blocking) CH9120
                                             # re-bring-up so the two don't stack into
                                             # one >CORE1_STALL_MS gap that starves the
                                             # WDT during a sustained outage.
                # The MQTT CONNECT rides on the CH9120's TCP socket. A failed CONNECT on
                # this transparent link almost always means that socket is dead (broker
                # restart, cable, idle close) -- reconnecting at the MQTT layer alone then
                # writes CONNECT bytes into a dead pipe forever (no CONNACK). Re-run the
                # CH9120 bring-up to re-establish the TCP client link, then rebind the stream.
                # The ONE failure where the socket is fine is a CONNACK *refusal* (we got a
                # CONNACK -> the link works; it's auth/availability), so skip the re-bringup
                # there (it wouldn't help and would just thrash).
                # NOTE: we deliberately do NOT consult the CH9120 TCPCS pin -- it is not
                # reliably wired/validated on this board and a false "down" caused a
                # connect->publish->forced-reconnect FLAP. A failed CONNACK is the
                # authoritative "socket dead" signal instead.
                refused = isinstance(e, MQTTError) and "refused" in str(e)
                if not refused:
                    log.warn("re-bringing up CH9120 TCP link", every_ms=3000, key="link")
                    try:
                        _u, _ch, _ = ch9120mod.bring_up(cfg)  # no IP re-read on reconnect
                        stream = UartStream(_u, _ch)
                        client.s = stream
                    except Exception as e2:
                        log.error("CH9120 re-bringup failed: %s" % e2,
                                  every_ms=3000, key="lbu")
                shared.ready = True          # arm WDT even if broker down (we self-heal)
                _sleep_beat(shared, led, mono, backoff)
                backoff = min(backoff * 2, cfg.RECONNECT_BACKOFF_MAX_MS)
                continue

        # ---- steady state ----
        _beat(shared, led, mono)

        # publish any queued gestures
        drained = 0
        while True:
            ev = queue.get()
            if ev is None:
                break
            index, gesture = ev
            board, pin = ha.split_index(index)
            try:
                ha.publish_action(client, cfg, device_id, board, pin, gesture)
                last_gesture = "b%d/in%d %s" % (board, pin, gesture)
                if led is not None:
                    led.notify_activity(mono.ms())
                log.debug("published b%d in%d=%s" % (board, pin, gesture))
            except Exception as e:
                log.error("publish failed: %s" % e, every_ms=2000, key="pub")
                client.connected = False     # force reconnect path
                break
            drained += 1
            if drained >= 16:                # bound work per pass; yield often
                break

        # keepalive + liveness; False -> session dead -> reconnect
        try:
            if not client.service():
                log.warn("link/keepalive lost; reconnecting")
                shared.set_net(mqtt_ok=False)
        except Exception as e:
            log.error("service error: %s" % e, every_ms=2000, key="svc")
            client.connected = False

        # Ethernet view for the LED + diagnostics. We have no trustworthy hardware link pin
        # (the CH9120 TCPCS pin isn't reliably wired/validated on this board, and acting on
        # it caused a false-"down" reconnect FLAP), so report Ethernet from the MQTT session:
        # if the broker session is up, the link is necessarily up. A genuinely dead link is
        # caught by the periodic keepalive/PINGRESP liveness in client.service() above, which
        # trips the reconnect (+ CH9120 re-bringup) path -- detection in ~keepalive*0.7 +
        # ping_timeout (~26 s), independent of any hardware status pin.
        shared.set_net(eth_ok=client.connected)

        # ---- diagnostics telemetry ----
        # Strictly lowest priority: only when connected and the gesture queue is
        # fully drained (button publishes always win the single CH9120 pipe). Sent
        # every DIAG_INTERVAL_S OR immediately on a health/fault change (core0 bumped
        # mcp_change_version, or eth/mqtt flipped) so root cause surfaces at once.
        # One small retained JSON, fire-and-forget -- never in front of an action.
        now_ms = mono.ms()       # clock.Monotonic ms is ever-increasing (wrap-safe)
        h = shared.health()
        change_ver = shared.mcp_change_version
        on_change = (change_ver != applied_change_ver
                     or h["ethernet"] != last_eth_pub or h["mqtt"] != last_mqtt_pub)
        due = now_ms - diag_last_ms >= cfg.DIAG_INTERVAL_S * 1000
        if cfg.DIAG_ENABLE and client.connected and len(queue) == 0 and (due or on_change):
            temp_c = None
            if temp_adc is not None:
                try:
                    temp_c = diag.rp2040_temp_c(temp_adc.read_u16())
                except Exception:
                    temp_c = None
            snap = shared.mcp_diag_snapshot()
            boards_total = snap.get("boards_total", n_boards)
            boards_ok = snap.get("boards_ok", 0)
            health = mcp_health.health_summary(
                h["ethernet"], h["mqtt"], boards_total, boards_ok)
            counters = dict(snap.get("counters", {}))
            counters["reconnects"] = reconnects
            counters["dropped"] = queue.dropped
            state = diag.build_state(
                cfg.SW_VERSION, now_ms // 1000, ip_display,
                h["ethernet"], h["mqtt"], boards_total,
                gc.mem_free(), reconnects, queue.dropped, last_gesture,
                temp_c=temp_c, board_addrs=board_addrs,
                hw=getattr(cfg, "HW_VERSION", None), reset_cause=shared.reset_cause,
                health=health, boards_total=boards_total, boards_ok=boards_ok,
                mcp=snap.get("mcp", []), counters=counters,
                last_fault=snap.get("last_fault"), recent=snap.get("recent", []))
            try:
                diag.publish_state(client, cfg, device_id, state)
                diag_last_ms = now_ms
                applied_change_ver = change_ver
                last_eth_pub = h["ethernet"]
                last_mqtt_pub = h["mqtt"]
            except Exception as e:
                log.error("diag publish failed: %s" % e, every_ms=5000, key="diag")
                client.connected = False

        # ---- fault event stream (diag/event, non-retained) ----
        # core0 bumps event_version on each NEW fault; mirror it to a logbook-friendly
        # event topic. Queue-gated so it never delays an action publish.
        if (cfg.DIAG_ENABLE and client.connected and len(queue) == 0
                and shared.event_version != applied_event_ver):
            ev_ver, fault = shared.take_fault()
            if fault is not None:
                try:
                    diag.publish_event(client, cfg, device_id, fault)
                    applied_event_ver = ev_ver
                except Exception as e:
                    log.error("event publish failed: %s" % e,
                              every_ms=5000, key="evt")
                    client.connected = False
            else:
                applied_event_ver = ev_ver

        # identify: flash the LED white for ~3s after the command (non-blocking;
        # repeated activity pulses keep it visibly blinking to locate the board).
        if identify_until[0]:
            if now_ms < identify_until[0]:
                if led is not None and now_ms - identify_last[0] > 250:
                    led.notify_activity(now_ms)
                    identify_last[0] = now_ms
            else:
                identify_until[0] = 0

        # log mirror: surface the latest WARN/ERROR line promptly (still queue-gated,
        # so it never sits in front of an action). Only when the stash changed.
        if cfg.DIAG_ENABLE and client.connected and len(queue) == 0:
            entry = log_stash[0]
            if entry is not None and entry is not last_log_pub:
                try:
                    diag.publish_log(client, cfg, device_id, entry[0], entry[1],
                                     now_ms // 1000)
                    last_log_pub = entry
                except Exception:
                    client.connected = False

        # OTA: drive the NAK re-request while a download is in flight; otherwise run
        # boot-confirm once we've proven healthy.
        if client.connected and ota_rx[0] is not None:
            _ota_progress_check(now_ms)
        elif client.connected:
            _ota_confirm_if_healthy(now_ms)

        utime.sleep_ms(5)        # yield (releases GIL so core0 runs)
