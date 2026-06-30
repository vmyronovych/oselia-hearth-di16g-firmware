"""Onboard WS2812 (NeoPixel) status indicator.

The RP2040-ETH has ONE addressable RGB LED (WS2812 on GP25, confirmed against
Waveshare's own WS2812B demo). The chip is clocked out via the RP2040 PIO -- the
board's MicroPython 1.15 build ships WITHOUT the `neopixel` module and without
`machine.bitstream`, but `rp2` (PIO) is always present, so PIO is the portable path.
A single pixel can't show every subsystem at once, so this module encodes status
as **colour + blink pattern**, choosing the highest-priority unhealthy subsystem
(root-cause first). When everything is healthy the LED is solid green. Each
published gesture briefly flashes the LED as activity feedback.

The colour/blink decision logic is PURE (no hardware imports) so it is unit-tested
on a host. The WS2812 write is the only hardware-touching part and is imported
lazily, so this module also imports cleanly under CPython.

Subsystem keys + default priority (first = shown first when unhealthy):
    "boot"      -> initialising (solid blue)
    "mqtt"      -> broker/link down (orange, medium blink)
    "mcp"       -> MCP23017 not responding (yellow, fast blink)
All healthy -> solid green. Activity -> brief white flash (overrides).
"""

# Base colours (full-scale 0..255; brightness is scaled at write time).
BLACK = (0, 0, 0)
ORANGE = (255, 70, 0)
YELLOW = (255, 200, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
WHITE = (255, 255, 255)

# Priority order: earlier entries win when several are unhealthy.
PRIORITY = ("boot", "mqtt", "mcp")

# Per-subsystem fault colour + blink period (ms). duty = 0.5.
_FAULT = {
    "boot": (BLUE, 0),         # period 0 => solid
    "mqtt": (ORANGE, 600),
    "mcp": (YELLOW, 300),
}

FLASH_MS = 90                  # activity flash duration


def _blink_on(now_ms, period_ms, duty=0.5):
    if period_ms <= 0:
        return True
    return (now_ms % period_ms) < int(period_ms * duty)


def select_fault(states):
    """Return the key of the highest-priority unhealthy subsystem, or None.

    `states` maps subsystem key -> healthy bool. "boot" is treated as a fault
    (still initialising) when present and truthy-as-active; see compute_color.
    """
    for key in PRIORITY:
        if key == "boot":
            if states.get("boot", False):     # boot True => actively booting
                return "boot"
            continue
        if key in states and not states[key]:
            return key
    return None


def compute_color(states, now_ms, last_activity_ms=None):
    """Pure: decide the (r,g,b) to display this instant.

    * activity flash (white) overrides everything for FLASH_MS.
    * else show the highest-priority fault with its blink pattern.
    * else solid green (all healthy).
    """
    if last_activity_ms is not None and 0 <= (now_ms - last_activity_ms) < FLASH_MS:
        return WHITE

    fault = select_fault(states)
    if fault is None:
        return GREEN
    color, period = _FAULT[fault]
    return color if _blink_on(now_ms, period) else BLACK


def scale(color, brightness):
    """Scale a base colour by brightness 0.0..1.0 -> WS2812 byte tuple."""
    b = 0.0 if brightness < 0 else (1.0 if brightness > 1 else brightness)
    return (int(color[0] * b), int(color[1] * b), int(color[2] * b))


class _Ws2812Pio:
    """Single-pixel WS2812 driver over the RP2040 PIO (no `neopixel` dependency).

    Mirrors Waveshare's RP2040-ETH WS2812B demo: a state machine clocks out the
    top 24 bits of each FIFO word, MSB-first, at 8 MHz. `write` packs the logical
    (r, g, b) into the LED's wire order (GRB for standard WS2812/WS2812B).
    """

    def __init__(self, pin_num):
        import rp2
        from machine import Pin

        @rp2.asm_pio(sideset_init=rp2.PIO.OUT_LOW,
                     out_shiftdir=rp2.PIO.SHIFT_LEFT,
                     autopull=True, pull_thresh=24)
        def _ws2812():
            T1, T2, T3 = 2, 5, 3
            wrap_target()
            label("bitloop")
            out(x, 1)             .side(0)[T3 - 1]    # noqa: F821 (PIO asm)
            jmp(not_x, "do_zero") .side(1)[T1 - 1]    # noqa: F821
            jmp("bitloop")        .side(1)[T2 - 1]    # noqa: F821
            label("do_zero")
            nop()                 .side(0)[T2 - 1]    # noqa: F821
            wrap()

        self._sm = rp2.StateMachine(0, _ws2812, freq=8_000_000,
                                    sideset_base=Pin(pin_num))
        self._sm.active(1)

    def write(self, rgb, order):
        r, g, b = rgb
        # The byte in bits 24..31 is shifted out first; WS2812 wants green first.
        if order == "RGB":
            word = (r << 24) | (g << 16) | (b << 8)
        else:                                     # "GRB" (standard WS2812/WS2812B)
            word = (g << 24) | (r << 16) | (b << 8)
        self._sm.put(word)


class StatusLed:
    """Non-blocking status LED. Call update(now_ms) every main-loop iteration."""

    def __init__(self, pin_num, brightness=0.2, order="GRB"):
        # `order` is the LED's WIRE order (GRB for the RP2040-ETH WS2812); the PIO
        # backend handles the byte packing. Flip to "RGB" only if red/green look
        # swapped on a different LED.
        self.brightness = brightness
        self.order = order
        self._states = {"boot": True}     # start in boot
        self._last_activity = None
        self._last_written = None
        # Lazy hardware init so the module still imports under CPython (host tests).
        try:
            self._backend = _Ws2812Pio(pin_num)
        except Exception:
            self._backend = None          # host / no hardware: pure logic still runs

    def set_state(self, key, healthy):
        self._states[key] = healthy

    def boot_done(self):
        self._states["boot"] = False

    def notify_activity(self, now_ms):
        self._last_activity = now_ms

    def update(self, now_ms):
        color = compute_color(self._states, now_ms, self._last_activity)
        rgb = scale(color, self.brightness)
        if rgb != self._last_written:
            self._last_written = rgb
            if self._backend is not None:
                self._backend.write(rgb, self.order)
