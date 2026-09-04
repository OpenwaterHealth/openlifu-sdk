from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from openlifu_sdk.ui.simulated_interface import SimulatedLIFUInterface
from openlifu_sdk.util.firmware import (
    get_console_firmware_version,
    get_transmitter_firmware_version,
)


class TestSimulatedInterfaceFirmware(unittest.TestCase):
    def setUp(self):
        self._objs = []

    def tearDown(self):
        for obj in self._objs:
            try:
                obj.close()
            except Exception:
                pass

    def _track(self, obj):
        self._objs.append(obj)
        return obj

    def test_default_versions_follow_bundled_firmware(self):
        iface = self._track(SimulatedLIFUInterface())
        expected_console = get_console_firmware_version()
        expected_tx = get_transmitter_firmware_version()
        self.assertEqual(iface.hvcontroller.get_version(), f"v{expected_console}")
        self.assertEqual(iface.txdevice.get_version(), f"v{expected_tx}")
        cfg = iface.txdevice.read_config().json_data
        self.assertEqual(cfg["fw_ver"], expected_tx)

    def test_constructor_supports_version_overrides(self):
        iface = self._track(
            SimulatedLIFUInterface(
                tx_firmware_version="9.8.7",
                hv_firmware_version="1.2.3",
            )
        )
        self.assertEqual(iface.txdevice.get_version(), "v9.8.7")
        self.assertEqual(iface.hvcontroller.get_version(), "v1.2.3")
        self.assertEqual(iface.txdevice.read_config().json_data["fw_ver"], "9.8.7")

        shared = self._track(SimulatedLIFUInterface(firmware_version="4.5.6"))
        self.assertEqual(shared.txdevice.get_version(), "v4.5.6")
        self.assertEqual(shared.hvcontroller.get_version(), "v4.5.6")

    def test_simulated_firmware_update_changes_reported_version(self):
        iface = self._track(SimulatedLIFUInterface(firmware_version="1.0.0"))
        tx_progress = []
        hv_progress = []

        self.assertTrue(
            iface.txdevice.update_firmware(
                module=0,
                firmware_version="2.3.4",
                progress_callback=lambda done, total, label: tx_progress.append((done, total, label)),
            )
        )
        self.assertEqual(iface.txdevice.get_version(), "v2.3.4")
        self.assertEqual(iface.txdevice.read_config().json_data["fw_ver"], "2.3.4")
        self.assertEqual(tx_progress, [(0, 1, "simulated-update"), (1, 1, "simulated-update")])

        self.assertTrue(
            iface.hvcontroller.update_firmware(
                firmware_version="3.2.1",
                progress_callback=lambda done, total, label: hv_progress.append((done, total, label)),
            )
        )
        self.assertEqual(iface.hvcontroller.get_version(), "v3.2.1")
        self.assertEqual(hv_progress, [(0, 1, "simulated-update"), (1, 1, "simulated-update")])

    def test_update_without_args_applies_latest_bundled_versions(self):
        iface = self._track(SimulatedLIFUInterface(firmware_version="0.0.1"))
        iface.txdevice.update_firmware()
        iface.hvcontroller.update_firmware()
        self.assertEqual(iface.txdevice.get_version(), f"v{get_transmitter_firmware_version()}")
        self.assertEqual(iface.hvcontroller.get_version(), f"v{get_console_firmware_version()}")

    def test_simulated_update_can_read_version_from_firmware_file(self):
        iface = self._track(SimulatedLIFUInterface(firmware_version="0.0.1"))
        fw_dir = Path(_SRC) / "openlifu_sdk" / "firmware"
        tx_fw = fw_dir / "openlifu-transmitter-fw-signed.bin"
        hv_fw = fw_dir / "openlifu-console-fw-signed.bin"

        iface.txdevice.update_firmware(module=0, package_file=str(tx_fw))
        iface.hvcontroller.update_firmware(package_file=str(hv_fw))

        self.assertEqual(iface.txdevice.get_version(), f"v{get_transmitter_firmware_version()}")
        self.assertEqual(iface.hvcontroller.get_version(), f"v{get_console_firmware_version()}")


if __name__ == "__main__":
    unittest.main()
