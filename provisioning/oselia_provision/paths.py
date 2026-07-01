"""Resolve the on-disk locations the tool needs (UF2 images, firmware src, logo).

Editable install (`pip install -e .` from provisioning/) keeps the repo layout, so
these resolve relative to this package. Each is env-overridable for non-standard
layouts / CI. The UF2 resolver in uf2.py also falls back to ~/.cache/oselia + download.
"""
import os

# .../provisioning/oselia_provision/paths.py -> PKG_DIR, then PROVISIONING_DIR, REPO_ROOT
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
PROVISIONING_DIR = os.path.normpath(os.path.join(PKG_DIR, ".."))
REPO_ROOT = os.path.normpath(os.path.join(PROVISIONING_DIR, ".."))

# UF2 images shipped alongside the tool so flashing works with no internet.
UF2_DIR = os.environ.get("OSELIA_UF2_DIR", os.path.join(PROVISIONING_DIR, "uf2"))

# The firmware app the tool deploys to /slots/a. Override for non-standard layouts.
SRC_DIR = os.environ.get(
    "OSELIA_FIRMWARE_SRC", os.path.join(REPO_ROOT, "firmware", "src"))

# The kept OTA pipeline scripts (bundle builder + publisher). `oselia ota …` fronts these
# so the acceptance suite only ever calls `oselia`; the OTA logic + its host tests stay put.
OTA_BUILD = os.environ.get(
    "OSELIA_OTA_BUILD", os.path.join(REPO_ROOT, "firmware", "tools", "ota_build.py"))
OTA_PUBLISH = os.environ.get(
    "OSELIA_OTA_PUBLISH", os.path.join(REPO_ROOT, "firmware", "tools", "ota_publish.py"))

# Brand logo embedded into the rendered dashboard YAML (optional; --no-logo skips it).
LOGO_SVG = os.environ.get(
    "OSELIA_LOGO_SVG", os.path.join(REPO_ROOT, "homeassistant", "hearth_logo.svg"))

CACHE_DIR = os.path.expanduser("~/.cache/oselia")
