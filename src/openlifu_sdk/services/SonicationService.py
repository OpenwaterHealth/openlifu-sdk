from __future__ import annotations

from ..io.LIFUTransmitter import LIFUTransmitter
from ..io.LIFUConsole import LIFUConsole


class SonicationService:
    """Coordinates HV power and TX trigger for sonication start/stop."""
    def __init__(self, transmitter: LIFUTransmitter, console: LIFUConsole):
        self.transmitter = transmitter
        self.console = console

    def start_sonication(self):
        """Start sonication by triggering the TX and enabling HV power."""
        pass    

    def stop_sonication(self):
        """Stop sonication by stopping the TX trigger and disabling HV power."""
        pass
    
