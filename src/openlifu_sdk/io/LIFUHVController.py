from __future__ import annotations

from asyncio import timeout
import logging
import struct

from openlifu_sdk.io.LIFUConfig import (
    CONTROLLER_COMMANDS,
    DEFAULT_TIMEOUT,
    GLOBAL_COMMANDS,
    OW_CONSOLE_PID,
    OW_ERROR,
    OW_POWER,
    OW_POWER_12V_OFF,
    OW_POWER_12V_ON,
    OW_POWER_GET_12VON,
    OW_POWER_GET_FAN,
    OW_POWER_GET_HV,
    OW_POWER_GET_HVON,
    OW_POWER_GET_RGB,
    OW_POWER_GET_TEMP1,
    OW_POWER_GET_TEMP2,
    OW_POWER_HV_ENABLE,
    OW_POWER_HV_OFF,
    OW_POWER_HV_ON,
    OW_POWER_RAW_DAC,
    OW_POWER_SET_DACS,
    OW_POWER_SET_FAN,
    OW_POWER_SET_HV,
    OW_POWER_SET_RGB,
    OW_POWER_VMON,
    OW_VID,
    POWER_COMMANDS,
)
from openlifu_sdk.io.component import OWComponent, register_command_packet_types, register_command_packet_types
from openlifu_sdk.io.uart import OWUart
from openlifu_sdk.util.hwid import format_hwid

logger = logging.getLogger(__name__)
ch = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)
logger.propagate = True

