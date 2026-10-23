# SPDX-License-Identifier: Apache-2.0
"""Run test code in a fresh interpreter, for the Qt binding tests.

:mod:`openlifu_sdk.ui._qt` chooses its binding once per process, and
pytest may already have imported one, so binding cases each need their
own interpreter.
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


def run(code: str, qt_api: str | None = None) -> subprocess.CompletedProcess:
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


def check(code: str, qt_api: str | None = None) -> subprocess.CompletedProcess:
    """:func:`run`, asserting a zero exit status."""
    result = run(code, qt_api)
    assert result.returncode == 0, result.stderr
    return result
