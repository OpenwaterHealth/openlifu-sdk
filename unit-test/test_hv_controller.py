"""
HVController Test Suite
=======================
Supports two modes:

  Unit test (mocked UART, no hardware required):
    python -m pytest unit-test/test_hv_controller.py -v
    -- or --
    python unit-test/test_hv_controller.py

  Interactive menu-driven tests (real hardware):
    python unit-test/test_hv_controller.py --interactive
"""
from __future__ import annotations

import argparse
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
from openlifu_sdk.io.LIFUConfig import (
    OW_ERROR,
    OW_POWER,
    OW_RESP,
)
from openlifu_sdk.io.LIFUHVController import HVController
from openlifu_sdk.io.uart import OWUart


# ---------------------------------------------------------------------------
# Helper: build a lightweight mock UartPacket
# ---------------------------------------------------------------------------
def _make_packet(data: bytes = b"", packet_type: int = OW_RESP, reserved: int = 0):
    pkt = MagicMock()
    pkt.packet_type = packet_type
    pkt.data = bytearray(data)
    pkt.data_len = len(data)
    pkt.reserved = reserved
    pkt.print_packet = MagicMock()
    return pkt


def _make_hv_controller() -> tuple[HVController, MagicMock]:
    """Construct an HVController whose underlying UART is fully mocked."""
    hv = HVController()
    mock_uart = MagicMock(spec=OWUart)
    mock_uart.desc = "HV"
    mock_uart.demo_mode = False
    mock_uart.asyncMode = False
    mock_uart.is_connected = True  # property -> just an attribute on the mock
    mock_uart.clear_buffer = MagicMock(return_value=None)
    hv._uart = mock_uart
    return hv, mock_uart


