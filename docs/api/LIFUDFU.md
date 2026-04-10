# `LIFUDFU` API

Firmware update utilities and DFU transport implementations.

Module contents
- `stm32_crc32(data: bytes, init: int=0xFFFFFFFF) -> int` — STM32-compatible CRC32.
- `parse_signed_package(pkg: bytes) -> dict` — validate signed firmware package and return `fw_address`, `meta_address`, `fw`, `meta`.

USB DFU (module 0)
- `STM32USBDFU(vid=..., pid=..., transfer_size=1024, timeout_ms=4000, libusb_dll=None, device_profile=None)` — minimal PyUSB-based DFU client for USB DFU programming.
  - `open()`, `close()`, `get_version()`, `write_memory(address,data,...)`, `manifest()` and context-manager support.
  - Requires `pyusb` and libusb backend; helper `_find_bundled_libusb_dll()` locates bundled DLL on Windows if present.

I2C DFU via master (modules 1+)
- `STM32I2CDFUviaMaster(uart: LIFUUart, i2c_addr=0x72)` — performs I2C DFU operations routed through the USB-master using `OW_I2C_PASSTHRU` packets.
  - `get_status()`, `erase_page()`, `mass_erase()`, `write_block()`, `write_memory()`, `manifest()`, `reset()`, `get_version()`.

High-level manager
- `LIFUDFUManager(uart: LIFUUart)` — orchestrates module updates for USB (module 0) and I2C (modules 1+).
  - `get_bootloader_version_usb(...)` / `get_bootloader_version_i2c(...)`
  - `program_usb(package_file, ...)` — parse signed package and program module 0 via `STM32USBDFU`.
  - `program_i2c(package_file, ...)` — program slave modules via `STM32I2CDFUviaMaster`.
  - `update_module(module, package_file, enter_dfu_fn, ...)` — high-level flow: trigger DFU entry, wait, detect bootloader, program, and manifest.

Notes and behaviour
- The package format is checked using `parse_signed_package` which validates header CRC and payload CRC.
- USB DFU client implements DfuSe DNLOAD/UPLOAD primitives and performs page erases before writes; `write_memory` enforces alignment and pads blocks when required.
- I2C DFU implements a passthrough protocol; block sizes are limited by `I2C_DFU_MAX_XFER_SIZE` (512 bytes).

Usage example (from `TxDevice.update_firmware`):

```py
from openlifu_sdk.io.LIFUDFU import LIFUDFUManager
mgr = LIFUDFUManager(uart=txdevice.uart)
mgr.update_module(module=1, package_file="fw.signed.bin", enter_dfu_fn=txdevice.enter_dfu)
```

See also: `docs/api/LIFUTXDevice.md` which delegates firmware work to `LIFUDFUManager`.