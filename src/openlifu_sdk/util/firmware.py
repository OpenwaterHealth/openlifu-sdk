"""Local firmware discovery.

The SDK flashes the firmware images shipped with the wheel under
``openlifu_sdk/firmware/`` with the canonical filenames
``openlifu-console-fw-signed.bin`` and ``openlifu-transmitter-fw-signed.bin``
(SBSFU 'SFU1' signed images — the release the SDK was built/tested against).

There is deliberately no runtime GitHub download flow: to flash a different
version, download the signed image from the firmware repo's GitHub releases
(or build and sign it from source) and pass its path explicitly (e.g.
``python -m openlifu_sdk.io.LIFUFirmwareUpdate --app <signed.bin>``).

Firmware repos:
  - Console:     https://github.com/OpenwaterHealth/openlifu-console-fw
  - Transmitter: https://github.com/OpenwaterHealth/openlifu-transmitter-fw
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

CONSOLE_FIRMWARE_URL = "https://github.com/OpenwaterHealth/openlifu-console-fw"
TRANSMITTER_FIRMWARE_URL = "https://github.com/OpenwaterHealth/openlifu-transmitter-fw"

FIRMWARE_DIR_REL = '../firmware'

# Canonical bundled filenames (match the CI release artifact names).
CONSOLE_FIRMWARE_FILENAME = 'openlifu-console-fw-signed.bin'
TRANSMITTER_FIRMWARE_FILENAME = 'openlifu-transmitter-fw-signed.bin'


# =============================================================================
# Filesystem helpers
# =============================================================================

def _firmware_dir() -> Path:
    return (Path(__file__).parent / FIRMWARE_DIR_REL).resolve()


def _get_firmware_version(filename: Path | str) -> str:
    """Version of a signed firmware image.

    An SBSFU 'SFU1' signed image carries its FwVersion in the signed header
    (the 16-bit bitfield decoded via LIFUCrypto). For any other file, fall
    back to the first ``MAJOR.MINOR.PATCH`` ASCII triple embedded in the blob
    (the app's git-describe string).
    """
    if isinstance(filename, str):
        filename = Path(filename)
    data = filename.read_bytes()

    if data[:4] == b"SFU1":
        from openlifu_sdk.io import LIFUCrypto
        try:
            header = LIFUCrypto.FirmwareHeader.from_bytes(
                data[:LIFUCrypto.HEADER_TOTAL_LEN])
            return header.fw_version_str
        except LIFUCrypto.LIFUCryptoError as e:
            logger.warning("SFU1 header parse failed for %s (%s); falling "
                           "back to string scan", filename, e)

    match = re.search(rb'\d+\.\d+\.\d+', data)
    if match:
        return match.group().decode()
    raise ValueError(f"No firmware version found in {filename}")


def _bundled_firmware(filename: str, label: str) -> Path:
    path = _firmware_dir() / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"No bundled {label} firmware found at {path}. To flash a "
            f"different version, download the signed image from the firmware "
            f"repo's GitHub releases (or build it from source) and pass its "
            f"path explicitly.")
    return path


# =============================================================================
# Public: path + version lookup
# =============================================================================

def get_console_firmware_path() -> Path:
    """Path to the bundled console firmware image."""
    return _bundled_firmware(CONSOLE_FIRMWARE_FILENAME, "console")


def get_transmitter_firmware_path() -> Path:
    """Path to the bundled transmitter firmware image."""
    return _bundled_firmware(TRANSMITTER_FIRMWARE_FILENAME, "transmitter")


def get_console_firmware_version() -> str:
    """Version of the bundled console firmware."""
    return _get_firmware_version(get_console_firmware_path())


def get_transmitter_firmware_version() -> str:
    """Version of the bundled transmitter firmware."""
    return _get_firmware_version(get_transmitter_firmware_path())


__all__ = [
    "CONSOLE_FIRMWARE_FILENAME",
    "CONSOLE_FIRMWARE_URL",
    "FIRMWARE_DIR_REL",
    "TRANSMITTER_FIRMWARE_FILENAME",
    "TRANSMITTER_FIRMWARE_URL",
    "get_console_firmware_path",
    "get_console_firmware_version",
    "get_transmitter_firmware_path",
    "get_transmitter_firmware_version",
]
