"""Console stress test – exercises basic commands with varying echo sizes.

Sends echo payloads from 16 to 240 bytes (step 16) and verifies the
returned data matches what was sent. Also runs ping, version, hwid,
and temperature commands each iteration.

Usage:
    python console_stress_test.py [loop_count]

    loop_count  Number of full test cycles (default: 1).
"""
import argparse
import random
import string
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openlifu_sdk.io import LIFUInterface

ECHO_SIZES = list(range(16, 241, 16))  # 16, 32, 48, ... 240


def random_bytes(length: int) -> bytes:
    """Generate *length* random printable ASCII bytes."""
    return "".join(random.choices(string.ascii_letters + string.digits, k=length)).encode()


def run_stress_iteration(con, iteration: int) -> tuple[int, int]:
    """Run one full stress-test iteration. Returns (passed, failed)."""
    passed = 0
    failed = 0

    # PING
    if con.ping():
        print(f"  [{iteration}] PING       -> OK")
        passed += 1
    else:
        print(f"  [{iteration}] PING       -> FAILED")
        failed += 1

    # VERSION
    version = con.get_version()
    if version:
        print(f"  [{iteration}] VERSION    -> {version}")
        passed += 1
    else:
        print(f"  [{iteration}] VERSION    -> FAILED")
        failed += 1

    # HARDWARE ID
    hw_id = con.get_hardware_id()
    if hw_id:
        print(f"  [{iteration}] HWID       -> {hw_id}")
        passed += 1
    else:
        print(f"  [{iteration}] HWID       -> FAILED")
        failed += 1

    # TEMPERATURE 1
    temp1 = con.get_temperature1()
    if temp1 is not None:
        print(f"  [{iteration}] TEMP1      -> {temp1}")
        passed += 1
    else:
        print(f"  [{iteration}] TEMP1      -> FAILED")
        failed += 1

    # TEMPERATURE 2
    temp2 = con.get_temperature2()
    if temp2 is not None:
        print(f"  [{iteration}] TEMP2      -> {temp2}")
        passed += 1
    else:
        print(f"  [{iteration}] TEMP2      -> FAILED")
        failed += 1

    # ECHO – sweep from 16 to 240 bytes
    for size in ECHO_SIZES:
        tx_data = random_bytes(size)
        try:
            rx_data, rx_len = con.echo(echo_data=tx_data)
        except Exception as e:
            print(f"  [{iteration}] ECHO {size:>3}B  -> EXCEPTION: {e}")
            failed += 1
            continue

        if rx_data == tx_data and rx_len == size:
            print(f"  [{iteration}] ECHO {size:>3}B  -> OK")
            passed += 1
        else:
            mismatch = ""
            if rx_len != size:
                mismatch += f" len={rx_len} expected={size}"
            if rx_data != tx_data:
                mismatch += " DATA MISMATCH"
            print(f"  [{iteration}] ECHO {size:>3}B  -> FAILED{mismatch}")
            failed += 1

    return passed, failed


def main():
    parser = argparse.ArgumentParser(description="LIFU console stress test")
    parser.add_argument(
        "loop_count",
        nargs="?",
        type=int,
        default=1,
        help="Number of test cycles (default: 1)",
    )
    args = parser.parse_args()
    loop_count = args.loop_count

    # --- Connect ----------------------------------------------------------
    iface = LIFUInterface()
    if not iface.console.connect():
        print("Console not found – exiting.")
        sys.exit(1)

    print(f"Console connected (FW {iface.console.get_version()})")
    print(f"Echo sizes: {ECHO_SIZES[0]}–{ECHO_SIZES[-1]} bytes "
          f"({len(ECHO_SIZES)} steps)")
    print(f"Iterations: {loop_count}")
    print()

    total_passed = 0
    total_failed = 0
    start_time = time.time()

    try:
        for i in range(1, loop_count + 1):
            print(f"=== Iteration {i}/{loop_count} ===")
            p, f = run_stress_iteration(iface.console, i)
            total_passed += p
            total_failed += f
            print()
    except KeyboardInterrupt:
        print("\nInterrupted by user.")

    # --- Summary ----------------------------------------------------------
    elapsed = time.time() - start_time
    total_tests = total_passed + total_failed
    print("=" * 50)
    print(f"Results: {total_passed}/{total_tests} passed, "
          f"{total_failed} failed  ({elapsed:.1f}s)")
    print("=" * 50)

    iface.console.close()
    sys.exit(1 if total_failed > 0 else 0)


if __name__ == "__main__":
    main()
