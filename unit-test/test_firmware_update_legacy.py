# SPDX-License-Identifier: Apache-2.0
"""Legacy-bootloader units are refused, before anything is flashed (SR-021, #91).

The legacy-bootloader update paths authorized an interim image with the
legacy bootloader's shared HMAC trust key. With the key removed, the
updaters refuse those units and say how to recover them; units with a
running app on the other bootloader generations still take their paths.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from openlifu_sdk.io.LIFUDFU import DFU_KIND_LEGACY, LIFUDFUManager
from openlifu_sdk.io.LIFUFirmwareUpdate import (
    COHORT_LEGACY,
    LIFUFirmwareUpdate,
    LIFUTransmitterFirmwareUpdate,
)


def _mgr(dfu_kind=None) -> MagicMock:
    mgr = MagicMock(spec=LIFUDFUManager)
    if dfu_kind is not None:
        mgr.detect_console_dfu_kind.return_value = dfu_kind
    return mgr


class TestLegacyPathsAreGone(unittest.TestCase):

    def test_no_legacy_migration_methods_remain(self):
        for name in ("migrate_console_legacy", "migrate_transmitter_legacy_usb",
                     "program_transmitter_slave_legacy_i2c"):
            self.assertFalse(hasattr(LIFUDFUManager, name), name)
        import openlifu_sdk.io.LIFUDFU as dfu
        self.assertFalse(hasattr(dfu, "build_legacy_metadata"))


class TestConsoleLegacyRefused(unittest.TestCase):

    def _fw(self, hv=None, dfu_kind=None) -> LIFUFirmwareUpdate:
        fw = LIFUFirmwareUpdate(hv=hv)
        fw._mgr = _mgr(dfu_kind)
        return fw

    def test_a_running_legacy_app_is_refused_without_entering_dfu(self):
        for ver in ("1.2.3", "1.2.5"):
            with self.subTest(ver=ver):
                hv = MagicMock()
                hv.get_version.return_value = ver
                fw = self._fw(hv=hv)
                self.assertEqual(fw.detect_cohort(), (COHORT_LEGACY, "app"))
                with self.assertRaisesRegex(RuntimeError, r"LEGACY bootloader .*SR-021.*BOOT0"):
                    fw.update()
                hv.enter_dfu.assert_not_called()
                self.assertEqual(fw._mgr.method_calls, [])

    def test_a_unit_parked_in_the_legacy_dfu_is_refused(self):
        fw = self._fw(dfu_kind=(DFU_KIND_LEGACY, "0.0.3"))
        with self.assertRaisesRegex(RuntimeError, r"LEGACY bootloader \(its DFU\)"):
            fw.update()
        self.assertEqual([c[0] for c in fw._mgr.method_calls], ["detect_console_dfu_kind"])

    def test_force_production_from_the_legacy_dfu_is_refused(self):
        fw = self._fw(dfu_kind=(DFU_KIND_LEGACY, "0.0.3"))
        with self.assertRaisesRegex(RuntimeError, r"LEGACY bootloader \(its DFU\).*SR-021"):
            fw.update(force_production=True)
        self.assertEqual([c[0] for c in fw._mgr.method_calls], ["detect_console_dfu_kind"])

    def test_a_running_legacy_app_can_still_take_force_production(self):
        # The ROM-loader reflash needs no key (beta/unlocked units; option 2b keeps it in the SDK).
        hv = MagicMock()
        hv.get_version.return_value = "1.2.4"
        fw = self._fw(hv=hv)
        self.assertEqual(fw.update(force_production=True).action, "force-production")
        fw._mgr.migrate_console_full_image.assert_called_once()
        self.assertEqual(fw._mgr.migrate_console_full_image.call_args.kwargs["enter_stm32_rom_dfu_fn"],
                         fw._enter_rom)

    def test_the_refusal_of_a_running_legacy_app_names_force_production(self):
        hv = MagicMock()
        hv.get_version.return_value = "1.2.4"
        with self.assertRaisesRegex(RuntimeError, "--force-production"):
            self._fw(hv=hv).update()

    def test_a_secure_unit_still_takes_the_app_update(self):
        hv = MagicMock()
        hv.get_version.return_value = "1.2.6"
        fw = self._fw(hv=hv)
        fw._mgr.update_console.return_value = "OW DFU 1.0.3"
        self.assertEqual(fw.update().action, "app-update")
        fw._mgr.update_console.assert_called_once()


class TestTransmitterLegacyRefused(unittest.TestCase):

    def test_a_master_parked_in_the_legacy_dfu_is_refused(self):
        fw = LIFUTransmitterFirmwareUpdate(tx=None)
        fw._mgr = _mgr((DFU_KIND_LEGACY, "0.0.5"))
        for call in (fw.update, lambda: fw.update(force_production=True)):
            with self.subTest(call=call):
                with self.assertRaisesRegex(RuntimeError, r"LIFU BL DFU 0\.0\.5.*SR-021"):
                    call()
        self.assertEqual({c[0] for c in fw._mgr.method_calls}, {"detect_console_dfu_kind"})

    def test_a_running_legacy_master_still_migrates_through_the_rom_loader(self):
        # No key involved: the 0x77 force switch reaches the STM32 ROM loader.
        tx = MagicMock()
        tx.get_version.return_value = "2.0.5"
        fw = LIFUTransmitterFirmwareUpdate(tx=tx)
        fw._mgr = _mgr()
        self.assertEqual(fw.update().action, "migrate-rom")
        fw._mgr.migrate_transmitter_full_image.assert_called_once()

    def test_a_legacy_slave_is_refused_before_dfu_entry(self):
        tx = MagicMock()
        tx.get_version.return_value = "2.0.6"
        fw = LIFUTransmitterFirmwareUpdate(tx=tx)
        for force_production in (False, True):
            with self.subTest(force_production=force_production):
                with self.assertRaisesRegex(RuntimeError, r"slave module 1 .*LEGACY .*module 0"):
                    fw.update_slave(1, force_production=force_production)
                tx.enter_dfu.assert_not_called()

    def test_a_secure_slave_still_takes_its_paths(self):
        for force_production, path in ((False, "_update_slave_secure"),
                                       (True, "_update_slave_force_production")):
            with self.subTest(force_production=force_production):
                tx = MagicMock()
                tx.get_version.return_value = "2.0.8"
                fw = LIFUTransmitterFirmwareUpdate(tx=tx)
                setattr(fw, path, MagicMock(return_value="done"))
                self.assertEqual(fw.update_slave(1, force_production=force_production), "done")
                getattr(fw, path).assert_called_once()

    def test_a_no_bootloader_slave_gets_its_own_message(self):
        tx = MagicMock()
        tx.get_version.return_value = "2.0.3"
        fw = LIFUTransmitterFirmwareUpdate(tx=tx)
        with self.assertRaisesRegex(RuntimeError, "NO bootloader") as raised:
            fw.update_slave(1)
        self.assertNotIn("LEGACY", str(raised.exception))
        tx.enter_dfu.assert_not_called()

    def test_an_unreadable_slave_version_is_refused(self):
        tx = MagicMock()
        tx.get_version.side_effect = RuntimeError("no reply")
        fw = LIFUTransmitterFirmwareUpdate(tx=tx)
        with self.assertRaisesRegex(RuntimeError, "could not read slave 1 version"):
            fw.update_slave(1)
        tx.enter_dfu.assert_not_called()


if __name__ == "__main__":
    unittest.main()
