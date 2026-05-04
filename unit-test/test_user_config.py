"""
LIFU User Config Test Suite
===========================
Tests the read_config / write_config / write_config_json helpers in
``openlifu_sdk.io.component.OWComponent`` for both TX and HV devices.

Supports two modes:

  Unit test (mocked UART, no hardware required):
    python -m pytest unit-test/test_user_config.py -v
    -- or --
    python unit-test/test_user_config.py

  Interactive menu-driven tests (real hardware):
    Run against TX:
        python unit-test/test_user_config.py --interactive --device tx
    Run against HV:
        python unit-test/test_user_config.py --interactive --device hv

  Hardware safety:
    The interactive runner reads the device config FIRST, runs its tests
    using a non-destructive "scratch key" pattern (added then removed),
    and on exit RESTORES the original config exactly as it was. If a test
    crashes mid-run, the original config is still restored from the saved
    snapshot.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import struct
import sys
import time
import unittest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Ensure src/ is importable when run directly from the repo root
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from openlifu_sdk.io.exceptions import (
    LIFUDeviceError,
    LIFUNotConnectedError,
    LIFUProtocolError,
)
from openlifu_sdk.io.LIFUConfig import OW_ERROR, OW_RESP
from openlifu_sdk.io.LIFUHVController import HVController
from openlifu_sdk.io.LIFUTXDevice import TxDevice
from openlifu_sdk.io.LIFUUserConfig import (
    LIFU_MAGIC,
    LIFU_VER,
    LifuUserConfig,
    LifuUserConfigHeader,
)
from openlifu_sdk.io.uart import OWUart


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_packet(data: bytes = b"", packet_type: int = OW_RESP, reserved: int = 0):
    pkt = MagicMock()
    pkt.packet_type = packet_type
    pkt.data = bytearray(data)
    pkt.data_len = len(data)
    pkt.reserved = reserved
    pkt.print_packet = MagicMock()
    return pkt


def _build_wire_config(json_data: dict, seq: int = 1, crc: int = 0xABCD) -> bytes:
    """Build a valid LIFU config wire payload from a dict."""
    cfg = LifuUserConfig(
        header=LifuUserConfigHeader(
            magic=LIFU_MAGIC, version=LIFU_VER,
            seq=seq, crc=crc, json_len=0,
        ),
        json_data=json_data,
    )
    return cfg.to_wire_bytes()


def _build_response_header(seq: int, crc: int = 0x1234, json_len: int = 0) -> bytes:
    """Build a 16-byte header-only response (as returned by write_config)."""
    return LifuUserConfigHeader(
        magic=LIFU_MAGIC, version=LIFU_VER,
        seq=seq, crc=crc, json_len=json_len,
    ).to_bytes()


def _patch_component_uart(component) -> MagicMock:
    """Replace a component's UART with a MagicMock and return it."""
    mock_uart = MagicMock(spec=OWUart)
    mock_uart.desc = "MOCK"
    mock_uart.demo_mode = False
    mock_uart.asyncMode = False
    mock_uart.is_connected = True
    mock_uart.clear_buffer = MagicMock(return_value=None)
    component._uart = mock_uart
    return mock_uart


