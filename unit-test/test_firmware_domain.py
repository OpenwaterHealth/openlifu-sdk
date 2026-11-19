# SPDX-License-Identifier: Apache-2.0
"""firmware_domain(): which trusted key signed the running firmware (SR-021).

The SDK ships each board's research and clinical public keys
(openlifu_sdk/keys, PROVENANCE.txt). The fingerprints are pinned here: a
replaced key file fails until this test is updated alongside it. The domain
logic is tested with generated keys standing in for the shipped pair.
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from openlifu_sdk.io import LIFUCrypto  # noqa: E402
from openlifu_sdk.io.component import FirmwareDomain  # noqa: E402
from openlifu_sdk.io.LIFUConfig import (  # noqa: E402
    OW_ERROR, OW_RESP, SIGCHK_BARE_METAL, SIGCHK_SLOT_IMAGE,
)
from openlifu_sdk.io.LIFUHVController import HVController, LIFUHVController  # noqa: E402
from openlifu_sdk.io.LIFUTXDevice import LIFUTxDevice, TxDevice  # noqa: E402
from openlifu_sdk.io.uart import OWUart  # noqa: E402

# SHA-256 of the DER SubjectPublicKeyInfo; must match keys/PROVENANCE.txt and
# OpenWater-KMS docs/KEY-CEREMONY-RECORD (Open-LIFU).
PINNED = {
    ("console", "research"):
        "4606fc385177cc91f9f7f2d123b6a0f33839084f43a6d4df4de5f58beb4e9775",
    ("console", "clinical"):
        "5cffd2326732f1b2f89f9eed3bdfdf2384bf1e5649a11ef95ff0b6769145dade",
    ("transmitter", "research"):
        "f0a67190fc72379a29f4a3e88d173689a761d4edd9565880f250dfcb6ce3ce76",
    ("transmitter", "clinical"):
        "d0538bd89e77eb16f8a52cef0188f2ce6fac09d5b5a40f3b2416b572c2b94978",
}


def _fingerprint(key) -> str:
    der = key.public_bytes(serialization.Encoding.DER,
                           serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


@pytest.mark.parametrize("board, domain", sorted(PINNED))
def test_shipped_keys_are_the_pinned_keys(board, domain):
    key = LIFUCrypto.trusted_public_key(board, domain)
    assert _fingerprint(key) == PINNED[(board, domain)]


def test_shipped_keys_are_all_distinct():
    assert len(set(PINNED.values())) == len(PINNED)


@pytest.mark.parametrize("board, domain", [("console", "dev"), ("sensor", "research")])
def test_unknown_board_or_domain_is_refused(board, domain):
    with pytest.raises(ValueError):
        LIFUCrypto.trusted_public_key(board, domain)


def test_device_classes_name_their_board():
    assert HVController.FIRMWARE_BOARD == LIFUHVController.FIRMWARE_BOARD == "console"
    assert TxDevice.FIRMWARE_BOARD == LIFUTxDevice.FIRMWARE_BOARD == "transmitter"


# --------------------------------------------------------------------------
# Domain logic, with generated keys in place of the shipped ones
# --------------------------------------------------------------------------

def _signer(tmp_path, name):
    priv = ec.generate_private_key(ec.SECP256R1())
    return LIFUCrypto.SigningKeys(keys_dir=tmp_path / name, private_key=priv,
                                  public_key=priv.public_key(), aes_key=bytes(16))


@pytest.fixture
def keyring(tmp_path, monkeypatch):
    """Generated research/clinical keys per board, served as the trusted keys.
    Returns a function signing a header with (board, domain)'s key."""
    signers = {(b, d): _signer(tmp_path, f"{b}-{d}")
               for b in LIFUCrypto.FIRMWARE_BOARDS for d in LIFUCrypto.FIRMWARE_DOMAINS}
    monkeypatch.setattr(LIFUCrypto, "trusted_public_key",
                        lambda board, domain: signers[(board, domain)].public_key)

    def header(board, domain, keys=None):
        image = LIFUCrypto.sign_firmware_bytes(b"\x5A" * 512, keys or signers[(board, domain)], "1.3.0")
        return image[:LIFUCrypto.HEADER_TOTAL_LEN]
    header.signer = _signer
    return header


def _device(make, payload: bytes, packet_type=OW_RESP):
    device = make()
    uart = MagicMock(spec=OWUart)
    uart.desc = "DEV"
    uart.demo_mode = False
    uart.asyncMode = False
    uart.is_connected = True
    reply = MagicMock()
    reply.packet_type = packet_type
    reply.data = bytearray(payload)
    reply.data_len = len(payload)
    reply.reserved = 0
    uart.send_packet.return_value = reply
    device._uart = uart
    return device


BOARDS = [(HVController, "console"), (TxDevice, "transmitter")]


@pytest.mark.parametrize("make, board", BOARDS)
@pytest.mark.parametrize("domain", ["research", "clinical"])
def test_signed_by_the_boards_key(keyring, make, board, domain):
    dev = _device(make, bytes([SIGCHK_SLOT_IMAGE]) + keyring(board, domain))
    assert dev.firmware_domain() is FirmwareDomain(domain)


@pytest.mark.parametrize("make, board", BOARDS)
def test_the_other_boards_key_is_unknown(keyring, make, board):
    other = "transmitter" if board == "console" else "console"
    dev = _device(make, bytes([SIGCHK_SLOT_IMAGE]) + keyring(other, "clinical"))
    assert dev.firmware_domain() is FirmwareDomain.UNKNOWN


def test_a_development_key_is_unknown(keyring, tmp_path):
    dev_key = keyring.signer(tmp_path, "dev")
    dev = _device(HVController, bytes([SIGCHK_SLOT_IMAGE]) + keyring("console", "research", dev_key))
    assert dev.firmware_domain() is FirmwareDomain.UNKNOWN


def test_no_header_in_the_slot_is_unknown(keyring):
    dev = _device(HVController, bytes([SIGCHK_SLOT_IMAGE]) + b"\xFF" * LIFUCrypto.HEADER_TOTAL_LEN)
    assert dev.firmware_domain() is FirmwareDomain.UNKNOWN


@pytest.mark.parametrize("make, board", BOARDS)
def test_bare_metal(keyring, make, board):
    assert _device(make, bytes([SIGCHK_BARE_METAL])).firmware_domain() is FirmwareDomain.BARE_METAL


def test_refused_or_malformed_is_an_error(keyring):
    assert _device(TxDevice, b"", OW_ERROR).firmware_domain() is FirmwareDomain.ERROR
    assert _device(HVController, b"").firmware_domain() is FirmwareDomain.ERROR


def test_one_request_per_call(keyring):
    dev = _device(TxDevice, bytes([SIGCHK_SLOT_IMAGE]) + keyring("transmitter", "research"))
    dev.firmware_domain(module=1)
    assert dev._uart.send_packet.call_count == 1
    assert dev._uart.send_packet.call_args.kwargs["addr"] == 1
