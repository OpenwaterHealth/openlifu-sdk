from __future__ import annotations

import logging
import queue
import threading
import time

import serial
import serial.tools.list_ports

from .LIFUConfig import (
    OW_DATA, OW_START_BYTE, OW_END_BYTE, OW_ACK, OW_CMD_NOP,
    DEFAULT_TIMEOUT, USB_POLL_INTERVAL, MAX_DATA_LEN,
)
from .uart_packet import OWUartPacket
from .signal import OWSignal

log = logging.getLogger("OWUart")


class _PendingCommand:
    __slots__ = ("packet_id", "packet_bytes", "timeout", "event", "response", "error")

    def __init__(self, packet_id: int, packet_bytes: bytes, timeout: float):
        self.packet_id = packet_id
        self.packet_bytes = packet_bytes
        self.timeout = timeout
        self.event = threading.Event()
        self.response: OWUartPacket | None = None
        self.error: str | None = None


class OWUart:
    def __init__(self, vid: int, pid: int, baudrate: int = 921600,
                 timeout: float = DEFAULT_TIMEOUT, desc: str = "VCP"):
        self.vid = vid
        self.pid = pid
        self.baudrate = baudrate
        self.timeout = timeout
        self.desc = desc

        self._serial: serial.Serial | None = None
        self._connected = False
        self._running = False

        self._send_queue: queue.Queue[_PendingCommand] = queue.Queue()
        self._pending: dict[int, _PendingCommand] = {}
        self._pending_lock = threading.Lock()

        self._id_counter = 0
        self._id_lock = threading.Lock()

        self._sender_thread: threading.Thread | None = None
        self._reader_thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None

        # Signals  -- (desc, ...) for all emissions
        self.signal_connected = OWSignal()       # (desc, port)
        self.signal_disconnected = OWSignal()    # (desc, port)
        self.signal_data_received = OWSignal()   # (desc, OWUartPacket)
        self.signal_error = OWSignal()           # (desc, packet_id, message)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # ID generation
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        with self._id_lock:
            self._id_counter = (self._id_counter + 1) % 0xFFFF
            if self._id_counter == 0:
                self._id_counter = 1
            return self._id_counter

    # ------------------------------------------------------------------
    # Port discovery
    # ------------------------------------------------------------------

    def _find_port(self) -> str | None:
        for port in serial.tools.list_ports.comports():
            if (hasattr(port, "vid") and hasattr(port, "pid")
                    and port.vid == self.vid and port.pid == self.pid):
                return port.device
        return None

    @staticmethod
    def _packet_summary(packet: OWUartPacket) -> str:
        return (
            f"id={packet.id} type=0x{packet.packet_type:02X} "
            f"cmd=0x{packet.command:02X} addr=0x{packet.addr:02X} "
            f"reserved=0x{packet.reserved:02X} data_len={packet.data_len}"
        )

    # ------------------------------------------------------------------
    # Connection (sync & async)
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        port = self._find_port()
        if not port:
            return False
        try:
            ser = serial.Serial(port, self.baudrate, timeout=0.1)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            self._serial = ser
            self._connected = True
            log.info("%s connected on %s", self.desc, port)
            return True
        except serial.SerialException as exc:
            log.error("%s connect failed: %s", self.desc, exc)
            return False

    def disconnect(self):
        if not self._connected and self._serial is None:
            return
        self._connected = False
        ser = self._serial
        self._serial = None
        if ser:
            try:
                ser.close()
            except Exception:
                pass
        self._fail_all_pending("Disconnected")

    def _fail_all_pending(self, reason: str):
        with self._pending_lock:
            entries = list(self._pending.values())
            self._pending.clear()
        for entry in entries:
            entry.error = reason
            entry.event.set()

    # ------------------------------------------------------------------
    # Async mode lifecycle
    # ------------------------------------------------------------------

    def start(self):
        if self._running:
            return
        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name=f"{self.desc}-monitor", daemon=True)
        self._sender_thread = threading.Thread(
            target=self._sender_loop, name=f"{self.desc}-sender", daemon=True)
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name=f"{self.desc}-reader", daemon=True)
        self._monitor_thread.start()
        self._sender_thread.start()
        self._reader_thread.start()

    def stop(self):
        if not self._running:
            return
        self._running = False
        # Unblock any pending commands
        self._fail_all_pending("Stopped")
        # Drain the send queue
        while True:
            try:
                entry = self._send_queue.get_nowait()
                entry.error = "Stopped"
                entry.event.set()
            except queue.Empty:
                break
        # Wait for threads to exit
        for t in (self._sender_thread, self._reader_thread, self._monitor_thread):
            if t is not None and t.is_alive():
                t.join(timeout=5)
        self._sender_thread = None
        self._reader_thread = None
        self._monitor_thread = None
        self.disconnect()

    # ------------------------------------------------------------------
    # Monitor thread – watches USB presence
    # ------------------------------------------------------------------

    def _monitor_loop(self):
        while self._running:
            try:
                port = self._find_port()
                if port and not self._connected:
                    try:
                        ser = serial.Serial(port, self.baudrate, timeout=0.1)
                        self._serial = ser
                        self._connected = True
                        log.info("%s connected on %s", self.desc, port)
                        self.signal_connected.emit(self.desc, port)
                    except serial.SerialException as exc:
                        log.error("%s auto-connect failed: %s", self.desc, exc)
                elif not port and self._connected:
                    log.info("%s disconnected", self.desc)
                    self.disconnect()
                    self.signal_disconnected.emit(self.desc, port)
            except Exception:
                log.exception("%s monitor error", self.desc)
            time.sleep(USB_POLL_INTERVAL)

    # ------------------------------------------------------------------
    # Sender thread – serialises outgoing commands
    # ------------------------------------------------------------------

    def _sender_loop(self):
        while self._running:
            try:
                entry = self._send_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            # Register as pending *before* writing
            with self._pending_lock:
                self._pending[entry.packet_id] = entry

            # Attempt to send
            ser = self._serial
            if ser is not None and ser.is_open:
                try:
                    ser.write(entry.packet_bytes)
                except (serial.SerialException, OSError) as exc:
                    log.error("%s write error: %s", self.desc, exc)
                    with self._pending_lock:
                        self._pending.pop(entry.packet_id, None)
                    entry.error = f"Send failed: {exc}"
                    entry.event.set()
                    self.signal_error.emit(self.desc, entry.packet_id, entry.error)
                    continue
            else:
                with self._pending_lock:
                    self._pending.pop(entry.packet_id, None)
                entry.error = "Not connected"
                entry.event.set()
                self.signal_error.emit(self.desc, entry.packet_id, entry.error)
                continue

            # Block until response or timeout (one command at a time)
            if not entry.event.wait(timeout=entry.timeout):
                with self._pending_lock:
                    self._pending.pop(entry.packet_id, None)
                entry.error = "Timeout"
                entry.event.set()
                self.signal_error.emit(self.desc, entry.packet_id, "Timeout")

    # ------------------------------------------------------------------
    # Reader thread – continuous packet reception
    # ------------------------------------------------------------------

    def _reader_loop(self):
        buf = bytearray()
        while self._running:
            if not self._connected:
                time.sleep(0.01)
                continue

            ser = self._serial
            if ser is None or not ser.is_open:
                time.sleep(0.01)
                continue

            try:
                waiting = ser.in_waiting
                data = ser.read(waiting) if waiting > 0 else ser.read(1)
            except (serial.SerialException, OSError):
                continue

            if not data:
                continue

            buf.extend(data)
            while True:
                pkt, consumed = self._extract_packet(buf)
                if consumed > 0:
                    buf = buf[consumed:]
                if pkt is not None:
                    self._dispatch_response(pkt)
                if consumed == 0:
                    break

    def _dispatch_response(self, packet: OWUartPacket):
        with self._pending_lock:
            entry = self._pending.pop(packet.id, None)
        if entry is not None:
            entry.response = packet
            entry.event.set()
        # Always emit so async listeners see every packet (including unsolicited)  
        # TODO: Consider separate signal for unsolicited packets vs responses to async commands      
        # self.signal_data_received.emit(self.desc, packet)
        if packet.packet_type == OW_DATA and packet.id == 0:
            # Unsolicited data packet with no ID – emit decoded text for convenience            
            text = packet.data.decode('utf-8', errors='replace')
            self.signal_data_received.emit(self.desc, text)
            
    # ------------------------------------------------------------------
    # Packet framing
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_packet(buf: bytearray):
        """Try to extract one packet from *buf*.

        Returns ``(packet, bytes_consumed)`` where *packet* is an
        `OWUartPacket` or ``None`` and *bytes_consumed* is how many
        leading bytes to strip from the buffer.
        """
        try:
            idx = buf.index(OW_START_BYTE)
        except ValueError:
            if buf:
                log.debug("%s parser discarded %d bytes without start byte", "OWUart", len(buf))
            return None, len(buf)  # no start byte – discard all

        if idx > 0:
            log.debug("%s parser skipped %d bytes before start byte", "OWUart", idx)
            return None, idx  # discard garbage before start byte

        if len(buf) < 9:
            return None, 0  # need more header bytes

        data_len = int.from_bytes(buf[7:9], "big")
        if data_len > MAX_DATA_LEN:
            log.warning("%s parser rejected oversized payload length=%d", "OWUart", data_len)
            return None, 1  # corrupted length – skip this start byte

        total_len = 12 + data_len  # header(9) + data + crc(2) + end(1)
        if len(buf) < total_len:
            return None, 0  # need more data

        if buf[total_len - 1] != OW_END_BYTE:
            log.debug("%s parser rejected packet with invalid end byte 0x%02X", "OWUart", buf[total_len - 1])
            return None, 1  # bad end byte – skip start

        try:
            pkt = OWUartPacket(buffer=bytes(buf[:total_len]))
            return pkt, total_len
        except ValueError:
            log.debug("%s parser rejected packet due to CRC/format validation failure", "OWUart")
            return None, 1  # CRC error – skip start

    # ------------------------------------------------------------------
    # Public send API
    # ------------------------------------------------------------------

    def send_packet(self, packet_type: int = OW_ACK, command: int = OW_CMD_NOP,
                    addr: int = 0, reserved: int = 0, data: bytearray | None = None,
                    timeout: float | None = None, packet_id: int | None = None) -> OWUartPacket | None:
        """Send a packet and **block** until the response arrives or *timeout* expires.

        Works in both sync (no threads) and async (threads running) mode.
        Returns the response `OWUartPacket`, or ``None`` on timeout / error.
        """
        if data is None:
            data = bytearray()
        if timeout is None:
            timeout = self.timeout
        if packet_id is None:
            packet_id = self._next_id()

        pkt = OWUartPacket(
            id=packet_id, packet_type=packet_type, command=command,
            addr=addr, reserved=reserved, data=data,
        )

        if self._running:
            return self._send_via_queue(pkt, timeout)
        return self._send_direct(pkt, timeout)

    def send_packet_async(self, packet_type: int = OW_ACK, command: int = OW_CMD_NOP,
                          addr: int = 0, reserved: int = 0, data: bytearray | None = None,
                          timeout: float | None = None, packet_id: int | None = None) -> int:
        """Queue a packet for sending **without blocking** (async mode only).

        The response is delivered via ``signal_data_received``.
        Timeout errors are delivered via ``signal_error``.
        Returns the packet ID so callers can correlate.
        """
        if not self._running:
            raise RuntimeError("Async mode not started – call start() first.")
        if data is None:
            data = bytearray()
        if timeout is None:
            timeout = self.timeout
        if packet_id is None:
            packet_id = self._next_id()

        pkt = OWUartPacket(
            id=packet_id, packet_type=packet_type, command=command,
            addr=addr, reserved=reserved, data=data,
        )
        entry = _PendingCommand(packet_id, pkt.to_bytes(), timeout)
        self._send_queue.put(entry)
        return packet_id

    # ------------------------------------------------------------------
    # Internal send helpers
    # ------------------------------------------------------------------

    def _send_via_queue(self, pkt: OWUartPacket, timeout: float) -> OWUartPacket | None:
        """Enqueue then block until the sender/reader pair complete the round-trip."""
        entry = _PendingCommand(pkt.id, pkt.to_bytes(), timeout)
        self._send_queue.put(entry)
        # The sender thread guarantees the event is set (response, timeout, or error).
        # Add generous extra time to account for commands queued ahead of this one.
        entry.event.wait(timeout=timeout + self._send_queue.qsize() * timeout + 5)
        if entry.error:
            return None
        return entry.response

    def _send_direct(self, pkt: OWUartPacket, timeout: float) -> OWUartPacket | None:
        """Sync-mode: write packet and read the response in the calling thread."""
        ser = self._serial
        if ser is None or not ser.is_open:
            self.signal_error.emit(self.desc, pkt.id, "Not connected")
            return None
        request_summary = self._packet_summary(pkt)
        discarded_bytes = 0
        try:
            try:
                discarded_bytes = ser.in_waiting
            except (serial.SerialException, OSError):
                discarded_bytes = 0
            if discarded_bytes:
                log.warning("%s sync send discarding %d buffered bytes before %s",
                            self.desc, discarded_bytes, request_summary)
            ser.reset_input_buffer()
            send_start = time.monotonic()
            ser.write(pkt.to_bytes())
        except (serial.SerialException, OSError) as exc:
            self.signal_error.emit(self.desc, pkt.id, str(exc))
            return None
        write_elapsed_ms = (time.monotonic() - send_start) * 1000.0
        log.debug("%s sync send wrote %s in %.2f ms", self.desc, request_summary, write_elapsed_ms)
        return self._read_response_sync(pkt.id, timeout, request_summary=request_summary)

    def _read_response_sync(self, expected_id: int, timeout: float,
                            request_summary: str | None = None) -> OWUartPacket | None:
        """Read packets in a loop until the one matching *expected_id* arrives."""
        buf = bytearray()
        deadline = time.monotonic() + timeout
        started_at = time.monotonic()
        total_bytes = 0
        read_calls = 0
        request_label = request_summary or f"id={expected_id}"

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ser = self._serial
            if ser is None or not ser.is_open:
                break
            try:
                original_timeout = ser.timeout
                ser.timeout = min(max(remaining, 0.0), 0.1)
                waiting = ser.in_waiting
                data = ser.read(waiting) if waiting > 0 else ser.read(1)
            except (serial.SerialException, OSError):
                break
            finally:
                try:
                    ser.timeout = original_timeout
                except Exception:
                    pass

            if data:
                read_calls += 1
                total_bytes += len(data)
                log.debug("%s sync read received %d bytes while waiting for %s",
                          self.desc, len(data), request_label)
                buf.extend(data)
                while True:
                    pkt, consumed = self._extract_packet(buf)
                    if consumed > 0:
                        buf = buf[consumed:]
                    if pkt is not None:
                        if pkt.id == expected_id:
                            elapsed_ms = (time.monotonic() - started_at) * 1000.0
                            log.debug(
                                "%s sync read matched %s after %.2f ms (%d reads, %d bytes)",
                                self.desc,
                                self._packet_summary(pkt),
                                elapsed_ms,
                                read_calls,
                                total_bytes,
                            )
                            return pkt
                        # Unsolicited packet – emit for any listeners
                        log.debug("%s sync read observed unrelated packet while waiting for %s: %s",
                                  self.desc, request_label, self._packet_summary(pkt))
                        
                        text = pkt.data.decode('utf-8', errors='replace')
                        #TODO Send the packet to listeners
                        # self.signal_data_received.emit(self.desc, pkt)
                        if pkt.packet_type == OW_DATA and pkt.id == 0:
                            self.signal_data_received.emit(self.desc, text)
                    if consumed == 0:
                        break

        elapsed_ms = (time.monotonic() - started_at) * 1000.0
        log.warning("%s sync read timed out after %.2f ms waiting for %s (%d reads, %d bytes, %d buffered bytes remain)",
                    self.desc, elapsed_ms, request_label, read_calls, total_bytes, len(buf))
        self.signal_error.emit(self.desc, expected_id, "Timeout")
        return None
