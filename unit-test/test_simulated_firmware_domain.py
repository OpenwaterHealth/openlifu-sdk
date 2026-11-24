# SPDX-License-Identifier: Apache-2.0
"""The simulator reports a firmware domain, as hardware does through
``OWComponent.firmware_domain``, and lets a test set it."""

import inspect

import pytest

pytest.importorskip("openlifu_sdk.ui._qt")

from openlifu_sdk.io.component import FirmwareDomain, OWComponent
from openlifu_sdk.ui.simulated_interface import SimulatedHVController, SimulatedTxDevice


class _Module:
    def __init__(self, i):
        self.id, self.name, self.frequency, self.attrs = f"m{i}", f"m{i}", 400e3, {}


class _Array:
    def __init__(self, n):
        self.modules = [_Module(i) for i in range(n)]


def test_defaults_to_research():
    assert SimulatedTxDevice(num_modules=2).firmware_domain(1) is FirmwareDomain.RESEARCH
    assert SimulatedHVController().firmware_domain() is FirmwareDomain.RESEARCH


def test_tx_sets_all_modules_or_one():
    tx = SimulatedTxDevice(num_modules=3)
    tx.set_firmware_domain(FirmwareDomain.CLINICAL)
    assert [tx.firmware_domain(m) for m in range(3)] == [FirmwareDomain.CLINICAL] * 3
    tx.set_firmware_domain("bare-metal", module=2)
    assert [tx.firmware_domain(m) for m in range(3)] == [
        FirmwareDomain.CLINICAL, FirmwareDomain.CLINICAL, FirmwareDomain.BARE_METAL]


def test_hv_set_accepts_value_string():
    hv = SimulatedHVController()
    hv.set_firmware_domain("clinical")
    assert hv.firmware_domain() is FirmwareDomain.CLINICAL


def test_unknown_domain_value_is_refused():
    with pytest.raises(ValueError):
        SimulatedHVController().set_firmware_domain("fda")


def test_module_count_change_keeps_the_domain():
    tx = SimulatedTxDevice(num_modules=1)
    tx.set_firmware_domain(FirmwareDomain.CLINICAL)
    tx.apply_simulated_transducer(_Array(2))
    assert tx.num_modules == 2
    assert [tx.firmware_domain(m) for m in range(2)] == [FirmwareDomain.CLINICAL] * 2


def test_signature_matches_hardware():
    hw = inspect.signature(OWComponent.firmware_domain)
    assert list(inspect.signature(SimulatedTxDevice.firmware_domain).parameters) == list(hw.parameters)
    assert list(inspect.signature(SimulatedHVController.firmware_domain).parameters) == list(hw.parameters)


def test_a_module_added_later_reports_the_set_domain():
    tx = SimulatedTxDevice(num_modules=1)
    tx.set_firmware_domain(FirmwareDomain.CLINICAL)
    tx.num_modules = 3  # as tests and debug UIs grow the chain
    assert tx.firmware_domain(2) is FirmwareDomain.CLINICAL


def test_setting_every_module_clears_a_module_override():
    tx = SimulatedTxDevice(num_modules=2)
    tx.set_firmware_domain(FirmwareDomain.UNKNOWN, module=1)
    tx.set_firmware_domain(FirmwareDomain.CLINICAL)
    assert tx.firmware_domain(1) is FirmwareDomain.CLINICAL
