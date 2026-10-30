# `LIFUTXDevice` API

`TxDevice` is the TX-module controller that programs TX7332 chips, configures triggers, and performs firmware updates for transmitter modules.

> **FDA / RUO split.** `TxDevice` is the FDA component created by
> `DeviceInterface`: it exposes only identity, temperature, trigger
> read/start/stop and stored-preset (`get_preset` / `load_preset` /
> `get_loaded_preset_state` / `is_preset_locked`) methods,
> and refuses any other send before it reaches the wire (below). Every other
> method listed below (trigger/profile programming, register access,
> `async_mode`, config writes, DFU, LED/echo/reset) lives on the RUO subclass
> `LIFUTxDevice`, which only `LIFUInterface` creates. So do the methods that
> list, store and delete the presets of a DEV image (`get_preset_slot` /
> `get_preset_capacity` / `write_preset` / `delete_preset`; a bare-metal
> image only), and `lock_presets`.
>
> **What the host-side check is.** The FDA classes send only the
> (packet type, opcode) pairs their own methods use, and the user config
> only as a read (`reserved == 0`); anything else, including through the
> public `send` / `send_checked`, raises `ValueError` before it reaches the
> wire. That is a guard against host mistakes, not the safety boundary: an
> FDA_MODE firmware image refuses register writes, host-set voltage and
> user-config writes itself (`OW_FDA_REFUSED` / `OW_HV_FDA_REFUSED`).

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

Presets and the preset lock (transmitter-fw `presets.h`)
- `get_preset(index)` / `load_preset(index, regs_crc, duration_index, module, settings_crc=...)` / `unload_preset(module)` — describe, load (CRC-checked, run length chosen by `duration_index`) and unload a preset.
- `get_loaded_preset_state(module=None) -> dict` — what the firmware has loaded (`OW_PRESET_LOADED`): `loaded`, `index`, `duration_index`, `train_count`, `settings_crc`, `regs_crc`, `missing_modules`. The master asks every module; `loaded` is False unless all of them hold the same load, and `missing_modules` names the ones that don't (for example after a raw write on a DEV image unloaded one). With `module`, that module's own state.
- `get_loaded_preset() -> (index, duration_index)` — the same, or `(None, None)`; the simulator has the same method.
- `is_preset_locked() -> bool` — the preset lock (`OW_PRESET_LOCK`, read). Always True on an FDA image.
- `lock_presets() -> bool` (`LIFUTxDevice` only) — lock a DEV image until it is reset. Locked, it refuses what an FDA image refuses: raw register, profile and trigger writes (`OW_INVALID_PACKET`); user config writes, DFU, raw I2C and storing or deleting presets (`OW_FDA_REFUSED`). `LIFUInterface(preset_lock=True)` calls it when the transmitter connects.
- Unlocked, a DEV image takes raw writes as a research image does, but a raw write leaves nothing loaded on the module it runs on, and is refused (`OW_PRESET_BUSY`) while a loaded preset runs.

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