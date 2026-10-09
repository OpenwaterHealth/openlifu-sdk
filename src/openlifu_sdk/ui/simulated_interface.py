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
import time
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
from openlifu_sdk.io.exceptions import LIFUDeviceError, LIFUSolutionError
from openlifu_sdk.io.LIFUConfig import OW_BAD_CRC, OW_HV_PRESET_CRC
from openlifu_sdk.io.LIFUUserConfig import sensitivity_at_frequency
from openlifu_sdk.io.signal import OWSignal
from openlifu_sdk.ui.status_frame import format_status_frame as _format_status_frame

logger = logging.getLogger(__name__)
logger.debug("openlifu_sdk.ui.simulated_interface using Qt backend: %s", _QT_BACKEND)

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

    def __init__(self, num_modules: int = 1,
                 preset_flash: Optional[List[tuple[dict, int]]] = None):
        self.num_modules = max(1, int(num_modules))
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
        self.serial_number: Optional[str] = None
        self.hwid: Optional[str] = None
        self.hardware_id: Optional[str] = None
        self.module_user_configs: list[dict] = []

        # Current pulse-train counter during an active sonication.
        # Written by the engine on each tick so a polled host (via
        # :meth:`get_trigger` / :meth:`get_trigger_json`) can read
        # progress without subscribing to the status-frame
        # OWSignal. Reset to 0 at engine start; left at its final
        # value after natural completion (so a post-run poll shows
        # train_count == pulse_train_count); unchanged by an
        # operator-initiated stop (so the host sees how far the
        # sonication got).
        self._train_curr = 0

        # Simulated flash-baked preset table (SR-002 / SR-003). Each
        # entry is ``(machine_config_dict, regs_crc)`` -- what the
        # FDA firmware image carries for that preset index. The
        # machine_config must include ``id``, ``settings_crc`` and
        # ``pulse_train_count_selections``. ``None`` seeds a single
        # default preset; ``[]`` simulates an image with no presets.
        self._preset_flash: List[tuple[dict, int]] = (
            list(preset_flash) if preset_flash is not None
            else [self._default_flash_entry()]
        )
        self._loaded_preset_index: Optional[int] = None
        self._loaded_duration_index: Optional[int] = None

        # Reported firmware version. Mutable so an app-side debug UI
        # (or a test) can flip it to simulate an incompatible firmware
        # and exercise the operator connector's compat check. Single
        # string for all modules; if per-module version drift ever
        # becomes worth simulating, extend this to a list.
        self._fw_version: str = "sim-1.0.7"
        self.refresh_metadata()

    # ---- helpers --------------------------------------------------------

    def _default_user_config(self, idx: int) -> dict:
        # Plausible 100 - 1000 kHz sensitivity table, V/MPa-ish.
        return {
            "sn": "SIMULATED",
            "hwid": "ABCDEFGH",
            "freq": 400,
            "hw_ver": "SIM",
            "fw_ver": "2.0.5",
            "sdk_ver": "1.0.7",
            "updated": "2026-05-12 08:00:41",
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

    @staticmethod
    def _default_flash_entry() -> tuple[dict, int]:
        machine_config = {
            "id": "sim-default",
            "settings_crc": 0x12345678,
            "pulse_interval_ms": 100.0,
            "pulse_count": 1,
            "pulse_train_interval_s": 0.0,
            "pulse_length_us": 100.0,
            "pulse_train_count_selections": [1],
            "start_C": 35.0,
            "shutoff_C": 75.0,
        }
        return machine_config, 0x87654321

    def refresh_metadata(self) -> None:
        self.module_user_configs = [dict(cfg) for cfg in self._user_configs]
        primary = self.module_user_configs[0] if self.module_user_configs else {}
        self.serial_number = primary.get("sn") if isinstance(primary.get("sn"), str) else None
        hwid = primary.get("hwid") if isinstance(primary.get("hwid"), str) else None
        self.hwid = hwid
        self.hardware_id = hwid

    def get_sensitivity_for_frequency(self, freq_hz: float) -> Optional[float]:
        return sensitivity_at_frequency(self.module_user_configs, freq_hz)

    def _flash_entry(self, index: int) -> tuple[dict, int]:
        if not 0 <= index < len(self._preset_flash):
            raise LIFUDeviceError(f"TX: no preset at index {index}")
        return self._preset_flash[index]

    def get_preset(self, index: int) -> dict:
        """Same shape as :meth:`TxDevice.get_preset` (OW_PRESET_GET)."""
        machine_config, regs_crc = self._flash_entry(index)
        return {
            "count": len(self._preset_flash),
            "index": index,
            "chip_count": self.num_modules * 2,
            "profile_count": 1,
            "settings_crc": int(machine_config["settings_crc"]),
            "regs_crc": int(regs_crc),
            "baked_regs_crc": int(regs_crc),
            "id": machine_config["id"],
            "train_counts": list(machine_config.get("pulse_train_count_selections") or []),
            "start_c": machine_config.get("start_C"),
            "shutoff_c": machine_config.get("shutoff_C"),
        }

    def load_preset(self, index: int, regs_crc: int, duration_index: int = 0) -> bool:
        """Same contract as :meth:`TxDevice.load_preset` (OW_PRESET_LOAD).

        Like the firmware, loading applies the preset's trigger and pulse
        configuration, so no separate ``set_trigger`` is needed before
        ``start_sonication``.
        """
        machine_config, flash_regs_crc = self._flash_entry(index)
        if int(regs_crc) != int(flash_regs_crc):
            raise LIFUDeviceError(
                f"TX: regs_crc 0x{int(regs_crc):08X} does not match preset {index} "
                f"(0x{int(flash_regs_crc):08X})",
                device_error_code=OW_BAD_CRC,
            )
        selections = list(machine_config.get("pulse_train_count_selections") or [])
        if not 0 <= duration_index < len(selections):
            raise LIFUDeviceError(f"TX: preset {index} has no duration index {duration_index}")
        self._sequence = {
            "pulse_interval": float(machine_config.get("pulse_interval_ms", 0.0)) / 1000.0,
            "pulse_count": int(machine_config.get("pulse_count", 1)),
            "pulse_train_interval": float(machine_config.get("pulse_train_interval_s", 0.0)),
            "pulse_train_count": int(selections[duration_index]),
        }
        self._pulse["duration"] = float(machine_config.get("pulse_length_us", 100.0)) / 1_000_000.0
        self._normalize_train_interval()
        self._loaded_preset_index = index
        self._loaded_duration_index = duration_index
        return True

    def get_loaded_preset(self) -> tuple[Optional[int], Optional[int]]:
        """``(preset_index, duration_index)`` of the last successful
        :meth:`load_preset`, or ``(None, None)``. Simulator inspection only."""
        return self._loaded_preset_index, self._loaded_duration_index

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
        return self._fw_version

    def set_version(self, version: str, module: int = 0) -> None:
        """Override the reported firmware version.

        Intended for simulator debug UIs / tests that need to force
        the operator connector's ``check_firmware_compat`` down the
        incompatible-firmware path. Applies to all modules regardless
        of the ``module`` arg; the arg is present only to mirror
        :meth:`get_version`'s signature.
        """
        self._fw_version = str(version)

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
        self.refresh_metadata()
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
            self.num_modules = n
            self._modules = [_ModuleThermal(i) for i in range(n)]
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
        self.refresh_metadata()

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
            "TrainCount": self._train_curr,
            **self._sequence,
        }

    def get_trigger(self) -> dict:
        """Return the current trigger state as a snake_case dict.

        Mirrors the shape of
        :meth:`openlifu_sdk.io.LIFUTXDevice.TxDevice.get_trigger`
        so operator-interface code calling ``interface.txdevice.get_trigger()``
        gets the same keys against the sim and real hardware.

        Includes two fields the walking-skeleton polling loop needs:

        * ``train_count`` -- current pulse-train counter, 0 before
          an engine starts, incremented each train tick, equal to
          ``pulse_train_count`` on natural completion.
        * ``trigger_status`` -- ``"RUNNING"`` or ``"STOPPED"``;
          flips to ``"STOPPED"`` on both operator stop and natural
          completion so a polled host sees a single stop edge
          either way.
        """
        seq = self._sequence
        pulse_interval = seq.get("pulse_interval", 0.1)
        return {
            "pulse_interval": pulse_interval,
            "pulse_count": seq.get("pulse_count", 1),
            "pulse_width": self._pulse.get("duration", 100e-6) * 1e6,
            "pulse_train_interval": seq.get("pulse_train_interval", 0.0),
            "pulse_train_count": seq.get("pulse_train_count", 1),
            "mode": "sequence",
            "profile_index": 1,
            "profile_increment": True,
            "train_count": self._train_curr,
            "trigger_status": "RUNNING" if self._trigger_running else "STOPPED",
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
                        target_version: Optional[str] = None, **_kwargs) -> str:
        """Simulate a firmware update on the TX board.

        Mirrors the shape of the real
        :meth:`~openlifu_sdk.io.LIFUTXDevice.TxDevice.update_firmware`
        (accepting a ``module`` and a ``package_file`` path) but
        performs no real DFU. Instead, sets the reported firmware
        version to ``target_version`` (default: the string
        ``"sim-1.0.7"`` -- the simulator's original default) so a
        subsequent ``get_version`` returns the post-update value.

        Callers that want a specific post-update version (e.g. the
        operator connector's ``MIN_TX_FIRMWARE_VERSION``) pass
        ``target_version`` explicitly.

        Returns the new version string so tests / debug UI can
        confirm the update landed.
        """
        new_version = target_version or "sim-1.0.7"
        self._fw_version = str(new_version)
        return self._fw_version

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

    def __init__(self):
        self.signal_connected = OWSignal()
        self.signal_disconnected = OWSignal()
        self.signal_data_received = OWSignal()
        self.signal_error = OWSignal()

        self._connected = False
        self._hv_on = False
        self._v12_on = True
        self._voltage_setpoint = 0.0
        self._rgb_state = 0
        self.uart = None  # connector reads this for FW DFU; not used here
        self.supply_voltage = 0.0
        self.last_device_sensitivity: Optional[float] = None
        self._selected_preset_index: Optional[int] = None
        self._presets = [{
            "count": 1,
            "index": 0,
            "selected": None,
            "settings_crc": 0x12345678,
            "voltage": 20.0,
            "id": "sim-default",
            "sensitivity_ref": None,
            "min_voltage": 5.0,
            "max_voltage": 100.0,
        }]

        # Reported firmware version. See
        # :attr:`SimulatedTxDevice._fw_version` for rationale.
        self._fw_version: str = "sim-1.0.7"

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
        return self._fw_version

    def set_version(self, version: str) -> None:
        """Override the reported firmware version. See
        :meth:`SimulatedTxDevice.set_version` for rationale."""
        self._fw_version = str(version)

    def get_hardware_id(self, raw_hex: bool = False) -> str:
        return "C0C1C2C3C4C5C6C7D0D1D2D3D4D5D6D7"

    def get_temperature1(self) -> float:
        return 30.0 + 0.05 * self._voltage_setpoint + _gauss(HV_TEMP_NOISE_SIGMA)

    def get_temperature2(self) -> float:
        return 31.0 + 0.05 * self._voltage_setpoint + _gauss(HV_TEMP_NOISE_SIGMA)

    def set_voltage(self, voltage: float) -> bool:
        self._voltage_setpoint = float(voltage)
        self.supply_voltage = self._voltage_setpoint
        return True

    def configure_presets(self, presets: List[dict]) -> None:
        self._presets = [dict(preset) for preset in presets]

    def get_preset(self, index: int) -> dict | None:
        if not 0 <= index < len(self._presets):
            raise LIFUDeviceError(f"HV: no preset at index {index}")
        preset = dict(self._presets[index])
        preset.setdefault("count", len(self._presets))
        preset.setdefault("index", index)
        preset["selected"] = self._selected_preset_index
        return preset

    def select_preset(self,
                      index: int,
                      settings_crc: int,
                      device_sensitivity: float | None = None,
                      frequency_hz: float | None = None) -> bool:
        if (device_sensitivity is None) != (frequency_hz is None):
            raise ValueError("device_sensitivity and frequency_hz must be given together")
        preset = self.get_preset(index)
        if int(preset.get("settings_crc", settings_crc)) != int(settings_crc):
            raise LIFUDeviceError("HV: preset settings CRC mismatch",
                                  device_error_code=OW_HV_PRESET_CRC)
        voltage = float(preset.get("voltage", 0.0))
        ref_sensitivity = preset.get("sensitivity_ref")
        if (
            ref_sensitivity is not None
            and isinstance(device_sensitivity, (int, float))
            and float(device_sensitivity) > 0.0
        ):
            voltage *= float(ref_sensitivity) / float(device_sensitivity)
        min_voltage = float(preset.get("min_voltage", voltage))
        max_voltage = float(preset.get("max_voltage", voltage))
        voltage = max(min_voltage, min(max_voltage, voltage))
        self._selected_preset_index = index
        self.last_device_sensitivity = float(device_sensitivity) if isinstance(device_sensitivity, (int, float)) else None
        self._voltage_setpoint = voltage
        self.supply_voltage = voltage
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
        raise NotImplementedError("DFU not supported in simulation mode")

    def update_firmware(self, package_file: Optional[str] = None,
                        target_version: Optional[str] = None, **_kwargs) -> str:
        """Simulate a firmware update on the HV controller. See
        :meth:`SimulatedTxDevice.update_firmware` for the shape."""
        new_version = target_version or "sim-1.0.7"
        self._fw_version = str(new_version)
        return self._fw_version

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
        # Reset the TxDevice's running counter so a polled host sees
        # progress starting from 0 at engine start.
        self._tx._train_curr = 0
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
        # Mirror the running counter back to the TxDevice so a
        # polled host (:meth:`SimulatedTxDevice.get_trigger`) sees
        # progress without subscribing to the status-frame OWSignal.
        self._tx._train_curr = self._train_curr
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
            # Flip the TxDevice's trigger_running flag so a polled
            # host's :meth:`get_trigger` sees ``trigger_status ==
            # "STOPPED"`` on the natural-completion edge, matching
            # the operator-initiated stop edge. (Pre-2026-10-01 the
            # sim left trigger_running=True after natural end --
            # that only mattered for callers that subscribed to the
            # STOPPED status frame, not for a polled host.)
            self._tx.stop_trigger()
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
# Simulated DeviceInterface (top-level FDA fake)
# =============================================================================

