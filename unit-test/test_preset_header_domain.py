# SPDX-License-Identifier: Apache-2.0
"""Which trusted key signed a preset package (PresetHeader.domain), and the
simulated boards' package headers (get_preset_info / set_preset_package)."""

import pytest

from openlifu_sdk.io import LIFUPresetPackage as pp
from openlifu_sdk.io.component import FirmwareDomain

SHA = "ab" * 20


def _keys_signing_with(tmp_path, private_key):
    """A SigningKeys whose private key is *private_key*."""
    from cryptography.hazmat.primitives import serialization

    from openlifu_sdk.io.LIFUCrypto import SigningKeys

    d = tmp_path / "keys"
    d.mkdir(exist_ok=True)
    (d / "ecdsa_private.pem").write_bytes(private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return SigningKeys.from_directory(d)


def _header_signed_by_trusted(monkeypatch, tmp_path, board, domain):
    """Sign a package with a fresh key and make the SDK trust that key as
    *board*'s *domain* key (the real private keys are not available)."""
    from cryptography.hazmat.primitives.asymmetric import ec

    from openlifu_sdk.io import LIFUCrypto

    key = ec.generate_private_key(ec.SECP256R1())
    real = LIFUCrypto.trusted_public_key
    monkeypatch.setattr(
        LIFUCrypto, "trusted_public_key",
        lambda b, d: key.public_key() if (b, d) == (board, domain) else real(b, d))
    data = pp.build_preset_package(b"\x01" * 32, diathermy_sha=SHA, context="clinical",
                                   keys=_keys_signing_with(tmp_path, key))
    return pp.parse_preset_header(data)


@pytest.mark.parametrize("domain", ["clinical", "research"])
def test_a_package_signed_with_a_trusted_key_reports_its_domain(monkeypatch, tmp_path, domain):
    header = _header_signed_by_trusted(monkeypatch, tmp_path, "console", domain)
    assert header.domain("console") is FirmwareDomain(domain)
    # The other board's keys did not sign it.
    assert header.domain("transmitter") is FirmwareDomain.UNKNOWN


def test_an_unsigned_slot_header_is_unknown():
    header = pp.parse_preset_header(pp.tx_slot_image(b"\x02" * 64))
    assert header.domain("transmitter") is FirmwareDomain.UNKNOWN


def test_contexts_keep_their_byte_values():
    """Packages written before the rename still parse to the new names."""
    assert pp.PRESET_CONTEXTS == {"clinical": 1, "research": 2}
