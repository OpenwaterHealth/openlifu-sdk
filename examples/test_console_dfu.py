from __future__ import annotations

import argparse
import sys
import time

from openlifu_sdk.io.LIFUDFU import LIFUDFUManager
from openlifu_sdk.io.LIFUInterface import LIFUInterface

# End-to-end console firmware update over USB DFU using the SDK
# (replaces the bootloader repo's flash_firmware.py / STM32CubeProgrammer):
#   1. Connect to the running console and request DFU mode.
#   2. Wait for the DFU bootloader to enumerate; print its version.
#   3. Validate the signed image locally (LIFUCrypto) and read the installed
#      version over DFU — a downgrade is refused BEFORE anything is erased.
#   4. Program the image, manifest, and let the bootloader verify + boot it.
#
# set PYTHONPATH=%cd%\src;%PYTHONPATH%
# python examples\test_console_dfu.py path\to\lifu-console-fw_signed.bin ^
#        [--keys path\to\bl-keys\console] [--force]

parser = argparse.ArgumentParser(description="Console firmware update via USB DFU")
parser.add_argument("image", help="Signed SBSFU image (from LIFUCrypto or "
                                  "the bootloader's sign_firmware.py)")
parser.add_argument("--keys", help="Keys directory for full signature "
                                   "validation before flashing (recommended)")
parser.add_argument("--force", action="store_true",
                    help="Flash even if the image version is below the "
                         "installed one (bootloader may still reject at boot)")
parser.add_argument("--already-in-dfu", action="store_true",
                    help="Skip app connection; the console is already in DFU "
                         "(e.g. empty slot after a rejected image)")
args = parser.parse_args()

def progress(written: int, total: int, label: str) -> None:
    pct = 100 * written // total if total else 100
    print(f"\r  {label}: {written:,}/{total:,} bytes ({pct}%)", end="", flush=True)

mgr = LIFUDFUManager()
enter_dfu_fn = None

if not args.already_in_dfu:
    print("Connecting to the console...")
    interface = LIFUInterface(TX_test_mode=False)
    _tx, hv_connected = interface.is_device_connected()
    if not hv_connected:
        print("Console not connected. If it is sitting in DFU mode "
              "(e.g. after a rejected image), rerun with --already-in-dfu.")
        sys.exit(1)
    interface.hvcontroller.ping()
    enter_dfu_fn = interface.hvcontroller.enter_dfu

try:
    mgr.update_console(
        args.image,
        enter_dfu_fn=enter_dfu_fn,
        keys_dir=args.keys,
        force=args.force,
        progress_callback=progress,
    )
except ValueError as e:
    print(f"\nREFUSED: {e}")
    sys.exit(1)

print("\nProgramming complete - the bootloader is now verifying the image.")
print("Waiting for the application to boot...")
time.sleep(6)

interface = LIFUInterface(TX_test_mode=False)
_tx, hv_connected = interface.is_device_connected()
if hv_connected and interface.hvcontroller.ping():
    print("Console is back up and responding.")
else:
    print("Console did not come back - if the bootloader rejected the image "
          "(anti-rollback), the board is in DFU mode awaiting a valid image.")
    sys.exit(1)
