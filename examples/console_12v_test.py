"""12V rail test script.

Turn the 12V rail on/off or query its status.

Usage:
    python console_12v_test.py          # Print current status
    python console_12v_test.py on       # Turn 12V on
    python console_12v_test.py off      # Turn 12V off
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openlifu_sdk.io import LIFUInterface


def main():
    parser = argparse.ArgumentParser(description="LIFU 12V rail test")
    parser.add_argument(
        "action",
        nargs="?",
        default=None,
        choices=["on", "off"],
        help="on / off. Omit to query status.",
    )
    args = parser.parse_args()

    # --- Connect ----------------------------------------------------------
    iface = LIFUInterface()
    if not iface.console.connect():
        print("Console not found – exiting.")
        sys.exit(1)

    print(f"Console connected (FW {iface.console.get_version()})")

    if args.action == "on":
        print("\nTurning 12V ON ...")
        if iface.console.turn_12v_on():
            print("12V is ON.")
        else:
            print("Failed to turn 12V on.")
    elif args.action == "off":
        print("\nTurning 12V OFF ...")
        if iface.console.turn_12v_off():
            print("12V is OFF.")
        else:
            print("Failed to turn 12V off.")

    # Always print current status
    status = iface.console.get_12v_status()
    print(f"\n12V status: {'ON' if status else 'OFF'}")

    iface.console.close()


if __name__ == "__main__":
    main()
