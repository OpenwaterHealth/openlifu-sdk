from __future__ import annotations

import argparse
import sys
import time

from openlifu_sdk.io.LIFUFirmwareUpdate import LIFUFirmwareUpdate
from openlifu_sdk.io.LIFUInterface import LIFUInterface

# One-command console firmware update. Detects the unit's state and runs the
# right path automatically:
#   no bootloader (<1.2.0)     -> migrate to the secure bootloader
#   legacy bootloader (1.2.x)  -> migrate via the RAM self-updater
#   secure bootloader (>=1.2.6) -> normal app update
#
# Uses the firmware images bundled with the SDK and needs NO signing keys.
#
# set PYTHONPATH=%cd%\src;%PYTHONPATH%
# python examples\update_console_firmware.py            # bundled images, auto-detect
# python examples\update_console_firmware.py --force    # allow same/downgrade on secure

parser = argparse.ArgumentParser(description="Update console firmware (auto-detect)")
parser.add_argument("--production", help="Override the combined bootloader+app image")
parser.add_argument("--app", help="Override the signed app image")
parser.add_argument("--keys", help="Optional keys dir to pre-validate the app signature")
parser.add_argument("--force", action="store_true",
                    help="Secure path only: flash even if not newer (bootloader "
                         "floor still applies at boot)")
args = parser.parse_args()

def progress(w: int, t: int, label: str) -> None:
    pct = 100 * w // t if t else 100
    print(f"\r  {label}: {w:,}/{t:,} ({pct}%)", end="", flush=True)

print("Connecting to the console...")
interface = LIFUInterface(TX_test_mode=False)
_tx, hv = interface.is_device_connected()
if not hv:
    print("Console not connected.")
    sys.exit(1)
interface.hvcontroller.ping()

fw = LIFUFirmwareUpdate(hv=interface.hvcontroller, keys_dir=args.keys)
cohort, source = fw.detect_cohort()
print(f"Console state: {cohort} (from {source})")

try:
    result = fw.update(
        production_image=args.production,
        signed_app=args.app,
        force=args.force,
        progress_callback=progress,
    )
except (ValueError, RuntimeError) as e:
    print(f"\nUPDATE FAILED: {e}")
    sys.exit(1)

print(f"\n{result.summary}")
if result.reboot_required:
    print("POWER-CYCLE the console to boot the new application.")
time.sleep(1)
