from __future__ import annotations

import logging

import json

from .LIFUConfig import (
    OW_CMD, OW_ERROR,
    GLOBAL_COMMANDS,
    DEFAULT_TIMEOUT, HW_ID_DATA_LENGTH,
    OW_CMD_PING, OW_CMD_VERSION, OW_CMD_ECHO, OW_CMD_HWID,
    OW_CMD_TOGGLE_LED, OW_CMD_DFU, OW_CMD_RESET, OW_CMD_USR_CFG,
)
from .LIFUUserConfig import LifuUserConfig, LifuUserConfigHeader
from .uart import OWUart
from .uart_packet import OWUartPacket
from .signal import OWSignal


log = logging.getLogger("OWComponent")

# Build a command -> packet-type lookup so callers only need to specify the
# command byte; the correct packet type is inferred automatically.
_CMD_TO_PKT_TYPE: dict[int, int] = {cmd: OW_CMD for cmd in GLOBAL_COMMANDS}

def format_hwid(hex_str: str) -> str:
    """Format a raw hex string into a dash-separated hardware ID.

    Example: 'deadbeefcafe' -> 'DEAD-BEEF-CAFE'
    """
    hex_str = hex_str.upper()
    return "-".join(hex_str[i:i+4] for i in range(0, len(hex_str), 4))

def register_command_packet_type(command: int, packet_type: int) -> None:
    _CMD_TO_PKT_TYPE[command] = packet_type


def register_command_packet_types(commands: set[int], packet_type: int) -> None:
    for command in commands:
        register_command_packet_type(command, packet_type)


