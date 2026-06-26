<div align="center">

# OSELIA Hearth (DI16-G) — Firmware & Provisioning

**MicroPython firmware, host-side installer, and Home Assistant assets for the OSELIA
Hearth gateway** — a wired 24 V wall-switch input hub for a locally-controlled smart home.

[![ci](https://github.com/vmyronovych/oselia-hearth-di16g-firmware/actions/workflows/ci.yml/badge.svg)](https://github.com/vmyronovych/oselia-hearth-di16g-firmware/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/vmyronovych/oselia-hearth-di16g-firmware?filter=fw-v*)](https://github.com/vmyronovych/oselia-hearth-di16g-firmware/releases)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

</div>

---

## What it is

The **Hearth** (RP2040-ETH) reads up to 128 isolated 24 V inputs over I²C, classifies
every press (**single / double / long**), and publishes it to **Home Assistant over
MQTT** — no cloud, no DNS, deterministic timing. This repo holds everything needed to
build, flash, provision, and update a unit.

> The first-party Home Assistant **integration** lives in its own HACS repo:
> **[vmyronovych/oselia-hearth-di16g-ha](https://github.com/vmyronovych/oselia-hearth-di16g-ha)**.
> The broader product (hardware, architecture) lives in
> **[vmyronovych/oselia](https://github.com/vmyronovych/oselia)**.

## Repository structure

| Path | What's inside |
|------|---------------|
| [`firmware/`](firmware/) | MicroPython firmware (RP2040-ETH): inputs → press detection → MQTT, device diagnostics, two-way control, and OTA. See its [README](firmware/README.md) and [SPEC](firmware/SPEC.md). |
| [`provisioning/`](provisioning/) | USB installer wizard that configures a fresh unit and wires up the HA side — see the [install guide](provisioning/INSTALL.md). |
| [`homeassistant/`](homeassistant/) | HA assets the installer pushes: the `/oselia-hearth` dashboard generator + the switch blueprint, plus the integration [design contract](homeassistant/INTEGRATION_SPEC.md). |

## Getting started

- **Install a gateway** → [`provisioning/INSTALL.md`](provisioning/INSTALL.md)
- **Hack on the firmware** → [`firmware/README.md`](firmware/README.md)
- **Understand the design** → [`firmware/SPEC.md`](firmware/SPEC.md)
- **Cut a firmware release** → [`firmware/RELEASING.md`](firmware/RELEASING.md)

## OTA / release feed

Firmware releases are published here as `fw-v*` GitHub Releases; the HA integration's
release feed points at this repo's
[`releases/latest`](https://github.com/vmyronovych/oselia-hearth-di16g-firmware/releases/latest).
See [`firmware/RELEASING.md`](firmware/RELEASING.md) and [`firmware/OTA_SPEC.md`](firmware/OTA_SPEC.md).

## License

[GPL-3.0](LICENSE).
