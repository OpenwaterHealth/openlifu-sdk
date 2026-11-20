# SPDX-License-Identifier: Apache-2.0
"""Bootloader migration through the updater shim (open-lifu-*-bl-updater).

A unit on a secure bootloader older than 1.1.0 first gets the shim, which
replaces the bootloader and comes back in the new bootloader's DFU; the
application is installed there. Devices, the DFU manager and the I2C DFU
client are faked: what is checked is the sequence of operations.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from openlifu_sdk.io import LIFUFirmwareUpdate as fu
from openlifu_sdk.io.LIFUConfig import NODE_MODE_APP, NODE_MODE_BOOTLOADER

SHIM = "shim-signed.bin"


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s

    monkeypatch.setattr(fu.time, "sleep", sleep)
    monkeypatch.setattr(fu.time, "monotonic", lambda: clock["t"])


@pytest.fixture
def shim(tmp_path):
    p = tmp_path / SHIM
    p.write_bytes(b"shim")
    return str(p)


@pytest.fixture(autouse=True)
def app_image(tmp_path, monkeypatch):
    """The images come from the caller (the SDK carries none): a real file."""
    p = tmp_path / "app-signed.bin"
    p.write_bytes(b"app")
    monkeypatch.setattr(sys.modules[__name__], "APP", str(p))
    return str(p)


APP = None   # set per test by app_image


@pytest.mark.parametrize("version, needs", [
    ("1.0.3-rc.1", True), ("1.0.1", True), ("1.0.0", True), ("v1.0.2-dirty", True),
    ("1.1.0-rc.1", False), ("1.1.0", False), ("1.2.0", False), ("2.0.0", False),
    ("", False), (None, False), ("garbage", False),
])
def test_bootloader_needs_migration(version, needs):
    assert fu.bootloader_needs_migration(version) is needs


# ---------------------------------------------------------------- console (USB)

def console(bl_versions, app_version="1.2.6", slot_empty=True):
    hv = MagicMock()
    hv.get_version.return_value = app_version
    upd = fu.LIFUFirmwareUpdate(hv=hv)
    mgr = MagicMock()
    mgr.get_console_bootloader_version.side_effect = list(bl_versions)
    if isinstance(slot_empty, list):    # installed version per poll: None = empty
        mgr.get_console_installed_version.side_effect = slot_empty
    else:
        mgr.get_console_installed_version.return_value = None if slot_empty else 2143
    upd._mgr = mgr
    return upd, hv, mgr


def programmed(mgr, method):
    return [c.args[0] for c in getattr(mgr, method).call_args_list]


def test_console_old_bootloader_gets_the_shim_then_the_app(shim):
    # old BL, old BL still visible while the shim works, then the new BL
    upd, hv, mgr = console(["1.0.3-rc.1", "1.0.3-rc.1", "1.1.0-rc.3"])
    r = upd.update(signed_app=APP, bl_updater=shim)
    hv.enter_dfu.assert_called_once()
    assert programmed(mgr, "program_console") == [shim, APP]
    assert r.action == "bl-migrate" and r.bl_version == "1.1.0-rc.3"
    mgr.abort_dfu.assert_not_called()


def test_console_current_bootloader_gets_the_app_only(shim):
    upd, _, mgr = console(["1.1.0-rc.3"], app_version="1.3.0")
    r = upd.update(signed_app=APP, bl_updater=shim)
    assert programmed(mgr, "program_console") == [APP]
    assert r.action == "app-update"


def test_console_no_updater_image_writes_nothing():
    upd, _, mgr = console(["1.0.3-rc.1"])
    with pytest.raises(RuntimeError, match="no bootloader updater image was given"):
        upd.update(signed_app=APP)
    mgr.program_console.assert_not_called()


def test_no_app_image_is_refused_before_dfu(shim):
    upd, hv, mgr = console(["1.1.0-rc.3"], app_version="1.3.0")
    with pytest.raises(ValueError, match="no signed application image given"):
        upd.update(bl_updater=shim)
    hv.enter_dfu.assert_not_called()
    mgr.program_console.assert_not_called()


def test_console_replace_bootloader_on_a_current_bootloader(shim):
    # research 1.1.0-rc.3 -> the updater -> (old BL still holds the updater) -> new BL, empty slot
    upd, _, mgr = console(["1.1.0-rc.3", "1.1.0-rc.3", "1.1.0-rc.3"], app_version="1.3.0",
                          slot_empty=[2143, None])
    r = upd.update(signed_app=APP, bl_updater=shim, replace_bootloader=True)
    assert programmed(mgr, "program_console") == [shim, APP]
    assert r.action == "bl-migrate"
    assert mgr.get_console_installed_version.call_count == 2


def test_console_current_bootloader_is_kept_without_replace(shim):
    upd, _, mgr = console(["1.1.0-rc.3"], app_version="1.3.0")
    upd.update(signed_app=APP, bl_updater=shim)
    assert programmed(mgr, "program_console") == [APP]


def test_console_shim_refused_before_erase_boots_the_app_again(shim):
    upd, _, mgr = console(["1.0.3-rc.1"])
    mgr.program_console.side_effect = ValueError("downgrade")
    with pytest.raises(ValueError):
        upd.update(signed_app=APP, bl_updater=shim)
    mgr.abort_dfu.assert_called_once()


def test_console_new_bootloader_never_comes_up(shim, monkeypatch):
    monkeypatch.setattr(fu, "MIGRATION_TIMEOUT_S", 10.0)
    upd, _, mgr = console(["1.0.3-rc.1"] + ["1.0.3-rc.1"] * 20)
    with pytest.raises(RuntimeError, match="no newer bootloader came up"):
        upd.update(signed_app=APP, bl_updater=shim)
    assert programmed(mgr, "program_console") == [shim]   # the app was not attempted


def test_console_already_in_old_bootloader_dfu(shim):
    upd, hv, mgr = console(["1.0.3-rc.1", "1.1.0-rc.3"])
    hv.get_version.side_effect = RuntimeError("no app")       # unit sits in DFU
    mgr.detect_console_dfu_kind.return_value = (fu.DFU_KIND_SECURE, "1.0.3-rc.1")
    r = upd.update(signed_app=APP, bl_updater=shim)
    hv.enter_dfu.assert_not_called()
    assert programmed(mgr, "program_console") == [shim, APP]
    assert r.action == "bl-migrate"


# ---------------------------------------------------------------- transmitter master (USB)

def master(bl_versions, app_version="2.0.8"):
    tx = MagicMock()
    tx.get_version.return_value = app_version
    upd = fu.LIFUTransmitterFirmwareUpdate(tx=tx)
    mgr = MagicMock()
    mgr._wait_for_dfu_kind.side_effect = list(bl_versions)
    mgr.get_transmitter_installed_version.return_value = None
    upd._mgr = mgr
    return upd, tx, mgr


def test_master_old_bootloader_gets_the_shim_then_the_app(shim):
    upd, tx, mgr = master(["1.0.1", "1.1.0-rc.1"])
    r = upd.update(signed_app=APP, bl_updater=shim)
    assert programmed(mgr, "program_transmitter") == [shim, APP]
    assert r.action == "bl-migrate" and r.bl_version == "1.1.0-rc.1"
    for c in mgr._wait_for_dfu_kind.call_args_list:
        assert c.args[0] == fu.DFU_KIND_SECURE


def test_master_current_bootloader_gets_the_app_only(shim):
    upd, _, mgr = master(["1.1.0-rc.1"], app_version="2.1.0")
    r = upd.update(signed_app=APP, bl_updater=shim)
    assert programmed(mgr, "program_transmitter") == [APP]
    assert r.action == "app-update"


# ---------------------------------------------------------------- transmitter slave (I2C)

class FakeChain:
    """The master's view of a 2-module chain. The slave runs its app until it
    is asked for DFU; after that it is a bootloader node."""

    def __init__(self):
        self.uart = object()
        self.modes = {1: NODE_MODE_APP}
        self.log = []

    def get_module_count(self):
        return 2

    def enumerate_modules(self):
        self.log.append(("enumerate", dict(self.modes)))
        return 2

    def enter_dfu(self, module):
        self.log.append(("enter_dfu", module))
        self.modes[module] = NODE_MODE_BOOTLOADER

    def get_module_mode(self, module):
        return self.modes[module]

    def get_module_i2c_addr(self, module):
        return 0x20

    def get_version(self, module=0):
        return "2.1.0-rc.4"


@pytest.fixture
def slave_env(monkeypatch):
    chain = FakeChain()
    versions = iter(["1.0.1", "1.0.1", "1.1.0-rc.1"])

    class FakeI2C:
        def __init__(self, uart, i2c_addr):
            assert i2c_addr == 0x20

        def get_version(self):
            return next(versions)

        def read_memory(self, address, length):
            assert address == fu.SLAVE_SLOT_BASE
            return bytes([0xFF]) * length        # the updater erased the slot

    mgr = MagicMock()
    mgr.program_transmitter_slave_i2c.return_value = "1.1.0-rc.1"
    monkeypatch.setattr(fu, "STM32I2CDFUviaMaster", FakeI2C)
    monkeypatch.setattr(fu, "LIFUDFUManager", lambda uart=None: mgr)
    return chain, mgr


def test_slave_old_bootloader_gets_the_shim_then_the_app(slave_env, shim):
    chain, mgr = slave_env
    upd = fu.LIFUTransmitterFirmwareUpdate(tx=chain)
    r = upd.update_slave(1, signed_app=APP, legacy=False, bl_updater=shim)
    assert [c.args[0] for c in mgr.program_transmitter_slave_i2c.call_args_list] == [shim, APP]
    assert r.action == "bl-migrate-i2c"
    # Every re-enumeration happened with the slave out of its application (#80).
    enum_modes = [modes for what, modes in chain.log if what == "enumerate"]
    assert enum_modes and all(m[1] == NODE_MODE_BOOTLOADER for m in enum_modes[:-1])
    assert chain.log[0] == ("enter_dfu", 1)


def test_slave_module_version_does_not_re_enumerate():
    chain = FakeChain()
    ver, mode = fu.LIFUTransmitterFirmwareUpdate(tx=chain).slave_module_version(1)
    assert (ver, mode) == ("2.1.0-rc.4", NODE_MODE_APP)
    assert not any(what == "enumerate" for what, _ in chain.log)


def test_update_chain_updates_slaves_before_the_master():
    tx = MagicMock()
    tx.get_module_count.return_value = 3
    upd = fu.LIFUTransmitterFirmwareUpdate(tx=tx)
    order = []
    upd.update_slave = lambda m, **kw: order.append(("slave", m)) or f"s{m}"
    upd.update = lambda **kw: order.append(("master", 0)) or "m"
    assert upd.update_chain(signed_app=APP) == ["s1", "s2", "m"]
    assert order == [("slave", 1), ("slave", 2), ("master", 0)]
