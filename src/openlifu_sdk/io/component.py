from __future__ import annotations

import json
import logging
import struct

from .LIFUConfig import (
    OW_CMD, OW_ERROR,
    GLOBAL_COMMANDS,
    DEFAULT_TIMEOUT, HW_ID_DATA_LENGTH,
    OW_CMD_PING, OW_CMD_VERSION, OW_CMD_ECHO, OW_CMD_HWID,
    OW_CMD_TOGGLE_LED, OW_CMD_DFU, OW_CMD_RESET, OW_CMD_USR_CFG,
    LIFU_ERR_BAD_PAYLOAD_FORMAT,
    LIFU_ERR_BAD_PAYLOAD_LENGTH,
)
from .exceptions import (
    LIFUCommunicationError,
    LIFUDeviceError,
    LIFUNotConnectedError,
    LIFUProtocolError,
)
from .LIFUUserConfig import LifuUserConfig, LifuUserConfigHeader
from .uart import OWUart
from .uart_packet import OWUartPacket
from .signal import OWSignal


log = logging.getLogger(__name__)

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
        """Send *command* and block until the response arrives.

        Returns ``None`` on transport timeout. Most callers should prefer
        :meth:`send_checked`, which raises a typed :class:`LIFUError` instead
        of returning ``None``.
        """
        pt = self._resolve(command, packet_type)
        r = self._uart.send_packet(
            packet_type=pt, command=command,
            addr=addr, reserved=reserved, data=data, timeout=timeout,
        )
        if r is not None:
            r.print_packet()
        return r

    def send_checked(self, command: int, addr: int = 0, reserved: int = 0,
                     data: bytearray | None = None, timeout: float | None = None,
                     packet_type: int | None = None,
                     op: str | None = None,
                     retries: int = 1) -> OWUartPacket:
        """Send *command* and validate the response.

        Args:
            retries: Number of automatic retries on transient transport
                timeouts (``LIFUCommunicationError``). Default 1 (so up to
                2 attempts total) — see motivation below. Set to 0 to
                disable retries for callers that handle retry themselves
                (e.g. firmware-update progress loops where a duplicate
                send would corrupt sequencing). Device errors
                (``OW_ERROR``) are NOT retried.

        Why retry by default? When the SDK is driven from a heavily
        threaded host (Qt UI + qasync + telemetry poll thread + the
        three UART daemon threads), the response packet can occasionally
        be delayed by Python GIL / OS-scheduler jitter for longer than
        the per-command timeout, producing a spurious
        ``LIFUCommunicationError`` even though the device responded
        correctly. A single in-SDK retry recovers from this transparently
        without every caller having to wrap the call themselves.

        Raises:
            LIFUNotConnectedError: If the UART is not open.
            LIFUCommunicationError: If every attempt times out.
            LIFUDeviceError: If the device replies with ``OW_ERROR``.
        """
        self._require_connected()
        label = op or f"cmd 0x{command:02X}"
        total_attempts = max(1, retries + 1)
        last_timeout_exc: LIFUCommunicationError | None = None
        for attempt in range(1, total_attempts + 1):
            r = self.send(command, addr=addr, reserved=reserved, data=data,
                          timeout=timeout, packet_type=packet_type)
            if r is None:
                last_timeout_exc = LIFUCommunicationError(
                    f"{self._uart.desc}: {label} timed out"
                )
                if attempt < total_attempts:
                    log.warning(
                        "%s: %s timed out on attempt %d/%d; retrying...",
                        self._uart.desc, label, attempt, total_attempts,
                    )
                    continue
                raise last_timeout_exc
            if r.packet_type == OW_ERROR:
                # Surface whatever diagnostic detail the device packed
                # into the OW_ERROR reply so the failure isn't reduced
                # to a bare "returned device error". The firmware uses
                # the ``reserved`` byte as a sub-error code and may
                # also stash a payload (often an ASCII reason string,
                # sometimes packed struct bytes). Include both forms
                # so the operator-facing popup carries enough context
                # to triage the failure without a debug build.
                #
                # Attribute fetches are tolerant of mocks / partial
                # packets used in tests -- only ints with sane values
                # are formatted as hex, everything else is dropped.
                def _as_int(value):
                    if isinstance(value, bool):
                        return None
                    return value if isinstance(value, int) else None

                def _as_bytes(value):
                    if value is None:
                        return b""
                    try:
                        return bytes(value)
                    except TypeError:
                        return b""

                cmd_byte = _as_int(getattr(r, "command", None))
                addr_byte = _as_int(getattr(r, "addr", None))
                device_err_code = _as_int(getattr(r, "reserved", None))
                payload = _as_bytes(getattr(r, "data", None))

                detail_parts: list[str] = []
                if cmd_byte is not None:
                    detail_parts.append(f"cmd=0x{cmd_byte:02X}")
                if addr_byte is not None:
                    detail_parts.append(f"addr=0x{addr_byte:02X}")
                if device_err_code is not None:
                    detail_parts.append(f"device_err=0x{device_err_code:02X}")
                if payload:
                    payload_hex = payload.hex(" ")
                    detail_parts.append(f"payload={payload_hex}")
                    # Best-effort decode: most firmware error payloads
                    # are short ASCII reason strings. Fall through
                    # silently if it isn't decodable as printable text.
                    try:
                        text = payload.rstrip(b"\x00").decode("ascii")
                    except UnicodeDecodeError:
                        text = ""
                    if text and all(c.isprintable() or c in "\r\n\t" for c in text):
                        detail_parts.append(f'message="{text.strip()}"')

                detail = " ".join(detail_parts)
                msg = f"{self._uart.desc}: {label} returned device error"
                if detail:
                    msg = f"{msg} ({detail})"
                raise LIFUDeviceError(
                    msg,
                    packet=r,
                    device_error_code=device_err_code,
                    device_error_data=payload or None,
                )
            if attempt > 1:
                # Make it visible WHEN slow responses are recovering --
                # if rt is comfortably under the per-call timeout the
                # issue was the timeout itself; if it's close to (or
                # past) the timeout, the device / link is genuinely slow.
                rt = getattr(r, "_owuart_rt", 0.0)
                log.warning(
                    "%s: %s recovered on attempt %d/%d (rt=%.2fs)",
                    self._uart.desc, label, attempt, total_attempts, rt,
                )
            return r
        # Unreachable: the loop either returns or raises above.
        raise last_timeout_exc  # pragma: no cover

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
            raise LIFUNotConnectedError(f"{self._uart.desc} not connected")

    def ping(self, module: int = 0) -> bool:
        """Send a PING to the device.

        Returns:
            True if the device responded with a non-error packet.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        log.info("Send Ping to %s", self._uart.desc)
        self.send_checked(OW_CMD_PING, addr=module, op="ping")
        return True

    def get_version(self, module: int = 0) -> str:
        """Query the firmware version string.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        r = self.send_checked(OW_CMD_VERSION, addr=module, op="get_version")
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

    def echo(self, module: int = 0, echo_data: bytes | bytearray = b"Hello LIFU!") -> tuple[bytes, int]:
        """Echo a payload through the device.

        Raises:
            TypeError: If *echo_data* is not bytes/bytearray.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        if not isinstance(echo_data, (bytes, bytearray)):
            raise TypeError("echo_data must be bytes or bytearray")
        r = self.send_checked(OW_CMD_ECHO, addr=module, data=bytearray(echo_data), op="echo")
        return bytes(r.data[:r.data_len]), r.data_len

    def get_hardware_id(self, module: int = 0, raw_hex: bool = False) -> str:
        """Read the device hardware ID.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError: If the payload length is wrong.
        """
        r = self.send_checked(OW_CMD_HWID, addr=module, op="get_hardware_id")
        if r.data_len < HW_ID_DATA_LENGTH:
            raise LIFUProtocolError(
                f"{self._uart.desc}: HWID payload too short "
                f"(got {r.data_len}, expected {HW_ID_DATA_LENGTH})",
                code=LIFU_ERR_BAD_PAYLOAD_LENGTH,
            )
        hwid = r.data[:HW_ID_DATA_LENGTH].hex()
        return hwid if raw_hex else format_hwid(hwid)

    def toggle_led(self, module: int = 0) -> bool:
        """Toggle the device's indicator LED.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        self.send_checked(OW_CMD_TOGGLE_LED, addr=module, op="toggle_led")
        return True

    def soft_reset(self, module: int = 0) -> bool:
        """Perform a soft reset on the device.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        self.send_checked(OW_CMD_RESET, addr=module, op="soft_reset")
        return True

    def enter_dfu(self, module: int = 0, reserved: int = 0x00) -> bool:
        """Reboot the device into DFU mode.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError.
        """
        self.send_checked(OW_CMD_DFU, addr=module, op="enter_dfu", reserved=reserved)
        return True

    # ------------------------------------------------------------------
    # User configuration helpers
    # ------------------------------------------------------------------

    def read_config(self, module: int = 0) -> LifuUserConfig:
        """Read the user configuration from device flash.

        Args:
            module: Target module address (default 0).

        Returns:
            Parsed LifuUserConfig.

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError: If the response payload cannot be parsed.
        """
        log.debug("Reading user config from %s ...", self._uart.desc)
        r = self.send_checked(OW_CMD_USR_CFG, addr=module, reserved=0, op="read_config")
        try:
            config = LifuUserConfig.from_wire_bytes(r.data)
        except (ValueError, struct.error) as exc:
            raise LIFUProtocolError(
                f"{self._uart.desc}: failed to parse config response: {exc}",
                code=LIFU_ERR_BAD_PAYLOAD_FORMAT,
            ) from exc
        log.debug("Read config: seq=%d, json_len=%d", config.header.seq, config.header.json_len)
        return config

    def write_config(self, config: LifuUserConfig, module: int = 0) -> LifuUserConfig:
        """Write user configuration to device flash.

        Args:
            config: LifuUserConfig to write.
            module: Target module address (default 0).

        Returns:
            Updated LifuUserConfig (with new seq/crc from the device).

        Raises:
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError: If the response header cannot be parsed.
        """
        wire_data = config.to_wire_bytes()
        log.debug("Writing config to %s: %d bytes", self._uart.desc, len(wire_data))
        r = self.send_checked(OW_CMD_USR_CFG, addr=module, reserved=1,
                              data=bytearray(wire_data), op="write_config")
        try:
            updated_header = LifuUserConfigHeader.from_bytes(r.data[:16])
        except (ValueError, IndexError) as exc:
            raise LIFUProtocolError(
                f"{self._uart.desc}: failed to parse write response: {exc}",
                code=LIFU_ERR_BAD_PAYLOAD_FORMAT,
            ) from exc
        updated_config = LifuUserConfig(header=updated_header, json_data=config.json_data)
        log.debug("Config written: new seq=%d", updated_config.header.seq)
        return updated_config

    def write_config_json(self, json_str: str, module: int = 0) -> LifuUserConfig:
        """Write user configuration from a JSON string.

        Raises:
            ValueError: If *json_str* is not valid JSON.
            LIFUNotConnectedError, LIFUCommunicationError, LIFUDeviceError,
            LIFUProtocolError.
        """
        try:
            config = LifuUserConfig()
            config.set_json_str(json_str)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc
        return self.write_config(config, module=module)
