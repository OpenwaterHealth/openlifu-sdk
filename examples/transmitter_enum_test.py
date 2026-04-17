"""Transmitter module enumeration test.

Connects to the transmitter and queries the number of connected modules.

Usage:
    python transmitter_enum_test.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openlifu_sdk import LIFUInterface


def main() -> int:
    iface = LIFUInterface()
    if not iface.transmitter.connect():
        print("ERROR: No transmitter found.")
        return 1

    tx = iface.transmitter
    print(f"Connected to transmitter on {tx.uart._serial.port}\n")

    count = tx.get_module_count()
    print(f"Module count: {count}")

    tx7332_count = tx.enum_tx7332_devices()
    print(f"TX7332 device count: {tx7332_count}")

    for i in range(count):
        hw_id = tx.get_hardware_id(module=i)
        fw_ver = tx.get_version(module=i)
        temp = tx.get_temperature(module=i)
        ambient = tx.get_ambient(module=i)
        print(f"  Module {i}: hw_id={hw_id}, fw_ver={fw_ver}, temp={temp} °C, ambient={ambient} °C")

    tx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
