<!--
Per-release rollout section for the PR body and release notes. Goes FIRST (consumer-first),
above any ## Technical details section.
Fill <SUMMARY_UA> / <SUMMARY_EN> with this release's PLAIN-LANGUAGE problem + outcome in each
language -- what was wrong / what gets better for a homeowner or installer, no jargon, no file
names. Then paste this whole block in. The HOW-TO-APPLY steps are NOT duplicated here -- they
live in the canonical firmware/docs/upgrading.md, which every block links to. Keep the steps there.

Layout: two root-level collapsible language blocks, Ukrainian first (`<details open>`),
English second (`<details>`) -- GFM has no tabs; `<details>` is the equivalent. Each block:
the per-release summary, then the upgrade-guide link.
-->

<details open>
<summary><b>🇺🇦 Українською — що змінилось</b></summary>

<br>

<SUMMARY_UA>

**Як застосувати:** відкрийте пристрій у Home Assistant і натисніть **Встановити** на картці
оновлення прошивки — решта автоматично (див. [інструкцію з оновлення](https://github.com/vmyronovych/oselia-hearth-di16g-firmware/blob/main/firmware/docs/upgrading.md)).

</details>

<details>
<summary><b>🇬🇧 English — what changed</b></summary>

<br>

<SUMMARY_EN>

**How to apply:** open the device in Home Assistant and click **Install** on the firmware
update card — the rest is automatic (see the [upgrade guide](https://github.com/vmyronovych/oselia-hearth-di16g-firmware/blob/main/firmware/docs/upgrading.md)).

</details>
