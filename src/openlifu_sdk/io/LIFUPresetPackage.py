# SPDX-License-Identifier: Apache-2.0
"""Signed preset packages (``PRS1``) for a board's preset flash page.

Presets live in their own flash page above the application slot, not in the
firmware image, so one firmware release serves bare-metal, research and
clinical units and a preset change needs no firmware rebuild. The page holds
one package:

    0x000  PRS1 header, the SFU1 header layout (320 bytes):
             magic "PRS1", protocol 1, FwVersion 0,
             FwSize = PartialFwSize = body size,
             FwTag = PartialFwTag = SHA-256 of the body,
             InitVector zero,
             Reserved = diathermy commit SHA (20 bytes) + context byte + 7 zeros,
           then the ECDSA P-256 signature of those 128 bytes (raw R||S), the
           image-state bytes (0xFF) and the fingerprint (0x00)
    0x140  body: the preset records (console: PRESET_HV_RECORD v2, back to back)

It is signed with the same key as the firmware for that board and domain
(research: the firmware repo's FW_SIGNING_KEY; clinical: the Cloud KMS key).
The magic is inside the signed bytes, so a preset header never passes as a
firmware header or the reverse. The bootloader (open-lifu-console-bl >=
1.1.0-rc.2) verifies the header before it lets page 61 be erased or written
over DFU, and the body's SHA-256 when the download completes.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Iterable

from openlifu_sdk.io import LIFUCrypto
from openlifu_sdk.io.LIFUCrypto import (
    HEADER_AUTH_LEN, HEADER_FP_LEN, HEADER_SIGN_LEN, HEADER_STATE_LEN,
    HEADER_TOTAL_LEN, LIFUCryptoError, SigningKeys, _AUTH_STRUCT,
)

PRESET_MAGIC = b"PRS1"
PRESET_PROTOCOL = 1
PRESET_BODY_OFFSET = HEADER_TOTAL_LEN        # 0x140

#: Console preset page (open-lifu-console-bl memory_map.h MEM_PRESET_*).
CONSOLE_PRESET_BASE = 0x0801E800
CONSOLE_PRESET_SIZE = 0x800

#: Transmitter preset slots (open-lifu-transmitter-bl / openlifu-transmitter-fw
#: memory_map.h): 10 slots of 4 KB, each one PRS1 header + one preset blob.
TX_PRESET_BASE = 0x08035000
TX_PRESET_SLOT_SIZE = 0x1000
TX_PRESET_SLOTS = 10


def tx_preset_slot_address(slot: int) -> int:
    """Flash address of transmitter preset slot *slot* (0-based)."""
    if not 0 <= slot < TX_PRESET_SLOTS:
        raise ValueError(f"Transmitter preset slot must be 0..{TX_PRESET_SLOTS - 1}, got {slot}")
    return TX_PRESET_BASE + slot * TX_PRESET_SLOT_SIZE

#: The context byte: which released preset set the body is.
PRESET_CONTEXTS = {"fda": 1, "test": 2}
_CONTEXT_NAMES = {v: k for k, v in PRESET_CONTEXTS.items()}

_SHA_LEN = 20                                 # git commit SHA-1, as bytes


@dataclass(frozen=True)
class PresetHeader:
    """A parsed PRS1 header: what a device's preset page reports
    (HVController.get_preset_info) without its body."""

    header: bytes          # the 320-byte PRS1 header
    body_size: int
    body_sha256: bytes     # FwTag
    diathermy_sha: str     # 40 hex characters
    context: str           # "fda" / "test", or "unknown(<n>)"

    def signed_by(self, public_key) -> bool:
        """True if the header's signature verifies with *public_key*
        (anything LIFUCrypto.load_public_key takes). The signature covers the
        body's hash, so this says who signed the whole preset set."""
        key = LIFUCrypto.load_public_key(public_key)
        return LIFUCrypto.verify_header_signature(
            self.header[:HEADER_AUTH_LEN + HEADER_SIGN_LEN], key)


