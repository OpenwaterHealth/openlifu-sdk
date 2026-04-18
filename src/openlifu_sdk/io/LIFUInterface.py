from __future__ import annotations

import logging
from typing import Dict, Literal, Optional

from ow_comms.config import DEFAULT_TIMEOUT, OW_VID

from .LIFUTransmitter import LIFUTransmitter, TriggerMode
from .LIFUConsole import LIFUConsole
from .LIFUConfig import OW_TRANSMITTER_PID, OW_CONSOLE_PID, LIFUInterfaceStatus
from ..services.SonicationService import SonicationService

log = logging.getLogger("LIFUInterface")

class LIFUInterface:
    """Top-level facade that holds a :class:`LIFUTransmitter` and a :class:`LIFUConsole`."""

    def __init__(self, baudrate: int = 921600, timeout: float = DEFAULT_TIMEOUT,
                 tx_vid: int = OW_VID, tx_pid: int = OW_TRANSMITTER_PID,
                 con_vid: int = OW_VID, con_pid: int = OW_CONSOLE_PID,
                 voltage_table_selection: Optional[str] = None,
                 sequence_time_selection: Optional[str] = None,
                 duty_cycle_selection: Optional[str] = None):
        
        self.transmitter = LIFUTransmitter(tx_vid, tx_pid, baudrate=baudrate, timeout=timeout)
        self.console = LIFUConsole(con_vid, con_pid, baudrate=baudrate, timeout=timeout)

        self.sonication_service = SonicationService(
            transmitter=self.transmitter,
            console=self.console,
            voltage_table_selection=voltage_table_selection,
            sequence_time_selection=sequence_time_selection,
            duty_cycle_selection=duty_cycle_selection
        )

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

    def get_status(self) -> LIFUInterfaceStatus:
        """
        Query the device status.

        Returns:
            int: The device status.
        """
        return self.sonication_service.get_status()
    
    def check_solution(self, solution: Dict) -> None:
        """
        Check if the solution is valid.
        Args:
            solution (Dict): The solution to check.
        Raises:
            ValueError: If the solution is invalid.
        """
        self.sonication_service.check_solution(solution)

    def set_solution(self,
                     solution: Dict,
                     profile_index:int=1,
                     profile_increment:bool=True,
                     trigger_mode: TriggerMode = TriggerMode.SEQUENCE,
                     _allow_unsafe_solution: bool = False
                     ) -> bool:
        """
        Load a solution to the device.

        Args:
            solution (Solution): The solution to load.
            profile_index (int): The profile index to load the solution to (defaults to 0)
            profile_increment (bool): Increment the profile index
            trigger_mode (TriggerMode): The trigger mode to use (defaults to "sequence")
            module_invert (List[bool]|bool): Invert the signal on all modules (singleton) or specific modules (list) (defaults to False)
            _allow_unsafe_solution (bool): Allow loading a solution that may be unsafe (defaults to False)
        """
        
        return self.sonication_service.set_solution(
                solution=solution,
                profile_index=profile_index,
                profile_increment=profile_increment,
                trigger_mode=trigger_mode,
                _allow_unsafe_solution=_allow_unsafe_solution,
        )


    def start_sonication(self, async_mode: bool | None = None) -> bool:
        """
        Start sonication.

        Args:
            async_mode (bool | None): Whether to start sonication in asynchronous mode (defaults to None, which means it will use the current async mode setting of the interface).

        Sets the device to a running state and sends a start command if necessary.
        """
        return self.sonication_service.start_sonication()


    def stop_sonication(self) -> bool:
        """
        Stop sonication.

        Stops the current sonication process.
        """
        return self.sonication_service.stop_sonication()

    def close(self):
        self.stop()

        """Close all connections."""
        if self.transmitter:
            self.transmitter.close()
        if self.console:
            self.console.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
