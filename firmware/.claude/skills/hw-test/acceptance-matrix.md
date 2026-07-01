# Acceptance evidence matrix — `docs/spec.md §10`

Durable, versioned source of truth for the `hw-test` skill. Each row maps a §10 criterion to
its **USB-log** proof, its **MQTT** proof, and the `oselia` command(s) that gather each. A
criterion PASSes only when **both** channels are gathered and agree (see the skill's Hard
rules). `<id>` = `oselia board id`; base topic = `hearth`.

Conventions:
- USB via `oselia monitor --passive`. MQTT via `oselia mqtt watch … --json --for N`.
- `status`/`cfg`/`diag` are **retained** → ignore the first (elapsed≈0) hit; trust only a
  message that arrives *after* the trigger, or whose `uptime_s` matches this run.
- Gesture proof lines (`published …`, `gesture idx…`) are `log.debug` → first raise verbosity:
  `oselia mqtt cmd <id> log_level debug`.

| # | Criterion | USB-log proof (regex) | MQTT proof | oselia to gather / trigger | Rig |
|---|-----------|-----------------------|-----------|----------------------------|-----|
| 1 | Boot + CH9120 TCP client up | `configuring CH9120\.\.\.` then `CH9120 DHCP lease: ` (DHCP) | (covered via #2 online) | `oselia monitor --passive` during `oselia provision` | auto |
| 2 | MQTT CONNECT + LWT, `status`=online | `MQTT online \(CONNACK ok\)` | `hearth/<id>/status` == `online` (retained), published *after* boot | `oselia mqtt watch hearth/<id>/status --for 40` | auto |
| 3 | OSELIA entities; NO firmware HA discovery | (n/a — absence proof is MQTT) | `oselia mqtt watch 'homeassistant/#' --for 6 --expect-absent '.'` exits 0 (nothing) | watch above; entities verified in HA registry, not firmware | auto |
| 4 | Gesture classification (single/double/long) | `gesture idx<i>=<g>` and `published b<b> in<p>=<g>` | `hearth/<id>/board<b>/input<p>/action` payload ∈ {single,double,long}; no stray `single` before `double`/`long` | `oselia mqtt cmd <id> log_level debug`; **operator press** (`--interactive`) | human |
| 5 | Simultaneous inputs classified independently | two `gesture idx…` lines, distinct indices | two `…/action` messages, one per input, correct each | operator presses 2 inputs together (`--interactive`) | human |
| 6 | ISR does no I²C/alloc; heavy work in loop | (static) | (static) | code/host review + `tests/test_press_detector.py`; not a wire check | host |
| 7 | Host tests + `py_compile` green | (host) | (host) | `python3 -m py_compile src/*.py`; `for t in tests/test_*.py; do python3 "$t"; done` | host |
| 8 | Reconnect w/ backoff; resub + cfg reseed | `link/keepalive lost; reconnecting` → `connect failed: .* \(retry \d+ms\)` → `MQTT online \(CONNACK ok\)` → `resubscribed cmd/# \+ reseeded cfg` | `status` goes `online`→(offline)→`online`; `cfg` re-published after reconnect | `oselia mqtt bounce --down 8`; watch `hearth/<id>/status` + monitor | auto |
| 9 | Input timing unaffected during reconnect | gesture lines appear during the outage; flush after `MQTT online` | `…/action` for the press arrives *after* reconnect (buffered then flushed) | `oselia mqtt bounce --down 8`; operator presses during outage (`--interactive`) | human |
| 10 | Hung core → watchdog reset | next boot: `boot: reset_cause=wdt` (preceded by `ACCEPTANCE: stalling core1 …`) | `status` LWT→`offline` then fresh `online` with low `diag uptime_s` | `oselia mqtt cmd <id> _debug_stall` (needs `--acceptance` build) | hook |
| 11 | MCP glitch retried; absent chip recovered | `board<b> read failed` → `MCP recovery L1/L2` → `board<b> MCP@0x.. ready` | `diag/state` `boards_ok` dips then returns; a `diag/event` fault record | `oselia mqtt cmd <id> _debug_mcp_fault <b>` (needs `--acceptance`); watch `hearth/<id>/diag/#` | hook |
| 12 | Multi-board numbering; one absent doesn't shift others | `MCP boards: N (…)`; per-board `gesture idx…` | `…/board<b>/input<p>/action` for each wired board, correct numbering | needs ≥2 MCP chips wired | **BLOCKED** unless wired |

## Extending for new functionality (change-scoped runs)

When a diff adds behavior not covered above, append a row with the same three columns. If no
existing USB log proves the new behavior, **propose a debug-log diff** in `src/` (HR4) rather
than inferring success — a criterion with only one proof channel is BLOCKED-pending-log, never
PASS. If proving it needs an `oselia` capability that doesn't exist, STOP and flag the gap
(HR2) instead of improvising.
