# SPDX-License-Identifier: Apache-2.0
"""The preset lock and the loaded-preset query (transmitter-fw#41, #44).

- Real components, mocked UART: the OW_PRESET_LOADED / OW_PRESET_LOCK
  replies decode as the firmware writes them (if_commands.c), and the FDA
  TxDevice may read the lock but not set it.
The simulator's side is in test_simulated_preset_lock.py.
"""

from __future__ import annotations

import struct
from unittest.mock import MagicMock

import pytest

from openlifu_sdk.io.exceptions import LIFUProtocolError
from openlifu_sdk.io.LIFUConfig import (
    OW_CMD_USR_CFG,
    OW_CONTROLLER,
    OW_PRESET_LOADED,
    OW_PRESET_LOCK,
    OW_RESP,
)
from openlifu_sdk.io.LIFUInterface import LIFUInterface
from openlifu_sdk.io.LIFUTXDevice import LIFUTxDevice, TxDevice
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
    tx.get_loaded_preset_state(module=1)
    assert uart.send_packet.call_args.kwargs["addr"] == 1


def test_a_short_loaded_reply_is_a_protocol_error():
    tx, uart = _with_uart(TxDevice())
    uart.send_packet.return_value = _packet(b"\x01\x00")
    with pytest.raises(LIFUProtocolError):
        tx.get_loaded_preset_state()


def test_the_fda_txdevice_reads_the_lock():
    tx, uart = _with_uart(TxDevice())
    uart.send_packet.return_value = _packet(b"\x01")
    assert tx.is_preset_locked() is True
    sent = uart.send_packet.call_args.kwargs
    assert (sent["command"], sent["reserved"]) == (OW_PRESET_LOCK, 0)


@pytest.mark.parametrize("cls", [TxDevice, LIFUTxDevice], ids=["fda", "ruo"])
def test_lock_presets_sends_the_lock_and_checks_the_reply(cls):
    """The FDA component may turn the lock on too: it only restricts."""
    tx, uart = _with_uart(cls())
    uart.send_packet.return_value = _packet(b"\x01")
    assert tx.lock_presets() is True
    sent = uart.send_packet.call_args.kwargs
    assert (sent["command"], sent["reserved"]) == (OW_PRESET_LOCK, 1)

    uart.send_packet.return_value = _packet(b"\x00")
    with pytest.raises(LIFUProtocolError):
        tx.lock_presets()


def test_the_fda_txdevice_still_cannot_write_user_config():
    tx, uart = _with_uart(TxDevice())
    with pytest.raises(ValueError):
        tx.send(OW_CMD_USR_CFG, reserved=1)
    uart.send_packet.assert_not_called()


def test_preset_lock_needs_a_synchronous_interface():
    with pytest.raises(ValueError):
        LIFUInterface(preset_lock=True, run_async=True)


def test_preset_lock_locks_the_transmitter_once_it_connects(monkeypatch):
    # Build only the attributes _connect_devices uses; no USB involved.
    iface = LIFUInterface.__new__(LIFUInterface)
    iface._async_mode = False
    iface._preset_lock = True
    iface.txdevice = MagicMock()
    iface.txdevice.is_connected.return_value = True
    iface.hvcontroller = None
    iface._connect_devices()
    iface.txdevice.lock_presets.assert_called_once_with()

    iface.txdevice.reset_mock()
    iface.txdevice.is_connected.return_value = False
    iface._connect_devices()
    iface.txdevice.lock_presets.assert_not_called()
