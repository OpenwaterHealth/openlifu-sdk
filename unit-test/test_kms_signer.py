# SPDX-License-Identifier: Apache-2.0
"""Cloud KMS signing (LIFUCrypto.KmsSigner): the clinical key signs firmware
headers and preset packages, through the same sign_auth_header as local keys.

The KMS client is faked: get_public_key reports a P-256 key, asymmetric_sign
signs the digest it is sent with the matching private key (DER), as KMS does.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed  # noqa: E402

from openlifu_sdk.io import LIFUCrypto  # noqa: E402
from openlifu_sdk.io import LIFUPresetPackage as pp  # noqa: E402

KEY = "projects/p/locations/l/keyRings/r/cryptoKeys/k/cryptoKeyVersions/1"


class FakeKms:
    """What KmsSigner uses of KeyManagementServiceClient."""

    def __init__(self, algorithm="EC_SIGN_P256_SHA256", signing_key=None):
        self.priv = ec.generate_private_key(ec.SECP256R1())
        self.signing_key = signing_key or self.priv     # sign with another key to break it
        self.algorithm = algorithm
        self.signed = []

    def get_public_key(self, request):
        pem = self.priv.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        return SimpleNamespace(pem=pem, algorithm=SimpleNamespace(name=self.algorithm),
                               protection_level=SimpleNamespace(name="HSM"))

    def asymmetric_sign(self, request):
        digest = request["digest"]["sha256"]
        self.signed.append((request["name"], digest))
        der = self.signing_key.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))
        return SimpleNamespace(signature=der)


def test_signs_the_sha256_digest_and_returns_raw_rs():
    kms = FakeKms()
    signer = LIFUCrypto.KmsSigner(KEY, client=kms)
    data = b"x" * 128
    sig = signer.sign_raw(data)
    assert len(sig) == 64
    assert kms.signed == [(KEY, hashlib.sha256(data).digest())]
    assert LIFUCrypto._raw_rs_verifies(kms.priv.public_key(), data, sig)
    assert signer.protection_level == "HSM"


def test_a_signature_from_another_key_is_refused():
    kms = FakeKms(signing_key=ec.generate_private_key(ec.SECP256R1()))
    signer = LIFUCrypto.KmsSigner(KEY, client=kms)
    with pytest.raises(LIFUCrypto.LIFUCryptoError, match="does not verify"):
        signer.sign_raw(b"y" * 128)


def test_only_a_p256_key_version_is_taken():
    with pytest.raises(LIFUCrypto.LIFUCryptoError, match="VERSION"):
        LIFUCrypto.KmsSigner("projects/p/locations/l/keyRings/r/cryptoKeys/k", client=FakeKms())
    with pytest.raises(LIFUCrypto.LIFUCryptoError, match="EC_SIGN_P256_SHA256"):
        LIFUCrypto.KmsSigner(KEY, client=FakeKms(algorithm="RSA_SIGN_PSS_2048_SHA256"))


def test_a_firmware_image_signed_with_kms_verifies_with_its_public_key_only():
    kms = FakeKms()
    signer = LIFUCrypto.KmsSigner(KEY, client=kms)
    image = LIFUCrypto.sign_firmware_bytes(b"\x01" * 1000, signer, "1.3.0")
    report = LIFUCrypto.validate_signed_image(image, public_key=kms.priv.public_key())
    assert report.ok and report.header.fw_version_str == "1.3.0"
    other = ec.generate_private_key(ec.SECP256R1()).public_key()
    assert not LIFUCrypto.validate_signed_image(image, public_key=other).signature_ok


def test_local_keys_sign_without_an_aes_key(tmp_path):
    priv = ec.generate_private_key(ec.SECP256R1())
    keys = LIFUCrypto.SigningKeys(keys_dir=tmp_path, private_key=priv, public_key=priv.public_key())
    image = LIFUCrypto.sign_firmware_bytes(b"\x02" * 64, keys, "2.1.0")
    assert LIFUCrypto.validate_signed_image(image, public_key=priv.public_key()).ok
    keys.private_key = None
    with pytest.raises(LIFUCrypto.LIFUCryptoError, match="private"):
        LIFUCrypto.sign_firmware_bytes(b"\x02" * 64, keys, "2.1.0")


@pytest.mark.parametrize("board", ["console", "transmitter"])
def test_preset_packages_sign_with_kms(board):
    kms = FakeKms()
    signer = LIFUCrypto.KmsSigner(KEY, client=kms)
    body = b"OWPR" + bytes(300)
    if board == "transmitter":
        data = pp.build_tx_preset(body, diathermy_sha="cd" * 20, context="fda", keys=signer)
    else:
        data = pp.build_preset_package(body, diathermy_sha="cd" * 20, context="fda", keys=signer)
    assert pp.verify_preset_package(data, kms.priv.public_key())
    assert pp.parse_preset_package(data).context == "fda"


def test_fingerprint_matches_the_shipped_provenance():
    # PROVENANCE.txt: the console clinical key is sha256:5cffd232...
    key = LIFUCrypto.trusted_public_key("console", "clinical")
    assert LIFUCrypto.public_key_fingerprint(key).startswith("5cffd232")


def test_cli_sign_with_kms_then_verify(tmp_path, monkeypatch, capsys):
    kms = FakeKms()
    monkeypatch.setattr(LIFUCrypto, "KmsSigner",
                        lambda key, client=None, _real=LIFUCrypto.KmsSigner: _real(key, client=kms))
    fw = tmp_path / "app.bin"
    fw.write_bytes(b"\x03" * 2048)
    out = tmp_path / "app-signed.bin"
    assert LIFUCrypto.main(["sign", "--kms-key", KEY, "--firmware", str(fw),
                            "--version", "1.3.0-rc.4", "--output", str(out)]) == 0
    assert "Cloud KMS" in capsys.readouterr().out
    pem = tmp_path / "pub.pem"
    pem.write_bytes(kms.priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    assert LIFUCrypto.main(["verify", str(out), "--public-key", str(pem)]) == 0
    other = tmp_path / "other.pem"
    other.write_bytes(ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    assert LIFUCrypto.main(["verify", str(out), "--public-key", str(other)]) == 1


def test_cli_needs_exactly_one_signer(tmp_path):
    with pytest.raises(SystemExit):
        LIFUCrypto.main(["sign", "--firmware", "a", "--version", "1.0.0", "--output", "b"])
    with pytest.raises(SystemExit):
        LIFUCrypto.main(["sign", "--keys", str(tmp_path), "--kms-key", KEY, "--firmware", "a",
                         "--version", "1.0.0", "--output", "b"])


def test_preset_cli_builds_and_verifies(tmp_path, monkeypatch):
    kms = FakeKms()
    monkeypatch.setattr(LIFUCrypto, "KmsSigner",
                        lambda key, client=None, _real=LIFUCrypto.KmsSigner: _real(key, client=kms))
    body = tmp_path / "blob.bin"
    body.write_bytes(b"OWPR" + bytes(500))
    out = tmp_path / "presets.bin"
    assert pp.main(["build", "--board", "transmitter", "--body", str(body), "--diathermy-sha", "ab" * 20,
                    "--context", "fda", "--kms-key", KEY, "--output", str(out)]) == 0
    pem = tmp_path / "pub.pem"
    pem.write_bytes(kms.priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    assert pp.main(["verify", str(out), "--public-key", str(pem)]) == 0
    big = tmp_path / "big.bin"
    big.write_bytes(b"\x01" * 4000)
    assert pp.main(["build", "--board", "transmitter", "--body", str(big), "--diathermy-sha", "ab" * 20,
                    "--context", "fda", "--kms-key", KEY, "--output", str(out)]) == 2
