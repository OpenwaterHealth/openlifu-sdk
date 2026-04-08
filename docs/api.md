# API Reference

This page enumerates the main public modules and classes provided by the SDK.

Core package:

- `openlifu_sdk.LIFUInterface` — high-level interface that coordinates TX and HV modules and provides methods to load solutions, check safety limits, and start/stop sonication. See [docs/api/LIFUInterface.md](api/LIFUInterface.md).

I/O modules (in `openlifu_sdk.io`):

- `LIFUTXDevice` — TX module device control (profiles, triggers, apodizations).
- `LIFUUart` — low-level USB/serial transport wrapper and monitor.
- `LIFUHVController` — HV controller interface (voltage control, turn on/off).
- `LIFUDFU` — firmware DFU helper routines.
- `LIFUConfig`, `LIFUUserConfig` — configuration helpers for device registers and user settings.

Utility modules (in `openlifu_sdk.util`):

- `hwid` — hardware ID helpers.
- `units` — helpers for handling units.
- `annotations` — typing and helper annotations for the package.

For more details read the per-module pages inside `docs/api/` and consult `examples/` for usage patterns.