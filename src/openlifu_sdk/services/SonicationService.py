from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, Optional

import numpy as np
import pandas as pd

from ..io.LIFUConsole import LIFUConsole
from ..io.LIFUTransmitter import LIFUTransmitter, TriggerMode

from ..models.tx_registers import (
    DEFAULT_PATTERN_DUTY_CYCLE,
    NUM_CHANNELS,
    Tx7332DelayProfile,
    Tx7332PulseProfile,
)

logger = logging.getLogger(__name__)

REF_MAX_SEQUENCE_TIMES = {
    "default": [2*60, 5*60, 10*60],    # users to use default values
    "stress_test": [60*60, 60*60, 60*60] # QA to use stress test values
}

REF_MAX_DUTY_CYCLES = {
    "default": [0.05, 0.1, 0.2, 0.3, 0.4, 0.5],
    "stress_test": [0.5, 0.5, 0.5, 0.5, 0.5, 0.5]
}

MAX_VOLTAGE_BY_DUTY_CYCLE_AND_SEQUENCE_TIME = {
    "evt2": [
        [45, 45, 45], # 0.05
        [40, 40, 40], # 0.1
        [40, 40, 35], # 0.2
        [40, 35, 30], # 0.3
        [35, 30, 25], # 0.4
        [30, 25, 20] # 0.5
    ],
    "evt0": [
        [65, 65, 65], # 0.05
        [65, 65, 50], # 0.1
        [50, 40, 35], # 0.2
        [45, 35, 30], # 0.3
        [35, 30, 25], # 0.4
        [30, 25, 20] # 0.5
    ],
}

class SonicationService:
    """Validates, translates, and programs a solution onto the TX device
    and HV controller. Also responsible for starting and stopping sonication by triggering the TX and enabling/disabling HV power."""

    def __init__(self, transmitter: LIFUTransmitter, console: LIFUConsole):
        self.transmitter = transmitter
        self.console = console

    def is_solution_loaded(self) -> bool:
        """Check if a solution is currently loaded on the device."""
        pass    

    def get_loaded_solution(self) -> Dict | None:
        """Get the currently loaded solution from the device, if any. Returns a dict with keys "profiles" and "sonications", where "profiles" is a list of profile dicts and "sonications" is a list of sonication dicts. If no solution is loaded, returns None."""
        pass

    def clear_solution(self, reset_hardware: bool = True) -> None:
        """Clear the currently loaded solution from the device.

        Args:
            reset_hardware (bool): Whether to reset the hardware after clearing the solution (defaults to True)
        """
        pass

    def get_status(self) -> Dict:
        """Get the current status of the device, including whether a solution is loaded, and any errors."""
        pass

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

    def start_sonication(self):
        """Start sonication by triggering the TX and enabling HV power."""
        pass    

    def stop_sonication(self):
        """Stop sonication by stopping the TX trigger and disabling HV power."""
        pass
    
