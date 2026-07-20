from __future__ import annotations

import argparse
import sys
import time

from openlifu_sdk.io.LIFUDFU import (
    LIFUDFUManager,
    infer_console_bootloader_from_app_version,
)
from openlifu_sdk.io.LIFUInterface import LIFUInterface

# Migrate a beta console unit to the secure bootloader, over USB only.
#
# All cohorts converge on one path: the running app honours the hidden
# force-STM32-ROM-DFU switch, the ROM loader can write the whole flash, and
# migrate_console_rom_dfu() installs the new secure bootloader + signed app.
#
#   no-bootloader (<1.2.0) | legacy BL (1.2.0-1.2.5) | secure BL (>=1.2.6)
#        --> STM32 ROM DFU --> [new secure bootloader @ 0x08000000]
#                              [signed app @ 0x08010000] --> power cycle
#
# WARNING: only works on unlocked beta units. After RDP/FDA lockdown the
# force switch is inert and the bootloader region cannot be erased.
#
# set PYTHONPATH=%cd%\src;%PYTHONPATH%
# python examples\migrate_console_bootloader.py ^
#     --bootloader path\to\lifu-console-bl.bin ^
#     --app        path\to\lifu-console-fw_signed.bin ^
#     --keys       path\to\bl-keys\console

parser = argparse.ArgumentParser(description="Migrate a console to the secure bootloader")
src = parser.add_mutually_exclusive_group(required=True)
src.add_argument("--image", help="Combined full-flash image (bootloader + "
                                 "signed app), e.g. openlifu-console-fw-prod_vX.bin")
src.add_argument("--bootloader", help="Raw secure bootloader .bin "
                                      "(use with --app instead of --image)")
parser.add_argument("--app", help="Signed SBSFU app image (with --bootloader)")
parser.add_argument("--keys", help="Keys dir to fully validate the signed app")
parser.add_argument("--already-in-dfu", action="store_true",
                    help="Unit is already in STM32 ROM DFU; skip app connection")
parser.add_argument("--dwell", type=float, metavar="SECONDS",
                    help="Safety pre-check only: force ROM DFU and confirm the "
                         "unit stays there for SECONDS (no writes). Recommended "
                         "on the first legacy-bootloader unit to rule out an "
                         "IWDG reset mid-migration.")
args = parser.parse_args()
if args.bootloader and not args.app:
    parser.error("--bootloader requires --app")

def progress(written: int, total: int, label: str) -> None:
    pct = 100 * written // total if total else 100
    print(f"\r  {label}: {written:,}/{total:,} ({pct}%)", end="", flush=True)

mgr = LIFUDFUManager()
enter_fn = None

if not args.already_in_dfu:
    print("Connecting to the console...")
    interface = LIFUInterface(TX_test_mode=False)
    _tx, hv = interface.is_device_connected()
    if not hv:
        print("Console not connected. If it is already in ROM DFU, rerun "
              "with --already-in-dfu.")
        sys.exit(1)
    interface.hvcontroller.ping()

    # Advisory: report the cohort inferred from the running app version.
    try:
        ver = interface.hvcontroller.get_version()  # app version string
        cohort = infer_console_bootloader_from_app_version(str(ver))
        print(f"Running app version : {ver}  (inferred cohort: {cohort})")
    except Exception as e:
        print(f"(could not read app version: {e})")

    enter_fn = interface.hvcontroller.enter_stm32_rom_dfu

if args.dwell:
    print(f"Dwell safety check: forcing ROM DFU, watching {args.dwell:.0f}s "
          "for an IWDG reset (no writes)...")
    stable = mgr.dwell_rom_dfu_check(enter_stm32_rom_dfu_fn=enter_fn,
                                     seconds=args.dwell)
    if stable:
        print("STABLE - the unit held ROM DFU. Safe to migrate (rerun without "
              "--dwell).")
        sys.exit(0)
    print("UNSTABLE - the unit dropped out of ROM DFU (likely IWDG). Do NOT "
          "migrate this cohort over USB; use SWD/bench.")
    sys.exit(1)

print("Migrating (this forces ROM DFU, writes the bootloader + app, and "
      "verifies)...")
try:
    if args.image:
        # Full-chip erase + write the whole combined production image. Clean
        # slate (wipes legacy metadata/config/anti-rollback), recommended.
        mgr.migrate_console_full_image(
            combined_image=args.image,
            enter_stm32_rom_dfu_fn=enter_fn,
            keys_dir=args.keys,
            progress_callback=progress,
        )
    else:
        mgr.migrate_console(
            bootloader_bin=args.bootloader,
            signed_app=args.app,
            enter_stm32_rom_dfu_fn=enter_fn,
            keys_dir=args.keys,
            progress_callback=progress,
        )
except (ValueError, RuntimeError) as e:
    print(f"\nMIGRATION FAILED: {e}")
    sys.exit(1)

print("\nMigration written and verified. POWER-CYCLE the console now.")
print("The secure bootloader will verify and launch the app; confirm with "
      "test_console_dfu.py or a normal SDK connection.")
