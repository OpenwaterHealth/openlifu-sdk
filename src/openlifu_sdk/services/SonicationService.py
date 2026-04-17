from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, Optional

import numpy as np
import pandas as pd

from openlifu_sdk.io.LIFUConfig import LIFUInterfaceStatus

from ..io.LIFUConsole import LIFUConsole
from ..io.LIFUTransmitter import LIFUTransmitter, TriggerMode

from ..models.tx_registers import (
    DEFAULT_PATTERN_DUTY_CYCLE,
    NUM_CHANNELS,
    Tx7332DelayProfile,
    Tx7332PulseProfile,
    TxDeviceRegisters,
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

ACTIVE_TRIGGER_STATUSES = {"RUNNING", "STARTED", "ACTIVE", "ON"}
INACTIVE_TRIGGER_STATUSES = {"READY", "STOPPED", "IDLE", "OFF"}

class SonicationService:
    """Validates, translates, and programs a solution onto the TX device
    and HV controller. Also responsible for starting and stopping sonication by triggering the TX and enabling/disabling HV power."""

    def __init__(
            self, 
            transmitter: LIFUTransmitter, 
            console: LIFUConsole, 
            voltage_table_selection: str, 
            sequence_time_selection: str, 
            duty_cycle_selection: str
        ):

        self.transmitter = transmitter
        self.console = console

        self.voltage_table_selection = voltage_table_selection
        self.sequence_time_selection = sequence_time_selection
        self.duty_cycle_selection = duty_cycle_selection

        self.tx_registers = None
        self.voltage_table = None
        self.sequence_time = None
        self.duty_cycles = None
        self.module_invert = False
        self.status = LIFUInterfaceStatus.STATUS_NOT_READY
        self._trigger_active = False
        self._solution = None
                
        
    # Temporary fix for hardware variations between EVT0 and EVT2    
    def _resolve_voltage_chart_evt_version(self, voltage_table: Optional[str]) -> list[list[int]]:
        if voltage_table is None:
            try:
                evt_version = "evt0" if self.console.get_version().startswith("v1.1") else "evt2"
            except Exception as e:
                logger.error("Error getting console version: %s", e)
                raise e
        else:
            evt_version = voltage_table.lower()
            if evt_version not in MAX_VOLTAGE_BY_DUTY_CYCLE_AND_SEQUENCE_TIME:
                raise ValueError(f"Invalid voltage_table option '{voltage_table}'. Valid options are: {tuple(MAX_VOLTAGE_BY_DUTY_CYCLE_AND_SEQUENCE_TIME.keys())}")

        return MAX_VOLTAGE_BY_DUTY_CYCLE_AND_SEQUENCE_TIME[evt_version]

    # Restrict sequence time options for users vs QA
    def _resolve_max_sequence_time_set(self, sequence_time: Optional[str]) -> list[int]:
        if sequence_time is None:
            return REF_MAX_SEQUENCE_TIMES["default"]
        else:
            sequence_time = sequence_time.lower()
            if sequence_time not in REF_MAX_SEQUENCE_TIMES:
                raise ValueError(f"Invalid sequence_time option '{sequence_time}'. Valid options are: {tuple(REF_MAX_SEQUENCE_TIMES.keys())}")
            return REF_MAX_SEQUENCE_TIMES[sequence_time]

    # Restrict duty cycle options for users vs QA
    def _resolve_duty_cycle_set(self, duty_cycle: Optional[str]) -> list[float]:
        if duty_cycle is None:
            return REF_MAX_DUTY_CYCLES["default"]
        else:
            duty_cycle = duty_cycle.lower()
            if duty_cycle not in REF_MAX_DUTY_CYCLES:
                raise ValueError(f"Invalid duty_cycle option '{duty_cycle}'. Valid options are: {tuple(REF_MAX_DUTY_CYCLES.keys())}")
            return REF_MAX_DUTY_CYCLES[duty_cycle]

    def _turn_hv_off_if_connected(self) -> bool:
        if self.console is None or not self.console.is_connected():
            return False

        hv_off = self.console.turn_hv_off()
        if not hv_off:
            logger.error("Failed to turn off HV power")
        return hv_off

    # -- solution queries --------------------------------------------------

    @staticmethod
    def get_sequence_duty_cycle(solution: Dict) -> float:
        """Get the duty cycle of the sequence in the solution."""
        if solution['sequence']['pulse_train_interval'] == 0:
            return solution['pulse']['duration'] / solution['sequence']['pulse_interval']
        else:
            return (solution['pulse']['duration'] * solution['sequence']['pulse_count']) / solution['sequence']['pulse_train_interval']

    @staticmethod
    def get_sequence_duration(solution: Dict) -> float:
        """Get the duration of the sequence in the solution."""
        if solution['sequence']['pulse_train_interval'] == 0:
            return solution['sequence']['pulse_interval'] * solution['sequence']['pulse_count'] * solution['sequence']['pulse_train_count']
        else:
            return solution['sequence']['pulse_train_interval'] * solution['sequence']['pulse_train_count']

    def get_max_voltage(self, solution: Dict) -> float:
        """Get the maximum voltage for a given solution."""
        sequence_duty_cycle = self.get_sequence_duty_cycle(solution)
        sequence_duration = self.get_sequence_duration(solution)

        duty_cycles_limits = np.array(self.duty_cycles)
        duty_cycle_index = np.where(duty_cycles_limits >= sequence_duty_cycle)[0][0]

        duration_limits = np.array(self.sequence_time)
        duration_index = np.where(duration_limits >= sequence_duration)[0][0]

        return self.voltage_table[duty_cycle_index][duration_index]

    def get_max_voltage_table(self) -> pd.DataFrame:
        """Get a table of the maximum voltages for different duty cycles and sequence times."""
        data = {
            "Duty Cycle (%)": [f"<={100 * dc:0.1f}%" for dc in self.duty_cycles],
            }
        for i, duration in enumerate(self.sequence_time):
            col_name = f"<={duration // 60} min"
            data[col_name] = [
                self.voltage_table[j][i] for j in range(len(self.duty_cycles))
            ]
        max_voltage = pd.DataFrame(data).set_index("Duty Cycle (%)")
        max_voltage.Name = "Maximum Voltage (V)"
        max_voltage.Description = "This table shows the maximum voltage for different duty cycles and sequence times."
        return max_voltage

    # -- solution validation -----------------------------------------------

    def check_solution(self, solution: Dict) -> None:
        """
        Check if the solution is valid.
        Args:
            solution (Dict): The solution to check. Should be a dict with keys "profiles" and "sonications", where "profiles" is a list of profile dicts and "sonications" is a list of sonication dicts. 
        Raises:
            ValueError: If the solution is invalid.
        """
        
        self.voltage_table = self._resolve_voltage_chart_evt_version(self.voltage_table_selection)
        self.sequence_time = self._resolve_max_sequence_time_set(self.sequence_time_selection)
        self.duty_cycles = self._resolve_duty_cycle_set(self.duty_cycle_selection)
        sequence_duty_cycle = self.get_sequence_duty_cycle(solution)
        duty_cycles_limits = np.array(self.duty_cycles)
        if sequence_duty_cycle > duty_cycles_limits.max():
            raise ValueError(f"Sequence duty cycle ({100*sequence_duty_cycle:0.1f} %) exceeds maximum allowed duty cycle ({100*duty_cycles_limits.max():0.1f} %).")
        duty_cycle_index = np.where(duty_cycles_limits >= sequence_duty_cycle)[0][0]

        sequence_duration = self.get_sequence_duration(solution)
        duration_limits = np.array(self.sequence_time)
        if sequence_duration > duration_limits.max():
            raise ValueError(f"Sequence duration ({sequence_duration:0.0f} s) exceeds maximum allowed duration ({duration_limits.max()} s).")
        duration_index = np.where(duration_limits >= sequence_duration)[0][0]

        max_voltage = self.voltage_table[duty_cycle_index][duration_index]
        if solution['voltage'] > max_voltage:
            raise ValueError(f"Voltage ({solution['voltage']:0.1f}V) exceeds maximum allowed voltage ({max_voltage:0.1f}V) for duty cycle ({100*sequence_duty_cycle:0.1f} <= {100*duty_cycles_limits[duty_cycle_index]}%) and sequence time ({sequence_duration:0.0f} <= {duration_limits[duration_index]}s).")

    # -- solution programming -----------

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
        if not _allow_unsafe_solution:
            self.check_solution(solution)

        self._solution = solution
        self._trigger_active = False

        if "name" in solution:
            solution_name = f'Solution "{solution["name"]}"'
        else:
            solution_name = "Solution"

        logger.info("Loading %s...", solution_name)
        self.status = LIFUInterfaceStatus.STATUS_PROGRAMMING

        # Module inversion
        if "transducer" in solution and solution["transducer"] is not None and "module_invert" in solution["transducer"]:
            self.set_module_invert(solution["transducer"]["module_invert"])
        else:
            self.set_module_invert(False)

        # Build profiles from solution
        pulse = solution['pulse']
        delays = np.array(solution['delays'])
        apodizations = np.array(solution['apodizations'])
        if delays.ndim == 1:
            delays = delays.reshape(1, -1)
        if apodizations.ndim == 1:
            apodizations = apodizations.reshape(1, -1)

        n = delays.shape[0]
        n_elements = delays.shape[1]
        n_required_devices = int(n_elements / NUM_CHANNELS)

        if self.transmitter is None or not self.transmitter.is_connected():
            self.status = LIFUInterfaceStatus.STATUS_COMMS_ERROR
            raise ConnectionError("Transmitter is not connected")
        
        n_detected_tx = self.transmitter.enum_tx7332_devices()

        self.tx_registers = TxDeviceRegisters(num_transmitters=n_detected_tx, module_invert=self.module_invert)
        
        if n_required_devices != n_detected_tx:
            self.status = LIFUInterfaceStatus.STATUS_ERROR
            errmsg = (
                f"Number of detected TX devices ({n_detected_tx}) "
                f"does not match required ({n_required_devices})"
            )
            logger.exception(errmsg)
            raise OSError(errmsg)

        if n != apodizations.shape[0]:
            self.status = LIFUInterfaceStatus.STATUS_ERROR
            raise ValueError("Delays and apodizations must have the same number of rows")
        if n > 1:
            self.status = LIFUInterfaceStatus.STATUS_ERROR
            raise NotImplementedError("Multiple foci not supported yet")

        for profile in range(n):
            duty_cycle = (
                DEFAULT_PATTERN_DUTY_CYCLE
                * max(apodizations[profile, :])
                * pulse["amplitude"]
            )
            pulse_profile = Tx7332PulseProfile(
                profile=profile + 1,
                frequency=pulse["frequency"],
                cycles=int(pulse["duration"] * pulse["frequency"]),
                duty_cycle=duty_cycle,
            )

            self.tx_registers.add_pulse_profile(pulse_profile)

            delay_profile = Tx7332DelayProfile(
                profile=profile + 1,
                delays=delays[profile, :],
                apodizations=apodizations[profile, :],
            )

            self.tx_registers.add_delay_profile(delay_profile)

        sequence = solution['sequence']
        trigger_mode_int = trigger_mode.value
        trigger_json = {
            "TriggerFrequencyHz": sequence["pulse_interval"],
            "TriggerPulseCount": sequence["pulse_count"],
            "TriggerPulseWidthUsec": sequence["pulse_width"],
            "TriggerPulseTrainInterval": sequence["pulse_train_interval"] * 1000000,
            "TriggerPulseTrainCount": sequence["pulse_train_count"],
            "TriggerMode": trigger_mode_int,
            "ProfileIndex": profile_index,
            "ProfileIncrement": profile_increment
        }

        self.transmitter.set_trigger(trigger_json)
           
        register_set = self.tx_registers.get_registers(pack=True, pack_single=True)
        for txi, txregs in enumerate(register_set):
            for addr, reg_values in txregs.items():
                if not self.transmitter.write_block(identifier=txi, start_address=addr, reg_values=reg_values):
                    self.status = LIFUInterfaceStatus.STATUS_ERROR
                    errmsg = f"Failed to write block to transmitter {txi} at address {addr}"
                    logger.exception(errmsg)
                    raise OSError(errmsg)
            
        # Set voltage on HV controller
        hv_set_value = float(solution['voltage'])
        if self.console is not None and self.console.is_connected() and hv_set_value is not None and hv_set_value > 0:
            if not self.console.set_hv(hv_set_value):
                self.status = LIFUInterfaceStatus.STATUS_ERROR
                errmsg = f"Failed to set HV voltage to {hv_set_value} V on console"
                logger.exception(errmsg)
                raise OSError(errmsg)
            
            self.status = LIFUInterfaceStatus.STATUS_READY
        else:
            logger.warning("Console not connected or invalid voltage value; skipping HV set command")
            self.status = LIFUInterfaceStatus.STATUS_WARNING


    # -- sonication control ------------------------------------------------

    def start_sonication(self) -> bool:
        """Start sonication by triggering the TX and enabling HV power."""
        trigger_on = False
        logger.debug("Starting sonication")
        if self.console is not None and self.console.is_connected():
            if not self.console.turn_hv_on():
                self.status = LIFUInterfaceStatus.STATUS_ERROR
                logger.error("Failed to turn on HV power")
                return trigger_on
            
        if self.transmitter is None or not self.transmitter.is_connected():
            self._turn_hv_off_if_connected()
            self.status = LIFUInterfaceStatus.STATUS_COMMS_ERROR
            logger.error("Transmitter is not connected")
            return trigger_on
        
        trigger_on = self.transmitter.start_trigger()
        if not trigger_on:
            self._turn_hv_off_if_connected()
            self.status = LIFUInterfaceStatus.STATUS_ERROR
            logger.error("Failed to start trigger on transmitter")
            return trigger_on

        self._trigger_active = True
        self.status = LIFUInterfaceStatus.STATUS_RUNNING
        return trigger_on
    
    def stop_sonication(self) -> bool:
        """Stop sonication by stopping the TX trigger and disabling HV power."""
        logger.debug("Stopping sonication")
        trigger_off = False
        if self.transmitter is None or not self.transmitter.is_connected():
            self.status = LIFUInterfaceStatus.STATUS_WARNING
            logger.error("Transmitter is not connected")
        else:
            trigger_off = self.transmitter.stop_trigger()
            if trigger_off:
                self._trigger_active = False
                self.status = LIFUInterfaceStatus.STATUS_READY
            else:
                self.status = LIFUInterfaceStatus.STATUS_WARNING 
        
        if self.console is not None and self.console.is_connected():
            if not self.console.turn_hv_off():
                self.status = LIFUInterfaceStatus.STATUS_WARNING
            elif not trigger_off:
                self.status = LIFUInterfaceStatus.STATUS_WARNING
        
        return trigger_off
    
    # -- solution management --------------------------------------------------

    def is_trigger_active(self) -> bool:
        """Check whether sonication trigger is active on the transmitter."""
        if self.transmitter is None or not self.transmitter.is_connected():
            return self._trigger_active

        trigger_state = self.transmitter.get_trigger()
        if not isinstance(trigger_state, dict):
            logger.warning("get_trigger did not return a dict")
            return self._trigger_active

        trigger_status = str(trigger_state.get("TriggerStatus", "")).upper()
        if trigger_status in ACTIVE_TRIGGER_STATUSES:
            self._trigger_active = True
            return True

        if trigger_status in INACTIVE_TRIGGER_STATUSES:
            self._trigger_active = False
            return False

        logger.warning("Unknown trigger status '%s'", trigger_status)
        return self._trigger_active

    def is_solution_loaded(self) -> bool:
        """Check if a solution is currently loaded on the device."""
        return self._solution is not None    

    def get_loaded_solution(self) -> Dict | None:
        """Get the currently loaded solution from the device, if any. Returns a dict with keys "profiles" and "sonications", where "profiles" is a list of profile dicts and "sonications" is a list of sonication dicts. If no solution is loaded, returns None."""
        return self._solution
    
    def clear_solution(self, reset_hardware: bool = True) -> None:
        """Clear the currently loaded solution from the device.

        Args:
            reset_hardware (bool): Whether to reset the hardware after clearing the solution (defaults to True)
        """
        self.tx_registers = None
        self._solution = None
        self._trigger_active = False
        if reset_hardware:
            if self.transmitter is not None and self.transmitter.is_connected():
                self.transmitter.stop_trigger()

            if self.console is not None and self.console.is_connected():
                self.console.turn_hv_off()
                self.console.set_hv(0)

        self.status = LIFUInterfaceStatus.STATUS_NOT_READY

    def get_status(self) -> LIFUInterfaceStatus:
        """Get the current status of the device, including whether a solution is loaded, and any errors."""
        return self.status

