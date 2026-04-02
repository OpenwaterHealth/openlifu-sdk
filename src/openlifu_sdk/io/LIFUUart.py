from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from contextlib import suppress

import serial
import serial.tools.list_ports

from openlifu_sdk.io.LIFUConfig import OW_ACK, OW_CMD_NOP, OW_DATA, OW_ERROR, OW_RESP
from openlifu_sdk.io.LIFUSignal import LIFUSignal

# Packet structure constants
OW_START_BYTE = 0xAA
OW_END_BYTE = 0xDD
ID_COUNTER = 0  # Initializing the ID counter

log = logging.getLogger(__name__)

# CRC16-ccitt lookup table
crc16_tab = [
    0x0000,
    0x1021,
    0x2042,
    0x3063,
    0x4084,
    0x50A5,
    0x60C6,
    0x70E7,
    0x8108,
    0x9129,
    0xA14A,
    0xB16B,
    0xC18C,
    0xD1AD,
    0xE1CE,
    0xF1EF,
    0x1231,
    0x0210,
    0x3273,
    0x2252,
    0x52B5,
    0x4294,
    0x72F7,
    0x62D6,
    0x9339,
    0x8318,
    0xB37B,
    0xA35A,
    0xD3BD,
    0xC39C,
    0xF3FF,
    0xE3DE,
    0x2462,
    0x3443,
    0x0420,
    0x1401,
    0x64E6,
    0x74C7,
    0x44A4,
    0x5485,
    0xA56A,
    0xB54B,
    0x8528,
    0x9509,
    0xE5EE,
    0xF5CF,
    0xC5AC,
    0xD58D,
    0x3653,
    0x2672,
    0x1611,
    0x0630,
    0x76D7,
    0x66F6,
    0x5695,
    0x46B4,
    0xB75B,
    0xA77A,
    0x9719,
    0x8738,
    0xF7DF,
    0xE7FE,
    0xD79D,
    0xC7BC,
    0x48C4,
    0x58E5,
    0x6886,
    0x78A7,
    0x0840,
    0x1861,
    0x2802,
    0x3823,
    0xC9CC,
    0xD9ED,
    0xE98E,
    0xF9AF,
    0x8948,
    0x9969,
    0xA90A,
    0xB92B,
    0x5AF5,
    0x4AD4,
    0x7AB7,
    0x6A96,
    0x1A71,
    0x0A50,
    0x3A33,
    0x2A12,
    0xDBFD,
    0xCBDC,
    0xFBBF,
    0xEB9E,
    0x9B79,
    0x8B58,
    0xBB3B,
    0xAB1A,
    0x6CA6,
    0x7C87,
    0x4CE4,
    0x5CC5,
    0x2C22,
    0x3C03,
    0x0C60,
    0x1C41,
    0xEDAE,
    0xFD8F,
    0xCDEC,
    0xDDCD,
    0xAD2A,
    0xBD0B,
    0x8D68,
    0x9D49,
    0x7E97,
    0x6EB6,
    0x5ED5,
    0x4EF4,
    0x3E13,
    0x2E32,
    0x1E51,
    0x0E70,
    0xFF9F,
    0xEFBE,
    0xDFDD,
    0xCFFC,
    0xBF1B,
    0xAF3A,
    0x9F59,
    0x8F78,
    0x9188,
    0x81A9,
    0xB1CA,
    0xA1EB,
    0xD10C,
    0xC12D,
    0xF14E,
    0xE16F,
    0x1080,
    0x00A1,
    0x30C2,
    0x20E3,
    0x5004,
    0x4025,
    0x7046,
    0x6067,
    0x83B9,
    0x9398,
    0xA3FB,
    0xB3DA,
    0xC33D,
    0xD31C,
    0xE37F,
    0xF35E,
    0x02B1,
    0x1290,
    0x22F3,
    0x32D2,
    0x4235,
    0x5214,
    0x6277,
    0x7256,
    0xB5EA,
    0xA5CB,
    0x95A8,
    0x8589,
    0xF56E,
    0xE54F,
    0xD52C,
    0xC50D,
    0x34E2,
    0x24C3,
    0x14A0,
    0x0481,
    0x7466,
    0x6447,
    0x5424,
    0x4405,
    0xA7DB,
    0xB7FA,
    0x8799,
    0x97B8,
    0xE75F,
    0xF77E,
    0xC71D,
    0xD73C,
    0x26D3,
    0x36F2,
    0x0691,
    0x16B0,
    0x6657,
    0x7676,
    0x4615,
    0x5634,
    0xD94C,
    0xC96D,
    0xF90E,
    0xE92F,
    0x99C8,
    0x89E9,
    0xB98A,
    0xA9AB,
    0x5844,
    0x4865,
    0x7806,
    0x6827,
    0x18C0,
    0x08E1,
    0x3882,
    0x28A3,
    0xCB7D,
    0xDB5C,
    0xEB3F,
    0xFB1E,
    0x8BF9,
    0x9BD8,
    0xABBB,
    0xBB9A,
    0x4A75,
    0x5A54,
    0x6A37,
    0x7A16,
    0x0AF1,
    0x1AD0,
    0x2AB3,
    0x3A92,
    0xFD2E,
    0xED0F,
    0xDD6C,
    0xCD4D,
    0xBDAA,
    0xAD8B,
    0x9DE8,
    0x8DC9,
    0x7C26,
    0x6C07,
    0x5C64,
    0x4C45,
    0x3CA2,
    0x2C83,
    0x1CE0,
    0x0CC1,
    0xEF1F,
    0xFF3E,
    0xCF5D,
    0xDF7C,
    0xAF9B,
    0xBFBA,
    0x8FD9,
    0x9FF8,
    0x6E17,
    0x7E36,
    0x4E55,
    0x5E74,
    0x2E93,
    0x3EB2,
    0x0ED1,
    0x1EF0,
]