# ===========================================================================
# Unit Tests — wire format helpers
# ===========================================================================
class TestLifuUserConfigWireFormat(unittest.TestCase):
    """Round-trip tests for LifuUserConfigHeader / LifuUserConfig wire format."""

    def test_01_header_roundtrip(self):
        """Header serializes and parses back to identical values."""
        h = LifuUserConfigHeader(
            magic=LIFU_MAGIC, version=LIFU_VER,
            seq=42, crc=0xBEEF, json_len=128,
        )
        parsed = LifuUserConfigHeader.from_bytes(h.to_bytes())
        self.assertEqual(parsed.magic, LIFU_MAGIC)
        self.assertEqual(parsed.version, LIFU_VER)
        self.assertEqual(parsed.seq, 42)
        self.assertEqual(parsed.crc, 0xBEEF)
        self.assertEqual(parsed.json_len, 128)

    def test_02_header_validates_magic_and_version(self):
        """is_valid() is True only for matching magic + version."""
        good = LifuUserConfigHeader(LIFU_MAGIC, LIFU_VER, 0, 0, 0)
        bad_magic = LifuUserConfigHeader(0xDEADBEEF, LIFU_VER, 0, 0, 0)
        bad_ver = LifuUserConfigHeader(LIFU_MAGIC, 0x00000001, 0, 0, 0)
        self.assertTrue(good.is_valid())
        self.assertFalse(bad_magic.is_valid())
        self.assertFalse(bad_ver.is_valid())

    def test_03_header_too_short_raises(self):
        """from_bytes() raises ValueError when buffer < 16 bytes."""
        with self.assertRaises(ValueError):
            LifuUserConfigHeader.from_bytes(b"\x00" * 8)

    def test_04_config_roundtrip(self):
        """LifuUserConfig.to_wire_bytes -> from_wire_bytes preserves data."""
        data = {"hello": "world", "count": 7, "nested": {"a": [1, 2, 3]}}
        wire = _build_wire_config(data, seq=5, crc=0x9999)
        cfg = LifuUserConfig.from_wire_bytes(wire)
        self.assertEqual(cfg.json_data, data)
        self.assertEqual(cfg.header.seq, 5)
        self.assertEqual(cfg.header.crc, 0x9999)
        self.assertGreater(cfg.header.json_len, 0)

    def test_05_config_invalid_magic_raises(self):
        """from_wire_bytes() raises ValueError when magic is wrong."""
        bad = struct.pack('<IIIHH', 0xDEADBEEF, LIFU_VER, 0, 0, 0)
        with self.assertRaises(ValueError):
            LifuUserConfig.from_wire_bytes(bad)

    def test_06_config_handles_truncated_json(self):
        """from_wire_bytes() tolerates truncated JSON (returns empty dict)."""
        header = LifuUserConfigHeader(LIFU_MAGIC, LIFU_VER, 0, 0, 100).to_bytes()
        # Provide only 10 bytes of JSON when header claims 100
        truncated = header + b'{"x":1}'
        cfg = LifuUserConfig.from_wire_bytes(truncated)
        # Even truncated, the small JSON snippet should still parse
        self.assertEqual(cfg.json_data, {"x": 1})

    def test_07_config_handles_invalid_json(self):
        """from_wire_bytes() falls back to empty dict on invalid JSON."""
        bad_json = b"not json at all"
        header = LifuUserConfigHeader(
            LIFU_MAGIC, LIFU_VER, 0, 0, len(bad_json)
        ).to_bytes()
        cfg = LifuUserConfig.from_wire_bytes(header + bad_json)
        self.assertEqual(cfg.json_data, {})

    def test_08_set_get_update(self):
        """set/get/update operate on the json_data dict."""
        cfg = LifuUserConfig()
        cfg.set("foo", 1)
        cfg.update({"bar": 2, "baz": 3})
        self.assertEqual(cfg.get("foo"), 1)
        self.assertEqual(cfg.get("bar"), 2)
        self.assertEqual(cfg.get("missing", "default"), "default")
        self.assertEqual(cfg.to_dict(), {"foo": 1, "bar": 2, "baz": 3})


