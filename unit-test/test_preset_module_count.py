# SPDX-License-Identifier: Apache-2.0
"""LIFUTXPresets.store_preset / load_preset refuse a preset whose module count
differs from the transmitter's before sending anything (#80)."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, call

from openlifu_sdk.io.exceptions import LIFUError
from openlifu_sdk.io.LIFUConfig import LIFU_ERR_MODULE_COUNT_MISMATCH
from openlifu_sdk.io.LIFUTXPresets import (
    compile_preset,
    load_preset,
    preset_blob,
    preset_modules,
    regs_crc,
    store_preset,
)


def _compiled(elements: int) -> dict:
    """A preset over *elements* elements: 64 is one module, 128 two."""
    doc = {
        "id": "unit",
        "frequency_khz": 400.0,
        "pulse_length_us": 20.0,
        "pulse_interval_ms": 100.0,
        "pulse_count": 10,
        "pulse_train_interval_s": 0,
        "pulse_train_count_selections": [1, 5],
        "delays": [[0.0] * elements],
        "apodization": [[1.0] * elements],
        "start_C": 35,
        "shutoff_C": 75,
        "voltage": 20.0,
    }
    return compile_preset(doc, settings_bytes=json.dumps(doc).encode())


def _tx(modules: int) -> MagicMock:
    tx = MagicMock()
    tx.get_module_count.return_value = modules
    return tx


class TestModuleCountGuard(unittest.TestCase):
    def test_fixture_module_counts(self):
        self.assertEqual(preset_modules(_compiled(64)), 1)
        self.assertEqual(preset_modules(_compiled(128)), 2)

    def test_store_mismatch_writes_nothing(self):
        for elements, modules in ((64, 2), (128, 1), (64, 0)):
            with self.subTest(preset_modules=elements // 64, tx_modules=modules):
                tx = _tx(modules)
                with self.assertRaises(LIFUError) as ctx:
                    store_preset(tx, 3, _compiled(elements))
                self.assertEqual(ctx.exception.code, LIFU_ERR_MODULE_COUNT_MISMATCH)
                self.assertIn("unit spans %d transmitter module(s); the TX reports %d"
                              % (elements // 64, modules), str(ctx.exception))
                # Nothing but the module-count query reaches the device.
                self.assertEqual(tx.method_calls, [call.get_module_count()])

    def test_load_mismatch_loads_nothing(self):
        for elements, modules in ((64, 2), (128, 1), (64, 0)):
            with self.subTest(preset_modules=elements // 64, tx_modules=modules):
                tx = _tx(modules)
                with self.assertRaises(LIFUError) as ctx:
                    load_preset(tx, 0, _compiled(elements))
                self.assertEqual(ctx.exception.code, LIFU_ERR_MODULE_COUNT_MISMATCH)
                self.assertEqual(tx.method_calls, [call.get_module_count()])

    def test_store_matching_count_writes_every_module(self):
        mc = _compiled(128)
        tx = _tx(2)
        # verify_preset reads the slot back; answer with what was stored.
        tx.get_preset.return_value = {
            "id": mc["id"], "settings_crc": mc["settings_crc"], "regs_crc": regs_crc(mc),
            "start_c": 35.0, "shutoff_c": 75.0,
        }
        store_preset(tx, 3, mc)
        self.assertEqual(tx.write_preset.call_args_list,
                         [call(3, preset_blob(mc, 0), 0), call(3, preset_blob(mc, 1), 1)])
        tx.get_preset.assert_called_once_with(3)

    def test_load_matching_count_loads_every_module(self):
        mc = _compiled(128)
        tx = _tx(2)
        load_preset(tx, 0, mc, duration_index=1)
        crc = mc["settings_crc"]
        self.assertEqual(tx.load_preset.call_args_list,
                         [call(0, regs_crc(mc, 0), 1, 0, settings_crc=crc),
                          call(0, regs_crc(mc, 1), 1, 1, settings_crc=crc)])


if __name__ == "__main__":
    unittest.main()
