"""
FDA preset limit tests (no hardware)
====================================
The thermal limits a preset bakes into the TX image, the per-preset HV table
the console is built with, and the device replies that report them back.

    python -m pytest unit-test/test_fda_presets.py -v
"""
from __future__ import annotations

import json
import os
import re
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from openlifu_sdk.io.LIFUConfig import OW_CMD_ECHO, OW_POWER_SET_HV, OW_RESP, OW_TX7332_WREG
from openlifu_sdk.io.LIFUHVController import HVController, LIFUHVController
from openlifu_sdk.io.LIFUInterface import DeviceInterface, LIFUInterface, LIFUInterfaceStatus
from openlifu_sdk.io.LIFUTXDevice import LIFUTxDevice, TxDevice
from openlifu_sdk.io.LIFUTXPresets import (
    compile_preset,
    generate_console_presets,
    generate_header_files,
    generate_preset_set,
    parse_preset_blob,
    preset_blob,
    preset_voltage,
    regs_crc,
    thermal_limits,
    verify_console_preset,
)
from openlifu_sdk.io.LIFUUserConfig import LifuUserConfig
from openlifu_sdk.io.uart import OWUart


def _preset(**overrides) -> dict:
    """A minimal two-chip, single-profile preset with every required field."""
    doc = {
        "id": "unit",
        "frequency_khz": 400.0,
        "pulse_length_us": 20.0,
        "pulse_interval_ms": 100.0,
        "pulse_count": 10,
        "pulse_train_interval_s": 0,
        "pulse_train_count_selections": [1, 5],
        "delays": [[0.0] * 64],
        "apodization": [[1.0] * 64],
        "start_C": 35,
        "shutoff_C": 75,
        "voltage": 20.0,
    }
    doc.update(overrides)
    return doc


def _compiled(**overrides) -> dict:
    doc = _preset(**overrides)
    return compile_preset(doc, settings_bytes=json.dumps(doc).encode())


def _packet(data: bytes) -> MagicMock:
    pkt = MagicMock()
    pkt.packet_type = OW_RESP
    pkt.data = bytearray(data)
    pkt.data_len = len(data)
    pkt.reserved = 0
    return pkt


def _mock_uart(component, desc: str) -> MagicMock:
    uart = MagicMock(spec=OWUart)
    uart.desc = desc
    uart.demo_mode = False
    uart.asyncMode = False
    uart.is_connected = True
    component._uart = uart
    return uart


class TestThermalLimits(unittest.TestCase):

    def test_raw_tenths(self):
        self.assertEqual(thermal_limits(_compiled(start_C=35, shutoff_C=75)), (350, 750))
        self.assertEqual(thermal_limits(_compiled(start_C=36.5, shutoff_C=41.2)), (365, 412))

    def test_missing_fields_named(self):
        doc = _preset()
        del doc["start_C"], doc["shutoff_C"]
        with self.assertRaisesRegex(ValueError, "start_C, shutoff_C"):
            compile_preset(doc)

    def test_refused_rather_than_rounded(self):
        with self.assertRaisesRegex(ValueError, "multiple of 0.1"):
            _compiled(start_C=35.25)

    def test_start_must_be_below_shutoff(self):
        with self.assertRaisesRegex(ValueError, "below shutoff_C"):
            _compiled(start_C=75, shutoff_C=75)

    def test_not_a_number(self):
        for bad in ("35", True, None, float("nan")):
            with self.assertRaises(ValueError, msg=repr(bad)):
                _compiled(start_C=bad)

    def test_regs_crc_covers_them(self):
        base = _compiled()
        self.assertNotEqual(regs_crc(base), regs_crc(_compiled(start_C=36)))
        self.assertNotEqual(regs_crc(base), regs_crc(_compiled(shutoff_C=74)))
        # i16 LE pair right after the run-length choices
        self.assertIn(struct.pack("<B2I", 2, 1, 5) + struct.pack("<hh", 350, 750), preset_blob(base))

    def test_blob_carries_them(self):
        got = parse_preset_blob(preset_blob(_compiled(start_C=36.5, shutoff_C=41)))
        self.assertEqual((got["start_c_x10"], got["shutoff_c_x10"]), (365, 410))


