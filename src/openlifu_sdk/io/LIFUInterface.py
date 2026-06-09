from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import os
import sys
from enum import Enum
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from openlifu_sdk.io.LIFUConfig import (
    DEFAULT_TIMEOUT, 
    OW_CONSOLE_PID, 
    OW_TRANSMITTER_PID, 
    OW_VID,
    SETTLE_TIME_HV_OFF,
    SETTLE_TIME_HV_ON
)
from openlifu_sdk.io.exceptions import (
    LIFUHardwareInUseError,
    LIFUNoTriggerStatusError,
    LIFUSolutionError,
)
from openlifu_sdk.io.LIFUHVController import HVController
from openlifu_sdk.io.LIFUTXDevice import TriggerModeOpts, TxDevice

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

class LIFUInterfaceStatus(Enum):
    STATUS_COMMS_ERROR = -1
    STATUS_SYS_OFF = 0
    STATUS_SYS_POWERUP = 1
    STATUS_SYS_ON = 2
    STATUS_PROGRAMMING = 3
    STATUS_READY = 4
    STATUS_NOT_READY = 5
    STATUS_RUNNING = 6
    STATUS_FINISHED = 7
    STATUS_ERROR = 8

logger = logging.getLogger(__name__)

OPENLIFU_HW_INTERFACE_PID_ENV = "OPENLIFU_HW_INTERFACE_PID"