# ===========================================================================
# Unit Tests  (pytest / unittest)
# ===========================================================================
class TestHVControllerUnit(unittest.TestCase):
    """Unit tests for HVController using a fully mocked OWUart."""

    def setUp(self):
        self.hv, self.uart = _make_hv_controller()

    # --- connection state ---------------------------------------------------

    def test_01_is_connected_true(self):
        """is_connected() mirrors UART state (True)."""
        self.uart.is_connected = True
        self.assertTrue(self.hv.is_connected())

    def test_02_is_connected_false(self):
        """is_connected() mirrors UART state (False)."""
        self.uart.is_connected = False
        self.assertFalse(self.hv.is_connected())

    # --- ping ---------------------------------------------------------------

    def test_03_ping_success(self):
        """ping() returns True on a non-error response."""
        self.uart.send_packet.return_value = _make_packet(b"\x00")
        self.assertTrue(self.hv.ping())

    def test_04_ping_error_packet_raises(self):
        """ping() raises LIFUDeviceError when device replies with OW_ERROR."""
        self.uart.send_packet.return_value = _make_packet(packet_type=OW_ERROR)
        with self.assertRaises(LIFUDeviceError):
            self.hv.ping()

    def test_05_ping_disconnected_raises(self):
        """ping() raises LIFUNotConnectedError when UART is not connected."""
        self.uart.is_connected = False
        with self.assertRaises(LIFUNotConnectedError):
            self.hv.ping()

    # --- temperatures -------------------------------------------------------

    def test_06_get_temperature1_valid(self):
        """get_temperature1() decodes a little-endian float correctly."""
        expected = 36.6
        self.uart.send_packet.return_value = _make_packet(struct.pack("<f", expected))
        self.assertAlmostEqual(self.hv.get_temperature1(), expected, places=1)

    def test_07_get_temperature1_bad_payload(self):
        """get_temperature1() raises LIFUProtocolError on wrong payload size."""
        self.uart.send_packet.return_value = _make_packet(b"\x01\x02")
        with self.assertRaises(LIFUProtocolError):
            self.hv.get_temperature1()

    def test_08_get_temperature2_valid(self):
        """get_temperature2() decodes a little-endian float correctly."""
        expected = 41.2
        self.uart.send_packet.return_value = _make_packet(struct.pack("<f", expected))
        self.assertAlmostEqual(self.hv.get_temperature2(), expected, places=1)

    # --- 12V rail -----------------------------------------------------------

    def test_09_turn_12v_on(self):
        """turn_12v_on() returns True and updates is_12v_on flag."""
        self.uart.send_packet.return_value = _make_packet()
        self.assertTrue(self.hv.turn_12v_on())
        self.assertTrue(self.hv.is_12v_on)

    def test_10_turn_12v_off(self):
        """turn_12v_off() returns True and clears is_12v_on flag."""
        self.uart.send_packet.return_value = _make_packet()
        self.hv.is_12v_on = True
        self.assertTrue(self.hv.turn_12v_off())
        self.assertFalse(self.hv.is_12v_on)

    def test_11_get_12v_status_on(self):
        """get_12v_status() returns True when reserved == 1."""
        self.uart.send_packet.return_value = _make_packet(reserved=1)
        self.assertTrue(self.hv.get_12v_status())

    def test_12_get_12v_status_off(self):
        """get_12v_status() returns False when reserved == 0."""
        self.uart.send_packet.return_value = _make_packet(reserved=0)
        self.assertFalse(self.hv.get_12v_status())

    # --- HV rail ------------------------------------------------------------

    def test_13_turn_hv_on(self):
        """turn_hv_on() returns True and updates is_hv_on flag."""
        self.uart.send_packet.return_value = _make_packet()
        self.assertTrue(self.hv.turn_hv_on())
        self.assertTrue(self.hv.is_hv_on)

    def test_14_turn_hv_off(self):
        """turn_hv_off() returns True and clears is_hv_on flag."""
        self.uart.send_packet.return_value = _make_packet()
        self.hv.is_hv_on = True
        self.assertTrue(self.hv.turn_hv_off())
        self.assertFalse(self.hv.is_hv_on)

    def test_15_get_hv_status_on(self):
        """get_hv_status() returns True when reserved == 1."""
        self.uart.send_packet.return_value = _make_packet(reserved=1)
        self.assertTrue(self.hv.get_hv_status())

    def test_16_get_hv_status_off(self):
        """get_hv_status() returns False when reserved == 0."""
        self.uart.send_packet.return_value = _make_packet(reserved=0)
        self.assertFalse(self.hv.get_hv_status())

    # --- set_voltage --------------------------------------------------------

    def test_17_set_voltage_valid(self):
        """set_voltage() returns True and stores supply_voltage."""
        self.uart.send_packet.return_value = _make_packet()
        self.assertTrue(self.hv.set_voltage(50.0))
        self.assertEqual(self.hv.supply_voltage, 50.0)

    def test_18_set_voltage_payload_format(self):
        """set_voltage() sends the voltage as a big-endian float."""
        self.uart.send_packet.return_value = _make_packet()
        voltage = 42.5
        self.hv.set_voltage(voltage)
        # First call args: packet with the float payload
        first_call = self.uart.send_packet.call_args_list[0]
        sent = first_call.kwargs.get("data")
        self.assertEqual(bytes(sent), struct.pack(">f", voltage))

    def test_19_set_voltage_too_low_raises(self):
        """set_voltage() raises ValueError below 5 V."""
        with self.assertRaises(ValueError):
            self.hv.set_voltage(4.99)

    def test_20_set_voltage_too_high_raises(self):
        """set_voltage() raises ValueError above 100 V."""
        with self.assertRaises(ValueError):
            self.hv.set_voltage(100.1)

    def test_21_set_voltage_reasserts_hv_when_on(self):
        """set_voltage() sends a second HV_ON packet if HV was already on."""
        self.uart.send_packet.return_value = _make_packet()
        self.hv.is_hv_on = True
        self.hv.set_voltage(60.0)
        # Expect 2 packets: SET_HV + reassert HV_ON
        self.assertEqual(self.uart.send_packet.call_count, 2)

    # --- get_voltage --------------------------------------------------------

    def test_22_get_voltage_valid(self):
        """get_voltage() decodes a little-endian float correctly."""
        expected = 47.25
        self.uart.send_packet.return_value = _make_packet(struct.pack("<f", expected))
        self.assertAlmostEqual(self.hv.get_voltage(), expected, places=2)

    def test_23_get_voltage_bad_payload(self):
        """get_voltage() raises LIFUProtocolError on wrong payload size."""
        self.uart.send_packet.return_value = _make_packet(b"\x00")
        with self.assertRaises(LIFUProtocolError):
            self.hv.get_voltage()

    # --- set_dacs -----------------------------------------------------------

    def test_24_set_dacs_valid(self):
        """set_dacs() returns True for in-range values."""
        self.uart.send_packet.return_value = _make_packet()
        self.assertTrue(self.hv.set_dacs(100, 200, 300, 400))

    def test_25_set_dacs_payload_format(self):
        """set_dacs() packs HVP, HRP, HVM, HRM as big-endian uint16 pairs."""
        self.uart.send_packet.return_value = _make_packet()
        hvp, hvm, hrp, hrm = 0x0123, 0x0456, 0x0789, 0x0ABC
        self.hv.set_dacs(hvp, hvm, hrp, hrm)
        sent = self.uart.send_packet.call_args.kwargs.get("data")
        expected = bytes([
            (hvp >> 8) & 0xFF, hvp & 0xFF,
            (hrp >> 8) & 0xFF, hrp & 0xFF,
            (hvm >> 8) & 0xFF, hvm & 0xFF,
            (hrm >> 8) & 0xFF, hrm & 0xFF,
        ])
        self.assertEqual(bytes(sent), expected)

    def test_26_set_dacs_out_of_range_raises(self):
        """set_dacs() raises ValueError when any code is > 4095."""
        with self.assertRaises(ValueError):
            self.hv.set_dacs(4096, 0, 0, 0)

    def test_27_set_dacs_none_treated_as_zero(self):
        """set_dacs() treats None values as 0."""
        self.uart.send_packet.return_value = _make_packet()
        self.assertTrue(self.hv.set_dacs(None, None, None, None))
        sent = self.uart.send_packet.call_args.kwargs.get("data")
        self.assertEqual(bytes(sent), b"\x00" * 8)

    # --- fans ---------------------------------------------------------------

    def test_28_set_fan_speed_valid(self):
        """set_fan_speed() returns the speed it set."""
        self.uart.send_packet.return_value = _make_packet()
        self.assertEqual(self.hv.set_fan_speed(0, 75), 75)

    def test_29_set_fan_speed_bad_id(self):
        """set_fan_speed() raises ValueError for invalid fan id."""
        with self.assertRaises(ValueError):
            self.hv.set_fan_speed(2, 50)

    def test_30_set_fan_speed_bad_speed(self):
        """set_fan_speed() raises ValueError for out-of-range speed."""
        with self.assertRaises(ValueError):
            self.hv.set_fan_speed(0, 150)

    def test_31_get_fan_speed_valid(self):
        """get_fan_speed() returns the duty-cycle byte."""
        self.uart.send_packet.return_value = _make_packet(b"\x55")
        self.assertEqual(self.hv.get_fan_speed(0), 0x55)

    def test_32_get_fan_speed_bad_id(self):
        """get_fan_speed() raises ValueError for invalid fan id."""
        with self.assertRaises(ValueError):
            self.hv.get_fan_speed(7)

    def test_33_get_fan_speed_bad_payload(self):
        """get_fan_speed() raises LIFUProtocolError on empty payload."""
        self.uart.send_packet.return_value = _make_packet(b"")
        with self.assertRaises(LIFUProtocolError):
            self.hv.get_fan_speed(0)

    # --- RGB LED ------------------------------------------------------------

    def test_34_set_rgb_led_valid(self):
        """set_rgb_led() returns True for a valid state."""
        self.uart.send_packet.return_value = _make_packet()
        self.assertTrue(self.hv.set_rgb_led(2))
        self.assertEqual(self.uart.send_packet.call_args.kwargs.get("reserved"), 2)

    def test_35_set_rgb_led_invalid(self):
        """set_rgb_led() raises ValueError for out-of-range state."""
        with self.assertRaises(ValueError):
            self.hv.set_rgb_led(9)

    def test_36_get_rgb_led_value(self):
        """get_rgb_led() returns the reserved byte from the packet."""
        self.uart.send_packet.return_value = _make_packet(reserved=3)
        self.assertEqual(self.hv.get_rgb_led(), 3)

    # --- raw DAC ------------------------------------------------------------

    def test_37_set_raw_dac_valid(self):
        """set_raw_dac() returns the dac_value it programmed."""
        self.uart.send_packet.return_value = _make_packet()
        self.assertEqual(self.hv.set_raw_dac(1, 1234), 1234)

    def test_38_set_raw_dac_payload_format(self):
        """set_raw_dac() sends a 2-byte big-endian value."""
        self.uart.send_packet.return_value = _make_packet()
        self.hv.set_raw_dac(0, 0x0ABC)
        sent = self.uart.send_packet.call_args.kwargs.get("data")
        self.assertEqual(bytes(sent), bytes([0x0A, 0xBC]))

    def test_39_set_raw_dac_bad_id(self):
        """set_raw_dac() raises ValueError for an invalid dac_id."""
        with self.assertRaises(ValueError):
            self.hv.set_raw_dac(4, 0)

    def test_40_set_raw_dac_bad_value(self):
        """set_raw_dac() raises ValueError when dac_value > 4095."""
        with self.assertRaises(ValueError):
            self.hv.set_raw_dac(0, 4096)

    # --- hv_enable ----------------------------------------------------------

    def test_41_hv_enable_true(self):
        """hv_enable(True) sends addr=1."""
        self.uart.send_packet.return_value = _make_packet()
        self.assertTrue(self.hv.hv_enable(True))
        self.assertEqual(self.uart.send_packet.call_args.kwargs.get("addr"), 1)

    def test_42_hv_enable_false(self):
        """hv_enable(False) sends addr=0."""
        self.uart.send_packet.return_value = _make_packet()
        self.assertTrue(self.hv.hv_enable(False))
        self.assertEqual(self.uart.send_packet.call_args.kwargs.get("addr"), 0)

    # --- vmon ---------------------------------------------------------------

    def test_43_get_vmon_values_valid(self):
        """get_vmon_values() decodes 8 channels from an 80-byte payload."""
        raw_adc = list(range(8))
        voltages = [float(i) for i in range(8)]
        converted = [float(i * 2) for i in range(8)]
        payload = (
            struct.pack("<8H", *raw_adc)
            + struct.pack("<8f", *voltages)
            + struct.pack("<8f", *converted)
        )
        self.uart.send_packet.return_value = _make_packet(payload)
        result = self.hv.get_vmon_values()
        self.assertEqual(len(result), 8)
        self.assertEqual(result[0]["raw_adc"], 0)
        self.assertAlmostEqual(result[3]["voltage"], 3.0, places=2)
        self.assertAlmostEqual(result[4]["converted_voltage"], 8.0, places=2)

    def test_44_get_vmon_values_bad_payload(self):
        """get_vmon_values() raises LIFUProtocolError on wrong payload size."""
        self.uart.send_packet.return_value = _make_packet(b"\x00" * 10)
        with self.assertRaises(LIFUProtocolError):
            self.hv.get_vmon_values()


