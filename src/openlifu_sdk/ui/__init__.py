# SPDX-License-Identifier: Apache-2.0
"""Reusable Qt building blocks for openlifu-sdk-based applications.

Most submodules in this package (``base_connector``, ``telemetry_thread``,
``version_check``) need a Qt6 binding: PyQt6 (install it with the ``ui``
optional extra)::

    pip install openlifu-sdk[ui]

or PySide6. They follow the binding the host app has already
imported, so a PySide6 app gets PySide6 ``QObject`` subclasses and a
PyQt6 app gets PyQt6 ones. ``OPENLIFU_QT_API=pyqt6|pyside6`` selects
the binding when the host has imported none or both; it must match the
host's binding if the host has imported one. Import the host binding
before this package (see :mod:`openlifu_sdk.ui._qt`).

The :mod:`~openlifu_sdk.ui.simulated_interface` submodule also works
with the PythonQt-based ``qt`` module that 3D Slicer ships, so the
simulated hardware can be plugged into a Slicer-based application
without pulling a Qt6 binding alongside Slicer's bundled Qt5.

Public symbols are imported lazily via :pep:`562` ``__getattr__`` so
this package can be imported even when no Qt binding is installed.
Errors surface only when a Qt-backed symbol is first accessed:
:class:`ImportError` if no Qt6 binding is installed, :class:`ValueError`
if the binding can't be chosen safely (an unknown ``OPENLIFU_QT_API``,
one that contradicts the host's binding, or both bindings imported
with no ``OPENLIFU_QT_API``). Constructing a ``BaseConnector`` or
``SimulatedDeviceInterface`` raises :class:`RuntimeError` if the
host's ``QCoreApplication`` runs on the other binding.

Public surface (loaded on first attribute access):

- :class:`BaseConnector` — minimal QObject scaffolding that wires a
  :class:`~openlifu_sdk.LIFUInterface` to QML: connection-state machine
  (DISCONNECTED / CONNECTED / READY / RUNNING), connect/disconnect
  signals, and a telemetry polling thread.
- :class:`SimulatedDeviceInterface` — in-memory fake of the FDA-facing
  :class:`~openlifu_sdk.io.DeviceInterface` for ``--simulate`` modes.
- :class:`SimulatedLIFUInterface` — compatibility fake of
  :class:`~openlifu_sdk.LIFUInterface` for research / RUO ``--simulate``
  modes.
- :func:`check_sdk_version` / :func:`show_incompatible_version_dialog` —
  pre-flight SDK-version check + QMessageBox helper for apps that pin
  a ``MIN_SDK_VERSION``.
- :func:`parse_status_string` — parser for the unsolicited STATUS
  frames emitted by both real firmware and the simulator.
"""

from __future__ import annotations

import importlib

# Map every public symbol to the submodule that defines it.
_LAZY_SOURCES = {
    "BaseConnector": "openlifu_sdk.ui.base_connector",
    "ConnectorState": "openlifu_sdk.ui.base_connector",
    "SimulatedDeviceInterface": "openlifu_sdk.ui.simulated_interface",
    "SimulatedHVController": "openlifu_sdk.ui.simulated_interface",
    "SimulatedLIFUInterface": "openlifu_sdk.ui.simulated_interface",
    "SimulatedTxDevice": "openlifu_sdk.ui.simulated_interface",
    "TelemetryPollThread": "openlifu_sdk.ui.telemetry_thread",
    "check_sdk_version": "openlifu_sdk.ui.version_check",
    "format_status_frame": "openlifu_sdk.ui.status_frame",
    "parse_sdk_version": "openlifu_sdk.ui.version_check",
    "parse_status_string": "openlifu_sdk.ui.status_frame",
    "show_incompatible_version_dialog": "openlifu_sdk.ui.version_check",
}

__all__ = sorted(_LAZY_SOURCES)


def __getattr__(name: str):
    src = _LAZY_SOURCES.get(name)
    if src is None:
        raise AttributeError(f"module 'openlifu_sdk.ui' has no attribute {name!r}")
    return getattr(importlib.import_module(src), name)


def __dir__():
    return list(__all__)