def util_crc16(buf):
    crc = 0xFFFF

    for byte in buf:
        crc = ((crc << 8) & 0xFFFF) ^ crc16_tab[((crc >> 8) ^ byte) & 0xFF]

    return crc


class UartPacket:
    def __init__(self, id=None, packet_type=None, command=None, addr=None, reserved=None, data=None, buffer=None):
        if buffer:
            self.from_buffer(buffer)
        else:
            self.id = id
            self.packet_type = packet_type
            self.command = command
            self.addr = addr
            self.reserved = reserved
            self.data = data
            self.data_len = len(data)
            self.crc = self.calculate_crc()

    def calculate_crc(self) -> int:
        crc_value = 0xFFFF
        packet = bytearray()
        packet.append(OW_START_BYTE)
        packet.extend(self.id.to_bytes(2, "big"))
        packet.append(self.packet_type)
        packet.append(self.command)
        packet.append(self.addr)
        packet.append(self.reserved)
        packet.extend(self.data_len.to_bytes(2, "big"))
        if self.data_len > 0:
            packet.extend(self.data)
        crc_value = util_crc16(packet[1:])
        return crc_value

    def to_bytes(self) -> bytes:
        buffer = bytearray()
        buffer.append(OW_START_BYTE)
        buffer.extend(self.id.to_bytes(2, "big"))
        buffer.append(self.packet_type)
        buffer.append(self.command)
        buffer.append(self.addr)
        buffer.append(self.reserved)
        buffer.extend(self.data_len.to_bytes(2, "big"))
        if self.data_len > 0:
            buffer.extend(self.data)
        crc_value = util_crc16(buffer[1:])
        buffer.extend(crc_value.to_bytes(2, "big"))
        buffer.append(OW_END_BYTE)
        return bytes(buffer)

    def from_buffer(self, buffer: bytes):
        if buffer[0] != OW_START_BYTE or buffer[-1] != OW_END_BYTE:
            raise ValueError("Invalid buffer format")

        self.id = int.from_bytes(buffer[1:3], "big")
        self.packet_type = buffer[3]
        self.command = buffer[4]
        self.addr = buffer[5]
        self.reserved = buffer[6]
        self.data_len = int.from_bytes(buffer[7:9], "big")
        self.data = bytearray(buffer[9 : 9 + self.data_len])
        crc_value = util_crc16(buffer[1 : 9 + self.data_len])
        self.crc = int.from_bytes(buffer[9 + self.data_len : 11 + self.data_len], "big")
        if self.crc != crc_value:
            raise ValueError("CRC mismatch")

    def print_packet(self):
        log.info("UartPacket:")
        log.info("  Packet ID: %s", self.id)
        log.info("  Packet Type: %s", hex(self.packet_type))
        log.info("  Command: %s", hex(self.command))
        log.info("  Address: %s", hex(self.addr))
        log.info("  Reserved: %s", hex(self.reserved))
        log.info("  Data Length: %s", self.data_len)
        if self.data_len > 0:
            log.info("  Data: %s", self.data.hex())
        else:
            log.info("  Data: None")
        log.info("  CRC: %s", hex(self.crc))


