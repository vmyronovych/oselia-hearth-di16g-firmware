# Releasing firmware (GitHub → Home Assistant OTA)

Firmware ships to units **over Ethernet** via the OSELIA HA integration — no USB after
the first provision. GitHub Releases is the feed; HA polls it and offers the update.

## Branch model

- `main` is the **release branch** — always green and releasable.
- All WIP lives in **feature branches**; changes land on `main` only via PR.
- The `ci` workflow (`.github/workflows/ci.yml`) runs `py_compile` + the host test suites
  on every PR; its job is named **`test`**.
- **Branch protection is kept as code** in `.github/rulesets/protect-main.json` (a GitHub
  *ruleset*): require a PR, require the `test` check to pass, and block direct pushes /
  force-pushes / deletion of the default branch. The `apply-rulesets` workflow pushes it
  to GitHub via the API (idempotent), so the rules live in git, not just the UI.
  **Bootstrap (once):**
  1. Create a **fine-grained PAT** with this repo's *Administration: Read and write*, and
     add it as the repo secret **`ADMIN_PAT`** (Settings → Secrets and variables → Actions).
     This is required — the built-in `GITHUB_TOKEN` *cannot* manage rulesets
     (`administration` isn't a grantable token scope).
  2. Merge to the default branch, then run **`apply-rulesets`** from the Actions tab.

  After that it re-applies whenever the ruleset file changes. Adjust
  `required_approving_review_count` in the ruleset if you want mandatory reviews.

  > **Plan requirement:** rulesets and branch protection are **free on public repos**
  > but need **GitHub Pro** (or Team/Enterprise) on a **private** repo. On a private
  > free repo the workflow can't apply them and **skips with a warning** (it stays
  > green). Until you upgrade or make the repo public, treat `ci` as advisory: it still
  > runs on every PR and shows pass/fail — it just can't be a *hard* merge gate.

## Tagging and releasing

Tagging is automatic; releasing is a deliberate, manual step.

**Auto-tag (`.github/workflows/auto-tag.yml`)** — after a `main` commit's **`ci` run
passes** (it runs on `ci` success via `workflow_run`, and tags the exact validated
commit — a red build is never tagged), it creates `fw-v<major.minor>.<next-patch>`:
- `major.minor` is the **base** read from `src/config.py` `SW_VERSION` (so `0.1.0` → the
  `0.1` series). To start a new series, bump `SW_VERSION`'s major.minor in a PR.
- the **patch auto-increments** per merge: `fw-v0.1.1`, `fw-v0.1.2`, `fw-v0.1.3`, …

So every `main` state has its own tag, but **tags don't release on their own.**

**Release (`.github/workflows/firmware-release.yml`)** — run it manually from the
**Actions tab → Run workflow**. Leave `tag` blank to release the **latest** `fw-v*` tag,
or type a specific one (e.g. `fw-v0.1.2`). It:
1. checks out that tag and runs the host tests (release gate),
2. **stamps the version from the tag** into the bundled `config.py` (the tag is the
   single source of version truth — the device reports exactly this; `site.json` still
   overlays per-unit values),
3. builds the OTA bundle + `manifest.json` with `tools/ota_build.py` — modules are
   compiled to MicroPython bytecode (`.mpy`) before packaging, so the bundle is ~70%
   smaller (fewer MQTT chunks → less loss exposure); the device imports `.mpy`
   transparently and the on-device contract (manifest names + per-file/whole sha256) is
   unchanged. The workflow pins `mpy-cross==1.27.0.post2` (emits `.mpy` v6.3, accepted by
   the board's MicroPython 1.28.0). `--no-mpy` builds raw `.py` for local iteration.
4. publishes a **GitHub Release** for the tag with both assets (auto-generated notes).

The committed `src/config.py` `SW_VERSION` patch is just a base/dev default; releases
carry the tag version, so they can't drift.

## Home Assistant side

**The provisioning wizard sets this up automatically.** `provision.py --oselia` adds the
OSELIA integration to HA and configures the firmware release feed (and, for a private
repo, the GitHub token). So a freshly provisioned unit is wired to the feed with no
manual HA steps. To set/override it by hand: OSELIA → **Configure** → *Firmware release
feed URL* (+ *GitHub token* for a private repo).

**Public repo** — point the feed at the stable latest-release manifest (always resolves
to the newest non-prerelease release's asset):
```
https://github.com/vmyronovych/oselia-hearth-di16g-firmware/releases/latest/download/manifest.json
```

**Private repo** — `releases/.../download/...` URLs require a browser session, so use the
**GitHub Releases API** plus a **token**:
```
feed:  https://api.github.com/repos/vmyronovych/oselia-hearth-di16g-firmware/releases/latest
token: a fine-grained PAT with read-only "Contents" access to this repo
```
The integration sends the token, reads the release's assets, and downloads the `.bundle`
via its authenticated asset URL. The token is stored **HA-side only** (the gateway never
sees it). Provide it to the wizard via `--github-token`, `$OSELIA_GH_TOKEN`, or
`~/.config/oselia/gh_token`.

From then on: a new release → HA shows "Firmware update available" → click **Install**.
HA downloads the bundle from GitHub (HTTPS) and streams it to the gateway over the local
broker — the device never touches the internet.

## Notes

- **Beta channel:** mark a release as a *prerelease* (e.g. tag `fw-v0.7.0-rc1`); the
  `latest/download` URL skips prereleases, so stable units aren't offered it.
- **Integrity vs authenticity:** the device verifies the bundle sha256 (corruption);
  the download is HTTPS from GitHub. **Signing** the bundle (authenticity) is a sensible
  future hardening — see `OTA_SPEC.md` "Out of scope".
