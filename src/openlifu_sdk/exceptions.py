"""SDK-level exception hierarchy for openlifu-sdk.

Catch :class:`OpenLIFUError` to handle any SDK-specific error.
Sub-classes provide finer-grained handling.
"""

from __future__ import annotations


class OpenLIFUError(Exception):
    """Base class for all openlifu-sdk exceptions."""


class DeviceNotConnectedError(OpenLIFUError):
    """Raised when a hardware operation is attempted but the device is not connected."""


class CommunicationError(OpenLIFUError):
    """Raised when a transport-level communication failure occurs (CRC mismatch, timeout, etc.)."""


class SolutionValidationError(OpenLIFUError, ValueError):
    """Raised when a sonication solution fails safety validation (voltage, duty cycle, etc.)."""


class FirmwareUpdateError(OpenLIFUError):
    """Raised when a firmware update (DFU) operation fails."""


class ConfigurationError(OpenLIFUError):
    """Raised when device configuration is invalid or cannot be parsed."""
