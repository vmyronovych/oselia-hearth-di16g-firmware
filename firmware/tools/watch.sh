#!/usr/bin/env bash
# Watch broker traffic for N seconds. Uses device-id wildcards, so it works for any
# board. Usage: watch.sh [status|actions|discovery|diag|all] [seconds]
#   status    -> availability online/offline (LWT)
#   actions   -> gesture publishes (.../boardN/inputM/action = single|double|long)
#   discovery -> retained HA device-automation configs
#   diag      -> diagnostics: the retained diag/state JSON + its HA diag entities
source "$(dirname "$0")/_common.sh"

what="${1:-all}"; secs="${2:-30}"
st='hearth/+/status'
act='hearth/+/+/+/action'
disc='homeassistant/device_automation/+/#'
dst='hearth/+/diag/state'                 # retained telemetry snapshot
# diag entities are sensor/binary_sensor configs; '+' must be a whole level (MQTT
# forbids partial-level wildcards like 'diag_+'), so match by component level.
ddisc_s='homeassistant/sensor/+/+/config'
ddisc_b='homeassistant/binary_sensor/+/+/config'
case "$what" in
  status)    topics=(-t "$st") ;;
  actions)   topics=(-t "$act") ;;
  discovery) topics=(-t "$disc") ;;
  diag)      topics=(-t "$dst" -t "$ddisc_s" -t "$ddisc_b") ;;
  all)       topics=(-t "$st" -t "$act" -t "$disc" -t "$dst") ;;
  *) echo "usage: watch.sh [status|actions|discovery|diag|all] [seconds]"; exit 2 ;;
esac

echo "Watching '$what' on $BROKER:$BROKER_PORT for ${secs}s ..."
_t "$secs" mosquitto_sub -h "$BROKER" -p "$BROKER_PORT" -v "${topics[@]}" || true
