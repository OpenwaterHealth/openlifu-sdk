"""Transmitter TX7332 block write/read-back stress test.

Enumerates all TX7332 chips, then for each iteration picks a random chip,
random start address within the delay data range (0x020-0x11F), and a
random block size (4-16 registers).  Writes random data, reads it back,
and verifies every value.

Usage:
    python transmitter_stress_test.py [--loops 1]
"""
import argparse
import random
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openlifu_sdk import LIFUInterface

# Safe delay-data register range
DELAY_DATA_START = 0x0020
DELAY_DATA_END = 0x011F  # inclusive

MIN_BLOCK = 4
MAX_BLOCK = 16


def main() -> int:
    parser = argparse.ArgumentParser(
        description="TX7332 block write/read-back stress test")
    parser.add_argument("--loops", type=int, default=1,
                        help="Number of iterations (default: 1)")
    args = parser.parse_args()

    iface = LIFUInterface()
    if not iface.transmitter.connect():
        print("ERROR: No transmitter found.")
        return 1

    tx = iface.transmitter
    print(f"Connected to transmitter on {tx.uart._serial.port}")

    num_chips = tx.enum_tx7332_devices()
    print(f"TX7332 chips detected: {num_chips}")
    if num_chips == 0:
        print("ERROR: No TX7332 chips found.")
        tx.close()
        return 1

    loops = args.loops
    total_pass = 0
    total_fail = 0
    t_start = time.perf_counter()

    print(f"\nRunning {loops} iteration{'s' if loops != 1 else ''}...\n")

    for iteration in range(1, loops + 1):
        iter_ok = True
        for chip in range(num_chips):
            count = random.randint(MIN_BLOCK, MAX_BLOCK)
            max_start = DELAY_DATA_END - count + 1
            addr = random.randint(DELAY_DATA_START, max_start)
            test_values = [random.randint(0, 0xFFFFFFFF) for _ in range(count)]

            tag = f"[{iteration}/{loops}] chip={chip} addr=0x{addr:04X} count={count}"

            print(f"  {tag}")

            # Write
            print("    Write:")
            for i, v in enumerate(test_values):
                print(f"      [0x{addr + i:04X}] = 0x{v:08X}")
            ok = tx.write_block(chip, addr, test_values)
            if not ok:
                print("    WRITE FAILED")
                total_fail += count
                iter_ok = False
                continue

            # Read back
            readback = tx.read_block(chip, addr, count)
            if readback is None:
                print("    READ FAILED")
                total_fail += count
                iter_ok = False
                continue

            print("    Read:")
            for i, v in enumerate(readback):
                print(f"      [0x{addr + i:04X}] = 0x{v:08X}")

            # Verify
            chip_fail = 0
            for i in range(count):
                if test_values[i] != readback[i]:
                    chip_fail += 1

            if chip_fail:
                total_fail += chip_fail
                total_pass += count - chip_fail
                iter_ok = False
                print(f"    FAIL ({chip_fail}/{count} mismatched)")
                for i in range(count):
                    if test_values[i] != readback[i]:
                        print(f"      [0x{addr + i:04X}]  "
                              f"exp=0x{test_values[i]:08X}  "
                              f"got=0x{readback[i]:08X}")
            else:
                total_pass += count
                print(f"    OK ({count}/{count})")

        status = "OK" if iter_ok else "FAIL"
        print(f"  [{iteration}/{loops}] all chips  {status}")

    elapsed = time.perf_counter() - t_start
    total_regs = total_pass + total_fail

    print(f"\n{'=' * 55}")
    print(f"Stress test complete: {loops} iterations x {num_chips} chips, "
          f"{total_regs} registers tested")
    print(f"  Passed : {total_pass}")
    print(f"  Failed : {total_fail}")
    print(f"  Time   : {elapsed:.2f}s")
    print(f"{'=' * 55}")

    tx.close()
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
