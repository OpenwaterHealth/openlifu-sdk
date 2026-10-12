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

from openlifu_sdk.io.LIFUConfig import OW_RESP
from openlifu_sdk.io.LIFUHVController import HVController
from openlifu_sdk.io.LIFUTXDevice import TxDevice, pattern_select_registers
from openlifu_sdk.io.LIFUTXPresets import (
    adjusted_voltage,
    compile_preset,
    device_sensitivity,
    generate_console_presets,
    generate_header_files,
    generate_preset_set,
    parse_preset_blob,
    preset_blob,
    preset_modules,
    preset_sensitivity,
    preset_voltage,
    regs_crc,
    thermal_limits,
    verify_console_preset,
)
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
        self.assertEqual(blob[:8], b"OWPR" + bytes([1, 4, 0, 1]))   # version, id length, module 0 of 1
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
        self.assertIn("#define PRESET_MODULES 1u", table)
        self.assertIn("\t{ { PRESET_A_BLOB, sizeof(PRESET_A_BLOB) } },\n"
                      "\t{ { PRESET_B_BLOB, sizeof(PRESET_B_BLOB) } },", table)

    def test_one_blob_per_module(self):
        # A module programs only its own two chips, so it gets only those.
        one = _compiled()
        two = _compiled(id="wide", delays=[[0.0] * 128], apodization=[[1.0] * 128])
        self.assertEqual((preset_modules(one), preset_modules(two)), (1, 2))
        self.assertEqual(preset_blob(one), preset_blob(one, 0))
        halves = [parse_preset_blob(preset_blob(two, m)) for m in (0, 1)]
        self.assertEqual([h["chips"] for h in halves], [two["chips"][0:2], two["chips"][2:4]])
        self.assertEqual([h["regs_crc"] for h in halves], [regs_crc(two, 0), regs_crc(two, 1)])
        self.assertEqual(len(preset_blob(two, 0)), len(preset_blob(one)) + len("wide") - len("unit"))
        with self.assertRaisesRegex(ValueError, "nothing for module 2"):
            preset_blob(two, 2)
        with self.assertRaisesRegex(ValueError, "nothing for module 1"):
            regs_crc(one, 1)

    def test_table_has_a_column_per_module(self):
        presets = [_compiled(id="a"), _compiled(id="wide", delays=[[0.0] * 128], apodization=[[1.0] * 128])]
        with tempfile.TemporaryDirectory() as d:
            written = generate_preset_set(d, presets)
            table, wide = written[-1].read_text(), written[1].read_text()
        self.assertIn("#define PRESET_MODULES 2u", table)
        self.assertIn("\t{ { PRESET_A_BLOB, sizeof(PRESET_A_BLOB) }, { 0, 0 } },\n"
                      "\t{ { PRESET_WIDE_BLOB, sizeof(PRESET_WIDE_BLOB) }, "
                      "{ PRESET_WIDE_M1_BLOB, sizeof(PRESET_WIDE_M1_BLOB) } },", table)
        for module, name in enumerate(("PRESET_WIDE_BLOB", "PRESET_WIDE_M1_BLOB")):
            body = wide[wide.index("static const uint8_t %s[" % name):]
            body = body[body.index("{") + 1:body.index("};")]
            emitted = bytes(int(x, 16) for x in re.findall(r"0x([0-9a-f]{2}),", body))
            self.assertEqual(emitted, preset_blob(presets[1], module))


