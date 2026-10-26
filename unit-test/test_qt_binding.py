# SPDX-License-Identifier: Apache-2.0
"""Unit tests for :mod:`openlifu_sdk.ui._qt` binding selection (#76).

``openlifu_sdk.ui`` builds its QObjects on the host app's Qt binding:
``OPENLIFU_QT_API`` if set (it must match the host's binding), else the
binding the host already imported, else PyQt6, then PySide6. Cases it
can't resolve safely raise, and SDK objects refuse to construct when the
host's application runs on the other binding. 3D Slicer's PythonQt
path in ``simulated_interface`` bypasses the selector.

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

import pytest

from qt_subprocess import check as _check
from qt_subprocess import needs_pyqt6, needs_pyside6
from qt_subprocess import run as _run


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
    # "the host imported PyQt6". A binding that isn't there is no
    # warning.
    result = _check("""
        import logging, sys
        logging.basicConfig(level=logging.WARNING)
        sys.modules["PyQt6"] = None
        from openlifu_sdk.ui import _qt
        assert _qt.QT_API == "PySide6", _qt.QT_API
    """)
    assert "failed to import" not in result.stderr


@needs_pyqt6
@needs_pyside6
def test_warns_when_an_installed_binding_fails_to_import():
    # PyQt6 is installed, but its QtCore fails the way a missing Qt DLL
    # does. The fallback to PySide6 must say so rather than stay silent.
    result = _check("""
        import importlib.abc, logging, sys
        logging.basicConfig(level=logging.WARNING)

        class BrokenQtCore(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name == "PyQt6.QtCore":
                    raise ImportError("DLL load failed while importing QtCore")

        sys.meta_path.insert(0, BrokenQtCore())
        from openlifu_sdk.ui import _qt
        assert _qt.QT_API == "PySide6", _qt.QT_API
    """)
    assert "PyQt6 is installed but failed to import" in result.stderr
    assert "DLL load failed while importing QtCore" in result.stderr


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


def test_both_bindings_imported_without_env_var_is_refused():
    # Stand-in QtCore modules mark both bindings as imported (a binding
    # counts once its QtCore is in sys.modules); the refusal comes
    # before any real Qt loads.
    result = _run("""
        import sys, types
        for name in ("PyQt6", "PySide6"):
            sys.modules[name] = types.ModuleType(name)
            sys.modules[f"{name}.QtCore"] = types.ModuleType(f"{name}.QtCore")
        from openlifu_sdk.ui import _qt
    """)
    assert result.returncode != 0
    assert "ValueError: Both PyQt6 and PySide6 are imported" in result.stderr
    assert "Set OPENLIFU_QT_API" in result.stderr


@pytest.mark.parametrize(
    ("qt_api", "real", "stand_in"),
    [
        pytest.param("pyqt6", "PyQt6", "PySide6", marks=needs_pyqt6),
        pytest.param("pyside6", "PySide6", "PyQt6", marks=needs_pyside6),
    ],
)
def test_both_bindings_imported_env_var_picks_one(qt_api, real, stand_in):
    _check(f"""
        import sys, types
        import {real}.QtCore
        sys.modules["{stand_in}"] = types.ModuleType("{stand_in}")
        sys.modules["{stand_in}.QtCore"] = types.ModuleType("{stand_in}.QtCore")
        from openlifu_sdk.ui import _qt
        assert _qt.QT_API == "{real}", _qt.QT_API
    """, qt_api=qt_api)


@needs_pyqt6
@needs_pyside6
def test_a_bare_package_import_does_not_count_as_the_host_binding():
    # A library probing for PyQt6 imports the bare package, which loads
    # no Qt runtime: a PySide6 host must not be refused as "both imported".
    _check("""
        import importlib.util, sys
        import PySide6.QtCore
        assert importlib.util.find_spec("PyQt6.QtCore") is not None
        assert "PyQt6" in sys.modules and "PyQt6.QtCore" not in sys.modules
        from openlifu_sdk.ui import _qt
        assert _qt.QT_API == "PySide6", _qt.QT_API
        assert "PyQt6.QtCore" not in sys.modules
    """)


_HOST_APP_ON_OTHER_BINDING = """
    import sys, types
    from openlifu_sdk.ui import _qt
    assert _qt.QT_API == "{chosen}", _qt.QT_API
    # The host imports its binding only now, after the SDK, and starts
    # its application on it. A stand-in plays that binding, so only one
    # real Qt runtime loads.
    other = types.ModuleType("{other}")
    other.QtCore = types.ModuleType("{other}.QtCore")
    other.QtCore.QCoreApplication = type(
        "QCoreApplication", (), {{"instance": staticmethod(lambda: object())}},
    )
    sys.modules["{other}"] = other
    sys.modules["{other}.QtCore"] = other.QtCore
    from openlifu_sdk.ui.base_connector import BaseConnector
    from openlifu_sdk.ui.simulated_interface import SimulatedDeviceInterface
    from openlifu_sdk.ui.telemetry_thread import TelemetryPollThread
    for cls, args in (
        (BaseConnector, ()), (SimulatedDeviceInterface, ()), (TelemetryPollThread, (None,)),
    ):
        try:
            cls(*args)
        except RuntimeError as exc:
            assert "the application runs on {other}" in str(exc), exc
            assert "Import {other} before openlifu_sdk.ui" in str(exc), exc
        else:
            raise AssertionError(f"{{cls.__name__}} constructed on the wrong binding")
"""


@pytest.mark.parametrize(
    ("qt_api", "chosen", "other"),
    [
        pytest.param("pyqt6", "PyQt6", "PySide6", marks=needs_pyqt6),
        pytest.param("pyside6", "PySide6", "PyQt6", marks=needs_pyside6),
    ],
)
def test_sdk_objects_refuse_a_host_application_on_the_other_binding(qt_api, chosen, other):
    _check(_HOST_APP_ON_OTHER_BINDING.format(chosen=chosen, other=other), qt_api=qt_api)


@pytest.mark.parametrize(
    ("qt_api", "binding"),
    [
        pytest.param("pyqt6", "PyQt6", marks=needs_pyqt6),
        pytest.param("pyside6", "PySide6", marks=needs_pyside6),
    ],
)
def test_sdk_objects_construct_with_no_application_or_one_on_their_binding(qt_api, binding):
    _check(f"""
        from openlifu_sdk.ui import _qt
        from openlifu_sdk.ui.base_connector import BaseConnector
        from openlifu_sdk.ui.simulated_interface import SimulatedDeviceInterface
        from openlifu_sdk.ui.telemetry_thread import TelemetryPollThread
        assert _qt.QT_API == "{binding}", _qt.QT_API
        BaseConnector(); SimulatedDeviceInterface(); TelemetryPollThread(None)
        from {binding}.QtCore import QCoreApplication
        app = QCoreApplication([])
        BaseConnector(); SimulatedDeviceInterface(); TelemetryPollThread(None)
    """, qt_api=qt_api)


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
    # A stand-in QtCore plays the host's binding; the refusal comes
    # before anything is imported, so no real Qt loads.
    result = _run(f"""
        import sys, types
        sys.modules["{imported}"] = types.ModuleType("{imported}")
        sys.modules["{imported}.QtCore"] = types.ModuleType("{imported}.QtCore")
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
