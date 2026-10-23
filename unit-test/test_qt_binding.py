# SPDX-License-Identifier: Apache-2.0
"""Unit tests for :mod:`openlifu_sdk.ui._qt` binding selection (#76).

``openlifu_sdk.ui`` builds its QObjects on the host app's Qt binding:
``OPENLIFU_QT_API`` if set, else the binding the host already
imported, else PyQt6, then PySide6. 3D Slicer's PythonQt path in
``simulated_interface`` bypasses the selector.

Each case runs in a fresh interpreter: the binding is chosen once per
process, and pytest itself may already have imported one. Cases that
need a binding skip when it is not installed; the ``pytest (PySide6 +
PyQt6)`` CI job installs both, so none skip there.

In-process behaviour on each binding (BaseConnector's QML contract,
cross-thread delivery, the simulator's timers) is covered by
``test_base_connector.py`` and the simulator tests, which the PySide6
job runs with ``OPENLIFU_QT_API=pyside6``.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap

import pytest

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")

HAS_PYQT6 = importlib.util.find_spec("PyQt6") is not None
HAS_PYSIDE6 = importlib.util.find_spec("PySide6") is not None
needs_pyqt6 = pytest.mark.skipif(not HAS_PYQT6, reason="PyQt6 not installed")
needs_pyside6 = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not installed")


def _run(code: str, qt_api: str | None = None) -> subprocess.CompletedProcess:
    """Run ``code`` in a fresh interpreter; ``qt_api`` sets OPENLIFU_QT_API."""
    env = {k: v for k, v in os.environ.items() if k != "OPENLIFU_QT_API"}
    if qt_api is not None:
        env["OPENLIFU_QT_API"] = qt_api
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [_SRC, env.get("PYTHONPATH")]))
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        env=env, capture_output=True, text=True, timeout=60,
    )


def _check(code: str, qt_api: str | None = None) -> None:
    result = _run(code, qt_api)
    assert result.returncode == 0, result.stderr


@needs_pyqt6
def test_defaults_to_pyqt6_when_no_binding_is_imported():
    _check("""
        import sys
        from openlifu_sdk.ui import _qt
        assert _qt.QT_API == "PyQt6", _qt.QT_API
        assert "PySide6" not in sys.modules
    """)


@needs_pyside6
def test_falls_back_to_pyside6_when_pyqt6_cannot_be_imported():
    # sys.modules[name] = None blocks the import; it must not count as
    # "the host imported PyQt6".
    _check("""
        import sys
        sys.modules["PyQt6"] = None
        from openlifu_sdk.ui import _qt
        assert _qt.QT_API == "PySide6", _qt.QT_API
    """)


@needs_pyside6
def test_follows_pyside6_when_the_host_imported_it_first():
    _check("""
        import sys
        from PySide6.QtCore import QObject, QThread
        from openlifu_sdk.ui import _qt
        from openlifu_sdk.ui.base_connector import BaseConnector
        from openlifu_sdk.ui.telemetry_thread import TelemetryPollThread
        assert _qt.QT_API == "PySide6", _qt.QT_API
        assert issubclass(BaseConnector, QObject)
        assert issubclass(TelemetryPollThread, QThread)
        assert "PyQt6" not in sys.modules
    """)


@needs_pyqt6
def test_follows_pyqt6_when_the_host_imported_it_first():
    _check("""
        import sys
        from PyQt6.QtCore import QObject, QThread
        from openlifu_sdk.ui import _qt
        from openlifu_sdk.ui.base_connector import BaseConnector
        from openlifu_sdk.ui.telemetry_thread import TelemetryPollThread
        assert _qt.QT_API == "PyQt6", _qt.QT_API
        assert issubclass(BaseConnector, QObject)
        assert issubclass(TelemetryPollThread, QThread)
        assert "PySide6" not in sys.modules
    """)


@needs_pyqt6
def test_both_bindings_imported_falls_back_to_the_default_order_with_a_warning():
    # A stand-in module marks PySide6 as imported, so only one real Qt
    # runtime loads.
    result = _run("""
        import logging, sys, types
        logging.basicConfig(level=logging.WARNING)
        import PyQt6.QtCore
        sys.modules["PySide6"] = types.ModuleType("PySide6")
        from openlifu_sdk.ui import _qt
        assert _qt.QT_API == "PyQt6", _qt.QT_API
    """)
    assert result.returncode == 0, result.stderr
    assert "Both PyQt6 and PySide6 are imported" in result.stderr


@pytest.mark.parametrize(
    ("qt_api", "expected", "other"),
    [
        pytest.param("pyside6", "PySide6", "PyQt6", marks=needs_pyside6),
        pytest.param(" PyQt6 ", "PyQt6", "PySide6", marks=needs_pyqt6),
    ],
)
def test_env_var_selects_the_binding_and_loads_only_that_one(qt_api, expected, other):
    # No host import here: loading a second binding on top of a host's
    # Qt puts two Qt runtimes in one process, which this module exists
    # to prevent (on Windows the second QtCore fails to load).
    _check(f"""
        import sys
        from openlifu_sdk.ui import _qt
        assert _qt.QT_API == "{expected}", _qt.QT_API
        assert "{other}" not in sys.modules
    """, qt_api=qt_api)


def test_env_var_rejects_an_unknown_binding():
    result = _run("from openlifu_sdk.ui import _qt", qt_api="pyqt5")
    assert result.returncode != 0
    assert "ValueError: OPENLIFU_QT_API='pyqt5' is not supported" in result.stderr


@pytest.mark.parametrize(
    ("qt_api", "imported"),
    [("pyqt6", "PySide6"), ("pyside6", "PyQt6")],
)
def test_env_var_naming_another_binding_than_the_host_imported_is_refused(qt_api, imported):
    # A stand-in module plays the host's binding; the refusal comes
    # before anything is imported, so no real Qt loads.
    result = _run(f"""
        import sys, types
        sys.modules["{imported}"] = types.ModuleType("{imported}")
        from openlifu_sdk.ui import _qt
    """, qt_api=qt_api)
    assert result.returncode != 0
    assert f"already imported {imported}" in result.stderr
    assert "second Qt runtime" in result.stderr


def test_bad_env_var_surfaces_from_simulated_interface():
    """The simulator's PythonQt fallback catches ImportError only, so a
    configuration error is reported as itself, not as "no Qt found"."""
    result = _run(
        "from openlifu_sdk.ui import simulated_interface", qt_api="pyqt5",
    )
    assert result.returncode != 0
    assert "ValueError: OPENLIFU_QT_API='pyqt5' is not supported" in result.stderr
    assert "3D Slicer's PythonQt" not in result.stderr


@needs_pyside6
def test_pyside6_queued_signal_and_sdk_timer_deliver_on_the_host_loop():
    """On a PySide6 host, a queued BaseConnector signal is delivered only
    by the host's event loop, and a QTimer parented to an SDK object
    fires on it. The simulator is built on PySide6."""
    _check("""
        import sys
        from PySide6.QtCore import QCoreApplication, QObject, Qt
        app = QCoreApplication([])
        from openlifu_sdk.ui._qt import QTimer
        from openlifu_sdk.ui.base_connector import BaseConnector
        from openlifu_sdk.ui.simulated_interface import (
            _QT_BACKEND, SimulatedDeviceInterface,
        )
        assert _QT_BACKEND == "PySide6", _QT_BACKEND
        assert isinstance(SimulatedDeviceInterface(), QObject)

        received = []
        connector = BaseConnector()
        connector.stateChanged.connect(
            received.append, Qt.ConnectionType.QueuedConnection,
        )
        connector.stateChanged.emit(2)
        assert received == [], "queued delivery must wait for the loop"

        timer = QTimer(connector)
        timer.setSingleShot(True)
        timer.timeout.connect(app.quit)
        timer.start(0)
        QTimer.singleShot(5000, lambda: app.exit(1))
        assert app.exec() == 0, "SDK-parented timer did not fire"
        assert received == [2], received
        assert "PyQt6" not in sys.modules
    """)


def test_slicer_pythonqt_path_bypasses_the_selector():
    """Inside 3D Slicer, simulated_interface uses Slicer's ``qt`` module
    and never imports ``_qt`` or a Qt6 binding."""
    _check("""
        import sys, types
        sys.modules["slicer"] = types.ModuleType("slicer")
        qt = types.ModuleType("qt")

        class QObject:
            def __init__(self, *args, **kwargs):
                pass

        class QTimer(QObject):
            pass

        qt.QObject, qt.QTimer = QObject, QTimer
        qt.Signal = lambda *args, **kwargs: None
        sys.modules["qt"] = qt

        from openlifu_sdk.ui import simulated_interface
        assert simulated_interface._QT_BACKEND == "PythonQt"
        assert "openlifu_sdk.ui._qt" not in sys.modules
        assert "PyQt6" not in sys.modules
        assert "PySide6" not in sys.modules
    """)
