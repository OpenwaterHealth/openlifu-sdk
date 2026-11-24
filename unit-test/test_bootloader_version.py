# SPDX-License-Identifier: Apache-2.0
"""get_bootloader_version(): the bootloader version, read from the running
application (OW_CMD_BL_VERSION, SR-021).

Firmware without the command answers OW_ERROR; that, and an empty reply
(bare metal / no version found), is None. A transport failure still raises.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from openlifu_sdk.io.exceptions import LIFUCommunicationError
from openlifu_sdk.io.LIFUConfig import (
    GLOBAL_COMMANDS,
    GLOBAL_FDA_COMMANDS,
    OW_CMD,
    OW_CMD_BL_VERSION,
    OW_ERROR,
    OW_RESP,
)
from openlifu_sdk.io.LIFUHVController import HVController, LIFUHVController
from openlifu_sdk.io.LIFUTXDevice import LIFUTxDevice, TxDevice
from openlifu_sdk.io.uart import OWUart

BOARDS = [LIFUHVController, LIFUTxDevice]


def _device(make, payload: bytes | None, packet_type=OW_RESP):
    """*make*'s component on a fake UART answering *payload* (None: a
    timeout)."""
    device = make()
    uart = MagicMock(spec=OWUart)
    uart.desc = "DEV"
    uart.is_connected = True
    if payload is None:
        uart.send_packet.return_value = None
    else:
        reply = MagicMock()
        reply.packet_type = packet_type
        reply.data = bytearray(payload)
        reply.data_len = len(payload)
        reply.reserved = 0
        uart.send_packet.return_value = reply
    device._uart = uart
    return device


def test_the_command_is_a_global_research_command_only():
    assert OW_CMD_BL_VERSION == 0x70
    assert OW_CMD_BL_VERSION in GLOBAL_COMMANDS
    assert OW_CMD_BL_VERSION not in GLOBAL_FDA_COMMANDS


@pytest.mark.parametrize("make", BOARDS)
@pytest.mark.parametrize("text", ["1.1.0", "1.1.0-rc.3", "1.0.1", "1.1.0-rc.3-clinical"])
def test_the_version_string(make, text):
    assert _device(make, text.encode()).get_bootloader_version() == text


@pytest.mark.parametrize("make", BOARDS)
def test_padding_is_dropped(make):
    assert _device(make, b"1.1.0\x00\x00").get_bootloader_version() == "1.1.0"


@pytest.mark.parametrize("make", BOARDS)
def test_no_version_is_none(make):
    assert _device(make, b"").get_bootloader_version() is None


@pytest.mark.parametrize("make", BOARDS)
def test_firmware_without_the_command_is_none(make):
    """Every release before the command (e.g. console 1.3.0-rc.4,
    transmitter 2.1.0-rc.7, field 1.2.6 / 2.0.8) answers OW_ERROR."""
    assert _device(make, b"", OW_ERROR).get_bootloader_version() is None


@pytest.mark.parametrize("make", BOARDS)
def test_a_timeout_still_raises(make):
    with pytest.raises(LIFUCommunicationError):
        _device(make, None).get_bootloader_version()


def test_a_reply_longer_than_the_spec_is_cut():
    assert _device(LIFUTxDevice, b"1" * 40).get_bootloader_version() == "1" * 32


def test_the_module_is_the_address():
    dev = _device(LIFUTxDevice, b"1.1.0-rc.2")
    dev.get_bootloader_version(module=2)
    kw = dev._uart.send_packet.call_args.kwargs
    assert (kw["packet_type"], kw["command"], kw["addr"]) == (OW_CMD, OW_CMD_BL_VERSION, 2)


@pytest.mark.parametrize("make", [HVController, TxDevice])
def test_the_fda_components_do_not_send_it(make):
    dev = _device(make, b"1.1.0")
    with pytest.raises(ValueError, match="not supported"):
        dev.get_bootloader_version()
    dev._uart.send_packet.assert_not_called()
