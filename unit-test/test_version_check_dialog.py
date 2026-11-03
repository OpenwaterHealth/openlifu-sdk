# SPDX-License-Identifier: Apache-2.0
"""Unit tests for :func:`openlifu_sdk.ui.version_check.show_incompatible_version_dialog`
on each Qt binding (#76).

The dialog is a startup gate: apps call it, then exit. It must show on
a widgets application, and on any other host it must report on stderr
rather than let Qt abort the process. Each case runs in a fresh
interpreter, on the binding named by ``OPENLIFU_QT_API``.
"""

from __future__ import annotations

import pytest

from qt_subprocess import check, needs_pyqt6, needs_pyside6

BINDINGS = [
    pytest.param("pyqt6", "PyQt6", marks=needs_pyqt6),
    pytest.param("pyside6", "PySide6", marks=needs_pyside6),
]

# Replace QMessageBox.critical so the modal dialog records its call
# instead of blocking the test.
_RECORD_CRITICAL = """
    calls = []
    QtWidgets = __import__("{binding}.QtWidgets", fromlist=["QtWidgets"])
    QtWidgets.QMessageBox.critical = staticmethod(
        lambda parent, title, text: calls.append((title, text))
    )
"""


@pytest.mark.parametrize(("qt_api", "binding"), BINDINGS)
def test_dialog_shows_on_a_widgets_application(qt_api, binding):
    check(_RECORD_CRITICAL.format(binding=binding) + """
    from openlifu_sdk.ui.version_check import show_incompatible_version_dialog
    show_incompatible_version_dialog("SDK too old", title="Incompatible")
    assert calls == [("Incompatible", "SDK too old\\n\\nThe application will now exit.")], calls
    """, qt_api=qt_api)


@pytest.mark.parametrize(("qt_api", "binding"), BINDINGS)
def test_dialog_reports_on_stderr_for_a_qml_only_application(qt_api, binding):
    result = check(_RECORD_CRITICAL.format(binding=binding) + """
    import logging
    logging.basicConfig(level=logging.ERROR)
    QtGui = __import__("{binding}.QtGui", fromlist=["QtGui"])
    app = QtGui.QGuiApplication([])
    from openlifu_sdk.ui.version_check import show_incompatible_version_dialog
    show_incompatible_version_dialog("SDK too old")
    assert calls == [], calls
    """.format(binding=binding), qt_api=qt_api)
    assert "ERROR: SDK too old" in result.stderr
    assert "not a QApplication" in result.stderr


@pytest.mark.parametrize(
    ("qt_api", "binding", "other"),
    [
        pytest.param("pyqt6", "PyQt6", "PySide6", marks=needs_pyqt6),
        pytest.param("pyside6", "PySide6", "PyQt6", marks=needs_pyside6),
    ],
)
def test_dialog_reports_on_stderr_for_an_application_on_the_other_binding(
    qt_api, binding, other,
):
    # A stand-in plays the host's binding and its running application,
    # so only one real Qt runtime loads. The dialog must not start a
    # second application on the SDK's binding.
    result = check(_RECORD_CRITICAL.format(binding=binding) + f"""
    import logging, sys, types
    logging.basicConfig(level=logging.ERROR)
    other = types.ModuleType("{other}")
    other.QtCore = types.ModuleType("{other}.QtCore")
    other.QtCore.QCoreApplication = type(
        "QCoreApplication", (), {{"instance": staticmethod(lambda: object())}},
    )
    sys.modules["{other}"] = other
    sys.modules["{other}.QtCore"] = other.QtCore
    from openlifu_sdk.ui.version_check import show_incompatible_version_dialog
    show_incompatible_version_dialog("SDK too old")
    assert calls == [], calls
    assert QtWidgets.QApplication.instance() is None, "started a second application"
    """, qt_api=qt_api)
    assert "ERROR: SDK too old" in result.stderr
    assert f"the application runs on {other}" in result.stderr


@pytest.mark.parametrize(("qt_api", "binding"), BINDINGS)
def test_dialog_falls_back_to_stderr_and_logs_when_qt_fails(qt_api, binding):
    result = check("""
    import logging
    logging.basicConfig(level=logging.ERROR)
    from openlifu_sdk.ui import _qt
    def _broken(name):
        raise ImportError(f"no {name}")
    _qt.import_qt_module = _broken
    from openlifu_sdk.ui.version_check import show_incompatible_version_dialog
    show_incompatible_version_dialog("SDK too old")
    """, qt_api=qt_api)
    assert "ERROR: SDK too old" in result.stderr
    assert "Could not show the SDK version dialog" in result.stderr
    assert "ImportError: no QtWidgets" in result.stderr
