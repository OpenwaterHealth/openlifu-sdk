"""Fan test script.

Sets fan speed and continuously displays temperature readings until
the user presses Enter to stop.

Usage:
    python console_fan_test.py <speed> [--fan <id>]

    speed   Fan speed 0–100 percent.
    --fan   Fan ID: 0 = bottom (default), 1 = top.
"""
import argparse
import sys
import os
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openlifu_sdk import LIFUInterface


def main():
    parser = argparse.ArgumentParser(description="LIFU fan test")
    parser.add_argument(
        "speed",
        type=int,
        help="Fan speed 0–100 percent",
    )
    parser.add_argument(
        "--fan",
        type=int,
        default=0,
        choices=[0, 1],
        help="Fan ID: 0 = bottom (default), 1 = top",
    )
    args = parser.parse_args()

    if not 0 <= args.speed <= 100:
        print("Error: speed must be between 0 and 100")
        sys.exit(1)

    fan_name = "bottom" if args.fan == 0 else "top"

    # --- Connect ----------------------------------------------------------
    iface = LIFUInterface()
    if not iface.console.connect():
        print("Console not found – exiting.")
        sys.exit(1)

    print(f"Console connected (FW {iface.console.get_version()})")

    # --- Set fan speed ----------------------------------------------------
    print(f"\nSetting {fan_name} fan (id={args.fan}) to {args.speed}% ...")
    result = iface.console.set_fan(fan_id=args.fan, speed=args.speed)
    if result < 0:
        print("Failed to set fan speed.")
        iface.console.close()
        sys.exit(1)

    readback = iface.console.get_fan(fan_id=args.fan)
    print(f"Fan set to {args.speed}%, readback: {readback}%")
    print("\nMonitoring temperatures. Press Enter to stop.\n")

    # --- Poll temps until user presses Enter ------------------------------
    stop_event = threading.Event()

    def wait_for_enter():
        input()
        stop_event.set()

    input_thread = threading.Thread(target=wait_for_enter, daemon=True)
    input_thread.start()

    try:
        while not stop_event.is_set():
            try:
                t1 = iface.console.get_temperature1()
                t2 = iface.console.get_temperature2()
                fan_speed = iface.console.get_fan(fan_id=args.fan)
                print(f"  Temp1: {t1:6.2f}°C | Temp2: {t2:6.2f}°C | Fan: {fan_speed}%")
            except Exception as e:
                print(f"  [read error: {e}]")
            stop_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        pass

    # --- Shut down --------------------------------------------------------
    print(f"\nSetting {fan_name} fan to 0% ...")
    iface.console.set_fan(fan_id=args.fan, speed=0)
    print("Fan stopped.")

    iface.console.close()
    print("Done.")


if __name__ == "__main__":
    main()
