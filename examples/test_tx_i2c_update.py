"""LIFU Transmitter I2C Firmware Update — DFU-already-active variant

Use this script when the slave module is already sitting in DFU bootloader
mode (e.g. it failed to boot its application and fell back to the BL, or you
entered DFU mode manually) and the normal test_tx_fw_update.py flow cannot
connect to the live application to request DFU entry.

The script:
  1. Connects to the master module via UART (USB VCP).
  2. Pings the slave I2C DFU bootloader at *i2c_addr* (default 0x72) via the
     master's OW_I2C_PASSTHRU passthrough to confirm it is responsive.
  3. Programs the signed firmware package.
  4. Optionally pings the slave after reset to report the new version.

Usage
-----
  set PYTHONPATH=%cd%\\src;%PYTHONPATH%
  python examples\\test_tx_i2c_update.py <package_file> [options]

Examples
--------
  # Defaults (VID=0x0483, PID=0x57AF, slave addr=0x72)
  python examples\\test_tx_i2c_update.py build\\DebugBL\\lifu-transmitter-fw.bin.signed.bin

  # Custom slave address
  python examples\\test_tx_i2c_update.py firmware.bin.signed.bin --i2c-addr 0x73
"""

from __future__ import annotations

import argparse
import sys
import time

from openlifu_sdk.io.LIFUDFU import I2C_DFU_SLAVE_ADDR, LIFUDFUManager
from openlifu_sdk.io.LIFUUart import LIFUUart


# ---------------------------------------------------------------------------
# Progress display helper
# ---------------------------------------------------------------------------

def _progress(written: int, total: int, label: str) -> None:
    pct = 100 * written // total
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
        "package_file",
        help="Path to signed firmware package (.bin.signed.bin)"
    )
    p.add_argument(
        "--i2c-addr", type=lambda x: int(x, 0),
        default=I2C_DFU_SLAVE_ADDR, metavar="ADDR",
        help=f"I2C slave address of DFU bootloader (default: 0x{I2C_DFU_SLAVE_ADDR:02X})"
    )
    p.add_argument(
        "--vid", type=lambda x: int(x, 0), default=0x0483,
        help="USB VID of the master TX module VCP (default: 0x0483)"
    )
    p.add_argument(
        "--pid", type=lambda x: int(x, 0), default=0x57AF,
        help="USB PID of the master TX module VCP (default: 0x57AF)"
    )
    p.add_argument(
        "--baudrate", type=int, default=921600,
        help="UART baud rate (default: 921600)"
    )
    p.add_argument(
        "--post-wait", type=float, default=3.0, metavar="SEC",
        help="Seconds to wait after reset before reading new version (default: 3.0)"
    )
    p.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt"
    )
    args = p.parse_args()

    print("=" * 60)
    print("  LIFU I2C DFU Firmware Update (slave already in DFU mode)")
    print("=" * 60)
    print(f"  Package file  : {args.package_file}")
    print(f"  Slave I2C addr: 0x{args.i2c_addr:02X}")
    print(f"  Master VCP    : VID=0x{args.vid:04X}, PID=0x{args.pid:04X}")
    print()

    # ------------------------------------------------------------------
    # Connect to master module UART
    # ------------------------------------------------------------------
    print("Connecting to master module UART...")
    uart = LIFUUart(vid=args.vid, pid=args.pid, baudrate=args.baudrate,
                    timeout=10, desc="TX")
    uart.port = uart.list_vcp_with_vid_pid()
    if uart.port is None:
        print(f"ERROR: No USB VCP found with VID=0x{args.vid:04X}, PID=0x{args.pid:04X}.")
        sys.exit(1)
    uart.connect()

    if not uart.is_connected():
        print("ERROR: Could not connect to master module UART.")
        sys.exit(1)
    print(f"  Connected on {uart.port}.")

    mgr = LIFUDFUManager(uart=uart)

    # ------------------------------------------------------------------
    # Ping the slave DFU bootloader
    # ------------------------------------------------------------------
    print(f"\nPinging slave DFU bootloader at 0x{args.i2c_addr:02X}...")
    try:
        bl_version = mgr.get_bootloader_version_i2c(i2c_addr=args.i2c_addr)
        print(f"  Bootloader version: {bl_version}")
    except Exception as e:
        print(f"ERROR: Slave DFU bootloader at 0x{args.i2c_addr:02X} did not respond: {e}")
        print("  Make sure the slave module is powered and in DFU bootloader mode.")
        uart.disconnect()
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
            uart.disconnect()
            sys.exit(0)

    # ------------------------------------------------------------------
    # Show package layout before programming
    # ------------------------------------------------------------------
    from openlifu_sdk.io.LIFUDFU import STM32I2CDFUviaMaster, parse_signed_package
    with open(args.package_file, "rb") as _f:
        _pkg = parse_signed_package(_f.read())
    print(f"  Package layout:")
    print(f"    fw  : {len(_pkg['fw']):6d} B @ 0x{_pkg['fw_address']:08X}")
    print(f"    meta: {len(_pkg['meta']):6d} B @ 0x{_pkg['meta_address']:08X}  (written by bootloader at manifest)")

    # ------------------------------------------------------------------
    # Program
    # ------------------------------------------------------------------
    print(f"\nProgramming slave 0x{args.i2c_addr:02X}...")
    try:
        mgr.program_i2c(
            package_file=args.package_file,
            i2c_addr=args.i2c_addr,
            progress_callback=_progress,
        )
    except RuntimeError as e:
        print(f"\nERROR: Programming failed — {e}")
        uart.disconnect()
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: Unexpected error — {e}")
        uart.disconnect()
        sys.exit(1)

    print("\nProgramming complete. Resetting slave...")
    try:
        dfu = STM32I2CDFUviaMaster(uart=uart, i2c_addr=args.i2c_addr)
        dfu.reset()
    except Exception as e:
        print(f"  WARNING: reset command failed ({e}) — slave may self-reset after manifest.")

    # ------------------------------------------------------------------
    # Post-update version check
    # ------------------------------------------------------------------
    print(f"Waiting {args.post_wait:.0f} s for slave to boot application...")
    time.sleep(args.post_wait)

    try:
        from openlifu_sdk.io.LIFUConfig import OW_CONTROLLER
        from openlifu_sdk.io.LIFUConfig import OW_CMD_VERSION

        # Module index 1 is the first slave; send version request via UART OW
        r = uart.send_packet(id=None, packetType=OW_CONTROLLER,
                             command=OW_CMD_VERSION, addr=1)
        if r is not None and r.data:
            new_version = bytes(r.data).rstrip(b"\x00").decode("ascii", errors="replace")
            print(f"  New firmware version: {new_version}")
        else:
            print("  WARNING: version read returned no data — "
                  "slave may still be booting or module index differs.")
    except Exception as e:
        print(f"  WARNING: post-update version check failed ({e})")

    uart.disconnect()
    print("\nDone.")


if __name__ == "__main__":
    main()