class LIFUUart:
    def __init__(self, vid, pid, baudrate=921600, timeout=10, align=0, desc="VCP", demo_mode=False, async_mode=False):
        """
        Initialize the UART instance.

        Args:
            vid (int): Vendor ID of the USB device.
            pid (int): Product ID of the USB device.
            baudrate (int): Communication speed.
            timeout (int): Read timeout in seconds.
            align (int): Data alignment parameter.
            desc (str): Descriptor for the device (e.g. "TX" or "HV").
            demo_mode (bool): If True, simulate the connection.
            async_mode (bool): If True, use asynchronous mode.
        """
        self.vid = vid
        self.pid = pid
        self.port = None
        self.baudrate = baudrate
        self.timeout = timeout
        self.align = align
        self.packet_count = 0
        self.serial = None
        self.running = False
        self.asyncMode = async_mode
        self.monitoring_task = None
        self.demo_mode = demo_mode
        self.descriptor = desc
        self.read_thread = None
        self.read_buffer = []
        self.last_rx = time.monotonic()
        self.demo_responses = []  # Predefined responses for testing

        # Signals: each signal emits (descriptor, port or data)
        self.signal_connect = LIFUSignal()
        self.signal_disconnect = LIFUSignal()
        self.signal_data_received = LIFUSignal()

        if async_mode:
            self.loop = asyncio.get_event_loop()
            self.response_queues = {}  # Dictionary to map packet IDs to response queues
            self.response_lock = threading.Lock()  # Lock for thread-safe access to response_queues

    def connect(self):
        """Open the serial port."""
        if self.demo_mode:
            log.info("Demo mode: Simulating UART connection.")
            self.signal_connect.emit(self.descriptor, "demo_mode")
            return
        try:
            self.serial = serial.Serial(port=self.port, baudrate=self.baudrate, timeout=self.timeout)
            log.info("Connected to UART on port %s.", self.port)
            self.signal_connect.emit(self.descriptor, self.port)

            if self.asyncMode:
                log.info("Starting read thread for %s.", self.descriptor)
                self.running = True
                self.read_thread = threading.Thread(target=self._read_data)
                self.read_thread.daemon = True
                self.read_thread.start()
        except serial.SerialException as se:
            log.error("Failed to connect to %s: %s", self.port, se)
            self.running = False
            self.port = None
        except Exception as e:
            raise e

    def disconnect(self):
        """Close the serial port."""
        self.running = False
        if self.demo_mode:
            log.info("Demo mode: Simulating UART disconnection.")
            self.signal_disconnect.emit(self.descriptor, "demo_mode")
            return

        if self.read_thread:
            self.read_thread.join()
        if self.serial and self.serial.is_open:
            self.serial.close()
            self.serial = None
        log.info("Disconnected from UART.")
        self.signal_disconnect.emit(self.descriptor, self.port)
        self.port = None

    def is_connected(self) -> bool:
        """
        Check if the device is connected.

        Returns:
            bool: True if connected, False otherwise.
        """
        if self.demo_mode:
            return True
        return self.port is not None and self.serial is not None and self.serial.is_open

    def check_usb_status(self):
        """Check if the USB device is connected or disconnected."""
        device = self.list_vcp_with_vid_pid()
        if device and not self.port:
            log.debug("Device found; trying to connect.")
            self.port = device
            self.connect()
        elif not device and self.port:
            log.debug("Device removed; disconnecting.")
            self.running = False
            self.disconnect()

    async def monitor_usb_status(self, interval=1):
        """Periodically check for USB device connection."""
        if self.demo_mode:
            log.debug("Monitoring in demo mode.")
            self.connect()
            return
        while True:
            self.check_usb_status()
            await asyncio.sleep(interval)

    def start_monitoring(self, interval=1):
        """Start the periodic USB device connection check."""
        if self.demo_mode:
            log.debug("Monitoring in demo mode.")
            return
        if not self.monitoring_task and self.asyncMode:
            self.monitoring_task = asyncio.create_task(self.monitor_usb_status(interval))

    def stop_monitoring(self):
        """Stop the periodic USB device connection check."""
        if self.demo_mode:
            log.info("Monitoring in demo mode.")
            return
        if self.monitoring_task:
            self.monitoring_task.cancel()
            self.monitoring_task = None

    def list_vcp_with_vid_pid(self):
        """Find the USB device by VID and PID."""
        ports = serial.tools.list_ports.comports()
        for port in ports:
            if hasattr(port, "vid") and hasattr(port, "pid") and port.vid == self.vid and port.pid == self.pid:
                return port.device
        return None

    def reopen_after_reset(self, retries=5, delay=1.0):
        """
        Attempt to reopen the serial port after a device reset or disconnection.
        Returns True if reconnected, False otherwise.
        """
        try:
            if self.serial and self.serial.is_open:
                with suppress(Exception):
                    self.serial.close()
            self.serial = None
            self.port = None
            for _ in range(retries):
                time.sleep(delay)
                port = self.list_vcp_with_vid_pid()
                if port:
                    self.port = port
                    try:
                        self.connect()
                        log.info("Reconnected to UART on %s after reset.", self.port)
                        return True
                    except (OSError, ValueError) as e:
                        log.warning("Reconnect attempt failed: %s", e)
            log.error("Failed to reconnect UART after reset.")
            return False
        except (OSError, ValueError) as e:
            log.error("reopen_after_reset() error: %s", e)
            return False

    def _read_data(self, timeout=20):
        """Read data from the serial port in a separate thread."""
        log.debug("Starting data read loop for %s.", self.descriptor)
        if self.demo_mode:
            while self.running:
                if self.demo_responses:
                    data = self.demo_responses.pop(0)
                    log.info("Demo mode: Simulated data received: %s", data)
                    self.signal_data_received.emit(self.descriptor, "Demo Response")
                time.sleep(10)  # Simulate delay (10 second)
            return

        # In async mode, run the reading loop in a thread
        while self.running:
            try:
                if self.serial.in_waiting > 0:
                    data = self.serial.read(self.serial.in_waiting)
                    self.read_buffer.extend(data)
                    log.debug("Data received on %s: %s", self.descriptor, data)
                    # Attempt to parse a complete packet from read_buffer.
                    try:
                        # Note: Depending on your protocol, you might need to check for start/end bytes
                        # and possibly handle partial packets.
                        packet = UartPacket(buffer=bytes(self.read_buffer))
                        # Clear the buffer after a successful parse.

                        self.read_buffer = []
                        if self.asyncMode:
                            with self.response_lock:
                                # Check if a queue is waiting for this packet ID.
                                if packet.id in self.response_queues:
                                    self.response_queues[packet.id].put(packet)
                                elif packet.packet_type == OW_DATA and packet.id == 0:
                                    text = packet.data.decode("utf-8", errors="replace")
                                    self.signal_data_received.emit(self.descriptor, text)
                                else:
                                    log.warning("Received an unsolicited packet with ID %d", packet.id)
                        elif packet.packet_type == OW_DATA:
                            self.signal_data_received.emit(self.descriptor, OW_DATA)

                    except ValueError as ve:
                        log.error("Error parsing packet: %s", ve)
                else:
                    time.sleep(0.05)  # Brief sleep to avoid a busy loop
            except serial.SerialException as e:
                if "ClearCommError" in str(e):
                    log.warning("Device disconnected on %s", self.descriptor)
                else:
                    log.error("Serial _read_data error on %s: %s", self.descriptor, e)
                self.running = False

    def _tx(self, data: bytes):
        """Send data over UART."""
        if not self.serial or not self.serial.is_open:
            log.error("Serial port is not initialized.")
            return
        if self.demo_mode:
            log.info("Demo mode: Simulating data transmission: %s", data)
            return
        try:
            if self.align > 0:
                while len(data) % self.align != 0:
                    data += bytes([OW_END_BYTE])
            self.serial.write(data)
        except Exception as e:
            log.error("Error during transmission: %s", e)
            raise e

    def read_packet(self, timeout=20) -> UartPacket:
        """
        Read a packet from the UART interface.

        Returns:
            UartPacket: Parsed packet or an error packet if parsing fails.
        """
        start_time = time.monotonic()
        raw_data = b""
        count = 0

        while timeout == -1 or time.monotonic() - start_time < timeout:
            time.sleep(0.05)
            try:
                raw_data += self.serial.read_all()
            except serial.SerialException as e:
                if "ClearCommError" in str(e):
                    log.warning("Serial lost during read_packet; attempting reconnect...")
                    self.reopen_after_reset()
                    return None
                raise
            if raw_data:
                count += 1
                if count > 1:
                    break

        try:
            if not raw_data:
                raise ValueError("No data received from UART within timeout")
            packet = UartPacket(buffer=raw_data)
        except Exception as e:
            log.error("Error parsing packet: %s", e)
            packet = UartPacket(id=0, packet_type=OW_ERROR, command=0, addr=0, reserved=0, data=[])
            raise e

        return packet

    def send_packet(self, id=None, packetType=OW_ACK, command=OW_CMD_NOP, addr=0, reserved=0, data=None, timeout=20):
        """
        Send a packet over UART and, if not running, return a response packet.
        """
        try:
            if not self.serial or not self.serial.is_open:
                log.error("Cannot send packet. Serial port is not connected.")
                return None

            if id is None:
                self.packet_count += 1
                id = self.packet_count

            if data:
                if not isinstance(data, bytes | bytearray):
                    raise ValueError("Data must be bytes or bytearray")
                payload = data
                payload_length = len(payload)
            else:
                payload_length = 0
                payload = b""

            packet = bytearray()
            packet.append(OW_START_BYTE)
            packet.extend(id.to_bytes(2, "big"))
            packet.append(packetType)
            packet.append(command)
            packet.append(addr)
            packet.append(reserved)
            packet.extend(payload_length.to_bytes(2, "big"))
            if payload_length > 0:
                packet.extend(payload)

            crc_value = util_crc16(packet[1:])  # Exclude start byte
            packet.extend(crc_value.to_bytes(2, "big"))
            packet.append(OW_END_BYTE)

            self._tx(packet)

            if not self.asyncMode:
                try:
                    return self.read_packet(timeout=timeout)
                except serial.SerialException as e:
                    if "ClearCommError" in str(e):
                        log.warning("Serial handle lost after RESET, attempting reconnect...")
                        self.reopen_after_reset()
                        return None
                    raise
            else:
                response_queue = queue.Queue()
                with self.response_lock:
                    self.response_queues[id] = response_queue

                try:
                    # Wait for a response that matches the packet ID.
                    response = response_queue.get(timeout=timeout)
                    # Optionally, check that the response has the expected type and command.
                    if response.packet_type == OW_RESP and response.command == command:
                        return response
                    else:
                        log.error("Received unexpected response: %s", response)
                        return response
                except queue.Empty:
                    log.error("Timeout waiting for response to packet ID %d", id)
                    return None
                finally:
                    with self.response_lock:
                        # Clean up the queue entry regardless of outcome.
                        self.response_queues.pop(id, None)

        except ValueError as ve:
            log.error("Validation error in send_packet: %s", ve)
            raise
        except Exception as e:
            log.error("Unexpected error in send_packet: %s", e)
            raise

    def clear_buffer(self):
        """Clear the read buffer."""
        self.read_buffer = []

    def run_coroutine(self, coro):
        """Run a coroutine using the internal event loop."""
        if not self.loop.is_running():
            return self.loop.run_until_complete(coro)
        else:
            return asyncio.create_task(coro)

    def add_demo_response(self, response: bytes):
        """Add a predefined response for demo mode."""
        if self.demo_mode:
            self.demo_responses.append(response)
        else:
            log.warning("Cannot add demo response when not in demo mode.")

    def print(self):
        """Print the current UART configuration."""
        log.info("    Serial Port: %s", self.port)
        log.info("    Serial Baud: %s", self.baudrate)
