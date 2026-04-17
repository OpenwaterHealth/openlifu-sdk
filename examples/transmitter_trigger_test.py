"""Transmitter trigger set/get round-trip test.

Connects to the transmitter, sets a trigger configuration, reads it back,
and verifies every field matches the values that were sent.

Usage:
    python transmitter_trigger_test.py [--pulse-interval 0.001]
                                       [--pulse-count 10]
                                       [--pulse-width 100]
                                       [--train-interval 0.0]
                                       [--train-count 1]
                                       [--trigger-mode 0]
                                       [--profile-index 0]
                                       [--profile-increment 0]
"""
import argparse
import math
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openlifu_sdk.io import LIFUInterface

# Tolerance for floating-point comparisons
REL_TOL = 1e-3
ABS_TOL = 1e-6

# JSON keys used by the firmware
KEY_FREQ = "TriggerFrequencyHz"
KEY_PULSE_COUNT = "TriggerPulseCount"
KEY_PULSE_WIDTH = "TriggerPulseWidthUsec"
KEY_TRAIN_INTERVAL = "TriggerPulseTrainInterval"
KEY_TRAIN_COUNT = "TriggerPulseTrainCount"
KEY_MODE = "TriggerMode"
KEY_PROFILE_IDX = "ProfileIndex"
KEY_PROFILE_INC = "ProfileIncrement"


def build_trigger_json(args) -> dict:
    """Build the trigger JSON dict from CLI arguments."""
    freq_hz = 1.0 / args.pulse_interval if args.pulse_interval > 0 else 0.0
    return {
        KEY_FREQ: freq_hz,
        KEY_PULSE_COUNT: args.pulse_count,
        KEY_PULSE_WIDTH: args.pulse_width,
        KEY_TRAIN_INTERVAL: args.train_interval * 1_000_000,  # seconds -> µs
        KEY_TRAIN_COUNT: args.train_count,
        KEY_MODE: args.trigger_mode,
        KEY_PROFILE_IDX: args.profile_index,
        KEY_PROFILE_INC: args.profile_increment,
    }


def check_field(label: str, expected, actual, is_float: bool = False) -> bool:
    """Compare a single field, return True if it matches."""
    if is_float:
        ok = math.isclose(expected, actual, rel_tol=REL_TOL, abs_tol=ABS_TOL)
    else:
        ok = expected == actual

    status = "OK" if ok else "FAIL"
    print(f"  {label:<30s}  expected={expected:<14}  got={actual:<14}  [{status}]")
    return ok


def verify_trigger(sent: dict, received: dict) -> tuple[int, int]:
    """Compare sent vs received trigger dicts. Returns (passed, failed)."""
    passed = 0
    failed = 0

    checks = [
        (KEY_FREQ,           True),
        (KEY_PULSE_COUNT,    False),
        (KEY_PULSE_WIDTH,    False),
        (KEY_TRAIN_INTERVAL, True),
        (KEY_TRAIN_COUNT,    False),
        (KEY_MODE,           False),
        (KEY_PROFILE_IDX,    False),
        (KEY_PROFILE_INC,    False),
    ]

    for key, is_float in checks:
        exp = sent.get(key)
        act = received.get(key)
        if act is None:
            print(f"  {key:<30s}  MISSING in response  [FAIL]")
            failed += 1
            continue
        if check_field(key, exp, act, is_float=is_float):
            passed += 1
        else:
            failed += 1

    return passed, failed


