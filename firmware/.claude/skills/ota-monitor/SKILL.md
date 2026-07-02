---
name: ota-monitor
description: >-
  Observe and VERIFY an OTA firmware update on the bench RP2040-ETH board against the code
  and docs — does the observed flow match what OTA is supposed to do. Use when asked to
  watch/monitor/verify an OTA update, prove an OTA change works on hardware, or check that a
  firmware update installed cleanly. Live progress comes from the USB log; the broker's
  retained ota/state cross-checks the outcome; the full USB log is diffed against the
  firmware source + docs to catch wrinkles. Observer-only — it asks you to trigger the OTA.
---

# ota-monitor — verify an OTA update on the bench (oselia-only)

The true goal is **verification**: reconstruct what an OTA *should* do from the firmware
source and docs, capture what it *actually* did on the wire and the USB log, and report
where they diverge. Monitoring is just how the evidence is gathered. **Observer-only** — you
trigger the update (HA `update` card or `oselia ota publish`); this skill watches and judges.

All board and broker interaction goes through the `oselia` CLI (see the `oselia-provision`
skill). No raw `mpremote` / `mosquitto_*`, no `tools/*.sh`.

## Hard rules (do not violate)

1. **`oselia` only.** Board = `oselia board … / monitor`; broker = `oselia mqtt …`. If a step
   seems to need `mpremote`/`mosquitto_*`/a one-off script, that's rule 2.
2. **Missing capability → CRITICAL, STOP.** If a step needs something `oselia` doesn't
   expose, flag it CRITICAL and stop. Never fall back to `mpremote` or a hand-rolled tool.
   (Adding it to the CLI is a separate change — see `oselia-provision` → "Extending".)
3. **No USB logs → CRITICAL, STOP.** If the passive monitor captures zero bytes / no serial,
   you are guessing, and guessing is not verification.
4. **Dual source.** The outcome is trusted only when the **USB log** and the broker's
   **retained `ota/state`** agree. One channel is never enough.
5. **Analysis is mandatory, every run.** Step 6 (USB log vs source + docs) runs on SUCCESS
   too — a clean-looking update can still carry wrinkles worth a hypothesis.
6. **Read the source at analysis time.** Derive the expected sequence by reading the files in
   step 6 *this run* — never from a baked-in list here (it rots).

## Verdict taxonomy

- **SUCCESS** — USB log shows `OTA build <target> confirmed healthy`, retained `ota/state` is
  `stage: idle` with `running_version == target_version`, and the post-read board version ==
  target. Both channels agree.
- **FAILURE — aborted** — `OTA aborted: <reason>` on USB and/or retained `ota/state`
  `stage: error`. Report the reason/error field.
- **FAILURE — auto-reverted** — board returns on the *old* version, no `confirmed healthy`.
- **TIMEOUT** — no terminal state within the budget (default 60 s). Report last stage/percent.
- **CRITICAL / STOP** — hard rule 2 or 3 tripped. Emit the partial report and the blocker.

