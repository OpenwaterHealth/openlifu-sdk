from __future__ import annotations

from openlifu_sdk.io.LIFUInterface import LIFUInterface

# set PYTHONPATH=%cd%\src;%PYTHONPATH%
# python examples\test_multiple_modules.py
"""
Test script to automate:
1. Connect to the device.
2. Test HVController: Turn HV on/off and check voltage.
3. Test Device functionality.
"""
print("Starting LIFU Test Script...")
interface = LIFUInterface()
tx_connected, hv_connected = interface.is_device_connected()
if tx_connected and hv_connected:
    print("LIFU Device Fully connected.")
else:
    print(f'LIFU Device NOT Fully Connected. TX: {tx_connected}, HV: {hv_connected}')

print("Ping the device")
interface.txdevice.ping()

print("Enumerate TX7332 chips")
num_tx_devices = interface.txdevice.enum_tx7332_devices()
if num_tx_devices > 0:

    print(f"Number of TX7332 devices found: {num_tx_devices}")
    module_count = int(num_tx_devices/2)
    for module_index in range(module_count):

        ping_result = interface.txdevice.ping(module=module_index)
        print(f"Module {module_index} ping result: {ping_result}")

        version = interface.txdevice.get_version(module=module_index)
        print(f"Module {module_index} Version: {version}")

        hw_id = interface.txdevice.get_hardware_id(module=module_index)
        print(f"Module {module_index} HW ID: {hw_id}")

        echo_data, echo_len = interface.txdevice.echo(module=module_index, echo_data=b'The cat, a sleek silhouette with emerald eyes and whiskers like twin antennae, hopped onto the sunlit windowsill, surveyed the rain-soaked street below, and let out a soft, conspiratorial meow that promised a day of quiet mischief and small adventures.')
        print(f"Module {module_index} Echo Test Returned: {echo_data} STATUS: {'PASS' if echo_data == b'The cat, a sleek silhouette with emerald eyes and whiskers like twin antennae, hopped onto the sunlit windowsill, surveyed the rain-soaked street below, and let out a soft, conspiratorial meow that promised a day of quiet mischief and small adventures.' else 'FAIL'}")

        temp = interface.txdevice.get_temperature(module=module_index)
        print(f"Module {module_index} Temperature: {temp}")

        ambient = interface.txdevice.get_ambient_temperature(module=module_index)
        print(f"Module {module_index} Ambient Temperature: {ambient}")

        # config = interface.txdevice.read_config(module=module_index)
        # print(f"Module {module_index} Config (read): {config.get_json_str() if config else None}")
# 
        # if config is not None:
        #     config.json_data['module_id'] = module_index
        #     updated_config = interface.txdevice.write_config(config, module=module_index)
        #     print(f"Module {module_index} Config (write): {updated_config.get_json_str() if updated_config else None}")


    """
    print("Write Demo Registers to TX7332 chips")
    for device_index in range(num_tx_devices):
        interface.txdevice.demo_tx7332(device_index)

    print("Starting Trigger...")
    if interface.start_sonication():
        print("Trigger Running Press enter to STOP:")
        input()  # Wait for the user to press Enter
        if interface.stop_sonication():
            print("Trigger stopped successfully.")
        else:
            print("Failed to stop trigger.")
    else:
        print("Failed to start trigger.")
    """
else:
    raise Exception("No TX7332 devices found.")
