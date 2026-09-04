"""In-memory fake of :class:`openlifu_sdk.LIFUInterface` for ``--simulate`` modes.

Drives a :class:`~openlifu_sdk.ui.base_connector.BaseConnector`
(or any equivalent connector that talks to a ``LIFUInterface``)
end-to-end without any USB I/O. The seam used by app-side connectors
is typically ``_make_interface``, so the connector's state machine,
retry/poll/log code paths all run against the fake exactly as they do
against real hardware. Telemetry frames emitted during sonication
match the format consumed by
:func:`~openlifu_sdk.ui.status_frame.parse_status_string`.

Thermal model
-------------
Per-module TX temperature integrates ``dT/dt = k * V^2 * duty`` while
sonicating and decays toward 25 deg C otherwise (Newton's law,
``tau = 600 s``). ``k`` is calibrated so 45 V at 25 % duty rises
50 deg C over 10 minutes.
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
import time
from datetime import datetime, timezone
from typing import List, Optional

# Qt backend selection: prefer 3D Slicer's PythonQt-based ``qt`` module
# when we're running inside Slicer, since Slicer's GUI event loop is Qt5
# and Qt6 timers parented to that loop would never fire. Outside Slicer
# (e.g. the openlifu desktop apps), fall back to PyQt6.
import sys as _sys

if "slicer" in _sys.modules:
    try:
        import qt as _slicer_qt  # type: ignore
    except ImportError as _exc:  # pragma: no cover
        raise ImportError(
            "openlifu_sdk.ui.simulated_interface could not import 3D Slicer's "
            "PythonQt 'qt' module despite running inside Slicer."
        ) from _exc
    QObject = _slicer_qt.QObject
    QTimer = _slicer_qt.QTimer
    pyqtSignal = _slicer_qt.Signal
    _QT_BACKEND = "PythonQt"
else:
    try:
        from PyQt6.QtCore import QObject, QTimer, pyqtSignal  # type: ignore
        _QT_BACKEND = "PyQt6"
    except ImportError:  # pragma: no cover - exercised only in Slicer
        try:
            import qt as _slicer_qt  # type: ignore
        except ImportError as _exc:  # pragma: no cover - no Qt at all
            raise ImportError(
                "openlifu_sdk.ui.simulated_interface requires PyQt6 (install "
                "with the 'ui' extra) or 3D Slicer's PythonQt 'qt' module."
            ) from _exc
        QObject = _slicer_qt.QObject
        QTimer = _slicer_qt.QTimer
        pyqtSignal = _slicer_qt.Signal
        _QT_BACKEND = "PythonQt"

from openlifu_sdk.io import LIFUInterfaceStatus
from openlifu_sdk.io.signal import OWSignal
from openlifu_sdk.ui.status_frame import format_status_frame as _format_status_frame

logger = logging.getLogger(__name__)
logger.debug("openlifu_sdk.ui.simulated_interface using Qt backend: %s", _QT_BACKEND)

_FALLBACK_CONSOLE_FW_VERSION = "1.2.6"
_FALLBACK_TX_FW_VERSION = "2.0.5"

# k chosen so 45 V * 0.25 duty * 600 s -> 50 deg C
TX_HEATING_K = 50.0 / (45.0 * 45.0 * 0.25 * 600.0)
# Newton's-law cooling time constant (seconds). 600 s ~ 10 min half-life-ish.
TX_COOLING_TAU_S = 600.0
TX_AMBIENT_C = 25.0
TX_TEMP_NOISE_SIGMA = 0.2
HV_TEMP_NOISE_SIGMA = 0.1
HV_VMON_NOISE_SIGMA = 0.05

# Auto-connect delay after start_monitoring() (seconds). Set to 0 so
# the simulator reports HV + TX as already connected when the QML
# bindings first evaluate; this avoids transient "Cannot read property
# of null" warnings from QML expressions that touch device state during
# launch.
AUTO_CONNECT_DELAY_S = 0.0
# How often the run engine emits a temperature heartbeat STATUS frame.
HEARTBEAT_INTERVAL_MS = 1000


def _gauss(sigma: float) -> float:
    return random.gauss(0.0, sigma)


def _normalize_semver(version: Optional[str], fallback: str) -> str:
    text = str(version or "").strip()
    if not text:
        return fallback
    base = text.lstrip("v").split("-")[0].split("+")[0]
    if re.match(r"^\d+\.\d+\.\d+$", base):
        return base
    return text


def _version_for_component(version: Optional[str], fallback: str) -> str:
    return _normalize_semver(version, fallback)


def _latest_console_fw_version() -> str:
    try:
        from openlifu_sdk.util.firmware import get_console_firmware_version

        return _normalize_semver(get_console_firmware_version(), _FALLBACK_CONSOLE_FW_VERSION)
    except Exception:
        logger.debug("Falling back to default simulated console firmware version", exc_info=True)
        return _FALLBACK_CONSOLE_FW_VERSION


def _latest_tx_fw_version() -> str:
    try:
        from openlifu_sdk.util.firmware import get_transmitter_firmware_version

        return _normalize_semver(get_transmitter_firmware_version(), _FALLBACK_TX_FW_VERSION)
    except Exception:
        logger.debug("Falling back to default simulated transmitter firmware version", exc_info=True)
        return _FALLBACK_TX_FW_VERSION


def _sim_updated_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _read_version_from_firmware_image(package_file: str, fallback: str) -> str:
    try:
        from openlifu_sdk.util.firmware import _get_firmware_version

        return _normalize_semver(_get_firmware_version(package_file), fallback)
    except Exception:
        logger.debug("Could not parse firmware version from %s", package_file, exc_info=True)
        return fallback


# =============================================================================
# Per-module thermal model
# =============================================================================

class _ModuleThermal:
    """Tracks TX module temperature with simple heating / cooling."""

    def __init__(self, module_idx: int):
        self.module = module_idx
        self.temp_c = TX_AMBIENT_C
        # Per-module +/- 5 % variation in heating coefficient so modules
        # diverge during a long run.
        self.k_scale = 1.0 + (random.random() - 0.5) * 0.10
        self._last_update = time.monotonic()

    def heat_step(self, voltage: float, duty: float, dt_s: float):
        if dt_s <= 0:
            return
        rise = TX_HEATING_K * self.k_scale * voltage * voltage * duty * dt_s
        self.temp_c += rise
        self._last_update = time.monotonic()

    def cool_step(self):
        now = time.monotonic()
        dt = now - self._last_update
        self._last_update = now
        if dt <= 0:
            return
        # Exact Newton's-law step (more stable than Euler for big dt).
        self.temp_c = TX_AMBIENT_C + (self.temp_c - TX_AMBIENT_C) * math.exp(-dt / TX_COOLING_TAU_S)

    def read_temp(self) -> float:
        self.cool_step()
        return self.temp_c + _gauss(TX_TEMP_NOISE_SIGMA)

    def read_ambient(self) -> float:
        return TX_AMBIENT_C + _gauss(0.05)


# =============================================================================
# Simulated TX device
# =============================================================================

class SimulatedTxDevice:
    """Implements every attribute / method that a connector calls on
    ``interface.txdevice``. Owns per-module thermal state and serves as
    the emitter for unsolicited STATUS frames during sonication.
    """

    def __init__(self, num_modules: int = 1, firmware_version: Optional[str] = None):
        self.num_modules = max(1, int(num_modules))
        self._firmware_versions = [
            _version_for_component(firmware_version, _latest_tx_fw_version())
            for _ in range(self.num_modules)
        ]
        self.signal_connected = OWSignal()
        self.signal_disconnected = OWSignal()
        self.signal_data_received = OWSignal()
        self.signal_error = OWSignal()

        self._connected = False
        self._async_mode = False
        self._modules = [_ModuleThermal(i) for i in range(self.num_modules)]
        # Per-module user_config dicts (with module.sensitivity table).
        self._user_configs = [self._default_user_config(i) for i in range(self.num_modules)]
        # Last-applied sequence (kept so set_trigger / get_trigger_json round-trip).
        self._sequence = {
            "pulse_interval": 0.1,
            "pulse_count": 1,
            "pulse_train_interval": 0.0,
            "pulse_train_count": 1,
        }
        self._pulse = {"frequency": 400_000.0, "duration": 100e-6, "amplitude": 1.0}
        self._trigger_running = False

    # ---- helpers --------------------------------------------------------

    def _default_user_config(self, idx: int) -> dict:
        # Plausible 100 - 1000 kHz sensitivity table, V/MPa-ish.
        return {
            "sn": "SIMULATED",
            "hwid": "ABCDEFGH",
            "freq": 400,
            "hw_ver": "SIM",
            "fw_ver": self._firmware_versions[idx],
            "sdk_ver": "1.0.7",
            "updated": _sim_updated_timestamp(),
            "module": {
                "id": "txm_400_sim-400k-01",
                "name": "TXM 400kHz (S/N SIMULATED-400K-01)",
                "nx": 8,
                "ny": 8,
                "pitch": 5,
                "frequency": 400000.0,
                "kerf": 0.3,
                "crosstalk_frac": 0.12,
                "crosstalk_dist": 0.00505,
                "sensitivity": [
                    [375000, 3144],
                    [380000, 3110],
                    [385000, 2823],
                    [390000, 2796],
                    [395000, 2744],
                    [400000, 2720],
                    [405000, 2300],
                    [410000, 2267],
                ],
            },
            "device": {},
        }

    def is_connected(self) -> bool:
        return self._connected

    def emit_connected(self, port: str = "SIM:TX"):
        if self._connected:
            return
        self._connected = True
        self.signal_connected.emit("TX", port)

    def emit_disconnected(self, port: str = "SIM:TX"):
        if not self._connected:
            return
        self._connected = False
        self.signal_disconnected.emit("TX", port)

    def emit_status_frame(self, pt_curr: int, pt_total: int,
                          p_curr: int = 0, p_total: int = 0,
                          status: str = "RUNNING",
                          mode: str = "SEQUENCE") -> None:
        # Use module 0 temp as the representative one (matches firmware behavior).
        temp_tx = self._modules[0].read_temp()
        temp_amb = self._modules[0].read_ambient()
        frame = _format_status_frame(
            pt_curr, pt_total, p_curr, p_total, temp_tx, temp_amb,
            status=status, mode=mode,
        )
        self.signal_data_received.emit("TX", frame)

    # ---- methods called by the connector --------------------------------

    def get_tx_module_count(self) -> int:
        return self.num_modules

    def get_module_count(self) -> int:
        return self.num_modules

    def get_temperature(self, module: int = 0) -> float:
        return self._modules[module].read_temp()

    def get_ambient_temperature(self, module: int = 0) -> float:
        return self._modules[module].read_ambient()

    def get_version(self, module: int = 0) -> str:
        return f"v{self._firmware_versions[module]}"

    def get_hardware_id(self, module: int = 0, raw_hex: bool = False) -> str:
        return f"{0xA0A1A2A3A4A5A6A7B0B1B2B3B4B5B6B7 + module:032X}"

    def read_config(self, module: int = 0):
        from openlifu_sdk.io.LIFUUserConfig import LifuUserConfig
        return LifuUserConfig(json_data=dict(self._user_configs[module]))

    def write_config_json(self, json_str: str, module: int = 0):
        from openlifu_sdk.io.LIFUUserConfig import LifuUserConfig
        try:
            self._user_configs[module] = json.loads(json_str)
        except Exception:
            logger.warning("SimulatedTxDevice.write_config_json: invalid json; ignored")
        return LifuUserConfig(json_data=dict(self._user_configs[module]))

    def apply_simulated_transducer(self, arr) -> None:
        """Reconfigure the simulator to mimic a transducer (array).

        ``arr`` is duck-typed against :class:`openlifu.xdc.TransducerArray`:
        it must expose ``modules`` (a sequence) where each module exposes
        ``id``, ``name``, ``frequency`` (Hz), and (optionally) an ``attrs``
        mapping that may carry ``hwid``. The number of simulated TX modules
        is rebuilt to match ``len(arr.modules)``, and each per-module
        ``user_config`` is overwritten so that ``read_config(module=i)`` /
        ``get_version`` / etc. return values consistent with the picked
        transducer.
        """
        modules_list = list(getattr(arr, "modules", []) or [])
        n = max(1, len(modules_list))
        if n != self.num_modules:
            current = list(self._firmware_versions)
            self.num_modules = n
            self._modules = [_ModuleThermal(i) for i in range(n)]
            default_fw = current[0] if current else _latest_tx_fw_version()
            self._firmware_versions = [
                current[i] if i < len(current) else default_fw
                for i in range(n)
            ]
            self._user_configs = [self._default_user_config(i) for i in range(n)]
        for i, m in enumerate(modules_list):
            cfg = self._user_configs[i]
            freq_hz = float(getattr(m, "frequency", 400e3) or 400e3)
            cfg["freq"] = int(round(freq_hz / 1000.0))
            attrs = getattr(m, "attrs", None) or {}
            hwid_str = attrs.get("hwid") if isinstance(attrs, dict) else None
            if isinstance(hwid_str, str) and hwid_str:
                cfg["hwid"] = hwid_str
            mod_block = cfg.setdefault("module", {})
            mod_id = getattr(m, "id", None)
            mod_name = getattr(m, "name", None)
            if mod_id:
                mod_block["id"] = mod_id
            if mod_name:
                mod_block["name"] = mod_name
            mod_block["frequency"] = freq_hz
        # Stash array-level identity on module 0's ``device`` block so callers
        # that read it back via ``read_config(0)`` (e.g. SlicerOpenLIFU's
        # device-vs-session compatibility check) can recover the simulated
        # transducer's id/name. ``to_device_config`` is the canonical
        # serializer used by real hardware too.
        to_device_config = getattr(arr, "to_device_config", None)
        if callable(to_device_config):
            try:
                self._user_configs[0]["device"] = to_device_config()
            except Exception:  # noqa: BLE001
                logger.debug("apply_simulated_transducer: to_device_config() failed", exc_info=True)
        else:
            self._user_configs[0]["device"] = {
                "id": getattr(arr, "id", None),
                "name": getattr(arr, "name", None),
            }

    def _normalize_train_interval(self):
        """Substitute pulse_train_interval=0 with pulse_count*pulse_interval."""
        try:
            ti = float(self._sequence.get("pulse_train_interval", 0.0))
        except (TypeError, ValueError):
            ti = 0.0
        if ti > 0:
            return
        try:
            pi = float(self._sequence.get("pulse_interval", 0.0))
            pc = int(self._sequence.get("pulse_count", 1))
        except (TypeError, ValueError):
            pi, pc = 0.0, 1
        self._sequence["pulse_train_interval"] = max(1e-3, pc * pi)

    def set_solution(self, pulse=None, delays=None, apodizations=None,
                     sequence=None, profile_index=1, profile_increment=True,
                     trigger_mode="sequence"):
        if pulse:
            self._pulse = dict(pulse)
        if sequence:
            self._sequence = dict(sequence)
            self._normalize_train_interval()
        return True

    def set_trigger(self, pulse_interval=None, pulse_count=None,
                    pulse_train_interval=None, pulse_train_count=None,
                    trigger_mode="sequence"):
        if pulse_interval is not None:
            self._sequence["pulse_interval"] = float(pulse_interval)
        if pulse_count is not None:
            self._sequence["pulse_count"] = int(pulse_count)
        if pulse_train_interval is not None:
            self._sequence["pulse_train_interval"] = float(pulse_train_interval)
        if pulse_train_count is not None:
            self._sequence["pulse_train_count"] = int(pulse_train_count)
        self._normalize_train_interval()
        return self.get_trigger_json()

    def get_trigger_json(self) -> dict:
        return {
            "TriggerStatus": "RUNNING" if self._trigger_running else "STOPPED",
            "TriggerMode": "SEQUENCE",
            **self._sequence,
        }

    def set_trigger_json(self, data) -> dict:
        if isinstance(data, dict):
            for k in ("pulse_interval", "pulse_count",
                      "pulse_train_interval", "pulse_train_count"):
                if k in data:
                    self._sequence[k] = data[k]
            self._normalize_train_interval()
        return self.get_trigger_json()

    def async_mode(self, enable: Optional[bool] = None) -> bool:
        if enable is not None:
            self._async_mode = bool(enable)
        return self._async_mode

    def start_trigger(self):
        self._trigger_running = True

    def stop_trigger(self):
        self._trigger_running = False

    def set_module_invert(self, invert):
        return None

    def ping(self, module: int = 0):
        return True

    def toggle_led(self, module: int = 0):
        return True

    def echo(self, echo_data: bytes, module: int = 0):
        return (echo_data, len(echo_data))

    def soft_reset(self, module: Optional[int] = None):
        return True

    def update_firmware(self, module: int = 0, package_file: Optional[str] = None,
                        progress_callback=None, firmware_version: Optional[str] = None,
                        **_kwargs):
        if module < 0 or module >= self.num_modules:
            raise ValueError(f"Module index out of range: {module}")
        current = self._firmware_versions[module]
        target = _version_for_component(firmware_version, current)
        if package_file and firmware_version is None:
            target = _read_version_from_firmware_image(package_file, current)
        if progress_callback is not None:
            try:
                progress_callback(0, 1, "simulated-update")
            except Exception:
                logger.debug("Simulated update progress callback failed", exc_info=True)
        self._firmware_versions[module] = target
        self._user_configs[module]["fw_ver"] = target
        self._user_configs[module]["updated"] = _sim_updated_timestamp()
        if progress_callback is not None:
            try:
                progress_callback(1, 1, "simulated-update")
            except Exception:
                logger.debug("Simulated update progress callback failed", exc_info=True)
        return True

    def close(self):
        self._connected = False

    async def start_monitoring(self, interval: int = 1):
        return None

    def stop_monitoring(self):
        return None


# =============================================================================
# Simulated HV controller
# =============================================================================

class SimulatedHVController:
    """Implements every attribute / method that a connector calls on
    ``interface.hvcontroller``."""

    def __init__(self, firmware_version: Optional[str] = None):
        self.signal_connected = OWSignal()
        self.signal_disconnected = OWSignal()
        self.signal_data_received = OWSignal()
        self.signal_error = OWSignal()

        self._connected = False
        self._firmware_version = _version_for_component(firmware_version, _latest_console_fw_version())
        self._hv_on = False
        self._v12_on = True
        self._voltage_setpoint = 0.0
        self._rgb_state = 0
        self.uart = None  # connector reads this for FW DFU; not used here

    def is_connected(self) -> bool:
        return self._connected

    def emit_connected(self, port: str = "SIM:CON"):
        if self._connected:
            return
        self._connected = True
        self.signal_connected.emit("HV", port)

    def emit_disconnected(self, port: str = "SIM:CON"):
        if not self._connected:
            return
        self._connected = False
        self.signal_disconnected.emit("HV", port)

    # ---- methods --------------------------------------------------------

    def turn_hv_on(self):
        self._hv_on = True
        return True

    def turn_hv_off(self):
        self._hv_on = False
        return True

    def get_hv_status(self) -> bool:
        return self._hv_on

    def turn_12v_on(self):
        self._v12_on = True
        return True

    def turn_12v_off(self):
        self._v12_on = False
        return True

    def get_12v_status(self) -> bool:
        return self._v12_on

    def get_version(self) -> str:
        return f"v{self._firmware_version}"

    def update_firmware(self, package_file: Optional[str] = None,
                        progress_callback=None,
                        firmware_version: Optional[str] = None,
                        **_kwargs):
        target = _version_for_component(firmware_version, self._firmware_version)
        if package_file and firmware_version is None:
            target = _read_version_from_firmware_image(package_file, self._firmware_version)
        if progress_callback is not None:
            try:
                progress_callback(0, 1, "simulated-update")
            except Exception:
                logger.debug("Simulated update progress callback failed", exc_info=True)
        self._firmware_version = target
        if progress_callback is not None:
            try:
                progress_callback(1, 1, "simulated-update")
            except Exception:
                logger.debug("Simulated update progress callback failed", exc_info=True)
        return True

    def get_hardware_id(self, raw_hex: bool = False) -> str:
        return "C0C1C2C3C4C5C6C7D0D1D2D3D4D5D6D7"

    def get_temperature1(self) -> float:
        return 30.0 + 0.05 * self._voltage_setpoint + _gauss(HV_TEMP_NOISE_SIGMA)

    def get_temperature2(self) -> float:
        return 31.0 + 0.05 * self._voltage_setpoint + _gauss(HV_TEMP_NOISE_SIGMA)

    def set_voltage(self, voltage: float) -> bool:
        self._voltage_setpoint = float(voltage)
        return True

    def get_voltage(self) -> float:
        return self._voltage_setpoint if self._hv_on else 0.0

    def get_vmon_values(self) -> List[dict]:
        """Match the real SDK shape: list of 8 dicts with channel, raw_adc,
        voltage, and converted_voltage fields. QML reads ``converted_voltage``.
        """
        v = self._voltage_setpoint if self._hv_on else 0.0
        v12 = 12.0 + _gauss(HV_VMON_NOISE_SIGMA) if self._v12_on else 0.0
        converted = [
            +v + _gauss(HV_VMON_NOISE_SIGMA),       # HVP1
            +v + _gauss(HV_VMON_NOISE_SIGMA),       # HVP2
            -v + _gauss(HV_VMON_NOISE_SIGMA),       # HVM2
            -v + _gauss(HV_VMON_NOISE_SIGMA),       # HVM1
            v12,                                     # 12V
            3.3 + _gauss(0.01),                     # VCA1
            3.3 + _gauss(0.01),                     # VCB1
            1.8 + _gauss(0.01),                     # VCC1
        ]
        return [
            {
                "channel": i,
                "raw_adc": int(max(0, min(65535, abs(cv) * 1000))),
                "voltage": round(cv, 3),
                "converted_voltage": round(cv, 3),
            }
            for i, cv in enumerate(converted)
        ]

    def set_rgb_led(self, state: int):
        self._rgb_state = int(state)
        return True

    def get_rgb_led(self) -> int:
        return self._rgb_state

    def ping(self):
        return True

    def toggle_led(self):
        return True

    def echo(self, echo_data: bytes):
        return (echo_data, len(echo_data))

    def soft_reset(self):
        return True

    def enter_dfu(self):
        return True

    def close(self):
        self._connected = False

    async def start_monitoring(self, interval: int = 1):
        return None

    def stop_monitoring(self):
        return None


# =============================================================================
# Run engine - emits STATUS frames during sonication
# =============================================================================

class _SimulatedRunEngine(QObject):
    """Drives one sonication run: emits STATUS frames and applies thermal
    heating to the TX modules at the configured pulse-train cadence.

    The engine lives on the main thread; both timers are QTimers parented
    to it. ``alive`` flips False when the run finishes, which the
    connector's polling sees via :meth:`SimulatedLIFUInterface.is_running`
    and uses to drive its own RUNNING -> READY transition.

    Use :meth:`set_finished_callback` to be notified when the run
    completes (we use a plain Python callable rather than a Qt signal
    because this class is instantiated under either PyQt6 or Slicer's
    PythonQt-based ``qt`` module, and class-level signal declarations
    are not portable between the two backends).
    """

    def __init__(self, txdevice: SimulatedTxDevice, hvcontroller: SimulatedHVController,
                 sequence: dict, pulse: dict, voltage: float,
                 trigger_mode: str = "sequence", parent=None):
        super().__init__(parent)
        self._finished_cb: Optional[callable] = None
        self._tx = txdevice
        self._hv = hvcontroller
        self._voltage = float(voltage)
        self._trigger_mode = str(trigger_mode).lower()
        self._mode_label = {
            "sequence": "SEQUENCE",
            "continuous": "CONTINUOUS",
            "single": "SINGLE",
        }.get(self._trigger_mode, "SEQUENCE")

        # Effective pulse-train period: when pulse_train_interval is 0
        # the SDK uses pulse_count * pulse_interval.
        pulse_interval = float(sequence.get("pulse_interval", 0.1))
        pulse_count = int(sequence.get("pulse_count", 1))
        train_interval = float(sequence.get("pulse_train_interval", 0.0))
        self._pulse_count = pulse_count
        self._pulse_interval_s = pulse_interval
        self._train_period_s = train_interval if train_interval > 0 else max(
            1e-3, pulse_count * pulse_interval
        )
        # Trigger-mode shapes the train-count semantics:
        #   sequence   - run pulse_train_count trains, then STOPPED
        #   single     - run exactly one train, then STOPPED
        #   continuous - run forever (PT[1/1] held), only stops on
        #                explicit stop_sonication() from the host
        seq_total = max(1, int(sequence.get("pulse_train_count", 1)))
        if self._trigger_mode == "single":
            self._train_total = 1
            self._infinite = False
        elif self._trigger_mode == "continuous":
            self._train_total = 1
            self._infinite = True
        else:
            self._train_total = seq_total
            self._infinite = False

        # Duty for thermal model.
        pulse_duration_s = float(pulse.get("duration", 0.0))
        self._duty = (pulse_count * pulse_duration_s) / self._train_period_s if self._train_period_s > 0 else 0.0

        self._train_curr = 0
        self.alive = True

        self._train_timer = QTimer(self)
        self._train_timer.setSingleShot(False)
        self._train_timer.timeout.connect(self._on_train_tick)

        self._heartbeat = QTimer(self)
        self._heartbeat.setSingleShot(False)
        self._heartbeat.timeout.connect(self._on_heartbeat)

    def start(self):
        self._tx.start_trigger()
        # Apply heating for the very first train period as it elapses;
        # speed-clamp to avoid pegging the GUI on tiny periods.
        period_ms = max(20, int(round(self._train_period_s * 1000)))
        if self._infinite:
            est_duration = "infinite"
        else:
            est_duration = f"{self._train_total * self._train_period_s:.3f}s"
        logger.info(
            "[SIMRUN] start mode=%s pulse_count=%d pulse_interval=%.4fs "
            "train_period=%.4fs (timer=%dms) train_total=%s "
            "expected_duration=%s",
            self._mode_label, self._pulse_count, self._pulse_interval_s,
            self._train_period_s, period_ms,
            "inf" if self._infinite else str(self._train_total),
            est_duration,
        )
        # Emit an initial RUNNING frame at PT[0/N] so the UI flips into
        # the running state immediately rather than waiting one full
        # train period for the first tick.
        initial_total = self._train_curr if self._infinite else self._train_total
        self._tx.emit_status_frame(
            self._train_curr, max(1, initial_total),
            status="RUNNING", mode=self._mode_label,
        )
        self._train_timer.start(period_ms)
        # The heartbeat exists to carry temperature updates between
        # long train ticks. When the train period is already <= the
        # heartbeat interval, running both produces interleaved
        # duplicate frames (the heartbeat re-emits the previous count
        # right after a train tick has advanced it), so skip it.
        if period_ms > HEARTBEAT_INTERVAL_MS:
            self._heartbeat.start(HEARTBEAT_INTERVAL_MS)

    def stop(self):
        was_alive = self.alive
        # Mark inactive first so any queued timer ticks become no-ops.
        self.alive = False
        self._train_timer.stop()
        self._heartbeat.stop()
        self._tx.stop_trigger()
        # Emit a final STOPPED frame so the connector's STATUS-based
        # trigger-state machine flips cleanly (especially in continuous
        # mode where there's no natural completion).
        if was_alive:
            if self._infinite:
                total = self._train_curr if self._train_curr > 0 else 1
            else:
                total = self._train_total
            self._tx.emit_status_frame(
                self._train_curr, total,
                status="STOPPED", mode=self._mode_label,
            )

    def _on_train_tick(self):
        if not self.alive:
            return
        self._train_curr += 1
        # Apply heating for this train period.
        for m in self._tx._modules:
            m.heat_step(self._voltage, self._duty, self._train_period_s)
        if self._infinite:
            # Continuous mode: emit PT[curr/curr] so the counter keeps
            # ticking up forever; only stops on explicit stop_sonication.
            self._tx.emit_status_frame(
                self._train_curr, self._train_curr,
                status="RUNNING", mode=self._mode_label,
            )
            return
        self._tx.emit_status_frame(
            self._train_curr, self._train_total,
            status="RUNNING", mode=self._mode_label,
        )
        if self._train_curr >= self._train_total:
            # Mark inactive BEFORE emitting so a queued heartbeat tick
            # cannot race past us and re-emit a RUNNING frame that
            # would clobber the state reset on the connector side.
            self.alive = False
            self._train_timer.stop()
            self._heartbeat.stop()
            # Final STOPPED frame so the connector flips trigger state /
            # transitions back to READY.
            self._tx.emit_status_frame(
                self._train_curr, self._train_total,
                status="STOPPED", mode=self._mode_label,
            )
            if self._finished_cb is not None:
                try:
                    self._finished_cb()
                except Exception:  # noqa: BLE001
                    logger.exception("_SimulatedRunEngine finished callback raised")

    def set_finished_callback(self, cb):
        """Register a zero-arg callable to be invoked when the run completes."""
        self._finished_cb = cb

    def _on_heartbeat(self):
        if not self.alive:
            return
        # Carry latest progress + temperature between train ticks.
        if self._infinite:
            total = self._train_curr if self._train_curr > 0 else 1
        else:
            total = self._train_total
        self._tx.emit_status_frame(
            self._train_curr, total,
            status="RUNNING", mode=self._mode_label,
        )


# =============================================================================
# Simulated LIFUInterface (top-level fake)
# =============================================================================

class SimulatedLIFUInterface(QObject):
    """Drop-in fake for :class:`openlifu_sdk.LIFUInterface`."""

    #: Class-level marker so callers can cheaply distinguish a simulated
    #: interface from a real :class:`~openlifu_sdk.io.LIFUInterface`
    #: without importing the simulated class itself.
    is_simulated: bool = True

    def __init__(self, num_modules: int = 1,
                 transducer=None,
                 tx_firmware_version: Optional[str] = None,
                 hv_firmware_version: Optional[str] = None,
                 firmware_version: Optional[str] = None,
                 voltage_table_selection: Optional[str] = None,
                 **_unused):
        # When a transducer (array) is supplied, derive num_modules from it
        # so the TX device is built with the right module count up front.
        if transducer is not None:
            modules_attr = getattr(transducer, "modules", None)
            if modules_attr is not None:
                num_modules = max(1, len(list(modules_attr)))
        super().__init__()
        tx_version = tx_firmware_version if tx_firmware_version is not None else firmware_version
        hv_version = hv_firmware_version if hv_firmware_version is not None else firmware_version
        self.txdevice = SimulatedTxDevice(num_modules=num_modules, firmware_version=tx_version)
        self.hvcontroller = SimulatedHVController(firmware_version=hv_version)
        self.status = LIFUInterfaceStatus.STATUS_SYS_OFF
        self._engine: Optional[_SimulatedRunEngine] = None
        self.voltage_table_selection = voltage_table_selection
        self._last_solution_voltage = 0.0
        self._last_trigger_mode = "sequence"
        if transducer is not None and getattr(transducer, "modules", None) is not None:
            self.txdevice.apply_simulated_transducer(transducer)

    # ---- monitoring lifecycle -------------------------------------------

    async def start_monitoring(self, interval: int = 1):
        # Auto-connect both devices ~AUTO_CONNECT_DELAY_S after launch
        # via QTimer so the connect signals are delivered on the GUI
        # thread (mirroring the real OWSignal -> Bridge path).
        delay_ms = int(AUTO_CONNECT_DELAY_S * 1000)

        def _connect():
            logger.info("SimulatedLIFUInterface: emitting auto-connect for HV + TX")
            self.hvcontroller.emit_connected()
            self.txdevice.emit_connected()

        QTimer.singleShot(delay_ms, _connect)
        return None

    def stop_monitoring(self):
        return None

    def is_device_connected(self):
        return (self.txdevice.is_connected(), self.hvcontroller.is_connected())

    # ---- solution / sonication ------------------------------------------

    def set_solution(self, solution, profile_index=1, profile_increment=True,
                     trigger_mode="sequence", turn_hv_on: bool = False,
                     wait_for_settle: bool = False,
                     _allow_unsafe_solution: bool = False):
        """Skip safety checks; just store the bits the run engine needs."""
        voltage = float(solution.get("voltage", 0.0))
        self._last_solution_voltage = voltage
        self._last_trigger_mode = str(trigger_mode).lower()
        self.txdevice.set_solution(
            pulse=solution.get("pulse"),
            sequence=solution.get("sequence"),
            trigger_mode=trigger_mode,
        )
        # Real LIFUInterface.set_solution pushes the voltage setpoint
        # down to the HV controller as part of loading the solution.
        # Mirror that so QML's vmon plots / rail readouts track the
        # configured value.
        self.hvcontroller.set_voltage(voltage)
        self.set_status(LIFUInterfaceStatus.STATUS_READY)
        if turn_hv_on:
            self.hvcontroller.turn_hv_on()
        return True

    def start_sonication(self, async_mode: Optional[bool] = None,
                         turn_hv_on: bool = True,
                         wait_for_settle: bool = True) -> bool:
        if turn_hv_on:
            self.hvcontroller.turn_hv_on()
        if wait_for_settle:
            # Brief settle delay (real device is ~200 ms); not perceptible
            # but matches the real code path's blocking nature.
            time.sleep(0.2)
        # Stop any previous engine before starting a new one (pause/resume
        # rebuilds the trigger then re-calls start_sonication).
        if self._engine is not None and self._engine.alive:
            self._engine.stop()
        self.txdevice.async_mode(True)
        self._engine = _SimulatedRunEngine(
            txdevice=self.txdevice,
            hvcontroller=self.hvcontroller,
            sequence=self.txdevice._sequence,
            pulse=self.txdevice._pulse,
            voltage=self._last_solution_voltage,
            trigger_mode=self._last_trigger_mode,
            parent=self,
        )
        self._engine.start()
        self.set_status(LIFUInterfaceStatus.STATUS_RUNNING)
        return True

    def stop_sonication(self, turn_hv_off: bool = True,
                        wait_for_settle: bool = False) -> bool:
        if self._engine is not None:
            self._engine.stop()
            self._engine = None
        self.txdevice.async_mode(False)
        if turn_hv_off:
            self.hvcontroller.turn_hv_off()
        self.set_status(LIFUInterfaceStatus.STATUS_READY)
        return True

    def is_running(self) -> bool:
        return self._engine is not None and self._engine.alive

    # ---- misc -----------------------------------------------------------

    def set_status(self, status: LIFUInterfaceStatus):
        self.status = status

    def get_status(self) -> LIFUInterfaceStatus:
        return self.status

    def check_solution(self, solution):  # always passes
        return None

    def set_module_invert(self, module_invert):
        self.txdevice.set_module_invert(module_invert)

    def close(self):
        if self._engine is not None:
            self._engine.stop()
            self._engine = None
        try:
            self.hvcontroller.close()
        except Exception:
            pass
        try:
            self.txdevice.close()
        except Exception:
            pass


__all__ = [
    "SimulatedHVController",
    "SimulatedLIFUInterface",
    "SimulatedTxDevice",
]
