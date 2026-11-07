# SPDX-License-Identifier: Apache-2.0
"""The SDK ships no hard-coded key material (SR-021, #91).

The legacy bootloader's HMAC trust key (``LEGACY_TRUST_HMAC_KEY``) was
shared, public, and let anyone authorize any image on a legacy-bootloader
unit. It was removed together with every path that used it. These tests fail
if it comes back, under its old name or under any other, in the package
sources or in a bundled binary. Only the key's SHA-256 is held here, so the
test itself carries no key material.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "openlifu_sdk"

#: SHA-256 of the removed 32-byte legacy trust key.
REMOVED_KEY_SHA256 = "a3a676f886aa0ac953de7eb46ba3204eb707b70390d7c91cabb44291c4aba375"
KEY_LEN = 32

_HEX_BYTE = re.compile(rb"(?:0x|\\x)([0-9A-Fa-f]{2})")   # 0x17, ... or b"\x17..."
_HEX_RUN = re.compile(rb"[0-9A-Fa-f]{64,}")                  # bytes.fromhex("17b2...")
_B64_RUN = re.compile(rb"[A-Za-z0-9+/]{43,}={0,2}")          # base64 of 32 bytes or more


def _package_files():
    return [p for p in PACKAGE.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts]


def _decodings(data: bytes):
    """*data* itself, plus every byte string its source forms spell out: a run
    of ``0xNN`` / ``\\xNN`` literals, a hex string, a base64 string."""
    yield data
    yield bytes(int(h, 16) for h in _HEX_BYTE.findall(data))
    for run in _HEX_RUN.findall(data):
        yield binascii.unhexlify(run[: len(run) // 2 * 2])
        yield binascii.unhexlify(run[1:][: (len(run) - 1) // 2 * 2])   # odd alignment
    for run in _B64_RUN.findall(data):
        for start in range(4):   # the key may start mid-quantum
            chunk = run[start:]
            try:
                yield base64.b64decode(chunk[: len(chunk) // 4 * 4])
            except binascii.Error:
                pass


def _holds_key(data: bytes, key_sha256: str = REMOVED_KEY_SHA256) -> bool:
    """True if *data* contains the 32-byte key whose SHA-256 is *key_sha256*,
    raw or in one of the source forms of :func:`_decodings`."""
    return any(hashlib.sha256(blob[i:i + KEY_LEN]).hexdigest() == key_sha256
               for blob in _decodings(data)
               for i in range(len(blob) - KEY_LEN + 1))


class TestNoLegacyTrustKey(unittest.TestCase):

    def test_the_scan_finds_a_planted_key(self):
        # The scan must be able to fail: plant a stand-in key in both forms.
        key = bytes(range(0x40, 0x40 + KEY_LEN))
        digest = hashlib.sha256(key).hexdigest()
        raw = b"\x00" * 7 + key + b"\xff" * 3
        forms = {
            "raw": raw,
            "0xNN list": b"KEY = bytes([" + ", ".join(f"0x{b:02X}" for b in key).encode() + b"])",
            "bytes literal": b'KEY = b"' + "".join(f"\\x{b:02x}" for b in key).encode() + b'"',
            "hex string": b'KEY = bytes.fromhex("0' + key.hex().encode() + b'")',
            "base64": b"KEY = '" + base64.b64encode(b"zz" + key) + b"'",
        }
        for name, blob in forms.items():
            with self.subTest(form=name):
                self.assertTrue(_holds_key(blob, digest))
        self.assertFalse(_holds_key(raw[:-4] + b"\xff" * 4, digest))   # last key byte changed

    def test_examples_and_docs_hold_no_key_either(self):
        root = PACKAGE.parent.parent
        files = [p for d in ("examples", "docs") for p in (root / d).rglob("*") if p.is_file()]
        self.assertTrue(files)
        self.assertEqual([str(p.relative_to(root)) for p in files
                          if b"LEGACY_TRUST_HMAC_KEY" in p.read_bytes() or _holds_key(p.read_bytes())], [])

    def test_no_package_file_names_the_key(self):
        offenders = [str(p.relative_to(PACKAGE)) for p in _package_files()
                     if b"LEGACY_TRUST_HMAC_KEY" in p.read_bytes()]
        self.assertEqual(offenders, [])

    def test_no_package_file_holds_the_key_bytes(self):
        files = _package_files()
        self.assertTrue(any(p.suffix == ".bin" for p in files))   # the bundled images are scanned
        offenders = [str(p.relative_to(PACKAGE)) for p in files if _holds_key(p.read_bytes())]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
