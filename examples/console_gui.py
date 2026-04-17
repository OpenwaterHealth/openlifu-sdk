"""Console PyQt5 GUI – async UART with live status and controls.

Usage:
    python console_gui.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openlifu_sdk import LIFUInterface
from PyQt6.QtCore import QObject, pyqtSignal, QTimer
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QTextEdit, QPushButton, QLabel,
    QVBoxLayout, QHBoxLayout, QGridLayout, QWidget, QGroupBox,
    QSpinBox, QDoubleSpinBox, QTabWidget, QFrame,
)

class _Bridge(QObject):
    """Thread-safe bridge from OWSignal to pyqtSignal."""
    sig_connected = pyqtSignal(str, str)
    sig_disconnected = pyqtSignal(str)
    sig_data = pyqtSignal(str, object)
    sig_error = pyqtSignal(str, int, str)


class StatusIndicator(QFrame):
    """Small colored circle indicating connection state."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self._connected = False
        self._update_color()

    def set_connected(self, connected: bool):
        self._connected = connected
        self._update_color()

    def _update_color(self):
        color = "#2ecc71" if self._connected else "#e74c3c"
        self.setStyleSheet(
            f"background-color: {color}; border-radius: 8px; border: 1px solid #555;"
        )


class ConsoleGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LIFU Console Controller")
        self.resize(800, 700)

        self._iface = LIFUInterface()
        self._bridge = _Bridge()
        self._console_connected = False

        # Wire OWSignals -> bridge
        self._iface.console.signal_connected.connect(self._bridge.sig_connected.emit)
        self._iface.console.signal_disconnected.connect(self._bridge.sig_disconnected.emit)
        self._iface.console.signal_data_received.connect(self._bridge.sig_data.emit)
        self._iface.console.signal_error.connect(self._bridge.sig_error.emit)

        # Wire bridge -> UI
        self._bridge.sig_connected.connect(self._on_connected)
        self._bridge.sig_disconnected.connect(self._on_disconnected)
        self._bridge.sig_data.connect(self._on_data)
        self._bridge.sig_error.connect(self._on_error)

        self._build_ui()

        # Polling timer for live readings
        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(self._poll_readings)

        # Start async mode
        self._iface.start()
        self._set_controls_enabled(False)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # --- Status bar at top ---
        status_bar = QHBoxLayout()
        self._status_indicator = StatusIndicator()
        self._status_label = QLabel("Console: Disconnected")
        self._status_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        self._fw_label = QLabel("")
        status_bar.addWidget(self._status_indicator)
        status_bar.addWidget(self._status_label)
        status_bar.addStretch()
        status_bar.addWidget(self._fw_label)

        # --- Tabs ---
        tabs = QTabWidget()
        tabs.addTab(self._build_info_tab(), "Info")
        tabs.addTab(self._build_power_tab(), "Power")
        tabs.addTab(self._build_fan_tab(), "Fans")
        tabs.addTab(self._build_rgb_tab(), "RGB")
        tabs.addTab(self._build_vmon_tab(), "Voltage Monitor")
        self._tabs = tabs

        # --- Log ---
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(180)

        # --- Layout ---
        layout = QVBoxLayout()
        layout.addLayout(status_bar)
        layout.addWidget(tabs)
        layout.addWidget(QLabel("Log:"))
        layout.addWidget(self._log)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

    def _build_info_tab(self) -> QWidget:
        w = QWidget()
        layout = QGridLayout()

        btn_ping = QPushButton("Ping")
        btn_ping.clicked.connect(self._cmd_ping)
        btn_version = QPushButton("Get Version")
        btn_version.clicked.connect(self._cmd_version)
        btn_echo = QPushButton("Echo Test")
        btn_echo.clicked.connect(self._cmd_echo)
        btn_hwid = QPushButton("Get Hardware ID")
        btn_hwid.clicked.connect(self._cmd_hwid)
        btn_toggle_led = QPushButton("Toggle LED")
        btn_toggle_led.clicked.connect(self._cmd_toggle_led)

        self._lbl_ping = QLabel("—")
        self._lbl_version = QLabel("—")
        self._lbl_echo = QLabel("—")
        self._lbl_hwid = QLabel("—")

        layout.addWidget(btn_ping, 0, 0)
        layout.addWidget(self._lbl_ping, 0, 1)
        layout.addWidget(btn_version, 1, 0)
        layout.addWidget(self._lbl_version, 1, 1)
        layout.addWidget(btn_echo, 2, 0)
        layout.addWidget(self._lbl_echo, 2, 1)
        layout.addWidget(btn_hwid, 3, 0)
        layout.addWidget(self._lbl_hwid, 3, 1)
        layout.addWidget(btn_toggle_led, 4, 0)
        layout.setRowStretch(5, 1)

        w.setLayout(layout)
        return w

    def _build_power_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout()

        # Temperatures
        temp_group = QGroupBox("Temperatures")
        temp_layout = QGridLayout()
        self._lbl_temp1 = QLabel("—")
        self._lbl_temp2 = QLabel("—")
        btn_refresh_temp = QPushButton("Refresh")
        btn_refresh_temp.clicked.connect(self._cmd_refresh_temps)
        temp_layout.addWidget(QLabel("Temp 1:"), 0, 0)
        temp_layout.addWidget(self._lbl_temp1, 0, 1)
        temp_layout.addWidget(QLabel("Temp 2:"), 1, 0)
        temp_layout.addWidget(self._lbl_temp2, 1, 1)
        temp_layout.addWidget(btn_refresh_temp, 0, 2, 2, 1)
        temp_group.setLayout(temp_layout)

        # 12V
        v12_group = QGroupBox("12V Rail")
        v12_layout = QHBoxLayout()
        self._lbl_12v = QLabel("—")
        btn_12v_on = QPushButton("Turn ON")
        btn_12v_on.clicked.connect(self._cmd_12v_on)
        btn_12v_off = QPushButton("Turn OFF")
        btn_12v_off.clicked.connect(self._cmd_12v_off)
        btn_12v_status = QPushButton("Status")
        btn_12v_status.clicked.connect(self._cmd_12v_status)
        v12_layout.addWidget(QLabel("12V:"))
        v12_layout.addWidget(self._lbl_12v)
        v12_layout.addWidget(btn_12v_on)
        v12_layout.addWidget(btn_12v_off)
        v12_layout.addWidget(btn_12v_status)
        v12_group.setLayout(v12_layout)

        # HV
        hv_group = QGroupBox("HV Rail")
        hv_layout = QGridLayout()
        self._lbl_hv = QLabel("—")
        self._lbl_hv_status = QLabel("—")
        self._spin_hv = QDoubleSpinBox()
        self._spin_hv.setRange(5.0, 100.0)
        self._spin_hv.setValue(50.0)
        self._spin_hv.setSuffix(" V")
        self._spin_hv.setDecimals(1)
        btn_hv_set = QPushButton("Set Voltage")
        btn_hv_set.clicked.connect(self._cmd_hv_set)
        btn_hv_on = QPushButton("HV ON")
        btn_hv_on.clicked.connect(self._cmd_hv_on)
        btn_hv_off = QPushButton("HV OFF")
        btn_hv_off.clicked.connect(self._cmd_hv_off)
        btn_hv_read = QPushButton("Read HV")
        btn_hv_read.clicked.connect(self._cmd_hv_read)
        btn_hv_status = QPushButton("Status")
        btn_hv_status.clicked.connect(self._cmd_hv_status)

        hv_layout.addWidget(self._spin_hv, 0, 0)
        hv_layout.addWidget(btn_hv_set, 0, 1)
        hv_layout.addWidget(QLabel("Readback:"), 0, 2)
        hv_layout.addWidget(self._lbl_hv, 0, 3)
        hv_layout.addWidget(btn_hv_read, 0, 4)
        hv_layout.addWidget(btn_hv_on, 1, 0)
        hv_layout.addWidget(btn_hv_off, 1, 1)
        hv_layout.addWidget(QLabel("Status:"), 1, 2)
        hv_layout.addWidget(self._lbl_hv_status, 1, 3)
        hv_layout.addWidget(btn_hv_status, 1, 4)
        hv_group.setLayout(hv_layout)

        layout.addWidget(temp_group)
        layout.addWidget(v12_group)
        layout.addWidget(hv_group)
        layout.addStretch()
        w.setLayout(layout)
        return w

    def _build_fan_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout()

        for fan_id, name in [(0, "Bottom Fan"), (1, "Top Fan")]:
            group = QGroupBox(name)
            g_layout = QHBoxLayout()

            spin = QSpinBox()
            spin.setRange(0, 100)
            spin.setValue(50)
            spin.setSuffix(" %")

            lbl = QLabel("—")

            btn_set = QPushButton("Set")
            btn_get = QPushButton("Read")

            btn_set.clicked.connect(lambda _, fid=fan_id, s=spin: self._cmd_fan_set(fid, s.value()))
            btn_get.clicked.connect(lambda _, fid=fan_id, label=lbl: self._cmd_fan_get(fid, label))

            g_layout.addWidget(spin)
            g_layout.addWidget(btn_set)
            g_layout.addWidget(QLabel("Current:"))
            g_layout.addWidget(lbl)
            g_layout.addWidget(btn_get)
            group.setLayout(g_layout)
            layout.addWidget(group)

            if fan_id == 0:
                self._lbl_fan0 = lbl
                self._spin_fan0 = spin
            else:
                self._lbl_fan1 = lbl
                self._spin_fan1 = spin

        layout.addStretch()
        w.setLayout(layout)
        return w

    def _build_rgb_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout()

        self._lbl_rgb = QLabel("Current: —")
        self._lbl_rgb.setStyleSheet("font-size: 14px;")

        btn_layout = QHBoxLayout()
        for state, name, color in [
            (0, "OFF", "#888"),
            (1, "RED", "#e74c3c"),
            (2, "BLUE", "#3498db"),
            (3, "GREEN", "#2ecc71"),
        ]:
            btn = QPushButton(name)
            btn.setStyleSheet(
                f"background-color: {color}; color: white; font-weight: bold; "
                f"padding: 10px 20px; border-radius: 4px;"
            )
            btn.clicked.connect(lambda _, s=state: self._cmd_rgb_set(s))
            btn_layout.addWidget(btn)

        btn_read = QPushButton("Read State")
        btn_read.clicked.connect(self._cmd_rgb_get)

        layout.addWidget(self._lbl_rgb)
        layout.addLayout(btn_layout)
        layout.addWidget(btn_read)
        layout.addStretch()
        w.setLayout(layout)
        return w

    def _build_vmon_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout()

        self._vmon_labels: list[tuple[QLabel, QLabel, QLabel]] = []
        grid = QGridLayout()
        grid.addWidget(QLabel("Channel"), 0, 0)
        grid.addWidget(QLabel("Raw ADC"), 0, 1)
        grid.addWidget(QLabel("Voltage"), 0, 2)
        grid.addWidget(QLabel("Converted"), 0, 3)

        for i in range(8):
            lbl_raw = QLabel("—")
            lbl_v = QLabel("—")
            lbl_cv = QLabel("—")
            grid.addWidget(QLabel(f"CH{i}"), i + 1, 0)
            grid.addWidget(lbl_raw, i + 1, 1)
            grid.addWidget(lbl_v, i + 1, 2)
            grid.addWidget(lbl_cv, i + 1, 3)
            self._vmon_labels.append((lbl_raw, lbl_v, lbl_cv))

        btn_layout = QHBoxLayout()
        btn_read = QPushButton("Read Once")
        btn_read.clicked.connect(self._cmd_vmon_read)
        self._btn_vmon_poll = QPushButton("Start Polling")
        self._btn_vmon_poll.setCheckable(True)
        self._btn_vmon_poll.clicked.connect(self._toggle_vmon_polling)
        btn_layout.addWidget(btn_read)
        btn_layout.addWidget(self._btn_vmon_poll)

        layout.addLayout(grid)
        layout.addLayout(btn_layout)
        layout.addStretch()
        w.setLayout(layout)
        return w

    # ------------------------------------------------------------------
    # Connection events
    # ------------------------------------------------------------------

    def _on_connected(self, desc: str, port: str):
        if desc != "LIFUConsole":
            return
        self._console_connected = True
        self._status_indicator.set_connected(True)
        self._status_label.setText(f"Console: Connected ({port})")
        self._set_controls_enabled(True)
        self._append_log(f"Connected on {port}")
        # Fetch version
        self._cmd_version()

    def _on_disconnected(self, desc: str):
        if desc != "LIFUConsole":
            return
        self._console_connected = False
        self._status_indicator.set_connected(False)
        self._status_label.setText("Console: Disconnected")
        self._fw_label.setText("")
        self._set_controls_enabled(False)
        self._poll_timer.stop()
        self._btn_vmon_poll.setChecked(False)
        self._btn_vmon_poll.setText("Start Polling")
        self._append_log("Disconnected")

    def _on_data(self, desc: str, pkt):
        if desc != "LIFUConsole":
            return
        self._append_log(
            f"RX id={pkt.id} type=0x{pkt.packet_type:02X} "
            f"cmd=0x{pkt.command:02X} len={pkt.data_len}"
        )

    def _on_error(self, desc: str, pkt_id: int, msg: str):
        if desc != "LIFUConsole":
            return
        self._append_log(f"ERROR id={pkt_id} {msg}")

    def _set_controls_enabled(self, enabled: bool):
        self._tabs.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Commands (run in worker thread via send_packet, blocking)
    # ------------------------------------------------------------------

    def _run_cmd(self, func, *args, **kwargs):
        """Run a blocking console command. Since we're in async mode,
        send_packet already queues through the sender thread."""
        if not self._console_connected:
            self._append_log("Not connected")
            return None
        try:
            return func(*args, **kwargs)
        except Exception as e:
            self._append_log(f"Error: {e}")
            return None

    # Info
    def _cmd_ping(self):
        r = self._run_cmd(self._iface.console.ping)
        if r is not None:
            self._lbl_ping.setText("OK" if r else "FAILED")

    def _cmd_version(self):
        r = self._run_cmd(self._iface.console.get_version)
        if r is not None:
            self._lbl_version.setText(str(r))
            self._fw_label.setText(f"FW: {r}")

    def _cmd_echo(self):
        r = self._run_cmd(self._iface.console.echo, echo_data=b"Hello LIFU!")
        if r is not None:
            echo, length = r
            if length > 0:
                self._lbl_echo.setText(f"{echo.decode('utf-8')} ({length} bytes)")
            else:
                self._lbl_echo.setText("FAILED")

    def _cmd_hwid(self):
        r = self._run_cmd(self._iface.console.get_hardware_id)
        if r is not None:
            self._lbl_hwid.setText(str(r))

    def _cmd_toggle_led(self):
        self._run_cmd(self._iface.console.toggle_led)
        self._append_log("LED toggled")

    # Temperatures
    def _cmd_refresh_temps(self):
        t1 = self._run_cmd(self._iface.console.get_temperature1)
        if t1 is not None:
            self._lbl_temp1.setText(f"{t1:.2f} °C")
        t2 = self._run_cmd(self._iface.console.get_temperature2)
        if t2 is not None:
            self._lbl_temp2.setText(f"{t2:.2f} °C")

    # 12V
    def _cmd_12v_on(self):
        r = self._run_cmd(self._iface.console.turn_12v_on)
        if r:
            self._lbl_12v.setText("ON")
        self._append_log(f"12V ON: {'OK' if r else 'FAILED'}")

    def _cmd_12v_off(self):
        r = self._run_cmd(self._iface.console.turn_12v_off)
        if r:
            self._lbl_12v.setText("OFF")
        self._append_log(f"12V OFF: {'OK' if r else 'FAILED'}")

    def _cmd_12v_status(self):
        r = self._run_cmd(self._iface.console.get_12v_status)
        if r is not None:
            self._lbl_12v.setText("ON" if r else "OFF")

    # HV
    def _cmd_hv_set(self):
        v = self._spin_hv.value()
        r = self._run_cmd(self._iface.console.set_hv, v)
        self._append_log(f"Set HV {v:.1f}V: {'OK' if r else 'FAILED'}")

    def _cmd_hv_on(self):
        r = self._run_cmd(self._iface.console.turn_hv_on)
        if r:
            self._lbl_hv_status.setText("ON")
        self._append_log(f"HV ON: {'OK' if r else 'FAILED'}")

    def _cmd_hv_off(self):
        r = self._run_cmd(self._iface.console.turn_hv_off)
        if r:
            self._lbl_hv_status.setText("OFF")
        self._append_log(f"HV OFF: {'OK' if r else 'FAILED'}")

    def _cmd_hv_read(self):
        r = self._run_cmd(self._iface.console.get_hv)
        if r is not None:
            self._lbl_hv.setText(f"{r:.2f} V")

    def _cmd_hv_status(self):
        r = self._run_cmd(self._iface.console.get_hv_status)
        if r is not None:
            self._lbl_hv_status.setText("ON" if r else "OFF")

    # Fans
    def _cmd_fan_set(self, fan_id: int, speed: int):
        r = self._run_cmd(self._iface.console.set_fan, fan_id=fan_id, speed=speed)
        self._append_log(f"Fan {fan_id} set to {speed}%: {'OK' if r and r >= 0 else 'FAILED'}")

    def _cmd_fan_get(self, fan_id: int, label: QLabel):
        r = self._run_cmd(self._iface.console.get_fan, fan_id=fan_id)
        if r is not None and r >= 0:
            label.setText(f"{r} %")

    # RGB
    def _cmd_rgb_set(self, state: int):
        names = {0: "OFF", 1: "RED", 2: "BLUE", 3: "GREEN"}
        r = self._run_cmd(self._iface.console.set_rgb, state)
        if r:
            self._lbl_rgb.setText(f"Current: {names.get(state, '?')}")
        self._append_log(f"RGB {names.get(state, '?')}: {'OK' if r else 'FAILED'}")

    def _cmd_rgb_get(self):
        names = {0: "OFF", 1: "RED", 2: "BLUE", 3: "GREEN"}
        r = self._run_cmd(self._iface.console.get_rgb)
        if r is not None and r >= 0:
            self._lbl_rgb.setText(f"Current: {names.get(r, '?')} ({r})")

    # Voltage monitor
    def _cmd_vmon_read(self):
        r = self._run_cmd(self._iface.console.get_voltage_monitor)
        if r is not None:
            self._update_vmon_display(r)

    def _update_vmon_display(self, readings: list[dict]):
        for ch in readings:
            i = ch["channel"]
            lbl_raw, lbl_v, lbl_cv = self._vmon_labels[i]
            lbl_raw.setText(str(ch["raw_adc"]))
            lbl_v.setText(f"{ch['voltage']:.3f} V")
            lbl_cv.setText(f"{ch['converted_voltage']:.3f} V")

    def _toggle_vmon_polling(self, checked: bool):
        if checked:
            self._btn_vmon_poll.setText("Stop Polling")
            self._poll_timer.start(1000)
        else:
            self._btn_vmon_poll.setText("Start Polling")
            self._poll_timer.stop()

    def _poll_readings(self):
        self._cmd_vmon_read()
        self._cmd_refresh_temps()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _append_log(self, msg: str):
        self._log.append(msg)

    def closeEvent(self, event):
        self._poll_timer.stop()
        self._iface.stop()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = ConsoleGUI()
    win.show()
    sys.exit(app.exec())
