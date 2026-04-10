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
- `set_rgb_led(rgb_state:int) -> int` / `get_rgb_led() -> int` — control RGB state.

Telemetry
- `get_temperature1()` / `get_temperature2()` — read temperature sensors.

DFU entry and reset
- `soft_reset() -> bool` — soft reset the console.
- `enter_dfu() -> bool` — request DFU bootloader mode on console.

Notes
- Methods often raise `ValueError` if the console is not connected. Many methods have `demo_mode` behavior when `LIFUUart.demo_mode` is set.

See also: `docs/api/LIFUInterface.md` for high-level usage and `docs/api/LIFUDFU.md` for firmware programming details.