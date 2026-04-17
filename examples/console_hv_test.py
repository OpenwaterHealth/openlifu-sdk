"""HV supply test script.

Sets the HV voltage, waits for user confirmation to enable, then
continuously displays ADC voltage readings until the user presses
Enter to shut down.

Usage:
    python console_hv_test.py <voltage>

    voltage  Target HV voltage in volts (5.0 – 100.0).
"""
import argparse
import sys
import os
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openlifu_sdk.io import LIFUInterface


CHANNEL_LABELS = [
    "CH0", "CH1", "CH2", "CH3",
    "CH4", "CH5", "CH6", "CH7",
]


def print_voltage_table(readings: list[dict]):
    """Print a compact one-line voltage summary."""
    parts = []
    for ch in readings:
        label = CHANNEL_LABELS[ch["channel"]]
        parts.append(f"{label}: {ch['converted_voltage']:7.3f}V")
    print("  " + " | ".join(parts))


def main():
    parser = argparse.ArgumentParser(description="LIFU HV supply test")
    parser.add_argument(
        "voltage",
        type=float,
        help="Target HV voltage in volts (5.0 – 100.0)",
    )
    args = parser.parse_args()

    if not 5.0 <= args.voltage <= 100.0:
        print("Error: voltage must be between 5.0 and 100.0 V")
        sys.exit(1)

    # --- Connect ----------------------------------------------------------
    iface = LIFUInterface()
    if not iface.console.connect():
        print("Console not found – exiting.")
        sys.exit(1)

    print(f"Console connected (FW {iface.console.get_version()})")

    # --- Set voltage ------------------------------------------------------
    print(f"\nSetting HV to {args.voltage:.1f} V ...")
    if not iface.console.set_hv(args.voltage):
        print("Failed to set HV voltage.")
        iface.console.close()
        sys.exit(1)
    print("HV voltage set.")

    # --- Wait for user to enable ------------------------------------------
    input("\nPress Enter to turn HV ON ...")

    if not iface.console.turn_hv_on():
        print("Failed to turn HV on.")
        iface.console.close()
        sys.exit(1)
    print("HV is ON.  Polling ADC readings every second.")
    print("Press Enter to shut off HV and exit.\n")

    # --- Poll voltages until user presses Enter ---------------------------
    stop_event = threading.Event()

    def wait_for_enter():
        input()
        stop_event.set()

    input_thread = threading.Thread(target=wait_for_enter, daemon=True)
    input_thread.start()

    try:
        while not stop_event.is_set():
            try:
                readings = iface.console.get_voltage_monitor()
                print_voltage_table(readings)
            except Exception as e:
                print(f"  [VMON error: {e}]")
            stop_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        pass

    # --- Shut down --------------------------------------------------------
    print("\nTurning HV OFF ...")
    iface.console.turn_hv_off()
    print("HV is OFF.")

    iface.console.close()
    print("Done.")


if __name__ == "__main__":
    main()
