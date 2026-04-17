"""Synchronous stress-test script – no background threads.

Connects to available devices, runs a suite of commands, and repeats
for the requested number of iterations.

Usage:
    python sync_script.py [loop_count]

    loop_count  Number of test iterations (default: 1).
"""
import argparse
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openlifu_sdk import LIFUInterface


def run_transmitter_tests(tx) -> tuple[int, int]:
    """Run all transmitter tests. Returns (passed, failed) counts."""
    passed = 0
    failed = 0

    # PING
    if tx.ping():
        print("    PING       -> OK")
        passed += 1
    else:
        print("    PING       -> FAILED")
        failed += 1

    # VERSION
    version = tx.get_version()
    if version:
        print(f"    VERSION    -> {version}")
        passed += 1
    else:
        print("    VERSION    -> FAILED")
        failed += 1

    # ECHO
    echo, length = tx.echo(echo_data=b"Hello LIFU!")
    if length > 0:
        print(f"    ECHO       -> {echo.decode('utf-8')} ({length} bytes)")
        passed += 1
    else:
        print("    ECHO       -> FAILED")
        failed += 1

    # HARDWARE ID
    hw_id = tx.get_hardware_id()
    if hw_id:
        print(f"    HWID       -> {hw_id}")
        passed += 1
    else:
        print("    HWID       -> FAILED")
        failed += 1

    # TEMPERATURE
    temp = tx.get_temperature()
    if temp is not None:
        print(f"    TEMP       -> {temp}")
        passed += 1
    else:
        print("    TEMP       -> FAILED")
        failed += 1

    # AMBIENT
    amb_temp = tx.get_ambient()
    if amb_temp is not None:
        print(f"    AMBIENT    -> {amb_temp}")
        passed += 1
    else:
        print("    AMBIENT    -> FAILED")
        failed += 1

    return passed, failed


def run_console_tests(con) -> tuple[int, int]:
    """Run all console tests. Returns (passed, failed) counts."""
    passed = 0
    failed = 0

    # PING
    if con.ping():
        print("    PING       -> OK")
        passed += 1
    else:
        print("    PING       -> FAILED")
        failed += 1

    # VERSION
    version = con.get_version()
    if version:
        print(f"    VERSION    -> {version}")
        passed += 1
    else:
        print("    VERSION    -> FAILED")
        failed += 1

    # ECHO
    echo, length = con.echo(echo_data=b"Hello LIFU!")
    if length > 0:
        print(f"    ECHO       -> {echo.decode('utf-8')} ({length} bytes)")
        passed += 1
    else:
        print("    ECHO       -> FAILED")
        failed += 1

    # HARDWARE ID
    hw_id = con.get_hardware_id()
    if hw_id:
        print(f"    HWID       -> {hw_id}")
        passed += 1
    else:
        print("    HWID       -> FAILED")
        failed += 1

    # TEMPERATURE 1
    temp1 = con.get_temperature1()
    if temp1 is not None:
        print(f"    TEMP1      -> {temp1}")
        passed += 1
    else:
        print("    TEMP1      -> FAILED")
        failed += 1

    # TEMPERATURE 2
    temp2 = con.get_temperature2()
    if temp2 is not None:
        print(f"    TEMP2      -> {temp2}")
        passed += 1
    else:
        print("    TEMP2      -> FAILED")
        failed += 1

    return passed, failed


def main():
    parser = argparse.ArgumentParser(description="LIFU synchronous stress test")
    parser.add_argument(
        "loop_count",
        nargs="?",
        type=int,
        default=1,
        help="Number of test iterations (default: 1)",
    )
    args = parser.parse_args()
    loop_count = args.loop_count

    # --- Create interface and discover devices ----------------------------
    iface = LIFUInterface()

    tx_present = iface.transmitter.connect()
    con_present = iface.console.connect()

    if not tx_present and not con_present:
        print("No devices found – exiting.")
        sys.exit(1)

    print("Devices detected:")
    print(f"  Transmitter : {'YES' if tx_present else 'NO'}")
    print(f"  Console     : {'YES' if con_present else 'NO'}")
    print(f"  Iterations  : {loop_count}")
    print()

    total_passed = 0
    total_failed = 0
    start_time = time.time()

    try:
        for i in range(loop_count):
            print(f"=== Iteration {i + 1}/{loop_count} ===")

            # --- Transmitter tests ----------------------------------------
            if tx_present:
                print("  [Transmitter]")
                p, f = run_transmitter_tests(iface.transmitter)
                total_passed += p
                total_failed += f

            # --- Console tests --------------------------------------------
            if con_present:
                print("  [Console]")
                p, f = run_console_tests(iface.console)
                total_passed += p
                total_failed += f

            print()

    except KeyboardInterrupt:
        print("\nInterrupted by user.")

    # --- Summary ----------------------------------------------------------
    elapsed = time.time() - start_time
    total_tests = total_passed + total_failed
    print("=" * 40)
    print(f"Results: {total_passed}/{total_tests} passed, {total_failed} failed")
    print(f"Elapsed: {elapsed:.2f}s")

    # --- Close connections ------------------------------------------------
    if tx_present:
        iface.transmitter.close()
    if con_present:
        iface.console.close()

    sys.exit(1 if total_failed > 0 else 0)


if __name__ == "__main__":
    main()
