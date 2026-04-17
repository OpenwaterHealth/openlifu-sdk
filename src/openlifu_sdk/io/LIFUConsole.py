from __future__ import annotations

import logging
import struct

from ow_comms.config import (
    GLOBAL_COMMANDS, DEFAULT_TIMEOUT, OW_VID, OW_ERROR,
)

from ow_comms.component import OWComponent

from .LIFUConfig import ( 
    OW_CONSOLE_PID, OW_CONSOLE_PID,
    CONTROLLER_COMMANDS, OW_POWER, POWER_COMMANDS,
    OW_POWER_GET_TEMP1, OW_POWER_GET_TEMP2,
    OW_POWER_12V_ON, OW_POWER_12V_OFF, OW_POWER_GET_12VON,
    OW_POWER_HV_ON, OW_POWER_HV_OFF, OW_POWER_GET_HVON,
    OW_POWER_SET_HV, OW_POWER_GET_HV,
    OW_POWER_SET_FAN, OW_POWER_GET_FAN,
    OW_POWER_SET_RGB, OW_POWER_GET_RGB,
    OW_POWER_STATUS, OW_POWER_VMON,
)

log = logging.getLogger("LIFUConsole")


class LIFUConsole(OWComponent):
    """Manages the UART link to the console board.

    Supports **Global**, **Controller**, and **Power** commands.
    """

    def __init__(self, vid: int = OW_VID, pid: int = OW_CONSOLE_PID,
                 baudrate: int = 921600, timeout: float = DEFAULT_TIMEOUT):
        super().__init__(
            vid, pid,
            supported_commands=GLOBAL_COMMANDS | CONTROLLER_COMMANDS | POWER_COMMANDS,
            baudrate=baudrate, timeout=timeout, desc="LIFUConsole",
        )

    # ------------------------------------------------------------------
    # Power – temperature
    # ------------------------------------------------------------------

    def get_temperature1(self) -> float:
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_GET_TEMP1)
        r.print_packet()
        if r is None:
            raise RuntimeError("LIFUConsole: temperature1 request timed out")
        if r.data_len == 4:
            return round(struct.unpack("<f", r.data)[0], 2)
        raise ValueError("Invalid data length for temperature1")

    def get_temperature2(self) -> float:
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_GET_TEMP2)
        r.print_packet()
        if r is None:
            raise RuntimeError("LIFUConsole: temperature2 request timed out")
        if r.data_len == 4:
            return round(struct.unpack("<f", r.data)[0], 2)
        raise ValueError("Invalid data length for temperature2")

    # ------------------------------------------------------------------
    # Power – 12V rail
    # ------------------------------------------------------------------

    def turn_12v_on(self) -> bool:
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_12V_ON)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            log.error("Error turning on 12V")
            return False
        log.info("12V turned on")
        return True

    def turn_12v_off(self) -> bool:
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_12V_OFF)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            log.error("Error turning off 12V")
            return False
        log.info("12V turned off")
        return True

    def get_12v_status(self) -> bool:
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_GET_12VON)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            raise RuntimeError("LIFUConsole: 12V status request failed")
        return r.reserved == 1

    # ------------------------------------------------------------------
    # Power – HV rail
    # ------------------------------------------------------------------

    def turn_hv_on(self, timeout: float | None = 30.0) -> bool:
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_HV_ON, timeout=timeout)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            log.error("Error turning on HV")
            return False
        log.info("HV turned on")
        return True

    def turn_hv_off(self, timeout: float | None = 5.0) -> bool:
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_HV_OFF, timeout=timeout)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            log.error("Error turning off HV")
            return False
        log.info("HV turned off")
        return True

    def get_hv_status(self) -> bool:
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_GET_HVON)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            raise RuntimeError("LIFUConsole: HV status request failed")
        return r.reserved == 1

    def set_hv(self, voltage: float) -> bool:
        self._require_connected()
        if not 5.0 <= voltage <= 100.0:
            raise ValueError("HV voltage must be between 5 and 100 V")
        data = struct.pack('>f', voltage)
        r = self.send(packet_type=OW_POWER, command=OW_POWER_SET_HV, data=data)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            log.error("Error setting HV to %.2f", voltage)
            return False
        return True

    def get_hv(self) -> float:
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_GET_HV)
        r.print_packet()
        if r is None:
            raise RuntimeError("LIFUConsole: get HV request timed out")
        if r.data_len == 4:
            return round(struct.unpack("<f", r.data)[0], 2)
        raise ValueError("Invalid data length for HV reading")

    # ------------------------------------------------------------------
    # Power – fan
    # ------------------------------------------------------------------

    def set_fan(self, fan_id: int = 0, speed: int = 50) -> int:
        """Set fan speed.

        Args:
            fan_id: 0 = bottom fans, 1 = top fans.
            speed:  0–100 percent.

        Returns:
            The requested speed on success, -1 on error.
        """
        self._require_connected()
        if fan_id not in (0, 1):
            raise ValueError("Invalid fan ID. Must be 0 or 1")
        if not 0 <= speed <= 100:
            raise ValueError("Invalid fan speed. Must be 0 to 100")
        r = self.send(packet_type=OW_POWER, command=OW_POWER_SET_FAN, addr=fan_id,
                      data=bytearray([speed & 0xFF]))
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            log.error("Error setting fan %d speed", fan_id)
            return -1
        log.info("Set fan %d speed to %d", fan_id, speed)
        return speed

    def get_fan(self, fan_id: int = 0) -> int:
        """Get current fan speed percentage.

        Args:
            fan_id: 0 = bottom fans, 1 = top fans.

        Returns:
            Fan speed 0–100, or -1 on error.
        """
        self._require_connected()
        if fan_id not in (0, 1):
            raise ValueError("Invalid fan ID. Must be 0 or 1")
        r = self.send(packet_type=OW_POWER, command=OW_POWER_GET_FAN, addr=fan_id)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            log.error("Error getting fan %d speed", fan_id)
            return -1
        if r.data_len >= 1:
            return r.data[0]
        raise ValueError("Invalid data length for fan reading")

    # ------------------------------------------------------------------
    # Power – RGB LED
    # ------------------------------------------------------------------

    def set_rgb(self, state: int) -> bool:
        """Set the RGB LED state.

        Args:
            state: 0 = OFF, 1 = RED, 2 = BLUE, 3 = GREEN.
        """
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_SET_RGB, reserved=state)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            log.error("Error setting RGB state")
            return False
        return True

    def get_rgb(self) -> int:
        """Get the current RGB LED state.

        Returns:
            int: 0 = OFF, 1 = RED, 2 = BLUE, 3 = GREEN.  -1 on error.
        """
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_GET_RGB)
        if r is None or r.packet_type == OW_ERROR:
            log.error("Error getting RGB LED state")
            return -1
        return r.reserved

    # ------------------------------------------------------------------
    # Power – status / voltage monitor
    # ------------------------------------------------------------------

    def get_power_status(self) -> bytes:
        self._require_connected()
        r = self.send(packet_type=OW_POWER, command=OW_POWER_STATUS)
        r.print_packet()
        if r is None:
            raise RuntimeError("LIFUConsole: power status request timed out")
        return bytes(r.data[:r.data_len])

    def get_voltage_monitor(self) -> list[dict]:
        """Retrieve voltage monitor readings for 8 channels.

        Returns a list of 8 dicts with keys: channel, raw_adc, voltage,
        converted_voltage.
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
