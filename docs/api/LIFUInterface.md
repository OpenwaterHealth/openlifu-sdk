# `LIFUInterface` — high-level SDK entrypoint

`LIFUInterface` is the package-level entrypoint that composes the TX and HV subsystems and exposes user-facing operations.

Key constructor arguments:

- `vid`, `tx_pid`, `con_pid` — USB vendor/product IDs used to open the USB endpoints.
- `baudrate`, `timeout` — serial transport options.
- `TX_test_mode`, `HV_test_mode` — demo/test modes that skip real hardware operations.
- `run_async` — enable background USB monitoring and async operations.
- `ext_power_supply` — if True, the HV controller is not initialized and external power is assumed.

Important methods:

- `is_device_connected() -> (tx_connected, hv_connected)` — quick connectivity check.
- `set_solution(solution, profile_index=1, profile_increment=True, trigger_mode="sequence")` — validate and load a solution to the TX device; sets HV voltage when present.
- `start_sonication() -> bool` — enables HV (if present) and triggers the TX device.
- `stop_sonication() -> bool` — stops triggers and turns HV off.
- `check_solution(solution)` — runs safety checks (duty cycle and duration) and raises `ValueError` on violation.
- `get_sequence_duty_cycle(solution)` and `get_sequence_duration(solution)` — utility helpers used by safety checks.
- `get_max_voltage(solution)` / `get_max_voltage_table()` — helpers to query allowed voltage limits for a solution.

Context manager support: `with LIFUInterface() as iface:` ensures `close()` is called.

Notes:

- Safety parameters (allowed duty cycles and voltage charts) are defined in `LIFUInterface.py` and can be influenced by `voltage_table_selection` and `sequence_time_selection` constructor args.

See also:

- `LIFUTXDevice` (TX programming, profiles, triggers): [LIFUTXDevice API](LIFUTXDevice.md)
- `LIFUHVController` (console/HV control, power, telemetry): [LIFUHVController API](LIFUHVController.md)
- `LIFUDFU` (firmware update helpers): [LIFUDFU API](LIFUDFU.md)