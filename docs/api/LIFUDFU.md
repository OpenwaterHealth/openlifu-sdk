# `LIFUDFU` API

Firmware update utilities and DFU transport implementations for both the
**transmitter** (PGK1 packages, USB/I2C DFU) and the **console** (SBSFU signed
images, bootloader migration).

Module contents
- `stm32_crc32(data: bytes, init: int=0xFFFFFFFF) -> int` — STM32-compatible CRC32.
- `parse_signed_package(pkg: bytes) -> dict` — validate a transmitter 'PGK1' firmware package and return `fw_address`, `meta_address`, `fw`, `meta`.
- `split_console_flash_image(image: bytes) -> (bootloader, signed_app)` — split a combined console full-flash image (bootloader @0x08000000 + 'SFU1' app @0x08010000).
- `build_legacy_metadata(app_bytes, ...) -> bytes` — build a legacy-console-bootloader metadata block (124 B, HMAC trust tag; no private key required).
- (Firmware is written only by the SDK's own drivers — `STM32USBDFU` / `STM32I2CDFUviaMaster` in this module and `openlifu_sdk.io.STM32DFU` for the ROM loader. No external programmer is invoked, and the SDK never spawns a subprocess.)

Console DFU environment detection
- All three console DFU environments enumerate as `0483:DF11`; the USB product string tells them apart:
  - `DFU_KIND_ROM` (`"stm32-rom"`) — STM32 ROM system loader (`STM32  BOOTLOADER`); full-flash access.
  - `DFU_KIND_LEGACY` (`"legacy-bl"`) — legacy bootloader (`LIFU BL DFU 0.0.x`); app region only.
  - `DFU_KIND_SECURE` (`"secure-bl"`) — secure SBSFU bootloader (`OW DFU 1.x.x`, or the pre-branding CubeMX string).
  - `DFU_KIND_NONE` (`"no-bootloader"`) / `DFU_KIND_UNKNOWN`.
- `infer_console_bootloader_from_app_version(app_version: str) -> str` — fleet rule from the *running* app version: `>=1.2.6` secure, `1.2.3–1.2.5` legacy, `1.1.0–1.2.2` no bootloader (plain DFU request lands in the ROM loader), `<1.1.0` `DFU_KIND_NO_DFU` — no DFU environment is reachable, so the SDK refuses to update it.

USB DFU client
- `STM32USBDFU(vid=0x0483, pid=0xDF11, transfer_size=1024, timeout_ms=4000, libusb_dll=None, device_profile=None)` — minimal PyUSB DfuSe client.
  - `open()`, `close()`, context-manager support; requires `pyusb` + a libusb backend (`_find_bundled_libusb_dll()` finds the bundled Windows DLL).
  - `get_version()` — bootloader version string via the virtual address `0xFFFFFF00`.
  - `write_memory(address, data, page_erase=True, progress_callback=None)` — erase-then-write; reliable against the **custom** bootloaders' DFU. **Not reliable against the STM32 ROM loader** — the manager methods route ROM-loader writes through `openlifu_sdk.io.STM32DFU` (individually addressed blocks, DfuSe poll windows, read-back verify).
  - `read_memory(address, length) -> bytes` — DfuSe UPLOAD read (subject to the bootloader's read window).
  - `erase_pages(start, end, page_size=2048)` — explicit per-page erase.
  - `manifest()` — zero-length DNLOAD; device leaves DFU / launches firmware.
  - `trigger_reset(reset_vaddr=0xFFFFFF08)` — reset via the custom bootloaders' virtual reset address.
  - Device profiles: `TRANSMITTER_PROFILE`, `CONSOLE_PROFILE` (transfer size, version read length, program alignment).

I2C DFU via master (transmitter modules 1+)
- `STM32I2CDFUviaMaster(uart: LIFUUart, i2c_addr=0x72)` — I2C DFU routed through the USB master via `OW_I2C_PASSTHRU`.
  - `get_status()`, `erase_page()`, `mass_erase()`, `write_block()`, `write_memory()`, `manifest()`, `reset()`, `get_version()`.

High-level manager
- `LIFUDFUManager(uart: LIFUUart | None = None)` — `uart` is only needed for the I2C passthrough paths; console/USB-only use may omit it.

Transmitter paths
- `get_bootloader_version_usb(...)` / `get_bootloader_version_i2c(...)`
- `program_usb(package_file, ...)` — program module 0 via `STM32USBDFU` (PGK1 package).
- `program_i2c(package_file, ...)` — program slave modules via `STM32I2CDFUviaMaster`.
- `update_module(module, package_file, enter_dfu_fn, ...)` — trigger DFU entry, wait, detect bootloader, program, manifest.
- `migrate_transmitter_legacy_usb(signed_app, updater_bin, enter_dfu_fn=None, ...) -> str` — **USB recovery for a master parked in the legacy bootloader's DFU** (dead app, so the ROM loader is unreachable): write the RAM-resident legacy updater to `0x08010000` + on-the-fly WFM1 trust-tag metadata to `0x0800F800`, read-back verify, reset so the updater swaps in the secure bootloader, then flash the signed app over the secure DFU. Returns the secure bootloader's version. Leaves the anti-rollback floor page untouched — follow with a `--force-production` pass. Keep the unit powered through the bootloader swap.
- `program_transmitter_slave_i2c(signed_image, i2c_addr, ...)` — stream an SFU1 signed app to a **secure-bootloader** slave's I2C DFU (validated before the erase).
- `program_transmitter_slave_legacy_i2c(image, i2c_addr, ...)` — write a trusted image (+ on-the-fly WFM1 trust-tag metadata) through a **legacy-bootloader** slave's I2C DFU; used to install the RAM-resident DFU stub.
- `wait_transmitter_slave_stub(i2c_addr=0x72, timeout_s=30)` — poll until the booted DFU stub answers `GETVERSION` (`dfu-stub-x.y.z`) at the default DFU address.
- `program_transmitter_slave_production_i2c(combined_image, i2c_addr=0x72, ...)` — **one-shot legacy migration write**: via the DFU stub, full-chip erase (also resets a stale anti-rollback floor) and stream the whole production image (bootloader + signed app) to `0x08000000`. Keep the slave powered from erase to completion.

Console paths (SBSFU signed images from `LIFUCrypto`)
- `detect_console_dfu_kind(...) -> (kind, version)` — identify the enumerated DFU environment from the USB product string (no DFU transaction).
- `get_console_bootloader_version(...) -> str` — wait for enumeration and read the secure bootloader's version.
- `get_console_installed_version(...) -> int | None` — FwVersion of the image installed in the active slot (via DFU read of the header), or None.
- `program_console(signed_image, keys_dir=None, force=False, ...)` — flash a signed app over the secure bootloader's DFU. Pre-erase checks: local image validation (`LIFUCrypto`) and an installed-version comparison that **refuses a downgrade before anything is erased** (`force=True` overrides; the bootloader's anti-rollback floor remains the final authority at boot).
- `update_console(signed_image, enter_dfu_fn=None, keys_dir=None, ...) -> str` — high-level app update: enter DFU, wait, `program_console`; returns the bootloader version.
- `migrate_console_full_image(combined_image, enter_stm32_rom_dfu_fn=None, keys_dir=None, ...)` — **recommended migration**: force STM32 ROM DFU, then mass-erase + write + read-back-verify the whole combined production image with the SDK's pure-Python DfuSe driver (`openlifu_sdk.io.STM32DFU` — no external tools). The mass erase wipes legacy metadata/config and any anti-rollback floor; the write ends with a DfuSe leave so the unit boots without a power-cycle.
- `migrate_console(bootloader_bin, signed_app, enter_stm32_rom_dfu_fn=None, ...)` / `migrate_console_rom_dfu(..., verify_rom=True)` — same migration from separate bootloader + signed-app files (combined internally, written the same way).
- `migrate_console_legacy(updater_bin, signed_app, enter_dfu_fn=None, ...)` — migration for **legacy-bootloader** units (app 1.2.0–1.2.5), which cannot reach ROM DFU: writes a RAM-resident self-updater + trust-tag metadata over the legacy DFU (read-back verified), resets, waits for the secure bootloader, then flashes the signed app. The bootloader self-replacement is the one irreversible step — keep the unit powered.
- `dwell_rom_dfu_check(enter_stm32_rom_dfu_fn=None, seconds=30, ...) -> bool` — safety pre-check: force ROM DFU and confirm the unit *stays* there (detects wrong-DFU landings and watchdog reset loops) without writing anything.

Notes and behaviour
- Transmitter packages are validated with `parse_signed_package` (header + payload CRC). Console images are validated with `openlifu_sdk.io.LIFUCrypto` (structure, SHA-256 tag, optional ECDSA).
- Console migrations are for **unlocked (beta) units only**: after RDP/FDA lockdown the force-ROM-DFU switch is inert and the bootloader region is not erasable.
- Constants: `CONSOLE_FLASH_BASE=0x08000000`, `CONSOLE_SLOT_BASE=0x08010000`, legacy layout `LEGACY_META_ADDRESS=0x08007800`, `LEGACY_APP_ADDRESS=0x08008000`.

Usage examples

```py
from openlifu_sdk.io.LIFUDFU import LIFUDFUManager

# Transmitter module update
mgr = LIFUDFUManager(uart=txdevice.uart)
mgr.update_module(module=1, package_file="fw.signed.bin", enter_dfu_fn=txdevice.enter_dfu)

# Console app update (secure bootloader)
mgr = LIFUDFUManager()
mgr.update_console("app_signed.bin", enter_dfu_fn=interface.hvcontroller.enter_dfu,
                   keys_dir="bl-keys/console")

# Console migration to the secure bootloader (no-bootloader unit)
mgr.migrate_console_full_image(
    "openlifu-console-fw-production.bin",
    enter_stm32_rom_dfu_fn=interface.hvcontroller.enter_stm32_rom_dfu,
    keys_dir="bl-keys/console")
```

Runnable scripts: `examples/test_console_dfu.py` (app update),
`examples/migrate_console_bootloader.py` (no-bootloader migration, `--dwell`
pre-check), `examples/migrate_console_legacy.py` (legacy migration).

See also: `docs/api/LIFUCrypto.md` (image signing/validation),
`docs/api/LIFUHVController.md` (DFU entry), `docs/api/LIFUTXDevice.md`.
