# SPDX-License-Identifier: Apache-2.0
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
import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from openlifu_sdk.io.exceptions import LIFUDeviceError, LIFUError, LIFUSolutionError
from openlifu_sdk.io.LIFUConfig import (
    LIFU_ERR_MODULE_COUNT_MISMATCH,
    OW_BAD_CRC,
    OW_CMD,
    OW_CMD_ECHO,
    OW_CMD_USR_CFG,
    OW_CONTROLLER,
    OW_CTRL_GET_MODULE_COUNT,
    OW_CTRL_GET_SWTRIG,
    OW_CTRL_START_SWTRIG,
    OW_ERROR,
    OW_HV_PRESET_CRC,
    OW_POWER_HV_ON,
    OW_POWER_PRESET_SELECT,
    OW_POWER_SET_HV,
    OW_PRESET_LOAD,
    OW_RESP,
    OW_TX7332,
    OW_TX7332_DEVICE_COUNT,
    OW_TX7332_WREG,
)
from openlifu_sdk.io.LIFUHVController import HVController, LIFUHVController
from openlifu_sdk.io.LIFUInterface import DeviceInterface, LIFUInterface, LIFUInterfaceStatus
from openlifu_sdk.io.LIFUTXDevice import LIFUTxDevice, TxDevice, pattern_select_registers
from openlifu_sdk.io.LIFUTXPresets import (
    adjusted_voltage,
    compile_preset,
    console_preset_record,
    device_sensitivity,
    generate_console_fda_preset,
    generate_console_presets,
    generate_fda_preset,
    generate_header_files,
    generate_preset_set,
    parse_preset_blob,
    preset_blob,
    preset_files,
    preset_modules,
    preset_sensitivity,
    preset_voltage,
    regs_crc,
    store_preset,
    thermal_limits,
    treatment_timeout,
    verify_console_preset,
)
from openlifu_sdk.io.LIFUUserConfig import LifuUserConfig, sensitivity_at_frequency
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
        "treatment_timeout_s": 1800,
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


