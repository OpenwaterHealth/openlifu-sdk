## API Reference

This page enumerates the main public modules and classes provided by the SDK and links into the expanded API pages under `docs/api/`.

Core package (entry points):

- `openlifu_sdk.LIFUInterface` — high-level interface that composes TX and HV subsystems and exposes user-facing operations. See [LIFUInterface API](api/LIFUInterface.md).
- `openlifu_sdk.io.LIFUTXDevice` — TX module controller for programming TX7332 chips, triggers and profiles. See [LIFUTXDevice API](api/LIFUTXDevice.md).

I/O modules (in `openlifu_sdk.io`):

- `LIFUUart` — low-level USB/serial transport wrapper and monitor.
- `LIFUHVController` — HV/console interface (power, voltage, telemetry, RGB LED and effects). See [LIFUHVController API](api/LIFUHVController.md).
- `LIFUFirmwareUpdate` — one-call, auto-detecting console firmware update covering all three unit states (no-bootloader / legacy / secure); keyless, uses bundled images. See [LIFUFirmwareUpdate API](api/LIFUFirmwareUpdate.md).
- `LIFUDFU` — lower-level firmware DFU helpers and managers (USB/I2C DFU, per-scenario console update and bootloader migration methods). See [LIFUDFU API](api/LIFUDFU.md).
- `LIFUCrypto` — SBSFU firmware image signing, validation and inspection; owns the FwVersion encoding. See [LIFUCrypto API](api/LIFUCrypto.md).
- `LIFUConfig`, `LIFUUserConfig` — configuration helpers for device registers and user settings.

Utility modules (in `openlifu_sdk.util`):

- `hwid` — hardware ID helpers.
- `units` — helpers for handling units.
- `annotations` — typing and helper annotations for the package.

For more details read the per-module pages inside this `docs/api/` folder and consult the top-level `examples/` directory for runnable examples and scripts.