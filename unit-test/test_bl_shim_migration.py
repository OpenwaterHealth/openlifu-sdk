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
    is asked for DFU; after that it is a bootloader node. Records each call
    with the fake clock, and can drop its port once (a master watchdog reset)."""

    def __init__(self, clock, mode=NODE_MODE_APP, drop_port_once=False):
        self.uart = object()
        self.modes = {1: mode}
        self.log = []
        self.clock = clock
        self.connected = True
        self.drop_port_once = drop_port_once

    def is_connected(self):
        return self.connected

    def connect(self):
        self.log.append(("connect", self.clock["t"]))
        self.connected = True
        return True

    def get_module_count(self):
        return 2

    def enumerate_modules(self):
        if self.drop_port_once:
            self.drop_port_once = False
            self.connected = False
            raise RuntimeError("TX port lost")
        if not self.connected:
            raise RuntimeError("TX not connected")
        self.log.append(("enumerate", self.clock["t"], dict(self.modes)))
        return 2

    def enter_dfu(self, module):
        self.log.append(("enter_dfu", module))
        self.modes[module] = NODE_MODE_BOOTLOADER

    def get_module_mode(self, module):
        return self.modes[module]

    def get_module_i2c_addr(self, module):
        return 0x20

    def get_version(self, module=0):
        if module and self.modes[module] == NODE_MODE_BOOTLOADER:
            raise RuntimeError("no application")
        return "2.1.0-rc.4"


@pytest.fixture
def clock(monkeypatch):
    c = {"t": 0.0}
    monkeypatch.setattr(fu.time, "sleep", lambda s: c.__setitem__("t", c["t"] + s))
    monkeypatch.setattr(fu.time, "monotonic", lambda: c["t"])
    return c


def i2c_versions(monkeypatch, versions):
    """Fake STM32I2CDFUviaMaster with the real class's interface only: a
    method the real client does not have fails the test (autospec)."""
    from unittest.mock import create_autospec
    from openlifu_sdk.io.LIFUDFU import STM32I2CDFUviaMaster
    it = iter(versions)

    def make(uart, i2c_addr):
        assert i2c_addr == 0x20
        client = create_autospec(STM32I2CDFUviaMaster, instance=True)
        client.get_version.side_effect = lambda: next(it)
        return client

    monkeypatch.setattr(fu, "STM32I2CDFUviaMaster", make)


@pytest.fixture
def mgr(monkeypatch):
    m = MagicMock()
    m.program_transmitter_slave_i2c.return_value = "1.1.0-rc.1"
    monkeypatch.setattr(fu, "LIFUDFUManager", lambda uart=None: m)
    return m


def written(mgr):
    return [c.args[0] for c in mgr.program_transmitter_slave_i2c.call_args_list]


def test_slave_old_bootloader_gets_the_shim_then_the_app(clock, mgr, monkeypatch, shim):
    i2c_versions(monkeypatch, ["1.0.1", "1.1.0-rc.1"])
    chain = FakeChain(clock)
    r = fu.LIFUTransmitterFirmwareUpdate(tx=chain).update_slave(
        1, signed_app=APP, legacy=False, bl_updater=shim)
    assert written(mgr) == [shim, APP]
    assert r.action == "bl-migrate-i2c"
    assert chain.log[0] == ("enter_dfu", 1)


def test_slave_first_re_enumeration_waits_for_the_updater(clock, mgr, monkeypatch, shim):
    """No enumeration while the updater rewrites the bootloader (#80): the
    first one after the updater is written comes after the settle time."""
    i2c_versions(monkeypatch, ["1.0.1", "1.1.0-rc.1"])
    chain = FakeChain(clock)
    upd = fu.LIFUTransmitterFirmwareUpdate(tx=chain)
    t_written = {}
    mgr.program_transmitter_slave_i2c.side_effect = (
        lambda img, **kw: t_written.setdefault(img, clock["t"]) and "1.1.0-rc.1")
    upd.update_slave(1, signed_app=APP, legacy=False, bl_updater=shim)
    enums = [t for what, t, *_ in chain.log if what == "enumerate"]
    after_shim = [t for t in enums if t > t_written[shim]]
    assert after_shim and after_shim[0] - t_written[shim] >= fu.SLAVE_MIGRATION_SETTLE_S


def test_slave_wait_reopens_a_lost_master_port(clock, mgr, monkeypatch, shim):
    i2c_versions(monkeypatch, ["1.0.1", "1.1.0-rc.1"])
    chain = FakeChain(clock, drop_port_once=True)
    chain.modes[1] = NODE_MODE_BOOTLOADER          # parked; first enumerate drops the port
    r = fu.LIFUTransmitterFirmwareUpdate(tx=chain).update_slave(
        1, signed_app=APP, legacy=False, bl_updater=shim)
    assert any(what == "connect" for what, *_ in chain.log)
    assert written(mgr) == [shim, APP] and r.action == "bl-migrate-i2c"


def test_slave_new_bootloader_never_comes_up(clock, mgr, monkeypatch, shim):
    i2c_versions(monkeypatch, ["1.0.1"] + ["1.0.1"] * 50)
    chain = FakeChain(clock)
    with pytest.raises(RuntimeError, match="no newer bootloader came up"):
        fu.LIFUTransmitterFirmwareUpdate(tx=chain).update_slave(
            1, signed_app=APP, legacy=False, bl_updater=shim)
    assert written(mgr) == [shim]


def test_slave_parked_in_its_bootloader_is_resumed(clock, mgr, monkeypatch, shim):
    """A slave left in its bootloader's DFU (no application): no app version
    read, no DFU request; the app goes straight into its DFU."""
    i2c_versions(monkeypatch, ["1.1.0-rc.1"])
    chain = FakeChain(clock, mode=NODE_MODE_BOOTLOADER)
    r = fu.LIFUTransmitterFirmwareUpdate(tx=chain).update_slave(1, signed_app=APP, bl_updater=shim)
    assert written(mgr) == [APP]
    assert not any(what == "enter_dfu" for what, *_ in chain.log)
    assert r.action == "app-update-i2c"


def test_slave_parked_in_an_old_bootloader_gets_the_shim(clock, mgr, monkeypatch, shim):
    i2c_versions(monkeypatch, ["1.0.1", "1.1.0-rc.1"])
    chain = FakeChain(clock, mode=NODE_MODE_BOOTLOADER)
    r = fu.LIFUTransmitterFirmwareUpdate(tx=chain).update_slave(1, signed_app=APP, bl_updater=shim)
    assert written(mgr) == [shim, APP] and r.action == "bl-migrate-i2c"


def test_slave_module_version_does_not_re_enumerate(clock):
    chain = FakeChain(clock)
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
