"""Async PyQt6 example – USB hot-plug detection, non-blocking commands,
and unsolicited-message handling via OWSignal → pyqtSignal bridge.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openlifu_sdk.io import LIFUInterface
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QTextEdit, QPushButton,
    QVBoxLayout, QHBoxLayout, QWidget,
)

from ow_comms.config import OW_CMD_PING, OW_CMD_VERSION


class _Bridge(QObject):
    """Bridges OWSignal (emitted on worker threads) to pyqtSignals
    so that slots execute safely on the Qt main thread."""
    sig_connected = pyqtSignal(str, str)       # desc, port
    sig_disconnected = pyqtSignal(str)         # desc
    sig_data = pyqtSignal(str, object)         # desc, OWUartPacket
    sig_error = pyqtSignal(str, int, str)      # desc, packet_id, message


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OW UART Async Demo")
        self.resize(640, 400)

        self._iface = LIFUInterface()
        self._bridge = _Bridge()

        # Wire OWSignals → bridge (thread-safe pyqtSignals)
        for component in (self._iface.transmitter, self._iface.console):
            component.signal_connected.connect(self._bridge.sig_connected.emit)
            component.signal_disconnected.connect(self._bridge.sig_disconnected.emit)
            component.signal_data_received.connect(self._bridge.sig_data.emit)
            component.signal_error.connect(self._bridge.sig_error.emit)

        # Wire bridge to UI slots (main thread)
        self._bridge.sig_connected.connect(self._on_connected)
        self._bridge.sig_disconnected.connect(self._on_disconnected)
        self._bridge.sig_data.connect(self._on_data)
        self._bridge.sig_error.connect(self._on_error)

        # UI
        self._log = QTextEdit()
        self._log.setReadOnly(True)

        btn_ping_tx = QPushButton("Ping Transmitter")
        btn_ping_tx.clicked.connect(lambda: self._send(self._iface.transmitter, OW_CMD_PING))

        btn_ver_tx = QPushButton("Version Transmitter")
        btn_ver_tx.clicked.connect(lambda: self._send(self._iface.transmitter, OW_CMD_VERSION))

        btn_ping_con = QPushButton("Ping Console")
        btn_ping_con.clicked.connect(lambda: self._send(self._iface.console, OW_CMD_PING))

        btn_row = QHBoxLayout()
        btn_row.addWidget(btn_ping_tx)
        btn_row.addWidget(btn_ver_tx)
        btn_row.addWidget(btn_ping_con)

        layout = QVBoxLayout()
        layout.addLayout(btn_row)
        layout.addWidget(self._log)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        # Start async mode (monitor + sender + reader threads)
        self._iface.start()

    # -- slots (main thread) -------------------------------------------

    def _on_connected(self, desc: str, port: str):
        self._log.append(f"[{desc}] Connected on {port}")

    def _on_disconnected(self, desc: str):
        self._log.append(f"[{desc}] Disconnected")

    def _on_data(self, desc: str, pkt):
        self._log.append(
            f"[{desc}] RX  id={pkt.id}  type=0x{pkt.packet_type:02X}  "
            f"cmd=0x{pkt.command:02X}  data={pkt.data.hex() if pkt.data_len else '—'}"
        )

    def _on_error(self, desc: str, pkt_id: int, msg: str):
        self._log.append(f"[{desc}] ERROR  id={pkt_id}  {msg}")

    # -- helpers -------------------------------------------------------

    def _send(self, component, command):
        if not component.is_connected():
            self._log.append(f"[{component.uart.desc}] Not connected")
            return
        pid = component.send_async(command)
        self._log.append(f"[{component.uart.desc}] TX  id={pid}  cmd=0x{command:02X}")

    def closeEvent(self, event):
        self._iface.stop()
        super().closeEvent(event)


if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