def _rastered(**overrides) -> dict:
    """Three delay profiles cycled 1, 2, 3, 1 -- per-profile registers and a real order."""
    return _compiled(delays=[[i * 1e-7] * 64 for i in range(3)], apodization=[[1.0] * 64] * 3,
                     order=[1, 2, 3, 1], pulse_count=8, **overrides)


class TestPresetBlob(unittest.TestCase):

    def test_round_trip(self):
        for mc in (_compiled(), _rastered()):
            got = parse_preset_blob(preset_blob(mc))
            self.assertEqual(got["chips"], mc["chips"])       # every register, run for run
            self.assertEqual(got["execution_order"], mc["execution_order"])
            self.assertEqual(got["profile_index"], mc["profile_index"])
            self.assertEqual(got["train_counts"], mc["pulse_train_count_selections"])
            self.assertEqual((got["trig_hz"], got["trig_count"], got["trig_train_us"]), (10, mc["pulse_count"], 0))
            self.assertEqual((got["id"], got["settings_crc"]), (mc["id"], mc["settings_crc"]))

    def test_regs_crc_is_the_body_crc(self):
        mc = _rastered()
        got = parse_preset_blob(preset_blob(mc))
        self.assertEqual(got["regs_crc"], regs_crc(mc))
        self.assertEqual(got["body_crc"], regs_crc(mc))

    def test_id_and_source_file_are_outside_regs_crc(self):
        a, b = _compiled(id="a"), _compiled(id="b", voltage=30)
        self.assertEqual(regs_crc(a), regs_crc(b))
        self.assertNotEqual(preset_blob(a), preset_blob(b))

    def test_header_layout(self):
        mc = _compiled(id="unit")
        blob = preset_blob(mc)
        self.assertEqual(blob[:8], b"OWPR" + bytes([1, 4, 0, 0]))
        self.assertEqual(struct.unpack_from("<III", blob, 8), (len(blob), mc["settings_crc"], regs_crc(mc)))
        self.assertEqual(blob[20:24], b"unit")

    def test_base_registers_are_runs(self):
        # A run costs its 4-byte header plus 4 bytes a value, against 6 a register as pairs.
        mc = _rastered()
        runs = sum(len(c["registers"]) for c in mc["chips"])
        values = sum(len(v) for c in mc["chips"] for v in c["registers"].values())
        self.assertLess(runs, values)
        self.assertLess(4 * runs + 4 * values, 6 * values)

    def test_needs_settings_crc_and_a_usable_id(self):
        with self.assertRaisesRegex(ValueError, "settings_crc"):
            preset_blob(compile_preset(_preset()))
        for bad in ("x" * 65, "café", "tab\there"):
            with self.assertRaisesRegex(ValueError, "printable ASCII", msg=repr(bad)):
                preset_blob(_compiled(id=bad))

    def test_parser_refuses_damage(self):
        blob = preset_blob(_rastered())
        for bad in (blob[:-1], blob + b"\0", b"XXXX" + blob[4:], blob[:4] + b"\x02" + blob[5:], blob[:40]):
            with self.assertRaises(ValueError):
                parse_preset_blob(bad)

    def test_header_file_is_the_blob(self):
        mc = _rastered()
        with tempfile.TemporaryDirectory() as d:
            text = generate_header_files(Path(d) / "p.h", mc, "set").read_text()
        body = text[text.index("{") + 1:text.rindex("}")]
        emitted = bytes(int(x, 16) for x in re.findall(r"0x([0-9a-f]{2}),", body))
        self.assertEqual(emitted, preset_blob(mc))
        self.assertIn("static const uint8_t PRESET_SET_UNIT_BLOB[%d] = {" % len(emitted), text)

    def test_table_points_at_the_blobs(self):
        presets = [_compiled(id="a"), _compiled(id="b")]
        with tempfile.TemporaryDirectory() as d:
            table = generate_preset_set(d, presets)[-1].read_text()
        self.assertIn("#define PRESET_COUNT 2u", table)
        self.assertIn("\t{ PRESET_A_BLOB, sizeof(PRESET_A_BLOB) },\n\t{ PRESET_B_BLOB, sizeof(PRESET_B_BLOB) },", table)


