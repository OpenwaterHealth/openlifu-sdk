"""RGB LED test script.

Cycles through LED states or sets a specific color.

Usage:
    python console_rgb_test.py              # Cycle through all states
    python console_rgb_test.py red          # Set to red
    python console_rgb_test.py off          # Turn off
    python console_rgb_test.py 2            # Set by number (0=OFF, 1=RED, 2=BLUE, 3=GREEN)
"""
import argparse
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openlifu_sdk.io import LIFUInterface

STATE_NAMES = {0: "OFF", 1: "RED", 2: "BLUE", 3: "GREEN"}
NAME_TO_STATE = {"off": 0, "red": 1, "blue": 2, "green": 3}


def main():
    parser = argparse.ArgumentParser(description="LIFU RGB LED test")
    parser.add_argument(
        "color",
        nargs="?",
        default=None,
        help="Color: off, red, blue, green (or 0-3). Omit to cycle all.",
    )
    args = parser.parse_args()

    # --- Connect ----------------------------------------------------------
    iface = LIFUInterface()
    if not iface.console.connect():
        print("Console not found – exiting.")
        sys.exit(1)

    print(f"Console connected (FW {iface.console.get_version()})")

    current = iface.console.get_rgb()
    print(f"Current state: {STATE_NAMES.get(current, '?')} ({current})")

    if args.color is not None:
        # --- Set specific color -------------------------------------------
        if args.color.lower() in NAME_TO_STATE:
            state = NAME_TO_STATE[args.color.lower()]
        elif args.color.isdigit() and 0 <= int(args.color) <= 3:
            state = int(args.color)
        else:
            print(f"Unknown color '{args.color}'. Use: off, red, blue, green, or 0-3.")
            iface.console.close()
            sys.exit(1)

        print(f"\nSetting LED to {STATE_NAMES[state]} ...")
        if iface.console.set_rgb(state):
            readback = iface.console.get_rgb()
            print(f"LED set. Readback: {STATE_NAMES.get(readback, '?')} ({readback})")
        else:
            print("Failed to set LED.")
    else:
        # --- Cycle through all states -------------------------------------
        print("\nCycling: OFF -> RED -> BLUE -> GREEN -> OFF  (2s each)")
        for state in [0, 1, 2, 3, 0]:
            print(f"  {STATE_NAMES[state]} ...", end=" ", flush=True)
            if iface.console.set_rgb(state):
                readback = iface.console.get_rgb()
                print(f"OK (readback: {STATE_NAMES.get(readback, '?')})")
            else:
                print("FAILED")
            time.sleep(2)
        print("Cycle complete.")

    iface.console.close()
    print("Done.")


if __name__ == "__main__":
    main()
