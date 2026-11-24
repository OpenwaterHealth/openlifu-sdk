# SPDX-License-Identifier: Apache-2.0
"""Presets written in the bootloader DFU of a board that keeps its
application (a clinical unit): LIFUFirmwareUpdate.write_presets and
LIFUTransmitterFirmwareUpdate.write_presets / write_slave_presets.

Every preset write ends in a reset that boots the kept application, so DFU
is requested from the application for the first write and again before each
further one, never after the last; a failed session leaves DFU.
"""

from __future__ import annotations

import pytest

from openlifu_sdk.io import LIFUFirmwareUpdate as fu
from openlifu_sdk.io.LIFUConfig import NODE_MODE_BOOTLOADER


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(fu.time, "sleep", lambda _s: None)


class FakeApp:
    """A running application: answers its version, takes the DFU request."""

    def __init__(self, log, version="1.3.1"):
        self.log, self.version = log, version

    def is_connected(self):
        return True

    def connect(self):
        return True

    def get_version(self, module=0):
        return self.version

    def enter_dfu(self, module=0):
        self.log.append(("enter dfu", module))


class FakeMgr:
    def __init__(self, log):
        self.log = log

    def get_console_bootloader_version(self, timeout_s=0, **_):
        self.log.append("console dfu up")
        return "1.1.0-clinical"

    def _wait_for_dfu_kind(self, kind, timeout_s=0, **_):
        self.log.append("transmitter dfu up")
        return "1.1.0-clinical"

    def abort_dfu(self, profile=None, **_):
        self.log.append(("leave dfu", profile.name))


def _writer(log, n, fail_at=None):
    """A preset writer of *n* slots: each write resets the board, so it
    waits (wait_dfu / reacquire) between writes only."""
    def write(ctx):
        assert ctx.returns_to_dfu is False
        wait = ctx.wait_dfu or ctx.reacquire
        for slot in range(n):
            if slot == fail_at:
                raise RuntimeError(f"slot {slot} refused")
            log.append(("write", slot))
            if slot < n - 1:
                wait()
    return write


def test_console_presets_are_written_from_the_running_application():
    log = []
    update = fu.LIFUFirmwareUpdate(hv=FakeApp(log))
    update._mgr = FakeMgr(log)
    result = update.write_presets(_writer(log, 1))
    assert log == [("enter dfu", 0), "console dfu up", ("write", 0)]
    assert result.action == "presets-dfu" and result.bl_version == "1.1.0-clinical"
    assert "1.3.1 is running" in result.summary and result.reboot_required is False


def test_dfu_is_requested_again_between_two_writes_and_not_after_the_last():
    log = []
    update = fu.LIFUTransmitterFirmwareUpdate(tx=FakeApp(log, "2.1.0"))
    update._mgr = FakeMgr(log)
    update.write_presets(_writer(log, 3))
    assert log == [("enter dfu", 0), "transmitter dfu up", ("write", 0),
                   ("enter dfu", 0), "transmitter dfu up", ("write", 1),
                   ("enter dfu", 0), "transmitter dfu up", ("write", 2)]


def test_a_failed_write_leaves_dfu_and_is_raised():
    log = []
    update = fu.LIFUTransmitterFirmwareUpdate(tx=FakeApp(log, "2.1.0"))
    update._mgr = FakeMgr(log)
    with pytest.raises(RuntimeError, match="slot 1 refused"):
        update.write_presets(_writer(log, 3, fail_at=1))
    assert log[-1] == ("leave dfu", fu.TRANSMITTER_PROFILE.name)


def test_an_application_that_does_not_come_back_stops_the_session(monkeypatch):
    log = []
    hv = FakeApp(log)
    update = fu.LIFUFirmwareUpdate(hv=hv)
    update._mgr = FakeMgr(log)
    monkeypatch.setattr(fu, "PRESET_APP_TIMEOUT_S", 0.0)

    def write(ctx):
        log.append(("write", 0))
        hv.version = ""           # the application never answers again
        ctx.wait_dfu()

    with pytest.raises(RuntimeError, match="did not answer"):
        update.write_presets(write)
    assert log[-1] == ("leave dfu", fu.CONSOLE_PROFILE.name)


class FakeChain(FakeApp):
    """A master brokering slave module 1: the DFU request parks the slave in
    its bootloader (a new I2C address each time); a reset boots it back."""

    def __init__(self, log):
        super().__init__(log, "2.1.0")
        self.uart = object()
        self.slave_mode = "app"
        self.addr = 0x40

    def get_module_count(self):
        return 2

    def enter_dfu(self, module=0):
        self.log.append(("enter dfu", module))
        self.slave_mode, self.addr = "bootloader", self.addr + 1

    def enumerate_modules(self):
        return 2

    def get_module_mode(self, module):
        return NODE_MODE_BOOTLOADER if self.slave_mode == "bootloader" else 0

    def get_module_i2c_addr(self, module):
        return self.addr


def test_a_slave_is_reacquired_at_its_new_address_between_writes(monkeypatch):
    log = []
    tx = FakeChain(log)

    class I2cDfu:
        def __init__(self, uart, i2c_addr):
            self.addr = i2c_addr

        def get_version(self):
            return "1.1.0-clinical"

        def reset(self):
            log.append(("reset slave", self.addr))

    monkeypatch.setattr(fu, "STM32I2CDFUviaMaster", I2cDfu)
    monkeypatch.setattr(fu, "LIFUDFUManager", lambda uart=None: "i2c mgr")
    seen = []

    def write(ctx):
        for slot in range(2):
            seen.append((slot, ctx.i2c_addr, ctx.mgr))
            tx.slave_mode = "app"          # the write's reset boots the slave application
            if slot == 0:
                ctx.reacquire()

    result = fu.LIFUTransmitterFirmwareUpdate(tx=tx).write_slave_presets(1, write)
    assert seen == [(0, 0x41, "i2c mgr"), (1, 0x42, "i2c mgr")]
    assert log == [("enter dfu", 1), ("enter dfu", 1)]
    assert result.action == "presets-dfu-i2c" and "now 2.1.0" in result.summary


def test_a_failed_slave_write_resets_the_slave_out_of_dfu(monkeypatch):
    log = []
    tx = FakeChain(log)

    class I2cDfu:
        def __init__(self, uart, i2c_addr):
            self.addr = i2c_addr

        def get_version(self):
            return "1.1.0-clinical"

        def reset(self):
            log.append(("reset slave", self.addr))

    monkeypatch.setattr(fu, "STM32I2CDFUviaMaster", I2cDfu)
    monkeypatch.setattr(fu, "LIFUDFUManager", lambda uart=None: "i2c mgr")

    def write(_ctx):
        raise RuntimeError("stage refused")

    with pytest.raises(RuntimeError, match="stage refused"):
        fu.LIFUTransmitterFirmwareUpdate(tx=tx).write_slave_presets(1, write)
    assert log == [("enter dfu", 1), ("reset slave", 0x41)]
