"""LIFU Transmitter I2C Firmware Update — DFU-already-active variant

Use this script when the slave module is already sitting in DFU bootloader
mode (e.g. it has no application yet, it failed to boot its application and
fell back to the BL, or you entered DFU mode manually) and the normal
firmware-update flow cannot connect to the live application to request DFU
entry.

The SECURE bootloader (open-lifu-transmitter-bl) consumes the RAW signed
image produced by sign_firmware.py — [320B 'SFU1' header][0xFF pad][encrypted
firmware] — written whole to the slot base 0x08010000. It does NOT use the
legacy PGK1 package format (parse_signed_package / program_i2c), whose
metadata-page writes the secure BL rejects with BAD_ADDR.

The script:
  1. Connects to the master module via LIFUInterface (USB VCP).
  2. Pings the slave I2C DFU bootloader at *i2c_addr* (default 0x72) via the
     master's OW_I2C_PASSTHRU passthrough to confirm it is responsive.
  3. Programs the raw signed image: mass erase -> write @0x08010000 ->
     manifest -> reset.
  4. Reads back the new application version through the master.

Usage
-----
  set PYTHONPATH=%cd%\\src;%PYTHONPATH%
  python examples\\test_tx_i2c_update.py <signed_image> [options]

Examples
--------
  # Defaults (slave addr=0x72, module index 1)
  python examples\\test_tx_i2c_update.py openlifu-transmitter-fw-signed.bin

  # Custom slave address
  python examples\\test_tx_i2c_update.py openlifu-transmitter-fw-signed.bin --i2c-addr 0x73

See also: open-lifu-transmitter-bl/test/program_slave_i2c.py (same flow, with
an optional enter-DFU step for a slave whose application is still running).
"""

from __future__ import annotations

import argparse
import sys
import time

from openlifu_sdk.io.LIFUDFU import I2C_DFU_SLAVE_ADDR, STM32I2CDFUviaMaster
from openlifu_sdk.io.LIFUInterface import LIFUInterface

SLOT_BASE = 0x08010000   # raw signed image is written here whole


# ---------------------------------------------------------------------------
# Progress display helper
# ---------------------------------------------------------------------------

def _progress(written: int, total: int, label: str = "write") -> None:
    pct = 100 * written // total if total else 100
    filled = pct // 5
    bar = "#" * filled + "-" * (20 - filled)
    print(f"\r  {label}: [{bar}] {pct:3d}%  ({written}/{total} B)",
          end="", flush=True)
    if written >= total:
        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="LIFU I2C DFU firmware update (slave already in DFU mode)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "signed_image",
        help="Raw signed image from sign_firmware.py "
             "(e.g. openlifu-transmitter-fw-signed.bin)"
    )
    p.add_argument(
        "--module", type=int, default=1, metavar="IDX",
        help="Module index of the slave (default: 1)"
    )
    p.add_argument(
        "--i2c-addr", type=lambda x: int(x, 0),
        default=I2C_DFU_SLAVE_ADDR, metavar="ADDR",
        help=f"I2C slave address of DFU bootloader (default: 0x{I2C_DFU_SLAVE_ADDR:02X})"
    )
    p.add_argument(
        "--post-wait", type=float, default=6.0, metavar="SEC",
        help="Seconds to wait after reset before reading new version (default: 6.0)"
    )
    p.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt"
    )
    args = p.parse_args()

    print("=" * 60)
    print("  LIFU I2C DFU Firmware Update (slave already in DFU mode)")
    print("=" * 60)
    print(f"  Signed image  : {args.signed_image}")
    print(f"  Module index  : {args.module}")
    print(f"  Slave I2C addr: 0x{args.i2c_addr:02X}")
    print()

    # ------------------------------------------------------------------
    # Sanity-check the image before anything touches hardware
    # ------------------------------------------------------------------
    with open(args.signed_image, "rb") as f:
        raw = f.read()
    if raw[0:4] != b"SFU1":
        print(f"ERROR: {args.signed_image} does not start with 'SFU1' — "
              f"expected a raw signed image from sign_firmware.py.")
        sys.exit(1)
    print(f"  Image: {len(raw)} bytes -> slave slot 0x{SLOT_BASE:08X}")
    print()

    # ------------------------------------------------------------------
    # Connect to the master module
    # ------------------------------------------------------------------
    print("Connecting to LIFU interface...")
    interface = LIFUInterface()
    tx_connected, _ = interface.is_device_connected()
    if not tx_connected:
        print("ERROR: TX device (master) not connected.")
        sys.exit(1)
    if not interface.txdevice.ping():
        print("ERROR: master module did not answer ping.")
        sys.exit(1)
    print(f"  Master module connected "
          f"(version {interface.txdevice.get_version(module=0)}).")

    dfu = STM32I2CDFUviaMaster(uart=interface.txdevice.uart,
                               i2c_addr=args.i2c_addr)

    # ------------------------------------------------------------------
    # Ping the slave DFU bootloader
    # ------------------------------------------------------------------
    print(f"\nPinging slave DFU bootloader at 0x{args.i2c_addr:02X}...")
    try:
        blver = dfu.get_version()
        blver = blver.decode(errors="replace") if isinstance(blver, (bytes, bytearray)) else blver
        print(f"  Bootloader version: {blver}")
    except Exception as e:
        print(f"ERROR: Slave DFU bootloader at 0x{args.i2c_addr:02X} did not respond: {e}")
        print("  Make sure the slave module is powered and in DFU bootloader mode.")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Confirmation
    # ------------------------------------------------------------------
    print()
    if not args.yes:
        answer = input(
            f"Proceed with firmware update on slave 0x{args.i2c_addr:02X}? (y/n): "
        ).strip().lower()
        if answer != "y":
            print("Aborted by user.")
            sys.exit(0)

    # ------------------------------------------------------------------
    # Program: mass-erase app slot, write the raw signed image whole at the
    # slot base, manifest, reset. The secure BL verifies the signature,
    # decrypts and launches the application on the next boot.
    # ------------------------------------------------------------------
    print(f"\nProgramming slave 0x{args.i2c_addr:02X}...")
    try:
        print("  mass-erasing slave application slot...")
        dfu.mass_erase()
        dfu.write_memory(SLOT_BASE, raw, progress_callback=_progress)
        print("  manifest...")
        dfu.manifest()
        print("  resetting slave (secure BL verifies signature + launches app)...")
        dfu.reset()
    except Exception as e:
        print(f"\nERROR: Programming failed — {e}")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Post-update version check via the normal application protocol
    # ------------------------------------------------------------------
    print(f"Waiting {args.post_wait:.0f} s for slave to boot application...")
    time.sleep(args.post_wait)

    try:
        version = interface.txdevice.get_version(module=args.module)
        print(f"  Module {args.module} firmware version: {version}")
        print("\nSLAVE I2C UPDATE COMPLETE")
    except Exception as e:
        print(f"  WARNING: could not read module {args.module} version yet ({e}).")
        print("  The bootloader may still be verifying/installing; "
              "retry test_tx_getversion.py shortly.")


if __name__ == "__main__":
    main()