class OWComponent:
    """Base for Transmitter / Console -- wraps an OWUart and restricts the
    allowed command set."""

    def __init__(self, vid: int, pid: int, supported_commands: set[int],
                 baudrate: int = 921600, timeout: float = DEFAULT_TIMEOUT,
                 desc: str = "VCP"):
        self._uart = OWUart(vid, pid, baudrate=baudrate, timeout=timeout, desc=desc)
        self._supported_commands = supported_commands

    # -- Expose underlying OWUart attributes --------------------------

    @property
    def uart(self) -> OWUart:
        return self._uart

    def is_connected(self) -> bool:
        return self._uart.is_connected

    @property
    def signal_connected(self) -> OWSignal:
        return self._uart.signal_connected

    @property
    def signal_disconnected(self) -> OWSignal:
        return self._uart.signal_disconnected

    @property
    def signal_data_received(self) -> OWSignal:
        return self._uart.signal_data_received

    @property
    def signal_error(self) -> OWSignal:
        return self._uart.signal_error

    # -- Lifecycle ----------------------------------------------------

    def connect(self) -> bool:
        return self._uart.connect()
    
    def is_connected(self) -> bool:
        return self._uart.is_connected

    def disconnect(self):
        self._uart.disconnect()

    def start(self):
        self._uart.start()

    def stop(self):
        self._uart.stop()

    def close(self):
        """Stop async threads (if running) and disconnect."""
        self._uart.stop()
        self._uart.disconnect()

    # -- Command validation -------------------------------------------

    def _resolve(self, command: int, packet_type: int | None) -> int:
        if command not in self._supported_commands:
            raise ValueError(
                f"Command 0x{command:02X} is not supported by {self._uart.desc}"
            )
        if packet_type is not None:
            return packet_type
        return _CMD_TO_PKT_TYPE.get(command, OW_CMD)

    # -- Send helpers -------------------------------------------------

    def send(self, command: int, addr: int = 0, reserved: int = 0,
             data: bytearray | None = None, timeout: float | None = None,
             packet_type: int | None = None) -> OWUartPacket | None:
        """Send *command* and block until the response arrives."""
        pt = self._resolve(command, packet_type)
        return self._uart.send_packet(
            packet_type=pt, command=command,
            addr=addr, reserved=reserved, data=data, timeout=timeout,
        )

    def send_async(self, command: int, addr: int = 0, reserved: int = 0,
                   data: bytearray | None = None, timeout: float | None = None,
                   packet_type: int | None = None) -> int:
        """Queue *command* for sending without blocking (async mode).

        Returns the packet ID.  The response is delivered via
        ``signal_data_received``; timeouts via ``signal_error``.
        """
        pt = self._resolve(command, packet_type)
        return self._uart.send_packet_async(
            packet_type=pt, command=command,
            addr=addr, reserved=reserved, data=data, timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Global command helpers (available on every component)
    # ------------------------------------------------------------------

    def _require_connected(self):
        if not self.is_connected():
            raise ValueError(f"{self._uart.desc} not connected")

    def ping(self, module: int = 0) -> bool:
        self._require_connected()
        log.info("Send Ping to %s", self._uart.desc)
        r = self.send(OW_CMD_PING, addr=module)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            log.error("Ping failed on %s", self._uart.desc)
            return False
        return True

    def get_version(self, module: int = 0) -> str:
        self._require_connected()
        r = self.send(OW_CMD_VERSION)
        r.print_packet()
        r.print_packet()
        if r is None:
            raise RuntimeError(f"{self._uart.desc}: version request timed out")
        if r.data_len == 3:
            ver = f"v{r.data[0]}.{r.data[1]}.{r.data[2]}"
        elif r.data_len and r.data:
            ver = r.data[:r.data_len].decode("utf-8", errors="ignore").rstrip("\x00").strip()
            if not ver:
                ver = "v0.0.0"
        else:
            ver = "v0.0.0"
        log.info("%s version: %s", self._uart.desc, ver)
        return ver

    def echo(self, module: int = 0, echo_data: bytes | bytearray = b"Hello LIFU!") -> tuple[bytes | None, int]:
        self._require_connected()
        if not isinstance(echo_data, (bytes, bytearray)):
            raise TypeError("echo_data must be bytes or bytearray")
        r = self.send(OW_CMD_ECHO, addr=module, data=bytearray(echo_data))
        r.print_packet()
        if r is None:
            raise RuntimeError(f"{self._uart.desc}: echo request timed out")
        if r.data_len > 0:
            return bytes(r.data), r.data_len
        return None, 0

    def get_hardware_id(self, module: int = 0) -> str | None:
        self._require_connected()
        r = self.send(OW_CMD_HWID, addr=module)
        r.print_packet()
        if r is None:
            raise RuntimeError(f"{self._uart.desc}: HWID request timed out")
        if r.data_len >= HW_ID_DATA_LENGTH:
            return format_hwid(r.data[:HW_ID_DATA_LENGTH].hex())
        return None

    def toggle_led(self, module: int = 0) -> bool:
        self._require_connected()
        r = self.send(OW_CMD_TOGGLE_LED, addr=module)
        r.print_packet()
        return r is not None and r.packet_type != OW_ERROR

    def soft_reset(self, module: int = 0) -> bool:
        """Perform a soft reset on the device."""
        self._require_connected()
        r = self.send(OW_CMD_RESET, addr=module)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            log.error("Error resetting %s", self._uart.desc)
            return False
        return True

    def enter_dfu(self, module: int = 0) -> bool:
        """Perform a soft reset into DFU mode."""
        self._require_connected()
        r = self.send(OW_CMD_DFU, addr=module)
        r.print_packet()
        if r is None or r.packet_type == OW_ERROR:
            log.error("Error entering DFU mode on %s", self._uart.desc)
            return False
        return True

    # ------------------------------------------------------------------
    # User configuration helpers
    # ------------------------------------------------------------------

    def read_config(self, module: int = 0) -> LifuUserConfig | None:
        """Read the user configuration from device flash.

        Args:
            module: Target module address (default 0).

        Returns:
            Parsed LifuUserConfig, or None on error.

        Raises:
            ValueError: If the device is not connected.
        """
        self._require_connected()
        log.debug("Reading user config from %s ...", self._uart.desc)
        r = self.send(OW_CMD_USR_CFG, addr=module, reserved=0)  # 0 = READ
        if r is None or r.packet_type == OW_ERROR:
            log.error("Error reading config from %s", self._uart.desc)
            return None
        try:
            config = LifuUserConfig.from_wire_bytes(r.data)
            log.debug("Read config: seq=%d, json_len=%d", config.header.seq, config.header.json_len)
            return config
        except Exception as exc:
            log.error("Failed to parse config response: %s", exc)
            return None

    def write_config(self, config: LifuUserConfig, module: int = 0) -> LifuUserConfig | None:
        """Write user configuration to device flash.

        Args:
            config: LifuUserConfig to write.
            module: Target module address (default 0).

        Returns:
            Updated LifuUserConfig (with new seq/crc from the device),
            or None on error.

        Raises:
            ValueError: If the device is not connected.
        """
        self._require_connected()
        wire_data = config.to_wire_bytes()
        log.debug("Writing config to %s: %d bytes", self._uart.desc, len(wire_data))
        r = self.send(OW_CMD_USR_CFG, addr=module, reserved=1, data=bytearray(wire_data))  # 1 = WRITE
        if r is None or r.packet_type == OW_ERROR:
            log.error("Error writing config to %s", self._uart.desc)
            return None
        try:
            updated_header = LifuUserConfigHeader.from_bytes(r.data[:16])
            updated_config = LifuUserConfig(header=updated_header, json_data=config.json_data)
            log.debug("Config written: new seq=%d", updated_config.header.seq)
            return updated_config
        except Exception as exc:
            log.error("Failed to parse write response: %s", exc)
            return None

    def write_config_json(self, json_str: str, module: int = 0) -> LifuUserConfig | None:
        """Write user configuration from a JSON string.

        Convenience wrapper around :meth:`write_config`.

        Args:
            json_str: JSON string to write.
            module: Target module address (default 0).

        Returns:
            Updated LifuUserConfig from device, or None on error.

        Raises:
            ValueError: If JSON is invalid or device is not connected.
        """
        try:
            config = LifuUserConfig()
            config.set_json_str(json_str)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc
        return self.write_config(config, module=module)
