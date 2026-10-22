"""Qt binding selection for :mod:`openlifu_sdk.ui`.

The ``ui`` helpers subclass ``QObject`` / ``QThread`` and declare
signals, so they have to be built on the same Qt binding as the host
application: a PySide6 app cannot parent, connect or run timers on a
PyQt6 object (each binding ships and drives its own Qt runtime).

The binding is chosen once, at first import, in this order:

1. ``OPENLIFU_QT_API`` environment variable (``pyqt6`` or ``pyside6``).
2. A binding the host has already imported. Apps should therefore
   import their Qt binding before importing ``openlifu_sdk.ui``.
3. PyQt6 (the historical default and the ``ui`` extra), then PySide6.

Exports binding-neutral names: :data:`QObject`, :data:`QThread`,
:data:`QTimer`, :data:`Signal`, :data:`Slot`, :data:`Property`, the
chosen :data:`QT_API` (``"PyQt6"`` or ``"PySide6"``) and
:func:`import_qt_module`.
"""

from __future__ import annotations

import importlib
import os
import sys

_BINDINGS = {"pyqt6": "PyQt6", "pyside6": "PySide6"}


def _select_api() -> str:
    requested = os.environ.get("OPENLIFU_QT_API", "").strip().lower()
    if requested:
        if requested not in _BINDINGS:
            raise ImportError(
                f"OPENLIFU_QT_API={requested!r} is not supported; "
                f"use one of {sorted(_BINDINGS)}."
            )
        return _BINDINGS[requested]
    loaded = [api for api in _BINDINGS.values() if api in sys.modules]
    if len(loaded) == 1:
        return loaded[0]
    for api in _BINDINGS.values():
        try:
            importlib.import_module(f"{api}.QtCore")
        except ImportError:
            continue
        return api
    raise ImportError(
        "openlifu_sdk.ui requires PyQt6 (install with the 'ui' extra) or PySide6."
    )


QT_API = _select_api()

if QT_API == "PySide6":
    from PySide6.QtCore import Property, QObject, QThread, QTimer, Signal, Slot
else:
    from PyQt6.QtCore import QObject, QThread, QTimer
    from PyQt6.QtCore import pyqtProperty as Property
    from PyQt6.QtCore import pyqtSignal as Signal
    from PyQt6.QtCore import pyqtSlot as Slot


def import_qt_module(name: str):
    """Import ``<QT_API>.<name>`` (e.g. ``"QtWidgets"``) from the chosen binding."""
    return importlib.import_module(f"{QT_API}.{name}")


__all__ = [
    "QT_API",
    "Property",
    "QObject",
    "QThread",
    "QTimer",
    "Signal",
    "Slot",
    "import_qt_module",
]
