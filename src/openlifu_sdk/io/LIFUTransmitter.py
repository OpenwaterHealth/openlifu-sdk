from __future__ import annotations
import json
import logging
import struct

from ow_comms.config import (
    GLOBAL_COMMANDS, CONTROLLER_COMMANDS,
    OW_CMD_GET_TEMP, OW_CMD_GET_AMBIENT, OW_CONTROLLER, OW_ERROR,
    TEMPERATURE_DATA_LENGTH, TX7332_COMMANDS, DEFAULT_TIMEOUT,
    OW_VID, OW_TRANSMITTER_PID,
    OW_CTRL_SET_SWTRIG, OW_CTRL_GET_SWTRIG,
    OW_CTRL_START_SWTRIG, OW_CTRL_STOP_SWTRIG,
    OW_CTRL_GET_MODULE_COUNT,
    OW_TX7332, OW_TX7332_ENUM,
    OW_TX7332_WREG, OW_TX7332_RREG,
    OW_TX7332_WBLOCK, OW_TX7332_RBLOCK,
    OW_CMD_USR_CFG,
)
from ow_comms.component import OWComponent
from .LIFUUserConfig import LIFUUserConfig

log = logging.getLogger("LIFUTransmitter")


class LIFUTransmitter(OWComponent):
    """Manages the UART link to the transmitter board.

    Supports **Global**, **TX7332**, and **Controller** commands.
    """

    def __init__(self, vid: int = OW_VID, pid: int = OW_TRANSMITTER_PID,
                 baudrate: int = 921600, timeout: float = DEFAULT_TIMEOUT):
        super().__init__(
            vid, pid,
            supported_commands=GLOBAL_COMMANDS | TX7332_COMMANDS | CONTROLLER_COMMANDS,
            baudrate=baudrate, timeout=timeout, desc="LIFUTransmitter",
        )

    # ------------------------------------------------------------------
    # CONTROLLER – temperature
    # ------------------------------------------------------------------

    def get_temperature(self, module:int=0) -> float | None:
        self._require_connected()
        r = self.send(packet_type=OW_CONTROLLER, command=OW_CMD_GET_TEMP, addr=module)
        r.print_packet()
        if r is None or r.data_len < TEMPERATURE_DATA_LENGTH:
            return None
        
        temperature = struct.unpack('<f', r.data)[0]
        truncated_temperature = round(temperature, 2)
        return truncated_temperature

    def get_ambient(self, module:int=0) -> float | None:
        self._require_connected()
        r = self.send(packet_type=OW_CONTROLLER, command=OW_CMD_GET_AMBIENT, addr=module)
        r.print_packet()
        if r is None or r.data_len < TEMPERATURE_DATA_LENGTH:
            return None
        
        temperature = struct.unpack('<f', r.data)[0]
        truncated_temperature = round(temperature, 2)
        return truncated_temperature

    # ------------------------------------------------------------------
    # CONTROLLER – trigger
    # ------------------------------------------------------------------

    def set_trigger(self, data: dict) -> dict | None:
        """Set the trigger configuration.

        Args:
            data: Dictionary containing the trigger configuration.

        Returns:
            Parsed JSON response dict, or None on error.
        """
        self._require_connected()
        if data is None:
            raise ValueError("Trigger data cannot be None")
        payload = json.dumps(data).encode("utf-8")
        r = self.send(packet_type=OW_CONTROLLER, command=OW_CTRL_SET_SWTRIG, data=payload)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR or r.data_len == 0:
            log.error("set_trigger failed")
            return None
        try:
            return json.loads(r.data[:r.data_len].decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.error("set_trigger JSON decode error: %s", e)
            return None

    def get_trigger(self) -> dict | None:
        """Get the current trigger configuration.

        Returns:
            Parsed JSON response dict, or None on error.
        """
        self._require_connected()
        r = self.send(packet_type=OW_CONTROLLER, command=OW_CTRL_GET_SWTRIG)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR or r.data_len == 0:
            log.error("get_trigger failed")
            return None
        try:
            return json.loads(r.data[:r.data_len].decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.error("get_trigger JSON decode error: %s", e)
            return None

    def start_trigger(self) -> bool:
        """Start the software trigger.

        Returns:
            True if started successfully, False otherwise.
        """
        self._require_connected()
        r = self.send(packet_type=OW_CONTROLLER, command=OW_CTRL_START_SWTRIG)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            log.error("start_trigger failed")
            return False
        return True

    def stop_trigger(self) -> bool:
        """Stop the software trigger.

        Returns:
            True if stopped successfully, False otherwise.
        """
        self._require_connected()
        r = self.send(packet_type=OW_CONTROLLER, command=OW_CTRL_STOP_SWTRIG)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            log.error("stop_trigger failed")
            return False
        return True

    # ------------------------------------------------------------------
    # CONTROLLER – module count
    # ------------------------------------------------------------------

    def get_module_count(self) -> int:
        """Return the number of connected transmitter modules (including master).

        Module 0 is always the USB-connected master.  Additional modules are
        daisy-chained via I2C and relayed through module 0.

        Returns:
            Number of modules, or 0 on error.
        """
        self._require_connected()
        r = self.send(packet_type=OW_CONTROLLER, command=OW_CTRL_GET_MODULE_COUNT)
        if r is None or r.packet_type == OW_ERROR or r.data_len < 1:
            log.error("get_module_count failed")
            return 0
        return r.data[0]

    # ------------------------------------------------------------------
    # TX7332 – enumeration
    # ------------------------------------------------------------------

    def enum_tx7332_devices(self) -> int:
        """Enumerate TX7332 devices on the transmitter.

        Sends ``OW_TX7332_ENUM`` and returns the number of TX7332 chips
        detected.  Each module has 2 TX7332 chips.

        Returns:
            Number of TX7332 devices detected, or 0 on error.
        """
        self._require_connected()
        r = self.send(packet_type=OW_TX7332, command=OW_TX7332_ENUM)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR or r.reserved < 1:
            log.error("enum_tx7332_devices failed")
            return 0
        num_detected_devices = r.reserved
        log.info("TX7332 devices detected: %d", num_detected_devices)
        return num_detected_devices

    # ------------------------------------------------------------------
    # TX7332 – register read / write
    # ------------------------------------------------------------------

    def write_register(self, identifier: int, address: int, value: int) -> bool:
        """Write a single TX7332 register.

        Args:
            identifier: TX7332 chip index.
            address: Register address.
            value: 32-bit value to write.

        Returns:
            True on success, False on error.
        """
        self._require_connected()
        if identifier < 0:
            raise ValueError("TX chip identifier must be >= 0")
        data = struct.pack("<HI", address, value)
        r = self.send(packet_type=OW_TX7332, command=OW_TX7332_WREG, addr=identifier, data=data)
        if r is None or r.packet_type == OW_ERROR:
            log.error("write_register failed (chip=%d addr=0x%04X)", identifier, address)
            return False
        log.debug("Wrote 0x%08X to chip %d reg 0x%04X", value, identifier, address)
        return True

    def read_register(self, identifier: int, address: int) -> int | None:
        """Read a single TX7332 register.

        Args:
            identifier: TX7332 chip index.
            address: Register address.

        Returns:
            32-bit register value, or None on error.
        """
        self._require_connected()
        if identifier < 0:
            raise ValueError("TX chip identifier must be >= 0")
        data = struct.pack("<H", address)
        r = self.send(packet_type=OW_TX7332, command=OW_TX7332_RREG, addr=identifier, data=data)
        if r is None or r.packet_type == OW_ERROR:
            log.error("read_register failed (chip=%d addr=0x%04X)", identifier, address)
            return None
        if r.data_len < 4:
            log.error("read_register: unexpected data_len=%d", r.data_len)
            return None
        value = struct.unpack("<I", r.data[:4])[0]
        log.debug("Read 0x%08X from chip %d reg 0x%04X", value, identifier, address)
        return value

    def write_block(self, identifier: int, start_address: int, reg_values: list[int]) -> bool:
        """Write a contiguous block of TX7332 registers.

        Large blocks are automatically split into chunks of up to 62 registers.

        Args:
            identifier: TX7332 chip index.
            start_address: Starting register address.
            reg_values: List of 32-bit register values.

        Returns:
            True on success, False on error.
        """
        self._require_connected()
        if identifier < 0:
            raise ValueError("TX chip identifier must be >= 0")
        if not reg_values:
            raise ValueError("reg_values must be a non-empty list")

        max_regs = 62
        for chunk_start in range(0, len(reg_values), max_regs):
            chunk = reg_values[chunk_start : chunk_start + max_regs]
            fmt = "<HBB" + "I" * len(chunk)
            data = struct.pack(fmt, start_address + chunk_start, len(chunk), 0, *chunk)
            r = self.send(packet_type=OW_TX7332, command=OW_TX7332_WBLOCK, addr=identifier, data=data)
            if r is None or r.packet_type == OW_ERROR:
                log.error("write_block failed (chip=%d chunk at 0x%04X)", identifier, start_address + chunk_start)
                return False
        log.debug("write_block: %d regs from 0x%04X on chip %d", len(reg_values), start_address, identifier)
        return True

    def read_block(self, identifier: int, start_address: int, count: int) -> list[int] | None:
        """Read a contiguous block of TX7332 registers.

        Args:
            identifier: TX7332 chip index.
            start_address: Starting register address.
            count: Number of registers to read (1-62).

        Returns:
            List of 32-bit values, or None on error.
        """
        self._require_connected()
        if identifier < 0:
            raise ValueError("TX chip identifier must be >= 0")
        if count <= 0 or count > 62:
            raise ValueError(f"count must be 1-62, got {count}")
        data = struct.pack("<HBB", start_address, count, 0)
        r = self.send(packet_type=OW_TX7332, command=OW_TX7332_RBLOCK, addr=identifier, data=data)
        if r is None or r.packet_type == OW_ERROR:
            log.error("read_block failed (chip=%d addr=0x%04X count=%d)", identifier, start_address, count)
            return None
        expected = count * 4
        if r.data_len < expected:
            log.error("read_block: data_len=%d, expected=%d", r.data_len, expected)
            return None
        values = list(struct.unpack(f"<{count}I", r.data[:expected]))
        log.debug("read_block: %d regs from 0x%04X on chip %d", count, start_address, identifier)
        return values

    # ------------------------------------------------------------------
    # User configuration
    # ------------------------------------------------------------------

    def read_config(self, module: int = 0) -> LIFUUserConfig | None:
        """Read the user configuration from device flash.

        Args:
            module: Target module address (default 0).

        Returns:
            Parsed LIFUUserConfig, or None on error.
        """
        self._require_connected()
        log.debug("Reading user config from %s ...", self._uart.desc)
        r = self.send(OW_CMD_USR_CFG, addr=module, reserved=0)  # 0 = READ
        if r is None or r.packet_type == OW_ERROR:
            log.error("Error reading config from %s", self._uart.desc)
            return None
        try:
            config = LIFUUserConfig.from_wire_bytes(r.data)
            log.debug("Read config: seq=%d, json_len=%d", config.header.seq, config.header.json_len)
            return config
        except Exception as exc:
            log.error("Failed to parse config response: %s", exc)
            return None

    def write_config(self, config: LIFUUserConfig, module: int = 0) -> LIFUUserConfig | None:
        """Write user configuration to device flash.

        Args:
            config: LIFUUserConfig to write.
            module: Target module address (default 0).

        Returns:
            Updated LIFUUserConfig (with new seq/crc from the device), or None on error.
        """
        self._require_connected()
        wire_data = config.to_wire_bytes()
        log.debug("Writing config to %s: %d bytes", self._uart.desc, len(wire_data))
        r = self.send(OW_CMD_USR_CFG, addr=module, reserved=1, data=bytearray(wire_data))  # 1 = WRITE
        if r is None or r.packet_type == OW_ERROR:
            log.error("Error writing config to %s", self._uart.desc)
            return None
        try:
            from ow_comms import UserConfigHeader
            updated_header = UserConfigHeader.from_bytes(r.data[:16])
            return LIFUUserConfig(header=updated_header, json_data=config.json_data)
        except Exception as exc:
            log.error("Failed to parse write response: %s", exc)
            return None

    def write_config_json(self, json_str: str, module: int = 0) -> LIFUUserConfig | None:
        """Write user configuration from a JSON string.

        Args:
            json_str: JSON string to write.
            module: Target module address (default 0).

        Returns:
            Updated LIFUUserConfig from device, or None on error.

        Raises:
            ValueError: If JSON is invalid or device is not connected.
        """
        import json as _json
        try:
            config = LIFUUserConfig()
            config.set_json_str(json_str)
        except _json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc
        return self.write_config(config, module=module)