@dataclass(frozen=True)
class PresetPackage:
    """A parsed preset package. Parsing does not check the signature."""

    header: bytes          # the 320-byte PRS1 header
    body: bytes            # the preset records
    diathermy_sha: str     # 40 hex characters
    context: str           # "fda" / "test", or "unknown(<n>)"
    body_sha256: bytes     # FwTag from the header

    @property
    def body_ok(self) -> bool:
        """The body's SHA-256 matches the header."""
        return hashlib.sha256(self.body).digest() == self.body_sha256


def console_preset_body(machine_configs: Iterable[dict]) -> bytes:
    """The console preset page body for a released preset set: one
    PRESET_HV_RECORD v2 per preset, back to back, in the order the host
    indexes them (the same records the firmware used to link in)."""
    from openlifu_sdk.io.LIFUTXPresets import console_preset_record

    return b"".join(console_preset_record(mc, version=2) for mc in machine_configs)


def build_preset_package(body: bytes, *, diathermy_sha: str, context: str,
                         keys: SigningKeys,
                         page_size: int = CONSOLE_PRESET_SIZE) -> bytes:
    """Sign *body* (the preset records) into a preset package.

    Args:
        body: The preset records.
        diathermy_sha: The diathermy application commit the presets come from
            (40 hex characters).
        context: ``"fda"`` or ``"test"``.
        keys: Keys holding the private key for the board and domain.
        page_size: The preset page size; the package must fit it.

    Raises:
        ValueError: Empty or oversized body, bad SHA or context.
        LIFUCryptoError: No private key.
    """
    if not body:
        raise ValueError("A preset package needs at least one record")
    if PRESET_BODY_OFFSET + len(body) > page_size:
        raise ValueError(f"Preset body is {len(body)} bytes; the page holds "
                         f"{page_size - PRESET_BODY_OFFSET}")
    if context not in PRESET_CONTEXTS:
        raise ValueError(f"Unknown preset context {context!r}; expected one of "
                         f"{sorted(PRESET_CONTEXTS)}")
    try:
        sha = bytes.fromhex(diathermy_sha)
    except ValueError:
        sha = b""
    if len(sha) != _SHA_LEN:
        raise ValueError(f"diathermy_sha must be a 40-character commit SHA, "
                         f"got {diathermy_sha!r}")

    tag = hashlib.sha256(body).digest()
    reserved = sha + bytes([PRESET_CONTEXTS[context]]) + bytes(28 - _SHA_LEN - 1)
    auth = struct.pack(_AUTH_STRUCT, PRESET_MAGIC, PRESET_PROTOCOL, 0, len(body),
                       0, len(body), tag, tag, bytes(16), reserved)
    signature = LIFUCrypto.sign_auth_header(auth, keys)
    header = auth + signature + b"\xFF" * HEADER_STATE_LEN + b"\x00" * HEADER_FP_LEN
    return header + body


def tx_slot_image(blob: bytes, *, keys: SigningKeys | None = None,
                  diathermy_sha: str | None = None, context: str | None = None) -> bytes:
    """The image of one transmitter preset slot for *blob*: a PRS1 header,
    then the blob.

    With *keys* (and *diathermy_sha* and *context*) the header is signed, as
    :func:`build_tx_preset` makes it: what a released preset is, and what the
    bootloader takes. Without, the header carries the blob's size and hash and
    a zero signature, context and commit: a research or bare-metal image
    stores it (it checks integrity, not the signature), the bootloader never.

    Raises:
        ValueError: The blob does not fit a slot.
    """
    if keys is not None:
        return build_tx_preset(blob, diathermy_sha=diathermy_sha, context=context, keys=keys)
    if not blob:
        raise ValueError("A preset slot needs a blob")
    if PRESET_BODY_OFFSET + len(blob) > TX_PRESET_SLOT_SIZE:
        raise ValueError(f"Preset blob is {len(blob)} bytes; a slot holds "
                         f"{TX_PRESET_SLOT_SIZE - PRESET_BODY_OFFSET}")
    tag = hashlib.sha256(blob).digest()
    auth = struct.pack(_AUTH_STRUCT, PRESET_MAGIC, PRESET_PROTOCOL, 0, len(blob),
                       0, len(blob), tag, tag, bytes(16), bytes(28))
    header = auth + bytes(HEADER_SIGN_LEN) + b"\xFF" * HEADER_STATE_LEN + b"\x00" * HEADER_FP_LEN
    return header + blob