# ===========================================================================
# Unit Tests — read_config / write_config (TxDevice path)
# ===========================================================================
class TestReadWriteConfigTx(unittest.TestCase):
    """Verify read_config / write_config behavior on a TxDevice."""

    def setUp(self):
        self.tx = TxDevice()
        self.uart = _patch_component_uart(self.tx)

    # --- read_config --------------------------------------------------------

    def test_01_read_config_success(self):
        """read_config() parses a valid wire payload."""
        wire = _build_wire_config({"role": "tx", "version": 1}, seq=3)
        self.uart.send_packet.return_value = _make_packet(wire)
        cfg = self.tx.read_config()
        self.assertIsInstance(cfg, LifuUserConfig)
        self.assertEqual(cfg.json_data["role"], "tx")
        self.assertEqual(cfg.header.seq, 3)

    def test_02_read_config_empty_dict(self):
        """read_config() round-trips an empty config payload."""
        wire = _build_wire_config({})
        self.uart.send_packet.return_value = _make_packet(wire)
        cfg = self.tx.read_config()
        self.assertEqual(cfg.json_data, {})

    def test_03_read_config_bad_magic_raises_protocol_error(self):
        """read_config() raises LIFUProtocolError on bad magic."""
        bad = struct.pack('<IIIHH', 0xDEADBEEF, LIFU_VER, 0, 0, 0)
        self.uart.send_packet.return_value = _make_packet(bad)
        with self.assertRaises(LIFUProtocolError):
            self.tx.read_config()

    def test_04_read_config_disconnected_raises(self):
        """read_config() raises LIFUNotConnectedError when not connected."""
        self.uart.is_connected = False
        with self.assertRaises(LIFUNotConnectedError):
            self.tx.read_config()

    def test_05_read_config_device_error_raises(self):
        """read_config() raises LIFUDeviceError on OW_ERROR response."""
        self.uart.send_packet.return_value = _make_packet(packet_type=OW_ERROR)
        with self.assertRaises(LIFUDeviceError):
            self.tx.read_config()

    # --- write_config -------------------------------------------------------

    def test_06_write_config_returns_updated_seq(self):
        """write_config() returns config with updated header from device."""
        original = LifuUserConfig(json_data={"key": "value"})
        self.uart.send_packet.return_value = _make_packet(
            _build_response_header(seq=99, crc=0x4242)
        )
        updated = self.tx.write_config(original)
        self.assertEqual(updated.header.seq, 99)
        self.assertEqual(updated.header.crc, 0x4242)
        # Original json_data carries through
        self.assertEqual(updated.json_data, {"key": "value"})

    def test_07_write_config_payload_format(self):
        """write_config() sends [header][json] with reserved=1."""
        cfg = LifuUserConfig(json_data={"a": 1})
        self.uart.send_packet.return_value = _make_packet(
            _build_response_header(seq=2)
        )
        self.tx.write_config(cfg)
        kwargs = self.uart.send_packet.call_args.kwargs
        self.assertEqual(kwargs.get("reserved"), 1)
        sent = bytes(kwargs.get("data"))
        # Decode our own payload to validate framing
        round_trip = LifuUserConfig.from_wire_bytes(sent)
        self.assertEqual(round_trip.json_data, {"a": 1})

    def test_08_write_config_bad_response_raises(self):
        """write_config() raises LIFUProtocolError on unparseable response."""
        cfg = LifuUserConfig(json_data={"a": 1})
        self.uart.send_packet.return_value = _make_packet(b"\x00\x01\x02")
        with self.assertRaises(LIFUProtocolError):
            self.tx.write_config(cfg)

    def test_09_write_config_disconnected_raises(self):
        """write_config() raises LIFUNotConnectedError when not connected."""
        self.uart.is_connected = False
        with self.assertRaises(LIFUNotConnectedError):
            self.tx.write_config(LifuUserConfig())

    # --- write_config_json --------------------------------------------------

    def test_10_write_config_json_valid(self):
        """write_config_json() accepts a valid JSON string."""
        self.uart.send_packet.return_value = _make_packet(
            _build_response_header(seq=11)
        )
        result = self.tx.write_config_json('{"hello":"world"}')
        self.assertEqual(result.json_data, {"hello": "world"})
        self.assertEqual(result.header.seq, 11)

    def test_11_write_config_json_invalid_raises(self):
        """write_config_json() raises ValueError on invalid JSON."""
        with self.assertRaises(ValueError):
            self.tx.write_config_json("{not valid json}")


