"""Reusable Qt building blocks for openlifu-sdk-based applications.

Most submodules in this package (``base_connector``, ``telemetry_thread``,
``version_check``) require PyQt6 — install it with the ``ui`` optional
extra::

    pip install openlifu-sdk[ui]

The :mod:`~openlifu_sdk.ui.simulated_interface` submodule is more
flexible: it works with either PyQt6 or the PythonQt-based ``qt`` module
that 3D Slicer ships, so the simulated hardware can be plugged into a
Slicer-based application without pulling PyQt6 alongside Slicer's
bundled Qt5.

Public symbols are imported lazily via :pep:`562` ``__getattr__`` so
this package can be imported even when PyQt6 is not installed; an
:class:`ImportError` is raised only when a PyQt6-only symbol is actually
accessed.

Public surface (loaded on first attribute access):

- :class:`BaseConnector` — minimal QObject scaffolding that wires a
  :class:`~openlifu_sdk.LIFUInterface` to QML: connection-state machine
  (DISCONNECTED / CONNECTED / READY / RUNNING), connect/disconnect
  signals, and a telemetry polling thread.
- :class:`SimulatedLIFUInterface` — in-memory fake of
  :class:`~openlifu_sdk.LIFUInterface` for ``--simulate`` modes.
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

