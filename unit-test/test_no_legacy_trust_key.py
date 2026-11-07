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

import hashlib
import re
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "openlifu_sdk"

#: SHA-256 of the removed 32-byte legacy trust key.
REMOVED_KEY_SHA256 = "a3a676f886aa0ac953de7eb46ba3204eb707b70390d7c91cabb44291c4aba375"
KEY_LEN = 32

_HEX_BYTE = re.compile(rb"0x([0-9A-Fa-f]{2})\b")


def _package_files():
    return [p for p in PACKAGE.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts]


def _holds_key(data: bytes, key_sha256: str = REMOVED_KEY_SHA256) -> bool:
    """True if *data* contains the 32-byte key whose SHA-256 is *key_sha256*,
    either as raw bytes or as a run of ``0xNN`` literals (source form)."""
    literals = bytes(int(h, 16) for h in _HEX_BYTE.findall(data))
    return any(hashlib.sha256(blob[i:i + KEY_LEN]).hexdigest() == key_sha256
               for blob in (data, literals)
               for i in range(len(blob) - KEY_LEN + 1))


class TestNoLegacyTrustKey(unittest.TestCase):

    def test_the_scan_finds_a_planted_key(self):
        # The scan must be able to fail: plant a stand-in key in both forms.
        key = bytes(range(0x40, 0x40 + KEY_LEN))
        digest = hashlib.sha256(key).hexdigest()
        raw = b"\x00" * 7 + key + b"\xff" * 3
        source = b"KEY = bytes([" + ", ".join(f"0x{b:02X}" for b in key).encode() + b"])"
        self.assertTrue(_holds_key(raw, digest))
        self.assertTrue(_holds_key(source, digest))
        self.assertFalse(_holds_key(raw[:-4] + b"\xff" * 4, digest))   # one byte short

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