class TestSharedWithLiveDevice(unittest.TestCase):
    """A preset bakes what set_solution would send a live device for the same inputs."""

    @staticmethod
    def _sent(**trigger) -> dict:
        tx = TxDevice()
        uart = _mock_uart(tx, "TX")
        uart.send_packet.return_value = _packet(b"{}")
        tx.set_trigger(**trigger)
        return json.loads(uart.send_packet.call_args.kwargs["data"].decode())

    def test_trigger_is_what_set_trigger_sends(self):
        # 6 ms is 166.67 Hz; the trigger runs on whole Hz, truncated.
        got = parse_preset_blob(preset_blob(_compiled(pulse_interval_ms=6.0, pulse_train_interval_s=2.0)))
        sent = self._sent(pulse_interval=6.0 * 1e-3, pulse_count=10, pulse_train_interval=2.0, pulse_train_count=5)
        self.assertEqual((got["trig_hz"], got["trig_train_us"]), (166, 2_000_000))
        self.assertEqual((got["trig_hz"], got["trig_train_us"]),
                         (sent["TriggerFrequencyHz"], sent["TriggerPulseTrainInterval"]))

    def test_train_interval_follows_set_trigger(self):
        # The preset's train is 10 pulses at 10 Hz: 1 s. An interval equal to it is back-to-back.
        self.assertEqual(_compiled(pulse_train_interval_s=1.0)["trigger"]["pulse_train_interval_us"], 0)
        self.assertEqual(self._sent(pulse_interval=0.1, pulse_count=10, pulse_train_interval=1.0,
                                    pulse_train_count=5)["TriggerPulseTrainInterval"], 0)
        # Shorter than the train cannot run once more than one train is offered...
        with self.assertRaisesRegex(ValueError, "shorter than the on-device train"):
            _compiled(pulse_train_interval_s=0.5)
        # ...and has no effect when only one is.
        single = _compiled(pulse_train_interval_s=0.5, pulse_train_count_selections=[1])
        self.assertEqual(single["trigger"]["pulse_train_interval_us"], 0)

    def test_module_invert_reaches_the_registers(self):
        base = regs_crc(_compiled())
        self.assertEqual(regs_crc(_compiled(module_invert=False)), base)
        self.assertNotEqual(regs_crc(_compiled(module_invert=True)), base)

    def test_pattern_select_has_one_source(self):
        self.assertEqual(pattern_select_registers(3), {0x1F: 2, 0x1E: 2})
        for chip in _rastered()["chips"]:
            for profile, regs in enumerate(chip["profiles"], start=1):
                self.assertEqual({a: regs[a] for a in (0x1F, 0x1E)}, pattern_select_registers(profile))


def _f32(v: float) -> float:
    return struct.unpack("<f", struct.pack("<f", v))[0]


class TestPresetVoltage(unittest.TestCase):

    def test_voltage_is_float32(self):
        self.assertEqual(preset_voltage(_compiled(voltage=47.66997571)), _f32(47.66997571))

    def test_bad_voltage(self):
        for bad in (0, -5, "20", None, float("inf")):
            with self.assertRaises(ValueError, msg=repr(bad)):
                _compiled(voltage=bad)

    @staticmethod
    def _module(*pairs) -> dict:
        return {"module": {"sensitivity": [list(p) for p in pairs]}}

    def test_scaled_to_the_transmitters_sensitivity(self):
        # 40 V suits a 3600 transmitter; a 3000 one needs proportionally more.
        doc = _preset(voltage=40.0, sensitivity=3600)
        got = adjusted_voltage(doc, [self._module((395000, 9999), (400000, 3000))])
        self.assertEqual((got["adjusted"], got["voltage"]), (True, 48.0))
        self.assertEqual((got["preset_voltage"], got["preset_sensitivity"], got["device_sensitivity"]),
                         (40.0, 3600.0, 3000.0))
        # The compiled preset carries what the calculation needs.
        mc = _compiled(voltage=40.0, sensitivity=3600)
        self.assertEqual(adjusted_voltage(mc, [self._module((400000, 3000))])["voltage"], 48.0)

    def test_modules_are_averaged(self):
        doc = _preset(voltage=33.0, sensitivity=3600)
        got = adjusted_voltage(doc, [self._module((400000, 3000)), self._module((400000, 3600))])
        self.assertEqual(got["device_sensitivity"], 3300.0)
        self.assertAlmostEqual(got["voltage"], 36.0)
        # A module with no entry at the frequency is left out of the mean.
        modules = [self._module((400000, 3000)), self._module((380000, 3600)), {}]
        self.assertEqual(device_sensitivity(doc, modules), 3000.0)
        # None at all is no sensitivity, not a guess: an FDA console cannot be selected without one.
        self.assertIsNone(device_sensitivity(doc, [self._module((380000, 3600)), {}]))

    def test_unadjusted_says_why(self):
        modules = [self._module((400000, 3000))]
        got = adjusted_voltage(_preset(voltage=40.0), modules)
        self.assertEqual((got["adjusted"], got["voltage"]), (False, 40.0))
        self.assertIn("preset has no sensitivity", got["reason"])
        for configs in ([], [{}], [self._module((380000, 3000))], [self._module((400000, 0))]):
            got = adjusted_voltage(_preset(voltage=40.0, sensitivity=3600), configs)
            self.assertEqual((got["adjusted"], got["voltage"]), (False, 40.0), configs)
            self.assertIn("400000 Hz", got["reason"])


