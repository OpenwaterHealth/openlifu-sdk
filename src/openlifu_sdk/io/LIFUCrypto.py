"""SBSFU firmware image signing, validation and inspection.

Universal implementation of the LIFU secure-bootloader image format
(``SECBOOT_ECCDSA_WITH_AES128_CBC_SHA256`` in the direct-flash / NO_LOADER
configuration) shared by the console (STM32F072) and transmitter (STM32L443)
bootloaders. A signed image is:

    [320 B header] [0xFF pad to 0x400] [firmware body, stored in clear]

The 128-byte authenticated header region carries the metadata and the
SHA-256 of the firmware body (``FwTag``) and is signed with ECDSA-P256/
SHA-256. At boot the bootloader verifies the header signature and compares
SHA-256(slot body) against ``FwTag``; the AES fields exist only to satisfy
the header format and are unused in this configuration.

Key material is never bundled with the SDK: every operation that needs keys
takes an explicit keys directory containing some of::

    ecdsa_private.pem   ECDSA P-256 private key (signing)
    ecdsa_public.pem    ECDSA P-256 public key  (validation; derived from
                        the private key when absent)
    aes128.bin          16-byte AES-128 key     (signing, header format)

Typical use::

    from openlifu_sdk.io.LIFUCrypto import sign_firmware_file, validate_signed_image

    hdr = sign_firmware_file(
        firmware="app.bin", keys_dir="bl-keys/console",
        output="app_signed.bin", version="1.0.3",
    )
    report = validate_signed_image("app_signed.bin", keys_dir="bl-keys/console")
    assert report.ok

Command line::

    python -m openlifu_sdk.io.LIFUCrypto sign   --keys DIR --firmware F --output O \
                                                (--version V | --version-header build/.../generated/version.h)
    python -m openlifu_sdk.io.LIFUCrypto verify --keys DIR SIGNED_IMAGE
    python -m openlifu_sdk.io.LIFUCrypto info   SIGNED_IMAGE [--keys DIR]

Requires the optional ``cryptography`` package (``pip install
openlifu-sdk[crypto]``) for signing and signature verification; parsing and
hash checks work without it.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path

# Base class for LIFUCryptoError. Guarded so this module stays usable as a
# standalone signer with only `cryptography` installed (e.g. CI signing),
# without pulling in the full SDK (numpy/pandas/...). Inside the SDK the real
# LIFUError base is used so `except LIFUError` still catches crypto errors.
try:
    from openlifu_sdk.io.exceptions import LIFUError as _LIFUCryptoErrorBase
except Exception:  # pragma: no cover - standalone/minimal environment
    _LIFUCryptoErrorBase = Exception

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import (
        decode_dss_signature,
        encode_dss_signature,
    )
    from cryptography.exceptions import InvalidSignature
    _HAVE_CRYPTOGRAPHY = True
except ImportError:  # pragma: no cover - depends on environment
    _HAVE_CRYPTOGRAPHY = False


class LIFUCryptoError(_LIFUCryptoErrorBase):
    """Firmware signing / validation failure (bad keys, malformed image)."""


# ---------------------------------------------------------------------------
# SBSFU image format constants (must match the bootloaders' SECoreBin)
# ---------------------------------------------------------------------------
SFU_MAGIC          = b"SFU1"
PROTOCOL_VERSION   = 1
HEADER_AUTH_LEN    = 128     # bytes covered by the ECDSA signature
HEADER_SIGN_LEN    = 64      # ECDSA P-256 raw R||S
HEADER_STATE_LEN   = 96      # FwImageState: 3 x 32 bytes, 0xFF = VALID/new
HEADER_FP_LEN      = 32      # PrevHeaderFingerprint, 0x00 on first install
HEADER_TOTAL_LEN   = HEADER_AUTH_LEN + HEADER_SIGN_LEN + HEADER_STATE_LEN + HEADER_FP_LEN
IMAGE_OFFSET       = 0x400   # firmware body offset = SFU_IMG_IMAGE_OFFSET
FLASH_WORD         = 32      # padding granule (multiple of AES block and of
                             # every target's flash programming unit)

_AUTH_STRUCT = "<4sHHIII32s32s16s28s"

PRIVATE_KEY_FILE = "ecdsa_private.pem"
PUBLIC_KEY_FILE  = "ecdsa_public.pem"
AES_KEY_FILE     = "aes128.bin"


def _require_cryptography() -> None:
    if not _HAVE_CRYPTOGRAPHY:
        raise LIFUCryptoError(
            "The 'cryptography' package is required for this operation. "
            "Install it with: pip install openlifu-sdk[crypto]"
        )


# ---------------------------------------------------------------------------
# Firmware version encoding: 16-bit bitfield  major[15:11] minor[10:5] patch[4:0]
#
# Ranges: major 0-31, minor 0-63, patch 0-31 (max version 31.63.31 = 0xFFFF).
# The packing is strictly monotonic with semver ordering, so the bootloader's
# anti-rollback integer comparison needs no knowledge of the scheme.
#
# NOTE: this replaces the earlier decimal MMmmpp convention (major*10000 +
# minor*100 + patch, major <= 6). Devices that latched an anti-rollback floor
# under the old scheme will reject new-scheme images until the floor is reset
# (full-chip erase), because old encodings are numerically much larger.
# ---------------------------------------------------------------------------

VERSION_MAJOR_BITS = 5
VERSION_MINOR_BITS = 6
VERSION_PATCH_BITS = 5
VERSION_MAJOR_MAX  = (1 << VERSION_MAJOR_BITS) - 1   # 31
VERSION_MINOR_MAX  = (1 << VERSION_MINOR_BITS) - 1   # 63
VERSION_PATCH_MAX  = (1 << VERSION_PATCH_BITS) - 1   # 31


def encode_fw_version(version: int | str) -> int:
    """Encode a firmware version as the 16-bit bitfield integer.

    Accepts either an already-encoded integer (1-65535, passed through) or a
    semantic version string ``"major.minor.patch"``. Encoding is
    ``(major << 11) | (minor << 5) | patch`` with major 0-31, minor 0-63,
    patch 0-31 - monotonic with semver ordering. Pre-release / git-describe
    suffixes are ignored (``"1.8.0-rc.1-3-gabc"`` encodes as ``1.8.0``).
    """
    if isinstance(version, int):
        if not 1 <= version <= 0xFFFF:
            raise ValueError(f"Firmware version {version} out of range 1-65535")
        return version

    base = version.strip().lstrip("v").split("-")[0].split("+")[0]
    parts = base.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"Invalid semantic version: {version!r} (want 'M.m.p')")
    major, minor, patch = (int(p) for p in parts)
    if (major > VERSION_MAJOR_MAX or minor > VERSION_MINOR_MAX
            or patch > VERSION_PATCH_MAX):
        raise ValueError(
            f"Version {version!r} does not fit the 16-bit bitfield encoding "
            f"(major 0-{VERSION_MAJOR_MAX}, minor 0-{VERSION_MINOR_MAX}, "
            f"patch 0-{VERSION_PATCH_MAX})"
        )
    encoded = (major << (VERSION_MINOR_BITS + VERSION_PATCH_BITS)) \
              | (minor << VERSION_PATCH_BITS) | patch
    if encoded < 1:
        raise ValueError("Firmware version 0.0.0 is not allowed (minimum 0.0.1)")
    return encoded


def decode_fw_version(value: int) -> str:
    """Decode a 16-bit bitfield firmware version to ``"major.minor.patch"``."""
    major = value >> (VERSION_MINOR_BITS + VERSION_PATCH_BITS)
    minor = (value >> VERSION_PATCH_BITS) & VERSION_MINOR_MAX
    patch = value & VERSION_PATCH_MAX
    return f"{major}.{minor}.{patch}"


def read_fw_version_header(header_path: str | Path) -> dict[str, str]:
    """Parse a CMake-generated ``version.h`` (e.g. ``build/<cfg>/generated/
    version.h`` in the firmware build tree).

    Returns a dict with ``FW_VERSION`` (git describe, always present) and,
    when defined, ``FW_SHA`` and ``FW_BUILD_TIME``. Feed ``FW_VERSION``
    straight to :func:`encode_fw_version` / the signing functions - the
    bitfield encoding keeps major.minor.patch and drops pre-release/describe
    suffixes (``"1.2.6-rc.1-3-g2bfcf2a"`` encodes as ``1.2.6``); the full
    string, SHA and build time remain embedded in the firmware binary itself.

    Raises:
        LIFUCryptoError: File unreadable or no FW_VERSION define found.
    """
    header_path = Path(header_path)
    if not header_path.is_file():
        raise LIFUCryptoError(f"Version header not found: {header_path}")
    text = header_path.read_text(encoding="utf-8", errors="replace")
    info: dict[str, str] = {}
    for key in ("FW_VERSION", "FW_SHA", "FW_BUILD_TIME"):
        m = re.search(rf'#define\s+{key}\s+"([^"]*)"', text)
        if m:
            info[key] = m.group(1)
    if "FW_VERSION" not in info:
        raise LIFUCryptoError(
            f'No #define FW_VERSION "..." found in {header_path}')
    return info


# ---------------------------------------------------------------------------
# Key handling
# ---------------------------------------------------------------------------

@dataclass
class SigningKeys:
    """Key material loaded from a keys directory.

    ``private_key``/``aes_key`` are present only when the directory holds
    them; ``public_key`` is loaded from ``ecdsa_public.pem`` or derived from
    the private key.
    """

    keys_dir: Path
    private_key: object | None = None   # ec.EllipticCurvePrivateKey
    public_key: object | None = None    # ec.EllipticCurvePublicKey
    aes_key: bytes | None = None

    @classmethod
    def from_directory(cls, keys_dir: str | Path, *,
                       require_private: bool = False) -> "SigningKeys":
        """Load keys from *keys_dir*.

        Args:
            keys_dir: Directory containing the key files (see module doc).
            require_private: Require the private + AES keys (signing).

        Raises:
            LIFUCryptoError: Directory or required key files missing/invalid.
        """
        _require_cryptography()
        keys_dir = Path(keys_dir)
        if not keys_dir.is_dir():
            raise LIFUCryptoError(f"Keys directory not found: {keys_dir}")

        keys = cls(keys_dir=keys_dir)

        priv_path = keys_dir / PRIVATE_KEY_FILE
        if priv_path.is_file():
            try:
                keys.private_key = serialization.load_pem_private_key(
                    priv_path.read_bytes(), password=None)
            except (ValueError, TypeError) as e:
                raise LIFUCryptoError(f"Cannot load {priv_path}: {e}") from e

        pub_path = keys_dir / PUBLIC_KEY_FILE
        if pub_path.is_file():
            try:
                keys.public_key = serialization.load_pem_public_key(
                    pub_path.read_bytes())
            except ValueError as e:
                raise LIFUCryptoError(f"Cannot load {pub_path}: {e}") from e
        elif keys.private_key is not None:
            keys.public_key = keys.private_key.public_key()

        aes_path = keys_dir / AES_KEY_FILE
        if aes_path.is_file():
            keys.aes_key = aes_path.read_bytes()
            if len(keys.aes_key) != 16:
                raise LIFUCryptoError(
                    f"{aes_path} must be 16 bytes, got {len(keys.aes_key)}")

        if require_private:
            missing = []
            if keys.private_key is None:
                missing.append(PRIVATE_KEY_FILE)
            if keys.aes_key is None:
                missing.append(AES_KEY_FILE)
            if missing:
                raise LIFUCryptoError(
                    f"Signing requires {', '.join(missing)} in {keys_dir}")
        return keys


# ---------------------------------------------------------------------------
# Header parsing / metadata
# ---------------------------------------------------------------------------

@dataclass
class FirmwareHeader:
    """Parsed 320-byte SBSFU image header."""

    magic: bytes
    protocol_version: int
    fw_version: int
    fw_size: int
    partial_fw_offset: int
    partial_fw_size: int
    fw_tag: bytes
    partial_fw_tag: bytes
    init_vector: bytes
    signature: bytes
    image_state: bytes
    prev_fingerprint: bytes

    @classmethod
    def from_bytes(cls, data: bytes) -> "FirmwareHeader":
        """Parse a header from the first bytes of a signed image / slot dump.

        Raises:
            LIFUCryptoError: If *data* is shorter than one header.
        """
        if len(data) < HEADER_TOTAL_LEN:
            raise LIFUCryptoError(
                f"Image too short for an SBSFU header "
                f"({len(data)} < {HEADER_TOTAL_LEN} bytes)")
        (magic, proto, fw_version, fw_size, p_off, p_size,
         fw_tag, p_tag, iv, _reserved) = struct.unpack_from(_AUTH_STRUCT, data, 0)
        sig_off   = HEADER_AUTH_LEN
        state_off = sig_off + HEADER_SIGN_LEN
        fp_off    = state_off + HEADER_STATE_LEN
        return cls(
            magic=magic, protocol_version=proto, fw_version=fw_version,
            fw_size=fw_size, partial_fw_offset=p_off, partial_fw_size=p_size,
            fw_tag=fw_tag, partial_fw_tag=p_tag, init_vector=iv,
            signature=data[sig_off:state_off],
            image_state=data[state_off:fp_off],
            prev_fingerprint=data[fp_off:fp_off + HEADER_FP_LEN],
        )

    @property
    def fw_version_str(self) -> str:
        """Firmware version decoded per the bitfield convention."""
        return decode_fw_version(self.fw_version)

    @property
    def image_state_str(self) -> str:
        """Human-readable FwImageState (0xFF*96 = as-signed / VALID)."""
        if self.image_state == b"\xFF" * HEADER_STATE_LEN:
            return "VALID (factory / as-signed)"
        if self.image_state == b"\x00" * HEADER_STATE_LEN:
            return "INVALIDATED"
        return "MODIFIED (bootloader-managed state bytes present)"

    def describe(self) -> str:
        """Multi-line human-readable summary of the header metadata."""
        return "\n".join([
            f"Magic            : {self.magic!r}"
            + ("" if self.magic == SFU_MAGIC else "  (INVALID, expected b'SFU1')"),
            f"Protocol version : {self.protocol_version}",
            f"FW version       : {self.fw_version}  (semver {self.fw_version_str})",
            f"FW size          : {self.fw_size:,} bytes",
            f"FW tag (SHA-256) : {self.fw_tag.hex()}",
            f"Init vector      : {self.init_vector.hex()}  (unused at boot)",
            f"Signature (R||S) : {self.signature.hex()}",
            f"Image state      : {self.image_state_str}",
            f"Prev fingerprint : {self.prev_fingerprint.hex()}",
            f"Body offset      : 0x{IMAGE_OFFSET:X}",
        ])


def parse_signed_image(image: str | Path | bytes) -> FirmwareHeader:
    """Parse the header of a signed image given as a path or raw bytes."""
    data = image if isinstance(image, bytes) else Path(image).read_bytes()
    return FirmwareHeader.from_bytes(data)


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def sign_firmware_bytes(firmware: bytes, keys: SigningKeys,
                        version: int | str) -> bytes:
    """Sign a raw firmware binary; returns the complete signed image.

    Args:
        firmware: Raw application binary (linked for slot base + 0x400).
        keys: Keys loaded with ``require_private=True``.
        version: MMmmpp integer or ``"major.minor.patch"`` string.

    Raises:
        LIFUCryptoError: Missing keys.
        ValueError: Bad version encoding.
    """
    _require_cryptography()
    if keys.private_key is None or keys.aes_key is None:
        raise LIFUCryptoError(
            "Signing requires the private ECDSA key and the AES key "
            "(load keys with require_private=True)")
    fw_version = encode_fw_version(version)

    # Pad the body to the flash-word granule; FwTag covers the padded body.
    pad_len = (-len(firmware)) % FLASH_WORD
    body    = firmware + b"\xFF" * pad_len
    fw_tag  = hashlib.sha256(body).digest()

    auth_header = struct.pack(
        _AUTH_STRUCT,
        SFU_MAGIC,
        PROTOCOL_VERSION,
        fw_version,
        len(body),
        0,                      # PartialFwOffset
        0,                      # PartialFwSize
        fw_tag,
        fw_tag,                 # PartialFwTag == FwTag for a full image
        os.urandom(16),         # InitVector: header format only, unused
        b"\x00" * 28,           # Reserved
    )

    der_sig = keys.private_key.sign(auth_header, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")

    header = (auth_header + signature
              + b"\xFF" * HEADER_STATE_LEN      # FwImageState: VALID
              + b"\x00" * HEADER_FP_LEN)        # PrevHeaderFingerprint
    return header + b"\xFF" * (IMAGE_OFFSET - HEADER_TOTAL_LEN) + body


def sign_firmware_file(firmware: str | Path, keys_dir: str | Path,
                       output: str | Path,
                       version: int | str) -> FirmwareHeader:
    """Sign a firmware file and write the signed image.

    Args:
        firmware: Path to the raw application ``.bin``.
        keys_dir: Directory holding ``ecdsa_private.pem`` and ``aes128.bin``.
        output: Path the signed image is written to.
        version: MMmmpp integer or ``"major.minor.patch"`` string.

    Returns:
        The parsed header of the signed image.

    Raises:
        LIFUCryptoError: Missing/invalid keys or unreadable firmware.
    """
    firmware = Path(firmware)
    if not firmware.is_file():
        raise LIFUCryptoError(f"Firmware file not found: {firmware}")
    keys   = SigningKeys.from_directory(keys_dir, require_private=True)
    signed = sign_firmware_bytes(firmware.read_bytes(), keys, version)
    Path(output).write_bytes(signed)
    return FirmwareHeader.from_bytes(signed)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@dataclass
class ValidationReport:
    """Result of validating a signed image. ``ok`` is the overall verdict;
    the individual fields say which checks passed. ``signature_ok`` is None
    when no public key was available to check it."""

    header: FirmwareHeader
    magic_ok: bool
    protocol_ok: bool
    size_ok: bool
    fw_tag_ok: bool
    partial_tag_ok: bool
    pad_ok: bool
    signature_ok: bool | None
    trailing_bytes: int          # bytes past FwSize (slot dumps: erased flash)

    @property
    def structural_ok(self) -> bool:
        """All checks that need no key material (magic, sizes, hashes)."""
        return (self.magic_ok and self.protocol_ok and self.size_ok
                and self.fw_tag_ok and self.partial_tag_ok)

    @property
    def ok(self) -> bool:
        return self.structural_ok and self.signature_ok is True

    @property
    def verdict(self) -> str:
        if self.ok:
            return "VALID"
        if self.structural_ok and self.signature_ok is None:
            return "UNVERIFIED (structure/hashes pass; no key for signature check)"
        return "NOT VALID"

    def describe(self) -> str:
        def mark(v: bool | None) -> str:
            return "PASS" if v else ("SKIP (no public key)" if v is None else "FAIL")
        lines = [
            f"Magic 'SFU1'          : {mark(self.magic_ok)}",
            f"Protocol version      : {mark(self.protocol_ok)}",
            f"Body size vs FwSize   : {mark(self.size_ok)}",
            f"FwTag (SHA-256)       : {mark(self.fw_tag_ok)}",
            f"PartialFwTag == FwTag : {mark(self.partial_tag_ok)}",
            f"Header pad (0xFF)     : {mark(self.pad_ok)}",
            f"ECDSA signature       : {mark(self.signature_ok)}",
            f"Overall               : {self.verdict}",
        ]
        if self.trailing_bytes:
            lines.insert(-1, f"Trailing bytes        : {self.trailing_bytes:,} "
                             "(ignored; expected for slot dumps)")
        return "\n".join(lines)


def validate_signed_image(image: str | Path | bytes,
                          keys_dir: str | Path | None = None) -> ValidationReport:
    """Validate a signed image (or a dump of the active slot).

    Checks structure, sizes, the SHA-256 firmware tag, and - when *keys_dir*
    provides a public key - the ECDSA header signature: everything the
    bootloader itself checks before launching the application.

    Args:
        image: Path to the signed image, or its raw bytes. A dump of the
            whole slot also works; bytes past ``FwSize`` are ignored.
        keys_dir: Optional keys directory for the signature check.

    Raises:
        LIFUCryptoError: Image shorter than a header, or keys unreadable.
    """
    data   = image if isinstance(image, bytes) else Path(image).read_bytes()
    header = FirmwareHeader.from_bytes(data)

    body     = data[IMAGE_OFFSET:]
    size_ok  = len(body) >= header.fw_size
    fw_body  = body[:header.fw_size]
    fw_tag_ok = size_ok and hashlib.sha256(fw_body).digest() == header.fw_tag
    pad = data[HEADER_TOTAL_LEN:min(IMAGE_OFFSET, len(data))]

    signature_ok: bool | None = None
    if keys_dir is not None:
        keys = SigningKeys.from_directory(keys_dir)
        if keys.public_key is None:
            raise LIFUCryptoError(
                f"No {PUBLIC_KEY_FILE} or {PRIVATE_KEY_FILE} in {keys_dir} "
                "to verify the signature with")
        r = int.from_bytes(header.signature[:32], "big")
        s = int.from_bytes(header.signature[32:], "big")
        try:
            keys.public_key.verify(encode_dss_signature(r, s),
                                   data[:HEADER_AUTH_LEN],
                                   ec.ECDSA(hashes.SHA256()))
            signature_ok = True
        except InvalidSignature:
            signature_ok = False

    return ValidationReport(
        header=header,
        magic_ok=header.magic == SFU_MAGIC,
        protocol_ok=header.protocol_version == PROTOCOL_VERSION,
        size_ok=size_ok,
        fw_tag_ok=fw_tag_ok,
        partial_tag_ok=header.partial_fw_tag == header.fw_tag,
        pad_ok=pad == b"\xFF" * len(pad),
        signature_ok=signature_ok,
        trailing_bytes=max(0, len(body) - header.fw_size),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m openlifu_sdk.io.LIFUCrypto",
        description="Sign, validate and inspect LIFU SBSFU firmware images.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sign = sub.add_parser("sign", help="Sign a raw firmware binary")
    p_sign.add_argument("--keys", required=True,
                        help="Keys directory (ecdsa_private.pem + aes128.bin)")
    p_sign.add_argument("--firmware", required=True, help="Raw application .bin")
    p_sign.add_argument("--output", required=True, help="Signed image output path")
    ver_group = p_sign.add_mutually_exclusive_group(required=True)
    ver_group.add_argument("--version",
                           help="MMmmpp integer or 'major.minor.patch' "
                                "(git-describe suffixes are dropped)")
    ver_group.add_argument("--version-header",
                           help="Path to the build's generated version.h; "
                                "FW_VERSION is read from it")

    p_verify = sub.add_parser("verify", help="Validate a signed image")
    p_verify.add_argument("image", help="Signed image (or slot dump)")
    p_verify.add_argument("--keys", required=True,
                          help="Keys directory (ecdsa_public.pem)")

    p_info = sub.add_parser("info", help="Show signed-image metadata")
    p_info.add_argument("image", help="Signed image (or slot dump)")
    p_info.add_argument("--keys", help="Optional keys directory to also "
                                       "verify the signature")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "sign":
            if args.version_header:
                info = read_fw_version_header(args.version_header)
                version: int | str = info["FW_VERSION"]
                print(f"Version source : {args.version_header}")
                print(f"  FW_VERSION   : {info['FW_VERSION']}")
                if "FW_SHA" in info:
                    print(f"  FW_SHA       : {info['FW_SHA']}")
                if "FW_BUILD_TIME" in info:
                    print(f"  FW_BUILD_TIME: {info['FW_BUILD_TIME']}")
                print(f"  Encoded      : {encode_fw_version(version)} "
                      f"(header keeps major.minor.patch only)")
            else:
                version = args.version
                if version.isdigit():
                    version = int(version)
            header = sign_firmware_file(args.firmware, args.keys,
                                        args.output, version)
            print(f"Signed image written: {args.output}")
            print(header.describe())
            return 0
        if args.cmd == "verify":
            report = validate_signed_image(args.image, keys_dir=args.keys)
            print(report.describe())
            return 0 if report.ok else 1
        # info
        report = validate_signed_image(args.image, keys_dir=args.keys)
        print(report.header.describe())
        print()
        print(report.describe())
        return 0
    except (LIFUCryptoError, ValueError) as e:
        print(f"ERROR: {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
