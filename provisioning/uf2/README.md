# Bundled UF2 images

These RP2040 UF2 files ship with the tool so flashing works **without internet**. `oselia`
prefers a file here over the network (resolution order: explicit `--mpy-uf2` / `--erase-uf2`
→ **this folder** → `~/.cache/oselia` → download — see `oselia_provision/uf2.py`).

| File | What | Used by | Source |
|------|------|---------|--------|
| `RPI_PICO-20260406-v1.28.0.uf2` | MicroPython 1.28.0 (RPI_PICO build) — the pinned interpreter | `oselia flash` / `oselia provision` / `--mpy-uf2` default | <https://micropython.org/resources/firmware/RPI_PICO-20260406-v1.28.0.uf2> |
| `flash_nuke.uf2` | Raspberry Pi flash eraser (universal nuke) — wipes the whole flash to bare metal | `oselia erase` / a wiped `oselia flash` | <https://datasheets.raspberrypi.com/soft/flash_nuke.uf2> |

The file **names must match** `MPY_UF2_NAME` / `FLASH_NUKE_NAME` in
`oselia_provision/constants.py` (that's how the resolver finds them).

## Refreshing / bumping the pin

When the MicroPython pin changes, update `EXPECTED_MPY_VERSION`, `MPY_UF2_NAME`,
`MPY_UF2_URL` in `oselia_provision/constants.py` **and** `firmware/docs/flashing.md`, then
re-download here:

```bash
cd provisioning/uf2
curl -L -o RPI_PICO-<date>-v<ver>.uf2 https://micropython.org/resources/firmware/RPI_PICO-<date>-v<ver>.uf2
curl -L -o flash_nuke.uf2            https://datasheets.raspberrypi.com/soft/flash_nuke.uf2
# remove the superseded MicroPython .uf2 so only the pinned one remains
```