class TestConsolePresets(unittest.TestCase):

    def test_sensitivity_is_float32_and_required(self):
        self.assertEqual(preset_sensitivity(_compiled(sensitivity=3600)), 3600.0)
        self.assertEqual(preset_sensitivity(_compiled(sensitivity=2719.6)), _f32(2719.6))
        for bad in (None, 0, -1, "3600", float("nan")):
            with self.assertRaises(ValueError, msg=repr(bad)):
                preset_sensitivity({"id": "x", "sensitivity": bad})
        with self.assertRaisesRegex(ValueError, "sensitivity"):
            preset_sensitivity(_compiled())                    # the preset has none

    def test_table_in_index_order_with_exact_values(self):
        presets = [_compiled(id="a", voltage=47.66997571, sensitivity=3600),
                   _compiled(id="b", voltage=30, sensitivity=2719.6)]
        with tempfile.TemporaryDirectory() as d:
            text = generate_console_presets(d, presets, "vet").read_text()
        rows = re.findall(r'\{ "(\w+)", (\S+)f, (\S+)f, 0x([0-9a-f]{8})u \}', text)
        self.assertEqual([r[0] for r in rows], ["a", "b"])
        for (_, volts, sens, crc), mc in zip(rows, presets):
            # Each C literal parses back to the very float32 the preset compiles to,
            # and is a float literal even for a whole number.
            self.assertEqual(_f32(float(volts)), preset_voltage(mc))
            self.assertEqual(_f32(float(sens)), preset_sensitivity(mc))
            self.assertTrue("." in volts and "." in sens, (volts, sens))
            self.assertEqual(int(crc, 16), mc["settings_crc"])
        self.assertIn('#define PRESET_HV_CONTEXT_NAME "vet"', text)
        self.assertIn("#define PRESET_HV_COUNT 2u", text)

    def test_table_needs_every_field(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError, "settings_crc"):
                generate_console_presets(d, [compile_preset(_preset(sensitivity=3600))], "vet")
            with self.assertRaisesRegex(ValueError, "sensitivity"):
                generate_console_presets(d, [_compiled()], "vet")

    @staticmethod
    def _reply(selected: int, crc: int, volts: float, sens: float, applied: float, ident: bytes = b"unit") -> bytes:
        return (bytes([3, 1, selected]) + struct.pack("<Ifffff", crc, volts, sens, applied, 35.0, 45.0)
                + bytes([len(ident)]) + ident)

    def test_get_preset(self):
        hv = HVController()
        uart = _mock_uart(hv, "HV")
        uart.send_packet.return_value = _packet(self._reply(0xFF, 0xABCD, 40.0, 3600.0, 0.0))
        got = hv.get_preset(1)
        self.assertEqual((got["count"], got["index"], got["selected"], got["applied_voltage"]), (3, 1, None, None))
        self.assertEqual((got["settings_crc"], got["id"]), (0xABCD, "unit"))
        self.assertEqual((got["voltage"], got["sensitivity"]), (40.0, 3600.0))
        self.assertEqual((got["min_voltage"], got["max_voltage"]), (35.0, 45.0))
        uart.send_packet.return_value = _packet(self._reply(1, 0xABCD, 40.0, 3600.0, 42.5))
        self.assertEqual((hv.get_preset(1)["selected"], hv.get_preset(1)["applied_voltage"]), (1, 42.5))
        # A console that is not an FDA build has no table.
        uart.send_packet.return_value = _packet(b"")
        self.assertIsNone(hv.get_preset(0))

    def test_select_preset_sends_the_sensitivity_and_returns_the_voltage(self):
        hv = HVController()
        uart = _mock_uart(hv, "HV")
        uart.send_packet.return_value = _packet(struct.pack("<f", 42.5))
        self.assertEqual(hv.select_preset(1, 0xABCD, 3388.0), 42.5)
        sent = uart.send_packet.call_args_list[0].kwargs
        self.assertEqual((sent["reserved"], bytes(sent["data"])), (1, struct.pack("<If", 0xABCD, 3388.0)))
        self.assertEqual(hv.supply_voltage, 42.5)   # wait_for_settle aims at it

    def test_verify_console_preset(self):
        mc = _compiled(id="a", voltage=47.66997571, sensitivity=3600)
        hv = MagicMock()
        hv.get_preset.return_value = {"id": "a", "settings_crc": mc["settings_crc"],
                                      "voltage": preset_voltage(mc), "sensitivity": 3600.0, "selected": None}
        verify_console_preset(hv, 0, mc)
        for field, wrong in (("voltage", 47.0), ("sensitivity", 3601.0)):
            hv.get_preset.return_value = dict(hv.get_preset.return_value, **{field: wrong})
            with self.assertRaisesRegex(ValueError, field):
                verify_console_preset(hv, 0, mc)
        hv.get_preset.return_value = None
        with self.assertRaisesRegex(ValueError, "not an FDA_MODE image"):
            verify_console_preset(hv, 0, mc)


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


if __name__ == "__main__":
    unittest.main()
