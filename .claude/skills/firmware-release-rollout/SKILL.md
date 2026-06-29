---
name: firmware-release-rollout
description: >-
  Attach the right bilingual end-user rollout note whenever a PR or GitHub Release for
  this repo ships new firmware (a diff under firmware/src/** or a new OTA bundle). Firmware
  reaches units over-the-air via the OSELIA HA integration's update card, so the note is
  short — update in HA, click Install, auto-revert keeps it safe. Adapted from the
  oselia-waveshare-relay-ha "blueprint-release-rollout" skill. Use when asked to "open a
  PR" / "cut a release" / "write release notes" / "draft the PR body" and the diff touches
  firmware/src/** (or the OTA bundle), or when reminding a user what to do after a firmware
  update.
---

# Firmware release rollout

Firmware ships **over Ethernet via the OSELIA Home Assistant integration**, not USB. HA
polls the GitHub release feed, shows "Firmware update available", and on **Install** streams
the OTA bundle to the gateway over the local MQTT broker (`firmware/OTA_SPEC.md`,
`firmware/RELEASING.md`). An A/B slot layout with a boot-confirm gate means a bad build
**auto-reverts** — an update can never strand a unit.

What the rollout note must convey:

- **Open the device in Home Assistant and click *Install* on the firmware update card.
  That's it** — HA downloads the bundle and applies it over the local broker; the device
  reboots into the new build and confirms itself once it's back online and healthy.
- **It's safe.** Updates are A/B with auto-revert: a build that won't boot or won't reach
  the broker rolls back to the previous version automatically; a power cut mid-update leaves
  the running version untouched. Per-unit identity/credentials (`site.json`) and your tuned
  settings (long-press/double-tap/debounce, names) are preserved across updates.
- **Nothing to re-enter.** Discovery, entities, and automations reconnect on their own after
  the reboot.

As of the **.mpy bundle build (fw 0.8.0+)** the OTA bundle ships precompiled MicroPython
bytecode (~70% smaller → fewer chunks → more robust download); this is transparent to the
user and changes nothing about how to apply an update.

## Required layout (do not drop any of these)

The rollout section the user receives **must**:

1. Be **two root-level collapsible `<details>` blocks, one per language, Ukrainian first**
   (`<details open>`) and English second (`<details>`). GitHub-Flavored Markdown has no
   tabs; `<details>` is the native equivalent. There is **no** shared summary outside the
   blocks — a reader opens one block and has everything in their language.
2. Contain, in each block: (a) the release's **issue/fix summary** in that language, then
   (b) a **link to `UPGRADING.md`** for how to apply. The how-to-apply *steps* are not
   repeated in the note — they live in the canonical `firmware/UPGRADING.md`. **Every
   release note and PR body must carry this link.**

`rollout-snippet.md` encodes this; fill only `<SUMMARY_UA>` / `<SUMMARY_EN>` with the
per-release summary in each language. If you edit the snippet, preserve the layout and the
upgrade-guide link.

## What to do

When the diff (PR) or the release contents (since the previous tag) ship new firmware
(touch `firmware/src/**`, or produce a new OTA bundle):

1. Confirm it: `git diff --name-only <base>..<head> -- 'firmware/src/**'` (PR) or
   `git diff --name-only <prev_tag>..<new_tag> -- 'firmware/src/**'` (release).
2. Take the canonical text from [`rollout-snippet.md`](rollout-snippet.md) and fill only
   `<SUMMARY_UA>` / `<SUMMARY_EN>` (the issue/fix summary in each language). Paste it
   **whole** — it already is the two `<details>` blocks plus the `UPGRADING.md` link.
   - **PR body:** paste the filled snippet as its own section.
   - **Release notes:** paste it into the body (`gh release create --generate-notes` won't
     add it — append it yourself, or `gh release edit fw-vX.Y.Z --notes-file …`). Note the
     release workflow (`firmware-release.yml`) currently runs `--generate-notes`; add the
     rollout block on top via `gh release edit` after it publishes.
3. If the release changes *how updates work* (not just this fix), update
   **`firmware/UPGRADING.md`** too — it is the single source of the apply steps every
   release links to.

## Keep it consistent

`rollout-snippet.md` is the single source of the user-facing wording — edit it there, not
inline, so PR bodies and release notes never drift. This mirrors `firmware/RELEASING.md`
(release-engineer-facing) and `firmware/UPGRADING.md` (end-user-facing).