class TestTreatmentTimeout(unittest.TestCase):
    """SR-024: the preset's treatment_timeout_s, a u16 right after shutoff_C, inside regs_crc."""

    def test_whole_seconds(self):
        self.assertEqual(treatment_timeout(_compiled()), 1800)
        self.assertEqual(treatment_timeout(_compiled(treatment_timeout_s=1800.0)), 1800)
        self.assertEqual(treatment_timeout(_compiled(treatment_timeout_s=1)), 1)
        self.assertEqual(treatment_timeout(_compiled(treatment_timeout_s=0xFFFF)), 0xFFFF)

    def test_missing_is_refused_not_defaulted(self):
        doc = _preset()
        del doc["treatment_timeout_s"]
        with self.assertRaisesRegex(ValueError, "lacks treatment_timeout_s"):
            compile_preset(doc)

    def test_out_of_range_or_not_whole_refused(self):
        for bad in (0, -1, 0x10000, 1800.5, "1800", True, None, float("nan"), float("inf")):
            with self.assertRaisesRegex(ValueError, "treatment_timeout_s", msg=repr(bad)):
                _compiled(treatment_timeout_s=bad)

    def test_layout_follows_shutoff(self):
        # i16 start_C, i16 shutoff_C, then u16 treatment_timeout_s LE, then chip 0's run count.
        mc = _compiled(treatment_timeout_s=0x1234)
        blob = preset_blob(mc)
        thermal = struct.pack("<B2I", 2, 1, 5) + struct.pack("<hh", 350, 750)
        at = blob.index(thermal) + len(thermal)
        self.assertEqual(blob[at:at + 2], b"\x34\x12")
        self.assertEqual(struct.unpack_from("<H", blob, at + 2)[0], len(mc["chips"][0]["registers"]))

    def test_regs_crc_covers_it(self):
        base = _compiled()
        changed = _compiled(treatment_timeout_s=1799)
        self.assertNotEqual(regs_crc(base), regs_crc(changed))
        self.assertEqual(parse_preset_blob(preset_blob(changed))["body_crc"], regs_crc(changed))

    def test_round_trip(self):
        for mc in (_compiled(treatment_timeout_s=60), _rastered(treatment_timeout_s=60)):
            got = parse_preset_blob(preset_blob(mc))
            self.assertEqual(got["treatment_timeout_s"], 60)
            self.assertEqual(got["chips"], mc["chips"])

    def test_every_module_carries_it(self):
        wide = _compiled(id="wide", delays=[[0.0] * 128], apodization=[[1.0] * 128], treatment_timeout_s=900)
        self.assertEqual([parse_preset_blob(preset_blob(wide, m))["treatment_timeout_s"] for m in (0, 1)],
                         [900, 900])

    def test_header_names_it(self):
        with tempfile.TemporaryDirectory() as d:
            text = generate_fda_preset(d, _compiled()).read_text()
        self.assertIn("treatment_timeout_s 1800,", text)
        # 1800 = 0x0708, little-endian
        self.assertIn("\t// treatment_timeout_s: seconds from Start, pauses included\n\t0x08, 0x07,\n", text)

    def test_store_preset_writes_it(self):
        mc = _compiled(treatment_timeout_s=600)
        tx = MagicMock()
        tx.get_module_count.return_value = 1
        tx.get_preset.return_value = {"id": mc["id"], "settings_crc": mc["settings_crc"],
                                      "regs_crc": regs_crc(mc), "start_c": 35.0, "shutoff_c": 75.0}
        store_preset(tx, 3, mc)
        slot, blob, module = tx.write_preset.call_args.args
        self.assertEqual((slot, module), (3, 0))
        self.assertEqual((blob[4], parse_preset_blob(blob)["treatment_timeout_s"]), (2, 600))


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
        self.assertEqual(blob[:8], b"OWPR" + bytes([2, 4, 0, 1]))   # version, id length, module 0 of 1
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
        # A version 1 blob has no treatment_timeout_s: refused, as the firmware refuses it.
        for bad in (blob[:-1], blob + b"\0", b"XXXX" + blob[4:], blob[:4] + b"\x01" + blob[5:], blob[:40]):
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

    def test_preset_files_takes_a_file_or_a_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("b_10mm_settings.json", "b_5mm_settings.json"):
                (root / name).write_text(json.dumps(_preset()))
            (root / "constants.json").write_text("{}")
            (root / "nested").mkdir()
            (root / "nested" / "a_settings.json").write_text(json.dumps(_preset()))
            self.assertEqual([p.name for p in preset_files(root)],
                             ["a_settings.json", "b_5mm_settings.json", "b_10mm_settings.json"])
            # One file is taken as it is: compiling it is what says it is not a preset.
            for name in ("b_10mm_settings.json", "constants.json"):
                self.assertEqual(preset_files(root / name), [root / name])
            self.assertEqual(preset_files(root / "missing"), [])

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
        tx = LIFUTxDevice()
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

    def test_v2_record_layout(self):
        """console-fw#21: v1 plus float32 voltage_min/max after the sensitivity
        (a 28-byte header), the id, then a CRC-32 of everything before."""
        mc = _compiled(id="lowv", voltage=20, sensitivity=3728, voltage_range=[15, 25])
        rec = console_preset_record(mc, version=2)
        self.assertEqual(rec[:8], b"OWHV" + bytes([2, 4, 0, 0]))
        self.assertEqual(struct.unpack_from("<Iffff", rec, 8),
                         (mc["settings_crc"], 20.0, 3728.0, 15.0, 25.0))
        self.assertEqual(rec[28:32], b"lowv")
        self.assertEqual(struct.unpack_from("<I", rec, 32)[0], zlib.crc32(rec[:32]))
        self.assertEqual(len(rec), 36)
        # v1 is unchanged by the version argument's default.
        self.assertEqual(console_preset_record(mc), console_preset_record(mc, version=1))
        self.assertEqual(rec[8:20], console_preset_record(mc, version=1)[8:20])

    def test_v2_record_needs_a_voltage_range(self):
        with self.assertRaisesRegex(ValueError, "voltage_range"):
            console_preset_record(_compiled(sensitivity=3600), version=2)
        with self.assertRaisesRegex(ValueError, "version"):
            console_preset_record(_compiled(sensitivity=3600, voltage_range=[35, 45]), version=3)

    def test_table_is_v2_records_in_index_order(self):
        presets = [_compiled(id="a", voltage=40, sensitivity=3728, voltage_range=[35, 45]),
                   _compiled(id="b", voltage=20, sensitivity=2719.6, voltage_range=[15, 25])]
        with tempfile.TemporaryDirectory() as d:
            text = generate_console_presets(d, presets, "test").read_text()
        body = re.search(r"PRESET_HV_TABLE\[(\d+)\] = \{(.*?)\};", text, re.S)
        table = bytes(int(b, 16) for b in re.findall(r"0x([0-9a-f]{2})", body.group(2)))
        self.assertEqual(int(body.group(1)), len(table))
        self.assertEqual(table, b"".join(console_preset_record(mc, version=2) for mc in presets))
        self.assertIn('__attribute__((section(".fda_preset"), used))', text)
        self.assertIn('#define PRESET_HV_CONTEXT_NAME "test"', text)
        self.assertIn("#define PRESET_HV_COUNT 2u", text)

    def test_table_needs_every_field(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError, "settings_crc"):
                generate_console_presets(d, [compile_preset(_preset(sensitivity=3600))], "vet")
            with self.assertRaisesRegex(ValueError, "sensitivity"):
                generate_console_presets(d, [_compiled()], "vet")
            with self.assertRaisesRegex(ValueError, "voltage_range"):
                generate_console_presets(d, [_compiled(sensitivity=3600)], "vet")

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

    def test_deselect_is_a_select_with_no_payload(self):
        hv = HVController()
        uart = _mock_uart(hv, "HV")
        uart.send_packet.return_value = _packet(struct.pack("<f", 42.5))
        hv.select_preset(1, 0xABCD, 3388.0)
        uart.send_packet.return_value = _packet(b"")
        hv.deselect_preset()
        sent = uart.send_packet.call_args.kwargs
        self.assertEqual((sent["command"], bytes(sent["data"] or b"")), (OW_POWER_PRESET_SELECT, b""))
        self.assertIsNone(hv.supply_voltage)

    def test_console_record_layout(self):
        mc = _compiled(id="diathermy", voltage=40.0, sensitivity=3728)
        rec = console_preset_record(mc)
        self.assertEqual(rec[:8], b"OWHV" + bytes([1, 9, 0, 0]))
        self.assertEqual(struct.unpack("<Iff", rec[8:20]), (mc["settings_crc"], 40.0, 3728.0))
        self.assertEqual(rec[20:29], b"diathermy")
        self.assertEqual(struct.unpack("<I", rec[29:]), (zlib.crc32(rec[:29]) & 0xFFFFFFFF,))
        with self.assertRaisesRegex(ValueError, "sensitivity"):
            console_preset_record(dict(mc, sensitivity=None))

    def test_fda_headers_put_the_preset_in_its_section(self):
        mc = _compiled(id="diathermy", voltage=40.0, sensitivity=3728)
        with tempfile.TemporaryDirectory() as d:
            tx = generate_fda_preset(d, mc).read_text()
            con = generate_console_fda_preset(Path(d) / "console", mc).read_text()
        for text, symbol, data in ((tx, "FDA_PRESET_BLOB", preset_blob(mc)),
                                   (con, "FDA_PRESET_HV", console_preset_record(mc))):
            self.assertIn('__attribute__((section(".fda_preset"), used))\nstatic const uint8_t %s[%d] = {'
                          % (symbol, len(data)), text)
            body = text[text.index(symbol):]
            self.assertEqual(bytes(int(b, 16) for b in re.findall(r"0x([0-9a-f]{2}),", body)), data)
        with self.assertRaisesRegex(ValueError, "1x"):
            generate_fda_preset(".", _compiled(id="wide", delays=[[0.0] * 128], apodization=[[1.0] * 128]))

    def test_select_preset_takes_no_frequency(self):
        # The console firmware (hv_presets.c) accepts exactly <If; a frequency has nowhere to go,
        # so the method does not take one rather than accept and drop it.
        hv = HVController()
        _mock_uart(hv, "HV")
        with self.assertRaises(TypeError):
            hv.select_preset(1, 0xABCD, 3388.0, 400000.0)

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
        # Between calibrated frequencies there is no value: no interpolation.
        self.assertIsNone(tx.get_sensitivity_for_frequency(425000.0))


