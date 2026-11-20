# SPDX-License-Identifier: Apache-2.0
"""The DFU request from the updaters: the application reboots into its
bootloader on taking OW_CMD_DFU, usually before its reply gets out, so no
reply is the normal case. The updater then waits for the DFU device to
enumerate instead of failing."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from openlifu_sdk.io import LIFUFirmwareUpdate as fu
from openlifu_sdk.io.exceptions import LIFUCommunicationError, LIFUDeviceError


def test_no_reply_is_not_an_error():
    enter = MagicMock(side_effect=LIFUCommunicationError("TX: enter_dfu timed out"))
    fu._request_dfu(enter, "Transmitter")          # does not raise
    enter.assert_called_once()


def test_a_device_refusal_is_still_raised():
    enter = MagicMock(side_effect=LIFUDeviceError("refused"))
    with pytest.raises(LIFUDeviceError):
        fu._request_dfu(enter, "Transmitter")


@pytest.mark.parametrize("cls, attr, name", [
    (fu.LIFUTransmitterFirmwareUpdate, "tx", "enter_dfu"),
    (fu.LIFUTransmitterFirmwareUpdate, "tx", "enter_stm32_rom_dfu"),
    (fu.LIFUFirmwareUpdate, "hv", "enter_dfu"),
    (fu.LIFUFirmwareUpdate, "hv", "enter_stm32_rom_dfu"),
])
def test_updaters_ride_out_the_missing_reply(cls, attr, name):
    device = MagicMock()
    getattr(device, name).side_effect = LIFUCommunicationError("timed out")
    upd = cls(**{attr: device})
    (upd._enter_dfu if name == "enter_dfu" else upd._enter_rom)()
    getattr(device, name).assert_called_once()
