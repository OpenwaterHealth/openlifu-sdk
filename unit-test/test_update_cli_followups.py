# SPDX-License-Identifier: Apache-2.0
"""Update CLI follow-ups (SR-021).

- A secure-bootloader update (app-update / bl-migrate) on a console or a
  transmitter master ends with the DFU manifest: the bootloader resets and
  launches the new application, so no power-cycle is asked for.
- After a slave I2C update the slave application may not answer the version
  read yet ("v0.0.0" placeholder): only the read is retried, bounded, and the
  summary names the version only when a real one was read.

Devices and the DFU manager are faked (autospec where they stand in for a
real class); time is a fake clock.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, create_autospec

import pytest

from openlifu_sdk.io import LIFUFirmwareUpdate as fu
from openlifu_sdk.io.LIFUConfig import NODE_MODE_BOOTLOADER
from openlifu_sdk.io.LIFUDFU import LIFUDFUManager, STM32I2CDFUviaMaster
from openlifu_sdk.io.LIFUHVController import LIFUHVController
from openlifu_sdk.io.LIFUTXDevice import LIFUTxDevice


@pytest.fixture
def clock(monkeypatch):
    c = {"t": 0.0}
    monkeypatch.setattr(fu.time, "sleep", lambda s: c.__setitem__("t", c["t"] + s))
    monkeypatch.setattr(fu.time, "monotonic", lambda: c["t"])
    return c


@pytest.fixture
def images(tmp_path):
    app, shim = tmp_path / "app-signed.bin", tmp_path / "shim-signed.bin"
    app.write_bytes(b"app")
    shim.write_bytes(b"shim")
    return str(app), str(shim)


# ------------------------------------------------- secure USB paths: no power-cycle

def _console(bl_versions):
    hv = create_autospec(LIFUHVController, instance=True)
    hv.get_version.return_value = "1.3.0"
    upd = fu.LIFUFirmwareUpdate(hv=hv)
    mgr = create_autospec(LIFUDFUManager, instance=True)
    mgr.get_console_bootloader_version.side_effect = list(bl_versions)
    mgr.get_console_installed_version.return_value = None
    upd._mgr = mgr
    return upd, upd.detect_cohort


def _master(bl_versions):
    tx = create_autospec(LIFUTxDevice, instance=True)
    tx.get_version.return_value = "2.1.0"
    upd = fu.LIFUTransmitterFirmwareUpdate(tx=tx)
    mgr = create_autospec(LIFUDFUManager, instance=True)
    mgr._wait_for_dfu_kind.side_effect = list(bl_versions)
    mgr.get_transmitter_installed_version.return_value = None
    upd._mgr = mgr
    return upd, upd.detect


SECURE_CASES = [
    (_console, "console", ["1.1.0-rc.3"], "app-update"),
    (_console, "console", ["1.0.3-rc.1", "1.1.0-rc.3"], "bl-migrate"),
    (_master, "transmitter", ["1.1.0-rc.1"], "app-update"),
    (_master, "transmitter", ["1.0.1", "1.1.0-rc.1"], "bl-migrate"),
]


@pytest.mark.parametrize("make, label, bl_versions, action", SECURE_CASES)
def test_secure_update_needs_no_power_cycle(clock, images, capsys, make, label,
                                            bl_versions, action):
    app, shim = images
    upd, detect = make(bl_versions)
    args = SimpleNamespace(detect=False, bl_version=False,
                           force_production=False, yes=True)
    rc = fu._run_update_flow(
        upd, label, detect,
        lambda: upd.update(signed_app=app, bl_updater=shim), args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "rebooting into the new app" in out
    assert "Power-cycle" not in out


@pytest.mark.parametrize("make, label, bl_versions, action", SECURE_CASES)
def test_secure_update_result_reboot_not_required(clock, images, make, label,
                                                  bl_versions, action):
    app, shim = images
    upd, _ = make(bl_versions)
    r = upd.update(signed_app=app, bl_updater=shim)
    assert r.action == action
    assert r.reboot_required is False


def test_cli_still_asks_for_power_cycle_when_required(capsys):
    """Paths that do need a power-cycle keep the message."""
    result = fu.UpdateResult(fu.COHORT_LEGACY, "migrate-legacy", "Migrated.",
                             reboot_required=True)
    args = SimpleNamespace(detect=False, bl_version=False,
                           force_production=False, yes=True)
    rc = fu._run_update_flow(MagicMock(), "console",
                             lambda: (fu.COHORT_LEGACY, "app"),
                             lambda: result, args)
    assert rc == 0
    assert "Power-cycle the console to boot the new application." in capsys.readouterr().out


# ------------------------------------------------- slave post-update version read

def _slave_tx(versions):
    """A master whose slave 1 is parked in its bootloader (no DFU request)
    and whose version reads return *versions* in turn (the last repeats)."""
    tx = create_autospec(LIFUTxDevice, instance=True)
    tx.uart = object()
    tx.is_connected.return_value = True
    tx.enumerate_modules.return_value = 2
    tx.get_module_count.return_value = 2
    tx.get_module_mode.return_value = NODE_MODE_BOOTLOADER
    tx.get_module_i2c_addr.return_value = 0x20
    it = iter(versions)
    last = {"v": versions[-1]}

    def get_version(module=0):
        v = next(it, last["v"])
        if isinstance(v, Exception):
            raise v
        return v

    tx.get_version.side_effect = get_version
    return tx


@pytest.fixture
def slave_mgr(monkeypatch):
    mgr = create_autospec(LIFUDFUManager, instance=True)
    mgr.program_transmitter_slave_i2c.return_value = "1.1.0-rc.1"
    monkeypatch.setattr(fu, "LIFUDFUManager", lambda uart=None: mgr)

    def make_client(uart, i2c_addr):
        client = create_autospec(STM32I2CDFUviaMaster, instance=True)
        client.get_version.return_value = "1.1.0-rc.1"
        return client

    monkeypatch.setattr(fu, "STM32I2CDFUviaMaster", make_client)
    return mgr


@pytest.mark.parametrize("version, real", [
    ("2.1.0-rc.6", True), ("v2.1.0", True), ("1.0.0", True),
    ("v0.0.0", False), ("0.0.0", False), ("", False), (None, False),
    ("garbage", False),
])
def test_is_real_version(version, real):
    assert fu._is_real_version(version) is real


def test_slave_version_retried_until_valid(clock):
    tx = _slave_tx(["v0.0.0", RuntimeError("timeout"), "v0.0.0", "2.1.0-rc.6"])
    ver = fu.LIFUTransmitterFirmwareUpdate(tx=tx)._slave_post_boot_version(1, 6.0)
    assert ver == "2.1.0-rc.6"
    assert tx.get_version.call_count == 4
    tx.enumerate_modules.assert_called_once()           # never re-enumerated
    assert clock["t"] <= 6.0 + fu.SLAVE_VERSION_TIMEOUT_S


def test_slave_version_never_valid_is_bounded(clock, caplog):
    tx = _slave_tx(["v0.0.0"])
    with caplog.at_level("WARNING", logger=fu.logger.name):
        ver = fu.LIFUTransmitterFirmwareUpdate(tx=tx)._slave_post_boot_version(1, 6.0)
    assert ver == ""
    tx.enumerate_modules.assert_called_once()
    expected_reads = int(fu.SLAVE_VERSION_TIMEOUT_S / fu.SLAVE_VERSION_POLL_S) + 1
    assert tx.get_version.call_count == expected_reads
    assert clock["t"] <= 6.0 + fu.SLAVE_VERSION_TIMEOUT_S + fu.SLAVE_VERSION_POLL_S
    assert "did not report a valid app version" in caplog.text


def test_slave_version_enumeration_failure_returns_empty(clock):
    tx = _slave_tx(["2.1.0"])
    tx.enumerate_modules.side_effect = RuntimeError("TX not connected")
    assert fu.LIFUTransmitterFirmwareUpdate(tx=tx)._slave_post_boot_version(1, 6.0) == ""
    tx.get_version.assert_not_called()


def test_slave_summary_names_the_version_once_valid(clock, images, slave_mgr):
    app, shim = images
    tx = _slave_tx(["v0.0.0", "v0.0.0", "2.1.0-rc.6"])
    r = fu.LIFUTransmitterFirmwareUpdate(tx=tx).update_slave(
        1, signed_app=app, bl_updater=shim)
    assert r.action == "app-update-i2c"
    assert r.summary.endswith("(now 2.1.0-rc.6)")
    tx.enumerate_modules.assert_called_once()


def test_slave_summary_omits_an_invalid_version(clock, images, slave_mgr):
    app, shim = images
    tx = _slave_tx(["v0.0.0"])
    r = fu.LIFUTransmitterFirmwareUpdate(tx=tx).update_slave(
        1, signed_app=app, bl_updater=shim)
    assert "(now" not in r.summary and "v0.0.0" not in r.summary
    assert r.summary.endswith(".")