Collect these as **WARNING**s alongside the verdict (they don't end the run): repeated
`OTA NAK` (lossy link / CH9120 buffer pressure), `percent` stalling or going backwards, USB
vs MQTT disagreement on final version/stage, blank/unknown `target_version`.

## Inputs

Zero-argument by default; resolve everything from the board, the CLI, and the codebase:

- **Board / port** — first MicroPython board. **If more than one is detected, ASK which to
  use.** `--port` overrides.
- **Device id** — `oselia board id` (also printed by `board info`); the hardware unique id,
  not in `site.json`.
- **Broker + creds** — read from the board's own `site.json` in step 1
  (`oselia board cat site.json`): `broker_ip`, `broker_port`, `mqtt_user`, `mqtt_pass`. Do
  not guess.
- **Topics** — `hearth/<device_id>/ota/state` and `hearth/<device_id>/status` (base topic is
  `hearth`; the `ota/*` layout is defined in `firmware/src/net_task.py`).
- **Timeout** — default 60 s; `--timeout` overrides. (Heads-up: the ~20 s boot-confirm window
  means a real confirm can run close to 60 s.)

## Steps

### 1. Setup + BEFORE snapshot
- `oselia board list` — if >1 board, ask which. Lock onto that `--port` for board reads.
- `oselia board info` → record **running version**, active slot, and `device_id`.
- `oselia board cat site.json` → record `broker_ip`, `broker_port`, `mqtt_user`, `mqtt_pass`.
  Together these are the authoritative "currently running" read (from the board, per the goal).
- Print the **latest GitHub release** for context: `gh release list --limit 1` then
  `gh release view <tag>` (releases are tagged `fw-v<version>`, titled `Firmware X.Y.Z`).
- **Completion:** you have the board's before-version + `device_id` + broker, and the latest
  release is printed.

### 2. Attach the USB log (BEFORE you trigger)
Passive capture only sees output produced *after* it attaches, so attach first. `monitor
--passive` runs until killed, so background it to a logfile:
```
oselia monitor --passive > "$SCRATCH/ota-usb.log" 2>&1   # run_in_background
```
Give it ~2 s, then confirm bytes are landing in the logfile.
- **Completion:** the logfile is growing (serial is live). **Zero bytes ⇒ CRITICAL, STOP
  (rule 3).** Note: the board must not be held by another `oselia` command (serial lock) —
  do not run board reads while the passive monitor holds the port.

### 3. Prompt to trigger
Tell the operator: *"Monitoring is live. Trigger the OTA now — HA `update` card or
`oselia ota publish --broker <ip> --device <id>` — and I'll watch."* Then start the timeout
clock.
- **Completion:** operator confirms the OTA was triggered (or the first `OTA start` line
  appears in the logfile).

### 4. Watch live (USB)
Poll the logfile every few seconds and narrate meaningful events with elapsed time:
`OTA start -> target <v> (<n> chunks, <bytes>)`, periodic `downloading` percent, `OTA NAK`
(⚠ WARNING), `OTA verified -> slot <x>`, the reset, and `OTA build <v> confirmed healthy`.
**Stop early** the instant a terminal condition is reached — don't wait out the timeout.
- **Completion:** a terminal condition (SUCCESS / aborted / reverted) is seen in the log, or
  the timeout elapses ⇒ TIMEOUT. Kill the background monitor to release the port.

### 5. AFTER snapshot + MQTT cross-check
With the port released:
- `oselia board info` → the **after** running version (from the board).
- Read the broker's retained state (retained messages arrive immediately, elapsed ≈ 0):
  ```
  oselia mqtt watch "hearth/<id>/ota/state" "hearth/<id>/status" \
    --broker <ip> [--user U --password P] --for 3 --json
  ```
  Record final `stage`, `running_version`, `target_version`, `percent`, `error`, and the
  `status` (online/offline).
- **Completion:** you have before→after board versions and the retained `ota/state` +
  `status`, and have checked whether the two channels agree (disagreement ⇒ WARNING).

### 6. Deep analysis (mandatory — rule 5)
Read the OTA path *this run* and diff the captured `$SCRATCH/ota-usb.log` against the expected
sequence:
- `firmware/src/net_task.py` — the `_ota_*` handlers and their `log.*` lines (the wire
  behaviour: start → downloading → NAK/re-request → verified → reset → confirm).
- `firmware/src/ota.py` — the boot-confirm / auto-revert state machine and bundle
  verify/apply.
- `firmware/docs/ota.md` and `firmware/docs/spec.md` — the design contract (spec wins on
  conflicts).

Diff for **wrinkles**: unexpected ordering, extra retries/resets (e.g. two boots before
confirm — a near-miss on auto-revert), NAK storms that recovered, missing expected lines,
odd timing, or errors that don't map to a clean abort. Write a short **hypothesis** for each
wrinkle, anchored to the relevant `file:line`.
- **Completion:** every stage of the expected sequence is accounted for as present-as-expected
  or a named wrinkle-with-hypothesis. A clean run states "matched expected sequence, no
  wrinkles."

### 7. Verdict (three phases)
Print:
- **Setup** — board (port, id, before-version, slot), broker, latest release.
- **Live** — the timestamped USB narration from step 4.
- **Verdict** — header (✅ SUCCESS / ❌ FAILURE(reason) / ⏱ TIMEOUT / ⛔ CRITICAL); a table of
  running version **before → after** and **USB outcome vs retained `ota/state`** (the
  dual-source agreement); the WARNING/CRITICAL list; and the step-6 analysis + hypotheses.

### 8. If anything went wrong → propose an issue
Only when the verdict is not a clean SUCCESS (a FAILURE, TIMEOUT, or any wrinkle/WARNING),
**propose** filing a GitHub issue. Do not file on a clean run.
- If the operator **declines**, stop here.
- If they **accept**, first run the **grill-me** skill to sharpen the problem statement with
  them, *then* create the issue.

### 9. File the issue (only after grill-me)
`gh issue create` (see `docs/agents/issue-tracker.md`) with:
- **Title** — concise symptom, e.g. `OTA: <wrinkle> on <before>→<target> bench run`.
- **Body** — the grill-sharpened problem statement, then an **Evidence** section: the verdict
  block, before→after versions, the retained `ota/state`, the hypothesis, links to the
  diverging `file:line` (`net_task.py`, `ota.py`, `docs/ota.md`/`spec.md`), and the **full**
  captured USB log in a collapsed `<details>` code block.
- **Label** — `--label ready-for-agent` (a grill-me pass + bench repro + evidence + hypothesis
  is a fully-specified issue). Fall back to `needs-triage` only if the grill concluded it's
  still fuzzy.
- **Completion:** the issue URL is printed.
