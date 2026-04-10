## API Reference

This page enumerates the main public modules and classes provided by the SDK and links into the expanded API pages under `docs/api/`.

Core package (entry points):

- `openlifu_sdk.LIFUInterface` — high-level interface that composes TX and HV subsystems and exposes user-facing operations. See [LIFUInterface API](api/LIFUInterface.md).
- `openlifu_sdk.io.LIFUTXDevice` — TX module controller for programming TX7332 chips, triggers and profiles. See [LIFUTXDevice API](api/LIFUTXDevice.md).

I/O modules (in `openlifu_sdk.io`):

- `LIFUUart` — low-level USB/serial transport wrapper and monitor.
- `LIFUHVController` — HV/console interface (power, voltage, telemetry). See [LIFUHVController API](api/LIFUHVController.md).
- `LIFUDFU` — firmware DFU helpers and managers (USB DFU, I2C DFU). See [LIFUDFU API](api/LIFUDFU.md).
- `LIFUConfig`, `LIFUUserConfig` — configuration helpers for device registers and user settings.

Utility modules (in `openlifu_sdk.util`):

- `hwid` — hardware ID helpers.
- `units` — helpers for handling units.
- `annotations` — typing and helper annotations for the package.

For more details read the per-module pages inside this `docs/api/` folder and consult the top-level `examples/` directory for runnable examples and scripts.