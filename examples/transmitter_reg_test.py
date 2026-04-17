"""Transmitter TX7332 register write/read-back test.

Writes a value to a register on a TX7332 chip, reads it back,
and verifies the value matches.

Usage:
    python transmitter_reg_test.py [--chip 0] [--address 0x0010] [--value 0x1234]
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openlifu_sdk import LIFUInterface


def auto_int(x: str) -> int:
    """Parse an integer from decimal or hex (0x...) string."""
    return int(x, 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="TX7332 register write/read-back test")
    parser.add_argument("--chip", type=int, default=0,
                        help="TX7332 chip index (default: 0)")
    parser.add_argument("--address", type=auto_int, default=0x0020,
                        help="Register address (default: 0x0020)")
    parser.add_argument("--value", type=auto_int, default=0x1234,
                        help="Value to write (default: 0x1234)")
    parser.add_argument("--read-only", action="store_true", default=False,
                        help="Only read the register (ignore --value)")
    args = parser.parse_args()

    iface = LIFUInterface()
    if not iface.transmitter.connect():
        print("ERROR: No transmitter found.")
        return 1

    tx = iface.transmitter
    print(f"Connected to transmitter on {tx.uart._serial.port}\n")

    # Enumerate TX7332 chips first
    num_chips = tx.enum_tx7332_devices()
    print(f"TX7332 chips detected: {num_chips}")
    if num_chips == 0:
        print("ERROR: No TX7332 chips found.")
        tx.close()
        return 1
    if args.chip >= num_chips:
        print(f"ERROR: Chip {args.chip} out of range (0-{num_chips - 1}).")
        tx.close()
        return 1

    chip = args.chip
    addr = args.address
    value = args.value

    if args.read_only:
        print(f"\nRead-only: chip={chip}, address=0x{addr:04X}")
        readback = tx.read_register(chip, addr)
        if readback is None:
            print("  ERROR: read_register returned None")
            tx.close()
            return 1
        print(f"  Value: 0x{readback:08X}")
        tx.close()
        return 0

    print(f"\nTest: chip={chip}, address=0x{addr:04X}, value=0x{value:08X}")

    # Read original value
    original = tx.read_register(chip, addr)
    if original is not None:
        print(f"  Original value: 0x{original:08X}")
    else:
        print("  WARNING: Could not read original value")

    # Write
    print(f"  Writing 0x{value:08X} ...")
    if not tx.write_register(chip, addr, value):
        print("  ERROR: write_register failed")
        tx.close()
        return 1
    print("  Write OK")

    # Read back
    readback = tx.read_register(chip, addr)
    if readback is None:
        print("  ERROR: read_register returned None")
        tx.close()
        return 1
    print(f"  Read back: 0x{readback:08X}")

    # Verify
    if readback == value:
        print(f"\n  PASS: 0x{readback:08X} == 0x{value:08X}")
        result = 0
    else:
        print(f"\n  FAIL: 0x{readback:08X} != 0x{value:08X}")
        result = 1

    tx.close()
    return result


if __name__ == "__main__":
    sys.exit(main())
