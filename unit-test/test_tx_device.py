"""
TxDevice Test Suite
===================
Supports two modes:

  Unit test (mocked UART, no hardware required):
    python -m pytest unit-test/test_tx_device.py -v
    -- or --
    python unit-test/test_tx_device.py

  Interactive menu-driven tests (real hardware):
    python unit-test/test_tx_device.py --interactive
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import time
import unittest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Ensure src/ is importable when run directly from the repo root
# ---------------------------------------------------------------------------
import os
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from openlifu_sdk.io.LIFUConfig import (
    OW_ERROR,
    OW_RESP,
    OW_TX7332,
    TRIGGER_MODE_CONTINUOUS,
    TRIGGER_MODE_SEQUENCE,
    TRIGGER_MODE_SINGLE,
)
from openlifu_sdk.io.LIFUTXDevice import (
    ADDRESS_GLOBAL_MODE,
    HW_ID_DATA_LENGTH,
    TEMPERATURE_DATA_LENGTH,
    TxDevice,
)
from openlifu_sdk.io.LIFUUart import LIFUUart


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


# ===========================================================================
# Unit Tests  (pytest / unittest)
# ===========================================================================
class TestTxDeviceUnit(unittest.TestCase):
    """Unit tests for TxDevice using a fully mocked LIFUUart."""

    def setUp(self):
        self.uart = MagicMock(spec=LIFUUart)
        self.uart.demo_mode = False
        self.uart.asyncMode = False
        self.uart.is_connected.return_value = True
        self.uart.clear_buffer.return_value = None
        self.tx = TxDevice(uart=self.uart)

    # --- connection state ---------------------------------------------------

    def test_01_is_connected_true(self):
        """is_connected() mirrors UART state (True)."""
        self.uart.is_connected.return_value = True
        self.assertTrue(self.tx.is_connected())

    def test_02_is_connected_false(self):
        """is_connected() mirrors UART state (False)."""
        self.uart.is_connected.return_value = False
        self.assertFalse(self.tx.is_connected())

    # --- ping ---------------------------------------------------------------

    def test_03_ping_success(self):
        """ping() returns True on a non-error response."""
        self.uart.send_packet.return_value = _make_packet(b"\x00")
        self.assertTrue(self.tx.ping())

    def test_04_ping_error_packet(self):
        """ping() returns False when device replies with OW_ERROR."""
        self.uart.send_packet.return_value = _make_packet(packet_type=OW_ERROR)
        self.assertFalse(self.tx.ping())

    def test_05_ping_disconnected(self):
        """ping() returns False when UART is not connected."""
        self.uart.is_connected.return_value = False
        self.assertFalse(self.tx.ping())

    # --- version ------------------------------------------------------------

    def test_06_get_version_3byte(self):
        """get_version() parses a 3-byte version payload as vX.Y.Z."""
        self.uart.send_packet.return_value = _make_packet(b"\x01\x02\x03")
        self.assertEqual(self.tx.get_version(), "v1.2.3")

    def test_07_get_version_disconnected(self):
        """get_version() returns 'v0.0.0' when not connected."""
        self.uart.is_connected.return_value = False
        self.assertEqual(self.tx.get_version(), "v0.0.0")

    # --- echo ---------------------------------------------------------------

    def test_08_echo_round_trip(self):
        """echo() returns the same data payload it was given."""
        payload = b"\xDE\xAD\xBE\xEF"
        self.uart.send_packet.return_value = _make_packet(payload)
        echoed, length = self.tx.echo(echo_data=bytearray(payload))
        self.assertEqual(bytes(echoed), payload)
        self.assertEqual(length, len(payload))

    def test_09_echo_invalid_type_raises(self):
        """echo() raises TypeError when echo_data is not bytes/bytearray."""
        with self.assertRaises(TypeError):
            self.tx.echo(echo_data="not bytes")

    # --- toggle LED ---------------------------------------------------------

    def test_10_toggle_led_success(self):
        """toggle_led() returns True on any non-error response."""
        self.uart.send_packet.return_value = _make_packet(b"\x01")
        self.assertTrue(self.tx.toggle_led())

    def test_11_toggle_led_disconnected(self):
        """toggle_led() returns False when UART is not connected."""
        self.uart.is_connected.return_value = False
        self.assertFalse(self.tx.toggle_led())

    # --- hardware ID --------------------------------------------------------

    def test_12_get_hardware_id_valid(self):
        """get_hardware_id() returns a hex string when 12 bytes returned."""
        hw_id = bytes(range(HW_ID_DATA_LENGTH))
        self.uart.send_packet.return_value = _make_packet(hw_id)
        result = self.tx.get_hardware_id()
        self.assertIsNotNone(result)
        self.assertEqual(result, hw_id.hex())

    def test_13_get_hardware_id_wrong_length(self):
        """get_hardware_id() returns None when payload length is wrong."""
        self.uart.send_packet.return_value = _make_packet(b"\x01\x02")  # only 2 bytes
        result = self.tx.get_hardware_id()
        self.assertIsNone(result)

    def test_14_get_hardware_id_disconnected(self):
        """get_hardware_id() returns None when not connected."""
        self.uart.is_connected.return_value = False
        self.assertIsNone(self.tx.get_hardware_id())

    # --- temperature --------------------------------------------------------

    def test_15_get_temperature_valid(self):
        """get_temperature() decodes a little-endian float correctly."""
        expected = 36.6
        self.uart.send_packet.return_value = _make_packet(struct.pack("<f", expected))
        result = self.tx.get_temperature()
        self.assertAlmostEqual(result, expected, places=1)

    def test_16_get_temperature_disconnected(self):
        """get_temperature() returns 0 when not connected."""
        self.uart.is_connected.return_value = False
        self.assertEqual(self.tx.get_temperature(), 0)

    def test_17_get_ambient_temperature_valid(self):
        """get_ambient_temperature() decodes a little-endian float correctly."""
        expected = 22.5
        self.uart.send_packet.return_value = _make_packet(struct.pack("<f", expected))
        result = self.tx.get_ambient_temperature()
        self.assertAlmostEqual(result, expected, places=1)

    def test_18_get_ambient_temperature_disconnected(self):
        """get_ambient_temperature() returns 0 when not connected."""
        self.uart.is_connected.return_value = False
        self.assertEqual(self.tx.get_ambient_temperature(), 0)

    # --- set_trigger_json ---------------------------------------------------

    def test_19_set_trigger_json_sequence(self):
        """set_trigger_json() returns parsed JSON dict on success (SEQUENCE mode)."""
        response = {"TriggerFrequencyHz": 10.0, "TriggerMode": TRIGGER_MODE_SEQUENCE,
                    "TriggerStatus": "READY"}
        self.uart.send_packet.return_value = _make_packet(json.dumps(response).encode())
        result = self.tx.set_trigger_json(data={
            "TriggerFrequencyHz": 10.0,
            "TriggerPulseCount": 5,
            "TriggerPulseWidthUsec": 20,
            "TriggerPulseTrainInterval": 0,
            "TriggerPulseTrainCount": 1,
            "TriggerMode": TRIGGER_MODE_SEQUENCE,
            "ProfileIndex": 0,
            "ProfileIncrement": 0,
        })
        self.assertIsNotNone(result)
        self.assertEqual(result["TriggerMode"], TRIGGER_MODE_SEQUENCE)

    def test_20_set_trigger_json_none_data(self):
        """set_trigger_json() returns None when data is None."""
        result = self.tx.set_trigger_json(data=None)
        self.assertIsNone(result)

    def test_21_set_trigger_json_disconnected(self):
        """set_trigger_json() raises ValueError when not connected."""
        self.uart.is_connected.return_value = False
        with self.assertRaises(ValueError):
            self.tx.set_trigger_json(data={"TriggerFrequencyHz": 1.0})

    # --- set_trigger (high-level) ------------------------------------------

    def test_22_set_trigger_sequence_mode(self):
        """set_trigger() builds correct JSON payload for 'sequence' mode."""
        response = {"TriggerFrequencyHz": 10.0, "TriggerMode": TRIGGER_MODE_SEQUENCE}
        self.uart.send_packet.return_value = _make_packet(json.dumps(response).encode())
        result = self.tx.set_trigger(pulse_interval=0.1, pulse_count=5,
                                     pulse_width=20, trigger_mode="sequence")
        self.assertIsNotNone(result)

    def test_23_set_trigger_invalid_mode_raises(self):
        """set_trigger() raises ValueError for an unknown trigger mode."""
        with self.assertRaises(ValueError):
            self.tx.set_trigger(pulse_interval=0.1, trigger_mode="invalid")

    # --- start / stop trigger -----------------------------------------------

    def test_24_start_trigger_success(self):
        """start_trigger() returns True on a non-error response."""
        self.uart.send_packet.return_value = _make_packet(b"\x00")
        self.assertTrue(self.tx.start_trigger())

    def test_25_start_trigger_error_packet(self):
        """start_trigger() returns False on OW_ERROR response."""
        self.uart.send_packet.return_value = _make_packet(packet_type=OW_ERROR)
        self.assertFalse(self.tx.start_trigger())

    def test_26_stop_trigger_success(self):
        """stop_trigger() returns True on a non-error response."""
        self.uart.send_packet.return_value = _make_packet(b"\x00")
        self.assertTrue(self.tx.stop_trigger())

    def test_27_stop_trigger_error_packet(self):
        """stop_trigger() returns False on OW_ERROR response."""
        self.uart.send_packet.return_value = _make_packet(packet_type=OW_ERROR)
        self.assertFalse(self.tx.stop_trigger())

    # --- get_trigger_json ---------------------------------------------------

    def test_28_get_trigger_json_valid(self):
        """get_trigger_json() returns parsed dict from JSON response."""
        payload = {"TriggerFrequencyHz": 10.0, "TriggerMode": TRIGGER_MODE_SEQUENCE,
                   "TriggerStatus": "STOPPED"}
        self.uart.send_packet.return_value = _make_packet(json.dumps(payload).encode())
        result = self.tx.get_trigger_json()
        self.assertIsNotNone(result)
        self.assertEqual(result["TriggerStatus"], "STOPPED")

    # --- soft reset ---------------------------------------------------------

    def test_29_soft_reset_success(self):
        """soft_reset() returns True on success."""
        self.uart.send_packet.return_value = _make_packet(b"\x00")
        self.assertTrue(self.tx.soft_reset())

    def test_30_soft_reset_disconnected(self):
        """soft_reset() raises ValueError when not connected."""
        self.uart.is_connected.return_value = False
        with self.assertRaises(ValueError):
            self.tx.soft_reset()


# ===========================================================================
# TX7332 Command Unit Tests
# ===========================================================================
class TestTX7332Commands(unittest.TestCase):
    """
    Unit tests for TxDevice TX7332 register commands.

    Each test maps directly to a case in the firmware TX7332_ProcessCommand
    switch statement and verifies the Python SDK sends the correct packet
    and interprets the response correctly.
    """

    def setUp(self):
        self.uart = MagicMock(spec=LIFUUart)
        self.uart.demo_mode = False
        self.uart.asyncMode = False
        self.uart.is_connected.return_value = True
        self.uart.clear_buffer.return_value = None
        self.tx = TxDevice(uart=self.uart)
        self.chip_id = 0   # TX7332 chip index 0

    # --- OW_TX7332_DEVICE_COUNT ----------------------------------------

    def test_t01_get_tx_module_count_success(self):
        """get_tx_module_count(): firmware returns 1-byte count in data."""
        self.uart.send_packet.return_value = _make_packet(data=b"\x02")
        result = self.tx.get_tx_module_count()
        self.assertEqual(result, 2)

    def test_t02_get_tx_module_count_error_packet(self):
        """get_tx_module_count(): returns 0 on OW_ERROR response."""
        self.uart.send_packet.return_value = _make_packet(packet_type=OW_ERROR)
        result = self.tx.get_tx_module_count()
        self.assertEqual(result, 0)

    def test_t03_get_tx_module_count_disconnected(self):
        """get_tx_module_count(): raises ValueError when not connected."""
        self.uart.is_connected.return_value = False
        with self.assertRaises(ValueError):
            self.tx.get_tx_module_count()

    # --- OW_TX7332_ENUM -------------------------------------------------

    def test_t04_enum_devices_success(self):
        """enum_tx7332_devices(): firmware returns chip count in reserved field."""
        self.uart.send_packet.return_value = _make_packet(reserved=2)
        result = self.tx.enum_tx7332_devices()
        self.assertEqual(result, 2)

    def test_t05_enum_devices_expected_count_match(self):
        """enum_tx7332_devices(): passes when detected == num_devices."""
        self.uart.send_packet.return_value = _make_packet(reserved=2)
        result = self.tx.enum_tx7332_devices(num_devices=2)
        self.assertEqual(result, 2)

    def test_t06_enum_devices_count_mismatch_raises(self):
        """enum_tx7332_devices(): raises ValueError when detected != num_devices."""
        self.uart.send_packet.return_value = _make_packet(reserved=1)
        with self.assertRaises(ValueError):
            self.tx.enum_tx7332_devices(num_devices=2)

    def test_t07_enum_devices_disconnected(self):
        """enum_tx7332_devices(): raises ValueError when not connected."""
        self.uart.is_connected.return_value = False
        with self.assertRaises(ValueError):
            self.tx.enum_tx7332_devices()

    # --- OW_TX7332_DEMO -------------------------------------------------

    def test_t08_demo_tx7332_success(self):
        """demo_tx7332(): returns True on non-error response."""
        self.uart.send_packet.return_value = _make_packet(b"\x00")
        self.assertTrue(self.tx.demo_tx7332(self.chip_id))

    def test_t09_demo_tx7332_error_packet(self):
        """demo_tx7332(): returns False on OW_ERROR."""
        self.uart.send_packet.return_value = _make_packet(packet_type=OW_ERROR)
        self.assertFalse(self.tx.demo_tx7332(self.chip_id))

    def test_t10_demo_tx7332_disconnected(self):
        """demo_tx7332(): raises ValueError when not connected."""
        self.uart.is_connected.return_value = False
        with self.assertRaises(ValueError):
            self.tx.demo_tx7332(self.chip_id)

    # --- OW_TX7332_WREG -------------------------------------------------

    def test_t11_write_register_success(self):
        """write_register(): returns True on success."""
        self.uart.send_packet.return_value = _make_packet()
        self.assertTrue(self.tx.write_register(self.chip_id, 0x0010, 0xDEADBEEF))

    def test_t12_write_register_payload_format(self):
        """
        write_register(): firmware expects exactly 6 bytes:
          [addr_lo, addr_hi, val_b0, val_b1, val_b2, val_b3] (little-endian).
        """
        address = 0x001F
        value   = 0x12345678
        self.uart.send_packet.return_value = _make_packet()
        self.tx.write_register(self.chip_id, address, value)
        _, kwargs = self.uart.send_packet.call_args
        sent_data = kwargs.get("data") or self.uart.send_packet.call_args[0][-1]
        expected = struct.pack('<HI', address, value)   # 2 + 4 = 6 bytes
        self.assertEqual(bytes(sent_data), expected)

    def test_t13_write_register_error_packet(self):
        """write_register(): returns False on OW_ERROR response."""
        self.uart.send_packet.return_value = _make_packet(packet_type=OW_ERROR)
        self.assertFalse(self.tx.write_register(self.chip_id, 0x0010, 0x1234))

    def test_t14_write_register_negative_identifier(self):
        """write_register(): raises ValueError for negative chip identifier."""
        with self.assertRaises(ValueError):
            self.tx.write_register(-1, 0x0010, 0x1234)

    def test_t15_write_register_disconnected(self):
        """write_register(): raises ValueError when not connected."""
        self.uart.is_connected.return_value = False
        with self.assertRaises(ValueError):
            self.tx.write_register(self.chip_id, 0x0010, 0x1234)

    # --- OW_TX7332_RREG -------------------------------------------------

    def test_t16_read_register_success(self):
        """read_register(): decodes 4-byte LE uint32 from response."""
        expected_val = 0xCAFEBABE
        self.uart.send_packet.return_value = _make_packet(
            struct.pack('<I', expected_val)
        )
        result = self.tx.read_register(self.chip_id, 0x0010)
        self.assertEqual(result, expected_val)

    def test_t17_read_register_payload_format(self):
        """
        read_register(): firmware expects exactly 2 bytes (little-endian address).
        """
        address = 0x001B
        self.uart.send_packet.return_value = _make_packet(
            struct.pack('<I', 0xAABBCCDD)
        )
        self.tx.read_register(self.chip_id, address)
        _, kwargs = self.uart.send_packet.call_args
        sent_data = kwargs.get("data") or self.uart.send_packet.call_args[0][-1]
        self.assertEqual(bytes(sent_data), struct.pack('<H', address))   # 2 bytes

    def test_t18_read_register_error_packet(self):
        """read_register(): returns 0 on OW_ERROR response."""
        self.uart.send_packet.return_value = _make_packet(packet_type=OW_ERROR)
        self.assertEqual(self.tx.read_register(self.chip_id, 0x0010), 0)

    def test_t19_read_register_wrong_data_length(self):
        """read_register(): returns 0 when response is not exactly 4 bytes."""
        self.uart.send_packet.return_value = _make_packet(b"\x01\x02")  # 2 bytes
        self.assertEqual(self.tx.read_register(self.chip_id, 0x0010), 0)

    def test_t20_read_register_negative_identifier(self):
        """read_register(): raises ValueError for negative chip identifier."""
        with self.assertRaises(ValueError):
            self.tx.read_register(-1, 0x0010)

    def test_t21_read_register_disconnected(self):
        """read_register(): raises ValueError when not connected."""
        self.uart.is_connected.return_value = False
        with self.assertRaises(ValueError):
            self.tx.read_register(self.chip_id, 0x0010)

    # --- OW_TX7332_WBLOCK ----------------------------------------------

    def test_t22_write_block_success_single_chunk(self):
        """write_block(): single chunk (<= 62 regs) succeeds."""
        self.uart.send_packet.return_value = _make_packet()
        values = list(range(4))   # 4 registers
        self.assertTrue(self.tx.write_block(self.chip_id, 0x0020, values))
        self.assertEqual(self.uart.send_packet.call_count, 1)

    def test_t23_write_block_payload_format(self):
        """
        write_block(): firmware expects [addr_lo, addr_hi, count, dummy, val0..valN]
        in little-endian format (<HBB + count*I).
        """
        start_addr = 0x0020
        values = [0x11111111, 0x22222222, 0x33333333]
        self.uart.send_packet.return_value = _make_packet()
        self.tx.write_block(self.chip_id, start_addr, values)
        _, kwargs = self.uart.send_packet.call_args
        sent_data = kwargs.get("data") or self.uart.send_packet.call_args[0][-1]
        expected = struct.pack('<HBB' + 'I' * len(values),
                               start_addr, len(values), 0, *values)
        self.assertEqual(bytes(sent_data), expected)

    def test_t24_write_block_multi_chunk(self):
        """write_block(): 70 regs splits into 2 chunks (ceil(70/62)=2)."""
        self.uart.send_packet.return_value = _make_packet()
        values = [i for i in range(70)]
        self.assertTrue(self.tx.write_block(self.chip_id, 0x0020, values))
        self.assertEqual(self.uart.send_packet.call_count, 2)

    def test_t25_write_block_error_on_first_chunk(self):
        """write_block(): returns False when first chunk gets OW_ERROR."""
        self.uart.send_packet.return_value = _make_packet(packet_type=OW_ERROR)
        self.assertFalse(self.tx.write_block(self.chip_id, 0x0020, [1, 2, 3]))

    def test_t26_write_block_empty_list_raises(self):
        """write_block(): raises ValueError for empty register list."""
        with self.assertRaises(ValueError):
            self.tx.write_block(self.chip_id, 0x0020, [])

    def test_t27_write_block_non_integer_values_raises(self):
        """write_block(): raises ValueError when list contains non-integers."""
        with self.assertRaises(ValueError):
            self.tx.write_block(self.chip_id, 0x0020, [1, 2, 3.5])

    def test_t28_write_block_negative_identifier(self):
        """write_block(): raises ValueError for negative chip identifier."""
        with self.assertRaises(ValueError):
            self.tx.write_block(-1, 0x0020, [1, 2, 3])

    def test_t29_write_block_disconnected(self):
        """write_block(): raises ValueError when not connected."""
        self.uart.is_connected.return_value = False
        with self.assertRaises(ValueError):
            self.tx.write_block(self.chip_id, 0x0020, [1, 2, 3])

    # --- Register round-trip sanity check --------------------------------

    def test_t30_write_then_read_register_roundtrip(self):
        """
        Simulates a write followed by a read of the same address/value.
        Verifies the payload for write (6 bytes) and the read returns the
        correct decoded value — matching the firmware WREG/RREG logic.
        """
        address = ADDRESS_GLOBAL_MODE   # 0x0000
        value   = 0xABCD1234

        # --- write ---
        self.uart.send_packet.return_value = _make_packet()
        write_ok = self.tx.write_register(self.chip_id, address, value)
        self.assertTrue(write_ok)
        _, kwargs_w = self.uart.send_packet.call_args
        sent = kwargs_w.get("data") or self.uart.send_packet.call_args[0][-1]
        self.assertEqual(bytes(sent), struct.pack('<HI', address, value))

        # --- read ---
        self.uart.send_packet.return_value = _make_packet(
            struct.pack('<I', value)
        )
        read_val = self.tx.read_register(self.chip_id, address)
        self.assertEqual(read_val, value)


# ===========================================================================
# Interactive (menu-driven) Tests  — real hardware
# ===========================================================================
class TxDeviceInteractiveTests:
    """Menu-driven tests that exercise TxDevice against real connected hardware."""

    def __init__(self, tx_device: TxDevice):
        self.tx = tx_device
        self.menu_items = [
            ("Ping Device",                    self.test_ping),
            ("Get Firmware Version",           self.test_get_version),
            ("Echo Test",                      self.test_echo),
            ("Toggle LED",                     self.test_toggle_led),
            ("Get Hardware ID",                self.test_get_hardware_id),
            ("Get Core Temperature",           self.test_get_temperature),
            ("Get Ambient Temperature",        self.test_get_ambient_temperature),
            ("Set Trigger – SEQUENCE mode",    self.test_trigger_sequence),
            ("Set Trigger – SINGLE mode",      self.test_trigger_single),
            ("Set Trigger – CONTINUOUS mode",  self.test_trigger_continuous),
            ("Soft Reset",                     self.test_soft_reset),
            # TX7332 register commands
            ("TX7332: Get Module Count",        self.test_tx7332_get_module_count),
            ("TX7332: Enumerate Devices",       self.test_tx7332_enum_devices),
            ("TX7332: Demo Pattern",            self.test_tx7332_demo),
            ("TX7332: Write Register",          self.test_tx7332_write_register),
            ("TX7332: Read Register",           self.test_tx7332_read_register),
            ("TX7332: Write Block",             self.test_tx7332_write_block),
            ("TX7332: Register Round-Trip",     self.test_tx7332_roundtrip),
            ("Run All Tests",                  self.run_all),
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
        print("Pinging device...")
        result = self.tx.ping()
        self._ok("Device responded.") if result else self._err("No response.")
        return result

    def test_get_version(self):
        print("Reading firmware version...")
        ver = self.tx.get_version()
        self._ok(f"Version: {ver}")
        return ver

    def test_echo(self):
        payload = bytearray(b"\xDE\xAD\xBE\xEF\x01\x02\x03\x04")
        print(f"Sending echo: {payload.hex()}")
        echoed, length = self.tx.echo(echo_data=payload)
        if echoed and bytes(echoed[:length]) == bytes(payload):
            self._ok(f"Echo matched: {bytes(echoed[:length]).hex()}")
            return True
        self._err(f"Echo mismatch. Got: {bytes(echoed[:length]).hex() if echoed else 'None'}")
        return False

    def test_toggle_led(self):
        print("Toggling LED...")
        result = self.tx.toggle_led()
        self._ok("LED toggled.") if result else self._err("Toggle failed.")
        return result

    def test_get_hardware_id(self):
        print("Reading hardware ID...")
        hw_id = self.tx.get_hardware_id()
        if hw_id:
            self._ok(f"Hardware ID: {hw_id}")
        else:
            self._err("No hardware ID returned.")
        return hw_id

    def test_get_temperature(self):
        print("Reading core temperature...")
        temp = self.tx.get_temperature()
        self._ok(f"Core temperature: {temp:.2f} °C")
        return temp

    def test_get_ambient_temperature(self):
        print("Reading ambient temperature...")
        temp = self.tx.get_ambient_temperature()
        self._ok(f"Ambient temperature: {temp:.2f} °C")
        return temp

    def _run_trigger(self, mode_label: str, mode_int: int,
                     freq_hz: float = 10.0, pulse_count: int = 5,
                     pulse_width_us: int = 20,
                     train_interval_us: int = 600000,
                     train_count: int = 3) -> bool:
        print(f"Setting trigger [{mode_label}]  "
              f"{freq_hz} Hz | {pulse_count} pulses | {pulse_width_us} µs width ...")
        trigger_data = {
            "TriggerFrequencyHz": freq_hz,
            "TriggerPulseCount": pulse_count,
            "TriggerPulseWidthUsec": pulse_width_us,
            "TriggerPulseTrainInterval": train_interval_us,
            "TriggerPulseTrainCount": train_count,
            "TriggerMode": mode_int,
            "ProfileIndex": 0,
            "ProfileIncrement": 0,
        }
        result = self.tx.set_trigger_json(data=trigger_data)
        if not result:
            self._err("Failed to set trigger.")
            return False
        self._ok(f"Trigger configured: {result}")

        print("Starting trigger...")
        if not self.tx.start_trigger():
            self._err("Failed to start trigger.")
            return False
        self._ok("Trigger started.")

        if mode_int == TRIGGER_MODE_CONTINUOUS:
            input("  Running CONTINUOUS — press Enter to stop...")
            stopped = self.tx.stop_trigger()
            self._ok("Stopped.") if stopped else self._err("Stop failed.")
            return stopped

        if mode_int == TRIGGER_MODE_SEQUENCE:
            print("  Waiting for sequence to complete (max 15 s)...")
            for _ in range(30):
                status = self.tx.get_trigger_json()
                if status and status.get("TriggerStatus") == "STOPPED":
                    self._ok("Sequence complete.")
                    return True
                time.sleep(0.5)
            self._err("Timeout waiting for sequence to finish.")
            return False

        if mode_int == TRIGGER_MODE_SINGLE:
            self._ok("Single shot fired.")
            return True

        return True

    def test_trigger_sequence(self):
        return self._run_trigger("SEQUENCE", TRIGGER_MODE_SEQUENCE)

    def test_trigger_single(self):
        return self._run_trigger("SINGLE", TRIGGER_MODE_SINGLE, pulse_count=1)

    def test_trigger_continuous(self):
        return self._run_trigger("CONTINUOUS", TRIGGER_MODE_CONTINUOUS)

    def test_soft_reset(self):
        confirm = input("  Soft reset will restart the firmware. Continue? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            return None
        print("Resetting device...")
        result = self.tx.soft_reset()
        self._ok("Reset successful.") if result else self._err("Reset failed.")
        return result

    # ------------------------------------------------------------------
    # TX7332 Commands
    # ------------------------------------------------------------------

    def _prompt_chip_id(self) -> int:
        raw = input("  TX7332 chip index (0-based, default 0): ").strip()
        return int(raw) if raw else 0

    def test_tx7332_get_module_count(self):
        print("Getting TX module count...")
        count = self.tx.get_tx_module_count()
        self._ok(f"TX module count: {count}")
        return count

    def test_tx7332_enum_devices(self):
        print("Enumerating TX7332 devices...")
        n = self.tx.enum_tx7332_devices()
        self._ok(f"Detected {n} TX7332 chip(s).")
        return n

    def test_tx7332_demo(self):
        chip_id = self._prompt_chip_id()
        print(f"Writing demo pattern to TX7332 chip {chip_id}...")
        result = self.tx.demo_tx7332(chip_id)
        self._ok("Demo pattern written.") if result else self._err("Demo write failed.")
        return result

    def test_tx7332_write_register(self):
        chip_id = self._prompt_chip_id()
        try:
            addr  = int(input("  Register address (hex, e.g. 0x0010): "), 16)
            value = int(input("  Register value   (hex, e.g. 0xDEADBEEF): "), 16)
        except ValueError:
            self._err("Invalid input — enter hex values.")
            return False
        print(f"  Writing 0x{value:08X} → address 0x{addr:04X} on chip {chip_id}...")
        result = self.tx.write_register(chip_id, addr, value)
        self._ok("Write successful.") if result else self._err("Write failed.")
        return result

    def test_tx7332_read_register(self):
        chip_id = self._prompt_chip_id()
        try:
            addr = int(input("  Register address (hex, e.g. 0x0010): "), 16)
        except ValueError:
            self._err("Invalid input — enter a hex address.")
            return None
        print(f"  Reading address 0x{addr:04X} on chip {chip_id}...")
        value = self.tx.read_register(chip_id, addr)
        self._ok(f"Register 0x{addr:04X} = 0x{value:08X}")
        return value

    def test_tx7332_write_block(self):
        chip_id = self._prompt_chip_id()
        try:
            start_addr = int(input("  Start address (hex, e.g. 0x0020): "), 16)
            count      = int(input("  Number of registers to write: "))
            raw_vals   = input(f"  Enter {count} hex values separated by spaces: ").split()
            if len(raw_vals) != count:
                self._err(f"Expected {count} values, got {len(raw_vals)}.")
                return False
            values = [int(v, 16) for v in raw_vals]
        except ValueError:
            self._err("Invalid input.")
            return False
        print(f"  Writing {count} registers starting at 0x{start_addr:04X} on chip {chip_id}...")
        result = self.tx.write_block(chip_id, start_addr, values)
        self._ok("Block write successful.") if result else self._err("Block write failed.")
        return result

    def test_tx7332_roundtrip(self):
        chip_id = self._prompt_chip_id()
        try:
            addr  = int(input("  Register address (hex, e.g. 0x0010): "), 16)
            value = int(input("  Value to write   (hex, e.g. 0x12345678): "), 16)
        except ValueError:
            self._err("Invalid input.")
            return False
        print(f"  Writing 0x{value:08X} to 0x{addr:04X}...")
        if not self.tx.write_register(chip_id, addr, value):
            self._err("Write failed.")
            return False
        print(f"  Reading back 0x{addr:04X}...")
        read_val = self.tx.read_register(chip_id, addr)
        if read_val == value:
            self._ok(f"Round-trip OK: 0x{read_val:08X}")
            return True
        self._err(f"Mismatch: wrote 0x{value:08X}, read 0x{read_val:08X}")
        return False

    def run_all(self):
        print("\n=== Running All Tests ===")
        for label, fn in self.menu_items[:-1]:  # exclude 'Run All' itself
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
            print("   TxDevice Interactive Test Menu")
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
def _connect_tx_device() -> TxDevice:
    from openlifu_sdk.io.LIFUInterface import LIFUInterface

    print("Connecting to LIFU TX device...")
    iface = LIFUInterface()
    tx_connected, hv_connected = iface.is_device_connected()
    if not tx_connected:
        print("TX device not connected. Exiting.")
        sys.exit(1)
    print(f"TX connected: {tx_connected}  |  HV connected: {hv_connected}")
    return iface.txdevice


# ===========================================================================
# Entry point
# ===========================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TxDevice Test Suite")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Run menu-driven tests against real hardware",
    )
    args, remaining = parser.parse_known_args()

    if args.interactive:
        tx = _connect_tx_device()
        suite = TxDeviceInteractiveTests(tx)
        suite.run_menu()
    else:
        # Pass remaining argv to unittest so pytest-style -v etc. still work
        unittest.main(argv=[sys.argv[0]] + remaining)
