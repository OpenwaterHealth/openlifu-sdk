# SPDX-License-Identifier: Apache-2.0
"""Qt binding selection for :mod:`openlifu_sdk.ui`.

The ``ui`` helpers subclass ``QObject`` / ``QThread`` and declare
signals, so they have to be built on the same Qt binding as the host
application: a PySide6 app cannot parent, connect or run timers on a
PyQt6 object (each binding ships and drives its own Qt runtime).

The binding is chosen once, at first import, in this order:

1. ``OPENLIFU_QT_API`` environment variable (``pyqt6`` or ``pyside6``).
   It must name a binding the host has imported, if the host has
   imported any: anything else would load a second Qt runtime.
2. The binding the host has already imported. Apps should therefore
   import their Qt binding before importing ``openlifu_sdk.ui``.
3. If the host has imported neither: PyQt6 (the historical default and
   the ``ui`` extra), then PySide6. One that is present but fails to
   load (an ``ImportError`` other than ``ModuleNotFoundError``, such as
   a missing Qt DLL) is logged as a warning before the next is tried.

"Imported" means ``<binding>.QtCore`` is in ``sys.modules``; the bare
package alone loads no Qt runtime.

Every case the SDK can't resolve safely raises :class:`ValueError`: an
unknown ``OPENLIFU_QT_API``, one naming a binding other than the
host's, and a host that has imported both bindings with no
``OPENLIFU_QT_API`` to say which one its application runs on.

A host that imports ``openlifu_sdk.ui`` *before* its own binding can't
be caught here. :func:`check_host_binding`, called when an SDK object
is constructed, reports it then.

Exports binding-neutral names: :data:`QObject`, :data:`QThread`,
:data:`QTimer`, :data:`Signal`, :data:`Slot`, :data:`Property`, the
chosen :data:`QT_API` (``"PyQt6"`` or ``"PySide6"``),
:func:`import_qt_module` and :func:`check_host_binding`.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys

logger = logging.getLogger(__name__)

_BINDINGS = {"pyqt6": "PyQt6", "pyside6": "PySide6"}


def _select_api() -> str:
    # A binding is in use once its QtCore is imported, as in
    # check_host_binding: the bare package (``import PyQt6``, or a
    # ``find_spec("PyQt6.QtCore")`` probe) loads no Qt runtime. A ``None``
    # entry is an import blocker (``sys.modules[name] = None``), not a
    # loaded binding.
    loaded = [
        api for api in _BINDINGS.values() if sys.modules.get(f"{api}.QtCore") is not None
    ]
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
        raise ValueError(
            "Both PyQt6 and PySide6 are imported, so openlifu_sdk.ui can't tell "
            "which one the application runs on. Set OPENLIFU_QT_API to "
            "pyqt6 or pyside6."
        )
    for api in _BINDINGS.values():
        try:
            importlib.import_module(f"{api}.QtCore")
        except ModuleNotFoundError:  # not installed, blocked, or a stand-in
            continue
        except ImportError as exc:  # present, but e.g. a Qt DLL failed to load
            logger.warning("%s is present but failed to import: %s", api, exc)
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


def check_host_binding() -> None:
    """Raise :class:`RuntimeError` if the application object runs on the
    other binding.

    That happens when a host imports ``openlifu_sdk.ui`` before its own
    binding: the SDK picks the default, the host then starts its
    ``QCoreApplication`` on the other one, and SDK signals and timers
    would never run on the host's event loop. No application yet, or one
    on :data:`QT_API`, passes.
    """
    other = "PySide6" if QT_API == "PyQt6" else "PyQt6"
    other_core = sys.modules.get(f"{other}.QtCore")
    if other_core is None:
        return
    if import_qt_module("QtCore").QCoreApplication.instance() is not None:
        return
    if other_core.QCoreApplication.instance() is None:
        return
    raise RuntimeError(
        f"openlifu_sdk.ui is built on {QT_API}, but the application runs on "
        f"{other}: its signals and timers would never reach the event loop. "
        f"Import {other} before openlifu_sdk.ui, or set "
        f"OPENLIFU_QT_API={other.lower()}."
    )


__all__ = [
    "QT_API",
    "Property",
    "QObject",
    "QThread",
    "QTimer",
    "Signal",
    "Slot",
    "check_host_binding",
    "import_qt_module",
]