def _f32(v: float) -> float:
    return struct.unpack("<f", struct.pack("<f", v))[0]


class TestConsolePresets(unittest.TestCase):

    def test_voltage_is_float32(self):
        self.assertEqual(preset_voltage(_compiled(voltage=47.66997571)), _f32(47.66997571))

    def test_bad_voltage(self):
        for bad in (0, -5, "20", None, float("inf")):
            with self.assertRaises(ValueError, msg=repr(bad)):
                _compiled(voltage=bad)

    def test_table_in_index_order_with_exact_voltages(self):
        presets = [_compiled(id="a", voltage=47.66997571), _compiled(id="b", voltage=16.96599996)]
        with tempfile.TemporaryDirectory() as d:
            text = generate_console_presets(d, presets, "vet").read_text()
        rows = re.findall(r'\{ "(\w+)", (\S+)f, 0x([0-9a-f]{8})u \}', text)
        self.assertEqual([r[0] for r in rows], ["a", "b"])
        for (_, literal, crc), mc in zip(rows, presets):
            # The C literal parses back to the very float32 the preset compiles to.
            self.assertEqual(_f32(float(literal)), preset_voltage(mc))
            self.assertEqual(int(crc, 16), mc["settings_crc"])
        self.assertIn('#define PRESET_HV_CONTEXT_NAME "vet"', text)
        self.assertIn("#define PRESET_HV_COUNT 2u", text)
        self.assertNotIn("Every preset in this set uses", text)

    def test_shared_voltage_called_out(self):
        presets = [_compiled(id="a", voltage=30), _compiled(id="b", voltage=30)]
        with tempfile.TemporaryDirectory() as d:
            text = generate_console_presets(d, presets, "vet").read_text()
        self.assertIn("// Every preset in this set uses 30 V.", text)

    def test_needs_settings_crc(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaisesRegex(ValueError, "settings_crc"):
            generate_console_presets(d, [compile_preset(_preset())], "vet")


class TestDeviceReplies(unittest.TestCase):

    def _get_preset_reply(self, tail: bytes) -> bytes:
        ident = b"unit"
        return (bytes([3, 1, 2, 1]) + struct.pack("<III", 0x11, 0x22, 0x22)
                + bytes([len(ident)]) + ident + struct.pack("<B2I", 2, 188, 375) + tail)

    def test_get_preset_thermal_tail(self):
        tx = TxDevice()
        uart = _mock_uart(tx, "TX")
        uart.send_packet.return_value = _packet(self._get_preset_reply(struct.pack("<hh", 355, -12)))
        got = tx.get_preset(1)
        self.assertEqual((got["start_c"], got["shutoff_c"]), (35.5, -1.2))
        self.assertEqual(got["train_counts"], [188, 375])

    def test_get_preset_older_image(self):
        tx = TxDevice()
        uart = _mock_uart(tx, "TX")
        uart.send_packet.return_value = _packet(self._get_preset_reply(b""))
        got = tx.get_preset(1)
        self.assertIsNone(got["start_c"])
        self.assertEqual(got["train_counts"], [188, 375])

    @staticmethod
    def _console_reply(selected: int, crc: int, volts: float, ident: bytes = b"unit") -> bytes:
        return bytes([3, 1, selected]) + struct.pack("<If", crc, volts) + bytes([len(ident)]) + ident

    def test_console_get_preset(self):
        hv = HVController()
        uart = _mock_uart(hv, "HV")
        uart.send_packet.return_value = _packet(self._console_reply(0xFF, 0xABCD, 47.67))
        got = hv.get_preset(1)
        self.assertEqual((got["count"], got["index"], got["selected"]), (3, 1, None))
        self.assertEqual((got["settings_crc"], got["id"]), (0xABCD, "unit"))
        self.assertEqual(got["voltage"], _f32(47.67))

    def test_console_get_preset_non_fda(self):
        hv = HVController()
        uart = _mock_uart(hv, "HV")
        uart.send_packet.return_value = _packet(b"")
        self.assertIsNone(hv.get_preset(0))

    def test_console_select_preset(self):
        hv = HVController()
        uart = _mock_uart(hv, "HV")
        uart.send_packet.side_effect = [_packet(b""), _packet(self._console_reply(1, 0xABCD, 30.5))]
        hv.select_preset(1, 0xABCD)
        sent = uart.send_packet.call_args_list[0].kwargs
        self.assertEqual((sent["reserved"], bytes(sent["data"])), (1, struct.pack("<I", 0xABCD)))
        self.assertEqual(hv.supply_voltage, 30.5)   # wait_for_settle aims at it

    def test_console_select_preset_with_device_sensitivity(self):
        hv = HVController()
        uart = _mock_uart(hv, "HV")
        uart.send_packet.side_effect = [_packet(b""), _packet(self._console_reply(1, 0xABCD, 30.5))]
        hv.select_preset(1, 0xABCD, 2720.0, 400000.0)
        sent = uart.send_packet.call_args_list[0].kwargs
        self.assertEqual(sent["reserved"], 1)
        self.assertEqual(bytes(sent["data"]), struct.pack("<Iff", 0xABCD, 2720.0, 400000.0))
        self.assertEqual(hv.supply_voltage, 30.5)

    def test_console_select_preset_rejects_partial_scaling_args(self):
        hv = HVController()
        uart = _mock_uart(hv, "HV")
        with self.assertRaises(ValueError):
            hv.select_preset(1, 0xABCD, 2720.0)
        with self.assertRaises(ValueError):
            hv.select_preset(1, 0xABCD, frequency_hz=400000.0)
        uart.send_packet.assert_not_called()

    def test_verify_console_preset(self):
        mc = _compiled(id="a", voltage=47.66997571)
        hv = MagicMock()
        hv.get_preset.return_value = {"id": "a", "settings_crc": mc["settings_crc"],
                                      "voltage": preset_voltage(mc), "selected": None}
        verify_console_preset(hv, 0, mc)
        hv.get_preset.return_value = dict(hv.get_preset.return_value, voltage=47.0)
        with self.assertRaisesRegex(ValueError, "voltage"):
            verify_console_preset(hv, 0, mc)
        hv.get_preset.return_value = None
        with self.assertRaisesRegex(ValueError, "not an FDA_MODE image"):
            verify_console_preset(hv, 0, mc)


class TestTxMetadata(unittest.TestCase):

    def test_refresh_metadata_sets_identity_and_frequency_specific_sensitivity(self):
        tx = TxDevice()
        tx.get_module_count = MagicMock(return_value=2)
        tx.read_config = MagicMock(side_effect=[
            LifuUserConfig(json_data={
                "sn": "SN-001",
                "hwid": "HWID-001",
                "module": {
                    "frequency": 400000.0,
                    "sensitivity": [[350000.0, 2.0], [400000.0, 4.0], [450000.0, 6.0]],
                },
            }),
            LifuUserConfig(json_data={
                "sn": "SN-002",
                "hwid": "HWID-002",
                "module": {
                    "frequency": 400000.0,
                    "sensitivity": [[350000.0, 4.0], [400000.0, 8.0], [450000.0, 10.0]],
                },
            }),
        ])
        tx.get_hardware_id = MagicMock(return_value="FALLBACK-HWID")

        tx.refresh_metadata()

        self.assertEqual(tx.serial_number, "SN-001")
        self.assertEqual(tx.hwid, "HWID-001")
        self.assertEqual(tx.hardware_id, "HWID-001")
        self.assertEqual(len(tx.module_user_configs), 2)
        self.assertEqual(tx.get_sensitivity_for_frequency(400000.0), 6.0)
        self.assertEqual(tx.get_sensitivity_for_frequency(450000.0), 8.0)
        self.assertEqual(tx.get_sensitivity_for_frequency(425000.0), 7.0)


class TestInterfacePresetLoad(unittest.TestCase):

    def test_load_preset_orchestrates_tx_and_hv(self):
        iface = object.__new__(DeviceInterface)
        iface.txdevice = MagicMock()
        iface.txdevice.module_user_configs = [{"module": {"sensitivity": []}}]
        iface.txdevice.get_sensitivity_for_frequency.return_value = 2720.0
        iface.hvcontroller = MagicMock()
        iface.status = LIFUInterfaceStatus.STATUS_SYS_OFF

        iface.load_preset(1, 0xAABBCCDD, 0x11223344, 400000.0, duration_index=2)

        iface.txdevice.load_preset.assert_called_once_with(1, 0x11223344, duration_index=2)
        iface.txdevice.get_sensitivity_for_frequency.assert_called_once_with(400000.0)
        iface.hvcontroller.select_preset.assert_called_once_with(1, 0xAABBCCDD, 2720.0, 400000.0)
        self.assertEqual(iface.status, LIFUInterfaceStatus.STATUS_READY)

    def test_load_preset_refreshes_missing_metadata(self):
        iface = object.__new__(DeviceInterface)
        iface.txdevice = MagicMock()
        iface.txdevice.module_user_configs = []
        iface.txdevice.get_sensitivity_for_frequency.return_value = 3000.0
        iface.hvcontroller = MagicMock()
        iface.status = LIFUInterfaceStatus.STATUS_SYS_OFF

        iface.load_preset(0, 0xABCD, 0xDCBA, 500000.0)

        iface.txdevice.refresh_metadata.assert_called_once_with()
        iface.txdevice.get_sensitivity_for_frequency.assert_called_once_with(500000.0)
        iface.hvcontroller.select_preset.assert_called_once_with(0, 0xABCD, 3000.0, 500000.0)


class TestInterfaceSurface(unittest.TestCase):
    """DeviceInterface is the FDA surface; LIFUInterface adds the RUO endpoints."""

    RUO_ENDPOINTS = (
        "set_solution", "check_solution", "set_module_invert",
        "get_max_voltage", "get_max_voltage_table",
        "get_sequence_duty_cycle", "get_sequence_duration",
        "start_monitoring", "stop_monitoring",
    )
    FDA_ENDPOINTS = (
        "load_preset", "start_sonication", "stop_sonication", "is_running",
        "get_status", "is_device_connected", "close",
    )

    def test_lifu_interface_extends_device_interface(self):
        self.assertTrue(issubclass(LIFUInterface, DeviceInterface))

    def test_device_interface_has_no_ruo_endpoints(self):
        for name in self.RUO_ENDPOINTS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(DeviceInterface, name))
                self.assertTrue(hasattr(LIFUInterface, name))

    def test_device_interface_has_fda_endpoints(self):
        for name in self.FDA_ENDPOINTS:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(DeviceInterface, name, None)))

    def test_lifu_interface_creates_ruo_components(self):
        iface = object.__new__(LIFUInterface)
        iface._module_invert = False
        iface._ext_power_supply = False
        tx, hv = iface._create_devices(0x0483, 0x57A5, 0x57A4, 921600, 10, True, True)
        self.assertIsInstance(tx, LIFUTxDevice)
        self.assertIsInstance(hv, LIFUHVController)

    def test_device_interface_creates_fda_components(self):
        iface = object.__new__(DeviceInterface)
        tx, hv = iface._create_devices(0x0483, 0x57A5, 0x57A4, 921600, 10, True, True)
        self.assertIs(type(tx), TxDevice)
        self.assertIs(type(hv), HVController)


