# `LIFUFirmwareUpdate` API

One high-level entry point for **console** firmware updates. It auto-detects
the unit's state and runs the correct path — you don't choose the scenario:

| Unit state | App version | DFU entered | Image used |
|---|---|---|---|
| **No DFU support** | **< 1.1.0** | — the request never reaches a DFU | **Refused** (BOOT0 / SWD only) |
| No bootloader | 1.1.0 – 1.2.2 | STM32 ROM DFU (plain `OW_CMD_DFU`) | `openlifu-console-fw-production.bin` |
| **Legacy bootloader** | **1.2.3 – 1.2.5** | — | **Refused** (BOOT0 / SWD; or `--force-production` on a beta unit whose app runs — STM32 ROM loader, no key) |
| Current bootloader | ≥ 1.2.6 | the current BL's own DFU | `openlifu-console-fw-signed.bin` |

The app version picks the row, the DFU request is issued, and the bootloader
that answers it decides the image. A unit **already in DFU** skips the request
and is classified from its USB product string alone — `STM32 BOOTLOADER` →
production image, `OW DFU x.y.z` → signed app (`LIFU BL DFU x.y.z` → refused)
— so a console whose app is dead is still recoverable as long as it reaches
one of the first two.

Legacy-bootloader units are not updatable by this SDK. The paths that migrated them authorized an interim image with the legacy bootloader's shared HMAC trust key, and that key was removed (SR-021, #91). Recover such a unit with BOOT0 (STM32 ROM DFU, then re-run) or SWD.

Defaults to the firmware images **bundled with the SDK** and needs **no
signing keys**: the secure bootloader verifies the app at boot.
An optional `keys_dir` only adds an ECDSA app-signature pre-check.

Bundled image helpers
- `bundled_production_image() -> Path` — combined bootloader+app image
  (`firmware/openlifu-console-fw-production.bin`), source for the
  no-bootloader migration.
- `bundled_signed_app() -> Path` — signed console app
  (`firmware/openlifu-console-fw-signed.bin`), source for the secure app
  update.

Class
- `LIFUFirmwareUpdate(hv=None, keys_dir=None, libusb_dll=None, vid=0x0483, pid=0xDF11)`
  - `hv` — a connected `HVController` (e.g. `interface.hvcontroller`), used to
    read the app version and trigger DFU entry. May be omitted only when the
    unit is already in a DFU mode.
  - `keys_dir` — optional; ECDSA-validate the signed app before flashing.
- `detect_cohort() -> (cohort, source)` — `cohort` is `"no-bootloader"` /
  `"legacy-bl"` / `"secure-bl"`; `source` is `"app"` (from the running app
  version) or `"dfu"` (the unit was already in a bootloader DFU).
- `update(*, production_image=None, signed_app=None, force=False, force_production=False, progress_callback=None) -> UpdateResult`
  - Detects the state and runs the right path. All image args default to the
    bundled files. `force` (secure path only) flashes even if not newer (the
    bootloader's anti-rollback floor still applies at boot).

`UpdateResult` fields: `cohort`, `action` (`"migrate-rom"` / `"force-production"`
/ `"app-update"`), `summary`, `reboot_required`.

Behaviour notes
- If a running app is present, its version names the cohort and the updater
  triggers DFU entry itself. If the unit is **already in DFU**, the DFU product
  string is used and no extra DFU entry is triggered.
- Migrations are for **unlocked (beta) units only** — after RDP/FDA lockdown
  the force-ROM-DFU switch is inert and the bootloader is not erasable.
- The no-bootloader/ROM path needs **no external tools**: the ROM-loader write
  (mass erase → program → read-back verify → leave DFU) is done by the SDK's
  own DfuSe driver, `openlifu_sdk.io.STM32DFU`.
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

## Transmitter (`LIFUTransmitterFirmwareUpdate`)

Same auto-detect pattern for the transmitter. Module 0 (the USB master):

| Unit state | App version | Path taken |
|---|---|---|
| **No DFU support** | **< 2.0.2** | **Refused** — no `OW_CMD_DFU` handler, so no software request can reboot the unit into any DFU mode. BOOT0 (ROM loader) or SWD only |
| No bootloader | 2.0.2 – 2.0.3 | Migrate via STM32 ROM DFU (combined production image; plain `OW_CMD_DFU` — those apps ignore the reserved byte and always reboot into the ROM loader) |
| Legacy bootloader | 2.0.4 – 2.0.7 | Migrate via STM32 ROM DFU (combined production image, `OW_CMD_DFU` reserved=0x77 force switch) |
| Secure bootloader | ≥ 2.0.8 | Normal signed-app update over USB DFU |
| Legacy bootloader, **dead app** | — (unit parked in `LIFU BL DFU x.y.z`) | **Refused** (BOOT0 / SWD only) |

A master whose app is corrupt parks in its legacy bootloader's USB DFU, where
the ROM-loader switch is out of reach; legacy-bootloader units are not updatable by this SDK. The paths that migrated them authorized an interim image with the legacy bootloader's shared HMAC trust key, and that key was removed (SR-021, #91). Recover such a unit with BOOT0 (STM32 ROM DFU, then re-run) or SWD.

**Slave modules** (`--module N`, N ≥ 1) are updated over I2C through the
master's passthrough with `update_slave(module, ...)`:

- **secure** (app ≥ 2.0.8): the SFU1 signed app is streamed to the slave's
  secure-bootloader I2C DFU (enumerated address 0x20+).
- **no bootloader** (app 2.0.2 – 2.0.3): **not updatable over I2C** — DFU entry
  jumps those units into the STM32 ROM loader, which does not speak our I2C
  protocol. Connect the module as the USB master and update it as module 0.
- **no DFU support** (app < 2.0.2): **refused outright** — the master cannot
  put the module into any DFU mode, and connecting it as the USB master does
  not help. BOOT0 (ROM loader over USB) or SWD only.
- **legacy** (app 2.0.4 – 2.0.7): **refused** — the one-shot I2C migration
  needed the legacy trust key (SR-021). Connect the module as the USB master
  and update it as module 0 (ROM-DFU production image).

**Rolling a NEW bootloader onto already-migrated units** (`--force-production`):

- **Module 0** (≥ 2.0.4): the running app is forced into the STM32 ROM DFU
  (`OW_CMD_DFU` reserved=0x77) and the full production image is reflashed.
- **Slave, secure BL** (≥ 2.0.8): the **signed** DFU stub
  (`firmware/openlifu-transmitter-dfu-stub-signed.bin`) is installed through
  the secure I2C DFU like a normal app — the bootloader verifies its SFU1
  signature at boot — then the production image is streamed through the
  stub's full-flash DFU. The signed stub
  carries the same FwVersion as the bundled production app, so the
  anti-rollback floor (boot check: version ≥ floor) is untouched.
- A legacy slave is refused here too.

CLI:

```
python -m openlifu_sdk.io.LIFUFirmwareUpdate --device transmitter --module 1
```

`--production` overrides the combined image.

See also: `docs/api/LIFUDFU.md` (the underlying per-scenario methods),
`docs/api/LIFUCrypto.md` (image signing/validation),
`docs/api/LIFUHVController.md` (DFU entry).