# ===========================================================================
# Unit Tests — read_config / write_config (HVController path)
# ===========================================================================
class TestReadWriteConfigHV(unittest.TestCase):
    """Verify read_config / write_config behavior on the HV controller."""

    def setUp(self):
        self.hv = HVController()
        self.uart = _patch_component_uart(self.hv)

    def test_01_read_config_success(self):
        """HV read_config() parses a valid wire payload."""
        wire = _build_wire_config({"role": "hv", "fan": 50}, seq=7)
        self.uart.send_packet.return_value = _make_packet(wire)
        cfg = self.hv.read_config()
        self.assertEqual(cfg.json_data["role"], "hv")
        self.assertEqual(cfg.header.seq, 7)

    def test_02_write_config_returns_updated_header(self):
        """HV write_config() returns updated header from device."""
        original = LifuUserConfig(json_data={"hv_test_key": True})
        self.uart.send_packet.return_value = _make_packet(
            _build_response_header(seq=200, crc=0x55AA)
        )
        updated = self.hv.write_config(original)
        self.assertEqual(updated.header.seq, 200)
        self.assertEqual(updated.json_data, {"hv_test_key": True})

    def test_03_read_then_write_roundtrip(self):
        """Read a config, modify, write — verify the modification was sent."""
        original_data = {"keep": 1}
        self.uart.send_packet.side_effect = [
            _make_packet(_build_wire_config(original_data, seq=5)),  # read
            _make_packet(_build_response_header(seq=6)),             # write
        ]
        cfg = self.hv.read_config()
        cfg.set("added", "value")
        updated = self.hv.write_config(cfg)
        self.assertEqual(updated.header.seq, 6)
        # Inspect what was actually written
        write_call = self.uart.send_packet.call_args_list[1]
        sent_payload = LifuUserConfig.from_wire_bytes(
            bytes(write_call.kwargs.get("data"))
        )
        self.assertEqual(sent_payload.json_data, {"keep": 1, "added": "value"})

    def test_04_disconnected_raises(self):
        """HV read_config() raises LIFUNotConnectedError when not connected."""
        self.uart.is_connected = False
        with self.assertRaises(LIFUNotConnectedError):
            self.hv.read_config()


# ===========================================================================
# Interactive (menu-driven) Tests — real hardware
# ===========================================================================
SCRATCH_KEY = "__lifu_sdk_test_scratch__"


