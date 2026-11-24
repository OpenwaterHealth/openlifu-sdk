# SPDX-License-Identifier: Apache-2.0
"""OW_CMD_SIGCHK: is the running firmware signed by a given key (SR-021)?

Both firmwares answer with a status byte, then (slot builds) the signed
header from the slot start; the SDK runs the ECDSA check. These tests fake
the device reply with headers signed by real P-256 keys.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from openlifu_sdk.io import LIFUCrypto  # noqa: E402
from openlifu_sdk.io.component import SignatureCheck  # noqa: E402
from openlifu_sdk.io.LIFUConfig import (  # noqa: E402
    OW_CMD_SIGCHK, OW_ERROR, OW_RESP, SIGCHK_BARE_METAL, SIGCHK_SLOT_IMAGE,
)
from openlifu_sdk.io.LIFUHVController import HVController  # noqa: E402
from openlifu_sdk.io.LIFUTXDevice import TxDevice  # noqa: E402
from openlifu_sdk.io.uart import OWUart  # noqa: E402


def _keys(tmp_path, name):
    priv = ec.generate_private_key(ec.SECP256R1())
    keys = LIFUCrypto.SigningKeys(keys_dir=tmp_path / name, private_key=priv,
                                  public_key=priv.public_key(), aes_key=bytes(16))
    return keys


def _pem(public_key) -> bytes:
    return public_key.public_bytes(serialization.Encoding.PEM,
                                   serialization.PublicFormat.SubjectPublicKeyInfo)


@pytest.fixture
def signed(tmp_path):
    """(header signed by key A, key A, key B)."""
    a, b = _keys(tmp_path, "a"), _keys(tmp_path, "b")
    image = LIFUCrypto.sign_firmware_bytes(b"\x01\x02\x03\x04" * 64, a, "1.3.0")
    return image[:LIFUCrypto.HEADER_TOTAL_LEN], a.public_key, b.public_key


def _device(make, payload: bytes | None = None, packet_type=OW_RESP):
    device = make()
    uart = MagicMock(spec=OWUart)
    uart.desc = "DEV"
    uart.demo_mode = False
    uart.asyncMode = False
    uart.is_connected = True
    reply = MagicMock()
    reply.packet_type = packet_type
    reply.data = bytearray(payload or b"")
    reply.data_len = len(payload or b"")
    reply.reserved = 0
    uart.send_packet.return_value = reply
    device._uart = uart
    return device


@pytest.mark.parametrize("make", [HVController, TxDevice])
def test_signed_by_the_given_key(make, signed):
    header, key_a, _key_b = signed
    dev = _device(make, bytes([SIGCHK_SLOT_IMAGE]) + header)
    assert dev.check_signature(key_a) is SignatureCheck.SIGNED


@pytest.mark.parametrize("make", [HVController, TxDevice])
def test_signed_by_another_key(make, signed):
    header, _key_a, key_b = signed
    dev = _device(make, bytes([SIGCHK_SLOT_IMAGE]) + header)
    assert dev.check_signature(key_b) is SignatureCheck.NOT_SIGNED


def test_a_modified_header_is_not_signed(signed):
    header, key_a, _ = signed
    tampered = bytearray(header)
    tampered[6] ^= 0x01                       # FwVersion, inside the signed part
    dev = _device(HVController, bytes([SIGCHK_SLOT_IMAGE]) + bytes(tampered))
    assert dev.check_signature(key_a) is SignatureCheck.NOT_SIGNED


def test_a_slot_without_a_header_is_not_signed(signed):
    _, key_a, _ = signed
    dev = _device(HVController, bytes([SIGCHK_SLOT_IMAGE]) + b"\xFF" * LIFUCrypto.HEADER_TOTAL_LEN)
    assert dev.check_signature(key_a) is SignatureCheck.NOT_SIGNED


@pytest.mark.parametrize("make", [HVController, TxDevice])
def test_bare_metal(make, signed):
    _, key_a, _ = signed
    dev = _device(make, bytes([SIGCHK_BARE_METAL]))
    assert dev.check_signature(key_a) is SignatureCheck.BARE_METAL


@pytest.mark.parametrize("payload", [
    b"",                                                   # nothing
    bytes([SIGCHK_SLOT_IMAGE]) + b"\x00" * 100,            # short header
    bytes([0x7F]),                                         # unknown status
])
def test_malformed_reply_is_an_error(signed, payload):
    _, key_a, _ = signed
    assert _device(HVController, payload).check_signature(key_a) is SignatureCheck.ERROR


def test_device_refusal_is_an_error(signed):
    # Firmware without the command answers OW_ERROR.
    _, key_a, _ = signed
    dev = _device(TxDevice, b"", packet_type=OW_ERROR)
    assert dev.check_signature(key_a) is SignatureCheck.ERROR


def test_command_and_module_address(signed):
    header, key_a, _ = signed
    tx = _device(TxDevice, bytes([SIGCHK_SLOT_IMAGE]) + header)
    tx.check_signature(key_a, module=2)
    kwargs = tx._uart.send_packet.call_args.kwargs
    assert kwargs["command"] == OW_CMD_SIGCHK and kwargs["addr"] == 2


@pytest.mark.parametrize("form", ["pem", "file", "dir"])
def test_public_key_forms(tmp_path, signed, form):
    header, key_a, _ = signed
    pem = _pem(key_a)
    if form == "pem":
        key = pem
    else:
        d = tmp_path / "keys"
        d.mkdir()
        (d / LIFUCrypto.PUBLIC_KEY_FILE).write_bytes(pem)
        key = d if form == "dir" else d / LIFUCrypto.PUBLIC_KEY_FILE
    dev = _device(HVController, bytes([SIGCHK_SLOT_IMAGE]) + header)
    assert dev.check_signature(key) is SignatureCheck.SIGNED


def test_unusable_public_key_raises(signed):
    dev = _device(HVController, bytes([SIGCHK_BARE_METAL]))
    with pytest.raises(LIFUCrypto.LIFUCryptoError):
        dev.check_signature(b"not a key")
    dev._uart.send_packet.assert_not_called()          # checked before asking
