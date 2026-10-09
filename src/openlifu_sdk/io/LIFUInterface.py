from __future__ import annotations

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
from openlifu_sdk.io.LIFUHVController import HVController, LIFUHVController
from openlifu_sdk.io.LIFUTXDevice import LIFUTxDevice, TriggerModeOpts, TxDevice

# Maximum-voltage lookup tables keyed by hardware/test profile.
#
# Each entry is a dict that fully describes its own anchor points and
# voltage matrix:
#
#   - ``duty_cycles``     : list[float], one entry per row of ``voltages``.
#                           Treated as "max duty cycle for this row" — the
#                           lookup picks the first row whose limit >= the
#                           sequence's duty cycle.
#   - ``sequence_times``  : list[float] in seconds, one entry per column of
#                           ``voltages``. Same semantics as ``duty_cycles``
#                           but applied to total sequence duration.
#   - ``voltages``        : 2D list of ints (rows × cols), giving the max
#                           voltage allowed for that (duty_cycle, sequence_time)
#                           cell.
#
# Different profiles may use entirely different anchor points (e.g. ``dvt``
# is denser in duty cycle than ``evt2``/``evt0``); the lookup uses each
# entry's own anchors, so no global anchor list is required.
MAX_VOLTAGE_BY_DUTY_CYCLE_AND_SEQUENCE_TIME = {
    "evt2": {
        "duty_cycles": [0.05, 0.1, 0.2, 0.3, 0.4, 0.5],
        "sequence_times": [2*60, 5*60, 10*60],
        "voltages": [
            [45, 45, 45], # 0.05
            [40, 40, 40], # 0.1
            [40, 40, 35], # 0.2
            [40, 35, 30], # 0.3
            [35, 30, 25], # 0.4
            [30, 25, 20], # 0.5
        ],
    },
    "evt0": {
        "duty_cycles": [0.05, 0.1, 0.2, 0.3, 0.4, 0.5],
        "sequence_times": [2*60, 5*60, 10*60],
        "voltages": [
            [65, 65, 65], # 0.05
            [65, 65, 50], # 0.1
            [50, 40, 35], # 0.2
            [45, 35, 30], # 0.3
            [35, 30, 25], # 0.4
            [30, 25, 20], # 0.5
        ],
    },
    "dvt": {
        "duty_cycles": [0.05, 0.10, 0.15, 0.18, 0.22, 0.28, 0.35, 0.40, 0.45, 0.50],
        "sequence_times": [10*60],
        "voltages": [
            [65], # 0.05
            [60], # 0.10
            [55], # 0.15
            [50], # 0.18
            [45], # 0.22
            [40], # 0.28
            [35], # 0.35
            [30], # 0.40
            [25], # 0.45
            [20], # 0.50
        ],
    },
    # QA / stress-test profiles. Single-cell tables that effectively disable
    # the duty-cycle / duration ramp-down: any sequence at or below the listed
    # caps is allowed at the listed voltage.
    "stress_test_evt0": {
        "duty_cycles": [0.5],
        "sequence_times": [60*60],
        "voltages": [[65]],
    },
    "stress_test_evt2": {
        "duty_cycles": [0.5],
        "sequence_times": [60*60],
        "voltages": [[45]],
    },
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

class DeviceInterface:
    """FDA device interface: connection, preset load, and sonication control.

    This is the surface FDA-mode applications program against. It loads
    presets baked into device flash (:meth:`load_preset`) and starts / stops
    them; it has no way to program an arbitrary solution or HV setpoint. Its
    components are the FDA :class:`TxDevice` / :class:`HVController`, which
    neither define nor whitelist the RUO commands. It connects synchronously
    and has no async / USB-monitoring mode.
    :class:`LIFUInterface` extends it with those research (RUO) endpoints.
    """
    hvcontroller: HVController = None
    txdevice: TxDevice = None

    def __init__(self,
                 vid: int = OW_VID,
                 tx_pid: int = OW_TRANSMITTER_PID,
                 con_pid: int = OW_CONSOLE_PID,
                 baudrate: int = 921600,
                 timeout: float = DEFAULT_TIMEOUT,
                 TX_test_mode: bool = False,
                 HV_test_mode: bool = False) -> None:
        """
        Initialize the interface, create the TX and HV components and connect
        them.

        Args:
            vid (int): Vendor ID of the USB device.
            tx_pid (int): Product ID for TX device.
            con_pid (int): Product ID for console device.
            baudrate (int): Communication baud rate.
            timeout (int): Read timeout in seconds.
            TX_test_mode (bool): Enable TX test mode.
            HV_test_mode (bool): Enable HV test mode.
        """
        # Store parameters in instance variables
        self.txdevice = None
        self.hvcontroller = None
        self.status = LIFUInterfaceStatus.STATUS_SYS_OFF
        self._test_mode = TX_test_mode
        self._owns_hw_pid_env = False

        self._claim_hw_interface_pid()

        self.txdevice, self.hvcontroller = self._create_devices(
            vid, tx_pid, con_pid, baudrate, timeout, TX_test_mode, HV_test_mode)
        self._connect_devices()

    def _create_devices(self, vid: int, tx_pid: int, con_pid: int, baudrate: int,
                        timeout: float, TX_test_mode: bool,
                        HV_test_mode: bool) -> tuple[TxDevice, Optional[HVController]]:
        """Construct the TX and HV components (before they connect)."""
        txdevice = TxDevice(vid=vid, pid=tx_pid, baudrate=baudrate, timeout=timeout, test_mode=TX_test_mode)
        hvcontroller = HVController(vid=vid, pid=con_pid, baudrate=baudrate, timeout=timeout, test_mode=HV_test_mode)
        return txdevice, hvcontroller

    def _connect_devices(self) -> None:
        """Open the TX and HV serial links."""
        if self.txdevice is not None:
            self.txdevice.connect()
        if self.hvcontroller is not None:
            self.hvcontroller.connect()

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

    def load_preset(self,
                    preset_index: int,
                    settings_crc: int,
                    regs_crc: int,
                    frequency_hz: float,
                    duration_index: int = 0,
                    turn_hv_on: bool = False,
                    wait_for_settle: bool = False) -> bool:
        """Load an FDA preset across TX and HV in one operation."""
        self.set_status(LIFUInterfaceStatus.STATUS_PROGRAMMING)
        self.txdevice.load_preset(
            preset_index,
            regs_crc,
            duration_index=duration_index,
        )

        if self.hvcontroller is not None:
            if not self.txdevice.module_user_configs:
                self.txdevice.refresh_metadata()
            device_sensitivity = self.txdevice.get_sensitivity_for_frequency(frequency_hz)
            if device_sensitivity is None:
                raise LIFUSolutionError(
                    f"TX device sensitivity is unavailable at {frequency_hz:.3f} Hz; cannot select FDA preset on HV."
                )

            self.hvcontroller.select_preset(
                preset_index,
                settings_crc,
                device_sensitivity,
                frequency_hz,
            )
            logger.debug(
                "Selected HV preset %d using TX sensitivity %.6g at %.3f Hz",
                preset_index,
                device_sensitivity,
                frequency_hz,
            )
            if turn_hv_on:
                logger.debug("Turn ON HV")
                self.hvcontroller.turn_hv_on()
            if self.hvcontroller.get_hv_status() and wait_for_settle:
                logger.debug("Wait for Settle")
                self.hvcontroller.wait_for_settle(timeout=SETTLE_TIME_HV_ON)

        self.set_status(LIFUInterfaceStatus.STATUS_READY)
        logger.info("Preset %d loaded successfully.", preset_index)
        return True

    def start_sonication(self, turn_hv_on: bool = True, wait_for_settle: bool = True) -> bool:
        """Start sonication.

        Args:
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

        logger.info("Sonication stopped successfully.")
        self.set_status(LIFUInterfaceStatus.STATUS_FINISHED)
        return True

    def close(self):
        if self.txdevice:
            self.txdevice.close()
        if self.hvcontroller:
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
        # Report the version of the module actually imported (baked from the
        # git tag at build time), not whichever dist-info importlib finds
        # first -- the two can disagree when a stale install lingers on the
        # path. Runtime import avoids a circular import at module load.
        import openlifu_sdk

        return getattr(
            openlifu_sdk, "__version__", None
        ) or importlib.metadata.version("openlifu-sdk")


class LIFUInterface(DeviceInterface):
    """Research (RUO) interface: :class:`DeviceInterface` plus endpoints that
    program arbitrary solutions and HV setpoints, gated by the host-side
    duty-cycle / duration voltage tables. Builds the RUO components
    (:class:`LIFUTxDevice` / :class:`LIFUHVController`) and adds the async /
    USB-monitoring mode.
    """
    hvcontroller: LIFUHVController = None
    txdevice: LIFUTxDevice = None

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
                 voltage_table_selection: Optional[str] = None) -> None:
        """
        Initialize the LIFUInterface with given parameters and store them in the class.

        Args:
            vid (int): Vendor ID of the USB device.
            tx_pid (int): Product ID for TX device.
            con_pid (int): Product ID for console device.
            baudrate (int): Communication baud rate.
            timeout (int): Read timeout in seconds.
            TX_test_mode (bool): Enable TX test mode.
            HV_test_mode (bool): Enable HV test mode.
            run_async (bool): Enable asynchronous operation.
            ext_power_supply (bool): Use an external HV supply; no HV controller is created.
            module_invert (bool | List[bool]): Initial TX module invert configuration.
            voltage_table_selection (str | None): Voltage-table profile used by
                :meth:`check_solution`; inferred from the HV version if None.
        """
        self.voltage_table = None
        self.sequence_time = None
        self.duty_cycles = None
        self.voltage_table_selection = voltage_table_selection
        self._ext_power_supply = ext_power_supply
        self._module_invert = module_invert
        self._async_mode = run_async
        super().__init__(vid=vid, tx_pid=tx_pid, con_pid=con_pid, baudrate=baudrate,
                         timeout=timeout, TX_test_mode=TX_test_mode,
                         HV_test_mode=HV_test_mode)

    def _create_devices(self, vid: int, tx_pid: int, con_pid: int, baudrate: int,
                        timeout: float, TX_test_mode: bool,
                        HV_test_mode: bool) -> tuple[LIFUTxDevice, Optional[LIFUHVController]]:
        txdevice = LIFUTxDevice(vid=vid, pid=tx_pid, baudrate=baudrate, timeout=timeout, test_mode=TX_test_mode, module_invert=self._module_invert)
        if self._ext_power_supply:
            logger.debug("External power supply selected, skipping HVController initialization.")
            return txdevice, None
        hvcontroller = LIFUHVController(vid=vid, pid=con_pid, baudrate=baudrate, timeout=timeout, test_mode=HV_test_mode)
        return txdevice, hvcontroller

    def _connect_devices(self) -> None:
        # In async mode the monitor threads started by start_monitoring() open the ports.
        if not self._async_mode:
            super()._connect_devices()

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

    def start_sonication(self, async_mode: bool | None = None, turn_hv_on: bool = True, wait_for_settle: bool = True) -> bool:
        """Set the TX async mode, then start sonication (see
        :meth:`DeviceInterface.start_sonication`).

        Args:
            async_mode: If not None, override the interface's async-mode setting.
        """
        if not self._test_mode:
            self.txdevice.async_mode(async_mode if async_mode is not None else self._async_mode)
        return super().start_sonication(turn_hv_on=turn_hv_on, wait_for_settle=wait_for_settle)

    def stop_sonication(self, turn_hv_off: bool = True, wait_for_settle: bool = False) -> bool:
        """Stop sonication (see :meth:`DeviceInterface.stop_sonication`), then
        clear the TX async mode."""
        super().stop_sonication(turn_hv_off=turn_hv_off, wait_for_settle=wait_for_settle)
        if not self._test_mode:
            self.txdevice.async_mode(False)
        return True

    # Temporary fix for hardware variations between EVT0 and EVT2
    def _resolve_voltage_chart(self, voltage_table: Optional[str]) -> dict:
        """Return the voltage-table entry (``duty_cycles`` / ``sequence_times`` / ``voltages``)
        for the requested profile.

        If *voltage_table* is ``None``, the profile is inferred from the connected
        HV controller's reported version.
        """
        if voltage_table is None:
            evt_version = "evt0" if self.hvcontroller.get_version().startswith("v1.1") else "dvt"
        else:
            evt_version = voltage_table.lower()
            if evt_version not in MAX_VOLTAGE_BY_DUTY_CYCLE_AND_SEQUENCE_TIME:
                raise ValueError(f"Invalid voltage_table option '{voltage_table}'. Valid options are: {tuple(MAX_VOLTAGE_BY_DUTY_CYCLE_AND_SEQUENCE_TIME.keys())}")
        return MAX_VOLTAGE_BY_DUTY_CYCLE_AND_SEQUENCE_TIME[evt_version]

    def _load_voltage_table(self) -> None:
        """Populate ``self.voltage_table`` / ``self.duty_cycles`` / ``self.sequence_time``
        from the currently selected profile."""
        entry = self._resolve_voltage_chart(self.voltage_table_selection)
        self.duty_cycles = entry["duty_cycles"]
        self.sequence_time = entry["sequence_times"]
        self.voltage_table = entry["voltages"]

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
        self._load_voltage_table()
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
            execution_order=solution.get('execution_order'),
            pulse_profile_map=solution.get('pulse_profile_map'),
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