class TestComponentSurface(unittest.TestCase):
    """TxDevice / HVController are the FDA components; the LIFU subclasses add RUO methods."""

    COMMON_RUO = ("uart", "start", "stop", "send_async", "echo", "toggle_led",
                  "soft_reset", "enter_dfu", "enter_stm32_rom_dfu",
                  "write_config", "write_config_json")
    TX_RUO = COMMON_RUO + ("set_trigger", "set_trigger_json", "async_mode",
                           "get_tx_module_count", "enum_tx7332_devices",
                           "set_module_invert", "write_register", "read_register",
                           "update_firmware")
    HV_RUO = COMMON_RUO + ("turn_12v_on", "turn_12v_off", "set_voltage", "set_dacs",
                           "set_fan_speed", "set_rgb_led", "get_rgb_led", "hv_enable")
    TX_FDA = ("ping", "get_version", "get_hardware_id", "read_config",
              "refresh_metadata", "get_temperature", "get_ambient_temperature",
              "get_trigger", "get_trigger_json", "start_trigger", "stop_trigger",
              "get_preset", "load_preset", "get_module_count")
    HV_FDA = ("ping", "get_version", "get_hardware_id", "read_config",
              "turn_hv_on", "turn_hv_off", "get_hv_status", "get_12v_status",
              "wait_for_settle", "get_preset", "select_preset", "get_voltage",
              "get_temperature1", "get_temperature2", "get_fan_speed",
              "get_vmon_values")

    def _check(self, fda_cls, ruo_cls, fda_names, ruo_names):
        self.assertTrue(issubclass(ruo_cls, fda_cls))
        for name in ruo_names:
            with self.subTest(name=name):
                self.assertFalse(hasattr(fda_cls, name))
                self.assertTrue(hasattr(ruo_cls, name))
        for name in fda_names:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(fda_cls, name, None)))

    def test_tx_split(self):
        self._check(TxDevice, LIFUTxDevice, self.TX_FDA, self.TX_RUO)

    def test_hv_split(self):
        self._check(HVController, LIFUHVController, self.HV_FDA, self.HV_RUO)

    def test_simulated_components_mirror_split(self):
        from openlifu_sdk.ui.simulated_interface import (
            SimulatedHVController,
            SimulatedLIFUHVController,
            SimulatedLIFUTxDevice,
            SimulatedTxDevice,
        )
        sim_tx_ruo = ("set_trigger", "set_trigger_json", "async_mode", "set_solution",
                      "write_config_json", "set_module_invert", "get_tx_module_count",
                      "toggle_led", "echo", "soft_reset", "start_monitoring")
        sim_hv_ruo = ("turn_12v_on", "turn_12v_off", "set_voltage", "set_rgb_led",
                      "get_rgb_led", "toggle_led", "echo", "soft_reset", "enter_dfu",
                      "uart", "start_monitoring")
        self._check(SimulatedTxDevice, SimulatedLIFUTxDevice, (), sim_tx_ruo)
        self._check(SimulatedHVController, SimulatedLIFUHVController, (), sim_hv_ruo)


