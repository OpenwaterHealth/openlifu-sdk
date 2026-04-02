from __future__ import annotations

import json
import logging
import re
import struct
from typing import TYPE_CHECKING, Literal

import numpy as np

from openlifu_sdk.beamforming.tx7332 import (  # noqa: F401 -- re-exported for backward compatibility
    ADDRESS_APODIZATION,
    ADDRESS_DELAY_SEL,
    ADDRESS_DYNPWR_1,
    ADDRESS_DYNPWR_2,
    ADDRESS_GLOBAL_MODE,
    ADDRESS_LDO_PWR_1,
    ADDRESS_LDO_PWR_2,
    ADDRESS_PATTERN_MODE,
    ADDRESS_PATTERN_REPEAT,
    ADDRESS_PATTERN_SEL_G1,
    ADDRESS_PATTERN_SEL_G2,
    ADDRESS_STANDBY,
    ADDRESS_TRSW,
    ADDRESS_TRSW_TURNOFF,
    ADDRESS_TRSW_TURNON,
    ADDRESSES,
    ADDRESSES_DELAY_DATA,
    ADDRESSES_GLOBAL,
    ADDRESSES_PATTERN_DATA,
    APODIZATION_CHANNEL_ORDER,
    APODIZATION_CHANNEL_ORDER_REVERSED,
    DEFAULT_CLK_FREQ,
    DEFAULT_NUM_TRANSMITTERS,
    DEFAULT_PATTERN_DUTY_CYCLE,
    DEFAULT_TAIL_COUNT,
    DELAY_CHANNEL_MAP,
    DELAY_ORDER,
    DELAY_ORDER_REVERSED,
    DELAY_PROFILE_OFFSET,
    DELAY_WIDTH,
    ELASTIC_MODE_PULSE_LENGTH_ADJUST,
    MAX_ELASTIC_REPEAT,
    MAX_PATTERN_PERIOD_LENGTH,
    MAX_PATTERN_PERIODS,
    MAX_REGISTER,
    MAX_REPEAT,
    NUM_CHANNELS,
    PATTERN_LENGTH_WIDTH,
    PATTERN_LEVEL_WIDTH,
    PATTERN_MAP,
    PATTERN_PERIOD_ORDER,
    PATTERN_PROFILE_OFFSET,
    REGISTER_BYTES,
    REGISTER_WIDTH,
    TRANSMITTERS_PER_MODULE,
    VALID_DELAY_PROFILES,
    VALID_PATTERN_PROFILES,
    Tx7332DelayProfile,
    Tx7332PulseProfile,
    Tx7332Registers,
    TxDeviceRegisters,
    calc_pulse_pattern,
    get_delay_location,
    get_pattern_location,
    get_register_value,
    pack_registers,
    print_regs,
    set_register_value,
    swap_byte_order,
)
from openlifu_sdk.io.LIFUConfig import (
    OW_CMD,
    OW_CMD_ASYNC,
    OW_CMD_DFU,
    OW_CMD_ECHO,
    OW_CMD_GET_AMBIENT,
    OW_CMD_GET_TEMP,
    OW_CMD_HWID,
    OW_CMD_PING,
    OW_CMD_RESET,
    OW_CMD_TOGGLE_LED,
    OW_CMD_USR_CFG,
    OW_CMD_VERSION,
    OW_CONTROLLER,
    OW_CTRL_GET_MODULE_COUNT,
    OW_CTRL_GET_SWTRIG,
    OW_CTRL_SET_SWTRIG,
    OW_CTRL_START_SWTRIG,
    OW_CTRL_STOP_SWTRIG,
    OW_ERROR,
    OW_TX7332,
    OW_TX7332_DEMO,
    OW_TX7332_DEVICE_COUNT,
    OW_TX7332_ENUM,
    OW_TX7332_RBLOCK,
    OW_TX7332_RREG,
    OW_TX7332_VWBLOCK,
    OW_TX7332_VWREG,
    OW_TX7332_WBLOCK,
    OW_TX7332_WREG,
    TRIGGER_MODE_CONTINUOUS,
    TRIGGER_MODE_SEQUENCE,
    TRIGGER_MODE_SINGLE,
)
from openlifu_sdk.io.LIFUUart import LIFUUart
from openlifu_sdk.io.LIFUUserConfig import LifuUserConfig

DEFAULT_PULSE_WIDTH_US = 20
HW_ID_DATA_LENGTH = 12
TEMPERATURE_DATA_LENGTH = 4
ProfileOpts = Literal["active", "configured", "all"]
TriggerModeOpts = Literal["sequence", "continuous", "single"]

if TYPE_CHECKING:
    pass


logger = logging.getLogger(__name__)


