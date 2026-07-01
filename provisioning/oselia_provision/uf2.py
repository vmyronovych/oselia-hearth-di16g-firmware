"""Resolve a UF2 image to a local path, preferring the repo-bundled copies (offline)."""
import os

from . import console
from .constants import (FLASH_NUKE_NAME, FLASH_NUKE_URL, MPY_UF2_NAME, MPY_UF2_URL)
from .paths import CACHE_DIR, UF2_DIR


def cached_uf2(url, name, override, min_size=1000):
    """Resolve a UF2 in order of preference:
      1. `override` (an explicit --*-uf2 path),
      2. a copy bundled in provisioning/uf2/ (offline, repo-shipped),
      3. the per-user cache (~/.cache/oselia),
      4. download from `url` (and cache it).
    Returns the path, or None on failure."""
    if override:
        if not os.path.isfile(override):
            console.die("UF2 file not found: %s" % override)
        return override
    bundled = os.path.join(UF2_DIR, name)
    if os.path.isfile(bundled) and os.path.getsize(bundled) > min_size:
        return bundled                         # shipped with the tool -> no network
    os.makedirs(CACHE_DIR, exist_ok=True)
    dest = os.path.join(CACHE_DIR, name)
    if os.path.isfile(dest) and os.path.getsize(dest) > min_size:
        return dest
    console.info("  downloading %s ..." % name)
    try:
        import urllib.request
        tmp = dest + ".part"
        urllib.request.urlretrieve(url, tmp)
        os.replace(tmp, dest)
        return dest
    except Exception as e:
        console.warn("  download failed: %s" % e)
        return None


def resolve_mpy(override):
    """The pinned MicroPython UF2 (bundle / cache / download), or None."""
    uf2 = cached_uf2(MPY_UF2_URL, MPY_UF2_NAME, override, min_size=100000)
    if not uf2:
        console.warn("  Fetch it manually and pass --mpy-uf2 PATH (see firmware/docs/flashing.md):")
        console.warn("    " + MPY_UF2_URL)
    return uf2


def resolve_nuke(override):
    """Raspberry Pi's flash_nuke.uf2 (bundle / cache / download), or None."""
    return cached_uf2(FLASH_NUKE_URL, FLASH_NUKE_NAME, override)
