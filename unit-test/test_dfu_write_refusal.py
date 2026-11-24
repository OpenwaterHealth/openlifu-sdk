# SPDX-License-Identifier: Apache-2.0
"""DFU clients against a bootloader that checks the signed header before
erasing (open-lifu-transmitter-bl >= 1.1.0).

The bootloader refuses a wrong-board, tampered, downgraded or oversized image
at its first block. Over USB the device then sits in dfuERROR; over I2C the
slave reports status 0x05 (REJECTED), and while it checks the signature
(~1.5 s, interrupts masked) it is off the bus, so the master's status polls
fail. These tests drive the SDK clients with fake transports that behave that
way and check that a refusal is reported (not retried or skipped) and that
failed polls during the check are ridden out.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openlifu_sdk.io import LIFUDFU
from openlifu_sdk.io.LIFUConfig import OW_ERROR, OW_I2C_PASSTHRU
from openlifu_sdk.io.LIFUDFU import (
    I2C_DFU_STATE_DNBUSY,
    I2C_DFU_STATE_ERROR,
    I2C_DFU_STATUS_BUSY,
    I2C_DFU_STATUS_FLASH_ERR,
    I2C_DFU_STATUS_OK,
    I2C_DFU_STATUS_REJECTED,
    DFUWriteRefusedError,
    STM32I2CDFUviaMaster,
    STM32USBDFU,
)

SLOT = 0x08010000
DFU_IDLE = 2


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(LIFUDFU.time, "sleep", lambda _s: None)


# --------------------------------------------------------------------------
# I2C (through the master's OW_I2C_PASSTHRU)
# --------------------------------------------------------------------------

class FakeMasterUart:
    """Answers passthrough packets. Writes always succeed; each GETSTATUS
    exchange takes the next scripted reply: None = the master reports the
    exchange failed (slave NACKed), else a (status, state) pair."""

    def __init__(self, polls):
        self.polls = list(polls)
        self.writes = []
        self.status_polls = 0

    def send_packet(self, packet_id, packet_type, command, addr, reserved, data):
        assert packet_type == OW_I2C_PASSTHRU
        if command == 0x00:                       # write-only
            self.writes.append(bytes(data))
            return SimpleNamespace(packet_type=OW_I2C_PASSTHRU, data=b"")
        self.status_polls += 1
        reply = self.polls.pop(0) if self.polls else None
        if reply is None:
            return SimpleNamespace(packet_type=OW_ERROR, data=b"")
        return SimpleNamespace(packet_type=OW_I2C_PASSTHRU, data=bytes(reply))


def test_i2c_header_check_rides_out_nacked_polls():
    uart = FakeMasterUart([None, None, None,
                           (I2C_DFU_STATUS_BUSY, I2C_DFU_STATE_DNBUSY),
                           (I2C_DFU_STATUS_OK, 0x02)])
    STM32I2CDFUviaMaster(uart, i2c_addr=0x20).write_block(SLOT, b"\xAA" * 512)
    assert uart.status_polls == 5
    assert len(uart.writes) == 1                  # the DNLOAD was sent once


def test_i2c_rejected_header_is_reported_as_refusal():
    uart = FakeMasterUart([None, None, (I2C_DFU_STATUS_REJECTED, I2C_DFU_STATE_ERROR)])
    with pytest.raises(DFUWriteRefusedError, match="installed firmware is intact"):
        STM32I2CDFUviaMaster(uart, i2c_addr=0x20).write_block(SLOT, b"\xAA" * 512)


def test_i2c_flash_error_is_still_an_error():
    uart = FakeMasterUart([(I2C_DFU_STATUS_FLASH_ERR, I2C_DFU_STATE_ERROR)])
    with pytest.raises(DFUWriteRefusedError, match="status=0x04"):
        STM32I2CDFUviaMaster(uart, i2c_addr=0x20).write_block(SLOT, b"\xAA" * 512)


def test_i2c_slave_that_never_answers_times_out_with_the_cause():
    uart = FakeMasterUart([])                     # every poll fails
    dfu = STM32I2CDFUviaMaster(uart, i2c_addr=0x20)
    with pytest.raises(TimeoutError, match="last poll failed"):
        dfu._wait_while_busy(timeout_s=0.05)
    assert uart.status_polls > 1                  # it kept polling


def test_i2c_failed_write_is_not_retried():
    class WriteFails(FakeMasterUart):
        def send_packet(self, packet_id, packet_type, command, addr, reserved, data):
            if command == 0x00:
                return SimpleNamespace(packet_type=OW_ERROR, data=b"")
            return super().send_packet(packet_id, packet_type, command, addr, reserved, data)

    uart = WriteFails([])
    with pytest.raises(RuntimeError, match="passthrough write failed"):
        STM32I2CDFUviaMaster(uart, i2c_addr=0x20).write_block(SLOT, b"\xAA" * 512)
    assert uart.status_polls == 0


# --------------------------------------------------------------------------
# USB DfuSe
# --------------------------------------------------------------------------

class FakeUsbDfu(STM32USBDFU):
    """STM32USBDFU with the USB control transfers replaced by a minimal model
    of the bootloader's DFU state machine. Data blocks whose DfuSe target
    offset is in *refuse_offsets* fail (dfuERROR, bStatus errVENDOR), as do
    block-0 commands when *fail_block0* is set."""

    def __init__(self, refuse_offsets=(), fail_block0=False):
        self.transfer_size = 1024
        self.program_alignment = 8
        self.state = DFU_IDLE
        self.pointer = None
        self.refuse_offsets = set(refuse_offsets)
        self.fail_block0 = fail_block0
        self.data_blocks = []                     # (offset, len) per data DNLOAD

    def get_status(self):
        err = self.state == self.STATE_DFU_ERROR
        return {"status": 0x0B if err else 0x00, "poll_timeout_ms": 0, "state": self.state}

    def _ctrl_out(self, req, value, data=b""):
        if req in (self.DFU_CLRSTATUS, self.DFU_ABORT):
            self.state = DFU_IDLE
        elif req == self.DFU_DNLOAD and value == 0:
            if data[0] == self.CMD_SET_ADDRESS_POINTER:
                self.pointer = int.from_bytes(data[1:5], "little")
            self.state = self.STATE_DFU_ERROR if self.fail_block0 else self.STATE_DFU_DNLOAD_IDLE
        elif req == self.DFU_DNLOAD:
            offset = self.pointer + (value - 2) * self.transfer_size - SLOT
            self.data_blocks.append((offset, len(data)))
            refused = offset in self.refuse_offsets
            self.state = self.STATE_DFU_ERROR if refused else self.STATE_DFU_DNLOAD_IDLE
        return len(data)


def test_usb_refused_header_raises_and_is_not_retried():
    dfu = FakeUsbDfu(refuse_offsets={0})
    with pytest.raises(DFUWriteRefusedError, match=r"offset 0x0: .*refused the image header"):
        dfu.write_memory(SLOT, b"\xAA" * 4096)
    assert dfu.data_blocks == [(0, 1024)]         # sent once; nothing after it


def test_usb_refused_later_block_reports_its_offset():
    dfu = FakeUsbDfu(refuse_offsets={0x800})
    with pytest.raises(DFUWriteRefusedError) as exc:
        dfu.write_memory(SLOT, b"\xAA" * 4096)
    assert "offset 0x800" in str(exc.value)
    assert "header" not in str(exc.value)
    assert [o for o, _ in dfu.data_blocks] == [0, 0x400, 0x800]


def test_usb_accepted_image_writes_every_block():
    dfu = FakeUsbDfu()
    dfu.write_memory(SLOT, b"\xAA" * 4096)
    assert [o for o, _ in dfu.data_blocks] == [0, 0x400, 0x800, 0xC00]


def test_usb_block0_errors_keep_the_lenient_handling():
    # Erase / set-address failures were tolerated before and still are
    # (the ROM-DFU migration paths depend on it); only data blocks raise.
    dfu = FakeUsbDfu(fail_block0=True)
    dfu.write_memory(SLOT, b"\xAA" * 2048)
    assert len(dfu.data_blocks) == 2