def _pid_alive(pid: int) -> bool:
    """Return True if a process with the given PID is currently running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user
        return True
    return True


# ---------------------------------------------------------------------------
# Cross-process hardware-interface lock
#
# We need a value that any process on the machine (or for the user) can read
# to discover whether another process is currently holding the LIFU hardware.
# Per-process ``os.environ`` is not enough; on Windows we read/write the
# *persistent* User environment variable directly via the registry, which is
# visible to every process the user launches. On non-Windows platforms we
# fall back to a PID file under the user's home directory, which serves the
# same purpose.
# ---------------------------------------------------------------------------

def _hw_pid_lock_read() -> str:
    """Return the raw value of the cross-process hardware-interface PID slot.

    Empty string if it is not set / cannot be read.
    """
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, OPENLIFU_HW_INTERFACE_PID_ENV)
                return str(value).strip()
        except FileNotFoundError:
            return ""
        except OSError as exc:
            logger.debug("Could not read User env %s: %s", OPENLIFU_HW_INTERFACE_PID_ENV, exc)
            return ""
    path = _hw_pid_lock_file_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        logger.debug("Could not read PID lock file %s: %s", path, exc)
        return ""


def _hw_pid_lock_write(value: str) -> None:
    """Persist the hardware-interface PID slot. Empty string clears it."""
    if sys.platform == "win32":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, OPENLIFU_HW_INTERFACE_PID_ENV, 0, winreg.REG_SZ, value)
        # Mirror to the current process so subsequent reads of os.environ in
        # this process see the up-to-date value too.
        os.environ[OPENLIFU_HW_INTERFACE_PID_ENV] = value
        return
    path = _hw_pid_lock_file_path()
    if not value:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        os.environ[OPENLIFU_HW_INTERFACE_PID_ENV] = ""
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(value)
    os.environ[OPENLIFU_HW_INTERFACE_PID_ENV] = value


def _hw_pid_lock_file_path() -> str:
    """Path to the PID lock file used on non-Windows platforms."""
    return os.path.join(os.path.expanduser("~"), ".openlifu", "hw_interface_pid")

class LIFUInterface:
    hvcontroller: HVController = None
    txdevice: TxDevice = None

    def __init__(self,
                 vid: int = OW_VID,
                 tx_pid: int = OW_TRANSMITTER_PID,
                 con_pid: int = OW_CONSOLE_PID,
                 baudrate: int = 921600,
                 timeout: float = DEFAULT_TIMEOUT,
                 TX_test_mode: bool = False,
                 HV_test_mode: bool = False,
                 run_async: bool = False,
                 ext_power_supply: bool = False,
                 module_invert: bool | List[bool] = False,
                 voltage_table_selection: Optional[str] = None,
                 sequence_time_selection: Optional[str] = None,
                 duty_cycle_selection: Optional[str] = None) -> None:
        """
        Initialize the LIFUInterface with given parameters and store them in the class.

        Args:
            vid (int): Vendor ID of the USB device.
            tx_pid (int): Product ID for TX device.
            con_pid (int): Product ID for console device.
            baudrate (int): Communication baud rate.
            timeout (int): Read timeout in seconds.
            TX_test_mode (bool): Enable TX test mode.
            run_async (bool): Enable asynchronous operation.
        """
        # Store parameters in instance variables
        self._async_mode = run_async
        self.txdevice = None
        self.hvcontroller = None
        self.status = LIFUInterfaceStatus.STATUS_SYS_OFF
        self._test_mode = TX_test_mode
        self._owns_hw_pid_env = False

        self._claim_hw_interface_pid()

        self.voltage_table = None
        self.sequence_time = None
        self.duty_cycles = None
        self.voltage_table_selection = voltage_table_selection
        self.sequence_time_selection = sequence_time_selection
        self.duty_cycle_selection = duty_cycle_selection

        # Create a TXDevice instance as part of the interface
        self.txdevice = TxDevice(vid=vid, pid=tx_pid, baudrate=baudrate, timeout=timeout, test_mode=TX_test_mode, module_invert=module_invert)
        
        if ext_power_supply:
            logger.debug("External power supply selected, skipping HVController initialization.")
            self.hvcontroller = None
        else:
            # Create a LIFUHVController instance as part of the interface
            self.hvcontroller = HVController(vid=vid, pid=con_pid, baudrate=baudrate, timeout=timeout, test_mode=HV_test_mode)

        if not self._async_mode:
            if self.txdevice is not None:
                self.txdevice.connect()
            if self.hvcontroller is not None:
                self.hvcontroller.connect()

    # Temporary fix for hardware variations between EVT0 and EVT2
    def _resolve_voltage_chart_evt_version(self, voltage_table: str) -> list[list[int]]:
        if voltage_table is None:
            evt_version = "evt0" if self.hvcontroller.get_version().startswith("v1.1") else "evt2"
        else:
            evt_version = voltage_table.lower()
            if evt_version not in MAX_VOLTAGE_BY_DUTY_CYCLE_AND_SEQUENCE_TIME:
                raise ValueError(f"Invalid voltage_table option '{voltage_table}'. Valid options are: {tuple(MAX_VOLTAGE_BY_DUTY_CYCLE_AND_SEQUENCE_TIME.keys())}")

        return MAX_VOLTAGE_BY_DUTY_CYCLE_AND_SEQUENCE_TIME[evt_version]

    # Restrict sequence time options for users vs QA
    def _resolve_max_sequence_time_set(self, sequence_time: str) -> list[int]:
        if sequence_time is None:
            return REF_MAX_SEQUENCE_TIMES["default"]
        else:
            sequence_time = sequence_time.lower()
            if sequence_time not in REF_MAX_SEQUENCE_TIMES:
                raise ValueError(f"Invalid sequence_time option '{sequence_time}'. Valid options are: {tuple(REF_MAX_SEQUENCE_TIMES.keys())}")
            return REF_MAX_SEQUENCE_TIMES[sequence_time]

    # Restrict duty cycle options for users vs QA
    def _resolve_duty_cycle_set(self, duty_cycle: str) -> list[float]:
        if duty_cycle is None:
            return REF_MAX_DUTY_CYCLES["default"]
        else:
            duty_cycle = duty_cycle.lower()
            if duty_cycle not in REF_MAX_DUTY_CYCLES:
                raise ValueError(f"Invalid duty_cycle option '{duty_cycle}'. Valid options are: {tuple(REF_MAX_DUTY_CYCLES.keys())}")
            return REF_MAX_DUTY_CYCLES[duty_cycle]

    async def start_monitoring(self, interval: int = 1) -> None:
        """Start monitoring for USB device connections."""
        if self.txdevice is not None:
            self.txdevice.start()
        if self.hvcontroller is not None:
            self.hvcontroller.start()
        
    def stop_monitoring(self) -> None:
        """Stop monitoring for USB device connections."""
        if self.txdevice is not None:
            self.txdevice.stop()
        if self.hvcontroller is not None:
            self.hvcontroller.stop()

    def is_device_connected(self) -> tuple:
        """
        Check if the device is currently connected.

        Returns:
            tuple: (tx_connected, hv_connected)
        """
        tx_connected = self.txdevice.is_connected()
        if self.hvcontroller is None:
            hv_connected = False
        else:
            hv_connected = self.hvcontroller.is_connected()
        return tx_connected, hv_connected

    def get_max_voltage(self, solution: Dict) -> float:
        """
        Get the maximum voltage for a given solution.

        Args:
            solution (Dict): The solution to check.

        Returns:
            float: The maximum voltage for the solution.
        """
        
        sequence_duty_cycle = self.get_sequence_duty_cycle(solution)
        sequence_duration = self.get_sequence_duration(solution)

        # Find the index of the duty cycle in the reference list
        duty_cycles_limits = np.array(self.duty_cycles)
        duty_cycle_index = np.where(duty_cycles_limits >= sequence_duty_cycle)[0][0]

        # Find the index of the duration in the reference list
        duration_limits = np.array(self.sequence_time)
        duration_index = np.where(duration_limits >= sequence_duration)[0][0]

        # Return the maximum voltage for the given duty cycle and duration
        return self.voltage_table[duty_cycle_index][duration_index]

    def get_max_voltage_table(self) -> pd.DataFrame:
        """
        Get a table of the maximum voltages for different duty cycles and sequence times.

        Returns:
            pd.DataFrame: A DataFrame containing the maximum voltages.
        """
        data = {
            "Duty Cycle (%)": [f"<={100 * dc:0.1f}%" for dc in self.duty_cycles],
            }
        for i, duration in enumerate(self.sequence_time):
            col_name = f"<={duration // 60} min"
            data[col_name] = [
                self.voltage_table[j][i] for j in range(len(self.duty_cycles))
            ]
        max_voltage =  pd.DataFrame(data).set_index("Duty Cycle (%)")
        max_voltage.Name = "Maximum Voltage (V)"
        max_voltage.Description = "This table shows the maximum voltage for different duty cycles and sequence times."
        return max_voltage

    def check_solution(self, solution: Dict) -> None:
        """Check that the solution is within the configured safety limits.

        Raises:
            LIFUSolutionError: If the solution exceeds any safety limit.
        """

        self.voltage_table = self._resolve_voltage_chart_evt_version(self.voltage_table_selection)
        self.sequence_time = self._resolve_max_sequence_time_set(self.sequence_time_selection)
        self.duty_cycles = self._resolve_duty_cycle_set(self.duty_cycle_selection)
        sequence_duty_cycle = self.get_sequence_duty_cycle(solution)
        duty_cycles_limits = np.array(self.duty_cycles)
        if sequence_duty_cycle > duty_cycles_limits.max():
            raise LIFUSolutionError(f"Sequence duty cycle ({100*sequence_duty_cycle:0.1f} %) exceeds maximum allowed duty cycle ({100*duty_cycles_limits.max():0.1f} %).")
        duty_cycle_index = np.where(duty_cycles_limits >= sequence_duty_cycle)[0][0]

        sequence_duration = self.get_sequence_duration(solution)
        duration_limits = np.array(self.sequence_time)
        if sequence_duration > duration_limits.max():
            raise LIFUSolutionError(f"Sequence duration ({sequence_duration:0.0f} s) exceeds maximum allowed duration ({duration_limits.max()} s).")
        duration_index = np.where(duration_limits >= sequence_duration)[0][0]

        max_voltage = self.voltage_table[duty_cycle_index][duration_index]
        if solution['voltage'] > max_voltage:
            raise LIFUSolutionError(f"Voltage ({solution['voltage']:0.1f}V) exceeds maximum allowed voltage ({max_voltage:0.1f}V) for duty cycle ({100*sequence_duty_cycle:0.1f} <= {100*duty_cycles_limits[duty_cycle_index]}%) and sequence time ({sequence_duration:0.0f} <= {duration_limits[duration_index]}s).")

    def get_sequence_duty_cycle(self, solution: Dict) -> float:
        """
        Get the duty cycle of the sequence in the solution.

        Args:
            solution (Dict): The solution to check.

        Returns:
            float: The duty cycle of the sequence.
        """
        

        if solution['sequence']['pulse_train_interval'] == 0:
            return solution['pulse']['duration'] / solution['sequence']['pulse_interval']
        else:
            return (solution['pulse']['duration'] * solution['sequence']['pulse_count']) / solution['sequence']['pulse_train_interval']

    def get_sequence_duration(self, solution: Dict) -> float:
        """
        Get the duration of the sequence in the solution.

        Args:
            solution (Dict): The solution to check.

        Returns:
            float: The duration of the sequence.
        """
        

        if solution['sequence']['pulse_train_interval'] == 0:
            return solution['sequence']['pulse_interval'] * solution['sequence']['pulse_count'] * solution['sequence']['pulse_train_count']
        else:
            return solution['sequence']['pulse_train_interval'] * solution['sequence']['pulse_train_count']

    def set_module_invert(self, module_invert: bool | List[bool]) -> None:
        if self.txdevice is not None:
            self.txdevice.set_module_invert(module_invert)

    def set_solution(self,
                     solution: Dict,
                     profile_index:int=1,
                     profile_increment:bool=True,
                     trigger_mode: TriggerModeOpts = "sequence",
                     turn_hv_on: bool = False,
                     wait_for_settle: bool = False,
                     _allow_unsafe_solution: bool = False
                     ) -> bool:
        """Load a solution to the device.

        Args:
            solution: The solution to load.
            profile_index: The profile index to load the solution to (defaults to 1).
            profile_increment: Increment the profile index.
            trigger_mode: The trigger mode to use (defaults to "sequence").
            turn_hv_on: If True, turn on HV after loading the solution.
            wait_for_settle: If True, wait for HV to settle after turning on.
            _allow_unsafe_solution: Skip :meth:`check_solution` if True.

        Raises:
            LIFUSolutionError: If the solution fails safety checks (unless
                *_allow_unsafe_solution* is True).
            LIFUError: On any device-communication failure.
            LIFUHVSettleError: If *wait_for_settle* is requested and the HV
                rail does not settle in time.
        """
        if not _allow_unsafe_solution:
            self.check_solution(solution)

        if "transducer" in solution and solution["transducer"] is not None and "module_invert" in solution["transducer"]:
            self.txdevice.set_module_invert(solution["transducer"]["module_invert"])
        else:
            self.txdevice.set_module_invert(False)

        self.set_status(LIFUInterfaceStatus.STATUS_PROGRAMMING)

        if "name" in solution:
            solution_name = f'Solution "{solution["name"]}"'
        else:
            solution_name = "Solution"

        voltage = solution['voltage']
        logger.debug("Loading %s...", solution_name)
        self.txdevice.set_solution(
            pulse=solution['pulse'],
            delays=solution['delays'],
            apodizations=solution['apodizations'],
            sequence=solution['sequence'],
            profile_index=profile_index,
            profile_increment=profile_increment,
            trigger_mode=trigger_mode,
        )
        self.set_status(LIFUInterfaceStatus.STATUS_READY)

        if self.hvcontroller is not None:
            self.hvcontroller.set_voltage(voltage)
            logger.debug("Set HV to %.2f", self.hvcontroller.supply_voltage)
            if turn_hv_on:
                logger.debug("Turn ON HV")
                self.hvcontroller.turn_hv_on()
            if self.hvcontroller.get_hv_status() and wait_for_settle:
                logger.debug("Wait for Settle")
                self.hvcontroller.wait_for_settle(timeout=SETTLE_TIME_HV_ON)
        logger.info("%s loaded successfully.", solution_name)
        return True

    def start_sonication(self, async_mode: bool | None = None, turn_hv_on: bool = True, wait_for_settle: bool = True) -> bool:
        """Start sonication.

        Args:
            async_mode: If not None, override the interface's async-mode setting.
            turn_hv_on: If True, turn on HV before starting.
            wait_for_settle: If True, wait for HV to settle before starting.

        Raises:
            LIFUError: On any device-communication failure.
            LIFUHVSettleError: If the HV rail does not settle in time.
        """
        if self._test_mode:
            return True

        if self.hvcontroller is not None:
            if turn_hv_on:
                logger.debug("Turn ON HV")
                self.hvcontroller.turn_hv_on()
                hv_on = True
            else:
                hv_on = self.hvcontroller.get_hv_status()
            if hv_on:
                if wait_for_settle:
                    self.hvcontroller.wait_for_settle(timeout=SETTLE_TIME_HV_ON)
                else:
                    logger.debug("HV is ON. Skipping settle wait.")
            else:
                logger.warning("HV is OFF")
        else:
            logger.debug("No HV Controller detected, assuming external power supply. Skipping HV checks.")

        self.txdevice.async_mode(async_mode if async_mode is not None else self._async_mode)

        logger.debug("Starting Trigger")
        self.txdevice.start_trigger()
        logger.info("Sonication started successfully.")
        self.set_status(LIFUInterfaceStatus.STATUS_RUNNING)
        return True

    def set_status(self, status: LIFUInterfaceStatus) -> None:
        """
        Set the device status.

        Args:
            status (LIFUInterfaceStatus): The status to set.
        """
        logger.debug("Setting device status to %s", status.name)
        self.status = status

    def get_status(self) -> LIFUInterfaceStatus:
        """
        Query the device status.

        Returns:
            LIFUInterfaceStatus: The device status.
        """
        if self._test_mode:
            return LIFUInterfaceStatus.STATUS_READY

        return self.status

    def is_running(self) -> bool:
        """
        Check if the device is currently running a sonication.

        Returns:
            bool: True if the device is running, False otherwise.
        """
        trigger_json = self.txdevice.get_trigger_json()
        trigger_status = trigger_json.get("TriggerStatus", "NOSTATUS").upper()
        if trigger_status == "RUNNING":
            return True
        elif trigger_status == "STOPPED":
            return False
        elif trigger_status == "NOSTATUS":
            raise LIFUNoTriggerStatusError("Device failed to provide valid trigger status.")
        else:
            raise LIFUNoTriggerStatusError(f"Unexpected trigger status '{trigger_status}' received from device.")

    def stop_sonication(self, turn_hv_off: bool = True, wait_for_settle: bool = False) -> bool:
        """Stop sonication.

        Args:
            turn_hv_off: If True, turn off HV after stopping the trigger.
            wait_for_settle: If True, wait for HV to settle after turning off.

        Raises:
            LIFUError: On any device-communication failure.
            LIFUHVSettleError: If the HV rail does not settle in time.
        """
        if self._test_mode:
            return True

        logger.debug("Stopping trigger")
        self.txdevice.stop_trigger()

        if self.hvcontroller is not None:
            if turn_hv_off:
                logger.debug("Turn OFF HV")
                self.hvcontroller.turn_hv_off()
                if wait_for_settle:
                    logger.debug("Waiting for HV to settle after turning OFF")
                    self.hvcontroller.wait_for_settle(timeout=SETTLE_TIME_HV_OFF)
            else:
                if self.hvcontroller.get_hv_status():
                    logger.debug("HV is ON but turn_hv_off is False, HV will not be turned OFF.")
                elif wait_for_settle:
                    logger.debug("HV turned OFF, waiting for settle")
                    self.hvcontroller.wait_for_settle(timeout=SETTLE_TIME_HV_OFF)
                else:
                    logger.debug("HV is OFF and wait_for_settle is False, skipping settle wait.")
        else:
            logger.debug("Using external power supply, HV will not be turned OFF.")

        self.txdevice.async_mode(False)

        logger.info("Sonication stopped successfully.")
        self.set_status(LIFUInterfaceStatus.STATUS_FINISHED)
        return True

    def close(self):
        self.stop_monitoring()
        if self.txdevice:
            self.txdevice.stop()
            self.txdevice.close()
        if self.hvcontroller:
            self.hvcontroller.stop()
            self.hvcontroller.close()
        self._release_hw_interface_pid()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _claim_hw_interface_pid(self) -> None:
        """Register this process as the owner of the LIFU hardware interface.

        Reads the cross-process ``OPENLIFU_HW_INTERFACE_PID`` slot (the User
        environment variable on Windows, a PID file under the user's home
        directory elsewhere). If it contains a live PID owned by a different
        process, raises :class:`LIFUHardwareInUseError`. Otherwise overwrites
        it with the current process's PID.
        """
        existing = _hw_pid_lock_read()
        my_pid = os.getpid()
        if existing:
            try:
                existing_pid = int(existing)
            except ValueError:
                existing_pid = 0
            if existing_pid > 0 and existing_pid != my_pid and _pid_alive(existing_pid):
                raise LIFUHardwareInUseError(pid=existing_pid)
        _hw_pid_lock_write(str(my_pid))
        self._owns_hw_pid_env = True

    def _release_hw_interface_pid(self) -> None:
        """Clear the hardware-interface PID slot if this process owns it."""
        if not getattr(self, "_owns_hw_pid_env", False):
            return
        current = _hw_pid_lock_read()
        if current == str(os.getpid()):
            _hw_pid_lock_write("")
        self._owns_hw_pid_env = False

    @staticmethod
    def get_sdk_version() -> str:
        return importlib.metadata.version("openlifu-sdk")
