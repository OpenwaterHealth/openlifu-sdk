# `LIFUHVController` API

`HVController` manages console/HV interactions: powering, voltage control, temperature and telemetry.

Constructor
- `HVController(uart: LIFUUart = None)` — accepts `LIFUUart` for console communication.

Connectivity and basic
- `is_connected() -> bool` — True when console UART connected.
- `close()` — close UART transport.
- `ping() -> bool` — ping console.
- `get_version() -> str` — firmware version string.
- `get_hardware_id() -> str` — formatted HWID.

Power and HV control
- `turn_12v_on()` / `turn_12v_off()` — control 12V rail.
- `get_12v_status() -> bool` — query 12V state.
- `turn_hv_on()` / `turn_hv_off()` — turn HV supply on/off.
- `get_hv_status() -> bool` — query HV-on state.
- `hv_enable(enable: bool) -> bool` — enable/disable HV output.

Voltage and DACs
- `set_voltage(voltage: float) -> bool` — set HV output (5–100 V validated).
- `get_voltage() -> float` — read current HV setting.
- `set_dacs(hvp, hvm, hrp, hrm) -> bool` — low-level DAC setters (12-bit values).
- `set_raw_dac(dac_id:int, dac_value:int) -> int` — write raw DAC value.
- `get_vmon_values() -> list[dict]` — read VMON channels and converted voltages.

Cooling and LED
- `set_fan_speed(fan_id=0, fan_speed=50) -> int` — set fan 0 or 1 speed (0–100).
- `get_fan_speed(fan_id=0) -> int` — read fan speed.
- `set_rgb_led(rgb_state:int) -> bool` / `get_rgb_led() -> int` — legacy enum
  RGB state: 0 = OFF, 1 = RED, 2 = GREEN, 3 = BLUE. `get_rgb_led` reflects the
  last basic state only; the `rgb_*` effect methods below do not change it.

RGB color and effects (24-bit DMA color engine on the console; effects run
entirely on the device — the host just selects them. `set_rgb_led` remains
valid and cancels any running effect.)
- `set_rgb_color(r, g, b) -> bool` — static 24-bit color (each 0–255);
  cancels any running effect.
- `rgb_fade_to(r, g, b, duration_ms=1000) -> bool` — fade from the current
  color to the target, then hold; fading to (0,0,0) is a smooth off.
- `rgb_breathe(r, g, b, period_ms=3000) -> bool` — brightness ramps
  0 → full → 0 every period, repeating.
- `rgb_rainbow(period_ms=4000) -> bool` — full hue-wheel sweep per period,
  repeating.
- `rgb_flash(r, g, b, period_ms=1000) -> bool` — 50% on/off blink; the period
  is the full cycle (1000 = 0.5 s on, 0.5 s off).
- `rgb_color_cycle(colors, dwell_ms=1000) -> bool` — step through 1–8
  `(r, g, b)` tuples, each shown for `dwell_ms`, repeating.
- `rgb_effect_stop() -> bool` — cancel any effect; the LED holds its current
  color (follow with `set_rgb_color` for direct control).
- All raise `ValueError` locally for out-of-range values (channels 0–255,
  periods 0–65535 ms) before anything is sent to the device.

Telemetry
- `get_temperature1()` / `get_temperature2()` — read temperature sensors.

DFU entry and reset
- `soft_reset() -> bool` — soft reset the console.
- `enter_dfu(module=0, reserved=0x00) -> bool` — reboot into DFU. With the
  default `reserved` the device enters whichever DFU its installed bootloader
  provides (STM32 ROM for no-bootloader units, the legacy or secure
  bootloader's DFU otherwise).
- `enter_stm32_rom_dfu(module=0) -> bool` — force the STM32 ROM
  (system-memory) DFU loader regardless of the installed bootloader, via the
  hidden `OW_CMD_DFU reserved=0x77` switch (`OWComponent.DFU_FORCE_STM32_ROM`).
  This is the entry point for bootloader migration/replacement — the ROM
  loader can write the whole flash. Only effective on unlocked (beta) units;
  inert after RDP/FDA lockdown.

Notes
- Methods often raise `ValueError` if the console is not connected. Many methods have `demo_mode` behavior when `LIFUUart.demo_mode` is set.

See also: `docs/api/LIFUInterface.md` for high-level usage and `docs/api/LIFUDFU.md` for firmware programming details.