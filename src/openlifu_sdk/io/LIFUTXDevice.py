from __future__ import annotations

import json
import logging
import re
import struct
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Dict, List, Literal, Optional

import numpy as np

from openlifu_sdk.io.component import OWComponent, register_command_packet_types
from openlifu_sdk.util.annotations import OpenLIFUFieldData
from openlifu_sdk.util.units import getunitconversion
from openlifu_sdk.util.hwid import format_hwid

DEFAULT_NUM_TRANSMITTERS = 2
TRANSMITTERS_PER_MODULE = 2
ADDRESS_GLOBAL_MODE = 0x0
ADDRESS_STANDBY = 0x1
ADDRESS_DYNPWR_2 = 0x6
ADDRESS_LDO_PWR_1 = 0xB
ADDRESS_TRSW_TURNOFF = 0xC
ADDRESS_DYNPWR_1 = 0xF
ADDRESS_LDO_PWR_2 = 0x14
ADDRESS_TRSW_TURNON = 0x15
ADDRESS_DELAY_SEL = 0x16
ADDRESS_PATTERN_MODE = 0x18
ADDRESS_PATTERN_REPEAT = 0x19
ADDRESS_PATTERN_SEL_G2 = 0x1E
ADDRESS_PATTERN_SEL_G1 = 0x1F
ADDRESS_TRSW = 0x1A
ADDRESS_APODIZATION = 0x1B
ADDRESS_GLOBAL_CONTROL = 0x00
GLOBAL_CONTROL_LOAD_PROFILE = 0x08  # Self-clearing LOAD_PROF bit in register 0x00.
ADDRESSES_GLOBAL = [ADDRESS_GLOBAL_MODE,
                    ADDRESS_STANDBY,
                    ADDRESS_DYNPWR_2,
                    ADDRESS_LDO_PWR_1,
                    ADDRESS_TRSW_TURNOFF,
                    ADDRESS_DYNPWR_1,
                    ADDRESS_LDO_PWR_2,
                    ADDRESS_TRSW_TURNON,
                    ADDRESS_DELAY_SEL,
                    ADDRESS_PATTERN_MODE,
                    ADDRESS_PATTERN_REPEAT,
                    ADDRESS_PATTERN_SEL_G1,
                    ADDRESS_PATTERN_SEL_G2,
                    ADDRESS_TRSW,
                    ADDRESS_APODIZATION]
ADDRESSES_DELAY_DATA = list(range(0x20, 0x11F+1))
ADDRESSES_PATTERN_DATA = list(range(0x120, 0x19F+1))
ADDRESSES = ADDRESSES_GLOBAL + ADDRESSES_DELAY_DATA + ADDRESSES_PATTERN_DATA
NUM_CHANNELS = 32
MAX_REGISTER = 0x19F
REGISTER_BYTES = 4
REGISTER_WIDTH = REGISTER_BYTES*8
DELAY_ORDER = [[32, 30],
               [28, 26],
               [24, 22],
               [20, 18],
               [31, 29],
               [27, 25],
               [23, 21],
               [19, 17],
               [16, 14],
               [12, 10],
               [8, 6],
               [4, 2],
               [15, 13],
               [11, 9],
               [7, 5],
               [3, 1]]
DELAY_ORDER_REVERSED = [[33 - c for c in row] for row in DELAY_ORDER]
DELAY_CHANNEL_MAP = {}
for row, channels in enumerate(DELAY_ORDER_REVERSED):
    for i, channel in enumerate(channels):
        DELAY_CHANNEL_MAP[channel] = {'row': row, 'lsb': 16*(1-i)}
DELAY_PROFILE_OFFSET = 16
VALID_PULSE_PROFILES = list(range(1, 17))
VALID_DELAY_PROFILES = list(range(1, 17))
DELAY_WIDTH = 13
APODIZATION_CHANNEL_ORDER = [17, 19, 21, 23, 25, 27, 29, 31, 18, 20, 22, 24, 26, 28, 30, 32, 1, 3, 5, 7, 9, 11, 13, 15, 2, 4, 6, 8, 10, 12, 14, 16]
APODIZATION_CHANNEL_ORDER_REVERSED = [33 - c for c in APODIZATION_CHANNEL_ORDER]
DEFAULT_PATTERN_DUTY_CYCLE = 0.66
# Minimum inter-pulse dead time (seconds) required for SPI profile switching.
# Must match MIN_PROFILE_SWITCH_US in firmware trigger.h (1000 µs).
# Measured: ~460 µs at SPI prescaler /4 (12 MHz). Using 1 ms for safety margin.
MIN_PROFILE_SWITCH_INTERVAL = 1000e-6
PATTERN_PROFILE_OFFSET = 4
NUM_PATTERN_PROFILES = 32
VALID_PATTERN_PROFILES = list(range(1, NUM_PATTERN_PROFILES+1))
PATTERN_PROFILE_SELECT_MASK = 0x3F
BF_PROF_SEL_G1_SHIFT = 28  # Bits 28-31 for G1 delay profile selector
BF_PROF_SEL_G2_SHIFT = 12  # Bits 12-15 for G2 delay profile selector
BF_PROF_SEL_FIELD_MASK = 0x0F  # 4-bit profile field (0-15 for profiles 1-16)
MAX_PATTERN_PERIODS = 16
PATTERN_PERIOD_ORDER = [[1, 2, 3, 4],
                 [5, 6, 7, 8],
                 [9, 10, 11, 12],
                 [13, 14, 15, 16]]
PATTERN_LENGTH_WIDTH = 5
MAX_PATTERN_PERIOD_LENGTH = 30
PATTERN_LEVEL_WIDTH = 3
PATTERN_MAP = {}
for row, periods in enumerate(PATTERN_PERIOD_ORDER):
    for i, period in enumerate(periods):
        PATTERN_MAP[period] = {'row': row, 'lsb_lvl': i*(PATTERN_LEVEL_WIDTH+PATTERN_LENGTH_WIDTH), 'lsb_period': i*(PATTERN_LENGTH_WIDTH+PATTERN_LEVEL_WIDTH)+PATTERN_LEVEL_WIDTH}
MAX_REPEAT = 2**5-1
MAX_ELASTIC_REPEAT = 2**16-1
DEFAULT_TAIL_COUNT = 29
DEFAULT_CLK_FREQ = 10e6
ELASTIC_MODE_PULSE_LENGTH_ADJUST = 125e-6
ProfileOpts = Literal['active', 'configured', 'all']
TriggerModeOpts = Literal['sequence', 'continuous','single']
DEFAULT_PULSE_WIDTH_US = 20
TEMPERATURE_DATA_LENGTH = 4

from openlifu_sdk.io.LIFUConfig import (
    CONTROLLER_COMMANDS,
    DEFAULT_TIMEOUT,
    GLOBAL_COMMANDS,
    LIFU_ERR_BAD_PAYLOAD_FORMAT,
    LIFU_ERR_BAD_PAYLOAD_LENGTH,
    LIFU_ERR_EMPTY_RESPONSE,
    LIFU_ERR_MODULE_COUNT_MISMATCH,
    OW_CMD,
    OW_CMD_ASYNC,
    OW_CMD_DFU,
    OW_CMD_ECHO,
    OW_CMD_GET_AMBIENT,
    OW_CTRL_GET_MODULE_COUNT,
    OW_CTRL_GET_MODULE_MODE,
    OW_CTRL_ENUMERATE,
    NODE_MODE_APP,
    NODE_MODE_BOOTLOADER,
    NODE_MODE_UNKNOWN,
    OW_CTRL_GET_PATTERN_PROFILE,
    OW_CTRL_SET_PATTERN_PROFILE,
    OW_CTRL_SET_DELAY_PROFILE,
    OW_CTRL_GET_DELAY_PROFILE,
    OW_CTRL_SET_PROFILE_CYCLE,
    OW_CMD_GET_TEMP,
    OW_CMD_HWID,
    OW_CMD_PING,
    OW_CMD_RESET,
    OW_CMD_TOGGLE_LED,
    OW_CMD_USR_CFG,
    OW_CMD_VERSION,
    OW_CONTROLLER,
    OW_CTRL_GET_SWTRIG,
    OW_CTRL_SET_SWTRIG,
    OW_CTRL_START_SWTRIG,
    OW_CTRL_STOP_SWTRIG,
    OW_ERROR,
    OW_TRANSMITTER_PID,
    OW_TX7332,
    OW_TX7332_DEMO,
    OW_TX7332_DEVICE_COUNT,
    OW_TX7332_ENUM,
    OW_TX7332_RREG,
    OW_TX7332_RBLOCK,
    OW_TX7332_VWBLOCK,
    OW_TX7332_VWREG,
    OW_TX7332_WBLOCK,
    OW_TX7332_WREG,
    OW_VID,
    TRIGGER_MODE_CONTINUOUS,
    TRIGGER_MODE_SEQUENCE,
    TRIGGER_MODE_SINGLE,
    HW_ID_DATA_LENGTH,
    TX7332_COMMANDS
)
from openlifu_sdk.io.exceptions import LIFUError, LIFUProtocolError

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

