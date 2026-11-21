# SPDX-License-Identifier: Apache-2.0
"""Signed preset packages (PRS1) for the console preset page.

The bootloader (open-lifu-console-bl >= 1.1.0-rc.2) verifies the PRS1 header
with the same routine and key it uses for firmware headers, and the body's
SHA-256 against FwTag. These tests check the package the SDK builds against
those rules, with generated keys.
"""

from __future__ import annotations

import hashlib
import struct

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from openlifu_sdk.io import LIFUCrypto  # noqa: E402
from openlifu_sdk.io import LIFUPresetPackage as pp  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"
CONFIGS = [
    {"id": "soft", "settings_crc": 0x1234, "voltage": 40, "sensitivity": 3728,
     "voltage_range": [35, 45]},
    {"id": "lowv", "settings_crc": 0x99, "voltage": 20, "sensitivity": 3728,
     "voltage_range": [15, 25]},
]


def _keys(tmp_path, name="k"):
    priv = ec.generate_private_key(ec.SECP256R1())
    return LIFUCrypto.SigningKeys(keys_dir=tmp_path / name, private_key=priv,
                                  public_key=priv.public_key())


@pytest.fixture
def body():
    return pp.console_preset_body(CONFIGS)


def test_body_is_the_records_back_to_back(body):
    from openlifu_sdk.io.LIFUTXPresets import console_preset_record

    assert body == b"".join(console_preset_record(c, version=2) for c in CONFIGS)


def test_package_layout_matches_the_bootloader(tmp_path, body):
    keys = _keys(tmp_path)
    data = pp.build_preset_package(body, diathermy_sha=SHA, context="research", keys=keys)
    assert len(data) == pp.PRESET_BODY_OFFSET + len(body) == 0x140 + len(body)
    magic, proto, ver, size, poff, psize, tag, ptag, iv, reserved = struct.unpack_from(
        LIFUCrypto._AUTH_STRUCT, data, 0)
    assert (magic, proto, ver, size, poff, psize) == (b"PRS1", 1, 0, len(body), 0, len(body))
    assert tag == ptag == hashlib.sha256(body).digest()
    assert iv == bytes(16)
    assert reserved == bytes.fromhex(SHA) + bytes([pp.PRESET_CONTEXTS["research"]]) + bytes(7)
    assert data[192:288] == b"\xFF" * 96 and data[288:320] == bytes(32)
    assert data[0x140:] == body


@pytest.mark.parametrize("context", ["clinical", "research"])
def test_round_trip(tmp_path, body, context):
    keys = _keys(tmp_path)
    data = pp.build_preset_package(body, diathermy_sha=SHA, context=context, keys=keys)
    pkg = pp.parse_preset_package(data)
    assert (pkg.diathermy_sha, pkg.context, pkg.body) == (SHA, context, body)
    assert pkg.body_ok
    assert pp.verify_preset_package(data, keys.public_key)


def test_a_page_dump_parses_too(tmp_path, body):
    data = pp.build_preset_package(body, diathermy_sha=SHA, context="clinical", keys=_keys(tmp_path))
    page = data + b"\xFF" * (pp.CONSOLE_PRESET_SIZE - len(data))
    assert pp.parse_preset_package(page).body == body


def test_another_key_does_not_verify(tmp_path, body):
    data = pp.build_preset_package(body, diathermy_sha=SHA, context="clinical", keys=_keys(tmp_path, "a"))
    assert not pp.verify_preset_package(data, _keys(tmp_path, "b").public_key)


def test_a_changed_body_does_not_verify(tmp_path, body):
    keys = _keys(tmp_path)
    data = bytearray(pp.build_preset_package(body, diathermy_sha=SHA, context="clinical", keys=keys))
    data[0x140 + 10] ^= 0x01
    assert not pp.parse_preset_package(bytes(data)).body_ok
    assert not pp.verify_preset_package(bytes(data), keys.public_key)


def test_a_changed_header_does_not_verify(tmp_path, body):
    keys = _keys(tmp_path)
    data = bytearray(pp.build_preset_package(body, diathermy_sha=SHA, context="clinical", keys=keys))
    data[0x6C] ^= 0x01                           # the diathermy SHA, inside the signed part
    assert not pp.verify_preset_package(bytes(data), keys.public_key)


def test_a_firmware_image_is_not_a_preset_package(tmp_path):
    keys = _keys(tmp_path)
    keys.aes_key = bytes(16)
    image = LIFUCrypto.sign_firmware_bytes(b"\x00" * 64, keys, "1.3.0")
    with pytest.raises(LIFUCrypto.LIFUCryptoError, match="Not a preset package"):
        pp.parse_preset_package(image)


@pytest.mark.parametrize("kwargs, match", [
    ({"diathermy_sha": "abc"}, "40-character"),
    ({"diathermy_sha": "z" * 40}, "40-character"),
    ({"context": "fda"}, "Unknown preset context"),  # the old name
])
def test_bad_arguments_are_refused(tmp_path, body, kwargs, match):
    args = {"diathermy_sha": SHA, "context": "clinical", "keys": _keys(tmp_path)} | kwargs
    with pytest.raises(ValueError, match=match):
        pp.build_preset_package(body, **args)


def test_body_must_fit_the_page(tmp_path):
    room = pp.CONSOLE_PRESET_SIZE - pp.PRESET_BODY_OFFSET
    keys = _keys(tmp_path)
    assert pp.build_preset_package(b"\x01" * room, diathermy_sha=SHA, context="clinical", keys=keys)
    with pytest.raises(ValueError, match="page holds"):
        pp.build_preset_package(b"\x01" * (room + 1), diathermy_sha=SHA, context="clinical", keys=keys)
    with pytest.raises(ValueError, match="at least one record"):
        pp.build_preset_package(b"", diathermy_sha=SHA, context="clinical", keys=keys)


def test_signing_needs_the_private_key(tmp_path, body):
    keys = _keys(tmp_path)
    keys.private_key = None
    with pytest.raises(LIFUCrypto.LIFUCryptoError, match="private"):
        pp.build_preset_package(body, diathermy_sha=SHA, context="clinical", keys=keys)


def test_truncated_package_is_refused(tmp_path, body):
    data = pp.build_preset_package(body, diathermy_sha=SHA, context="clinical", keys=_keys(tmp_path))
    with pytest.raises(LIFUCrypto.LIFUCryptoError, match="truncated"):
        pp.parse_preset_package(data[:-1])
