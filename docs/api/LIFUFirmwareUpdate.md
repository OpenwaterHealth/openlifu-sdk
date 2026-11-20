# `LIFUFirmwareUpdate` API

One high-level entry point for **console** firmware updates. It auto-detects
the unit's state and runs the correct path — you don't choose the scenario:

| Unit state | App version | DFU entered | Image used |
|---|---|---|---|
| **No DFU support** | **< 1.1.0** | — the request never reaches a DFU | **Refused** (BOOT0 / SWD only) |
| No bootloader | 1.1.0 – 1.2.2 | STM32 ROM DFU (plain `OW_CMD_DFU`) | `openlifu-console-fw-production.bin` |
| Legacy bootloader | 1.2.3 – 1.2.5 | the legacy BL's own DFU | `openlifu-console-legacy-updater.bin`, then `openlifu-console-fw-signed.bin` |
| Current bootloader | ≥ 1.2.6 | the current BL's own DFU | `openlifu-console-fw-signed.bin` |

The app version picks the row, the DFU request is issued, and the bootloader
that answers it decides the image. A unit **already in DFU** skips the request
and is classified from its USB product string alone — `STM32 BOOTLOADER` →
production image, `LIFU BL DFU x.y.z` → updater + signed app, `OW DFU x.y.z` →
signed app — so a console whose app is dead is still recoverable as long as it
reaches one of those.

**The SDK carries no firmware.** The caller passes each image, taken from the
firmware releases:
- research: the GitHub releases of `openlifu-console-fw` /
  `openlifu-transmitter-fw` and `open-lifu-*-bl-updater` (the test app);
- clinical: the private artifact bucket, packaged by `openlifu-fw-updater`.

A path that needs an image it was not given refuses before anything is
written. No **signing keys** are needed: the legacy updater is authenticated by an HMAC trust tag
computed on the fly, and the secure bootloader verifies the app at boot.
An optional `keys_dir` only adds an ECDSA app-signature pre-check.

Images per path
- secure bootloader: `signed_app` (`--app`); a 1.0.x bootloader, or
  `replace_bootloader=True` (`--replace-bootloader`), also needs `bl_updater`
  (`--bl-updater`);
- no bootloader (ROM loader) and `force_production`: `production_image`
  (`--production`, bootloader + signed app);
- legacy bootloader: `signed_app` and `updater_bin` (`--updater`).

`openlifu_sdk.util.firmware.firmware_image_version(path)` reads the version an
image carries.

Class
- `LIFUFirmwareUpdate(hv=None, keys_dir=None, libusb_dll=None, vid=0x0483, pid=0xDF11)`
  - `hv` — a connected `HVController` (e.g. `interface.hvcontroller`), used to
    read the app version and trigger DFU entry. May be omitted only when the
    unit is already in a DFU mode.
  - `keys_dir` — optional; ECDSA-validate the signed app before flashing.
- `detect_cohort() -> (cohort, source)` — `cohort` is `"no-bootloader"` /
  `"legacy-bl"` / `"secure-bl"`; `source` is `"app"` (from the running app
  version) or `"dfu"` (the unit was already in a bootloader DFU).
