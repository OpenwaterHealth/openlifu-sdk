"""Reusable Qt/PyQt6 building blocks for openlifu-sdk-based applications.

This subpackage requires PyQt6. Install with the ``ui`` optional extra:

    pip install openlifu-sdk[ui]

Public surface:

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

from openlifu_sdk.ui.base_connector import BaseConnector, ConnectorState
from openlifu_sdk.ui.simulated_interface import (
    SimulatedHVController,
    SimulatedLIFUInterface,
    SimulatedTxDevice,
)
from openlifu_sdk.ui.status_frame import format_status_frame, parse_status_string
from openlifu_sdk.ui.telemetry_thread import TelemetryPollThread
from openlifu_sdk.ui.version_check import (
    check_sdk_version,
    parse_sdk_version,
    show_incompatible_version_dialog,
)

__all__ = [
    "BaseConnector",
    "ConnectorState",
    "SimulatedHVController",
    "SimulatedLIFUInterface",
    "SimulatedTxDevice",
    "TelemetryPollThread",
    "check_sdk_version",
    "format_status_frame",
    "parse_sdk_version",
    "parse_status_string",
    "show_incompatible_version_dialog",
]