class TestCommandWhitelist(unittest.TestCase):
    """FDA components refuse RUO opcodes before anything reaches the wire."""

    def _assert_rejected(self, component, desc, command):
        uart = _mock_uart(component, desc)
        with self.assertRaises(ValueError):
            component.send_checked(command)
        uart.send_packet.assert_not_called()

    def test_tx_rejects_register_write(self):
        self._assert_rejected(TxDevice(), "TX", OW_TX7332_WREG)

    def test_tx_rejects_echo(self):
        self._assert_rejected(TxDevice(), "TX", OW_CMD_ECHO)

    def test_hv_rejects_set_voltage(self):
        self._assert_rejected(HVController(), "HV", OW_POWER_SET_HV)

    def test_ruo_tx_allows_register_write(self):
        tx = LIFUTxDevice()
        uart = _mock_uart(tx, "TX")
        uart.send_packet.return_value = _packet(b"")
        tx.send_checked(OW_TX7332_WREG)
        uart.send_packet.assert_called_once()

    def test_ruo_hv_allows_set_voltage(self):
        hv = LIFUHVController()
        uart = _mock_uart(hv, "HV")
        uart.send_packet.return_value = _packet(b"")
        hv.send_checked(OW_POWER_SET_HV)
        uart.send_packet.assert_called_once()


if __name__ == "__main__":
    unittest.main()