- `update(*, production_image=None, signed_app=None, updater_bin=None, force=False, bl_updater=None, replace_bootloader=False, progress_callback=None) -> UpdateResult`
  - Detects the state and runs the right path with the images it is given
    (see "Images per path"). `force` (secure path only) flashes even if not
    newer (the bootloader's anti-rollback floor still applies at boot).
    `replace_bootloader` installs `bl_updater` even on a current bootloader:
    the bootloader it carries replaces the installed one (a research unit
    converted to clinical by openlifu-fw-updater).

`UpdateResult` fields: `cohort`, `action` (`"migrate-rom"` / `"migrate-legacy"`
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
- **Version encoding**: release images use the SDK's bitfield `FwVersion`
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

Rebuilding the console legacy updater image (for an updated
secure bootloader) is documented in `console-legacy-updater/README.md` — the
updater embeds a specific bootloader blob and is keyless.

## Transmitter (`LIFUTransmitterFirmwareUpdate`)

Same auto-detect pattern for the transmitter. Module 0 (the USB master):

| Unit state | App version | Path taken |
|---|---|---|
| **No DFU support** | **< 2.0.2** | **Refused** — no `OW_CMD_DFU` handler, so no software request can reboot the unit into any DFU mode. BOOT0 (ROM loader) or SWD only |
| No bootloader | 2.0.2 – 2.0.3 | Migrate via STM32 ROM DFU (combined production image; plain `OW_CMD_DFU` — those apps ignore the reserved byte and always reboot into the ROM loader) |
| Legacy bootloader | 2.0.4 – 2.0.7 | Migrate via STM32 ROM DFU (combined production image, `OW_CMD_DFU` reserved=0x77 force switch) |
| Secure bootloader | ≥ 2.0.8 | Normal signed-app update over USB DFU |
| Legacy bootloader, **dead app** | — (unit parked in `LIFU BL DFU x.y.z`) | USB legacy-updater recovery (see below) |

**Recovering a master with a corrupt/missing app.** Both migration rows above
need the *running* app to reach the STM32 ROM loader. When the app is corrupt
the legacy bootloader refuses to boot the slot and parks in its own USB DFU,
so that switch is gone — `detect()` reports `("legacy-bl", "dfu")` and
`update()` takes a USB-only path instead:

1. The RAM-resident **legacy updater**
   (`--updater`, the transmitter legacy updater image), which *embeds* the
   secure bootloader, is written to the legacy app slot (0x08010000) with an
   HMAC-trust-tagged WFM1 block at 0x0800F800 (computed on the fly — no keys),
   then read back and verified.
2. Reset: the legacy bootloader validates the trust tag, boots the updater,
   and the updater rewrites the bootloader region from the inside and resets.
3. The secure bootloader finds no SFU1 image in the slot (the updater is
   sitting there), parks in its own DFU, and the signed app is flashed
   over it.

The I2C DFU stub cannot be used here — it has no USB stack and needs a healthy
master app to broker it. **Keep the unit powered** through step 2; if the
secure bootloader never appears, recover via BOOT0 (STM32 ROM DFU) or SWD.
Unlike the ROM-DFU migration this path leaves the anti-rollback floor page
(0x0803F000) alone, so follow it with `--force-production` from the recovered
app to roll the newest bootloader and clear any stale floor.

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
- **legacy** (app 2.0.4 – 2.0.7): **one-shot migration** of bootloader + app.
  1. The small RAM-resident **DFU stub**
     (`stub_bin=`, built from
     `stub-code/transmitter/`) is written through the legacy I2C DFU with an
     HMAC-trust-tagged WFM1 metadata block (computed on the fly — no keys).
  2. The legacy bootloader validates and boots the stub, which copies itself
     to SRAM and serves the same I2C DFU protocol at the default address
     0x72 — with the writable window opened to the whole 256 KB flash.
  3. The SDK full-chip-erases (this also resets any stale anti-rollback
     floor) and streams the whole production image
     (`--production`) to 0x08000000.
  4. On reset the freshly written secure bootloader verifies the signed app
     in its slot and launches it.

  The stub embeds no bootloader blob, so a new bootloader release only needs
  a new production image — the stub is unchanged. **Keep the slave powered**
  from the full-chip erase until the write completes; the stub keeps serving
  DFU from RAM after any failed write (retry is possible), but a power loss
  in that window leaves the module SWD-recoverable only.

**Rolling a NEW bootloader onto already-migrated units** (`--force-production`):

- **Module 0** (≥ 2.0.4): the running app is forced into the STM32 ROM DFU
  (`OW_CMD_DFU` reserved=0x77) and the full production image is reflashed.
- **Slave, secure BL** (≥ 2.0.8): the **signed** DFU stub
  (`stub_bin=`) is installed through
  the secure I2C DFU like a normal app — the bootloader verifies its SFU1
  signature at boot — then the production image is streamed through the
  stub's full-flash DFU, same as the legacy migration. The signed stub
  must carry the production app's FwVersion, so the
  anti-rollback floor (boot check: version ≥ floor) is untouched.
- A legacy slave with `--force-production` simply takes the normal legacy
  path, which already reflashes the bootloader.

CLI:

```
python -m openlifu_sdk.io.LIFUFirmwareUpdate --device transmitter --module 1
```

`--legacy` forces the one-shot migration path; omit it to auto-detect from
the slave's app version. `--production` overrides the combined image, and
`--updater` overrides the RAM-resident legacy updater used by the USB
recovery path (and by the console legacy migration).

## Replacing a 1.0.x bootloader (bootloader updater shim)

Research units shipped with console app 1.2.6 / bootloader `1.0.3-rc.1` and
transmitter app 2.0.8 / bootloader `1.0.1`. The 1.1.x bootloaders move the
slot end and add the preset area, so those units must get the new bootloader
before a 1.3.x / 2.1.x application. `update()` (console, transmitter master)
and `update_slave()` do it on the way, over the normal DFU paths, with no ROM
loader and no SWD:

1. Read the secure bootloader's version from its DFU. Older than **1.1.0**
   (`bootloader_needs_migration()`): install the **bootloader updater**
   first — `open-lifu-console-bl-updater` / `open-lifu-transmitter-bl-updater`,
   a one-shot application signed with the research key the old bootloader
   trusts (image version 1.2.31 / 2.0.31: above every fielded floor, below the
   first application for the new bootloader).
2. The old bootloader verifies and launches it. It rewrites pages 0-31 with
   the bootloader it carries, erases the old slot, the preset area and the
   anti-rollback floor (the runtime-config page is kept), and resets into the
   **new** bootloader's DFU — USB on a console or master, I2C on a slave.
3. The SDK waits for the new bootloader (`MIGRATION_TIMEOUT_S`) and installs
   the application there. The result's `action` is `bl-migrate` /
   `bl-migrate-i2c`.

The updater refuses a protected unit (RDP > 0 or write protection) without
writing anything and comes back in the old bootloader's DFU; the SDK then
reports that no newer bootloader came up. A power loss while the bootloader
pages are rewritten (a few seconds) needs SWD recovery.

The updaters are the `open-lifu-{console,transmitter}-bl-updater` release
assets, passed as `bl_updater=` / `--bl-updater`. With `replace_bootloader=True`
(`--replace-bootloader`) the updater is installed even on a 1.1.x bootloader:
openlifu-fw-updater converts a research unit to clinical this way, with an
updater that carries the clinical bootloader. The SDK knows the new bootloader
is up when its DFU reports at least 1.1.0 **and** the slot is empty (the
updater erases it; the old bootloader still holds it).

**Whole transmitter chain:** `update_chain()` / `--all` updates every slave
over I2C first, while the master still runs the firmware that brokers it, then
the master over USB:

```
python -m openlifu_sdk.io.LIFUFirmwareUpdate --device transmitter --all
```

**Re-enumeration:** transmitter firmware up to 2.1.0-rc.4 hangs a slave that is
running its application when the chain is re-enumerated
(openlifu-transmitter-fw#80). The SDK re-enumerates only once the slave being
updated is in a bootloader; `slave_module_version()` / `--detect` read without
re-enumerating. In a chain of several old slaves, another slave still running
its application can hang during that re-enumeration; it comes back after its
watchdog reset and the next slave's update picks it up again.

See also: `docs/api/LIFUDFU.md` (the underlying per-scenario methods),
`docs/api/LIFUCrypto.md` (image signing/validation),
`docs/api/LIFUHVController.md` (DFU entry).
