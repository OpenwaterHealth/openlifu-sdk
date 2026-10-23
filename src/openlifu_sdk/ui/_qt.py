# SPDX-License-Identifier: Apache-2.0
"""Qt binding selection for :mod:`openlifu_sdk.ui`.

The ``ui`` helpers subclass ``QObject`` / ``QThread`` and declare
signals, so they have to be built on the same Qt binding as the host
application: a PySide6 app cannot parent, connect or run timers on a
PyQt6 object (each binding ships and drives its own Qt runtime).

The binding is chosen once, at first import, in this order:

1. ``OPENLIFU_QT_API`` environment variable (``pyqt6`` or ``pyside6``).
   An unknown value, or one naming a binding other than the one the
   host already imported, raises :class:`ValueError`: honouring it would
   load a second Qt runtime.
2. A binding the host has already imported. Apps should therefore
   import their Qt binding before importing ``openlifu_sdk.ui``.
3. PyQt6 (the historical default and the ``ui`` extra), then PySide6.
   This also applies, with a logged warning, when the host has imported
   both bindings.

Exports binding-neutral names: :data:`QObject`, :data:`QThread`,
:data:`QTimer`, :data:`Signal`, :data:`Slot`, :data:`Property`, the
chosen :data:`QT_API` (``"PyQt6"`` or ``"PySide6"``) and
:func:`import_qt_module`.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys

logger = logging.getLogger(__name__)

_BINDINGS = {"pyqt6": "PyQt6", "pyside6": "PySide6"}


def _select_api() -> str:
    # A ``None`` entry is an import blocker (``sys.modules[name] = None``),
    # not a loaded binding.
    loaded = [api for api in _BINDINGS.values() if sys.modules.get(api) is not None]
    requested = os.environ.get("OPENLIFU_QT_API", "").strip().lower()
    if requested:
        if requested not in _BINDINGS:
            raise ValueError(
                f"OPENLIFU_QT_API={requested!r} is not supported; "
                f"use one of {sorted(_BINDINGS)}."
            )
        api = _BINDINGS[requested]
        if loaded and api not in loaded:
            raise ValueError(
                f"OPENLIFU_QT_API={requested!r} asks for {api}, but the host "
                f"already imported {loaded[0]}; using {api} would load a "
                "second Qt runtime. Unset OPENLIFU_QT_API or make it match."
            )
        return api
    if len(loaded) == 1:
        return loaded[0]
    if loaded:
        logger.warning(
            "Both PyQt6 and PySide6 are imported; openlifu_sdk.ui uses PyQt6. "
            "Set OPENLIFU_QT_API to choose."
        )
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
