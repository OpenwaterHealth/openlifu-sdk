"""Transmitter TX7332 block write/read-back test.

Writes a block of randomized register values, reads them back,
and verifies every value matches.

Register map:
    0x000-0x01F  Global config/status — DO NOT write random data here!
    0x020-0x11F  Delay profile data   — safe for read/write testing
    0x120-0x19F  Pulse pattern data   — safe for read/write testing

Usage:
    python transmitter_block_test.py [--chip 0] [--address 0x0020] [--count 16]
"""
import argparse
import random
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openlifu_sdk.io import LIFUInterface


def auto_int(x: str) -> int:
    return int(x, 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="TX7332 block write/read-back test")
    parser.add_argument("--chip", type=int, default=0,
                        help="TX7332 chip index (default: 0)")
    parser.add_argument("--address", type=auto_int, default=0x0020,
                        help="Starting register address (default: 0x0020)")
    parser.add_argument("--count", type=int, default=16,
                        help="Number of registers to test (default: 16)")
    parser.add_argument("--read-only", action="store_true", default=False,
                        help="Only read the block (skip write)")
    args = parser.parse_args()

    # Warn if writing to config/status registers
    if not args.read_only and args.address < 0x0020:
        print("WARNING: Registers 0x000-0x01F are global config/status.")
        print("         Writing random data here will break chip operation!")
        resp = input("Continue anyway? [y/N] ").strip().lower()
        if resp != "y":
            print("Aborted.")
            return 0

    iface = LIFUInterface()
    if not iface.transmitter.connect():
        print("ERROR: No transmitter found.")
        return 1

    tx = iface.transmitter
    print(f"Connected to transmitter on {tx.uart._serial.port}\n")

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
    count = args.count

    if args.read_only:
        print(f"\nRead-only: chip={chip}, address=0x{addr:04X}, count={count}")
        values = tx.read_block(chip, addr, count)
        if values is None:
            print("  ERROR: read_block returned None")
            tx.close()
            return 1
        for i, v in enumerate(values):
            print(f"  [0x{addr + i:04X}] = 0x{v:08X}")
        tx.close()
        return 0

    # Save original values
    print(f"\nTest: chip={chip}, address=0x{addr:04X}, count={count}")
    print("Reading original values...")
    originals = tx.read_block(chip, addr, count)
    if originals is None:
        print("  WARNING: Could not read original values")

    # Generate random test data
    test_values = [random.randint(0, 0xFFFFFFFF) for _ in range(count)]
    print("\nTest values:")
    for i, v in enumerate(test_values):
        print(f"  [0x{addr + i:04X}] = 0x{v:08X}")

    # Write block
    print("\nWriting block...")
    if not tx.write_block(chip, addr, test_values):
        print("  ERROR: write_block failed")
        tx.close()
        return 1
    print("  Write OK")

    # Read back
    print("Reading back...")
    readback = tx.read_block(chip, addr, count)
    if readback is None:
        print("  ERROR: read_block returned None")
        tx.close()
        return 1

    # Verify
    passed = 0
    failed = 0
    print("\nVerification:")
    for i in range(count):
        exp = test_values[i]
        got = readback[i]
        if exp == got:
            print(f"  [0x{addr + i:04X}]  0x{got:08X}  [OK]")
            passed += 1
        else:
            print(f"  [0x{addr + i:04X}]  expected=0x{exp:08X}  got=0x{got:08X}  [FAIL]")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {count}")
    print(f"{'='*50}")

    tx.close()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