def build_tx_preset(blob: bytes, *, diathermy_sha: str, context: str,
                    keys: SigningKeys) -> bytes:
    """Sign one transmitter preset blob (``LIFUTXPresets.preset_blob``) into
    the image of one 4 KB preset slot: the PRS1 header, then the blob.

    Raises:
        ValueError: The blob does not fit a slot, or a bad SHA or context.
        LIFUCryptoError: No private key.
    """
    return build_preset_package(blob, diathermy_sha=diathermy_sha, context=context,
                                keys=keys, page_size=TX_PRESET_SLOT_SIZE)


def parse_preset_header(data: bytes) -> PresetHeader:
    """Parse the PRS1 header at the start of *data* (a header, a package or
    a page dump). Does not check the signature.

    Raises:
        LIFUCryptoError: Not a PRS1 header, or malformed.
    """
    if len(data) < PRESET_BODY_OFFSET:
        raise LIFUCryptoError(f"Preset package shorter than its header ({len(data)} bytes)")
    (magic, protocol, _version, size, _poff, psize, tag, ptag, _iv,
     reserved) = struct.unpack_from(_AUTH_STRUCT, data, 0)
    if magic != PRESET_MAGIC:
        raise LIFUCryptoError(f"Not a preset package (magic {magic!r})")
    if protocol != PRESET_PROTOCOL or psize != size or ptag != tag or size == 0:
        raise LIFUCryptoError("Malformed preset package header")
    ctx = reserved[_SHA_LEN]
    return PresetHeader(header=bytes(data[:PRESET_BODY_OFFSET]), body_size=size,
                        body_sha256=tag, diathermy_sha=reserved[:_SHA_LEN].hex(),
                        context=_CONTEXT_NAMES.get(ctx, f"unknown({ctx})"))


def parse_preset_package(data: bytes) -> PresetPackage:
    """Split a preset package (or a dump of the preset page) into its parts.
    Bytes past the body (erased flash in a page dump) are ignored.

    Raises:
        LIFUCryptoError: Not a preset package, or truncated.
    """
    hdr = parse_preset_header(data)
    if len(data) < PRESET_BODY_OFFSET + hdr.body_size:
        raise LIFUCryptoError(f"Preset package truncated: body is {hdr.body_size} bytes, "
                              f"{len(data) - PRESET_BODY_OFFSET} present")
    return PresetPackage(
        header=hdr.header,
        body=bytes(data[PRESET_BODY_OFFSET:PRESET_BODY_OFFSET + hdr.body_size]),
        diathermy_sha=hdr.diathermy_sha,
        context=hdr.context,
        body_sha256=hdr.body_sha256,
    )


def verify_preset_package(data: bytes, public_key) -> bool:
    """True if *data* is a preset package signed by the private key matching
    *public_key* and its body matches the signed hash: what the bootloader
    checks before and after writing the preset page.

    Args:
        data: The package (or a dump of the preset page).
        public_key: Anything :func:`LIFUCrypto.load_public_key` takes.

    Raises:
        LIFUCryptoError: Not a preset package, or the key cannot be loaded.
    """
    pkg = parse_preset_package(data)
    key = LIFUCrypto.load_public_key(public_key)
    return pkg.body_ok and LIFUCrypto.verify_header_signature(
        pkg.header[:HEADER_AUTH_LEN + HEADER_SIGN_LEN], key)
