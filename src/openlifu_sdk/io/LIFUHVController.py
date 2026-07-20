from __future__ import annotations

import logging
import struct
import time

from openlifu_sdk.io.LIFUConfig import (
    CONTROLLER_COMMANDS,
    DEFAULT_TIMEOUT,
    GLOBAL_COMMANDS,
    LIFU_ERR_BAD_PAYLOAD_LENGTH,
    OW_CONSOLE_PID,
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
    OW_POWER_SET_RGB_FX,
    OW_POWER_VMON,
    OW_RGB_CYCLE_MAX_COLORS,
    OW_RGB_FX_BREATHE,
    OW_RGB_FX_CYCLE,
    OW_RGB_FX_FADE,
    OW_RGB_FX_FLASH,
    OW_RGB_FX_RAINBOW,
    OW_RGB_FX_SOLID,
    OW_RGB_FX_STOP,
    OW_VID,
    POWER_COMMANDS,
)
from openlifu_sdk.io.component import OWComponent, register_command_packet_types
from openlifu_sdk.io.exceptions import LIFUHVSettleError, LIFUProtocolError

logger = logging.getLogger(__name__)

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
        """Retrieve the primary temperature reading from the HV controller.

        Returns:
            float: Temperature value in Celsius.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError: If the payload length is invalid.
        """
        r = self.send_checked(packet_type=OW_POWER, command=OW_POWER_GET_TEMP1,
                              op="get_temperature1")
        if r.data_len != 4:
            raise LIFUProtocolError(
                f"HV: temperature1 payload length {r.data_len} != 4",
                code=LIFU_ERR_BAD_PAYLOAD_LENGTH,
            )
        return round(struct.unpack("<f", r.data)[0], 2)

    def get_temperature2(self) -> float:
        """Retrieve the secondary temperature reading from the HV controller.

        Returns:
            float: Temperature value in Celsius.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError: If the payload length is invalid.
        """
        r = self.send_checked(packet_type=OW_POWER, command=OW_POWER_GET_TEMP2,
                              op="get_temperature2")
        if r.data_len != 4:
            raise LIFUProtocolError(
                f"HV: temperature2 payload length {r.data_len} != 4",
                code=LIFU_ERR_BAD_PAYLOAD_LENGTH,
            )
        return round(struct.unpack("<f", r.data)[0], 2)

    def turn_12v_off(self) -> bool:
        """Turn off the 12V rail.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        self.send_checked(packet_type=OW_POWER, command=OW_POWER_12V_OFF, op="turn_12v_off")
        logger.info("12V turned off")
        self.is_12v_on = False
        return True

    def turn_12v_on(self) -> bool:
        """Turn on the 12V rail.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        self.send_checked(packet_type=OW_POWER, command=OW_POWER_12V_ON, op="turn_12v_on")
        logger.info("12V turned on")
        self.is_12v_on = True
        return True

    def get_12v_status(self) -> bool:
        """Return True if the 12V rail is on.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        r = self.send_checked(packet_type=OW_POWER, command=OW_POWER_GET_12VON,
                              op="get_12v_status")
        return r.reserved == 1

    def turn_hv_on(self, timeout: float | None = 30.0) -> bool:
        """Turn on the high-voltage rail.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        self.send_checked(packet_type=OW_POWER, command=OW_POWER_HV_ON,
                          timeout=timeout, op="turn_hv_on")
        logger.info("HV turned on")
        self.is_hv_on = True
        return True

    def wait_for_settle(self,
                        range_volts: float = 2,
                        settle_time: float = 0.2,
                        timeout: float = 15.0,
                        polling_interval: float = 0.1) -> bool:
        """Wait for the high voltage to settle to within a target range.

        Raises:
            LIFUHVSettleError: If the voltage does not settle within *timeout*.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError: Propagated from :meth:`get_voltage`.
        """
        start_time = time.time()
        within_target_start_time = None
        within_range = False
        target_voltage = self.supply_voltage if self.is_hv_on else 0
        current_voltage = None
        while time.time() - start_time < timeout:
            loop_time = time.time()
            current_voltage = self.get_voltage()
            logger.debug(f"Current voltage: {current_voltage:.2f} V")
            if abs(current_voltage - target_voltage) <= range_volts:
                if not within_range:
                    logger.debug(f"Voltage ({current_voltage:.2f} V) is within target range of {target_voltage} ± {range_volts} V. Starting {settle_time:0.2f} S settle timer.")
                    within_target_start_time = time.time()
                    within_range = True
                elif time.time() - within_target_start_time >= settle_time:
                    logger.info(f"Voltage ({current_voltage:.2f} V) has settled successfully.")
                    return True
            else:
                if within_range:
                    logger.warning(f"Voltage ({current_voltage:.2f} V) went out of target range of {target_voltage} ± {range_volts} V. Resetting {settle_time:0.2f} S settle timer.")
                within_range = False
                within_target_start_time = None
            time.sleep(max(polling_interval - (time.time() - loop_time), 0))
        measured = f"{current_voltage:.2f}" if current_voltage is not None else "n/a"
        raise LIFUHVSettleError(
            f"Voltage ({measured} V) failed to stabilize for {settle_time:0.2f}S "
            f"within {target_voltage} ± {range_volts} V within {timeout} S."
        )

    def turn_hv_off(self, timeout: float | None = 5.0) -> bool:
        """Turn off the high-voltage rail.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        self.send_checked(packet_type=OW_POWER, command=OW_POWER_HV_OFF,
                          timeout=timeout, op="turn_hv_off")
        logger.info("HV turned off")
        self.is_hv_on = False
        return True

    def get_hv_status(self) -> bool:
        """Return True if the HV rail is on.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        r = self.send_checked(packet_type=OW_POWER, command=OW_POWER_GET_HVON,
                              op="get_hv_status")
        return r.reserved == 1

    def set_voltage(self, voltage: float) -> bool:
        """Set the HV supply voltage.

        Args:
            voltage: Desired output voltage (5.0 – 100.0 V).

        Raises:
            ValueError: If *voltage* is out of range.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        if not 5.0 <= voltage <= 100.0:
            raise ValueError("HV voltage must be between 5 and 100 V")
        logger.debug("Setting HV to %.2f", voltage)
        data = struct.pack('>f', voltage)
        self.send_checked(packet_type=OW_POWER, command=OW_POWER_SET_HV,
                          data=data, timeout=10.0, op="set_voltage")
        self.supply_voltage = voltage
        if self.is_hv_on:
            self.send_checked(packet_type=OW_POWER, command=OW_POWER_HV_ON, timeout=10.0, op="reassert_hv_on")
        return True

    def set_dacs(self, hvp: int, hvm: int, hrp: int, hrm: int) -> bool:
        """Set the four HV/HR DAC codes.

        Each argument must be 0-4095 (or ``None``, which is treated as 0).

        Raises:
            ValueError: If any DAC code is out of range.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        def _validate(name: str, value: int | None) -> int:
            if value is None:
                return 0
            if not 0 <= value <= 4095:
                raise ValueError(f"DAC {name} must be 0..4095, got {value}")
            return value

        hvp = _validate("hvp", hvp)
        hvm = _validate("hvm", hvm)
        hrp = _validate("hrp", hrp)
        hrm = _validate("hrm", hrm)

        data = bytes([
            (hvp >> 8) & 0xFF, hvp & 0xFF,
            (hrp >> 8) & 0xFF, hrp & 0xFF,
            (hvm >> 8) & 0xFF, hvm & 0xFF,
            (hrm >> 8) & 0xFF, hrm & 0xFF,
        ])
        self.send_checked(packet_type=OW_POWER, command=OW_POWER_SET_DACS,
                          data=data, op="set_dacs")
        return True

    def get_voltage(self) -> float:
        """Read the measured HV output voltage.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError: If the payload length is invalid.
        """
        r = self.send_checked(packet_type=OW_POWER, command=OW_POWER_GET_HV,
                              op="get_voltage")
        if r.data_len != 4:
            raise LIFUProtocolError(
                f"HV: get_voltage payload length {r.data_len} != 4",
                code=LIFU_ERR_BAD_PAYLOAD_LENGTH,
            )
        return round(struct.unpack("<f", r.data)[0], 2)

    def set_fan_speed(self, fan_id: int = 0, fan_speed: int = 50) -> int:
        """Set a fan's duty-cycle percentage.

        Args:
            fan_id: 0 = bottom fans, 1 = top fans.
            fan_speed: 0-100 percent.

        Raises:
            ValueError: If *fan_id* or *fan_speed* is out of range.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        if fan_id not in (0, 1):
            raise ValueError("Invalid fan ID. Must be 0 or 1")
        if not 0 <= fan_speed <= 100:
            raise ValueError("Invalid fan speed. Must be 0 to 100")
        self.send_checked(packet_type=OW_POWER, command=OW_POWER_SET_FAN,
                          addr=fan_id, data=bytearray([fan_speed & 0xFF]),
                          op="set_fan_speed")
        logger.info("Set fan %d speed to %d", fan_id, fan_speed)
        return fan_speed

    def get_fan_speed(self, fan_id: int = 0) -> int:
        """Read a fan's current duty-cycle percentage.

        Raises:
            ValueError: If *fan_id* is out of range.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError: If the payload length is invalid.
        """
        if fan_id not in (0, 1):
            raise ValueError("Invalid fan ID. Must be 0 or 1")
        r = self.send_checked(packet_type=OW_POWER, command=OW_POWER_GET_FAN,
                              addr=fan_id, op="get_fan_speed")
        if r.data_len < 1:
            raise LIFUProtocolError(
                f"HV: get_fan_speed payload length {r.data_len} < 1",
                code=LIFU_ERR_BAD_PAYLOAD_LENGTH,
            )
        return r.data[0]

    def set_rgb_led(self, rgb_state: int) -> bool:
        """Set the RGB LED state (0 = OFF, 1 = RED, 2 = GREEN, 3 = BLUE).

        Raises:
            ValueError: If *rgb_state* is out of range.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        if rgb_state not in (0, 1, 2, 3):
            raise ValueError(
                "Invalid RGB state. Must be 0 (OFF), 1 (RED), 2 (GREEN), or 3 (BLUE)"
            )
        self.send_checked(packet_type=OW_POWER, command=OW_POWER_SET_RGB,
                          reserved=rgb_state, op="set_rgb_led")
        return True

    def get_rgb_led(self) -> int:
        """Read the RGB LED state.

        Note: reflects the last basic set_rgb_led() state only; rgb_* effect
        methods do not change it.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        r = self.send_checked(packet_type=OW_POWER, command=OW_POWER_GET_RGB,
                              op="get_rgb_led")
        return r.reserved

    # ------------------------------------------------------------------
    # RGB effects (OW_POWER_SET_RGB_FX). The console drives the LED with a
    # DMA-based 24-bit color engine; these commands select an animation that
    # then runs entirely on the device. Basic set_rgb_led() remains valid
    # and cancels any running effect.
    # ------------------------------------------------------------------

    @staticmethod
    def _check_rgb(r: int, g: int, b: int, period_ms: int) -> None:
        for name, v in (("r", r), ("g", g), ("b", b)):
            if not 0 <= v <= 255:
                raise ValueError(f"Invalid {name} value {v}. Must be 0-255")
        if not 0 <= period_ms <= 0xFFFF:
            raise ValueError(f"Invalid period {period_ms}. Must be 0-65535 ms")

    def _send_rgb_fx(self, fx: int, r: int = 0, g: int = 0, b: int = 0,
                     period_ms: int = 0, extra: bytes = b"", op: str = "rgb_fx") -> bool:
        payload = bytearray(struct.pack("<BBBBH", fx, r, g, b, period_ms)) + extra
        self.send_checked(packet_type=OW_POWER, command=OW_POWER_SET_RGB_FX,
                          data=payload, op=op)
        return True

    def set_rgb_color(self, r: int, g: int, b: int) -> bool:
        """Set a static 24-bit LED color, cancelling any running effect.

        Raises:
            ValueError: If a channel value is out of range.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        self._check_rgb(r, g, b, 0)
        return self._send_rgb_fx(OW_RGB_FX_SOLID, r, g, b, op="set_rgb_color")

    def rgb_fade_to(self, r: int, g: int, b: int, duration_ms: int = 1000) -> bool:
        """Fade smoothly from the current color to (r, g, b) over duration_ms,
        then hold. Fading to (0, 0, 0) is a smooth off.

        Raises:
            ValueError: If a parameter is out of range.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        self._check_rgb(r, g, b, duration_ms)
        return self._send_rgb_fx(OW_RGB_FX_FADE, r, g, b, duration_ms,
                                 op="rgb_fade_to")

    def rgb_breathe(self, r: int, g: int, b: int, period_ms: int = 3000) -> bool:
        """Breathe the given color: brightness ramps 0 -> full -> 0 every
        period_ms, repeating until another command.

        Raises:
            ValueError: If a parameter is out of range.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        self._check_rgb(r, g, b, period_ms)
        return self._send_rgb_fx(OW_RGB_FX_BREATHE, r, g, b, period_ms,
                                 op="rgb_breathe")

    def rgb_rainbow(self, period_ms: int = 4000) -> bool:
        """Sweep the full hue wheel, one revolution every period_ms, repeating.

        Raises:
            ValueError: If *period_ms* is out of range.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        self._check_rgb(0, 0, 0, period_ms)
        return self._send_rgb_fx(OW_RGB_FX_RAINBOW, period_ms=period_ms,
                                 op="rgb_rainbow")

    def rgb_flash(self, r: int, g: int, b: int, period_ms: int = 1000) -> bool:
        """Flash the given color: 50% on/off blink with a full cycle of
        period_ms (e.g. 1000 = 0.5 s on, 0.5 s off), repeating.

        Raises:
            ValueError: If a parameter is out of range.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        self._check_rgb(r, g, b, period_ms)
        return self._send_rgb_fx(OW_RGB_FX_FLASH, r, g, b, period_ms,
                                 op="rgb_flash")

    def rgb_color_cycle(self, colors: list[tuple[int, int, int]],
                        dwell_ms: int = 1000) -> bool:
        """Step through a list of colors, showing each for dwell_ms, repeating.

        Args:
            colors: 1 to 8 (r, g, b) tuples.
            dwell_ms: Time each color is shown, in milliseconds.

        Raises:
            ValueError: If the color list or a value is out of range.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        if not 1 <= len(colors) <= OW_RGB_CYCLE_MAX_COLORS:
            raise ValueError(
                f"Invalid color list. Must contain 1-{OW_RGB_CYCLE_MAX_COLORS} colors"
            )
        for color in colors:
            self._check_rgb(*color, dwell_ms)
        first = colors[0]
        extra = b"".join(struct.pack("<BBB", *c) for c in colors[1:])
        return self._send_rgb_fx(OW_RGB_FX_CYCLE, first[0], first[1], first[2],
                                 dwell_ms, extra=extra, op="rgb_color_cycle")

    def rgb_effect_stop(self) -> bool:
        """Cancel any running effect; the LED holds its current color.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        return self._send_rgb_fx(OW_RGB_FX_STOP, op="rgb_effect_stop")

    def get_vmon_values(self) -> list[dict]:
        """Retrieve the voltage-monitor readings.

        Returns:
            A list of 8 dicts (one per channel) with raw_adc, voltage,
            and converted_voltage fields.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError: If the payload length is invalid.
        """
        r = self.send_checked(packet_type=OW_POWER, command=OW_POWER_VMON,
                              op="get_vmon_values")
        if r.data_len != 80:
            raise LIFUProtocolError(
                f"HV: VMON payload length {r.data_len} != 80",
                code=LIFU_ERR_BAD_PAYLOAD_LENGTH,
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
        """Set a raw 12-bit DAC value.

        Args:
            dac_id: 0-3.
            dac_value: 0-4095.

        Raises:
            ValueError: If *dac_id* or *dac_value* is out of range.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        if dac_id not in (0, 1, 2, 3):
            raise ValueError("Invalid DAC ID. Must be 0, 1, 2, or 3")
        if not 0 <= dac_value <= 4095:
            raise ValueError("Invalid DAC value. Must be 0 to 4095")
        logger.debug("Setting Raw DAC value.")
        data = bytes([(dac_value >> 8) & 0xFF, dac_value & 0xFF])
        self.send_checked(addr=dac_id, packet_type=OW_POWER,
                          command=OW_POWER_RAW_DAC, data=data,
                          op="set_raw_dac")
        logger.info("Set DAC value to %d", dac_value)
        return dac_value

    def hv_enable(self, enable: bool = False) -> bool:
        """Enable or disable the HV output stage.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        logger.debug("%s high voltage output.", "Enabling" if enable else "Disabling")
        self.send_checked(addr=1 if enable else 0, packet_type=OW_POWER,
                          command=OW_POWER_HV_ENABLE, op="hv_enable")
        self.is_hv_on = enable
        logger.info("High voltage output %s successfully.",
                    "enabled" if enable else "disabled")
        return True

