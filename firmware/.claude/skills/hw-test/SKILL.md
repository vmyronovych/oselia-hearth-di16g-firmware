---
name: hw-test
description: >-
  Bring up, debug, and run HARDWARE ACCEPTANCE on the RP2040-ETH switch firmware on the
  real board, driven entirely through the `oselia` CLI. Use when asked to flash/provision
  the board, prove a firmware change works on hardware, run the §10 acceptance suite, watch
  MQTT action/status/diag/cfg topics, exercise the broker-bounce reconnect path, or capture
  USB firmware logs. Every criterion is proven on BOTH the USB log AND the MQTT wire.
---

# hw-test — on-hardware acceptance & debug (oselia-only)

This skill drives a physical Hearth unit to a **per-criterion PASS/FAIL/BLOCKED verdict**
against `docs/spec.md §10`, and doubles as the bring-up/debug loop. **All board and broker
interaction goes through the `oselia` CLI** (see the `oselia-provision` skill). There is no
`tools/*.sh` and no raw `mpremote`/`mosquitto_*` — if a step seems to need one, that's a
signal to STOP (see Hard rules).

## Hard rules (do not violate)

1. **`oselia` only.** Never shell out to `mpremote`, `mosquitto_pub/sub`, or any script under
   `tools/`. Board = `oselia board … / flash / provision / monitor`; broker = `oselia mqtt …`.
2. **Missing capability → STOP.** If a step needs something `oselia` doesn't expose, STOP and
   flag it (add it to the CLI in a separate change — see the `oselia-provision` skill's
   "Extending"). Never hand-craft a one-off tool for a session.
3. **No USB logs → STOP.** If `oselia monitor` can't stream the firmware log, STOP. Without
   real logs you are guessing; guessing is not acceptance.
4. **Prove new functionality with logs.** If the existing USB logs can't *prove* a behavior,
   propose a concrete firmware debug-log addition (a diff) — don't infer that it "should work".
5. **Dual proof.** A criterion PASSes only when confirmed on **both** the USB log **and** the
   MQTT wire, and the two agree. One channel is never enough.

## Verdict taxonomy

- **PASS** — both USB-log and MQTT evidence gathered and they match the expectation.
- **FAIL** — both proofs gathered, but they contradict the expectation (wrong gesture, no
  `online`, HA discovery present when it must be absent, …).
- **BLOCKED** — can't gather a required proof this run:
  - rig/hardware absent (e.g. §12 needs a 2nd MCP chip wired),
  - a proof channel doesn't exist yet → emit the proposed debug-log/`oselia` diff; never PASS
    on one channel.
- **STOP (abort run)** — host gate red, or Hard rule 2/3 tripped. Emit the partial report and
  the blocking reason.

## Modes

- **Default (change-scoped, PR gate):** read the diff (`git diff main…HEAD`), derive the new
  behavior + the §10 criteria it touches, and prove only those.
- **`--full` (release gate):** walk all 12 §10 criteria.

## Preconditions (every run)

1. **Host gate first (hard):** `python3 -m py_compile src/*.py` and every `tests/test_*.py`
   must pass. Red → STOP before touching the board (never deploy code that fails the gate).
2. **Known baseline:** provision the unit to a clean, recorded state:
   - `oselia flash` only if `oselia board version` ≠ the pinned interpreter.
   - `oselia provision --broker <ip> [--acceptance]` → fresh `/slots/a`. Use `--acceptance`
     to enable the §10/§11 fault-injection hooks (bench-only; production never carries them).
   - Clear retained topics so a stale snapshot can't false-PASS:
     `oselia mqtt pub hearth/<id>/status "" --retain` (and `…/cfg` if needed).
   - Record `oselia board id`, `oselia board version`, `git rev-parse --short HEAD`,
     `build=acceptance|production` into the report header.

## The evidence matrix

`acceptance-matrix.md` (next to this file) is the durable, versioned source of truth: each
§10 criterion → the exact **USB-log** pattern, the exact **MQTT** assertion, and the `oselia`
command(s) that gather each. Read it and drive each row. For *new* functionality not yet in
the matrix, add a row following the same dual-proof rule (and propose the debug log if none
proves it).

**Retained-vs-live disambiguation:** `status`/`cfg`/`diag` are retained, so a watcher sees the
*prior* run's value first (elapsed ≈ 0 in `oselia mqtt watch --json`). Only trust a message
that arrives *after* your trigger, or whose `uptime_s`/timeline matches this run.

## Core oselia commands

- **USB log:** `oselia monitor --passive` (listen without restarting) or `oselia monitor`.
- **Watch MQTT:** `oselia mqtt watch <topics…> --for N [--json] [--expect-absent REGEX]`.
- **Publish / clear retained:** `oselia mqtt pub <topic> <payload> [--retain]`.
- **Control command:** `oselia mqtt cmd <id> <name> [payload]` — real names: `reboot`,
  `identify`, `long_ms`/`double_gap_ms`/`debounce_ms`, `log_level`; acceptance-only:
  `_debug_stall` (§10), `_debug_mcp_fault <board>` (§11).
- **Bounce broker (§8/§9):** `oselia mqtt bounce [--down N] [--container mosquitto]` (host
  Docker; not a board action).

## Running a criterion (the loop)

For each matrix row: start `oselia monitor --passive` (capture USB) and `oselia mqtt watch`
in parallel → apply the trigger via `oselia` (or, for §4/§5/§9, prompt the operator) → assert
the USB pattern AND the MQTT assertion → record PASS/FAIL/BLOCKED + the two evidence lines.

**Human-press criteria (§4/§5/§9)** need a physical 24 V switch press this skill can't
actuate. Default run: **BLOCKED**, listing the exact manual steps. With `--interactive`:
prompt the operator ("tap input 1 once", "hold input 1 >LONG_MS", for §9 "press input 2 during
the outage I just triggered"), then verify USB+MQTT within a timeout; no press in time → BLOCKED.

**Coverage on the single-board rig:** 1,2,3,7 automatable · 4,5,9 need `--interactive` ·
8 via `mqtt bounce` · 10,11 via the `--acceptance` hooks · 6 static/host · **12 BLOCKED**
until a 2nd MCP chip is wired.

## Report

Emit a Markdown verdict table (criteria as rows: verdict · USB evidence · MQTT evidence ·
`oselia` command) to the session scratchpad, plus a console summary. Header = provenance
(timestamp, `board id`, `version`, git SHA, `build`, broker, boards wired). Surface every
HR4 log-proposal / HR2 `oselia` gap as a concrete diff in the report — **never auto-apply**.
Change-scoped runs: **offer** to post the table as a PR comment (`gh pr comment`). The verdict
is advisory — a human decides accept/reject. Nothing is committed by the run.

## Debug loop (non-acceptance)

On a hardware fault: reproduce via the relevant `oselia` command → capture USB with
`oselia monitor` → localize in `src/` → fix → host gate → `oselia provision` → re-prove.
Mark hardware-confirmed assumptions with `# HW-VERIFY:` and keep the proven MQTT wire format
in `mqtt_packets` intact.
