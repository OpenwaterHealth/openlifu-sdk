# `LIFUTXDevice` API

`TxDevice` is the TX-module controller that programs TX7332 chips, configures triggers, and performs firmware updates for transmitter modules.

Constructor
- `TxDevice(uart: LIFUUart, module_invert: bool | list[bool]=False)` — low-level transport `LIFUUart` instance is required.

Connectivity/status
- `is_connected() -> bool` — True when the UART transport is connected.
- `close()` — close the UART connection.
- `ping(module:int=0) -> bool` — simple ping command.
- `get_version(module:int=0) -> str` — firmware version string.
- `get_hardware_id(module:int=0) -> str` — formatted HWID.

Triggering and profiles
- `set_trigger(...)` / `set_trigger_json(data)` — configure trigger parameters (pulse interval/count, mode, profile index, etc.).
- `get_trigger_json()` / `get_trigger()` — read current trigger configuration.
- `start_trigger()` / `stop_trigger()` — start / stop software trigger.
- `async_mode(enable: bool|None) -> bool` — enable/disable or read async mode state.

TX registers and profiles
- `get_tx_module_count() -> int` — number of detected TX7332 chips.
- `enum_tx7332_devices(num_devices: int|None=None) -> int` — enumerate TX7332 devices and populate internal register structures.
- `set_module_invert(module_invert)` — set module invert mapping.
- `set_solution(pulse, delays, apodizations, sequence, trigger_mode, profile_index, profile_increment)` — high-level method that programs pulse/delay/apodization profiles, sets trigger and applies all registers.
- `apply_all_registers()` — write buffered registers to chips.

Register access
- `write_register(identifier, address, value) -> bool`
- `read_register(identifier, address) -> int`
- `write_block(identifier, start_address, reg_values) -> bool`
- `read_block(identifier, start_address, count) -> List[int]`
- `write_register_verify(...)`, `write_block_verify(...)` — write with verification.

Configuration and telemetry
- `read_config(module=0) -> LifuUserConfig` — read JSON user config from flash.
- `write_config(config, module=0) -> LifuUserConfig` — write `LifuUserConfig` to flash.
- `write_config_json(json_str, module=0)` — convenience wrapper for JSON strings.
- `get_temperature(module=0) -> float` — module temperature.
- `get_ambient_temperature(module=0) -> float` — ambient temperature.

DFU / firmware update
- `get_module_count() -> int` — number of logical LIFU modules reported by firmware.
- `update_firmware(module, package_file, ...) -> bool` — high-level helper that delegates to `openlifu_sdk.io.LIFUDFU.LIFUDFUManager` to program a signed package. See `LIFUDFU` docs for details.

Helpers and utilities
- `get_delay_location(channel, profile=1)` — compute register address / LSB for delay element.
- `get_pattern_location(period, profile=1)` — compute pattern location.
- `calc_pulse_pattern(frequency, duty_cycle)` — produce pattern object (levels, lengths, t, y) for given frequency and duty cycle.

Notes
- Many methods raise `ValueError` when the UART is not connected; call `is_connected()` first when appropriate.
- Several functions support `demo_mode` on the underlying `LIFUUart` for testing without hardware.

See also: `docs/api/LIFUInterface.md` and `docs/api/LIFUDFU.md`.