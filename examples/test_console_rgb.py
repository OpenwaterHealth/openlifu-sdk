from __future__ import annotations

import sys
import time

from openlifu_sdk.io.LIFUInterface import LIFUInterface

# Exercises the console RGB driver end-to-end:
#   1. Legacy enum API (set_rgb_led / get_rgb_led) - backward compatibility
#   2. Static 24-bit colors (set_rgb_color)
#   3. Effects: fade, breathe, rainbow, flash, color cycle, stop
#
# Requires console firmware with OW_POWER_SET_RGB_FX support (>= 1.0.2).
#
# set PYTHONPATH=%cd%\src;%PYTHONPATH%
# python examples/test_console_rgb.py

print("Starting Console RGB Test Script...")

interface = LIFUInterface()
tx_connected, hv_connected = interface.is_device_connected()

if not hv_connected:
    print("❌ Console (HV controller) not connected.")
    sys.exit(1)

print("✅ Console connected.")
hv = interface.hvcontroller
hv.ping()

# ---------------------------------------------------------------------------
# 1. Backward compatibility: legacy enum API
# ---------------------------------------------------------------------------
print("\n--- Legacy enum API (set_rgb_led / get_rgb_led) ---")
for state, name in ((1, "RED"), (2, "GREEN"), (3, "BLUE"), (0, "OFF")):
    print(f"set_rgb_led({state})  -> {name}")
    hv.set_rgb_led(state)
    readback = hv.get_rgb_led()
    assert readback == state, f"get_rgb_led returned {readback}, expected {state}"
    time.sleep(1.0)
print("Legacy API OK (set/get round-trip verified)")

# ---------------------------------------------------------------------------
# 2. Static 24-bit colors (beyond the legacy 4 states)
# ---------------------------------------------------------------------------
print("\n--- Static 24-bit colors (set_rgb_color) ---")
static_colors = [
    ((255, 96, 0), "orange"),
    ((255, 0, 255), "magenta"),
    ((0, 255, 255), "cyan"),
    ((255, 255, 255), "white"),
    ((32, 32, 32), "dim white (gamma low end)"),
]
for (r, g, b), name in static_colors:
    print(f"set_rgb_color({r}, {g}, {b})  -> {name}")
    hv.set_rgb_color(r, g, b)
    time.sleep(1.2)

# ---------------------------------------------------------------------------
# 3. Effects - each runs on the device; the host just watches
# ---------------------------------------------------------------------------
print("\n--- Fade ---")
print("rgb_fade_to(255, 0, 0, 1500ms)  -> fade to red")
hv.rgb_fade_to(255, 0, 0, 1500)
time.sleep(2.0)
print("rgb_fade_to(0, 0, 255, 1500ms)  -> cross-fade to blue")
hv.rgb_fade_to(0, 0, 255, 1500)
time.sleep(2.0)
print("rgb_fade_to(0, 0, 0, 1000ms)    -> smooth off")
hv.rgb_fade_to(0, 0, 0, 1000)
time.sleep(1.5)

print("\n--- Breathe ---")
print("rgb_breathe(0, 160, 255, 3000ms) for 3 breaths")
hv.rgb_breathe(0, 160, 255, 3000)
time.sleep(9.0)

print("\n--- Rainbow ---")
print("rgb_rainbow(4000ms) for 2 revolutions")
hv.rgb_rainbow(4000)
time.sleep(8.0)

print("\n--- Flash ---")
print("rgb_flash(255, 0, 0, 1000ms) for 3 cycles")
hv.rgb_flash(255, 0, 0, 1000)
time.sleep(3.0)

print("\n--- Color cycle ---")
print("rgb_color_cycle(R, G, B, yellow, magenta; 700ms dwell)")
hv.rgb_color_cycle(
    [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255)],
    dwell_ms=700,
)
time.sleep(7.0)

print("\n--- Stop mid-effect ---")
print("rgb_effect_stop()  -> LED freezes on whatever the cycle was showing")
hv.rgb_effect_stop()
time.sleep(2.0)

# ---------------------------------------------------------------------------
# 4. Error handling: out-of-range requests must be rejected locally
# ---------------------------------------------------------------------------
print("\n--- Parameter validation ---")
for bad_call, kwargs in (
    ("set_rgb_led(7)", lambda: hv.set_rgb_led(7)),
    ("set_rgb_color(300, 0, 0)", lambda: hv.set_rgb_color(300, 0, 0)),
    ("rgb_flash period 70000", lambda: hv.rgb_flash(255, 0, 0, 70000)),
    ("rgb_color_cycle 9 colors", lambda: hv.rgb_color_cycle([(1, 2, 3)] * 9)),
):
    try:
        kwargs()
        print(f"❌ {bad_call} was NOT rejected")
        sys.exit(1)
    except ValueError as e:
        print(f"OK: {bad_call} rejected ({e})")

# ---------------------------------------------------------------------------
# 5. Legacy API still works after effects (cancels any residue)
# ---------------------------------------------------------------------------
print("\n--- Restore normal state ---")
print("set_rgb_led(2)  -> GREEN (idle indication)")
hv.set_rgb_led(2)
assert hv.get_rgb_led() == 2

print("\n✅ Console RGB test complete - LED should be solid green.")