def main() -> int:
    parser = argparse.ArgumentParser(description="Transmitter trigger set/get test")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--on", action="store_true", default=False,
                        help="Turn the trigger on (set config first if params given)")
    action.add_argument("--off", action="store_true", default=False,
                        help="Turn the trigger off (then set config if params given)")
    parser.add_argument("--pulse-interval", type=float, default=None,
                        help="Pulse interval in seconds (default: 0.001 = 1 kHz)")
    parser.add_argument("--pulse-count", type=int, default=None,
                        help="Number of pulses per train (default: 10)")
    parser.add_argument("--pulse-width", type=int, default=None,
                        help="Pulse width in microseconds (default: 100)")
    parser.add_argument("--train-interval", type=float, default=None,
                        help="Pulse train interval in seconds (default: 0.0)")
    parser.add_argument("--train-count", type=int, default=None,
                        help="Number of pulse trains (default: 1)")
    parser.add_argument("--trigger-mode", type=int, default=None,
                        help="Trigger mode: 0=sequence, 1=continuous, 2=single (default: 0)")
    parser.add_argument("--profile-index", type=int, default=None,
                        help="Profile index (default: 0)")
    parser.add_argument("--profile-increment", type=int, default=None,
                        help="Profile increment flag (default: 0)")
    args = parser.parse_args()

    # Determine if any trigger config params were explicitly provided
    config_fields = ["pulse_interval", "pulse_count", "pulse_width",
                     "train_interval", "train_count", "trigger_mode",
                     "profile_index", "profile_increment"]
    has_config = any(getattr(args, f) is not None for f in config_fields)

    # Apply defaults for any unset config params (used when has_config is True)
    DEFAULTS = dict(pulse_interval=0.001, pulse_count=10, pulse_width=100,
                    train_interval=0.0, train_count=1, trigger_mode=0,
                    profile_index=0, profile_increment=0)
    for k, v in DEFAULTS.items():
        if getattr(args, k) is None:
            setattr(args, k, v)

    iface = LIFUInterface()
    if not iface.transmitter.connect():
        print("ERROR: No transmitter found.")
        return 1

    tx = iface.transmitter
    print(f"Connected to transmitter on {tx.uart._serial.port}\n")

    total_passed = 0
    total_failed = 0

    # --off: turn trigger off first, then optionally set config
    if args.off:
        print("Stopping trigger...")
        if tx.stop_trigger():
            print("  -> Trigger stopped OK")
        else:
            print("  -> ERROR: stop_trigger failed")
            tx.close()
            return 1

        if has_config:
            p, f = _set_and_verify(tx, args)
            total_passed += p
            total_failed += f

    # --on: optionally set config, then turn trigger on
    elif args.on:
        if has_config:
            p, f = _set_and_verify(tx, args)
            total_passed += p
            total_failed += f

        print("Starting trigger...")
        if tx.start_trigger():
            print("  -> Trigger started OK")
        else:
            print("  -> ERROR: start_trigger failed")
            tx.close()
            return 1

    # Neither --on nor --off: just set config and verify (original behaviour)
    else:
        if not has_config:
            # Nothing to do – force defaults so we still exercise set/get
            has_config = True
        p, f = _set_and_verify(tx, args)
        total_passed += p
        total_failed += f

    if total_passed or total_failed:
        print(f"\n{'='*60}")
        print(f"Results: {total_passed} passed, {total_failed} failed")
        print(f"{'='*60}")

    tx.close()
    return 0 if total_failed == 0 else 1


def _set_and_verify(tx, args) -> tuple[int, int]:
    """Set trigger config, read it back, and verify. Returns (passed, failed)."""
    trigger_data = build_trigger_json(args)
    print("\nSetting trigger configuration:")
    for k, v in trigger_data.items():
        print(f"  {k}: {v}")

    set_resp = tx.set_trigger(trigger_data)
    if set_resp is None:
        print("\nERROR: set_trigger returned None")
        return 0, 1
    print("\nset_trigger response:")
    for k, v in set_resp.items():
        print(f"  {k}: {v}")

    get_resp = tx.get_trigger()
    if get_resp is None:
        print("\nERROR: get_trigger returned None")
        return 0, 1
    print("\nget_trigger response:")
    for k, v in get_resp.items():
        print(f"  {k}: {v}")

    print("\n--- Verifying set_trigger response ---")
    p1, f1 = verify_trigger(trigger_data, set_resp)

    print("\n--- Verifying get_trigger response ---")
    p2, f2 = verify_trigger(trigger_data, get_resp)

    return p1 + p2, f1 + f2


if __name__ == "__main__":
    sys.exit(main())
