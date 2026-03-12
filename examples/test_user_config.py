#!/usr/bin/env python3
"""
User Configuration Utility Script

Read and modify user configuration on the Lifu device.

Usage:
    # Read and display current configuration
    python scripts/user_config.py --read

    # Get a specific value
    python scripts/user_config.py --get <key>

    # Set values (types auto-detected: int, float, bool, string)
    python scripts/user_config.py --set key1 value1 [--set key2 value2]

    # Remove keys from configuration
    python scripts/user_config.py --remove key1 [--remove key2]

    # Write JSON file to device
    python scripts/user_config.py --write-file <file.json>

    # Read and save to file
    python scripts/user_config.py --read --output <file.json>
"""

import sys
import json
import argparse
import time
from openlifu_sdk.io.LIFUInterface import LIFUInterface

def parse_value(value_str: str) -> any:
    """
    Parse a string value and auto-detect its Python/JSON type.

    Args:
        value_str: String to parse

    Returns:
        Parsed value as int, float, bool, or str
    """
    # Check for boolean literals (case-insensitive)
    if value_str.lower() == 'true':
        return True
    if value_str.lower() == 'false':
        return False

    # Check for null/none
    if value_str.lower() in ('null', 'none', ''):
        return None

    # Try to parse as integer
    try:
        return int(value_str)
    except ValueError:
        pass

    # Try to parse as float
    try:
        return float(value_str)
    except ValueError:
        pass

    # Return as string (default)
    return value_str


def parse_json_value(value_str: str) -> any:
    """
    Parse a string as JSON. If it's not valid JSON, fall back to auto-detect.

    Args:
        value_str: String to parse (expecting JSON format)

    Returns:
        Parsed JSON value or the original parsed type
    """
    try:
        return json.loads(value_str)
    except json.JSONDecodeError:
        # Fall back to auto-detection for non-JSON values
        return parse_value(value_str)


def read_config_action(interface, output_file: str = None) -> bool:
    """Read configuration from device and display or save it."""
    print("Reading configuration from device...")
    config = interface.txdevice.read_config()

    if config is None:
        print("Error: Failed to read configuration")
        return False

    print("\nConfiguration metadata:")
    print(f"  Sequence: {config.header.seq}")
    print(f"  CRC: 0x{config.header.crc:04X}")
    print(f"  JSON length: {config.header.json_len}")

    json_str = config.get_json_str()
    print("\nJSON data:")
    print(json_str)

    if output_file:
        try:
            with open(output_file, 'w') as f:
                json.dump(config.to_dict(), f, indent=2)
            print(f"\nConfiguration saved to: {output_file}")
        except IOError as e:
            print(f"Error writing to file: {e}")
            return False

    return True


def get_value_action(interface, key: str) -> bool:
    """Get a specific value from the configuration."""
    print("Reading configuration from device...")
    config = interface.txdevice.read_config()

    if config is None:
        print("Error: Failed to read configuration")
        return False

    value = config.get(key)
    if value is None:
        print(f"Key '{key}' not found in configuration")
        return False

    # Print the raw value
    print(f"{key} = {value}")

    return True


def set_values_action(interface, key_value_pairs: list) -> bool:
    """Set one or more values in the configuration."""
    print("Reading current configuration...")
    config = interface.txdevice.read_config()

    if config is None:
        print("Error: Failed to read configuration")
        return False

    print(f"\nOriginal sequence: {config.header.seq}")

    # Apply updates
    for key, value_str in key_value_pairs:
        parsed_value = parse_json_value(value_str)
        old_value = config.get(key, "<not set>")
        print(f"  Setting '{key}': {old_value!r} -> {parsed_value!r}")
        config.set(key, parsed_value)

    # Display new configuration
    print("\nNew configuration:")
    print(config.get_json_str())

    # Write to device
    print("\nWriting configuration to device...")
    updated_config = interface.txdevice.write_config(config)

    if updated_config is None:
        print("Error: Failed to write configuration")
        return False

    print("\nConfiguration written successfully!")
    print(f"  New sequence: {updated_config.header.seq}")
    print(f"  New CRC: 0x{updated_config.header.crc:04X}")

    return True


