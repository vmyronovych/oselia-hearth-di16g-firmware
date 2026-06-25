#!/usr/bin/env bash
# Broker-bounce reconnect REGRESSION TEST.
#
# Restarts the MQTT broker and verifies the board self-heals: status must go
# offline (CH9120 TCP dropped) and then back online (firmware re-runs the CH9120
# bring-up and reconnects). This is the regression guard for the bug where runtime
# MQTT reconnect wrote CONNECT into a dead CH9120 socket and stayed offline forever.
#
# Exit 0 = pass. Env: WATCH=60 (post-bounce watch secs), MOSQ_CONTAINER=mosquitto.
source "$(dirname "$0")/_common.sh"
WATCH="${WATCH:-60}"

echo "[1/3] baseline ..."
base="$(_t 4 mosquitto_sub -h "$BROKER" -p "$BROKER_PORT" -v -t 'hearth/+/status' 2>/dev/null | awk '{print $2}' | tail -1)"
echo "      status=${base:-<none>}"
[ "$base" = online ] || echo "      WARNING: board not online before bounce (test may be inconclusive)"

echo "[2/3] bouncing '$MOSQ_CONTAINER' (in 2s), watching ${WATCH}s ..."
( _settle 2; docker restart "$MOSQ_CONTAINER" >/dev/null 2>&1 ) &
log="$(_t "$WATCH" mosquitto_sub -h "$BROKER" -p "$BROKER_PORT" -v -t 'hearth/+/status' 2>/dev/null || true)"
echo "$log" | awk 'NF{print "      STATUS:", $2}'

echo "[3/3] verdict ..."
final="$(echo "$log" | awk 'NF{print $2}' | tail -1)"
saw_offline="$(echo "$log" | grep -c offline || true)"
if [ "$final" = online ] && [ "${saw_offline:-0}" -ge 1 ]; then
  echo "PASS: board went offline then self-recovered to online."
  exit 0
fi
echo "FAIL: final='${final:-<none>}' offline_seen=${saw_offline:-0} -- board did not self-heal."
echo "      (if offline_seen=0, the board never noticed the drop in ${WATCH}s; raise WATCH.)"
exit 1
