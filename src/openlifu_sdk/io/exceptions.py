"""Custom exceptions raised by the openlifu-sdk I/O layer.

All device-communication failures raise a subclass of :class:`LIFUError` so
that callers can use a single ``except LIFUError`` block to catch any serial
failure, or handle specific subclasses for more targeted behaviour. Each
exception carries a stable numeric :attr:`LIFUError.code` (see
``LIFUConfig.LIFU_ERR_*``) that can be safely logged or persisted and used
programmatically without string-matching the message.

Basic input-validation errors continue to raise :class:`ValueError` /
:class:`TypeError` and are *not* assigned error codes.
"""

from __future__ import annotations

from .LIFUConfig import (
    LIFU_ERR_BAD_PAYLOAD_FORMAT,
    LIFU_ERR_BAD_PAYLOAD_LENGTH,
    LIFU_ERR_DEVICE_NAK,
    LIFU_ERR_EMPTY_RESPONSE,
    LIFU_ERR_HV_NOT_SETTLED,
    LIFU_ERR_MODULE_COUNT_MISMATCH,
    LIFU_ERR_NOT_CONNECTED,
    LIFU_ERR_RESPONSE_TIMEOUT,
    LIFU_ERR_SEND_FAILED,
    LIFU_ERR_SOLUTION_INVALID,
    LIFU_ERR_SONICATION_FAILED,
    LIFU_ERR_NO_TRIGGER_STATUS,
    LIFU_ERROR_MESSAGES,
)


class LIFUError(Exception):
    """Base class for all runtime errors raised by the LIFU I/O layer.

    Attributes:
        code: Stable numeric error code (see ``LIFUConfig.LIFU_ERR_*``).
    """

    default_code: int | None = None

    def __init__(self, message: str = "", *, code: int | None = None):
        self.code = code if code is not None else self.default_code
        if not message and self.code is not None:
            message = LIFU_ERROR_MESSAGES.get(self.code, "")
        if self.code is not None:
            prefix = f"[LIFU-{self.code}] "
            if not message.startswith(prefix):
                message = prefix + message
        super().__init__(message)


class LIFUNotConnectedError(LIFUError):
    """Operation attempted on a device that is not currently connected."""

    default_code = LIFU_ERR_NOT_CONNECTED


class LIFUCommunicationError(LIFUError):
    """Transport-level failure: send error or response timeout."""

    default_code = LIFU_ERR_RESPONSE_TIMEOUT


class LIFUDeviceError(LIFUError):
    """Device firmware returned an ``OW_ERROR`` packet."""

    default_code = LIFU_ERR_DEVICE_NAK


class LIFUProtocolError(LIFUError):
    """Response from the device was malformed or had an unexpected length."""

    default_code = LIFU_ERR_BAD_PAYLOAD_LENGTH


class LIFUHVSettleError(LIFUError):
    """High-voltage rail did not settle within the configured timeout."""

    default_code = LIFU_ERR_HV_NOT_SETTLED


class LIFUSolutionError(LIFUError):
    """Provided solution failed safety validation."""

    default_code = LIFU_ERR_SOLUTION_INVALID


class LIFUSonicationError(LIFUError):
    """Sonication start/stop did not complete cleanly."""

    default_code = LIFU_ERR_SONICATION_FAILED

class LIFUNoTriggerStatusError(LIFUError):
    """Sonication trigger status could not be determined after sonication."""

    default_code = LIFU_ERR_NO_TRIGGER_STATUS


__all__ = [
    "LIFUError",
    "LIFUNotConnectedError",
    "LIFUCommunicationError",
    "LIFUDeviceError",
    "LIFUProtocolError",
    "LIFUHVSettleError",
    "LIFUSolutionError",
    "LIFUSonicationError",
    # Re-export commonly used codes for convenience
    "LIFU_ERR_BAD_PAYLOAD_FORMAT",
    "LIFU_ERR_BAD_PAYLOAD_LENGTH",
    "LIFU_ERR_DEVICE_NAK",
    "LIFU_ERR_EMPTY_RESPONSE",
    "LIFU_ERR_HV_NOT_SETTLED",
    "LIFU_ERR_MODULE_COUNT_MISMATCH",
    "LIFU_ERR_NOT_CONNECTED",
    "LIFU_ERR_RESPONSE_TIMEOUT",
    "LIFU_ERR_SEND_FAILED",
    "LIFU_ERR_SOLUTION_INVALID",
    "LIFU_ERR_SONICATION_FAILED",
]