def remove_keys_action(interface, keys: list) -> bool:
    """Remove one or more keys from the configuration."""
    print("Reading current configuration...")
    config = interface.txdevice.read_config()

    if config is None:
        print("Error: Failed to read configuration")
        return False

    print(f"\nOriginal sequence: {config.header.seq}")

    # Track and display removed keys
    for key in keys:
        old_value = config.get(key, None)
        if old_value is not None:
            print(f"  Removing '{key}': {old_value!r}")
            del config.json_data[key]
        else:
            print(f"  Key '{key}' not found (already removed or never existed)")

    # Display new configuration
    print("\nNew configuration:")
    print(config.get_json_str())

    if len(keys) == 0:
        print("\nNo keys specified. Nothing to remove.")
        return True

    # Write to device
    print("\nWriting configuration to device...")
    updated_config = interface.txdevice.write_config(config)

    if updated_config is None:
        print("Error: Failed to write configuration")
        return False

    print("\nConfiguration written successfully!")
    print(f"  New sequence: {updated_config.header.seq}")
    print(f"  New CRC: 0x{updated_config.header.crc:04X}")

    return True


def write_file_action(interface, json_file: str) -> bool:
    """Write configuration from a JSON file to the device."""
    try:
        with open(json_file, 'r') as f:
            json_str = f.read()
    except IOError as e:
        print(f"Error reading file {json_file}: {e}")
        return False

    print(f"Reading configuration from: {json_file}")
    print("JSON content:")
    print(json_str)

    # Parse to verify it's valid JSON
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"\nError: Invalid JSON in file: {e}")
        return False

    print("\nWriting configuration to device...")
    config = interface.txdevice.read_config()

    if config is None:
        print("Error: Failed to read current configuration")
        return False

    # Update with new values while preserving header
    old_seq = config.header.seq
    config.json_data = data
    # Keep the same sequence number or increment it
    # The device will assign its own sequence on write

    updated_config = interface.txdevice.write_config(config)

    if updated_config is None:
        print("Error: Failed to write configuration")
        return False

    print("\nConfiguration written successfully!")
    print(f"  New sequence: {updated_config.header.seq}")
    print(f"  New CRC: 0x{updated_config.header.crc:04X}")

    return True


def main():
    parser = argparse.ArgumentParser(
        description='Read and modify user configuration on the Lifu device.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Read and display current configuration
  python scripts/user_config.py --read

  # Get a specific value
  python scripts/user_config.py --get device_name

  # Set values (types auto-detected: int, float, bool, string)
  python scripts/user_config.py --set device_name "My Device" --set enabled true --set count 42

  # Remove keys from configuration
  python scripts/user_config.py --remove old_key --remove another_key

  # Write JSON file to device
  python scripts/user_config.py --write-file my_config.json

  # Read and save to file
  python scripts/user_config.py --read --output backup.json
"""
    )

    parser.add_argument('--read', action='store_true',
                        help='Read and display current configuration')
    parser.add_argument('--get', metavar='KEY',
                        help='Get a specific value from the configuration')
    parser.add_argument('--set', nargs=2, metavar=('KEY', 'VALUE'), action='append',
                        help='Set a configuration value (type auto-detected)')
    parser.add_argument('--remove', nargs='+', metavar='KEY', action='append',
                        help='Remove one or more keys from the configuration')
    parser.add_argument('--write-file', metavar='FILE',
                        help='Write configuration from a JSON file to the device')
    parser.add_argument('--output', metavar='FILE',
                        help='Save read configuration to a JSON file')

    args = parser.parse_args()

    # Check if at least one action is specified
    if not any([args.read, args.get, args.set, args.remove, args.write_file]):
        parser.print_help()
        return 1

    # Acquire interface + connection state        
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
        return 1

    # Execute requested actions
    success = True

    if args.read:
        success = read_config_action(interface, args.output) and success

    if args.get:
        success = get_value_action(interface, args.get) and success

    if args.set:
        success = set_values_action(interface, args.set) and success

    if args.remove:
        # Flatten nested lists from action='append'
        all_keys = [key for sublist in args.remove for key in sublist]
        success = remove_keys_action(interface, all_keys) and success

    if args.write_file:
        success = write_file_action(interface, args.write_file) and success

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
