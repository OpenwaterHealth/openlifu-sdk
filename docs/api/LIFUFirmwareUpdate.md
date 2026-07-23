# `LIFUFirmwareUpdate` API

One high-level entry point for **console** firmware updates. It auto-detects
the unit's state and runs the correct path — you don't choose the scenario:

| Unit state | App version | Path taken |
|---|---|---|
| No bootloader | < 1.2.0 | Migrate to the secure bootloader via STM32 ROM DFU (combined image) |
| Legacy bootloader | 1.2.0–1.2.5 | Migrate via the RAM-resident self-updater |
| Secure bootloader | ≥ 1.2.6 | Normal signed-app update |

Defaults to the firmware images **bundled with the SDK** and needs **no
signing keys**: the legacy updater is authenticated by an HMAC trust tag
computed on the fly, and the secure bootloader verifies the app at boot.
An optional `keys_dir` only adds an ECDSA app-signature pre-check.

Bundled image helpers
- `bundled_production_image() -> Path` — combined bootloader+app image
  (`firmware/openlifu-console-fw-production.bin`), source for the
  no-bootloader migration.
- `bundled_signed_app() -> Path` — signed console app
  (`firmware/openlifu-console-fw-signed.bin`), source for the legacy migration
  and secure app update.
- (The legacy RAM updater is `LIFUDFU.bundled_updater_path()`.)

Class
- `LIFUFirmwareUpdate(hv=None, keys_dir=None, libusb_dll=None, vid=0x0483, pid=0xDF11)`
  - `hv` — a connected `HVController` (e.g. `interface.hvcontroller`), used to
    read the app version and trigger DFU entry. May be omitted only when the
    unit is already in a DFU mode.
  - `keys_dir` — optional; ECDSA-validate the signed app before flashing.
- `detect_cohort() -> (cohort, source)` — `cohort` is `"no-bootloader"` /
  `"legacy-bl"` / `"secure-bl"`; `source` is `"app"` (from the running app
  version) or `"dfu"` (the unit was already in a bootloader DFU).
- `update(*, production_image=None, signed_app=None, updater_bin=None, force=False, progress_callback=None) -> UpdateResult`
  - Detects the state and runs the right path. All image args default to the
    bundled files. `force` (secure path only) flashes even if not newer (the
    bootloader's anti-rollback floor still applies at boot).

`UpdateResult` fields: `cohort`, `action` (`"migrate-rom"` / `"migrate-legacy"`
/ `"app-update"`), `summary`, `reboot_required`.

Behaviour notes
- If a running app is present, its version names the cohort and the updater
  triggers DFU entry itself. If the unit is **already in DFU**, the DFU product
  string is used and no extra DFU entry is triggered.
- Migrations are for **unlocked (beta) units only** — after RDP/FDA lockdown
  the force-ROM-DFU switch is inert and the bootloader is not erasable.
- The no-bootloader/ROM path requires **STM32CubeProgrammer** (the ROM-loader
  write is delegated to it). See `LIFUDFU.find_stm32_programmer_cli`.
- **Version encoding**: bundled images use the SDK's bitfield `FwVersion`
  (1.2.6 = 2118). A unit whose anti-rollback floor was latched under the old
  decimal scheme (1.2.6 = 10206) rejects bitfield images until the floor is
  reset — which the full-erase migration does; see `LIFUCrypto`.

Usage

```py
from openlifu_sdk.io.LIFUInterface import LIFUInterface
from openlifu_sdk.io.LIFUFirmwareUpdate import LIFUFirmwareUpdate

interface = LIFUInterface(TX_test_mode=False)
fw = LIFUFirmwareUpdate(hv=interface.hvcontroller)   # no keys needed
result = fw.update()                                  # auto-detect + update
print(result.summary)
if result.reboot_required:
    print("Power-cycle the console.")
```

Runnable script: `examples/update_console_firmware.py`.

Rebuilding the bundled `updater.bin` (for an updated secure bootloader) is
documented in `console-legacy-updater/README.md` — the updater embeds a
specific bootloader blob and is keyless.

## Transmitter (`LIFUTransmitterFirmwareUpdate`)

Same auto-detect pattern for the transmitter. Module 0 (the USB master):

| Unit state | App version | Path taken |
|---|---|---|
| No secure bootloader | ≤ 2.0.7 | Migrate via STM32 ROM DFU (combined production image, `OW_CMD_DFU` force switch) |
| Secure bootloader | ≥ 2.0.8 | Normal signed-app update over USB DFU |

**Slave modules** (`--module N`, N ≥ 1) are updated over I2C through the
master's passthrough with `update_slave(module, ...)`:

- **secure** (app ≥ 2.0.8): the SFU1 signed app is streamed to the slave's
  secure-bootloader I2C DFU (enumerated address 0x20+).
- **legacy** (app ≤ 2.0.7): **one-shot migration** of bootloader + app.
  1. The small RAM-resident **DFU stub**
     (`firmware/openlifu-transmitter-dfu-stub.bin`, built from
     `transmitter-dfu-stub/`) is written through the legacy I2C DFU with an
     HMAC-trust-tagged WFM1 metadata block (computed on the fly — no keys).
  2. The legacy bootloader validates and boots the stub, which copies itself
     to SRAM and serves the same I2C DFU protocol at the default address
     0x72 — with the writable window opened to the whole 256 KB flash.
  3. The SDK full-chip-erases (this also resets any stale anti-rollback
     floor) and streams the whole production image
     (`firmware/openlifu-transmitter-fw-production.bin`) to 0x08000000.
  4. On reset the freshly written secure bootloader verifies the signed app
     in its slot and launches it.

  The stub embeds no bootloader blob, so a new bootloader release only needs
  a new production image — the stub is unchanged. **Keep the slave powered**
  from the full-chip erase until the write completes; the stub keeps serving
  DFU from RAM after any failed write (retry is possible), but a power loss
  in that window leaves the module SWD-recoverable only.

CLI:

```
python -m openlifu_sdk.io.LIFUFirmwareUpdate --device transmitter --module 1
```

`--legacy` forces the one-shot migration path; omit it to auto-detect from
the slave's app version. `--production` overrides the combined image.

See also: `docs/api/LIFUDFU.md` (the underlying per-scenario methods),
`docs/api/LIFUCrypto.md` (image signing/validation),
`docs/api/LIFUHVController.md` (DFU entry).
