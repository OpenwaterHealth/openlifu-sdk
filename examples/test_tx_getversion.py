from __future__ import annotations

import sys
import time

from openlifu_sdk.io.LIFUInterface import LIFUInterface

# set PYTHONPATH=%cd%\src;%PYTHONPATH%
# python examples/test_tx_getversion.py

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

module_idx = 0  # Assuming you want to get the version for module index 0
module_count = interface.txdevice.get_tx_module_count()
print(f"TX Module Count: {module_count}")

for module_idx in range(module_count):
    print(f"Getting version for module index: {module_idx}")
    version = interface.txdevice.get_version(module=module_idx)
    print(f"Version for module {module_idx}: {version}")

    hardware_id = interface.txdevice.get_hardware_id(module=module_idx)
    print(f"Hardware ID for module {module_idx}: {hardware_id}")


