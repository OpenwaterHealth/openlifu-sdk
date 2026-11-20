# SPDX-License-Identifier: Apache-2.0
"""Firmware image helpers.

The SDK carries no firmware. The images come from the firmware releases:

  - research (signed with the research key): the GitHub releases of
    openlifu-console-fw, openlifu-transmitter-fw and the
    open-lifu-*-bl-updater repositories;
  - clinical (signed with the Cloud KMS key): the private artifact bucket,
    packaged by openlifu-fw-updater.

The caller passes each image to the update functions (``signed_app``,
``bl_updater``, ``production_image``; ``--app``, ``--bl-updater``,
``--production`` on the command line). :func:`firmware_image_version` reads the
version an image carries.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

CONSOLE_FIRMWARE_URL = "https://github.com/OpenwaterHealth/openlifu-console-fw"
TRANSMITTER_FIRMWARE_URL = "https://github.com/OpenwaterHealth/openlifu-transmitter-fw"


def firmware_image_version(filename: Path | str) -> str:
    """Version of a firmware image file.

    An SBSFU 'SFU1' signed image carries its FwVersion in the signed header
    (the 16-bit bitfield decoded by LIFUCrypto). For any other file, the first
    ``MAJOR.MINOR.PATCH`` ASCII triple in it (the app's git-describe string).

    Raises:
        ValueError: No version found.
        OSError: The file cannot be read.
    """
    data = Path(filename).read_bytes()
    if data[:4] == b"SFU1":
        from openlifu_sdk.io import LIFUCrypto
        try:
            header = LIFUCrypto.FirmwareHeader.from_bytes(data[:LIFUCrypto.HEADER_TOTAL_LEN])
            return header.fw_version_str
        except LIFUCrypto.LIFUCryptoError as e:
            logger.warning("SFU1 header parse failed for %s (%s); falling "
                           "back to string scan", filename, e)
    match = re.search(rb"\d+\.\d+\.\d+", data)
    if match:
        return match.group().decode()
    raise ValueError(f"No firmware version found in {filename}")


__all__ = [
    "CONSOLE_FIRMWARE_URL",
    "TRANSMITTER_FIRMWARE_URL",
    "firmware_image_version",
]
