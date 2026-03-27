"""LIFU Transmitter Firmware Update Utility

Programs a signed firmware package onto a specific transmitter module.

  Module 0  (USB master) : host ──USB DFU──► module 0
  Module 1+ (I2C slave)  : host ──UART OW──► master ──I2C 0x72──► slave DFU BL

Usage
-----
  # Module 0 (USB master) — Windows with bundled libusb DLL
  set PYTHONPATH=%cd%\\src;%PYTHONPATH%
  python examples\\test_tx_fw_update.py 0 build\\DebugBL\\lifu-transmitter-fw.bin.signed.bin ^
      --libusb-dll test\\libusb-1.0.29\\VS2022\\MS64\\dll\\libusb-1.0.dll

  # Module 1 (I2C slave, routed through master)
  python examples\\test_tx_fw_update.py 1 build\\DebugBL\\lifu-transmitter-fw.bin.signed.bin
"""

from __future__ import annotations

import argparse
import sys
import time

from openlifu_sdk.io.LIFUDFU import I2C_DFU_SLAVE_ADDR
from openlifu_sdk.io.LIFUInterface import LIFUInterface


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
        description="LIFU transmitter firmware update utility",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "module_id", type=int,
        help="Module index to update (0 = USB master, 1+ = I2C slave via master)"
    )
    p.add_argument(
        "package_file",
        help="Path to signed firmware package (.bin.signed.bin)"
    )
    p.add_argument(
        "--libusb-dll", default=None, metavar="PATH",
        help="Full path to libusb-1.0.dll (Windows, module 0 only)"
    )
    p.add_argument(
        "--vid", type=lambda x: int(x, 0), default=0x0483,
        help="USB VID for USB DFU (default: 0x0483)"
    )
    p.add_argument(
        "--pid", type=lambda x: int(x, 0), default=0xDF11,
        help="USB PID for USB DFU (default: 0xDF11)"
    )
    p.add_argument(
        "--i2c-addr", type=lambda x: int(x, 0),
        default=I2C_DFU_SLAVE_ADDR, metavar="ADDR",
        help=f"I2C slave address of DFU bootloader (default: 0x{I2C_DFU_SLAVE_ADDR:02X})"
    )
    p.add_argument(
        "--dfu-wait", type=float, default=5.0, metavar="SEC",
        help="Seconds to wait after entering DFU mode (default: 5.0)"
    )
    p.add_argument(
        "--device-type", choices=("transmitter", "console"),
        default="transmitter",
        help="Target bootloader device type (transmitter or console). Default: transmitter"
    )
    p.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt"
    )
    args = p.parse_args()

    print("=" * 60)
    print("  LIFU Transmitter Firmware Update")
    print("=" * 60)
    print(f"  Target module : {args.module_id}")
    print(f"  Package file  : {args.package_file}")
    if args.module_id == 0:
        print(f"  Interface     : USB DFU  (VID=0x{args.vid:04X}, PID=0x{args.pid:04X})")
        if args.libusb_dll:
            print(f"  libusb DLL    : {args.libusb_dll}")
    else:
        print(f"  Interface     : I2C DFU via master"
              f"  (slave addr=0x{args.i2c_addr:02X})")
    print()

    # ------------------------------------------------------------------
    # Connect to LIFU device (select interface by device type)
    # ------------------------------------------------------------------
    interface = LIFUInterface()
    tx_connected, hv_connected = interface.is_device_connected()

    # Determine which connection we require based on --device-type
    if args.device_type == "console":
        print("Connecting to LIFU console (HV controller)...")
        required_connected = hv_connected
        required_label = "HV controller"
    else:
        print("Connecting to LIFU transmitter device...")
        required_connected = tx_connected
        required_label = "TX device"

    # If transmitter is expected but not present, attempt to enable 12V
    if args.device_type == "transmitter" and not tx_connected and hv_connected:
        print("  TX device not connected — enabling 12 V rail...")
        interface.hvcontroller.turn_12v_on()
        time.sleep(2)
        interface.stop_monitoring()
        del interface
        time.sleep(3)
        print("  Re-initialising LIFU interface...")
        interface = LIFUInterface()
        tx_connected, hv_connected = interface.is_device_connected()
        required_connected = tx_connected

    if not required_connected:
        print(f"ERROR: {required_label} not connected. Cannot proceed.")
        sys.exit(1)

    print(f"  {required_label} connected.")

    # Select the device controller object to use for general commands.
    # For console-targeted operations use the HV controller; for
    # transmitter-targeted operations use the TX device.
    if args.device_type == "console":
        txdev = interface.hvcontroller
    else:
        txdev = interface.txdevice

    # ------------------------------------------------------------------
    # Verify target module is present (only for transmitter modules)
    # For console device type module must be 0 and we skip module_count.
    # ------------------------------------------------------------------
    if args.device_type == "console":
        if args.module_id != 0:
            print("ERROR: console device type only supports module 0")
            sys.exit(2)
        print("\nConsole target selected (module 0 assumed).")
    else:
        print(f"\nDetecting connected modules...")
        module_count = txdev.get_module_count()
        print(f"  Detected {module_count} module(s).")

        if module_count <= args.module_id:
            print(
                f"\nERROR: Module {args.module_id} is not present "
                f"(only {module_count} module(s) detected)."
            )
            sys.exit(1)
        print(f"  Module {args.module_id} is present.")

    # ------------------------------------------------------------------
    # Show current firmware version
    # ------------------------------------------------------------------
    print(f"\nReading current firmware version for module {args.module_id}...")
    try:
        if args.device_type == "console":
            current_version = txdev.get_version()
        else:
            current_version = txdev.get_version(module=args.module_id)
        print(f"  Current firmware version: {current_version}")
    except Exception as e:
        print(f"  WARNING: could not read current version ({e})")
        current_version = "unknown"

    # ------------------------------------------------------------------
    # Confirmation
    # ------------------------------------------------------------------
    print()
    if not args.yes:
        answer = input(
            f"Proceed with firmware update on module {args.module_id}? (y/n): "
        ).strip().lower()
        if answer != "y":
            print("Aborted by user.")
            sys.exit(0)

    # Validate module/device-type policy:
    # when targeting the console bootloader.
    if args.module_id > 0 and args.device_type == "console":
        print("ERROR: module 0 is only valid with --device-type console")
        sys.exit(2)

    # ------------------------------------------------------------------
    # Firmware update
    # ------------------------------------------------------------------
    print(f"\nStarting firmware update for module {args.module_id}...")
    try:
        if args.device_type == "console":
            # For console (module 0 USB DFU) use LIFUDFUManager with the
            # HV controller's enter_dfu function to trigger DFU on the
            # console device.
            from openlifu_sdk.io.LIFUDFU import LIFUDFUManager

            # Use the console UART when operating on the console device
            mgr = LIFUDFUManager(uart=interface.hvcontroller.uart)
            try:
                mgr.update_module(
                module=args.module_id,
                package_file=args.package_file,
                enter_dfu_fn=interface.hvcontroller.enter_dfu,
                vid=args.vid,
                pid=args.pid,
                libusb_dll=args.libusb_dll,
                i2c_addr=args.i2c_addr,
                dfu_wait_s=args.dfu_wait,
                device_type=args.device_type,
                progress_callback=_progress,
            )
            except Exception as e:
                print(f"\nERROR: console DFU failed: {e}")
                print("Hint: try increasing --dfu-wait or run test/dfu-test.py program-package --manifest")
                raise
        else:
            txdev.update_firmware(
                module=args.module_id,
                package_file=args.package_file,
                vid=args.vid,
                pid=args.pid,
                libusb_dll=args.libusb_dll,
                i2c_addr=args.i2c_addr,
                dfu_wait_s=args.dfu_wait,
                device_type=args.device_type,
                progress_callback=_progress,
            )
    except RuntimeError as e:
        print(f"\nERROR: Firmware update failed — {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: Unexpected error — {e}")
        sys.exit(1)

    print(f"\nFirmware update for module {args.module_id} completed successfully.")

    # ------------------------------------------------------------------
    # Post-update verification (I2C slave modules only)
    # ------------------------------------------------------------------
    if args.module_id != 0:
        print("Waiting for module to restart...")
        time.sleep(2)
        try:
            if txdev.ping(module=args.module_id):
                new_version = txdev.get_version(module=args.module_id)
                print(f"  New firmware version: {new_version}")
                if new_version == current_version:
                    print("  WARNING: version unchanged — verify the package was correct.")
            else:
                print("  WARNING: module did not respond to ping after update.")
        except Exception as e:
            print(f"  WARNING: post-update check failed ({e})")
    else:
        if args.device_type == "console":
            print("Module 0 will reboot from the new firmware after DFU manifest.")
            print("Power-cycle or wait for USB re-enumeration before reconnecting.")
        else:
            print("Module 0 (master) will reboot from the new firmware after DFU manifest.")
            print("Power-cycle or wait for USB re-enumeration before reconnecting.")


if __name__ == "__main__":
    main()
