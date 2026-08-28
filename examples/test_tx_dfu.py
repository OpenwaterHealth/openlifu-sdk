from __future__ import annotations

import sys
import time

from openlifu_sdk.io.LIFUInterface import LIFUInterface

# set PYTHONPATH=%cd%\src;%PYTHONPATH%
# python examples\test_tx_dfu.py
MODULE_ID = 0
DFU_RESERVED_LEGACY = 0x77
DFU_RESERVED = 0x00
LEGACY_VERSION_MAX = (2, 0, 3)

print("Starting LIFU Test Script...")
interface = LIFUInterface()

tx_connected, hv_connected = interface.is_device_connected()

if not tx_connected and not hv_connected:
    print("✅ LIFU Console not connected.")
    sys.exit(1)

if not tx_connected:
    print("TX device not connected. Attempting to turn on 12V...")
    interface.hvcontroller.turn_12v_on()

    # Give time for the TX device to power up and enumerate over USB
    time.sleep(2)

    # Cleanup and recreate interface to reinitialize USB devices
    interface.stop_monitoring()
    del interface
    time.sleep(5)  # Short delay before recreating

    print("Reinitializing LIFU interface after powering 12V...")
    interface = LIFUInterface()

    # Re-check connection
    tx_connected, hv_connected = interface.is_device_connected()

if tx_connected:
    print("✅ LIFU Device TX connected.")
else:
    print("❌ LIFU Device NOT fully connected.")
    print(f"  TX Connected: {tx_connected}")
    print(f"  HV Connected: {hv_connected}")
    sys.exit(1)


print("Ping the device")
if not interface.txdevice.ping():
    print("❌ failed to communicate with transmit module")
    sys.exit(1)

print("Get Version")
version = interface.txdevice.get_version(module=MODULE_ID)
print(f"Version: {version}")


def parse_fw_version(ver: str) -> tuple[int, int, int] | None:
    """Parse a 'vX.Y.Z' (or 'X.Y.Z') firmware string into a tuple, or None."""
    if not ver:
        return None
    parts = ver.lstrip("vV").split(".")
    if len(parts) < 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


# The reserved=0x77 flag triggers the legacy DFU pass-through path that
# firmware <= 2.0.3 requires to enter DFU mode. Firmware 2.0.4+ ignores it
# (and in some intermediate builds, sending it causes a NAK), so only set
# it when we're talking to a known-old firmware. If the version is
# unparseable we err on the side of NOT sending it (assume modern firmware).
fw_version = parse_fw_version(version)
reserved = DFU_RESERVED_LEGACY if (fw_version is not None and fw_version <= LEGACY_VERSION_MAX) else DFU_RESERVED
if reserved == DFU_RESERVED_LEGACY:
    print(f"Firmware {version} <= v2.0.3 detected; using legacy reserved=0x77 DFU flag.")
else:
    print(f"Firmware {version} > v2.0.3 (or unparseable); using reserved=0x00.")


# Ask the user for confirmation
user_input = input("Do you want to Enter DFU Mode? (y/n): ").strip().lower()

if user_input == 'y':
    print("Enter DFU mode")
    if interface.txdevice.enter_dfu(module=MODULE_ID, reserved=reserved):
        print("Successful.")

elif user_input == 'n':
    print("Reset device")
    if interface.txdevice.soft_reset(module=MODULE_ID):
        print("Successful.")

time.sleep(5)

if user_input == 'y':
    print("Use stm32 cube programmer to update firmware, power cycle will put the console back into an operating state")
    sys.exit(0)

if MODULE_ID == 0:
    interface.txdevice.uart.reopen_after_reset()

print("Ping the device again")
if  interface.txdevice.ping(module=MODULE_ID):
    print("Test script complete.")
else:
    print("Device did not respond after reset.")