class TxDevice:
    def __init__(self, uart: LIFUUart, module_invert: bool | list[bool] = False):
        """
        Initialize the TxDevice.

        Args:
            uart (LIFUUart): The LIFUUart instance for communication.
        """
        self._tx_instances = []
        self.tx_registers = None
        self.uart = uart
        self.module_invert = module_invert
        if self.uart and not self.uart.asyncMode:
            self.uart.check_usb_status()
            if self.uart.is_connected():
                logger.debug("TX Device connected.")
            else:
                logger.debug("TX Device NOT Connected.")

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

    def is_connected(self) -> bool:
        """
        Check if the TX device is connected.

        Returns:
            bool: True if the device is connected, False otherwise.
        """
        if self.uart:
            return self.uart.is_connected()
        return False

    def close(self):
        """
        Close Uart
        """
        if self.uart and self.uart.is_connected():
            self.uart.disconnect()

    def ping(self, module: int = 0) -> bool:
        """
        Send a ping command to the TX device to verify connectivity.

        Raises:
            ValueError: If the UART is not connected.
            Exception: If an error occurs during the ping process.
        """
        try:
            if self.uart.demo_mode:
                return True

            if not self.uart.is_connected():
                logger.error("TX Device not connected")
                return False

            logger.debug("Send Ping to Device.")

            r = self.uart.send_packet(id=None, packetType=OW_CMD, command=OW_CMD_PING, addr=module)
            self.uart.clear_buffer()

            if r.packet_type == OW_ERROR:
                logger.error("Error sending ping")
                return False
            else:
                return True
        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def get_version(self, module: int = 0) -> str:
        """
        Retrieve the firmware version of the TX device.

        Returns:
            str: Firmware version in the format 'vX.Y.Z'.

        Raises:
            ValueError: If the UART is not connected.
            Exception: If an error occurs while fetching the version.
        """
        try:
            if self.uart.demo_mode:
                return "v0.1.1"

            if not self.uart.is_connected():
                logger.error("TX Device not connected")
                return "v0.0.0"

            r = self.uart.send_packet(id=None, packetType=OW_CMD, command=OW_CMD_VERSION, addr=module)
            self.uart.clear_buffer()
            r.print_packet()
            if r.data_len == 3:
                ver = f"v{r.data[0]}.{r.data[1]}.{r.data[2]}"
            elif r.data_len and r.data:
                try:
                    # Decode only the valid length, strip trailing NULs and whitespace
                    ver_str = r.data[: r.data_len].decode("utf-8", errors="ignore").rstrip("\x00").strip()
                    ver = ver_str if ver_str else "v0.0.0"
                except Exception:
                    ver = "v0.0.0"
            else:
                ver = "v0.0.0"
            logger.debug(ver)
            return ver
        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def echo(self, module: int = 0, echo_data=None) -> tuple[bytes, int]:
        """
        Send an echo command to the device with data and receive the same data in response.

        Args:
            echo_data (bytes): The data to send (must be a byte array).

        Returns:
            tuple[bytes, int]: The echoed data and its length.

        Raises:
            ValueError: If the UART is not connected.
            TypeError: If the `echo_data` is not a byte array.
            Exception: If an error occurs during the echo process.
        """
        try:
            if self.uart.demo_mode:
                data = b"Hello LIFU!"
                return data, len(data)

            if not self.uart.is_connected():
                logger.error("TX Device not connected")
                return None, None

            # Check if echo_data is a byte array
            if echo_data is not None and not isinstance(echo_data, bytes | bytearray):
                raise TypeError("echo_data must be a byte array")

            r = self.uart.send_packet(id=None, packetType=OW_CMD, command=OW_CMD_ECHO, addr=module, data=echo_data)
            self.uart.clear_buffer()
            # r.print_packet()
            if r.data_len > 0:
                return r.data, r.data_len
            else:
                return None, None

        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except TypeError as t:
            logger.error("TypeError: %s", t)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during echo process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def toggle_led(self, module: int = 0) -> bool:
        """
        Toggle the LED on the TX device.

        Raises:
            ValueError: If the UART is not connected.
            Exception: If an error occurs while toggling the LED.
        """
        try:
            if self.uart.demo_mode:
                return True

            if not self.uart.is_connected():
                logger.error("TX Device not connected")
                return False

            self.uart.send_packet(id=None, packetType=OW_CMD, command=OW_CMD_TOGGLE_LED, addr=module)
            self.uart.clear_buffer()
            # r.print_packet()
            return True

        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def get_hardware_id(self, module: int = 0) -> str:
        """
        Retrieve the hardware ID of the TX device.

        Returns:
            str: Hardware ID in hexadecimal format.

        Raises:
            ValueError: If the UART is not connected.
            Exception: If an error occurs while retrieving the hardware ID.
        """
        try:
            if self.uart.demo_mode:
                return bytes.fromhex("deadbeefcafebabe1122334455667788")

            if not self.uart.is_connected():
                logger.error("TX Device not connected")
                return None

            r = self.uart.send_packet(id=None, packetType=OW_CMD, command=OW_CMD_HWID, addr=module)
            self.uart.clear_buffer()
            # r.print_packet()
            if r.data_len == HW_ID_DATA_LENGTH:
                return r.data.hex()
            else:
                return None
        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def read_config(self, module: int = 0) -> LifuUserConfig | None:
        """
        Read the user configuration from device flash.

        The configuration is stored as JSON with metadata (magic, version, sequence, CRC).

        Returns:
            LifuUserConfig: Parsed configuration object, or None on error

        Raises:
            ValueError: If the UART is not connected
            Exception: If an error occurs during communication
        """
        try:
            if self.uart.demo_mode:
                logger.info("Demo mode: returning empty config")
                return LifuUserConfig()

            if not self.uart.is_connected():
                raise ValueError("Console Device not connected")

            # Send read command (reserved=0 for READ)
            logger.debug("Reading user config from device...")
            r = self.uart.send_packet(
                id=None,
                packetType=OW_CMD,
                addr=module,
                command=OW_CMD_USR_CFG,
                reserved=0,  # 0 = READ
            )
            self.uart.clear_buffer()

            if r.packet_type == OW_ERROR:
                logger.error("Error reading config from device")
                return None

            # Parse wire format response
            try:
                config = LifuUserConfig.from_wire_bytes(r.data)
                logger.debug("Read config: seq=%s, json_len=%s", config.header.seq, config.header.json_len)
                return config
            except Exception as e:
                logger.error("Failed to parse config response: %s", e)
                return None

        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise

        except Exception as e:
            logger.error("Unexpected error reading config: %s", e)
            raise

    def write_config(self, config: LifuUserConfig, module: int = 0) -> LifuUserConfig | None:
        """
        Write user configuration to device flash.

        Can pass either:
        - Full wire format (header + JSON)
        - Raw JSON bytes (device will parse as JSON)

        Args:
            config: LifuUserConfig object to write

        Returns:
            LifuUserConfig: Updated configuration from device (with new seq/crc), or None on error

        Raises:
            ValueError: If the UART is not connected
            Exception: If an error occurs during communication
        """
        try:
            if self.uart.demo_mode:
                logger.info("Demo mode: simulating config write")
                return config

            if not self.uart.is_connected():
                raise ValueError("Console Device not connected")

            # Convert config to wire format bytes
            wire_data = config.to_wire_bytes()

            logger.debug("Writing config to device: %s bytes", len(wire_data))

            # Send write command (reserved=1 for WRITE)
            r = self.uart.send_packet(
                id=None,
                packetType=OW_CMD,
                command=OW_CMD_USR_CFG,
                addr=module,
                reserved=1,  # 1 = WRITE
                data=wire_data,
            )
            self.uart.clear_buffer()

            if r.packet_type == OW_ERROR:
                logger.error("Error writing config to device")
                return None

            # Response contains only the updated 16-byte header (with new seq/crc).
            # Reconstruct the full config by combining the updated header with the
            # JSON data we just wrote (which is not echoed back by the firmware).
            try:
                from openlifu_sdk.io.LIFUUserConfig import LifuUserConfigHeader

                updated_header = LifuUserConfigHeader.from_bytes(r.data[:16])
                updated_config = LifuUserConfig(header=updated_header, json_data=config.json_data)
                logger.debug("Config written successfully: new seq=%s", updated_config.header.seq)
                return updated_config
            except Exception as e:
                logger.error("Failed to parse write response: %s", e)
                return None

        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise

        except Exception as e:
            logger.error("Unexpected error writing config: %s", e)
            raise

    def write_config_json(self, json_str: str, module: int = 0) -> LifuUserConfig | None:
        """
        Write user configuration from a JSON string.

        This is a convenience method that creates a LifuUserConfig from JSON
        and writes it to the device.

        Args:
            json_str: JSON string to write

        Returns:
            LifuUserConfig: Updated configuration from device, or None on error

        Raises:
            ValueError: If JSON is invalid or UART is not connected
            Exception: If an error occurs during communication
        """
        try:
            config = LifuUserConfig()
            config.set_json_str(json_str)
            return self.write_config(module=module, config=config)
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON: %s", e)
            raise ValueError(f"Invalid JSON: {e}") from e

    def get_temperature(self, module: int = 1) -> float:
        """
        Retrieve the temperature reading from the TX device.

        Returns:
            float: Temperature value in Celsius.

        Raises:
            ValueError: If the UART is not connected.
            Exception: If an error occurs or the received data length is invalid.
        """
        try:
            if self.uart.demo_mode:
                return 32.4

            if not self.uart.is_connected():
                logger.error("TX Device not connected")
                return 0

            # Send the GET_TEMP command
            r = self.uart.send_packet(id=None, packetType=OW_CONTROLLER, command=OW_CMD_GET_TEMP, addr=module)
            self.uart.clear_buffer()
            # r.print_packet()

            # Check if the data length matches a float (4 bytes)
            if r.data_len == TEMPERATURE_DATA_LENGTH:
                # Unpack the float value from the received data (assuming little-endian)
                temperature = struct.unpack("<f", r.data)[0]
                # Truncate the temperature to 2 decimal places
                truncated_temperature = round(temperature, 2)
                return truncated_temperature
            else:
                raise ValueError("Invalid data length received for temperature")
        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def get_ambient_temperature(self, module: int = 0) -> float:
        """
        Retrieve the ambient temperature reading from the TX device.

        Returns:
            float: Temperature value in Celsius.

        Raises:
            ValueError: If the UART is not connected.
            Exception: If an error occurs or the received data length is invalid.
        """
        try:
            if self.uart.demo_mode:
                return 28.9

            if not self.uart.is_connected():
                logger.error("TX Device not connected")
                return 0

            # Send the GET_TEMP command
            r = self.uart.send_packet(id=None, packetType=OW_CONTROLLER, command=OW_CMD_GET_AMBIENT, addr=module)
            self.uart.clear_buffer()
            # r.print_packet()

            # Check if the data length matches a float (4 bytes)
            if r.data_len == TEMPERATURE_DATA_LENGTH:
                # Unpack the float value from the received data (assuming little-endian)
                temperature = struct.unpack("<f", r.data)[0]
                # Truncate the temperature to 2 decimal places
                truncated_temperature = round(temperature, 2)
                return truncated_temperature
            else:
                logger.error("Invalid data length received for ambient temperature")
                return 0
        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle
            return 0

    def set_trigger(
        self,
        pulse_interval: float,
        pulse_count: int = 1,
        pulse_width: int = DEFAULT_PULSE_WIDTH_US,
        pulse_train_interval: float = 0.0,
        pulse_train_count: int = 1,
        trigger_mode: TriggerModeOpts = "sequence",
        profile_index: int = 0,
        profile_increment: bool = True,
    ) -> dict:
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

        if pulse_train_interval > 0 and (pulse_train_interval < pulse_interval * pulse_count):
            raise ValueError("Pulse train interval cannot be less than pulse interval * pulse count")

        logger.info(
            "Setting trigger with parameters: pulse_interval=%s, pulse_count=%s, "
            "pulse_width=%s, pulse_train_interval=%s, pulse_train_count=%s, trigger_mode=%s",
            pulse_interval,
            pulse_count,
            pulse_width,
            pulse_train_interval,
            pulse_train_count,
            trigger_mode,
        )

        trigger_json = {
            "TriggerFrequencyHz": 1 / pulse_interval,
            "TriggerPulseCount": pulse_count,
            "TriggerPulseWidthUsec": pulse_width,
            "TriggerPulseTrainInterval": pulse_train_interval * 1000000,
            "TriggerPulseTrainCount": pulse_train_count,
            "TriggerMode": trigger_mode_int,
            "ProfileIndex": 0,
            "ProfileIncrement": 0,
        }
        return self.set_trigger_json(data=trigger_json)

    def set_trigger_json(self, data=None) -> dict:
        """
        Set the trigger configuration on the TX device.

        Args:
            data (dict): A dictionary containing the trigger configuration.

        Returns:
            dict: JSON response from the device.

        Raises:
            ValueError: If `data` is None or the UART is not connected.
            Exception: If an error occurs while setting the trigger.
        """
        try:
            if self.uart.demo_mode:
                return None

            # Ensure data is not None and is a valid dictionary
            if data is None:
                logger.error("Data cannot be None.")
                return None

            if not self.uart.is_connected():
                raise ValueError("TX Device not connected")

            try:
                json_string = json.dumps(data)
            except json.JSONDecodeError as e:
                logger.error("Data must be valid JSON: %s", e)
                return None

            payload = json_string.encode("utf-8")

            r = self.uart.send_packet(
                id=None, packetType=OW_CONTROLLER, command=OW_CTRL_SET_SWTRIG, addr=0, data=payload
            )
            self.uart.clear_buffer()

            if r.packet_type != OW_ERROR and r.data_len > 0:
                # Parse response as JSON, if possible
                try:
                    response_json = json.loads(r.data.decode("utf-8"))
                    return response_json
                except json.JSONDecodeError as e:
                    logger.error("Error decoding JSON: %s", e)
                    return None
            else:
                return None
        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def get_trigger_json(self) -> dict:
        """
        Start the trigger on the TX device.

        Returns:
            bool: True if the trigger was started successfully, False otherwise.

        Raises:
            ValueError: If the UART is not connected.
            Exception: If an error occurs while starting the trigger.
        """
        try:
            if self.uart.demo_mode:
                return None

            if not self.uart.is_connected():
                raise ValueError("TX Device not connected")

            r = self.uart.send_packet(id=None, packetType=OW_CONTROLLER, command=OW_CTRL_GET_SWTRIG, addr=0, data=None)
            self.uart.clear_buffer()
            data_object = None
            try:
                data_object = json.loads(r.data.decode("utf-8"))
            except json.JSONDecodeError as e:
                logger.error("Error decoding JSON: %s", e)
            return data_object
        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def get_trigger(self):
        """
        Retrieve the current trigger configuration from the TX device.

        Returns:
            dict: The trigger configuration as a dictionary.

        Raises:
            ValueError: If the UART is not connected.
            Exception: If an error occurs while fetching the trigger configuration.
        """
        trigger_json = self.get_trigger_json()
        if trigger_json:
            if trigger_json["TriggerMode"] == TRIGGER_MODE_SEQUENCE:
                mode = "sequence"
            elif trigger_json["TriggerMode"] == TRIGGER_MODE_CONTINUOUS:
                mode = "continuous"
            elif trigger_json["TriggerMode"] == TRIGGER_MODE_SINGLE:
                mode = "single"
            else:
                mode = "unknown"
            trigger_dict = {
                "pulse_interval": 1 / trigger_json["TriggerFrequencyHz"],
                "pulse_count": trigger_json["TriggerPulseCount"],
                "pulse_width": trigger_json["TriggerPulseWidthUsec"],
                "pulse_train_interval": trigger_json["TriggerPulseTrainInterval"],
                "pulse_train_count": trigger_json["TriggerPulseTrainCount"],
                "mode": mode,
                "profile_index": trigger_json["ProfileIndex"],
                "profile_increment": bool(trigger_json["ProfileIncrement"]),
            }
            return trigger_dict

    def start_trigger(self) -> bool:
        """
        Start the trigger on the TX device.

        Returns:
            bool: True if the trigger was started successfully, False otherwise.

        Raises:
            ValueError: If the UART is not connected.
            Exception: If an error occurs while starting the trigger.
        """
        try:
            if self.uart.demo_mode:
                return True

            if not self.uart.is_connected():
                raise ValueError("TX Device not connected")

            r = self.uart.send_packet(
                id=None, packetType=OW_CONTROLLER, command=OW_CTRL_START_SWTRIG, addr=0, data=None
            )
            self.uart.clear_buffer()
            # r.print_packet()
            if r.packet_type == OW_ERROR:
                logger.error("Error starting trigger")
                return False
            else:
                return True
        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def stop_trigger(self) -> bool:
        """
        Stop the trigger on the TX device.

        This method sends a command to stop the software trigger on the TX device.
        It checks the device's connection status and handles errors appropriately.

        Returns:
            bool: True if the trigger was successfully stopped, False otherwise.

        Raises:
            ValueError: If the UART is not connected.
            Exception: If an error occurs during the operation.
        """
        try:
            if self.uart.demo_mode:
                return True

            # Check if the device is connected
            if not self.uart.is_connected():
                raise ValueError("TX Device not connected")

            # Send the STOP_SWTRIG command to the device
            r = self.uart.send_packet(id=None, packetType=OW_CONTROLLER, command=OW_CTRL_STOP_SWTRIG, addr=0, data=None)

            # Clear the UART buffer to prepare for further communication
            self.uart.clear_buffer()

            # Log the received packet for debugging purposes
            # r.print_packet()

            # Check the packet type to determine success
            if r.packet_type == OW_ERROR:
                logger.error("Error stopping trigger")
                return False
            else:
                return True
        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def soft_reset(self, module: int = 0) -> bool:
        """
        Perform a soft reset on the TX device.

        Returns:
            bool: True if the reset was successful, False otherwise.

        Raises:
            ValueError: If the UART is not connected.
            Exception: If an error occurs while resetting the device.
        """
        try:
            if self.uart.demo_mode:
                return True

            if not self.uart.is_connected():
                raise ValueError("TX Device not connected")

            self.uart.send_packet(id=None, packetType=OW_CONTROLLER, command=OW_CMD_RESET, addr=module)
            self.uart.clear_buffer()
            return True
        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def enter_dfu(self, module: int = 0) -> bool:
        """
        Perform a soft reset to enter DFU mode on TX device.

        Returns:
            bool: True if the reset was successful, False otherwise.

        Raises:
            ValueError: If the UART is not connected.
            Exception: If an error occurs while resetting the device.
        """
        try:
            if self.uart.demo_mode:
                return True

            if not self.uart.is_connected():
                raise ValueError("TX Device not connected")

            r = self.uart.send_packet(id=None, packetType=OW_CONTROLLER, command=OW_CMD_DFU, addr=module)
            self.uart.clear_buffer()
            if r is None:
                # Device disconnected immediately after reset — expected for DFU entry
                return True
            if r.packet_type == OW_ERROR:
                logger.error("Error setting DFU mode for device")
                return False
            else:
                return True
        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def async_mode(self, enable: bool | None = None) -> bool:
        """
        Enable or disable asynchronous mode for the TX device.

        Args:
            enable (bool | None): If True, enable async mode; if False, disable it; if None read the current state.

        Returns:
            bool: True if async mode is enabled, False otherwise.

        Raises:
            ValueError: If the UART is not connected.
            Exception: If an error occurs while setting async mode.
        """
        try:
            if self.uart.demo_mode:
                return True

            if not self.uart.is_connected():
                raise ValueError("TX Device not connected")

            if enable is not None:
                if enable:
                    payload = struct.pack("<B", 1)
                else:
                    payload = struct.pack("<B", 0)
            else:
                payload = None

            r = self.uart.send_packet(id=None, packetType=OW_CONTROLLER, command=OW_CMD_ASYNC, addr=0, data=payload)
            self.uart.clear_buffer()
            # r.print_packet()
            if r.packet_type == OW_ERROR:
                raise RuntimeError("Error running async mode command for device")
            else:
                return r.reserved == 1  # reserved field indicates async mode status

        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise

    def get_tx_module_count(self) -> int:
        """
        Retrieve the number of detected Transmit modules.

        Args:
        Returns:
            tx_module_count: number of transmitter modules connected.

        Raises:
            ValueError: If the UART is not connected.
            Exception: If an error occurs during enumeration.
        """
        tx_module_count = 0
        try:
            if self.uart.demo_mode:
                tx_module_count = 1
            else:
                if not self.uart.is_connected():
                    raise ValueError("TX Device not connected")

                r = self.uart.send_packet(id=None, packetType=OW_TX7332, command=OW_TX7332_DEVICE_COUNT, addr=0)
                self.uart.clear_buffer()
                # r.print_packet()
                if r.packet_type != OW_ERROR and r.data_len == 1:
                    tx_module_count = r.data[0]
                else:
                    logger.error("Error retrieving TX module count.")
            logger.info("TX Module Count: %d", tx_module_count)
            return tx_module_count
        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def enum_tx7332_devices(self, num_devices: int | None = None) -> int:
        """
        Enumerate TX7332 devices connected to the TX device.

        Args:
            num_transmitters (int): The number of transmitters expected to be enumerated. If None, the number of
                transmitters will be determined from the response. If provided and the number enumerated does not
                match the expected number, an error will be raised. If the UART is in demo mode, this argument is
                used to set the number of transmitters for the demo (or set to a default if omitted/None)

        Returns:
            n_transmitters: number of devices detected.

        Raises:
            ValueError: If the UART is not connected.
            Exception: If an error occurs during enumeration.
        """
        try:
            if self.uart.demo_mode:
                num_detected_devices = num_devices
            else:
                if not self.uart.is_connected():
                    raise ValueError("TX Device not connected")

                r = self.uart.send_packet(id=None, packetType=OW_TX7332, command=OW_TX7332_ENUM)
                self.uart.clear_buffer()
                # r.print_packet()
                if r.packet_type != OW_ERROR and r.reserved > 0:
                    num_detected_devices = r.reserved
                else:
                    logger.error("Error enumerating TX devices.")
                if num_devices is not None and num_detected_devices != num_devices:
                    raise ValueError(f"Expected {num_devices} devices, but detected {num_detected_devices} devices")
            self.tx_registers = TxDeviceRegisters(
                num_transmitters=num_detected_devices, module_invert=self.module_invert
            )
            logger.info("TX Device Count: %d", num_detected_devices)
            return num_detected_devices
        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def set_module_invert(self, module_invert: bool | list[bool]) -> None:
        """
        Set the module invert configuration for the TX device.

        Args:
            module_invert (bool | List[bool]): The module invert configuration to set.
        """
        self.module_invert = module_invert
        if self.tx_registers is not None:
            self.tx_registers.module_invert = module_invert

    def demo_tx7332(self, identifier: int) -> bool:
        """
        Sets all TX7332 chip registers with a test waveform.

        Returns:
            bool: True if all chips are programmed successfully, False otherwise.

        Raises:
            ValueError: If the UART is not connected.
        """
        try:
            if self.uart.demo_mode:
                return True

            if not self.uart.is_connected():
                raise ValueError("TX Device not connected")

            r = self.uart.send_packet(id=None, addr=identifier, packetType=OW_TX7332, command=OW_TX7332_DEMO)
            self.uart.clear_buffer()
            # r.print_packet()
            if r.packet_type == OW_ERROR:
                logger.error("Error demoing TX devices")
                return False

            return True
        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def write_register(self, identifier: int, address: int, value: int) -> bool:
        """
        Write a value to a register in the TX device.

        Args:
            address (int): The register address to write to.
            value (int): The value to write to the register.

        Returns:
            bool: True if the write operation was successful, False otherwise.

        Raises:
            ValueError: If the device is not connected, or the identifier is invalid.
            Exception: If an unexpected error occurs during the operation.
        """
        try:
            if self.uart.demo_mode:
                return True

            # Check if the UART is connected
            if not self.uart.is_connected():
                raise ValueError("TX Device not connected")

            # Validate the identifier
            if identifier < 0:
                raise ValueError("TX Chip address NOT SET")

            # Pack the address and value into the required format
            try:
                data = struct.pack("<HI", address, value)
            except struct.error as e:
                logger.error("Error packing address and value: %s", e)
                raise ValueError("Invalid address or value format") from e

            # Send the write command to the device
            r = self.uart.send_packet(id=None, packetType=OW_TX7332, command=OW_TX7332_WREG, addr=identifier, data=data)

            # Clear UART buffer after sending the packet
            self.uart.clear_buffer()

            # Check the response for errors
            if r.packet_type == OW_ERROR:
                logger.error("Error writing TX register value")
                return False

            logger.debug("Successfully wrote value 0x%08X to register 0x%04X", value, address)
            return True

        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def read_register(self, identifier: int, address: int) -> int:
        """
        Read a register value from the TX device.

        Args:
            address (int): The register address to read.

        Returns:
            int: The value of the register if successful, or 0 on failure.

        Raises:
            ValueError: If the identifier is not set or is out of range.
            Exception: If an unexpected error occurs during the operation.
        """
        try:
            if self.uart.demo_mode:
                return 45

            if not self.uart.is_connected():
                raise ValueError("TX Device not connected")

            # Validate the identifier
            if identifier < 0:
                raise ValueError("TX Chip address NOT SET")

            # Pack the address into the required format
            try:
                data = struct.pack("<H", address)
            except struct.error as e:
                logger.error("Error packing address %s: %s", address, e)
                raise ValueError("Invalid address format") from e

            # Send the read command to the device
            r = self.uart.send_packet(id=None, packetType=OW_TX7332, command=OW_TX7332_RREG, addr=identifier, data=data)

            # Clear UART buffer after sending the packet
            self.uart.clear_buffer()
            # r.print_packet()
            # Check for errors in the response
            if r.packet_type == OW_ERROR:
                logger.error("Error reading TX register value")
                return 0

            # Verify data length and unpack the register value
            if r.data_len == 4:
                try:
                    value = struct.unpack("<I", r.data)[0]
                except struct.error as e:
                    logger.error("Error unpacking register value: %s", e)
                    return 0
            else:
                logger.error("Unexpected data length: %s", r.data_len)
                return 0

            logger.debug("Successfully read value 0x%08X from register 0x%04X", value, address)
            return value
        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def write_block(self, identifier: int, start_address: int, reg_values: list[int]) -> bool:
        """
        Write a block of register values to the TX device.

        Args:
            start_address (int): The starting register address to write to.
            reg_values (List[int]): List of register values to write.

        Returns:
            bool: True if the block write operation was successful, False otherwise.

        Raises:
            ValueError: If the device is not connected, the identifier is invalid, or parameters are out of range.
        """
        try:
            if self.uart.demo_mode:
                return True

            # Ensure the UART connection is active
            if not self.uart.is_connected():
                raise ValueError("TX Device not connected")

            # Validate the identifier
            if identifier < 0:
                raise ValueError("TX Chip address NOT SET")

            # Validate the reg_values list
            if not reg_values or not isinstance(reg_values, list):
                raise ValueError("Invalid register values: Must be a non-empty list of integers")
            if any(not isinstance(value, int) for value in reg_values):
                raise ValueError("Invalid register values: All elements must be integers")

            # Configure chunking for large blocks
            max_regs_per_block = 62  # Maximum registers per block due to payload size
            num_chunks = (len(reg_values) + max_regs_per_block - 1) // max_regs_per_block
            logger.debug("Write Block: Total chunks = %s", num_chunks)

            # Write each chunk
            for i in range(num_chunks):
                chunk_start = i * max_regs_per_block
                chunk_end = min((i + 1) * max_regs_per_block, len(reg_values))
                chunk = reg_values[chunk_start:chunk_end]

                # Pack the chunk into the required data format
                try:
                    data_format = "<HBB" + "I" * len(
                        chunk
                    )  # Start address (H), chunk length (B), reserved (B), values (I...)
                    data = struct.pack(data_format, start_address + chunk_start, len(chunk), 0, *chunk)
                except struct.error as e:
                    logger.error("Error packing data for chunk %s: %s", i, e)
                    return False

                # Send the packet
                r = self.uart.send_packet(
                    id=None, packetType=OW_TX7332, command=OW_TX7332_WBLOCK, addr=identifier, data=data
                )

                # Clear the UART buffer after sending
                self.uart.clear_buffer()
                # r.print_packet()
                # Check for errors in the response
                if r.packet_type == OW_ERROR:
                    logger.error("Error writing TX block at chunk %s", i)
                    return False

            logger.debug("Block write successful")
            return True

        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handleected error in write_block: {e}")
            return False

    def read_block(self, identifier: int, start_address: int, count: int) -> list[int] | None:
        """
        Read a block of consecutive register values from the TX device.

        Args:
            identifier (int): TX chip index.
            start_address (int): The starting register address to read from.
            count (int): Number of registers to read.

        Returns:
            List[int]: List of register values, or None on error.

        Raises:
            ValueError: If the device is not connected or parameters are invalid.
            Exception: If an unexpected error occurs.
        """
        try:
            if self.uart.demo_mode:
                return [0] * count

            if not self.uart.is_connected():
                raise ValueError("TX Device not connected")

            if identifier < 0:
                raise ValueError("TX Chip address NOT SET")

            if count <= 0 or count > 62:
                raise ValueError(f"count must be 1-62, got {count}")

            # Request payload: uint16_t start_addr, uint8_t count, uint8_t reserved
            data = struct.pack("<HBB", start_address, count, 0)

            r = self.uart.send_packet(
                id=None, packetType=OW_TX7332, command=OW_TX7332_RBLOCK, addr=identifier, data=data
            )
            self.uart.clear_buffer()

            if r.packet_type == OW_ERROR:
                logger.error("Error reading TX register block")
                return None

            expected_len = count * 4
            if r.data_len != expected_len:
                logger.error("Unexpected data length: %s, expected %s", r.data_len, expected_len)
                return None

            values = list(struct.unpack(f"<{count}I", r.data))
            logger.debug("read_block: %s regs from 0x%04X on tx %s", count, start_address, identifier)
            return values

        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise

    def write_register_verify(self, address: int, value: int) -> bool:
        """
        Write a value to a register in the TX device with verification.

        Args:
            address (int): The register address to write to.
            value (int): The value to write to the register.

        Returns:
            bool: True if the write operation was successful, False otherwise.

        Raises:
            ValueError: If the device is not connected, or the identifier is invalid.
            Exception: If an unexpected error occurs during the operation.
        """
        try:
            if self.uart.demo_mode:
                return True

            # Check if the UART is connected
            if not self.uart.is_connected():
                raise ValueError("TX Device not connected")

            # Validate the identifier
            if self.identifier < 0:
                raise ValueError("TX Chip address NOT SET")

            # Pack the address and value into the required format
            try:
                data = struct.pack("<HI", address, value)
            except struct.error as e:
                logger.error("Error packing address and value: %s", e)
                raise ValueError("Invalid address or value format") from e

            # Send the write command to the device
            r = self.uart.send_packet(
                id=None, packetType=OW_TX7332, command=OW_TX7332_VWREG, addr=self.identifier, data=data
            )

            # Clear UART buffer after sending the packet
            self.uart.clear_buffer()

            # Check the response for errors
            if r.packet_type == OW_ERROR:
                logger.error("Error verifying writing TX register value")
                return False

            logger.debug("Successfully wrote value 0x%08X to register 0x%04X", value, address)
            return True

        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def write_block_verify(self, start_address: int, reg_values: list[int]) -> bool:
        """
        Write a block of register values to the TX device with verification.

        Args:
            start_address (int): The starting register address to write to.
            reg_values (List[int]): List of register values to write.

        Returns:
            bool: True if the block write operation was successful, False otherwise.

        Raises:
            ValueError: If the device is not connected, the identifier is invalid, or parameters are out of range.
        """
        try:
            if self.uart.demo_mode:
                return True

            # Ensure the UART connection is active
            if not self.uart.is_connected():
                raise ValueError("TX Device not connected")

            # Validate the identifier
            if self.identifier < 0:
                raise ValueError("TX Chip address NOT SET")

            # Validate the reg_values list
            if not reg_values or not isinstance(reg_values, list):
                raise ValueError("Invalid register values: Must be a non-empty list of integers")
            if any(not isinstance(value, int) for value in reg_values):
                raise ValueError("Invalid register values: All elements must be integers")

            # Configure chunking for large blocks
            max_regs_per_block = 62  # Maximum registers per block due to payload size
            num_chunks = (len(reg_values) + max_regs_per_block - 1) // max_regs_per_block
            logger.debug("Write Block: Total chunks = %s", num_chunks)

            # Write each chunk
            for i in range(num_chunks):
                chunk_start = i * max_regs_per_block
                chunk_end = min((i + 1) * max_regs_per_block, len(reg_values))
                chunk = reg_values[chunk_start:chunk_end]

                # Pack the chunk into the required data format
                try:
                    data_format = "<HBB" + "I" * len(
                        chunk
                    )  # Start address (H), chunk length (B), reserved (B), values (I...)
                    data = struct.pack(data_format, start_address + chunk_start, len(chunk), 0, *chunk)
                except struct.error as e:
                    logger.error("Error packing data for chunk %s: %s", i, e)
                    return False

                # Send the packet
                r = self.uart.send_packet(
                    id=None, packetType=OW_TX7332, command=OW_TX7332_VWBLOCK, addr=self.identifier, data=data
                )

                # Clear the UART buffer after sending
                self.uart.clear_buffer()

                # Check for errors in the response
                if r.packet_type == OW_ERROR:
                    logger.error("Error verifying writing TX block at chunk %s", i)
                    return False

            logger.debug("Block write successful")
            return True

        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def set_solution(
        self,
        pulse: dict,
        delays: np.ndarray,
        apodizations: np.ndarray,
        sequence: dict,
        trigger_mode: TriggerModeOpts = "sequence",
        profile_index: int = 1,
        profile_increment: bool = True,
    ):
        """
        Set the solution parameters on the TX device.

        Args:
            pulse (Dict): The pulse parameters to set.
            delays (list): The delays to set.
            apodizations (list): The apodizations to set.
            sequence (Dict): The sequence parameters to set.
            mode: The trigger mode to use.
            profile_index (int): The pulse profile to use.
            profile_increment (bool): Whether to increment the pulse profile.
        """
        delays = np.array(delays)
        if delays.ndim == 1:
            delays = delays.reshape(1, -1)
        apodizations = np.array(apodizations)
        if apodizations.ndim == 1:
            apodizations = apodizations.reshape(1, -1)
        n = delays.shape[0]
        n_elements = delays.shape[1]
        n_required_devices = int(n_elements / NUM_CHANNELS)
        n_detected_tx = self.enum_tx7332_devices(num_devices=n_required_devices)
        n_modules = n_detected_tx / TRANSMITTERS_PER_MODULE
        logger.debug("Detected %s TX devices (%s modules)", n_detected_tx, n_modules)
        if n_required_devices != n_detected_tx:
            errmsg = f"Number of detected TX devices ({n_detected_tx}) does not match required ({n_required_devices})"
            logger.exception(errmsg)
            raise OSError(errmsg)

        if n != apodizations.shape[0]:
            raise ValueError("Delays and apodizations must have the same number of rows")
        if n > 1:
            raise NotImplementedError("Multiple foci not supported yet")
        for profile in range(n):
            duty_cycle = DEFAULT_PATTERN_DUTY_CYCLE * max(apodizations[profile, :]) * pulse["amplitude"]
            pulse_profile = Tx7332PulseProfile(
                profile=profile + 1,
                frequency=pulse["frequency"],
                cycles=int(pulse["duration"] * pulse["frequency"]),
                duty_cycle=duty_cycle,
            )
            self.tx_registers.add_pulse_profile(pulse_profile)
            delay_profile = Tx7332DelayProfile(
                profile=profile + 1, delays=delays[profile, :], apodizations=apodizations[profile, :]
            )
            self.tx_registers.add_delay_profile(delay_profile)
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

        # Buffer the pulse and delay profiles in the microcontroller(s) so that they
        # can be used to switch profiles on trigger detection. These dicts are computed
        # here as a placeholder for future firmware commands; they are not yet sent.
        _delay_ctrl = {
            profile: self.tx_registers.get_delay_control_registers(profile)
            for profile in self.tx_registers.configured_delay_profiles()
        }
        _pulse_ctrl = {
            profile: self.tx_registers.get_pulse_control_registers(profile)
            for profile in self.tx_registers.configured_pulse_profiles()
        }
        logger.debug("Buffered %s delay and %s pulse profiles", len(_delay_ctrl), len(_pulse_ctrl))

    def apply_all_registers(self):
        """
        Apply all registers to the TX device.

        Raises:
            ValueError: If the device is not connected.
        """
        if self.uart.demo_mode:
            return True

        try:
            if not self.uart.is_connected():
                raise ValueError("TX Device not connected")
            registers = self.tx_registers.get_registers(pack=True, pack_single=True)
            for txi, txregs in enumerate(registers):
                for addr, reg_values in txregs.items():
                    if not self.write_block(identifier=txi, start_address=addr, reg_values=reg_values):
                        logger.error("Error applying TX CHIP ID: %s registers", txi)
                        return False
            return True

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def write_ti_config_to_tx_device(self, file_path: str, txchip_id: int) -> bool:
        """
        Parse a TI configuration file and write the register values to the TX device.

        Args:
            file_path (str): Path to the TI configuration file.
            txchip_id (int): The ID of the TX chip to write the registers to.

        Returns:
            bool: True if all registers were written successfully, False otherwise.
        """
        try:
            # Check if UART is connected
            if not self.uart.is_connected():
                raise ValueError("TX Device not connected")

            # Parse the TI configuration file
            parsed_registers = self.__parse_ti_cfg_file(file_path)
            if not parsed_registers:
                logger.error("No registers parsed from the TI configuration file.")
                return False

            # Write each register to the TX device
            for group, addr, value in parsed_registers:
                logger.debug("Writing to %-20s | Address: 0x%02X | Value: 0x%08X", group, addr, value)
                if not self.write_register(identifier=txchip_id, address=addr, value=value):
                    logger.error(
                        "Failed to write to TX CHIP ID: %s | Register: 0x%02X | Value: 0x%08X",
                        txchip_id,
                        addr,
                        value,
                    )
                    return False

            logger.debug("Successfully wrote all registers to the TX device.")
            return True

        except FileNotFoundError as e:
            logger.error("TI configuration file not found: %s. Error: %s", file_path, e)
            raise
        except ValueError as e:
            logger.error("Invalid input or device state: %s", e)
            raise
        except Exception as e:
            logger.error("Unexpected error while writing TI config to TX Device: %s", e)
            raise

    # ------------------------------------------------------------------
    # Firmware update (delegates to LIFUDFU.LIFUDFUManager)
    # ------------------------------------------------------------------

    def get_module_count(self) -> int:
        """Return the number of connected LIFU transmitter modules (including master).

        Sends ``OW_CTRL_GET_MODULE_COUNT`` (0x10) to the firmware. Falls back to
        deriving the count from the TX7332 chip count when the firmware does not
        yet support the command.
        """
        try:
            if self.uart.demo_mode:
                return 1

            if not self.uart.is_connected():
                logger.error("TX Device not connected")
                return 0

            r = self.uart.send_packet(id=None, packetType=OW_CMD, command=OW_CTRL_GET_MODULE_COUNT, addr=0)
            self.uart.clear_buffer()

            if r.packet_type != OW_ERROR and r.data_len >= 1:
                count = r.data[0]
                logger.info("Module count from firmware: %d", count)
                return count

            # Fallback: TX7332 chip count / 2
            logger.info("OW_CTRL_GET_MODULE_COUNT not supported; falling back to TX7332 count")
            module_count = self.get_tx_module_count()
            return module_count

        except Exception as e:
            logger.error("Error getting module count: %s", e)
            return 0

    def update_firmware(
        self,
        module: int,
        package_file: str,
        vid: int = 0x0483,
        pid: int = 0xDF11,
        libusb_dll: str | None = None,
        i2c_addr: int = 0x72,
        dfu_wait_s: float = 5.0,
        device_type: str = "transmitter",
        progress_callback=None,
    ) -> bool:
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

        if not self.uart.is_connected():
            raise ValueError("TX Device not connected")

        mgr = LIFUDFUManager(uart=self.uart)
        mgr.update_module(
            module=module,
            package_file=package_file,
            enter_dfu_fn=self.enter_dfu,
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
        print("TX Device Information")
        print("  UART Port:")
        self.uart.print()