class TestPresetVoltageRange(unittest.TestCase):
    """voltage_range is preset data, carried through compile_preset, not baked."""

    def test_carried_through_compile_without_changing_the_crcs(self):
        with_range = _compiled(voltage=40.0, sensitivity=3600, voltage_range=[37.0, 42.0])
        without = _compiled(voltage=40.0, sensitivity=3600)
        self.assertEqual(with_range["voltage_range"], [37.0, 42.0])
        self.assertNotIn("voltage_range", without)
        self.assertEqual(regs_crc(with_range), regs_crc(without))   # not in the blob

    def test_malformed_range_is_refused(self):
        for bad in ([42.0, 37.0], [0, 45], [35.0], "35-45", [35.0, float("nan")]):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "voltage_range"):
                    _compiled(voltage=40.0, sensitivity=3600, voltage_range=bad)


class TestSensitivityLookup(unittest.TestCase):
    """The SDK's one sensitivity lookup (LIFUUserConfig.sensitivity_at_frequency)."""

    @staticmethod
    def _module(*pairs):
        return {"module": {"sensitivity": [list(p) for p in pairs]}}

    def test_exact_frequency_only(self):
        modules = [self._module((380000, 3110), (400000, 2720))]
        self.assertEqual(sensitivity_at_frequency(modules, 400000.0), 2720.0)
        self.assertEqual(sensitivity_at_frequency(modules, 400000.0005), 2720.0)   # within 1 mHz
        self.assertIsNone(sensitivity_at_frequency(modules, 400000.01))

    def test_outside_the_table_is_refused_not_clamped(self):
        modules = [self._module((380000, 3110), (400000, 2720))]
        self.assertIsNone(sensitivity_at_frequency(modules, 500000.0))
        self.assertIsNone(sensitivity_at_frequency(modules, 300000.0))

    def test_non_positive_and_non_finite_entries_are_ignored(self):
        for bad in (0.0, -5.0, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                self.assertIsNone(sensitivity_at_frequency([self._module((400000, bad))], 400000.0))

    def test_malformed_rows_are_skipped_not_raised(self):
        # The user config is RUO-writable: one bad row must not break the lookup of a good one.
        module = {"module": {"sensitivity": [["x", 1], None, [1, 2, 3], [400000, None], [400000, 2720]]}}
        self.assertEqual(sensitivity_at_frequency([module], 400000.0), 2720.0)
        self.assertIsNone(sensitivity_at_frequency([{"module": {"sensitivity": [["x", 1]]}}], 400000.0))

    def test_device_sensitivity_is_the_same_lookup(self):
        modules = [self._module((400000, 3000)), self._module((400000, 3600))]
        doc = _preset(voltage=40.0, sensitivity=3600)
        self.assertEqual(device_sensitivity(doc, modules),
                         sensitivity_at_frequency(modules, float(doc["frequency_khz"]) * 1e3))


class TestInterfacePresetLoad(unittest.TestCase):

    @staticmethod
    def _mocked(cls=DeviceInterface, modules: int = 1) -> DeviceInterface:
        iface = object.__new__(cls)
        iface.txdevice = MagicMock()
        iface.txdevice.get_module_count.return_value = modules
        iface.txdevice.module_user_configs = [{"module": {"sensitivity": []}}]
        iface.txdevice.get_sensitivity_for_frequency.return_value = 2720.0
        iface.hvcontroller = MagicMock()
        iface.status = LIFUInterfaceStatus.STATUS_SYS_OFF
        iface._test_mode = False
        return iface

    def _cleared(self, iface, modules: int = 1):
        self.assertEqual([c.kwargs for c in iface.txdevice.unload_preset.call_args_list],
                         [{"module": m} for m in range(modules)])
        iface.hvcontroller.turn_hv_off.assert_called_once_with()
        iface.hvcontroller.deselect_preset.assert_called_once_with()
        self.assertEqual(iface.status, LIFUInterfaceStatus.STATUS_ERROR)

    def test_load_preset_orchestrates_tx_and_hv(self):
        iface = self._mocked()

        iface.load_preset(1, 0xAABBCCDD, 0x11223344, 400000.0, duration_index=2)

        iface.txdevice.load_preset.assert_called_once_with(1, 0x11223344, duration_index=2, module=0,
                                                           settings_crc=0xAABBCCDD)
        iface.txdevice.refresh_metadata.assert_called_once_with(module_count=1)
        iface.txdevice.get_sensitivity_for_frequency.assert_called_once_with(400000.0)
        iface.hvcontroller.select_preset.assert_called_once_with(1, 0xAABBCCDD, 2720.0)
        self.assertEqual(iface.status, LIFUInterfaceStatus.STATUS_READY)

    def test_load_preset_rereads_the_calibration_every_time(self):
        # A transmitter swapped since the last load must not inherit the old one's sensitivity.
        iface = self._mocked()
        iface.txdevice.get_sensitivity_for_frequency.side_effect = [3000.0, 3600.0]

        iface.load_preset(0, 0xABCD, 0xDCBA, 500000.0)
        iface.load_preset(0, 0xABCD, 0xDCBA, 500000.0)

        self.assertEqual(iface.txdevice.refresh_metadata.call_count, 2)
        self.assertEqual([c.args for c in iface.hvcontroller.select_preset.call_args_list],
                         [(0, 0xABCD, 3000.0), (0, 0xABCD, 3600.0)])

    def test_a_two_module_preset_loads_each_module_with_its_own_crc(self):
        # The vet app has 2x presets: the SDK supports them, one regs_crc per module.
        for cls in (DeviceInterface, LIFUInterface):
            with self.subTest(cls=cls.__name__):
                iface = self._mocked(cls, modules=2)
                iface.load_preset(0, 0xABCD, [0x1111, 0x2222], 400000.0, duration_index=1)
                self.assertEqual([(c.args, c.kwargs) for c in iface.txdevice.load_preset.call_args_list],
                                 [((0, 0x1111), {"duration_index": 1, "module": 0, "settings_crc": 0xABCD}),
                                  ((0, 0x2222), {"duration_index": 1, "module": 1, "settings_crc": 0xABCD})])
                iface.txdevice.refresh_metadata.assert_called_once_with(module_count=2)
                iface.hvcontroller.select_preset.assert_called_once_with(0, 0xABCD, 2720.0)
                self.assertEqual(iface.status, LIFUInterfaceStatus.STATUS_READY)

    def test_a_refused_two_module_load_unloads_every_module(self):
        iface = self._mocked(modules=2)
        iface.hvcontroller.select_preset.side_effect = LIFUDeviceError("refused", device_error_code=OW_HV_PRESET_CRC)
        with self.assertRaises(LIFUDeviceError):
            iface.load_preset(0, 0xABCD, [0x1111, 0x2222], 400000.0)
        self._cleared(iface, modules=2)

    def test_regs_crc_accepts_any_integer_type_and_refuses_the_rest(self):
        import numpy as np
        iface = self._mocked()
        iface.load_preset(0, 0xABCD, np.uint32(0xDCBA), 400000.0)
        self.assertEqual(iface.txdevice.load_preset.call_args.args, (0, 0xDCBA))
        for bad in (True, [], "0xDCBA", [0xDCBA, None], 1.5):
            with self.subTest(bad=bad):
                iface = self._mocked()
                with self.assertRaisesRegex(TypeError, "regs_crc"):
                    iface.load_preset(0, 0xABCD, bad, 400000.0)
                iface.txdevice.load_preset.assert_not_called()

    def test_a_preset_whose_module_count_differs_is_refused_before_anything_is_loaded(self):
        # The set-preset guard: a 1x preset (64 elements, e.g. diathermy) on a 2x transmitter, or the reverse.
        for cls in (DeviceInterface, LIFUInterface):
            for regs, modules in ((0xDCBA, 2), (0xDCBA, 0), ([0x1111, 0x2222], 1)):
                with self.subTest(cls=cls.__name__, preset_modules=1 if isinstance(regs, int) else len(regs),
                                  tx_modules=modules):
                    iface = self._mocked(cls, modules)
                    iface.status = LIFUInterfaceStatus.STATUS_READY   # an earlier load succeeded
                    with self.assertRaises(LIFUError) as raised:
                        iface.load_preset(0, 0xABCD, regs, 400000.0)
                    self.assertEqual(raised.exception.code, LIFU_ERR_MODULE_COUNT_MISMATCH)
                    iface.txdevice.load_preset.assert_not_called()
                    iface.hvcontroller.select_preset.assert_not_called()
                    # Handled like any refusal: what an earlier load left is cleared, status is ERROR.
                    self._cleared(iface)

    def test_the_presets_voltage_range_is_checked_by_the_host(self):
        # Preset data no console sees: the host holds the applied voltage to it.
        iface = self._mocked()
        iface.hvcontroller.select_preset.return_value = 40.0
        iface.load_preset(0, 0xABCD, 0xDCBA, 400000.0, voltage_range=(37.0, 42.0))
        self.assertEqual(iface.status, LIFUInterfaceStatus.STATUS_READY)

        iface = self._mocked()
        iface.hvcontroller.select_preset.return_value = 43.4
        with self.assertRaisesRegex(LIFUSolutionError, "voltage_range"):
            iface.load_preset(0, 0xABCD, 0xDCBA, 400000.0, voltage_range=(37.0, 42.0), turn_hv_on=True)
        iface.hvcontroller.turn_hv_on.assert_not_called()
        self._cleared(iface)

        iface = self._mocked()   # no range given: only the device's own limits apply
        iface.hvcontroller.select_preset.return_value = 43.4
        iface.load_preset(0, 0xABCD, 0xDCBA, 400000.0)
        self.assertEqual(iface.status, LIFUInterfaceStatus.STATUS_READY)

    def test_a_frequency_the_transmitter_is_not_calibrated_at_is_refused(self):
        iface = self._mocked()
        iface.txdevice.get_sensitivity_for_frequency.return_value = None
        with self.assertRaises(LIFUSolutionError):
            iface.load_preset(0, 0xABCD, 0xDCBA, 412345.0, turn_hv_on=True)
        iface.hvcontroller.select_preset.assert_not_called()
        iface.hvcontroller.turn_hv_on.assert_not_called()
        self._cleared(iface)

    def test_load_reads_the_module_count_once(self):
        iface = self._mocked()
        iface.load_preset(0, 0xABCD, 0xDCBA, 400000.0)
        iface.txdevice.get_module_count.assert_called_once_with()

    def test_connect_does_not_check_the_module_count(self):
        # The module guard is at set-preset; any transmitter may connect.
        for cls in (DeviceInterface, LIFUInterface):
            with self.subTest(cls=cls.__name__):
                tx, hv = MagicMock(), MagicMock()
                tx.is_connected.return_value = True
                tx.get_module_count.return_value = 2
                with patch.object(cls, "_claim_hw_interface_pid"), \
                        patch.object(cls, "_create_devices", return_value=(tx, hv)):
                    iface = cls()
                self.assertIs(iface.txdevice, tx)
                tx.connect.assert_called_once_with()
                tx.get_module_count.assert_not_called()
                tx.close.assert_not_called()

    def test_transmitter_refusal_leaves_the_console_with_nothing_selected(self):
        iface = self._mocked()
        iface.txdevice.load_preset.side_effect = LIFUDeviceError("refused", device_error_code=OW_BAD_CRC)
        with self.assertRaises(LIFUDeviceError) as raised:
            iface.load_preset(0, 0xABCD, 0xDCBA, 400000.0, turn_hv_on=True)
        self.assertEqual(raised.exception.device_error_code, OW_BAD_CRC)
        iface.hvcontroller.select_preset.assert_not_called()
        iface.hvcontroller.turn_hv_on.assert_not_called()
        self._cleared(iface)

    def test_console_refusal_leaves_the_transmitter_with_nothing_loaded(self):
        iface = self._mocked()
        iface.hvcontroller.select_preset.side_effect = LIFUDeviceError("refused",
                                                                       device_error_code=OW_HV_PRESET_CRC)
        with self.assertRaises(LIFUDeviceError) as raised:
            iface.load_preset(0, 0xABCD, 0xDCBA, 400000.0, turn_hv_on=True)
        self.assertEqual(raised.exception.device_error_code, OW_HV_PRESET_CRC)
        iface.hvcontroller.turn_hv_on.assert_not_called()
        self._cleared(iface)

    def test_a_clear_that_fails_does_not_hide_the_refusal(self):
        iface = self._mocked()
        iface.txdevice.load_preset.side_effect = LIFUDeviceError("refused", device_error_code=OW_BAD_CRC)
        iface.txdevice.unload_preset.side_effect = LIFUDeviceError("gone")
        with self.assertRaises(LIFUDeviceError) as raised:
            iface.load_preset(0, 0xABCD, 0xDCBA, 400000.0)
        self.assertEqual(raised.exception.device_error_code, OW_BAD_CRC)
        iface.hvcontroller.deselect_preset.assert_called_once_with()   # still tried

    def test_no_trigger_no_hv(self):
        # A transmitter with nothing loaded refuses to start: HV must not stay on.
        iface = self._mocked()
        iface.txdevice.start_trigger.side_effect = LIFUDeviceError("no preset")
        with self.assertRaises(LIFUDeviceError):
            iface.start_sonication()
        iface.hvcontroller.turn_hv_on.assert_called_once_with()
        iface.hvcontroller.turn_hv_off.assert_called_once_with()
        self.assertNotEqual(iface.status, LIFUInterfaceStatus.STATUS_RUNNING)

    def test_fda_transmitter_loads_any_module(self):
        # Multi-module presets are supported; the module-count guard is DeviceInterface.load_preset's.
        tx = TxDevice()
        uart = _mock_uart(tx, "TX")
        uart.send_packet.return_value = _packet(b"")
        tx.load_preset(0, 1, module=1, settings_crc=2)
        tx.unload_preset(module=1)
        self.assertEqual([c.kwargs["addr"] for c in uart.send_packet.call_args_list], [1, 1])

    def test_research_transmitter_loads_presets_on_any_module(self):
        # PRESET_MODE images span modules; LIFUTXPresets.load_preset loads each in turn.
        tx = LIFUTxDevice()
        uart = _mock_uart(tx, "TX")
        uart.send_packet.return_value = _packet(b"")
        tx.load_preset(0, 1, module=1, settings_crc=2)
        self.assertEqual(uart.send_packet.call_args.kwargs["addr"], 1)

    def test_unload_is_a_load_with_no_payload(self):
        tx = TxDevice()
        uart = _mock_uart(tx, "TX")
        uart.send_packet.return_value = _packet(b"")
        tx.unload_preset()
        sent = uart.send_packet.call_args.kwargs
        self.assertEqual((sent["command"], bytes(sent["data"] or b"")), (OW_PRESET_LOAD, b""))

    def test_load_preset_sends_what_the_firmware_accepts(self):
        # The real FDA components under the interface: every command it needs is one they
        # may send, the transmitter is given both CRCs, the console settings_crc + sensitivity.
        iface = object.__new__(DeviceInterface)
        iface.txdevice, iface.hvcontroller = TxDevice(), HVController()
        iface.status = LIFUInterfaceStatus.STATUS_SYS_OFF
        tx_uart, hv_uart = _mock_uart(iface.txdevice, "TX"), _mock_uart(iface.hvcontroller, "HV")
        tx_uart.send_packet.return_value = _packet(b"")
        hv_uart.send_packet.side_effect = [_packet(struct.pack("<f", 44.75)), _packet(b"")]
        # The real reads: the trigger status (no paused sequence, SR-023), the module count,
        # the load, then the user config.
        config = LifuUserConfig(json_data={"sn": "SN-1", "hwid": "HW-1",
                                           "module": {"sensitivity": [[400000, 3220]]}})
        tx_uart.send_packet.side_effect = [_packet(b'{"TriggerStatus": "STOPPED"}'), _packet(bytes([1])),
                                           _packet(b""), _packet(config.to_wire_bytes())]

        iface.load_preset(0, 0xAABBCCDD, 0x11223344, 400000.0, duration_index=2)

        sent = [c.kwargs for c in tx_uart.send_packet.call_args_list]
        # One count read: the HV scaling uses the module that check allowed, not a second read.
        self.assertEqual([s["command"] for s in sent],
                         [OW_CTRL_GET_SWTRIG, OW_CTRL_GET_MODULE_COUNT, OW_PRESET_LOAD, OW_CMD_USR_CFG])
        self.assertEqual((sent[2]["reserved"], bytes(sent[2]["data"])),
                         (0, struct.pack("<III", 2, 0x11223344, 0xAABBCCDD)))
        self.assertEqual((sent[3]["reserved"], sent[3]["addr"]), (0, 0))   # module 0's calibration, read only
        sent = hv_uart.send_packet.call_args_list[0].kwargs
        self.assertEqual((sent["command"], sent["reserved"], bytes(sent["data"])),
                         (OW_POWER_PRESET_SELECT, 0, struct.pack("<If", 0xAABBCCDD, 3220.0)))
        self.assertEqual(iface.hvcontroller.supply_voltage, 44.75)   # the console's answer, for wait_for_settle


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
        "pause_sonication", "resume_sonication",
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
              "pause_trigger", "resume_trigger",
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

    def test_fda_components_refuse_a_user_config_write(self):
        # reserved=1 is the write: it carries the calibration that scales HV.
        for component, desc in ((TxDevice(), "TX"), (HVController(), "HV")):
            with self.subTest(desc=desc):
                uart = _mock_uart(component, desc)
                with self.assertRaisesRegex(ValueError, "only be read"):
                    component.send_checked(OW_CMD_USR_CFG, reserved=1, data=b"{}")
                uart.send_packet.assert_not_called()

    def test_fda_components_still_read_the_user_config(self):
        for component, desc in ((TxDevice(), "TX"), (HVController(), "HV")):
            with self.subTest(desc=desc):
                uart = _mock_uart(component, desc)
                uart.send_packet.return_value = _packet(b"")
                component.send_checked(OW_CMD_USR_CFG, reserved=0)
                self.assertEqual(uart.send_packet.call_args.kwargs["packet_type"], OW_CMD)

    def test_fda_components_refuse_an_allowed_opcode_as_another_packet_type(self):
        # Opcode numbers repeat across packet types: the pair is what is allowed.
        for component, desc, command, packet_type in (
                (TxDevice(), "TX", OW_CTRL_START_SWTRIG, OW_TX7332),
                (TxDevice(), "TX", OW_PRESET_LOAD, OW_CMD),
                (HVController(), "HV", OW_POWER_HV_ON, OW_CMD)):
            with self.subTest(desc=desc, command=command, packet_type=packet_type):
                uart = _mock_uart(component, desc)
                with self.assertRaisesRegex(ValueError, "packet type"):
                    component.send_checked(command, packet_type=packet_type)
                uart.send_packet.assert_not_called()

    def test_ruo_components_keep_the_user_config_write(self):
        for component, desc in ((LIFUTxDevice(), "TX"), (LIFUHVController(), "HV")):
            with self.subTest(desc=desc):
                uart = _mock_uart(component, desc)
                uart.send_packet.return_value = _packet(b"")
                component.send_checked(OW_CMD_USR_CFG, reserved=1, data=b"{}")
                uart.send_packet.assert_called_once()

    def test_every_send_the_fda_components_make_is_allowed(self):
        # Each FDA method, against a mocked link: none is stopped by its own whitelist.
        tx, hv = TxDevice(), HVController()
        tx_uart, hv_uart = _mock_uart(tx, "TX"), _mock_uart(hv, "HV")
        tx_uart.send_packet.return_value = hv_uart.send_packet.return_value = _packet(b"")
        calls = [tx.ping, tx.get_version, tx.get_hardware_id, tx.read_config,
                 lambda: tx.refresh_metadata(module_count=1), tx.get_temperature,
                 tx.get_ambient_temperature, tx.get_trigger_json, tx.start_trigger, tx.stop_trigger,
                 tx.pause_trigger, tx.resume_trigger,
                 lambda: tx.get_preset(0), lambda: tx.load_preset(0, 1, settings_crc=2),
                 tx.unload_preset, tx.get_module_count,
                 hv.ping, hv.get_version, hv.read_config, hv.get_temperature1, hv.get_temperature2,
                 hv.get_12v_status, hv.turn_hv_on, hv.turn_hv_off, hv.get_hv_status, lambda: hv.get_preset(0),
                 lambda: hv.select_preset(0, 1, 2720.0), hv.deselect_preset, hv.get_voltage,
                 hv.get_fan_speed, hv.get_vmon_values]
        for call in calls:
            with self.subTest(call=getattr(call, "__name__", "lambda")):
                sent = tx_uart.send_packet.call_count + hv_uart.send_packet.call_count
                try:
                    call()
                except LIFUError:
                    pass   # an empty mocked reply is a protocol error; a whitelist refusal would be ValueError
                self.assertGreater(tx_uart.send_packet.call_count + hv_uart.send_packet.call_count, sent)

    def test_module_count_fallback_is_an_allowed_send(self):
        # Older images refuse OW_CTRL_GET_MODULE_COUNT; the fallback is the one OW_TX7332 pair allowed.
        tx = TxDevice()
        uart = _mock_uart(tx, "TX")
        refused = _packet(b"")
        refused.packet_type = OW_ERROR
        uart.send_packet.side_effect = [refused, _packet(bytes([1]))]
        self.assertEqual(tx.get_module_count(), 1)
        sent = [c.kwargs for c in uart.send_packet.call_args_list]
        self.assertEqual([(s["packet_type"], s["command"]) for s in sent],
                         [(OW_CONTROLLER, OW_CTRL_GET_MODULE_COUNT), (OW_TX7332, OW_TX7332_DEVICE_COUNT)])


if __name__ == "__main__":
    unittest.main()
