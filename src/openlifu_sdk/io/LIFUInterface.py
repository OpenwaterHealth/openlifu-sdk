from __future__ import annotations

import logging

from ow_comms.config import (
    DEFAULT_TIMEOUT, OW_VID, OW_TRANSMITTER_PID, OW_CONSOLE_PID,
)
from .LIFUTransmitter import LIFUTransmitter
from .LIFUConsole import LIFUConsole

log = logging.getLogger("LIFUInterface")


class LIFUInterface:
    """Top-level facade that holds a :class:`LIFUTransmitter` and a :class:`LIFUConsole`."""

    def __init__(self, baudrate: int = 921600, timeout: float = DEFAULT_TIMEOUT,
                 tx_vid: int = OW_VID, tx_pid: int = OW_TRANSMITTER_PID,
                 con_vid: int = OW_VID, con_pid: int = OW_CONSOLE_PID):
        self.transmitter = LIFUTransmitter(tx_vid, tx_pid, baudrate=baudrate, timeout=timeout)
        self.console = LIFUConsole(con_vid, con_pid, baudrate=baudrate, timeout=timeout)

    # -- Convenience batch operations ---------------------------------

    def connect(self) -> tuple[bool, bool]:
        """Connect both components.  Returns ``(tx_ok, con_ok)``."""
        return self.transmitter.connect(), self.console.connect()

    def disconnect(self):
        self.transmitter.disconnect()
        self.console.disconnect()

    def start(self):
        """Enter async mode for both components."""
        self.transmitter.start()
        self.console.start()

    def stop(self):
        """Leave async mode for both components."""
        self.transmitter.stop()
        self.console.stop()
