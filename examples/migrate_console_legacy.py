from __future__ import annotations

import argparse
import sys
import time

from openlifu_sdk.io.LIFUDFU import (
    LIFUDFUManager,
    infer_console_bootloader_from_app_version,
)
from openlifu_sdk.io.LIFUInterface import LIFUInterface

# Migrate a LEGACY-bootloader console (app 1.2.0-1.2.5) to the secure
# bootloader, over USB only.
#
# Legacy units can't reach the STM32 ROM DFU, so this flashes a RAM-resident
# self-updater via the legacy bootloader's own DFU; the legacy BL boots it and
# it rewrites the bootloader region from RAM, then the SDK flashes the signed
# app over the resulting secure DFU.
#
#   1.2.5 app --enter_dfu--> legacy BL DFU --write updater+meta--> reset
#            --> updater replaces BL --> secure BL DFU --> flash signed app
#
# WARNING: beta/unlocked units only. The bootloader self-replacement is the one
# irreversible step - keep the unit powered throughout.
#
# set PYTHONPATH=%cd%\src;%PYTHONPATH%
# python examples\migrate_console_legacy.py ^
#     --updater path\to\updater.bin ^
#     --app     path\to\lifu-console-fw_signed.bin ^
#     --keys    path\to\bl-keys\console

parser = argparse.ArgumentParser(description="Migrate a legacy-bootloader console")
parser.add_argument("--updater", required=True,
                    help="console-legacy-updater binary (updater.bin; embeds "
                         "the new secure bootloader)")
parser.add_argument("--app", required=True, help="Signed SBSFU app image")
parser.add_argument("--keys", help="Keys dir to validate the signed app")
args = parser.parse_args()

def progress(written: int, total: int, label: str) -> None:
    pct = 100 * written // total if total else 100
    print(f"\r  {label}: {written:,}/{total:,} ({pct}%)", end="", flush=True)

print("Connecting to the console...")
interface = LIFUInterface(TX_test_mode=False)
_tx, hv = interface.is_device_connected()
if not hv:
    print("Console not connected.")
    sys.exit(1)
interface.hvcontroller.ping()

ver = str(interface.hvcontroller.get_version())
cohort = infer_console_bootloader_from_app_version(ver)
print(f"Running app version : {ver}  (cohort: {cohort})")
if cohort != "legacy-bl":
    print(f"This script is for legacy-bootloader units (app 1.2.0-1.2.5). "
          f"This unit is '{cohort}'. Use the right migration path:")
    print("  no-bootloader (<1.2.0): migrate_console_bootloader.py --image ...")
    print("  secure (>=1.2.6):       test_console_dfu.py (normal app update)")
    sys.exit(1)

mgr = LIFUDFUManager()
print("Migrating (legacy DFU -> updater -> secure BL -> app)...")
try:
    mgr.migrate_console_legacy(
        updater_bin=args.updater,
        signed_app=args.app,
        enter_dfu_fn=interface.hvcontroller.enter_dfu,   # normal DFU -> legacy BL
        keys_dir=args.keys,
        progress_callback=progress,
    )
except (ValueError, RuntimeError) as e:
    print(f"\nMIGRATION FAILED: {e}")
    sys.exit(1)

print("\nMigration complete. POWER-CYCLE the console now.")
print("The secure bootloader will verify and launch the app.")
time.sleep(1)
