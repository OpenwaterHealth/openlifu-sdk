# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the simulated FDA components' ``connect`` (SR-014).

A host re-establishes a lost link by calling ``connect()`` on each
component, as on the real :class:`OWComponent`. The simulated link
follows the simulated cable, so ``connect()`` reports it and never
plugs it in.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from openlifu_sdk.ui.simulated_interface import (
    SimulatedHVController,
    SimulatedTxDevice,
)


@pytest.mark.parametrize("make", [SimulatedTxDevice, SimulatedHVController])
def test_connect_fails_while_unplugged(make):
    device = make()
    assert device.connect() is False
    assert device.is_connected() is False


@pytest.mark.parametrize("make", [SimulatedTxDevice, SimulatedHVController])
def test_connect_succeeds_once_plugged_back_in(make):
    device = make()
    device.emit_connected()
    device.emit_disconnected()
    assert device.connect() is False
    device.emit_connected()
    assert device.connect() is True
    assert device.is_connected() is True
