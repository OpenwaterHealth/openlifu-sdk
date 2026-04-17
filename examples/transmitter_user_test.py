"""Transmitter user-config read / write test.

Reads the current user configuration from the device, optionally writes
a new JSON configuration, and reads it back to verify the round-trip.

Usage:
    python transmitter_user_test.py                  # read-only
    python transmitter_user_test.py --write '{"key":"value"}'
    python transmitter_user_test.py --set foo=bar     # merge a key
    python transmitter_user_test.py --clear            # write empty config
"""
import argparse
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openlifu_sdk import LIFUInterface, LIFUUserConfig


def main() -> int:
    parser = argparse.ArgumentParser(description="Transmitter user-config test")
    parser.add_argument("--module", type=int, default=0,
                        help="Module address (default: 0)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--write", type=str, default=None, metavar="JSON",
                       help="Full JSON string to write (replaces existing config)")
    group.add_argument("--set", type=str, default=None, metavar="KEY=VALUE",
                       help="Set a single key (merges into existing config)")
    group.add_argument("--clear", action="store_true", default=False,
                       help="Write an empty configuration")
    args = parser.parse_args()

    # ---- connect --------------------------------------------------------
    iface = LIFUInterface()
    if not iface.transmitter.connect():
        print("ERROR: No transmitter found.")
        return 1

    tx = iface.transmitter
    print(f"Connected to transmitter on {tx.uart._serial.port}\n")

    # ---- read current config -------------------------------------------
    print("Reading current config ...")
    config = tx.read_config(module=args.module)
    if config is None:
        print("  No config stored on device (or read error).")
        config = LIFUUserConfig()
    else:
        print(f"  Header : magic=0x{config.header.magic:08X}  ver=0x{config.header.version:08X}"
              f"  seq={config.header.seq}  crc=0x{config.header.crc:04X}"
              f"  json_len={config.header.json_len}")
        print(f"  JSON   : {config.get_json_str()}")

    # ---- write (if requested) ------------------------------------------
    if args.write is not None:
        # Windows cmd passes single-quoted args with the quotes intact; strip them.
        json_str = args.write.strip("'")
        print(f"\nWriting full JSON config ...")
        try:
            new_config = LIFUUserConfig()
            new_config.set_json_str(json_str)
        except json.JSONDecodeError as exc:
            print(f"  ERROR: Invalid JSON: {exc}")
            tx.close()
            return 1

        result = tx.write_config(new_config, module=args.module)
        if result is None:
            print("  ERROR: write_config failed.")
            tx.close()
            return 1
        print(f"  Write OK — new seq={result.header.seq}, crc=0x{result.header.crc:04X}")

    elif args.set is not None:
        if "=" not in args.set:
            print("  ERROR: --set requires KEY=VALUE format.")
            tx.close()
            return 1
        key, _, raw_value = args.set.partition("=")
        # Try to parse value as JSON (numbers, bools, objects);
        # fall back to plain string.
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value

        print(f"\nMerging key '{key}' = {value!r} into existing config ...")
        config.set(key, value)
        result = tx.write_config(config, module=args.module)
        if result is None:
            print("  ERROR: write_config failed.")
            tx.close()
            return 1
        print(f"  Write OK — new seq={result.header.seq}, crc=0x{result.header.crc:04X}")

    elif args.clear:
        print("\nClearing config (writing empty JSON) ...")
        result = tx.write_config(LIFUUserConfig(), module=args.module)
        if result is None:
            print("  ERROR: write_config failed.")
            tx.close()
            return 1
        print(f"  Write OK — new seq={result.header.seq}, crc=0x{result.header.crc:04X}")

    else:
        # Read-only — we already printed it above.
        print("\n(read-only mode — use --write, --set, or --clear to modify)")
        tx.close()
        return 0

    # ---- read back & verify --------------------------------------------
    print("\nReading back config ...")
    readback = tx.read_config(module=args.module)
    if readback is None:
        print("  ERROR: read-back failed.")
        tx.close()
        return 1

    print(f"  Header : magic=0x{readback.header.magic:08X}  ver=0x{readback.header.version:08X}"
          f"  seq={readback.header.seq}  crc=0x{readback.header.crc:04X}"
          f"  json_len={readback.header.json_len}")
    print(f"  JSON   : {readback.get_json_str()}")

    # Compare written vs read-back JSON
    if args.write is not None:
        expected = json.loads(args.write)
    elif args.set is not None:
        expected = config.json_data
    elif args.clear:
        expected = {}
    else:
        expected = None

    if expected is not None:
        if readback.json_data == expected:
            print("\n  PASS: read-back matches written config.")
        else:
            print(f"\n  FAIL: mismatch!")
            print(f"    Expected: {json.dumps(expected, separators=(',',':'))}")
            print(f"    Got:      {json.dumps(readback.json_data, separators=(',',':'))}")
            tx.close()
            return 1

    tx.close()
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
