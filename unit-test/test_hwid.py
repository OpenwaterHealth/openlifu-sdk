# SPDX-License-Identifier: Apache-2.0
"""Hardware-ID string forms (SR-004.3).

A calibration record's ``hwid`` must match the module it was read from.
New records carry base58 of the full 12-byte UID; older ones carry base58
of its first 6 bytes. The expected strings below were computed with an
independent base58 implementation, not with the ``base58`` package.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from openlifu_sdk.io.LIFUConfig import OW_RESP
from openlifu_sdk.io.LIFUHVController import HVController
from openlifu_sdk.io.LIFUTXDevice import TxDevice
from openlifu_sdk.io.uart import OWUart
from openlifu_sdk.util.hwid import encode_hwid

# A leading zero byte checks base58's "1" prefix.
UID = bytes(range(12))


@pytest.mark.parametrize("kwargs, expected", [
    ({}, "0001-0203-0405-0607-0809-0A0B"),
    ({"raw_hex": True}, "000102030405060708090a0b"),
    ({"base58": True}, "1FVk6iLh9oT6QH8"),
    ({"base58": True, "legacy_6byte": True}, "17bWpTW"),
])
def test_encode_hwid_forms(kwargs, expected):
    assert encode_hwid(UID, **kwargs) == expected


def test_legacy_form_keeps_only_the_first_six_bytes():
    # Two UIDs that differ only past byte 6 share the legacy form.
    other = UID[:6] + bytes(6)
    assert encode_hwid(other, base58=True) != encode_hwid(UID, base58=True)
    assert (encode_hwid(other, base58=True, legacy_6byte=True)
            == encode_hwid(UID, base58=True, legacy_6byte=True))


@pytest.mark.parametrize("kwargs", [
    {"raw_hex": True, "base58": True},
    {"legacy_6byte": True},
    {"raw_hex": True, "legacy_6byte": True},
])
def test_encode_hwid_refuses_mixed_forms(kwargs):
    with pytest.raises(ValueError):
        encode_hwid(UID, **kwargs)


def _hwid_reply(device, uid: bytes):
    uart = MagicMock(spec=OWUart)
    uart.desc = "DEV"
    uart.demo_mode = False
    uart.asyncMode = False
    uart.is_connected = True
    reply = MagicMock()
    reply.packet_type = OW_RESP
    reply.data = bytearray(uid)
    reply.data_len = len(uid)
    reply.reserved = 0
    uart.send_packet.return_value = reply
    device._uart = uart
    return device


@pytest.mark.parametrize("make", [TxDevice, HVController])
@pytest.mark.parametrize("kwargs, expected", [
    ({}, "0001-0203-0405-0607-0809-0A0B"),
    ({"base58": True}, "1FVk6iLh9oT6QH8"),
    ({"base58": True, "legacy_6byte": True}, "17bWpTW"),
])
def test_get_hardware_id_forms(make, kwargs, expected):
    device = _hwid_reply(make(), UID)
    assert device.get_hardware_id(**kwargs) == expected


def test_get_hardware_id_reads_the_addressed_module():
    tx = _hwid_reply(TxDevice(), UID)
    tx.get_hardware_id(module=2, base58=True)
    assert tx._uart.send_packet.call_args.kwargs["addr"] == 2


def test_simulated_modules_are_calibrated_for_their_own_uid():
    pytest.importorskip("openlifu_sdk.ui._qt")
    from openlifu_sdk.ui.simulated_interface import SimulatedTxDevice

    tx = SimulatedTxDevice(num_modules=2)
    tx.refresh_metadata()
    assert tx.get_hardware_id(0, base58=True) == "42p8mJivqN6tpZqe2"
    assert tx.get_hardware_id(1, base58=True) == "42p8mJivqN6tpZqe3"
    # Both modules share the legacy form; only the full form tells them apart.
    assert tx.get_hardware_id(1, base58=True, legacy_6byte=True) == "2NzQV6J84"
    assert tx.get_hardware_id(0) == "A0A1-A2A3-A4A5-A6A7-B0B1-B2B3"
    assert [c["hwid"] for c in tx.module_user_configs] == [
        "42p8mJivqN6tpZqe2", "42p8mJivqN6tpZqe3"]


def test_simulated_console_offers_the_same_forms():
    pytest.importorskip("openlifu_sdk.ui._qt")
    from openlifu_sdk.ui.simulated_interface import SimulatedHVController

    hv = SimulatedHVController()
    assert hv.get_hardware_id() == "C0C1-C2C3-C4C5-C6C7-D0D1-D2D3"
    assert hv.get_hardware_id(base58=True) == "4dyWcpSyLJHurUomU"
    assert hv.get_hardware_id(base58=True, legacy_6byte=True) == "2ezGGCzTN"