class SimulatedDeviceInterface(QObject):
    """Drop-in fake for the FDA-facing :class:`openlifu_sdk.io.DeviceInterface`."""

    #: Class-level marker so callers can cheaply distinguish a simulated
    #: interface from a real :class:`~openlifu_sdk.io.LIFUInterface`
    #: without importing the simulated class itself.
    is_simulated: bool = True

    def __init__(self, num_modules: int = 1,
                 transducer=None,
                 preset_flash: Optional[List[tuple[dict, int]]] = None,
                 **_unused):
        # When a transducer (array) is supplied, derive num_modules from it
        # so the TX device is built with the right module count up front.
        if transducer is not None:
            modules_attr = getattr(transducer, "modules", None)
            if modules_attr is not None:
                num_modules = max(1, len(list(modules_attr)))
        super().__init__()
        self.txdevice = SimulatedTxDevice(
            num_modules=num_modules,
            preset_flash=preset_flash,
        )
        self.hvcontroller = SimulatedHVController()
        self.status = LIFUInterfaceStatus.STATUS_SYS_OFF
        self._engine: Optional[_SimulatedRunEngine] = None
        self._last_solution_voltage = 0.0
        self._last_trigger_mode = "sequence"
        if transducer is not None and getattr(transducer, "modules", None) is not None:
            self.txdevice.apply_simulated_transducer(transducer)
        if preset_flash is not None:
            hv_presets: list[dict] = []
            for index, (machine_config, _regs_crc) in enumerate(preset_flash):
                voltage_range = machine_config.get("voltage_range") or [machine_config.get("voltage", 0.0), machine_config.get("voltage", 0.0)]
                hv_presets.append({
                    "count": len(preset_flash),
                    "index": index,
                    "selected": None,
                    "settings_crc": int(machine_config.get("settings_crc", 0)),
                    "voltage": float(machine_config.get("voltage", 0.0)),
                    "id": machine_config.get("id", f"preset-{index}"),
                    "sensitivity_ref": machine_config.get("sensitivity"),
                    "min_voltage": float(voltage_range[0]),
                    "max_voltage": float(voltage_range[1]),
                })
            self.hvcontroller.configure_presets(hv_presets)

    # ---- monitoring lifecycle -------------------------------------------

    async def start_monitoring(self, interval: int = 1):
        # Auto-connect both devices ~AUTO_CONNECT_DELAY_S after launch
        # via QTimer so the connect signals are delivered on the GUI
        # thread (mirroring the real OWSignal -> Bridge path).
        delay_ms = int(AUTO_CONNECT_DELAY_S * 1000)

        def _connect():
            logger.info("SimulatedDeviceInterface: emitting auto-connect for HV + TX")
            self.hvcontroller.emit_connected()
            self.txdevice.emit_connected()

        QTimer.singleShot(delay_ms, _connect)
        return None

    def stop_monitoring(self):
        return None

    def is_device_connected(self):
        return (self.txdevice.is_connected(), self.hvcontroller.is_connected())

    # ---- FDA preset / sonication ----------------------------------------

    def load_preset(self, preset_index: int, settings_crc: int, regs_crc: int,
                    frequency_hz: float,
                    duration_index: int = 0, turn_hv_on: bool = False,
                    wait_for_settle: bool = False) -> bool:
        self.txdevice.load_preset(preset_index, regs_crc, duration_index)
        if not self.txdevice.module_user_configs:
            self.txdevice.refresh_metadata()
        device_sensitivity = self.txdevice.get_sensitivity_for_frequency(frequency_hz)
        if device_sensitivity is None:
            raise LIFUSolutionError(
                f"TX device sensitivity is unavailable at {frequency_hz:.3f} Hz; "
                "cannot select FDA preset on HV.")
        self.hvcontroller.select_preset(
            preset_index,
            settings_crc,
            device_sensitivity,
            frequency_hz,
        )
        self._last_solution_voltage = self.hvcontroller.supply_voltage or self._last_solution_voltage
        self.set_status(LIFUInterfaceStatus.STATUS_READY)
        if turn_hv_on:
            self.hvcontroller.turn_hv_on()
        if wait_for_settle:
            time.sleep(0.2)
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


# =============================================================================
# Simulated LIFUInterface (backwards-compatible RUO superset)
# =============================================================================

class SimulatedLIFUInterface(SimulatedDeviceInterface):
    """Compatibility fake for the research/RUO :class:`openlifu_sdk.LIFUInterface`."""

    def __init__(self, num_modules: int = 1,
                 transducer=None,
                 voltage_table_selection: Optional[str] = None,
                 preset_flash: Optional[list[tuple[dict, int]]] = None,
                 **_unused):
        super().__init__(num_modules=num_modules, transducer=transducer,
                         preset_flash=preset_flash)
        self.voltage_table_selection = voltage_table_selection

    def set_module_invert(self, module_invert):
        self.txdevice.set_module_invert(module_invert)

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

    def check_solution(self, solution):  # always passes
        return None


__all__ = [
    "SimulatedDeviceInterface",
    "SimulatedHVController",
    "SimulatedLIFUInterface",
    "SimulatedTxDevice",
]
