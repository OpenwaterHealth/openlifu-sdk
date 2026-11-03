# SPDX-License-Identifier: Apache-2.0
"""The loaded-preset query (OW_PRESET_LOADED; transmitter-fw#44), which a
host confirms just before Start (SR-003).

Real components, mocked UART: the reply decodes as the firmware writes it
(if_commands.c ``preset_loaded_reply``). The simulator's side is in
test_simulated_loaded_preset.py.
"""

from __future__ import annotations

import struct
from unittest.mock import MagicMock

import pytest

from openlifu_sdk.io.exceptions import LIFUProtocolError
from openlifu_sdk.io.LIFUConfig import (
    OW_CONTROLLER,
    OW_PRESET_LOADED,
    OW_RESP,
)
from openlifu_sdk.io.LIFUTXDevice import TxDevice
from openlifu_sdk.io.uart import OWUart


def _packet(data: bytes = b"", packet_type: int = OW_RESP, reserved: int = 0):
    pkt = MagicMock()
    pkt.packet_type = packet_type
    pkt.data = bytearray(data)
    pkt.data_len = len(data)
    pkt.reserved = reserved
    return pkt


def _with_uart(device):
    uart = MagicMock(spec=OWUart)
    uart.desc = "TX"
    uart.is_connected = True
    device._uart = uart
    return device, uart


def _loaded_reply(loaded: bool, index=0, duration=0, trains=0, settings=0, regs=0, missing=0) -> bytes:
    return bytes([1 if loaded else 0, index, duration]) + struct.pack("<III", trains, settings, regs) + bytes([missing])


# ---- real components, mocked UART -------------------------------------------------

def test_loaded_state_decodes_a_load_on_every_module():
    tx, uart = _with_uart(TxDevice())
    uart.send_packet.return_value = _packet(_loaded_reply(True, 2, 1, 30, 0x11223344, 0xAABBCCDD))

    state = tx.get_loaded_preset_state()

    assert state == {"loaded": True, "index": 2, "duration_index": 1, "train_count": 30,
                     "settings_crc": 0x11223344, "regs_crc": 0xAABBCCDD, "missing_modules": []}
    sent = uart.send_packet.call_args.kwargs
    assert (sent["packet_type"], sent["command"], sent["addr"]) == (OW_CONTROLLER, OW_PRESET_LOADED, 0)
    assert tx.get_loaded_preset() == (2, 1)


def test_loaded_state_names_the_modules_without_the_load():
    tx, uart = _with_uart(TxDevice())
    uart.send_packet.return_value = _packet(_loaded_reply(False, missing=0b10))

    state = tx.get_loaded_preset_state()

    assert state["loaded"] is False
    assert state["missing_modules"] == [1]
    assert state["index"] is None and state["train_count"] is None
    assert tx.get_loaded_preset() == (None, None)


def test_loaded_state_for_one_module_addresses_it():
    tx, uart = _with_uart(TxDevice())
    uart.send_packet.return_value = _packet(_loaded_reply(True, 0, 0, 5, 1, 2))
    assert tx.get_loaded_preset_state(module=1)["missing_modules"] == []
    assert uart.send_packet.call_args.kwargs["addr"] == 1


def test_one_module_without_a_load_names_itself():
    # The module answers alone, so its own bit is bit 0 (if_commands.c).
    tx, uart = _with_uart(TxDevice())
    uart.send_packet.return_value = _packet(_loaded_reply(False, missing=0b1))
    assert tx.get_loaded_preset_state(module=1)["missing_modules"] == [1]


def test_a_loaded_reply_naming_missing_modules_is_a_protocol_error():
    # SR-003 trusts the load before Start; an inconsistent reply is not one.
    tx, uart = _with_uart(TxDevice())
    uart.send_packet.return_value = _packet(_loaded_reply(True, 0, 0, 5, 1, 2, missing=0b10))
    with pytest.raises(LIFUProtocolError, match="inconsistent"):
        tx.get_loaded_preset_state()


@pytest.mark.parametrize(("reply", "module"), [
    (_loaded_reply(False), None),                      # not loaded, yet no module missing
    (bytes([2]) + bytes(15), None),                    # loaded byte neither 0 nor 1
    (_loaded_reply(False, missing=0b10), 1),           # a lone module naming another
])
def test_a_reply_the_firmware_does_not_send_is_a_protocol_error(reply, module):
    tx, uart = _with_uart(TxDevice())
    uart.send_packet.return_value = _packet(reply)
    with pytest.raises(LIFUProtocolError, match="inconsistent"):
        tx.get_loaded_preset_state(module=module)


def test_a_short_loaded_reply_is_a_protocol_error():
    tx, uart = _with_uart(TxDevice())
    uart.send_packet.return_value = _packet(b"\x01\x00")
    with pytest.raises(LIFUProtocolError):
        tx.get_loaded_preset_state()

