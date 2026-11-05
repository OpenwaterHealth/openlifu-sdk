# SPDX-License-Identifier: Apache-2.0
"""String forms of a device hardware ID (the MCU's 12-byte unique ID)."""

from base58 import b58encode

#: Bytes of the UID kept by the legacy calibration-record form. Older test
#: app and verification-tank releases sliced the hex string by a byte count
#: (``hwid_hex[:12]``), so their records hold base58 of the first 6 bytes.
LEGACY_HWID_BYTES = 6


def format_hwid(hex_str: str) -> str:
    """Format a raw hex string into a dash-separated hardware ID.

    Example: 'deadbeefcafe' -> 'DEAD-BEEF-CAFE'
    """
    hex_str = hex_str.upper()
    return "-".join(hex_str[i:i+4] for i in range(0, len(hex_str), 4))


def encode_hwid(uid: bytes, raw_hex: bool = False, base58: bool = False,
                legacy_6byte: bool = False) -> str:
    """Return *uid* in one of the forms ``get_hardware_id`` offers.

    Default: dash-separated upper-case hex (:func:`format_hwid`).
    ``raw_hex``: lower-case hex. ``base58``: base58 of the whole UID, the
    form calibration records carry. ``base58`` with ``legacy_6byte``: base58
    of the first :data:`LEGACY_HWID_BYTES` bytes, the form older records
    carry.

    Raises:
        ValueError: ``raw_hex`` with ``base58``, or ``legacy_6byte``
            without ``base58``.
    """
    if raw_hex and base58:
        raise ValueError("raw_hex and base58 are different forms; pass one")
    if legacy_6byte and not base58:
        raise ValueError("legacy_6byte is a base58 form; pass base58=True")
    if base58:
        kept = uid[:LEGACY_HWID_BYTES] if legacy_6byte else uid
        return b58encode(kept).decode("ascii")
    return uid.hex() if raw_hex else format_hwid(uid.hex())
