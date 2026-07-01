# Firmware docs

Reference material for the Hearth firmware. The front door is
[`../README.md`](../README.md); the agent working agreement is
[`../CLAUDE.md`](../CLAUDE.md).

| Doc | What's in it |
|-----|--------------|
| [`spec.md`](spec.md) | The full specification / contract — read this first. Behaviour, concurrency model, acceptance criteria, HW-verify items. |
| [`upgrading.md`](upgrading.md) | End-user upgrade guide (bilingual UA/EN). **Every release links here** — keep the apply steps in this one file. |
| [`hardware.md`](hardware.md) | Single home for wiring **and** device init: pin map + powering rules + confirmed CH9120 / MCP23017 / press-detection facts + POC provenance. |
| [`mqtt-contract.md`](mqtt-contract.md) | Canonical wire contract: topics, action/command payloads, `diag/state` schema, error taxonomy, OTA topics. The firmware owns it; HA links here. |
| [`ota.md`](ota.md) | OTA mechanism: A/B slots, thin loader, boot-confirm / auto-revert state machine. |
| [`flashing.md`](flashing.md) | Flashing the MicroPython interpreter onto a new RP2040-ETH (the manual reference behind `oselia flash`). |
| [`bringup.md`](bringup.md) | Bench bring-up checklist — the physical/HA steps scripts can't do. |
| [`releasing.md`](releasing.md) | Cut a firmware release: branch model, auto-tag, release workflow, HA feed config. |

`rp2040-eth-pinout.png` is the stock Waveshare pinout image referenced by `hardware.md`.