class UserConfigInteractiveTests:
    """
    Menu-driven tests against real hardware.

    Safety model:
      * On startup, read the device config and store it as `_snapshot`.
      * Tests only modify a scratch key (`SCRATCH_KEY`); the original
        keys/values are never touched.
      * On exit (or after `Restore Original Config`), the snapshot is
        written back verbatim, fully restoring the device's flash to its
        original state.
    """

    def __init__(self, component, device_label: str):
        self.component = component
        self.device_label = device_label
        self._snapshot: LifuUserConfig | None = None
        self.menu_items = [
            ("Read Current Config",          self.test_read_config),
            ("Show Snapshot (original)",     self.test_show_snapshot),
            ("Add Scratch Key",              self.test_add_scratch_key),
            ("Update Scratch Key Value",     self.test_update_scratch_key),
            ("Remove Scratch Key",           self.test_remove_scratch_key),
            ("Round-Trip Scratch Key",       self.test_roundtrip_scratch),
            ("Write Custom JSON (scratch)",  self.test_write_custom_json),
            ("Restore Original Config",      self.test_restore_original),
            ("Run Safe Test Suite",          self.run_all_safe),
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _ok(msg: str):
        print(f"  [PASS] {msg}")

    @staticmethod
    def _err(msg: str):
        print(f"  [FAIL] {msg}")

    def _take_snapshot(self):
        """Read and cache the original device config (call once at startup)."""
        if self._snapshot is not None:
            return
        print(f"Reading original {self.device_label} config (snapshot)...")
        self._snapshot = self.component.read_config()
        print(f"  Snapshot taken: seq={self._snapshot.header.seq}, "
              f"json_len={self._snapshot.header.json_len}")

    def _restore_snapshot(self) -> bool:
        """Write the cached snapshot back to the device."""
        if self._snapshot is None:
            print("  No snapshot available, nothing to restore.")
            return False
        print(f"Restoring original {self.device_label} config to device...")
        # Write a fresh copy so internal header.json_len gets re-derived
        restore_cfg = LifuUserConfig(
            json_data=copy.deepcopy(self._snapshot.json_data)
        )
        result = self.component.write_config(restore_cfg)
        print(f"  Restore complete. New device seq={result.header.seq}")
        return True

    # ------------------------------------------------------------------
    # Individual tests
    # ------------------------------------------------------------------
    def test_read_config(self):
        cfg = self.component.read_config()
        print(f"  seq={cfg.header.seq}  crc=0x{cfg.header.crc:04X}  "
              f"json_len={cfg.header.json_len}")
        print(f"  data={json.dumps(cfg.json_data, indent=2)}")
        self._ok("Config read.")
        return cfg

    def test_show_snapshot(self):
        if self._snapshot is None:
            self._err("No snapshot available.")
            return None
        print(f"  seq={self._snapshot.header.seq}  "
              f"crc=0x{self._snapshot.header.crc:04X}")
        print(f"  data={json.dumps(self._snapshot.json_data, indent=2)}")
        self._ok("Snapshot displayed.")
        return self._snapshot

    def test_add_scratch_key(self):
        """Read, add SCRATCH_KEY, write back."""
        cfg = self.component.read_config()
        cfg.set(SCRATCH_KEY, {"created": int(time.time()), "iter": 0})
        updated = self.component.write_config(cfg)
        verify = self.component.read_config()
        if SCRATCH_KEY in verify.json_data:
            self._ok(f"Scratch key added. New seq={updated.header.seq}")
            return True
        self._err("Scratch key not present after read-back.")
        return False

    def test_update_scratch_key(self):
        """Bump an iter counter inside the scratch key."""
        cfg = self.component.read_config()
        existing = cfg.get(SCRATCH_KEY) or {"created": int(time.time()), "iter": 0}
        existing["iter"] = existing.get("iter", 0) + 1
        existing["last_update"] = int(time.time())
        cfg.set(SCRATCH_KEY, existing)
        self.component.write_config(cfg)
        verify = self.component.read_config()
        if verify.get(SCRATCH_KEY, {}).get("iter") == existing["iter"]:
            self._ok(f"Scratch iter -> {existing['iter']}")
            return True
        self._err("Scratch iter did not update.")
        return False

    def test_remove_scratch_key(self):
        """Remove SCRATCH_KEY (does not touch any other keys)."""
        cfg = self.component.read_config()
        if SCRATCH_KEY not in cfg.json_data:
            print("  Scratch key not present; nothing to remove.")
            return True
        del cfg.json_data[SCRATCH_KEY]
        self.component.write_config(cfg)
        verify = self.component.read_config()
        if SCRATCH_KEY not in verify.json_data:
            self._ok("Scratch key removed.")
            return True
        self._err("Scratch key still present after delete.")
        return False

    def test_roundtrip_scratch(self):
        """Add → read-back → verify → remove → verify-removed."""
        ok = self.test_add_scratch_key()
        if not ok:
            return False
        ok = self.test_remove_scratch_key()
        if not ok:
            return False
        self._ok("Round-trip complete.")
        return True

    def test_write_custom_json(self):
        """
        Prompt user for a JSON object to MERGE under SCRATCH_KEY.
        Never overwrites original keys.
        """
        raw = input(
            f"  Enter JSON object to store under '{SCRATCH_KEY}' "
            f"(or blank to skip): "
        ).strip()
        if not raw:
            print("  Skipped.")
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._err(f"Invalid JSON: {exc}")
            return False
        cfg = self.component.read_config()
        cfg.set(SCRATCH_KEY, payload)
        self.component.write_config(cfg)
        verify = self.component.read_config()
        if verify.get(SCRATCH_KEY) == payload:
            self._ok("Custom JSON written under scratch key.")
            return True
        self._err("Read-back does not match.")
        return False

    def test_restore_original(self):
        """Force-restore the snapshot taken at startup."""
        return self._restore_snapshot()

    def run_all_safe(self):
        """Run only non-destructive scratch-key tests, then restore."""
        print("\n=== Running Safe Test Suite ===")
        sequence = [
            ("Read Current Config",      self.test_read_config),
            ("Add Scratch Key",          self.test_add_scratch_key),
            ("Update Scratch Key Value", self.test_update_scratch_key),
            ("Remove Scratch Key",       self.test_remove_scratch_key),
            ("Round-Trip Scratch Key",   self.test_roundtrip_scratch),
        ]
        for label, fn in sequence:
            print(f"\n[{label}]")
            try:
                fn()
            except Exception as exc:
                self._err(f"Exception: {exc}")
        print("\n[Restore Original Config]")
        try:
            self._restore_snapshot()
        except Exception as exc:
            self._err(f"Restore failed: {exc}")

    # ------------------------------------------------------------------
    # Menu loop
    # ------------------------------------------------------------------
    def run_menu(self):
        self._take_snapshot()
        try:
            while True:
                print("\n" + "=" * 56)
                print(f"   User Config Interactive Test Menu  ({self.device_label})")
                print("=" * 56)
                for idx, (label, _) in enumerate(self.menu_items, 1):
                    print(f"  {idx:2d}. {label}")
                print("   0. Exit (auto-restores original config)")
                choice = input("Select: ").strip()
                if choice == "0":
                    break
                try:
                    i = int(choice) - 1
                    if 0 <= i < len(self.menu_items):
                        label, fn = self.menu_items[i]
                        print(f"\n--- {label} ---")
                        try:
                            fn()
                        except Exception as exc:
                            self._err(f"Exception: {exc}")
                    else:
                        print("Invalid selection.")
                except ValueError:
                    print("Please enter a number.")
        finally:
            print("\n--- Final cleanup: restoring original config ---")
            try:
                self._restore_snapshot()
            except Exception as exc:
                print(f"  [WARN] Restore failed: {exc}")


# ===========================================================================
# Hardware connection helper
# ===========================================================================
def _connect_component(device: str):
    """Return (component, label) for the requested device ('tx' or 'hv')."""
    from openlifu_sdk.io.LIFUInterface import LIFUInterface

    print("Connecting to LIFU device(s)...")
    iface = LIFUInterface()
    tx_connected, hv_connected = iface.is_device_connected()
    print(f"TX connected: {tx_connected}  |  HV connected: {hv_connected}")

    if device == "tx":
        if not tx_connected:
            print("TX device not connected. Exiting.")
            sys.exit(1)
        return iface.txdevice, "TX"
    if device == "hv":
        if not hv_connected:
            print("HV controller not connected. Exiting.")
            sys.exit(1)
        return iface.hvcontroller, "HV"
    print(f"Unknown device '{device}' (use 'tx' or 'hv').")
    sys.exit(2)


# ===========================================================================
# Entry point
# ===========================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LIFU User Config Test Suite")
    parser.add_argument(
        "--interactive", action="store_true",
        help="Run menu-driven tests against real hardware",
    )
    parser.add_argument(
        "--device", choices=["tx", "hv"], default="tx",
        help="Which device to target in interactive mode (default: tx)",
    )
    args, remaining = parser.parse_known_args()

    if args.interactive:
        component, label = _connect_component(args.device)
        suite = UserConfigInteractiveTests(component, label)
        try:
            suite.run_menu()
        finally:
            try:
                component.close()
            except Exception:
                pass
    else:
        unittest.main(argv=[sys.argv[0]] + remaining)