class TxDevice(OWComponent):
    def __init__(self, vid: int = OW_VID, pid: int = OW_TRANSMITTER_PID,
                 baudrate: int = 921600, timeout: float = DEFAULT_TIMEOUT, test_mode: bool = False, module_invert: bool | list[bool] = False):
        """
        Initialize the TxDevice.

        Args:
            vid (int): USB Vendor ID of the device.
            pid (int): USB Product ID of the device.
            baudrate (int): Baud rate for UART communication.
            timeout (float): Timeout for UART operations in seconds.
            test_mode (bool): If True, simulates device responses without actual hardware communication.
            module_invert (bool | list[bool]): If True or list of bools, inverts the module addressing scheme.
        """
        super().__init__(
            vid, pid,
            supported_commands=GLOBAL_COMMANDS | TX7332_COMMANDS | CONTROLLER_COMMANDS,
            baudrate=baudrate, timeout=timeout, desc="TX",
        )
        
        register_command_packet_types(TX7332_COMMANDS, OW_TX7332)
        register_command_packet_types(CONTROLLER_COMMANDS, OW_CONTROLLER)

        self._tx_instances = []
        self.tx_registers = None
        self._test_mode = test_mode
        self.module_invert = module_invert

    def __parse_ti_cfg_file(self, file_path: str) -> list[tuple[str, int, int]]:
        """Parses the given configuration file and extracts all register groups, addresses, and values."""
        parsed_data = []
        pattern = re.compile(r"([\w\d\-]+)\|0x([0-9A-Fa-f]+)\t0x([0-9A-Fa-f]+)")

        with open(file_path) as file:
            for line in file:
                match = pattern.match(line.strip())
                if match:
                    group_name = match.group(1)  # Capture register group name
                    register_address = int(match.group(2), 16)  # Convert hex address to integer
                    register_value = int(match.group(3), 16)  # Convert hex value to integer
                    parsed_data.append((group_name, register_address, register_value))

        return parsed_data

    def get_temperature(self, module:int=0) -> float:
        """Retrieve the core temperature reading from the TX device.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError: If the payload length is invalid.
        """
        r = self.send_checked(packet_type=OW_CONTROLLER, command=OW_CMD_GET_TEMP,
                              addr=module, op="get_temperature")
        if r.data_len < TEMPERATURE_DATA_LENGTH:
            raise LIFUProtocolError(
                f"TX: temperature payload too short ({r.data_len} < {TEMPERATURE_DATA_LENGTH})",
                code=LIFU_ERR_BAD_PAYLOAD_LENGTH,
            )
        return round(struct.unpack('<f', r.data[:TEMPERATURE_DATA_LENGTH])[0], 2)

    def get_ambient_temperature(self, module:int=0) -> float:
        """Retrieve the ambient temperature reading from the TX device.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError: If the payload length is invalid.
        """
        r = self.send_checked(packet_type=OW_CONTROLLER, command=OW_CMD_GET_AMBIENT,
                              addr=module, op="get_ambient_temperature")
        if r.data_len < TEMPERATURE_DATA_LENGTH:
            raise LIFUProtocolError(
                f"TX: ambient temperature payload too short ({r.data_len} < {TEMPERATURE_DATA_LENGTH})",
                code=LIFU_ERR_BAD_PAYLOAD_LENGTH,
            )
        return round(struct.unpack('<f', r.data[:TEMPERATURE_DATA_LENGTH])[0], 2)

    def set_trigger(self,
                    pulse_interval: float,
                    pulse_count: int = 1,
                    pulse_width: int = DEFAULT_PULSE_WIDTH_US,
                    pulse_train_interval: float = 0.0,
                    pulse_train_count: int = 1,
                    trigger_mode: TriggerModeOpts = "sequence",
                    profile_index: int = 0,
                    profile_increment: bool = True) -> dict:
        """
        Set the trigger configuration on the TX device.

        Args:
            pulse_interval (float): The time interval between pulses in seconds.
            pulse_count (int): The number of pulses to generate.
            pulse_width (int): The pulse width in microseconds.
            pulse_train_interval (float): The time interval between pulse trains in seconds.
            pulse_train_count (int): The number of pulse trains to generate.
            mode (TriggerModeOpts): The trigger mode to use.
            profile_index (int): The pulse profile to use.
            profile_increment (bool): Whether to increment the pulse profile.
        """

        trigger_mode = trigger_mode.lower()
        if trigger_mode == "sequence":
            trigger_mode_int = TRIGGER_MODE_SEQUENCE
        elif trigger_mode == "continuous":
            trigger_mode_int = TRIGGER_MODE_CONTINUOUS
        elif trigger_mode == "single":
            trigger_mode_int = TRIGGER_MODE_SINGLE
        else:
            raise ValueError("Invalid trigger mode")

        if pulse_train_count <= 1:
            # Only one train will ever fire, so the inter-train spacing is
            # functionally meaningless -- but the firmware still validates
            # train_interval >= period * pulse_count on every start. Set it to
            # the full train duration plus margin (same as the interval==0
            # case) so single-train configs pass regardless of pulse_count.
            pulse_train_interval = pulse_interval * pulse_count + MIN_PROFILE_SWITCH_INTERVAL
        elif pulse_train_interval > 0 and (pulse_train_interval < pulse_interval * pulse_count):
            raise ValueError("Pulse train interval cannot be less than pulse interval * pulse count")
        elif pulse_train_interval == 0:
            # Back-to-back trains: pad the train duration with a safety margin.
            # If the interval exactly equals the train duration, the firmware
            # TIM2 (train) expiry races the final TIM1 pulse and the trigger
            # output becomes unstable.
            pulse_train_interval = pulse_interval * pulse_count + MIN_PROFILE_SWITCH_INTERVAL

        # Firmware <= 2.0.3 compatibility shim.
        #
        # FW <= 2.0.3 rejects start_trigger with TRIGGER_STATUS_ERROR
        # (surfaced as a LIFUDeviceError OW_ERROR NAK with no sub-code)
        # whenever:
        #     TriggerPulseTrainInterval > 0 AND
        #     TriggerPulseTrainInterval <= triggerPeriodUsec
        # where ``triggerPeriodUsec = 1_000_000 / TriggerFrequencyHz`` (the
        # per-pulse period) and ``TriggerFrequencyHz`` is parsed as uint32
        # (strtol, base 10) so fractional Hz get truncated. FW 2.0.7+ uses
        # strict ``<`` and a pulse_count factor, so it always passes.
        #
        # When the train interval lands at or within rounding of the
        # firmware per-pulse period we lengthen it to
        # ``1 / (int(1/pulse_train_interval) - 1)`` -- effectively rounding
        # the implied train frequency down by 1 Hz, which lengthens the
        # interval just enough to clear the firmware integer period without
        # changing pulse_interval (the user-meaningful sonication frequency).
        train_us = int(round(pulse_train_interval * 1_000_000))
        if train_us > 0 and pulse_interval > 0:
            freq_int = int(1.0 / pulse_interval)
            period_us = 1_000_000 // freq_int if freq_int > 0 else 0
            if train_us <= period_us:
                train_freq_int = int(1.0 / pulse_train_interval)
                if train_freq_int > 1:
                    new_train_s = 1.0 / (train_freq_int - 1)
                else:
                    new_train_s = pulse_train_interval + 1e-6
                logger.debug(
                    "FW <=2.0.3 compat: bumping pulse_train_interval from "
                    "%s s (%s us) to %s s (%s us) so it exceeds the firmware "
                    "per-pulse period (%s us).",
                    pulse_train_interval, train_us,
                    new_train_s, int(round(new_train_s * 1_000_000)),
                    period_us,
                )
                pulse_train_interval = new_train_s

        logger.info(f"Setting trigger with parameters: "
                        f"pulse_interval={pulse_interval}, "
                        f"pulse_count={pulse_count}, "
                        f"pulse_width={pulse_width}, "
                        f"pulse_train_interval={pulse_train_interval}, "
                        f"pulse_train_count={pulse_train_count}, "
                        f"trigger_mode={trigger_mode}")

        trigger_json = {
            "TriggerFrequencyHz": 1/pulse_interval,
            "TriggerPulseCount": pulse_count,
            "TriggerPulseWidthUsec": pulse_width,
            "TriggerPulseTrainInterval": pulse_train_interval * 1000000,
            "TriggerPulseTrainCount": pulse_train_count,
            "TriggerMode": trigger_mode_int,
            "ProfileIndex": 0,
            "ProfileIncrement": 0
        }
        return self.set_trigger_json(data=trigger_json)

    def set_trigger_json(self, data) -> dict:
        """Set the trigger configuration on the TX device from a dict.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError: If the response payload is empty or malformed.
        """
        payload = json.dumps(data).encode("utf-8")
        r = self.send_checked(packet_type=OW_CONTROLLER, command=OW_CTRL_SET_SWTRIG,
                              addr=0, data=payload, op="set_trigger_json")
        if r.data_len == 0:
            raise LIFUProtocolError(
                "TX: set_trigger_json returned empty payload",
                code=LIFU_ERR_EMPTY_RESPONSE,
            )
        try:
            return json.loads(r.data[:r.data_len].decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise LIFUProtocolError(
                f"TX: set_trigger_json decode error: {exc}",
                code=LIFU_ERR_BAD_PAYLOAD_FORMAT,
            ) from exc

    def get_trigger_json(self) -> dict:
        """Read the current trigger configuration as a raw JSON dict.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError: If the response payload is empty or malformed.
        """
        r = self.send_checked(packet_type=OW_CONTROLLER, command=OW_CTRL_GET_SWTRIG,
                              addr=0, op="get_trigger_json")
        if r.data_len == 0:
            raise LIFUProtocolError(
                "TX: get_trigger_json returned empty payload",
                code=LIFU_ERR_EMPTY_RESPONSE,
            )
        try:
            return json.loads(r.data[:r.data_len].decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise LIFUProtocolError(
                f"TX: get_trigger_json decode error: {exc}",
                code=LIFU_ERR_BAD_PAYLOAD_FORMAT,
            ) from exc

    def get_trigger(self) -> dict:
        """Retrieve the current trigger configuration as a canonical dict.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError.
        """
        trigger_json = self.get_trigger_json()
        if trigger_json["TriggerMode"] == TRIGGER_MODE_SEQUENCE:
            mode = "sequence"
        elif trigger_json["TriggerMode"] == TRIGGER_MODE_CONTINUOUS:
            mode = "continuous"
        elif trigger_json["TriggerMode"] == TRIGGER_MODE_SINGLE:
            mode = "single"
        else:
            mode = "unknown"
        return {
            "pulse_interval": 1 / trigger_json["TriggerFrequencyHz"],
            "pulse_count": trigger_json["TriggerPulseCount"],
            "pulse_width": trigger_json["TriggerPulseWidthUsec"],
            "pulse_train_interval": trigger_json["TriggerPulseTrainInterval"],
            "pulse_train_count": trigger_json["TriggerPulseTrainCount"],
            "mode": mode,
            "profile_index": trigger_json["ProfileIndex"],
            "profile_increment": bool(trigger_json["ProfileIncrement"]),
        }

    def set_pattern_profile(self, profile: int, identifier: int | None = None) -> bool:
        """Set the active TX pattern profile via MCU controller command.

        Routes profile switching through firmware (OW_CTRL_SET_PATTERN_PROFILE)
        so the MCU can keep its profile bookkeeping (including apodization
        handling) consistent, instead of direct host register writes.

        Args:
            profile: Pattern profile to activate (1-16).
            identifier: TX chip index, or None to apply to every chip.

        Raises:
            ValueError: If profile is out of range.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError:
                On device-communication failure.
        """
        if profile not in VALID_PULSE_PROFILES:
            raise ValueError(f"Invalid profile {profile}. Expected 1-16.")

        payload = struct.pack('<B', profile)
        for tx_id in self._resolve_tx_ids(identifier):
            self.send_checked(
                packet_type=OW_CONTROLLER,
                command=OW_CTRL_SET_PATTERN_PROFILE,
                addr=tx_id,
                data=payload,
                op=f"set_pattern_profile[{tx_id}]",
            )
        return True

    def get_pattern_profile(self, identifier: int | None = None) -> int:
        """Read the active TX pattern profile via MCU controller command.

        Args:
            identifier: TX chip index, or None to read every chip and require
                that all chips report the same profile.

        Returns:
            Active pattern profile (1-16).

        Raises:
            ValueError: If identifier is negative.
            LIFUProtocolError: If a payload is malformed or chips disagree.
        """
        if identifier is not None and identifier < 0:
            raise ValueError("TX chip identifier must be >= 0")

        profile: int | None = None
        for tx_id in self._resolve_tx_ids(identifier):
            r = self.send_checked(
                packet_type=OW_CONTROLLER,
                command=OW_CTRL_GET_PATTERN_PROFILE,
                addr=tx_id,
                op=f"get_pattern_profile[{tx_id}]",
            )
            if r.data_len < 1 or not r.data:
                raise LIFUProtocolError(
                    f"TX: get_pattern_profile payload too short ({r.data_len} < 1)",
                    code=LIFU_ERR_BAD_PAYLOAD_LENGTH,
                )
            chip_profile = int(r.data[0])
            if profile is None:
                profile = chip_profile
            elif profile != chip_profile:
                raise LIFUProtocolError(
                    f"TX: pattern profile mismatch across chips ({profile} != {chip_profile})",
                    code=LIFU_ERR_BAD_PAYLOAD_FORMAT,
                )
        if profile is None:
            raise ValueError("No TX chips selected to read pattern profile")
        return profile

    def get_delay_profile(self, identifier: int = 0) -> int:
        """Read the active TX delay profile via MCU controller command.

        Mirrors ``set_delay_profile`` by reading the same selector fields
        (G1/G2) used for register-based profile switching.

        Args:
            identifier: TX chip index to read from.

        Returns:
            Active delay profile (1-16).

        Raises:
            ValueError: If identifier is negative.
            LIFUProtocolError: If the payload is malformed or out of range.
        """
        if identifier < 0:
            raise ValueError("TX chip identifier must be >= 0")

        r = self.send_checked(
            packet_type=OW_CONTROLLER,
            command=OW_CTRL_GET_DELAY_PROFILE,
            addr=identifier,
            op=f"get_delay_profile[{identifier}]",
        )
        if r.data_len < 1 or not r.data:
            raise LIFUProtocolError(
                f"TX: get_delay_profile payload too short ({r.data_len} < 1)",
                code=LIFU_ERR_BAD_PAYLOAD_LENGTH,
            )
        profile = int(r.data[0])
        if profile not in VALID_DELAY_PROFILES:
            raise LIFUProtocolError(
                f"TX: get_delay_profile payload out of range ({profile})",
                code=LIFU_ERR_BAD_PAYLOAD_FORMAT,
            )
        return profile

    def start_trigger(self) -> bool:
        """Start the software trigger on the TX device.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        self.send_checked(packet_type=OW_CONTROLLER, command=OW_CTRL_START_SWTRIG,
                          addr=0, op="start_trigger")
        return True

    def stop_trigger(self) -> bool:
        """Stop the software trigger on the TX device.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        r = self.send_checked(packet_type=OW_CONTROLLER, command=OW_CTRL_STOP_SWTRIG,
                              addr=0, op="stop_trigger")
        r.print_packet()
        return True

    def async_mode(self, enable: bool | None = None) -> bool:
        """Enable, disable, or read the TX device's asynchronous mode.

        Args:
            enable: True/False to set the mode; None to query the current state.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        if enable is None:
            payload = None
        else:
            payload = struct.pack('<B', 1 if enable else 0)
        r = self.send_checked(packet_type=OW_CONTROLLER, command=OW_CMD_ASYNC,
                              addr=0, data=payload, op="async_mode")
        return r.reserved == 1

    def get_tx_module_count(self) -> int:
        """Return the number of connected TX modules.

        Thin alias for :meth:`get_module_count`, kept for backwards
        compatibility. ``get_module_count`` includes a fallback to the older
        ``OW_TX7332_DEVICE_COUNT`` command for legacy firmware.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError: If the payload length is invalid.
        """
        return self.get_module_count()

    def enum_tx7332_devices(self,
                            num_devices: int | None = None) -> int:
        """Enumerate TX7332 chips connected to the TX device.

        Args:
            num_devices: Expected chip count. If the device reports a
                different number an exception is raised.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUError: If the detected count does not match *num_devices*.
        """
        r = self.send_checked(packet_type=OW_TX7332, command=OW_TX7332_ENUM,
                              addr=0, op="enum_tx7332_devices")
        if r.reserved == 0:
            raise LIFUProtocolError(
                "TX: enum_tx7332_devices returned zero chips",
                code=LIFU_ERR_EMPTY_RESPONSE,
            )
        num_detected_devices = r.reserved

        if num_devices is not None and num_detected_devices != num_devices:
            raise LIFUError(
                f"TX: expected {num_devices} TX7332 chip(s), detected {num_detected_devices}",
                code=LIFU_ERR_MODULE_COUNT_MISMATCH,
            )

        self.tx_registers = TxDeviceRegisters(num_transmitters=num_detected_devices, module_invert=self.module_invert)
        logger.info("Found %d Transmit Modules (%d tx chips)", num_detected_devices // TRANSMITTERS_PER_MODULE, num_detected_devices)
        return num_detected_devices

    def set_module_invert(self, module_invert: bool | List[bool]) -> None:
        """
        Set the module invert configuration for the TX device.

        Args:
            module_invert (bool | List[bool]): The module invert configuration to set.
        """
        self.module_invert = module_invert
        if self.tx_registers is not None:
            self.tx_registers.module_invert = module_invert

    def demo_tx7332(self, identifier:int) -> bool:
        """Program the TX7332 chip with a demo waveform.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        self.send_checked(packet_type=OW_TX7332, command=OW_TX7332_DEMO,
                          addr=identifier, op="demo_tx7332")
        return True

    def write_register(self, identifier:int, address: int, value: int) -> bool:
        """Write a single TX7332 register.

        Raises:
            ValueError: If *identifier* is negative.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        if identifier < 0:
            raise ValueError("TX chip identifier must be >= 0")
        data = struct.pack("<HI", address, value)
        self.send_checked(packet_type=OW_TX7332, command=OW_TX7332_WREG,
                          addr=identifier, data=data, op="write_register")
        logger.debug("Wrote 0x%08X to chip %d reg 0x%04X", value, identifier, address)
        return True

    def read_register(self, identifier:int, address: int) -> int:
        """Read a single TX7332 register.

        Raises:
            ValueError: If *identifier* is negative.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError: If the payload length is invalid.
        """
        if identifier < 0:
            raise ValueError("TX chip identifier must be >= 0")
        data = struct.pack("<H", address)
        r = self.send_checked(packet_type=OW_TX7332, command=OW_TX7332_RREG,
                              addr=identifier, data=data, op="read_register")
        if r.data_len < 4:
            raise LIFUProtocolError(
                f"TX: read_register payload too short ({r.data_len} < 4)",
                code=LIFU_ERR_BAD_PAYLOAD_LENGTH,
            )
        value = struct.unpack("<I", r.data[:4])[0]
        logger.debug("Read 0x%08X from chip %d reg 0x%04X", value, identifier, address)
        return value

    def write_block(self, identifier: int, start_address: int, reg_values: List[int]) -> bool:
        """Write a contiguous block of TX7332 registers.

        Large blocks are automatically split into chunks of up to 62 registers.

        Raises:
            ValueError: If *identifier* is negative or *reg_values* is empty.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        if identifier < 0:
            raise ValueError("TX chip identifier must be >= 0")
        if not reg_values:
            raise ValueError("reg_values must be a non-empty list")

        max_regs = 62
        for chunk_start in range(0, len(reg_values), max_regs):
            chunk = reg_values[chunk_start : chunk_start + max_regs]
            fmt = "<HBB" + "I" * len(chunk)
            data = struct.pack(fmt, start_address + chunk_start, len(chunk), 0, *chunk)
            self.send_checked(packet_type=OW_TX7332, command=OW_TX7332_WBLOCK,
                              addr=identifier, data=data,
                              op=f"write_block[{chunk_start}]")
        logger.debug("write_block: %d regs from 0x%04X on chip %d", len(reg_values), start_address, identifier)
        return True

    def _resolve_tx_ids(self, identifier: int | None = None) -> List[int]:
        """Resolve an optional chip identifier to the chip indices to act on: all chips when None, else just that one."""
        if identifier is None:
            if self.tx_registers is None:
                raise ValueError("TX devices not enumerated. Call enum_tx7332_devices() first.")
            return list(range(self.tx_registers.num_transmitters))
        if identifier < 0:
            raise ValueError("TX chip identifier must be >= 0")
        return [identifier]

    def commit_profile_ram(self, identifier: int | None = None) -> bool:
        """Commit profile RAM writes by pulsing the self-clearing LOAD_PROF bit."""
        tx_ids = self._resolve_tx_ids(identifier)
        for tx_id in tx_ids:
            self.write_register(tx_id, ADDRESS_GLOBAL_CONTROL, GLOBAL_CONTROL_LOAD_PROFILE)
        return True

    def set_delay_profile(self, profile: int, identifier: int = 0) -> bool:
        """Set the active TX delay profile via MCU controller command.

        Routes delay profile switching through firmware (OW_CTRL_SET_DELAY_PROFILE),
        allowing MCU-side profile bookkeeping and apodization handling. Note the
        firmware applies the profile to every TX chip on the module regardless
        of ``identifier``.

        Args:
            profile: Delay profile to activate (1-16).
            identifier: TX chip index used for addressing/validation.

        Raises:
            ValueError: If profile is out of range.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError:
                On device-communication failure.
        """
        if profile not in VALID_DELAY_PROFILES:
            raise ValueError(f"Invalid delay profile {profile}. Expected 1-16.")

        payload = struct.pack('<B', profile)
        self.send_checked(
            packet_type=OW_CONTROLLER,
            command=OW_CTRL_SET_DELAY_PROFILE,
            addr=identifier,
            data=payload,
            op=f"set_delay_profile[{identifier}]",
        )
        return True

    def set_pattern_profile_select(self,
                                   profile: int,
                                   identifier: int | None = None) -> bool:
        """Select active pattern profile (1-32) via registers 0x1F/0x1E."""
        if profile not in VALID_PATTERN_PROFILES:
            raise ValueError(f"Invalid pattern profile {profile}. Expected 1-32.")
        profile_sel = profile - 1

        tx_ids = self._resolve_tx_ids(identifier)
        for tx_id in tx_ids:
            self.write_register(tx_id, ADDRESS_PATTERN_SEL_G1, profile_sel)
            self.write_register(tx_id, ADDRESS_PATTERN_SEL_G2, profile_sel)
        return True

    def read_block(self, identifier: int, start_address: int, count: int) -> list[int]:
        """Read a contiguous block of TX7332 registers.

        Raises:
            ValueError: If *identifier* is negative or *count* is out of range.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError: If the payload length is invalid.
        """
        if identifier < 0:
            raise ValueError("TX chip identifier must be >= 0")
        if count <= 0 or count > 62:
            raise ValueError(f"count must be 1-62, got {count}")
        data = struct.pack("<HBB", start_address, count, 0)
        r = self.send_checked(packet_type=OW_TX7332, command=OW_TX7332_RBLOCK,
                              addr=identifier, data=data, op="read_block")
        expected = count * 4
        if r.data_len < expected:
            raise LIFUProtocolError(
                f"TX: read_block payload too short ({r.data_len} < {expected})",
                code=LIFU_ERR_BAD_PAYLOAD_LENGTH,
            )
        values = list(struct.unpack(f"<{count}I", r.data[:expected]))
        logger.debug("read_block: %d regs from 0x%04X on chip %d", count, start_address, identifier)
        return values

    def _validate_delay_profile_number(self, profile_number: int) -> None:
        if not isinstance(profile_number, int):
            raise ValueError(f"profile_number must be an int, got {type(profile_number).__name__}")
        if profile_number not in VALID_DELAY_PROFILES:
            raise ValueError(f"profile_number must be in 1-16, got {profile_number}")

    def _get_delay_profile_start_address(self, profile_number: int) -> int:
        """Return the first register address of the selected delay profile block."""
        self._validate_delay_profile_number(profile_number)
        return ADDRESSES_DELAY_DATA[0] + ((profile_number - 1) * DELAY_PROFILE_OFFSET)

    def _decode_delay_profile_register_values(self,
                                              profile_number: int,
                                              register_values: List[int],
                                              units: str = "s") -> List[float]:
        """Decode one delay profile register block into per-channel delay values."""
        self._validate_delay_profile_number(profile_number)
        if len(register_values) != DELAY_PROFILE_OFFSET:
            raise ValueError(f"Expected {DELAY_PROFILE_OFFSET} delay registers, got {len(register_values)}")

        bf_clk_hz = self.tx_registers.bf_clk if self.tx_registers is not None else DEFAULT_CLK_FREQ
        profile_start = self._get_delay_profile_start_address(profile_number)
        delays = [0.0] * NUM_CHANNELS
        for channel in range(1, NUM_CHANNELS + 1):
            address, lsb = get_delay_location(channel, profile_number)
            packed_register = register_values[address - profile_start]
            delay_ticks = get_register_value(packed_register, lsb=lsb, width=DELAY_WIDTH)
            delays[channel - 1] = (delay_ticks / bf_clk_hz) * getunitconversion("s", units)
        return delays

    def read_delay_profile_value(self,
                                 profile_number: int,
                                 identifier: int = 0,
                                 units: str = "s") -> Dict[str, object]:
        """Read one delay profile block from device RAM and decode channel delay values.

        Args:
            profile_number: Delay profile index to read (1-16).
            identifier: TX chip index to read from.
            units: Output units for delay values (default seconds).

        Returns:
            Dict with profile number, chip id, start address, raw registers, and decoded delays.
        """
        if identifier < 0:
            raise ValueError("TX chip identifier must be >= 0")
        self._validate_delay_profile_number(profile_number)

        start_address = self._get_delay_profile_start_address(profile_number)
        raw_registers = self.read_block(identifier=identifier,
                                        start_address=start_address,
                                        count=DELAY_PROFILE_OFFSET)
        decoded_delays = self._decode_delay_profile_register_values(profile_number=profile_number,
                                                                    register_values=raw_registers,
                                                                    units=units)
        return {
            "profile_number": profile_number,
            "identifier": identifier,
            "start_address": start_address,
            "raw_registers": raw_registers,
            "delays": decoded_delays,
            "units": units,
        }

    def set_solution(self,
                     pulse: Dict | List[Dict],
                     delays: np.ndarray,
                     apodizations: np.ndarray,
                     sequence: Dict,
                     trigger_mode: TriggerModeOpts = "sequence",
                     profile_index: int = 1,
                     profile_increment: bool = True,
                     execution_order: List[int] | None = None,
                     pulse_profile_map: Dict[int, int] | None = None) -> bool:
        """Program the TX device with the supplied beamforming solution.

        For multi-profile solutions, row ``i`` of ``delays`` and ``apodizations``
        is grouped with delay profile ``i+1``. Pulse profiles are decoupled from
        delay profiles: ``pulse`` may be a single dict (one shared pulse profile)
        or a list of dicts (one per unique pulse profile).

        ``pulse_profile_map`` controls which pulse profile each delay profile uses.
        When ``pulse`` is a single dict and no map is provided, all delay profiles
        share pulse profile 1.  When ``pulse`` is a list of N dicts and no map is
        provided, a 1:1 mapping is used (backward compatible).

        Each delay profile i (1-16) is bundled as a group with a pulse profile
        (possibly shared across delay profiles), delay values (register 0x16
        selector + delay RAM), and an apodization configuration (register 0x1B).

        For multi-profile solutions the firmware cycles through
        ``execution_order`` at pulse boundaries: each entry gets
        ``pulse_count / len(execution_order)`` consecutive pulses, and the
        order restarts at the beginning of every pulse train.

        Args:
            pulse:              Dict (single shared pulse config) or List[Dict]
                                (one per unique pulse profile).
            delays:             np.ndarray of shape (N, 64) for N delay profiles.
            apodizations:       np.ndarray of shape (N, 64) for N delay profiles.
            sequence:           Dict with trigger timing (pulse_interval, pulse_count, etc.).
            trigger_mode:       "sequence", "continuous", or "single".
            profile_index:      Initial active delay profile (1-16).
            profile_increment:  Whether firmware auto-increments profile on sequence end.
            execution_order:    List[int] of delay profile indices to cycle.
                               If None, defaults to [1, 2, ..., N].
            pulse_profile_map:  Optional dict mapping delay profile index (1-based) to
                               pulse profile index (1-based).  When None, derived from
                               the ``pulse`` argument type.

        Raises:
            ValueError: If the inputs are malformed or execution_order is invalid.
            LIFUError: On any device-communication failure.
        """
        # Validate and normalize inputs.
        delays = np.array(delays)
        if delays.ndim == 1:
            delays = delays.reshape(1, -1)
        apodizations = np.array(apodizations)
        if apodizations.ndim == 1:
            apodizations = apodizations.reshape(1, -1)
        n = delays.shape[0]

        if n == 0:
            raise ValueError("At least one profile row is required")
        if n != apodizations.shape[0]:
            raise ValueError("Delays and apodizations must have the same number of rows")
        if delays.shape[1] != apodizations.shape[1]:
            raise ValueError("Delays and apodizations must have the same number of elements per row")

        # Delay RAM supports 16 profile slots on TX7332.
        if n > len(VALID_DELAY_PROFILES):
            raise ValueError(f"Too many profile rows ({n}). Max supported is {len(VALID_DELAY_PROFILES)}")

        # pulse_configs: list of unique pulse config dicts, indexed 0-based.
        if isinstance(pulse, list):
            pulse_configs = pulse
        else:
            pulse_configs = [pulse]
        num_pulse_profiles = len(pulse_configs)

        # Build pulse_profile_map: delay profile index (1-based) -> pulse profile index (1-based)
        if pulse_profile_map is None:
            if num_pulse_profiles == 1:
                # Single pulse config: all delay profiles share pulse profile 1.
                pulse_profile_map = {i + 1: 1 for i in range(n)}
            else:
                if num_pulse_profiles != n:
                    raise ValueError(
                        f"When pulse is a list without pulse_profile_map, "
                        f"it must have one entry per delay profile row ({n}), got {num_pulse_profiles}"
                    )
                pulse_profile_map = {i + 1: i + 1 for i in range(n)}
        else:
            for dp_idx, pp_idx in pulse_profile_map.items():
                if dp_idx < 1 or dp_idx > n:
                    raise ValueError(f"pulse_profile_map key {dp_idx} out of range 1-{n}")
                if pp_idx < 1 or pp_idx > num_pulse_profiles:
                    raise ValueError(f"pulse_profile_map value {pp_idx} out of range 1-{num_pulse_profiles}")
            for i in range(1, n + 1):
                if i not in pulse_profile_map:
                    raise ValueError(f"pulse_profile_map missing mapping for delay profile {i}")

        # Default to sequential execution order: [1, 2, ..., n].
        if execution_order is None:
            execution_order = list(range(1, n + 1))

        if not isinstance(execution_order, list):
            raise ValueError("execution_order must be a list of profile indices")
        if len(execution_order) == 0:
            raise ValueError("execution_order cannot be empty")
        for idx in execution_order:
            if not isinstance(idx, int) or idx < 1 or idx > n:
                raise ValueError(f"execution_order contains invalid profile index {idx}. Must be in 1-{n}")

        # Pulse-level profile switching interlocks: firmware switches profiles
        # between pulses, so pulse_count must be divisible by the execution
        # order length and the inter-pulse dead time must cover the SPI writes.
        if n > 1:
            pulse_count = sequence["pulse_count"]
            n_exec = len(execution_order)
            if pulse_count % n_exec != 0:
                raise ValueError(
                    f"pulse_count ({pulse_count}) must be divisible by the number of "
                    f"profiles in execution_order ({n_exec}). "
                    f"Each profile gets pulse_count/n_profiles = {pulse_count}/{n_exec} consecutive pulses."
                )
            pulse_interval = sequence["pulse_interval"]
            pulse_width_s = sequence.get("pulse_width", DEFAULT_PULSE_WIDTH_US) * 1e-6
            dead_time = pulse_interval - pulse_width_s
            if dead_time < MIN_PROFILE_SWITCH_INTERVAL:
                raise ValueError(
                    f"Inter-pulse dead time ({dead_time*1e6:.0f} µs) is less than the minimum "
                    f"required for profile switching ({MIN_PROFILE_SWITCH_INTERVAL*1e6:.0f} µs). "
                    f"Increase pulse_interval or decrease pulse_width."
                )

        logger.info(f"Grouped profile package system: {n} profiles, execution_order={execution_order}")

        n_elements = delays.shape[1]
        n_required_devices = int(n_elements / NUM_CHANNELS)
        # enum_tx7332_devices raises LIFUError on mismatch
        self.enum_tx7332_devices(num_devices=n_required_devices)

        # Pre-compute duty cycle per mapped pulse config: use max apodization
        # across all delay profiles sharing the same pulse config.
        duty_cycle_by_pulse = {}
        for pidx in set(pulse_profile_map.values()):
            pulse_cfg = pulse_configs[pidx - 1]
            mapped_dps = [k for k, v in pulse_profile_map.items() if v == pidx]
            max_apod = max(max(apodizations[dp - 1, :]) for dp in mapped_dps)
            duty_cycle_by_pulse[pidx] = DEFAULT_PATTERN_DUTY_CYCLE * max_apod * pulse_cfg["amplitude"]

        # Create one pulse profile per delay profile slot.
        # The TX7332 firmware cycles PATTERN_SEL in lockstep with DELAY_SEL,
        # so every delay profile slot must have valid pattern RAM data.
        # When multiple delay profiles share the same pulse config, the
        # pattern data is replicated across their slots.
        for dp in range(n):
            pidx = pulse_profile_map[dp + 1]
            pulse_cfg = pulse_configs[pidx - 1]
            pulse_profile = Tx7332PulseProfile(
                profile=dp + 1,
                frequency=pulse_cfg["frequency"],
                cycles=int(pulse_cfg["duration"] * pulse_cfg["frequency"]),
                duty_cycle=duty_cycle_by_pulse[pidx],
            )
            self.tx_registers.add_pulse_profile(pulse_profile)

        # Create all delay profiles (one per row).
        for dp in range(n):
            delay_profile = Tx7332DelayProfile(
                profile=dp + 1,
                delays=delays[dp, :],
                apodizations=apodizations[dp, :],
            )
            self.tx_registers.add_delay_profile(delay_profile)

        if profile_index not in self.tx_registers.configured_delay_profiles():
            raise ValueError(
                f"profile_index={profile_index} is not configured. "
                f"Configured delay profiles: {self.tx_registers.configured_delay_profiles()}"
            )
        if profile_index not in self.tx_registers.configured_pulse_profiles():
            raise ValueError(
                f"pulse profile {profile_index} is not configured. "
                f"Configured pulse profiles: {self.tx_registers.configured_pulse_profiles()}"
            )

        self.tx_registers.activate_delay_profile(profile_index)
        self.tx_registers.activate_pulse_profile(profile_index)

        self.set_trigger(
            pulse_interval=sequence["pulse_interval"],
            pulse_count=sequence["pulse_count"],
            pulse_train_interval=sequence["pulse_train_interval"],
            pulse_train_count=sequence["pulse_train_count"],
            trigger_mode=trigger_mode,
            profile_index=profile_index,
            profile_increment=profile_increment,
        )
        self.apply_all_registers()

        if n > 1:
            # Make the selected profile active for the first trigger: delay
            # selector, apodization, and (0-based) pattern selector as a unit.
            active_delay_groups = self.tx_registers.get_delay_control_registers(profile_index)
            for txi, regs in enumerate(active_delay_groups):
                self.write_register(txi, ADDRESS_DELAY_SEL, regs[ADDRESS_DELAY_SEL])
                self.write_register(txi, ADDRESS_APODIZATION, regs[ADDRESS_APODIZATION])
                self.write_register(txi, ADDRESS_PATTERN_SEL_G1, (profile_index - 1) & PATTERN_PROFILE_SELECT_MASK)
                self.write_register(txi, ADDRESS_PATTERN_SEL_G2, (profile_index - 1) & PATTERN_PROFILE_SELECT_MASK)

            # Send execution_order plus the pre-computed per-chip apodization
            # register of each profile so firmware can auto-cycle profiles at
            # pulse boundaries: apod_reg_values[profile - 1][chip] = uint32.
            apod_reg_values = []
            for profile in sorted(self.tx_registers.configured_delay_profiles()):
                delay_control = self.tx_registers.get_delay_control_registers(profile)
                apod_reg_values.append([regs[ADDRESS_APODIZATION] for regs in delay_control])
            self._send_grouped_profile_cycle(execution_order, apod_reg_values)

        return True

    def _send_grouped_profile_cycle(self, execution_order: List[int], apod_reg_values: List[List[int]]) -> bool:
        """Send grouped profile cycle command with pre-computed apodization registers.

        The SDK pre-computes the TX7332 apodization register values (including the
        TX7332 channel-to-bit mapping) and sends the uint32 register values directly.
        The firmware stores and writes them as-is during profile cycling — no mapping
        logic needed on the MCU.

        Protocol:
          Packet format: [n_profiles] [n_chips] [exec_order_len] [execution_order...]
                         [profile_0_chip_0_reg:4B LE] [profile_0_chip_1_reg:4B LE] ...
          - n_profiles: Number of configured profiles (1-16)
          - n_chips: Number of TX chips (typically 2)
          - exec_order_len: Length of execution order array
          - execution_order: Array of 1-based profile indices to cycle through
          - apod registers: Pre-computed uint32 apodization register per chip per profile (LE)

        Args:
            execution_order: List of 1-based profile indices to cycle through.
            apod_reg_values: List of lists — apod_reg_values[profile][chip] = uint32 register value.
        """
        if not hasattr(self, 'uart') or self.uart is None:
            raise ValueError("UART not initialized for grouped profile cycle setup")

        n_profiles = len(apod_reg_values)
        n_chips = len(apod_reg_values[0]) if n_profiles > 0 else 0

        payload = bytearray()

        # Header
        payload.append(n_profiles)
        payload.append(n_chips)
        payload.append(len(execution_order))

        # Execution order indices (1-based profile numbers)
        for profile_idx in execution_order:
            payload.append(profile_idx & 0xFF)

        # Pre-computed apodization registers: uint32 little-endian per chip per profile
        for profile_idx in range(n_profiles):
            for chip_idx in range(n_chips):
                reg_val = apod_reg_values[profile_idx][chip_idx] & 0xFFFFFFFF
                payload.extend(reg_val.to_bytes(REGISTER_BYTES, byteorder='little'))

        logger.info(
            f"Sending grouped profile cycle: {n_profiles} profiles, {n_chips} chips, "
            f"execution_order={execution_order}, payload_size={len(payload)} bytes"
        )

        self.send_checked(
            packet_type=OW_CONTROLLER,
            command=OW_CTRL_SET_PROFILE_CYCLE,
            addr=0,
            data=bytes(payload),
            op="set_grouped_profile_cycle"
        )
        logger.debug("Grouped profile cycle command sent successfully")
        return True

    def apply_all_registers(self, load_profile: bool = True) -> bool:
        """Flush all configured TX7332 registers to the device.

        Args:
            load_profile: When True, pulse LOAD_PROF afterwards so the chips
                latch the freshly written profile RAM.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        registers = self.tx_registers.get_registers(pack=True, pack_single=True)
        for txi, txregs in enumerate(registers):
            for addr, reg_values in txregs.items():
                self.write_block(identifier=txi, start_address=addr, reg_values=reg_values)
        if load_profile:
            self.commit_profile_ram()
        return True

    def write_ti_config_to_tx_device(self, file_path: str, txchip_id: int) -> bool:
        """Parse a TI configuration file and program its registers.

        Raises:
            FileNotFoundError: If *file_path* does not exist.
            ValueError: If the config file yields no registers.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        parsed_registers = self.__parse_ti_cfg_file(file_path)
        if not parsed_registers:
            raise ValueError(f"No registers parsed from TI configuration file: {file_path}")

        for group, addr, value in parsed_registers:
            logger.debug("Writing to %s | Address: 0x%02X | Value: 0x%08X", group, addr, value)
            self.write_register(identifier=txchip_id, address=addr, value=value)

        logger.debug("Successfully wrote all registers to the TX device.")
        return True

    # ------------------------------------------------------------------
    # Firmware update (delegates to LIFUDFU.LIFUDFUManager)
    # ------------------------------------------------------------------

    def get_module_count(self) -> int:
        """Return the number of connected LIFU transmitter modules.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError: If the payload length is invalid.
        """
        try:
            r = self.send_checked(packet_type=OW_CONTROLLER, command=OW_CTRL_GET_MODULE_COUNT,
                                addr=0, op="get_module_count")
        except LIFUError:
            r = self.send_checked(packet_type=OW_TX7332, command=OW_TX7332_DEVICE_COUNT,
                                addr=0, op="get_device_count")

        if not r.data or len(r.data) < 1:
            raise LIFUProtocolError(
                f"TX: get_module_count payload length {r.data_len} < 1",
                code=LIFU_ERR_BAD_PAYLOAD_LENGTH,
            )
        count = r.data[0]
        logger.debug("Detected %d module(s)", count)
        return count

    def enumerate_modules(self) -> int:
        """Re-run the master's robust enumeration (Phase 2).

        The master broadcasts OW_CMD_CLEAR_CONFIG down the one-wire chain (so every
        node — application or bootloader — drops any stale I2C address), waits for
        the chain to re-ready, then re-runs the discovery walk assigning fresh
        addresses 0x20, 0x21, .... Each node reports whether it is running the
        application or is stuck in the bootloader.

        Use this after forcing one or more slaves into DFU so they pick up unique
        addresses instead of all colliding at 0x72.

        Returns:
            int: the new module count (including the master).
        """
        # The master runs the discovery walk synchronously before replying; each hop
        # can incur one-wire timeouts (up to ~0.5 s). Allow generous headroom so a
        # slow-but-successful walk is not reported as a transport timeout.
        #
        # retries=0 is important: a retry would send a SECOND enumerate while the
        # master is still processing the first (the walk is long), overlapping two
        # re-enumerations and corrupting the master's state. One shot only.
        r = self.send_checked(packet_type=OW_CONTROLLER, command=OW_CTRL_ENUMERATE,
                              addr=0, op="enumerate_modules", timeout=30.0, retries=0)
        if not r.data or len(r.data) < 1:
            raise LIFUProtocolError(
                f"TX: enumerate_modules payload length {r.data_len} < 1",
                code=LIFU_ERR_BAD_PAYLOAD_LENGTH,
            )
        count = r.data[0]
        logger.debug("Re-enumeration detected %d module(s)", count)
        return count

    def get_module_mode(self, module: int) -> int:
        """Return a module's operating mode: NODE_MODE_APP or NODE_MODE_BOOTLOADER.

        The mode is recorded by the master during discovery (Phase 2). Module 0
        (the master) is always NODE_MODE_APP. A bootloader-mode module listens for
        I2C DFU at its assigned address (see :meth:`get_module_i2c_addr`).

        Args:
            module: module index (0 = master, 1.. = slaves).

        Returns:
            int: one of NODE_MODE_APP / NODE_MODE_BOOTLOADER / NODE_MODE_UNKNOWN.
        """
        r = self.send_checked(packet_type=OW_CONTROLLER, command=OW_CTRL_GET_MODULE_MODE,
                              addr=module, op="get_module_mode")
        if not r.data or len(r.data) < 1:
            raise LIFUProtocolError(
                f"TX: get_module_mode payload length {r.data_len} < 1",
                code=LIFU_ERR_BAD_PAYLOAD_LENGTH,
            )
        return r.data[0]

    def get_module_i2c_addr(self, module: int) -> int:
        """Return the assigned 7-bit I2C address for a slave module index.

        Slaves are enumerated sequentially from 0x20 (module 1 -> 0x20, module 2 ->
        0x21, ...). This mirrors BASE_I2C_ADDRESS in the firmware. Module 0 (the
        master) has no slave-bus address; 0 is returned.
        """
        if module <= 0:
            return 0
        return 0x20 + (module - 1)

    def scan_module_modes(self) -> list[dict]:
        """Return a per-module summary of index, mode and I2C address.

        Convenience wrapper around get_module_count + get_module_mode, e.g.::

            [{"module": 0, "mode": NODE_MODE_APP,        "i2c_addr": 0x00},
             {"module": 1, "mode": NODE_MODE_BOOTLOADER, "i2c_addr": 0x20}, ...]
        """
        out = []
        for m in range(self.get_module_count()):
            try:
                mode = self.get_module_mode(m)
            except LIFUError:
                mode = NODE_MODE_UNKNOWN
            out.append({"module": m, "mode": mode, "i2c_addr": self.get_module_i2c_addr(m)})
        return out

    def update_firmware(self, module: int, package_file: str,
                        vid: int = 0x0483, pid: int = 0xDF11,
                        libusb_dll: str | None = None,
                        i2c_addr: int = 0x72,
                        dfu_wait_s: float = 5.0,
                        device_type: str = "transmitter",
                        progress_callback=None,
                        enter_dfu_fn=None) -> bool:
        """Update firmware on a single module.

        Module 0 (USB master): host → USB DFU.
        Module 1+ (I2C slaves): host → UART OW → master → I2C 0x72 (DFU bootloader).

        Args:
            module:            Module index (0 = USB master).
            package_file:      Path to the signed firmware package file.
            vid:               USB VID for module 0 DFU (default 0x0483).
            pid:               USB PID for module 0 DFU (default 0xDF11).
            libusb_dll:        Optional path to libusb-1.0.dll (Windows).
            i2c_addr:          I2C address of the slave DFU bootloader (default 0x72).
            dfu_wait_s:        Seconds to wait after DFU-enter for bootloader to boot.
            progress_callback: Optional ``(written, total, label)`` callable.

        Returns:
            bool: True on success.

        Raises:
            ValueError:    If the UART is not connected.
            RuntimeError:  If DFU entry cannot be verified or programming fails.
        """
        from openlifu_sdk.io.LIFUDFU import LIFUDFUManager  # lazy import

        self._require_connected()

        mgr = LIFUDFUManager(uart=self.uart)
        mgr.update_module(
            module=module,
            package_file=package_file,
            enter_dfu_fn=self.enter_dfu if enter_dfu_fn is None else enter_dfu_fn,
            vid=vid,
            pid=pid,
            libusb_dll=libusb_dll,
            i2c_addr=i2c_addr,
            dfu_wait_s=dfu_wait_s,
            device_type=device_type,
            progress_callback=progress_callback,
        )
        return True

    @property
    def print(self) -> None:
        """
        Print TX device information.

        Raises:
            None
        """
        print("TX Device Information") # noqa: T201
        print("  UART Port:") # noqa: T201

def get_delay_location(channel:int, profile:int=1):
    """
    Gets the address and least significant bit of a delay

    :param channel: Channel number
    :param profile: Delay profile number
    :returns: Register address and least significant bit of the delay location
    """
    if channel not in DELAY_CHANNEL_MAP:
        raise ValueError(f"Invalid channel {channel}.")
    channel_map = DELAY_CHANNEL_MAP[channel]
    if profile not in VALID_DELAY_PROFILES:
        raise ValueError(f"Invalid Profile {profile}")
    address = ADDRESSES_DELAY_DATA[0] + (profile-1) * DELAY_PROFILE_OFFSET + channel_map['row']
    lsb = channel_map['lsb']
    return address, lsb

def set_register_value(reg_value:int, value:int, lsb:int=0, width: int | None=None):
    """
    Sets the value of a parameter in a register integer

    :param reg_value: Register value
    :param value: New value of the parameter
    :param lsb: Least significant bit of the parameter
    :param width: Width of the parameter (bits)
    :returns: New register value
    """
    if width is None:
        width = REGISTER_WIDTH - lsb
    mask = (1 << width) - 1
    if value < 0 or value > mask:
        raise ValueError(f"Value {value} does not fit in {width} bits")
    return (reg_value & ~(mask << lsb)) | ((int(value) & mask) << lsb)

def get_register_value(reg_value:int, lsb:int=0, width: int | None=None):
    """
    Extracts the value of a parameter from a register integer

    :param reg_value: Register value
    :param lsb: Least significant bit of the parameter
    :param width: Width of the parameter (bits)
    :returns: Value of the parameter
    """
    if width is None:
        width = REGISTER_WIDTH - lsb
    mask = (1 << width) - 1
    return (reg_value >> lsb) & mask

def calc_pulse_pattern(frequency:float, duty_cycle:float=DEFAULT_PATTERN_DUTY_CYCLE, bf_clk:float=DEFAULT_CLK_FREQ):
    """
    Calculates the pattern for a given frequency and duty cycle

    The pattern is calculated to represent a single cycle of a pulse with the specified frequency and duty cycle.
    If the pattern requires more than 16 periods, the clock divider is increased to reduce the period length.

    :param frequency: Frequency of the pattern in Hz
    :param duty_cycle: Duty cycle of the pattern
    :param bf_clk: Clock frequency of the BF system in Hz
    :returns: Tuple of lists of levels and lengths, and the clock divider setting
    """
    clk_div_n = 0
    while clk_div_n < 6:
        clk_n = bf_clk / (2**clk_div_n)
        period_samples = int(clk_n / frequency)
        first_half_period_samples = int(period_samples / 2)
        second_half_period_samples = period_samples - first_half_period_samples
        first_on_samples = int(first_half_period_samples * duty_cycle)
        if first_on_samples < 2:
            logging.warning("Duty cycle too short. Setting to minimum of 2 samples")
            first_on_samples = 2
        first_off_samples = first_half_period_samples - first_on_samples
        second_on_samples = max(2, int(second_half_period_samples * duty_cycle))
        if second_on_samples < 2:
            logging.warning("Duty cycle too short. Setting to minimum of 2 samples")
            second_on_samples = 2
        second_off_samples = second_half_period_samples - second_on_samples
        if first_off_samples > 0 and first_off_samples < 2:
            logging.warn
            first_off_samples = 0
            first_on_samples = first_half_period_samples
        if second_off_samples > 0 and first_off_samples < 2:
            second_off_samples = 0
            second_on_samples = second_half_period_samples
        levels = [1, 0, -1, 0]
        per_lengths = []
        per_levels = []
        for i, samples in enumerate([first_on_samples, first_off_samples, second_on_samples, second_off_samples]):
            while samples > 0:
                if samples > MAX_PATTERN_PERIOD_LENGTH+2:
                    if samples == MAX_PATTERN_PERIOD_LENGTH+3:
                        per_lengths.append(MAX_PATTERN_PERIOD_LENGTH-1)
                        samples -= (MAX_PATTERN_PERIOD_LENGTH+1)
                    else:
                        per_lengths.append(MAX_PATTERN_PERIOD_LENGTH)
                        samples -= (MAX_PATTERN_PERIOD_LENGTH+2)
                    per_levels.append(levels[i])
                else:
                    per_lengths.append(samples-2)
                    per_levels.append(levels[i])
                    samples = 0
        if len(per_levels) <= MAX_PATTERN_PERIODS:
            t = (np.arange(np.sum(np.array(per_lengths)+2))*(1/clk_n)).tolist()
            y = np.concatenate([[yi]*(ni+2) for yi,ni in zip(per_levels, per_lengths)]).tolist()
            pattern = {'levels': per_levels,
                        'lengths': per_lengths,
                        'clk_div_n': clk_div_n,
                        't': t,
                        'y': y}
            return pattern
        else:
            clk_div_n += 1
    raise ValueError(f"Pattern requires too many periods ({len(per_levels)} > {MAX_PATTERN_PERIODS})")

def get_pattern_location(period:int, profile:int=1):
    """
    Gets the address and least significant bit of a pattern period

    :param period: Pattern period number
    :param profile: Pattern profile number
    :returns: Register address and least significant bit of the pattern period location
    """
    if period not in PATTERN_MAP:
        raise ValueError(f"Invalid period {period}.")
    if profile not in VALID_PATTERN_PROFILES:
        raise ValueError(f"Invalid profile {profile}.")
    address = ADDRESSES_PATTERN_DATA[0] + (profile-1) * PATTERN_PROFILE_OFFSET + PATTERN_MAP[period]['row']
    lsb_lvl = PATTERN_MAP[period]['lsb_lvl']
    lsb_period = PATTERN_MAP[period]['lsb_period']
    return address, lsb_lvl, lsb_period

def print_regs(d):
    for addr, val in sorted(d.items()):
        if isinstance(val, list):
            for i, v in enumerate(val):
                print(f'0x{addr:X}[+{i:d}]:x{v:08X}') # noqa: T201
        else:
            print(f'0x{addr:X}:x{val:08X}') # noqa: T201

def pack_registers(regs, pack_single:bool=False):
    """
    Packs registers into contiguous blocks

    :param regs: Dictionary of registers
    :param pack_single: Pack single registers into arrays. Default True.
    :returns: Dictionary of packed registers.
    """
    addresses = sorted(regs.keys())
    if len(addresses) == 0:
        return {}
    last_addr = -255
    burst_addr = -255
    packed = {}
    for addr in addresses:
        if addr == last_addr+1 and burst_addr in packed:
            packed[burst_addr].append(regs[addr])
        else:
            packed[addr] = [regs[addr]]
            burst_addr = addr
        last_addr = addr
    if not pack_single:
        for addr, val in packed.items():
            if len(val) == 1:
                packed[addr] = val[0]
    return packed

def swap_byte_order(regs):
    """
    Swaps the byte order of the registers

    :param regs: Dictionary of registers
    :returns: Dictionary of registers with swapped byte order
    """
    swapped = {}
    for addr, val in regs.items():
        if isinstance(val, list):
            swapped[addr] = [int.from_bytes(v.to_bytes(REGISTER_BYTES, 'big'), 'little') for v in val]
        else:
            swapped[addr] = int.from_bytes(val.to_bytes(REGISTER_BYTES, 'big'), 'little')
    return swapped

@dataclass
class Tx7332DelayProfile:
    profile: Annotated[int, OpenLIFUFieldData("Profile Index (1-16)", "Index of the delay profile (1-16)")]
    """Index of the delay profile (1-16). The Tx7332 support 16 unique delay profiles."""

    delays: Annotated[List[float], OpenLIFUFieldData("Delay values", "Delay values for transducer elements")]
    """Delay values for transducer elements"""

    apodizations: Annotated[List[int] | None, OpenLIFUFieldData("Apodizations", "Apodization values for transducer elements")] = None
    """Apodization values for transducer elements"""

    units: Annotated[str, OpenLIFUFieldData("Units", "Time units used for delay values")] = 's'
    """Time units used for delay values"""

    def __post_init__(self):
        self.num_elements = len(self.delays)
        if self.apodizations is None:
            self.apodizations = [1]*self.num_elements
        if len(self.apodizations) != self.num_elements:
            raise ValueError(f"Apodizations list must have {self.num_elements} elements")
        if self.profile not in VALID_DELAY_PROFILES:
            raise ValueError(f"Invalid Profile {self.profile}")

@dataclass
class Tx7332PulseProfile:
    profile: Annotated[int, OpenLIFUFieldData("Profile index (1-32)", "Index of the pulse profile (1-32)")]
    """Index of the pulse profile (1-32). The Tx7332 supports 32 unique pulse profiles."""

    frequency: Annotated[float, OpenLIFUFieldData("Frequency (Hz)", "Center frequency of the pulse (Hz)")]
    """Center frequency of the pulse (Hz)"""

    cycles: Annotated[int, OpenLIFUFieldData("Number of cycles", "Number of cycles in the pulse")]
    """Number of cycles in the pulse"""

    duty_cycle: Annotated[float, OpenLIFUFieldData("Duty cycle (0-1)", "Pulse duty cycle for the generated square wave (0-1)")] = DEFAULT_PATTERN_DUTY_CYCLE
    """Pulse duty cycle for the generated square wave (0-1). By default 0.66 is used to approximate a sinusoidal wave."""

    tail_count: Annotated[int, OpenLIFUFieldData("Tail count (cycles)", "Clock cycles to actively drive the pulser to ground after the pulse ends")] = DEFAULT_TAIL_COUNT
    """Clock cycles to actively drive the pulser to ground after the pulse ends. Default 29"""

    invert: Annotated[bool, OpenLIFUFieldData("Invert polarity?", "Flag indicating whether to invert the pulse amplitude")] = False
    """Invert the pulse amplitude. Default False"""

    def __post_init__(self):
        if self.profile not in VALID_PATTERN_PROFILES:
            raise ValueError(f"Invalid profile {self.profile}.")

@dataclass
class Tx7332Registers:
    bf_clk: Annotated[float, OpenLIFUFieldData("Clock Frequency (Hz)", "The beamformer clock frequency in Hz.")] = DEFAULT_CLK_FREQ
    """The beamformer clock frequency in Hz. This much match the hardware clock frequency in order for calculated register values to produce the correct pulse and delay timting. Default is 64 MHz."""

    _delay_profiles_list: Annotated[List[Tx7332DelayProfile], OpenLIFUFieldData("Delay profiles list", "Internal list of available delay profiles")] = field(default_factory=list)
    """Internal list of available delay profiles"""

    _pulse_profiles_list: Annotated[List[Tx7332PulseProfile], OpenLIFUFieldData("Pulse profiles list", "Internal list of available pulse profiles")] = field(default_factory=list)
    """Internal list of available pulse profiles"""

    active_delay_profile: Annotated[int | None, OpenLIFUFieldData("Active delay profile", "Index of the currently active delay profile")] = None
    """Index of the currently active delay profile"""

    active_pulse_profile: Annotated[int | None, OpenLIFUFieldData("Active pulse profile", "Index of the currently active pulse profile")] = None
    """Index of the currently active pulse profile"""

    def __post_init__(self):
        delay_profile_indices = self.configured_delay_profiles()
        if len(delay_profile_indices) != len(set(delay_profile_indices)):
            raise ValueError("Duplicate delay profiles found")
        if self.active_delay_profile is not None and self.active_delay_profile not in delay_profile_indices:
            raise ValueError(f"Delay profile {self.active_delay_profile} not found")
        pulse_profile_indices = self.configured_pulse_profiles()
        if len(pulse_profile_indices) != len(set(pulse_profile_indices)):
            raise ValueError("Duplicate pulse profiles found")
        if self.active_pulse_profile is not None and self.active_pulse_profile not in pulse_profile_indices:
            raise ValueError(f"Pulse profile {self.active_pulse_profile} not found")

    def add_delay_profile(self, p: Tx7332DelayProfile, activate: bool | None=None):
        if p.num_elements != NUM_CHANNELS:
            raise ValueError(f"Delay profile must have {NUM_CHANNELS} elements")
        profile_indices = self.configured_delay_profiles()
        if p.profile in profile_indices:
            i = profile_indices.index(p.profile)
            self._delay_profiles_list[i] = p
        else:
            self._delay_profiles_list.append(p)
        if activate is None:
            activate = self.active_delay_profile is None
        if activate:
            self.active_delay_profile = p.profile

    def add_pulse_profile(self, p: Tx7332PulseProfile, activate: bool | None=None):
        profile_indices = self.configured_pulse_profiles()
        if p.profile in profile_indices:
            i = profile_indices.index(p.profile)
            self._pulse_profiles_list[i] = p
        else:
            self._pulse_profiles_list.append(p)
        if activate is None:
            activate = self.active_pulse_profile is None
        if activate:
            self.active_pulse_profile = p.profile

    def remove_delay_profile(self, profile:int):
        profile_indices = self.configured_delay_profiles()
        if profile not in profile_indices:
            raise ValueError(f"Delay profile {profile} not found")
        index = profile_indices.index(profile)
        del self._delay_profiles_list[index]
        if self.active_delay_profile == index:
            self.active_delay_profile = None

    def remove_pulse_profile(self, profile:int):
        profiles = self.configured_pulse_profiles()
        if profile not in profiles:
            raise ValueError(f"Pulse profile {profile} not found")
        index = profiles.index(profile)
        del self._pulse_profiles_list[index]
        if self.active_pulse_profile == index:
            self.active_pulse_profile = None

    def get_delay_profile(self, profile: int | None=None) -> Tx7332DelayProfile:
        if profile is None:
            profile = self.active_delay_profile
        profiles = self.configured_delay_profiles()
        if profile not in profiles:
            raise ValueError(f"Delay profile {profile} not found")
        index = profiles.index(profile)
        return self._delay_profiles_list[index]

    def configured_delay_profiles(self) -> List[int]:
        return [p.profile for p in self._delay_profiles_list]

    def get_pulse_profile(self, profile: int | None=None) -> Tx7332PulseProfile:
        if profile is None:
            profile = self.active_pulse_profile
        profiles = self.configured_pulse_profiles()
        if profile not in profiles:
            raise ValueError(f"Pulse profile {profile} not found")
        index = profiles.index(profile)
        return self._pulse_profiles_list[index]

    def configured_pulse_profiles(self) -> List[int]:
        return [p.profile for p in self._pulse_profiles_list]

    def activate_delay_profile(self, profile:int):
        if profile not in self.configured_delay_profiles():
            raise ValueError(f"Delay profile {profile} not configured")
        self.active_delay_profile = profile

    def activate_pulse_profile(self, profile:int):
        if profile not in self.configured_pulse_profiles():
            raise ValueError(f"Pulse profile {profile} not configured")
        self.active_pulse_profile = profile

    def get_delay_control_registers(self, profile: int | None=None) -> Dict[int,int]:
        if profile is None:
            profile = self.active_delay_profile
        delay_profile = self.get_delay_profile(profile)
        apod_register = 0
        for i, apod in enumerate(delay_profile.apodizations):
            apod_register = set_register_value(apod_register, 1-apod, lsb=APODIZATION_CHANNEL_ORDER_REVERSED.index(i+1), width=1)
        delay_sel_register = 0
        delay_sel_register = set_register_value(delay_sel_register, delay_profile.profile-1, lsb=12, width=4)
        delay_sel_register = set_register_value(delay_sel_register, delay_profile.profile-1, lsb=28, width=4)
        return {ADDRESS_DELAY_SEL: delay_sel_register,
                ADDRESS_APODIZATION: apod_register}

    def get_pulse_control_registers(self, profile: int | None=None, pulse_invert: bool = False) -> Dict[int,int]:
        if profile is None:
            profile = self.active_pulse_profile
        pulse_profile = self.get_pulse_profile(profile)
        if pulse_profile.profile not in VALID_PATTERN_PROFILES:
            raise ValueError(f"Invalid profile {pulse_profile.profile}.")
        pattern = calc_pulse_pattern(pulse_profile.frequency, pulse_profile.duty_cycle, bf_clk=self.bf_clk)
        clk_div_n = pattern['clk_div_n']
        clk_div = 2**clk_div_n
        clk_n = self.bf_clk / clk_div
        cycles = int(pulse_profile.cycles)
        if cycles > (MAX_REPEAT+1):
            # Use elastic repeat
            pulse_duration_samples = self.bf_clk * ((cycles  / pulse_profile.frequency) + ELASTIC_MODE_PULSE_LENGTH_ADJUST)
            repeat = 0
            elastic_repeat = int(pulse_duration_samples / 16)
            period_samples = int(clk_n / pulse_profile.frequency)
            cycles = 16*elastic_repeat / period_samples
            y = pattern['y']*int(cycles+1)
            y = y[:(16*elastic_repeat)]
            y = y + ([0]*pulse_profile.tail_count)
            t = np.arange(len(y))*(1/clk_n)
            elastic_mode = 1
            if elastic_repeat > MAX_ELASTIC_REPEAT:
                raise ValueError("Pattern duration too long for elastic repeat")
        else:
            repeat = cycles-1
            elastic_repeat = 0
            elastic_mode = 0
            y = pattern['y']*(repeat+1)
            y = np.array(y + [0]*pulse_profile.tail_count)
        reg_mode =  0x02000003
        reg_mode = set_register_value(reg_mode, clk_div_n, lsb=3, width=3)
        reg_mode = set_register_value(reg_mode, int(pulse_profile.invert^pulse_invert), lsb=6, width=1)
        reg_repeat = 0
        reg_repeat = set_register_value(reg_repeat, repeat, lsb=1, width=5)
        reg_repeat = set_register_value(reg_repeat, pulse_profile.tail_count, lsb=6, width=5)
        reg_repeat = set_register_value(reg_repeat, elastic_mode, lsb=11, width=1)
        reg_repeat = set_register_value(reg_repeat, elastic_repeat, lsb=12, width=16)
        reg_pat_sel = 0
        reg_pat_sel = set_register_value(reg_pat_sel, pulse_profile.profile-1, lsb=0, width=6)
        registers = {ADDRESS_PATTERN_MODE: reg_mode,
                     ADDRESS_PATTERN_REPEAT: reg_repeat,
                     ADDRESS_PATTERN_SEL_G1: reg_pat_sel,
                     ADDRESS_PATTERN_SEL_G2: reg_pat_sel}
        return registers

    def get_delay_data_registers(self, profile: int | None=None, pack: bool=False, pack_single: bool=False) -> Dict[int,int]:
        if profile is None:
            profile = self.active_delay_profile
        delay_profile = self.get_delay_profile(profile)
        data_registers = {}
        for channel in range(1, NUM_CHANNELS+1):
            address, lsb = get_delay_location(channel, delay_profile.profile)
            if address not in data_registers:
                data_registers[address] = 0
            delay_value = int(delay_profile.delays[channel-1] * getunitconversion(delay_profile.units, 's') * self.bf_clk)
            data_registers[address] = set_register_value(data_registers[address], delay_value, lsb=lsb, width=DELAY_WIDTH)
        if pack:
            data_registers = pack_registers(data_registers, pack_single=pack_single)
        return data_registers

    def get_pulse_data_registers(self, profile: int | None=None, pack: bool=False, pack_single: bool=False) -> Dict[int,int]:
        if profile is None:
            profile = self.active_pulse_profile
        profile_index = self.get_pulse_profile(profile)
        data_registers = {}
        pattern = calc_pulse_pattern(profile_index.frequency, profile_index.duty_cycle, bf_clk=self.bf_clk)
        levels = pattern['levels']
        lengths = pattern['lengths']
        nperiods = len(levels)
        level_lut = {-1: 0b01, 0: 0b11, 1: 0b10}  # Map levels to register values 0b11 drive to ground 0b00 high impedance
        for i, (level, length) in enumerate(zip(levels, lengths)):
            address, lsb_lvl, lsb_length = get_pattern_location(i+1, profile_index.profile)
            if address not in data_registers:
                data_registers[address] = 0
            data_registers[address] = set_register_value(data_registers[address], level_lut[level], lsb=lsb_lvl, width=PATTERN_LEVEL_WIDTH)
            data_registers[address] = set_register_value(data_registers[address], length, lsb=lsb_length, width=PATTERN_LENGTH_WIDTH)
        if nperiods< MAX_PATTERN_PERIODS:
            address, lsb_lvl, lsb_length = get_pattern_location(nperiods+1, profile_index.profile)
            if address not in data_registers:
                data_registers[address] = 0
            data_registers[address] = set_register_value(data_registers[address], 0b111, lsb=lsb_lvl, width=PATTERN_LEVEL_WIDTH)
            data_registers[address] = set_register_value(data_registers[address], 0, lsb=lsb_length, width=PATTERN_LENGTH_WIDTH)
        if pack:
            data_registers = pack_registers(data_registers, pack_single=pack_single)
        return data_registers

    def get_registers(self, profiles: ProfileOpts = "configured", pack: bool=False, pack_single: bool=False, pulse_invert: bool=False) -> Dict[int,int]:
        if len(self._delay_profiles_list) == 0:
            raise ValueError("No delay profiles have been configured")
        if len(self._pulse_profiles_list) == 0:
            raise ValueError("No pulse profiles have been configured")
        if self.active_delay_profile is None:
            raise ValueError("No delay profile activated")
        if self.active_pulse_profile is None:
            raise ValueError("No pulse profile activated")
        registers = {addr:0x0 for addr in ADDRESSES_GLOBAL}
        registers.update(self.get_delay_control_registers())
        registers.update(self.get_pulse_control_registers(pulse_invert=pulse_invert))
        if profiles == "active":
            delay_data = self.get_delay_data_registers()
            pulse_data = self.get_pulse_data_registers()
        else:
            if profiles == "all":
                delay_data = {addr:0x0 for addr in ADDRESSES_DELAY_DATA}
                pulse_data = {addr:0x0 for addr in ADDRESSES_PATTERN_DATA}
            else:
                delay_data = {}
                pulse_data = {}
            for delay_profile in self._delay_profiles_list:
                delay_data.update(self.get_delay_data_registers(profile=delay_profile.profile))
            for profile_index in self._pulse_profiles_list:
                pulse_data.update(self.get_pulse_data_registers(profile=profile_index.profile))
        registers.update(delay_data)
        registers.update(pulse_data)
        if pack:
            registers = pack_registers(registers, pack_single=pack_single)
        return registers

@dataclass
class TxDeviceRegisters:
    bf_clk: Annotated[int, OpenLIFUFieldData("Clock Frequency (Hz)", "The beamformer clock frequency in Hz.")] = DEFAULT_CLK_FREQ
    """The beamformer clock frequency in Hz. This much match the hardware clock frequency in order for calculated register values to produce the correct pulse and delay timting. Default is 64 MHz."""

    _delay_profiles_list: Annotated[List[Tx7332DelayProfile], OpenLIFUFieldData("Delay profiles list", "Internal list of available delay profiles")] = field(default_factory=list)
    """Internal list of available delay profiles"""

    _profiles_list: Annotated[List[Tx7332PulseProfile], OpenLIFUFieldData("Pulse profiles list", "Internal list of available pulse profiles")] = field(default_factory=list)
    """Internal list of available pulse profiles"""

    active_delay_profile: Annotated[int | None, OpenLIFUFieldData("Active delay profile", "Index of the currently active delay profile")] = None
    """Index of the currently active delay profile"""

    active_profile: Annotated[int | None, OpenLIFUFieldData("Active pulse profile", "Index of the currently active pulse profile")] = None
    """Index of the currently active pulse profile"""

    num_transmitters: Annotated[int, OpenLIFUFieldData("Number of transmitters", "The number of transmitters available on the device")] = DEFAULT_NUM_TRANSMITTERS
    """The number of transmitters available on the device"""

    module_invert: Annotated[List[bool] | bool, OpenLIFUFieldData("Module Invert", "List of flags indicating whether to invert the pulse amplitude for each module or a single flag for all modules")] = False
    """List of flags indicating whether to invert the pulse amplitude for each module or a single flag for all modules"""

    def __post_init__(self):
        self.transmitters = tuple([Tx7332Registers(bf_clk=self.bf_clk) for _ in range(self.num_transmitters)])

    def add_pulse_profile(self, pulse_profile: Tx7332PulseProfile, activate: bool | None=None):
        """
        Add a pulse profile

        :param p: Pulse profile
        :param activate: Activate the pulse profile
        """
        profiles = self.configured_pulse_profiles()
        if pulse_profile.profile in profiles:
            i = profiles.index(pulse_profile.profile)
            self._profiles_list[i] = pulse_profile
        else:
            self._profiles_list.append(pulse_profile)
        if activate is None:
            activate = self.active_profile is None
        if activate:
            self.active_profile = pulse_profile.profile
        for tx in self.transmitters:
            tx.add_pulse_profile(pulse_profile, activate = activate)

    def add_delay_profile(self, delay_profile: Tx7332DelayProfile, activate: bool | None=None):
        """
        Add a delay profile

        :param p: Delay profile
        :param activate: Activate the delay profile
        """
        if delay_profile.num_elements != NUM_CHANNELS*self.num_transmitters:
            raise ValueError(f"Delay profile must have {NUM_CHANNELS*self.num_transmitters} elements")
        profiles = self.configured_delay_profiles()
        if delay_profile.profile in profiles:
            i = profiles.index(delay_profile.profile)
            self._delay_profiles_list[i] = delay_profile
        else:
            self._delay_profiles_list.append(delay_profile)
        if activate is None:
            activate = self.active_delay_profile is None
        if activate:
            self.active_delay_profile = delay_profile.profile
        for i, tx in enumerate(self.transmitters):
            module = i // 2
            chip = (i+1) % 2
            start_channel = module*NUM_CHANNELS*2 + chip*NUM_CHANNELS
            profiles = np.arange(start_channel, start_channel+NUM_CHANNELS, dtype=int)
            tx_delays = np.array(delay_profile.delays)[profiles].tolist()
            tx_apodizations = np.array(delay_profile.apodizations)[profiles].tolist()
            txp = Tx7332DelayProfile(delay_profile.profile, tx_delays, tx_apodizations, delay_profile.units)
            tx.add_delay_profile(txp, activate = activate)

    def remove_delay_profile(self, profile:int):
        """
        Remove a delay profile

        :param profile: Delay profile number
        """
        profiles = self.configured_delay_profiles()
        if profile not in profiles:
            raise ValueError(f"Delay profile {profile} not found")
        i = profiles.index(profile)
        del self._delay_profiles_list[i]
        if self.active_delay_profile == profile:
            self.active_delay_profile = None
        for tx in self.transmitters:
            tx.remove_delay_profile(profile)

    def remove_pulse_profile(self, profile:int):
        """
        Remove a pulse profile

        :param profile: Pulse profile number
        """
        profiles = self.configured_pulse_profiles()
        if profile not in profiles:
            raise ValueError(f"Pulse profile {profile} not found")
        i = profiles.index(profile)
        del self._profiles_list[i]
        if self.active_profile == profile:
            self.active_profile = None
        for tx in self.transmitters:
            tx.remove_pulse_profile(profile)

    def get_delay_profile(self, profile:int | None=None) -> Tx7332DelayProfile:
        """
        Retrieve a delay profile

        :param profile: Delay profile number
        :return: Delay profile
        """
        if profile is None:
            profile = self.active_delay_profile
        profiles = self.configured_delay_profiles()
        if profile not in profiles:
            raise ValueError(f"Delay profile {profile} not found")
        i = profiles.index(profile)
        return self._delay_profiles_list[i]

    def configured_delay_profiles(self) -> List[int]:
        """
        Get the configured delay profiles

        :return: List of delay profiles
        """
        return [p.profile for p in self._delay_profiles_list]

    def get_pulse_profile(self, profile:int | None=None) -> Tx7332PulseProfile:
        """
        Retrieve a pulse profile

        :param profile: Pulse profile number
        :return: Pulse profile
        """
        if profile is None:
            profile = self.active_profile
        profiles = self.configured_pulse_profiles()
        if profile not in profiles:
            raise ValueError(f"Pulse profile {profile} not found")
        i = profiles.index(profile)
        return self._profiles_list[i]

    def configured_pulse_profiles(self) -> List[int]:
        """
        Get the configured pulse profiles

        :return: List of pulse profiles
        """
        return [p.profile for p in self._profiles_list]

    def activate_delay_profile(self, profile:int=1):
        """
        Activates a delay profile

        :param profile: Delay profile number
        """
        for tx in self.transmitters:
            tx.activate_delay_profile(profile)
        self.active_delay_profile = profile

    def activate_pulse_profile(self, profile:int=1):
        """
        Activates a pulse profile

        :param profile: Pulse profile number
        """
        for tx in self.transmitters:
            tx.activate_pulse_profile(profile)
        self.active_profile = profile

    def recompute_delay_profiles(self):
        """
        Recompute the delay profiles
        """
        for tx in self.transmitters:
            profiles = tx.configured_delay_profiles()
            for profile in profiles:
                tx.remove_delay_profile(profile)
        for dp in self._delay_profiles_list:
            self.add_delay_profile(dp, activate = dp.profile == self.active_delay_profile)

    def recompute_pulse_profiles(self):
        """
        Recompute the pulse profiles
        """
        for tx in self.transmitters:
            profiles = tx.configured_pulse_profiles()
            for profile in profiles:
                tx.remove_pulse_profile(profile)
            for pp in self._profiles_list:
                tx.add_pulse_profile(pp, activate = pp.profile == self.active_profile)

    def get_registers(self, profiles: ProfileOpts = "configured", recompute: bool = False, pack: bool=False, pack_single:bool=False) -> List[Dict[int,int]]:
        """
        Get the registers for all transmitters

        :param profiles: Profile options
        :param recompute: Recompute the registers
        :return: List of registers for each transmitter
        """
        if recompute:
            self.recompute_delay_profiles()
            self.recompute_pulse_profiles()
        if isinstance(self.module_invert, bool):
            tx_invert = [self.module_invert] * self.num_transmitters
        else:
            tx_invert = list(np.array(self.module_invert*TRANSMITTERS_PER_MODULE, dtype=bool).reshape(TRANSMITTERS_PER_MODULE, -1).T.reshape(-1))
        return [tx.get_registers(profiles, pack=pack, pack_single=pack_single, pulse_invert=inv) for tx, inv in zip(self.transmitters, tx_invert)]

    def get_delay_control_registers(self, profile:int | None=None) -> List[Dict[int,int]]:
        """
        Get the delay control registers for all transmitters

        :param profile: Delay profile number
        :return: List of delay control registers for each transmitter
        """
        if profile is None:
            profile = self.active_delay_profile
        return [tx.get_delay_control_registers(profile) for tx in self.transmitters]

    def get_pulse_control_registers(self, profile:int | None=None) -> List[Dict[int,int]]:
        """
        Get the pulse control registers for all transmitters

        :param profile: Pulse profile number
        :return: List of pulse control registers for each transmitter
        """
        if profile is None:
            profile = self.active_profile
        if isinstance(self.module_invert, bool):
            tx_invert = [self.module_invert] * self.num_transmitters
        else:
            tx_invert = list(np.array(self.module_invert*TRANSMITTERS_PER_MODULE, dtype=bool).T.reshape(-1))
        return [tx.get_pulse_control_registers(profile, pulse_invert=inv) for tx, inv in zip(self.transmitters, tx_invert)]

    def get_delay_data_registers(self, profile:int | None=None, pack: bool=False, pack_single: bool=False) -> List[Dict[int,int]]:
        """
        Get the delay data registers for all transmitters

        :param profile: Delay profile number
        :return: List of delay data registers for each transmitter
        """
        if profile is None:
            profile = self.active_delay_profile
        return [tx.get_delay_data_registers(profile, pack=pack, pack_single=pack_single) for tx in self.transmitters]

    def get_pulse_data_registers(self, profile:int | None=None, pack: bool=False, pack_single: bool=False) -> List[Dict[int,int]]:
        """
        Get the pulse data registers for all transmitters

        :param profile: Pulse profile number
        :return: List of pulse data registers for each transmitter
        """
        if profile is None:
            profile = self.active_profile
        return [tx.get_pulse_data_registers(profile, pack=pack, pack_single=pack_single) for tx in self.transmitters]

    def get_active_delay_profile(self, chip_id: int = 0) -> int:
        """
        Get the currently active delay profile from the register model.
        Ensures both Group 1 and Group 2 are using the same profile.

        :param chip_id: ID of the chip to query
        :return: Active delay profile number (1-based, 1 to 16)
        """
        if chip_id < 0 or chip_id >= len(self.transmitters):
            raise ValueError(f"Invalid chip_id {chip_id}. Must be 0-{len(self.transmitters) - 1}.")

        delay_reg = self.transmitters[chip_id].get_delay_control_registers()[ADDRESS_DELAY_SEL]
        active_profile_g1 = (delay_reg >> BF_PROF_SEL_G1_SHIFT) & BF_PROF_SEL_FIELD_MASK
        active_profile_g2 = (delay_reg >> BF_PROF_SEL_G2_SHIFT) & BF_PROF_SEL_FIELD_MASK

        # Sanity check: ensure the whole chip is on the same delay profile.
        if active_profile_g1 != active_profile_g2:
            raise ValueError(
                f"Desynchronized profiles on chip {chip_id}! "
                f"G1 is using Profile {active_profile_g1 + 1}, but G2 is using Profile {active_profile_g2 + 1}."
            )

        return active_profile_g1 + 1
