# SPDX-License-Identifier: Apache-2.0
"""The console's preset page from the host (console firmware with presets in
page 61): HVController.get_preset_info and LIFUHVController.write_presets.

The device is faked at the UART: each command gets a scripted reply, and the
sent packets are checked against the firmware's protocol (Core/Src/hv_presets.c).
"""

from __future__ import annotations

import struct
import zlib
from unittest.mock import MagicMock

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from openlifu_sdk.io import LIFUCrypto  # noqa: E402
from openlifu_sdk.io import LIFUPresetPackage as pp  # noqa: E402
from openlifu_sdk.io.exceptions import LIFUDeviceError, LIFUProtocolError  # noqa: E402
from openlifu_sdk.io.LIFUConfig import (  # noqa: E402
    OW_ERROR, OW_POWER, OW_POWER_PRESET_COMMIT, OW_POWER_PRESET_INFO,
    OW_POWER_PRESET_WRITE, OW_RESP, PRESET_INFO_EMPTY, PRESET_INFO_NO_PAGE, PRESET_INFO_OK,
)
from openlifu_sdk.io.LIFUHVController import HVController, LIFUHVController  # noqa: E402
from openlifu_sdk.io.uart import OWUart  # noqa: E402

SHA = "cd" * 20
CONFIGS = [
    {"id": f"p{i}", "settings_crc": 0x1000 + i, "voltage": 40, "sensitivity": 3728,
     "voltage_range": [35, 45]} for i in range(4)
]


def _keys(tmp_path, name="k"):
    priv = ec.generate_private_key(ec.SECP256R1())
    return LIFUCrypto.SigningKeys(keys_dir=tmp_path / name, private_key=priv,
                                  public_key=priv.public_key())


@pytest.fixture
def signed(tmp_path):
    keys = _keys(tmp_path)
    data = pp.build_preset_package(pp.console_preset_body(CONFIGS), diathermy_sha=SHA,
                                   context="test", keys=keys)
    return data, keys.public_key


def _reply(packet_type=OW_RESP, reserved=0, data=b""):
    r = MagicMock()
    r.packet_type, r.reserved = packet_type, reserved
    r.data, r.data_len = bytearray(data), len(data)
    return r


def _device(make, replies):
    dev = make()
    uart = MagicMock(spec=OWUart)
    uart.desc = "HV"
    uart.demo_mode = False
    uart.asyncMode = False
    uart.is_connected = True
    uart.send_packet.side_effect = list(replies)
    dev._uart = uart
    return dev


def _sent(dev):
    return [c.kwargs for c in dev._uart.send_packet.call_args_list]


# --------------------------------------------------------------------------
# get_preset_info
# --------------------------------------------------------------------------

@pytest.mark.parametrize("make", [HVController, LIFUHVController])
def test_info_reports_the_stored_set(make, signed, tmp_path):
    data, key = signed
    dev = _device(make, [_reply(reserved=PRESET_INFO_OK, data=data[:320])])
    info = dev.get_preset_info()
    assert (info.diathermy_sha, info.context) == (SHA, "test")
    assert info.body_size == len(data) - 320
    assert info.signed_by(key)
    assert not info.signed_by(_keys(tmp_path, "other").public_key)
    assert _sent(dev)[0]["packet_type"] == OW_POWER
    assert _sent(dev)[0]["command"] == OW_POWER_PRESET_INFO


def test_info_empty_page_is_none():
    dev = _device(HVController, [_reply(reserved=PRESET_INFO_EMPTY)])
    assert dev.get_preset_info() is None


def test_info_without_a_preset_page_raises():
    dev = _device(HVController, [_reply(reserved=PRESET_INFO_NO_PAGE)])
    with pytest.raises(LIFUDeviceError, match="no preset page"):
        dev.get_preset_info()


def test_info_that_is_not_a_header_is_a_protocol_error():
    dev = _device(HVController, [_reply(reserved=PRESET_INFO_OK, data=b"\x00" * 320)])
    with pytest.raises(LIFUProtocolError):
        dev.get_preset_info()


# --------------------------------------------------------------------------
# write_presets (research and bare-metal firmware)
# --------------------------------------------------------------------------

def test_write_sends_the_body_first_and_the_header_last(signed):
    data, _key = signed
    n_chunks = len(range(320, len(data), 256)) + len(range(0, 320, 256))
    dev = _device(LIFUHVController,
                  [_reply()] * n_chunks + [_reply(), _reply(reserved=PRESET_INFO_OK, data=data[:320])])
    info = dev.write_presets(data)
    sent = _sent(dev)
    writes = [s for s in sent if s["command"] == OW_POWER_PRESET_WRITE]
    offsets = [struct.unpack_from("<H", bytes(w["data"]))[0] for w in writes]
    assert offsets == [*range(320, len(data), 256), *range(0, 320, 256)]
    assert all(o % 4 == 0 for o in offsets)
    rebuilt = bytearray(len(data))
    for w in writes:
        off = struct.unpack_from("<H", bytes(w["data"]))[0]
        chunk = bytes(w["data"])[2:]
        rebuilt[off:off + len(chunk)] = chunk
    assert bytes(rebuilt) == data                    # every byte, exactly once
    commit = sent[len(writes)]
    assert commit["command"] == OW_POWER_PRESET_COMMIT
    assert bytes(commit["data"]) == struct.pack("<II", len(data), zlib.crc32(data))
    assert sent[-1]["command"] == OW_POWER_PRESET_INFO
    assert info.diathermy_sha == SHA


def test_write_rejects_a_bad_package_before_sending(signed):
    data, _key = signed
    bad = bytearray(data)
    bad[-1] ^= 0x01
    dev = _device(LIFUHVController, [])
    with pytest.raises(ValueError, match="signed hash"):
        dev.write_presets(bytes(bad))
    with pytest.raises(ValueError, match="Not a preset package"):
        dev.write_presets(b"SFU1" + bytes(400))
    dev._uart.send_packet.assert_not_called()


def test_write_refused_by_the_console_raises(signed):
    data, _key = signed
    dev = _device(LIFUHVController, [_reply(OW_ERROR, 0xF8)])   # an FDA build
    with pytest.raises(LIFUDeviceError):
        dev.write_presets(data)


def test_the_fda_component_reads_but_cannot_write():
    assert not hasattr(HVController, "write_presets")
    dev = _device(HVController, [])
    with pytest.raises(ValueError):
        dev.send_checked(packet_type=OW_POWER, command=OW_POWER_PRESET_WRITE,
                         data=bytearray(b"\x00\x00\xFF\xFF"))
    with pytest.raises(ValueError):
        dev.send_checked(packet_type=OW_POWER, command=OW_POWER_PRESET_COMMIT,
                         data=bytearray(8))
    dev._uart.send_packet.assert_not_called()