class HVController(OWComponent):
    def __init__(self,  vid: int = OW_VID, pid: int = OW_CONSOLE_PID,
                 baudrate: int = 921600, timeout: float = DEFAULT_TIMEOUT, test_mode: bool = False):
        """
        Initialize the HVController.

        Args:
            uart (OWUart): The OWUart instance for communication.
        """
        super().__init__(
            vid, pid,
            supported_commands=GLOBAL_COMMANDS | CONTROLLER_COMMANDS | POWER_COMMANDS,
            baudrate=baudrate, timeout=timeout, desc="HV",
        )
        
        register_command_packet_types(POWER_COMMANDS, OW_POWER)

        self._test_mode = test_mode

        # Initialize the high voltage state (should get this from device)
        self.output_voltage = 0.0
        self.is_hv_on = False
        self.is_12v_on = False

        self.supply_voltage = None

    def get_temperature1(self) -> float:
        """
        Retrieve the temperature reading from the HV controller.

        Returns:
            float: Temperature value in Celsius.

        Raises:
            ValueError: If the UART is not connected.
            Exception: If an error occurs or the received data length is invalid.
        """
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_GET_TEMP1)
        r.print_packet()
        if r is None:
            raise RuntimeError("LIFUConsole: temperature1 request timed out")
        if r.data_len == 4:
            return round(struct.unpack("<f", r.data)[0], 2)
        raise ValueError("Invalid data length for temperature1")

    def get_temperature2(self) -> float:
        """
        Retrieve the temperature reading from the HV controller.

        Returns:
            float: Temperature value in Celsius.

        Raises:
            ValueError: If the UART is not connected or invalid data is received.
        """
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_GET_TEMP2)
        r.print_packet()
        if r is None:
            raise RuntimeError("HVController: temperature2 request timed out")
        if r.data_len == 4:
            return round(struct.unpack("<f", r.data)[0], 2)
        raise ValueError("Invalid data length for temperature2")

    def turn_12v_off(self):
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_12V_OFF)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            logger.error("Error turning off 12V")
            return False
        logger.info("12V turned off")
        self.is_12v_on = False
        return True

    def turn_12v_on(self):
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_12V_ON)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            logger.error("Error turning on 12V")
            return False
        logger.info("12V turned on")
        self.is_12v_on = True
        return True

    def get_12v_status(self):
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_GET_12VON)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            raise RuntimeError("HVController: 12V status request failed")
        return r.reserved == 1

    def turn_hv_on(self, timeout: float | None = 30.0) -> bool:
        """
        Turn on the high voltage.
        """
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_HV_ON, timeout=timeout)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            logger.error("Error turning on HV")
            return False
        logger.info("HV turned on")
        self.is_hv_on = True
        return True

    def wait_for_settle(self, range_volts: float = 2, settle_time: float = 0.5, timeout: float = 15.0, polling_interval: float = 0.1):
        """
        Wait for the high voltage to settle to within a target range after turning on.

        Args:
            range_volts (float): The acceptable voltage range in volts.
            settle_time (float): The time in seconds to wait for the voltage to settle.
            timeout (float): The maximum time in seconds to wait before giving up.
            polling_interval (float): The interval in seconds between voltage checks.

        Returns:
            bool: True if the voltage settled successfully, False if it timed out or an error occurred.
        """
        import time

        start_time = time.time()
        within_target_start_time = None
        within_range = False
        if self.is_hv_on:
            target_voltage = self.supply_voltage
        else:
            target_voltage = 0
        while time.time() - start_time < timeout:
            loop_time = time.time()
            current_voltage = self.get_voltage()
            if current_voltage is None:
                raise ValueError("Failed to read voltage during settle wait.")
            logger.debug(f"Current voltage: {current_voltage:.2f} V")
            if abs(current_voltage - target_voltage) <= range_volts:
                if not within_range:
                    logger.debug(f"Voltage ({current_voltage:.2f} V) is within target range of {target_voltage} ± {range_volts} V. Starting {settle_time:0.2f} S settle timer.")
                    within_target_start_time = time.time()
                    within_range = True
                elif time.time() - within_target_start_time >= settle_time:
                    logger.info(f"Voltage ({current_voltage:.2f} V) has settled successfully.")
                    return
            else:
                if within_range:
                    logger.warning(f"Voltage ({current_voltage:.2f} V) went out of target range of {target_voltage} ± {range_volts} V. Resetting {settle_time:0.2f} S settle timer.")
                within_range = False
                within_target_start_time = None
            time.sleep(polling_interval - (max(time.time() - loop_time, 0)))  # Adjust sleep to maintain consistent polling interval
        raise TimeoutError(f"Voltage ({current_voltage:.2f} V) failed to stabilize for {settle_time:0.2f}S within {target_voltage} ± {range_volts} V within {timeout} S.")    


    def turn_hv_off(self, timeout: float | None = 5.0) -> bool:
        """
        Turn off the high voltage.
        """
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_HV_OFF, timeout=timeout)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            logger.error("Error turning off HV")
            return False
        logger.info("HV turned off")
        self.is_hv_on = False
        return True

    def get_hv_status(self) -> bool:
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_GET_HVON)
        if r is None or r.packet_type == OW_ERROR:
            raise RuntimeError("HVController: HV status request failed")
        r.print_packet()
        return r.reserved == 1

    def set_voltage(self, voltage: float) -> bool:
        """
        Set the output voltage.

        Args:
            voltage (float): The desired output voltage.

        Raises:
            ValueError: If the controller is not connected or voltage exceeds supply voltage.
        """
        logger.debug("Setting HV to %.2f", voltage)
        self._require_connected()
        if not 5.0 <= voltage <= 100.0:
            raise ValueError("HV voltage must be between 5 and 100 V")
        data = struct.pack('>f', voltage)
        r = self.send(packet_type=OW_POWER, command=OW_POWER_SET_HV, data=data)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            logger.error("Error setting HV to %.2f", voltage)
            return False
        self.supply_voltage = voltage
        return True
    
    def set_dacs(self, hvp: int, hvm: int, hrp: int, hrm: int) -> bool:
        """
        Set the output voltage.

        Args:
            voltage (float): The desired output voltage.

        Raises:
            ValueError: If the controller is not connected or voltage exceeds supply voltage.
        """
        self._require_connected()

        # Validate and process the DAC input
        if hvp is None:
            hvp = 0
        elif not (0 <= hvp <= 4095):
            raise ValueError("Dac hvp input range is 0 to 4095.")

        if hvm is None:
            hvm = 0
        elif not (0 <= hvm <= 4095):
            raise ValueError("Dac hvm input range is 0 to 4095.")

        if hrp is None:
            hrp = 0
        elif not (0 <= hrp <= 4095):
            raise ValueError("Dac hrp input range is 0 to 4095.")

        if hrm is None:
            hrm = 0
        elif not (0 <= hrm <= 4095):
            raise ValueError("Dac hrm input range is 0 to 4095.")

        try:
            # logger.info("Setting DAC Value %d.", dac_input)
            # Pack the 12-bit DAC input into two bytes
            data = bytes(
                [
                    (hvp >> 8) & 0xFF,  # High byte (most significant bits)
                    hvp & 0xFF,  # Low byte (least significant bits)
                    (hrp >> 8) & 0xFF,  # High byte (most significant bits)
                    hrp & 0xFF,  # Low byte (least significant bits)
                    (hvm >> 8) & 0xFF,  # High byte (most significant bits)
                    hvm & 0xFF,  # Low byte (least significant bits)
                    (hrm >> 8) & 0xFF,  # High byte (most significant bits)
                    hrm & 0xFF,  # Low byte (least significant bits)
                ]
            )

            r = self.send(packet_type=OW_POWER, command=OW_POWER_SET_DACS, data=data)
            
            if r is None or r.packet_type == OW_ERROR:
                logger.error("Error setting DACS")
                return False
            else:
                return True

        except ValueError as v:
            logger.error("ValueError: %s", v)
            raise  # Re-raise the exception for the caller to handle

        except Exception as e:
            logger.error("Unexpected error during process: %s", e)
            raise  # Re-raise the exception for the caller to handle

    def get_voltage(self) -> float:
        """
        Get the current output voltage setting.

        Returns:
            float: The current output voltage.

        Raises:
            ValueError: If the controller is not connected.
        """
        r = self.send(packet_type=OW_POWER, command=OW_POWER_GET_HV)
        r.print_packet()
        if r is None:
            raise RuntimeError("HVController: get HV request timed out")
        if r.data_len == 4:
            return round(struct.unpack("<f", r.data)[0], 2)
        raise ValueError("Invalid data length for HV reading")

    def set_fan_speed(self, fan_id: int = 0, fan_speed: int = 50) -> int:
        """
        Get the current output fan percentage.

        Args:
            fan_id (int): The desired fan to set (default is 0). bottom fans (0), and top fans (1).
            fan_speed (int): The desired fan speed (default is 50).

        Returns:
            int: The current output fan percentage.

        Raises:
            ValueError: If the controller is not connected.
        """
        self._require_connected()
        if fan_id not in (0, 1):
            raise ValueError("Invalid fan ID. Must be 0 or 1")
        if not 0 <= fan_speed <= 100:
            raise ValueError("Invalid fan speed. Must be 0 to 100")
        r = self.send(packet_type=OW_POWER, command=OW_POWER_SET_FAN, addr=fan_id,
                      data=bytearray([fan_speed & 0xFF]))
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            logger.error("Error setting fan %d speed", fan_id)
            return -1
        logger.info("Set fan %d speed to %d", fan_id, fan_speed)
        return fan_speed

    def get_fan_speed(self, fan_id: int = 0) -> int:
        """
        Get the current output fan percentage.

        Args:
            fan_id (int): The desired fan to read (default is 0). bottom fans (0), and top fans (1).

        Returns:
            int: The current output fan percentage.

        Raises:
            ValueError: If the controller is not connected.
        """
        self._require_connected()
        if fan_id not in (0, 1):
            raise ValueError("Invalid fan ID. Must be 0 or 1")
        r = self.send(packet_type=OW_POWER, command=OW_POWER_GET_FAN, addr=fan_id)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            logger.error("Error getting fan %d speed", fan_id)
            return -1
        if r.data_len >= 1:
            return r.data[0]
        raise ValueError("Invalid data length for fan reading")

    def set_rgb_led(self, rgb_state: int) -> bool:
        """
        Set the RGB LED state.

        Args:
            rgb_state (int): The desired RGB state (0 = OFF, 1 = RED, 2 = BLUE, 3 = GREEN).

        Returns:
            int: The current RGB state after setting.

        Raises:
            ValueError: If the controller is not connected or the RGB state is invalid.
        """
        self._require_connected()

        if rgb_state not in [0, 1, 2, 3]:
            raise ValueError(
                "Invalid RGB state. Must be 0 (OFF), 1 (RED), 2 (BLUE), or 3 (GREEN)"
            )

        r = self.send(packet_type=OW_POWER, command=OW_POWER_SET_RGB, reserved=rgb_state)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            logger.error("Error setting RGB state")
            return False
        return True

    def get_rgb_led(self) -> int:
        """
        Get the current RGB LED state.

        Returns:
            int: The current RGB state (0 = OFF, 1 = RED, 2 = BLUE, 3 = GREEN).

        Raises:
            ValueError: If the controller is not connected.
        """
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_GET_RGB)
        if r is None or r.packet_type == OW_ERROR:
            logger.error("Error getting RGB LED state")
            return -1
        return r.reserved

    def get_vmon_values(self) -> list[dict]:
        """
        Retrieve the voltage monitor readings from the console device.

        Returns:
            list[dict]: A list of 8 dictionaries, one for each channel, containing:
                - channel (int): Channel number (0-7)
                - raw_adc (int): Raw ADC reading (uint16)
                - reserved (int): Reserved value (uint16)
                - voltage (float): Voltage reading in volts
                - converted_voltage (float): Converted voltage value

        Raises:
            ValueError: If the UART is not connected.
            Exception: If an error occurs or the received data length is invalid.
        """
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_VMON)
        r.print_packet()
        if r is None:
            raise RuntimeError("LIFUConsole: VMON request timed out")
        if r.data_len != 80:
            raise ValueError(
                f"Invalid VMON data length: expected 80 bytes, got {r.data_len}"
            )
        raw_values = struct.unpack_from("<8H", r.data, 0)
        voltages = struct.unpack_from("<8f", r.data, 16)
        converted_voltages = struct.unpack_from("<8f", r.data, 48)
        return [
            {
                "channel": i,
                "raw_adc": raw_values[i],
                "voltage": round(voltages[i], 3),
                "converted_voltage": round(converted_voltages[i], 3),
            }
            for i in range(8)
        ]

    def set_raw_dac(self, dac_id: int = 0, dac_value: int = 0) -> int:
        """
        Set Raw DAC value.

        Args:
            dac_id (int): The desired DAC to set (default is 0). Valid IDs are 0, 1, 2, and 3.
            dac_value (int): The desired DAC value (default is 0). Must be between 0 and 4095.
        Returns:
            int: The current output DAC value.

        Raises:
            ValueError: If the controller is not connected.
        """
        self._require_connected()

        if dac_id not in [0, 1, 2 ,3]:
            raise ValueError("Invalid DAC ID. Must be 0, 1, 2, or 3")

        if dac_value not in range(4096):
            raise ValueError("Invalid DAC value. Must be 0 to 4095")

        logger.info("Setting Raw DAC value.")
        data = bytes(
            [
                (dac_value >> 8) & 0xFF,  # High byte (most significant bits)
                dac_value & 0xFF,  # Low byte (least significant bits)
            ]
        )

        r = self.send(
            addr=dac_id,
            packet_type=OW_POWER,
            command=OW_POWER_RAW_DAC,
            data=data,
        )
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            raise RuntimeError("LIFUHVController: RAW DAC request timed out")

        logger.info(f"Set DAC value to {dac_value}")
        return dac_value

    def hv_enable(self, enable: bool = False) -> bool:
        """
        Enable or disable high voltage output.

        Args:
            enable (bool): True to enable HV, False to disable.
        Returns:
            bool: True if the operation was successful, False otherwise.
        Raises:
            ValueError: If the controller is not connected.
        """
        self._require_connected()
        logger.info(f"{'Enabling' if enable else 'Disabling'} high voltage output.")

        r = self.send(
            addr=1 if enable else 0,
            packet_type=OW_POWER,
            command=OW_POWER_HV_ENABLE,
            data=None,
        )

        r.print_packet()

        if r is None or r.packet_type == OW_ERROR:
            raise RuntimeError("LIFUHVController: HV enable request timed out")
        
        logger.info(f"High voltage output {'enabled' if enable else 'disabled'} successfully.")
        return True