# ===========================================================================
# Interactive (menu-driven) Tests  — real hardware
# ===========================================================================
class HVControllerInteractiveTests:
    """Menu-driven tests that exercise HVController against real connected hardware."""

    def __init__(self, hv: HVController):
        self.hv = hv
        self.menu_items = [
            ("Ping Device",                 self.test_ping),
            ("Get Firmware Version",        self.test_get_version),
            ("Get Temperature 1",           self.test_get_temperature1),
            ("Get Temperature 2",           self.test_get_temperature2),
            ("Turn 12V ON",                 self.test_turn_12v_on),
            ("Turn 12V OFF",                self.test_turn_12v_off),
            ("Get 12V Status",              self.test_get_12v_status),
            ("Turn HV ON",                  self.test_turn_hv_on),
            ("Turn HV OFF",                 self.test_turn_hv_off),
            ("Get HV Status",               self.test_get_hv_status),
            ("Set HV Voltage",              self.test_set_voltage),
            ("Get HV Voltage",              self.test_get_voltage),
            ("Set Fan Speed",               self.test_set_fan_speed),
            ("Get Fan Speed",               self.test_get_fan_speed),
            ("Set RGB LED",                 self.test_set_rgb_led),
            ("Get RGB LED",                 self.test_get_rgb_led),
            ("Get VMON Values",             self.test_get_vmon_values),
            ("Set Raw DAC",                 self.test_set_raw_dac),
            ("Set DACs (HVP/HVM/HRP/HRM)",  self.test_set_dacs),
            ("HV Enable / Disable",         self.test_hv_enable),
            ("Wait for HV Settle",          self.test_wait_for_settle),
            ("Run All Read-Only Tests",     self.run_all),
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

    # ------------------------------------------------------------------
    # Individual tests
    # ------------------------------------------------------------------
    def test_ping(self):
        print("Pinging HV device...")
        try:
            self.hv.ping()
            self._ok("Device responded.")
            return True
        except Exception as exc:
            self._err(f"Ping failed: {exc}")
            return False

    def test_get_version(self):
        print("Reading firmware version...")
        ver = self.hv.get_version()
        self._ok(f"Version: {ver}")
        return ver

    def test_get_temperature1(self):
        print("Reading temperature 1...")
        t = self.hv.get_temperature1()
        self._ok(f"Temperature 1: {t:.2f} °C")
        return t

    def test_get_temperature2(self):
        print("Reading temperature 2...")
        t = self.hv.get_temperature2()
        self._ok(f"Temperature 2: {t:.2f} °C")
        return t

    def test_turn_12v_on(self):
        print("Turning 12V ON...")
        result = self.hv.turn_12v_on()
        self._ok("12V ON.") if result else self._err("Failed.")
        return result

    def test_turn_12v_off(self):
        print("Turning 12V OFF...")
        result = self.hv.turn_12v_off()
        self._ok("12V OFF.") if result else self._err("Failed.")
        return result

    def test_get_12v_status(self):
        print("Reading 12V status...")
        on = self.hv.get_12v_status()
        self._ok(f"12V is {'ON' if on else 'OFF'}")
        return on

    def test_turn_hv_on(self):
        confirm = input("  Turn HV ON? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            return None
        print("Turning HV ON...")
        result = self.hv.turn_hv_on()
        self._ok("HV ON.") if result else self._err("Failed.")
        return result

    def test_turn_hv_off(self):
        print("Turning HV OFF...")
        result = self.hv.turn_hv_off()
        self._ok("HV OFF.") if result else self._err("Failed.")
        return result

    def test_get_hv_status(self):
        print("Reading HV status...")
        on = self.hv.get_hv_status()
        self._ok(f"HV is {'ON' if on else 'OFF'}")
        return on

    def test_set_voltage(self):
        try:
            v = float(input("  Voltage to set (5.0 - 100.0 V): "))
        except ValueError:
            self._err("Invalid input.")
            return False
        print(f"Setting HV to {v:.2f} V...")
        try:
            result = self.hv.set_voltage(v)
            self._ok(f"Set voltage to {v:.2f} V.") if result else self._err("Failed.")
            return result
        except Exception as exc:
            self._err(f"Exception: {exc}")
            return False

    def test_get_voltage(self):
        print("Reading HV voltage...")
        v = self.hv.get_voltage()
        self._ok(f"Measured HV: {v:.2f} V")
        return v

    def test_set_fan_speed(self):
        try:
            fan_id = int(input("  Fan ID (0=bottom, 1=top): "))
            speed = int(input("  Speed (0-100): "))
        except ValueError:
            self._err("Invalid input.")
            return False
        try:
            result = self.hv.set_fan_speed(fan_id, speed)
            self._ok(f"Fan {fan_id} set to {result}%.")
            return result
        except Exception as exc:
            self._err(f"Exception: {exc}")
            return False

    def test_get_fan_speed(self):
        try:
            fan_id = int(input("  Fan ID (0 or 1): "))
        except ValueError:
            self._err("Invalid input.")
            return None
        speed = self.hv.get_fan_speed(fan_id)
        self._ok(f"Fan {fan_id} speed = {speed}")
        return speed

    def test_set_rgb_led(self):
        print("  RGB states: 0=OFF, 1=RED, 2=BLUE, 3=GREEN")
        try:
            state = int(input("  State: "))
        except ValueError:
            self._err("Invalid input.")
            return False
        try:
            result = self.hv.set_rgb_led(state)
            self._ok("RGB updated.") if result else self._err("Failed.")
            return result
        except Exception as exc:
            self._err(f"Exception: {exc}")
            return False

    def test_get_rgb_led(self):
        state = self.hv.get_rgb_led()
        names = {0: "OFF", 1: "RED", 2: "BLUE", 3: "GREEN"}
        self._ok(f"RGB = {state} ({names.get(state, '?')})")
        return state

    def test_get_vmon_values(self):
        print("Reading VMON channels...")
        try:
            channels = self.hv.get_vmon_values()
        except Exception as exc:
            self._err(f"Exception: {exc}")
            return None
        for ch in channels:
            print(f"   ch{ch['channel']}  raw=0x{ch['raw_adc']:04X}  "
                  f"V={ch['voltage']:.3f}  conv={ch['converted_voltage']:.3f}")
        self._ok("VMON read complete.")
        return channels

    def test_set_raw_dac(self):
        try:
            dac_id = int(input("  DAC ID (0-3): "))
            dac_value = int(input("  DAC value (0-4095): "))
        except ValueError:
            self._err("Invalid input.")
            return False
        try:
            result = self.hv.set_raw_dac(dac_id, dac_value)
            self._ok(f"DAC {dac_id} = {result}.")
            return result
        except Exception as exc:
            self._err(f"Exception: {exc}")
            return False

    def test_set_dacs(self):
        try:
            hvp = int(input("  HVP (0-4095): "))
            hvm = int(input("  HVM (0-4095): "))
            hrp = int(input("  HRP (0-4095): "))
            hrm = int(input("  HRM (0-4095): "))
        except ValueError:
            self._err("Invalid input.")
            return False
        try:
            result = self.hv.set_dacs(hvp, hvm, hrp, hrm)
            self._ok("DACs set.") if result else self._err("Failed.")
            return result
        except Exception as exc:
            self._err(f"Exception: {exc}")
            return False

    def test_hv_enable(self):
        choice = input("  Enable HV output? [y/N]: ").strip().lower()
        enable = (choice == "y")
        try:
            result = self.hv.hv_enable(enable)
            self._ok(f"HV output {'enabled' if enable else 'disabled'}.")
            return result
        except Exception as exc:
            self._err(f"Exception: {exc}")
            return False

    def test_wait_for_settle(self):
        print("Waiting for HV to settle (timeout 15 s)...")
        try:
            result = self.hv.wait_for_settle()
            self._ok("Voltage settled.") if result else self._err("Did not settle.")
            return result
        except Exception as exc:
            self._err(f"Exception: {exc}")
            return False

    def run_all(self):
        """Read-only sweep — does not enable HV or change configuration."""
        readonly = [
            "Ping Device", "Get Firmware Version",
            "Get Temperature 1", "Get Temperature 2",
            "Get 12V Status", "Get HV Status", "Get HV Voltage",
            "Get Fan Speed", "Get RGB LED", "Get VMON Values",
        ]
        print("\n=== Running All Read-Only Tests ===")
        for label, fn in self.menu_items:
            if label not in readonly:
                continue
            print(f"\n[{label}]")
            try:
                fn()
            except Exception as exc:
                self._err(f"Exception: {exc}")

    # ------------------------------------------------------------------
    # Menu loop
    # ------------------------------------------------------------------
    def run_menu(self):
        while True:
            print("\n" + "=" * 46)
            print("   HVController Interactive Test Menu")
            print("=" * 46)
            for idx, (label, _) in enumerate(self.menu_items, 1):
                print(f"  {idx:2d}. {label}")
            print("   0. Exit")
            choice = input("Select: ").strip()
            if choice == "0":
                print("Goodbye.")
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


# ===========================================================================
# Hardware connection helper
# ===========================================================================
def _connect_hv_controller() -> HVController:
    from openlifu_sdk.io.LIFUInterface import LIFUInterface

    print("Connecting to LIFU console (HV controller)...")
    iface = LIFUInterface()
    tx_connected, hv_connected = iface.is_device_connected()
    if not hv_connected:
        print("HV controller not connected. Exiting.")
        sys.exit(1)
    print(f"TX connected: {tx_connected}  |  HV connected: {hv_connected}")
    return iface.hvcontroller


# ===========================================================================
# Entry point
# ===========================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HVController Test Suite")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Run menu-driven tests against real hardware",
    )
    args, remaining = parser.parse_known_args()

    if args.interactive:
        hv = _connect_hv_controller()
        suite = HVControllerInteractiveTests(hv)
        try:
            suite.run_menu()
        finally:
            try:
                hv.close()
            except Exception:
                pass
    else:
        unittest.main(argv=[sys.argv[0]] + remaining)
