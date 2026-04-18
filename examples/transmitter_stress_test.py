"""txdevice TX7332 block write/read-back stress test.

Enumerates all TX7332 chips, then for each iteration picks a random chip,
random start address within the delay data range (0x020-0x11F), and a
random block size (4-16 registers).  Writes random data, reads it back,
and verifies every value.

Usage:
    python transmitter_stress_test.py [--loops 1] [--seed 1234]
"""
import argparse
import logging
import random
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openlifu_sdk.io.LIFUInterface import LIFUInterface

# Safe delay-data register range
DELAY_DATA_START = 0x0020
DELAY_DATA_END = 0x011F  # inclusive

MIN_BLOCK = 4
MAX_BLOCK = 16


def auto_int(value: str) -> int:
    return int(value, 0)


def build_rng(seed: int | None) -> tuple[random.Random, int]:
    if seed is None:
        seed = time.time_ns() & 0xFFFFFFFF
    return random.Random(seed), seed


def configure_logging(level_name: str):
    level = getattr(logging, level_name.upper(), logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


def dump_registers(title: str, start_addr: int, values: list[int]):
    print(f"    {title}:")
    for offset, value in enumerate(values):
        print(f"      [0x{start_addr + offset:04X}] = 0x{value:08X}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="TX7332 block write/read-back stress test")
    parser.add_argument("--loops", type=int, default=1,
                        help="Number of iterations (default: 1)")
    parser.add_argument("--seed", type=int,
                        help="Random seed for reproducible test cases")
    parser.add_argument("--chip", type=int,
                        help="Restrict the test to one TX7332 chip index")
    parser.add_argument("--address", type=auto_int,
                        help="Fixed starting register address (hex or decimal)")
    parser.add_argument("--count", type=int,
                        help="Fixed block size (default: randomized 4-16)")
    parser.add_argument("--delay-ms", type=float, default=0.0,
                        help="Delay between write and read in milliseconds")
    parser.add_argument("--timeout", type=float,
                        help="Override interface command timeout in seconds")
    parser.add_argument("--verbose", action="store_true",
                        help="Print register values for every successful transaction")
    parser.add_argument("--log-level", default="WARNING",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                        help="Python logging level for SDK diagnostics")
    args = parser.parse_args()

    configure_logging(args.log_level)

    if (args.address is None) != (args.count is None):
        parser.error("--address and --count must be provided together")
    if args.count is not None and not 1 <= args.count <= MAX_BLOCK:
        parser.error(f"--count must be between 1 and {MAX_BLOCK}")
    if args.delay_ms < 0:
        parser.error("--delay-ms must be >= 0")

    rng, seed = build_rng(args.seed)

    iface = LIFUInterface(timeout=args.timeout) if args.timeout is not None else LIFUInterface()

    tx_connected, hv_connected = iface.is_device_connected()

    if not tx_connected and not hv_connected:
        print("✅ LIFU Console not connected.")
        sys.exit(1)

    if not tx_connected:
        print("TX device not connected. Attempting to turn on 12V...")
        iface.hvcontroller.turn_12v_on()

        # Give time for the TX device to power up and enumerate over USB
        time.sleep(2)

        # Cleanup and recreate interface to reinitialize USB devices
        iface.stop_monitoring()
        del iface
        time.sleep(5)  # Short delay before recreating

        print("Reinitializing LIFU interface after powering 12V...")
        iface = LIFUInterface()

        # Re-check connection
        tx_connected, hv_connected = iface.is_device_connected()

    if tx_connected:
        print("✅ LIFU Device TX connected.")
    else:
        print("❌ LIFU Device NOT fully connected.")
        print(f"  TX Connected: {tx_connected}")
        print(f"  HV Connected: {hv_connected}")
        return 1
        

    tx = iface.txdevice
    serial_port = tx.uart._serial.port if tx.uart._serial is not None else "<unknown>"
    print(f"Connected to txdevice on {serial_port}")
    print(f"Seed: {seed}")
    print(f"Delay between write/read: {args.delay_ms:.3f} ms")
    print(f"Command timeout: {tx.uart.timeout:.3f} s")

    num_chips = tx.enum_tx7332_devices()
    print(f"TX7332 chips detected: {num_chips}")
    if num_chips == 0:
        print("ERROR: No TX7332 chips found.")
        tx.close()
        return 1

    if args.chip is not None and not 0 <= args.chip < num_chips:
        print(f"ERROR: Chip {args.chip} out of range (0-{num_chips - 1}).")
        tx.close()
        return 1

    chip_ids = [args.chip] if args.chip is not None else list(range(num_chips))

    loops = args.loops
    total_pass = 0
    total_fail = 0
    t_start = time.perf_counter()

    print(f"\nRunning {loops} iteration{'s' if loops != 1 else ''}...\n")

    for iteration in range(1, loops + 1):
        iter_ok = True
        for chip in chip_ids:
            count = args.count if args.count is not None else rng.randint(MIN_BLOCK, MAX_BLOCK)
            max_start = DELAY_DATA_END - count + 1
            if args.address is not None:
                addr = args.address
                if not DELAY_DATA_START <= addr <= max_start:
                    print(f"ERROR: Address 0x{addr:04X} with count {count} exceeds safe delay-data range.")
                    tx.close()
                    return 1
            else:
                addr = rng.randint(DELAY_DATA_START, max_start)
            test_values = [rng.randint(0, 0xFFFFFFFF) for _ in range(count)]

            tag = (
                f"[{iteration}/{loops}] chip={chip} addr=0x{addr:04X} "
                f"count={count} seed={seed}"
            )

            print(f"  {tag}")
            transaction_start = time.perf_counter()

            ok = tx.write_block(chip, addr, test_values)
            if not ok:
                if args.verbose:
                    dump_registers("Write", addr, test_values)
                elapsed_ms = (time.perf_counter() - transaction_start) * 1000.0
                print(f"    WRITE FAILED after {elapsed_ms:.2f} ms")
                total_fail += count
                iter_ok = False
                continue

            if args.delay_ms > 0:
                time.sleep(args.delay_ms / 1000.0)

            readback = tx.read_block(chip, addr, count)
            if readback is None:
                dump_registers("Write", addr, test_values)
                elapsed_ms = (time.perf_counter() - transaction_start) * 1000.0
                print(f"    READ FAILED after {elapsed_ms:.2f} ms")
                total_fail += count
                iter_ok = False
                continue

            # Verify
            chip_fail = 0
            for i in range(count):
                if test_values[i] != readback[i]:
                    chip_fail += 1

            elapsed_ms = (time.perf_counter() - transaction_start) * 1000.0
            if chip_fail:
                total_fail += chip_fail
                total_pass += count - chip_fail
                iter_ok = False
                print(f"    FAIL ({chip_fail}/{count} mismatched) after {elapsed_ms:.2f} ms")
                dump_registers("Write", addr, test_values)
                dump_registers("Read", addr, readback)
                for i in range(count):
                    if test_values[i] != readback[i]:
                        print(f"      [0x{addr + i:04X}]  "
                              f"exp=0x{test_values[i]:08X}  "
                              f"got=0x{readback[i]:08X}")
            else:
                total_pass += count
                print(f"    OK ({count}/{count}) after {elapsed_ms:.2f} ms")
                if args.verbose:
                    dump_registers("Write", addr, test_values)
                    dump_registers("Read", addr, readback)

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
