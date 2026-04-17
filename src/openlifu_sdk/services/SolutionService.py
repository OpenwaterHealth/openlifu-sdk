from __future__ import annotations


from typing import Dict

from ..io.LIFUConsole import LIFUConsole
from ..io.LIFUTransmitter import LIFUTransmitter, TriggerMode


class SolutionService:
    """Validates, translates, and programs a solution onto the TX device
    and HV controller."""

    def __init__(self, transmitter: LIFUTransmitter, console: LIFUConsole):
        self.transmitter = transmitter
        self.console = console

    def check_solution(self, solution: Dict) -> None:
        """
        Check if the solution is valid.
        Args:
            solution (Dict): The solution to check. Should be a dict with keys "profiles" and "sonications", where "profiles" is a list of profile dicts and "sonications" is a list of sonication dicts. 
        Raises:
            ValueError: If the solution is invalid.
        """
        pass

    def set_solution(self,
                     solution: Dict,
                     profile_index:int=1,
                     profile_increment:bool=True,
                     trigger_mode: TriggerMode = TriggerMode.SEQUENCE,
                     _allow_unsafe_solution: bool = False
                     ) -> None:
        """
        Load a solution to the device.

        Args:
            solution (Dict): The solution to load. Should be a dict with keys "profiles" and "sonications", where "profiles" is a list of profile dicts and "sonications" is a list of sonication dicts. 
            profile_index (int): The profile index to load the solution to (defaults to 1)
            profile_increment (bool): Whether to increment the profile index for each profile in the solution (defaults to True)
            trigger_mode (TriggerMode): The trigger mode to use (defaults to "sequence")
            _allow_unsafe_solution (bool): Allow loading a solution that may be unsafe (defaults to False)
        """
        pass    
