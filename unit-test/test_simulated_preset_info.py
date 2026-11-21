# SPDX-License-Identifier: Apache-2.0
"""The simulated boards' preset package headers (get_preset_info /
set_preset_package), as the hardware reports them."""

import pytest

pytest.importorskip("openlifu_sdk.ui._qt")

from openlifu_sdk.io.component import FirmwareDomain  # noqa: E402  # reason: after importorskip
from openlifu_sdk.io.exceptions import LIFUDeviceError  # noqa: E402  # reason: after importorskip
from openlifu_sdk.ui.simulated_interface import (  # noqa: E402  # reason: after importorskip
    SimulatedHVController,
    SimulatedTxDevice,
)

SHA = "ab" * 20

def _tx(n_presets=2, num_modules=2):
    flash = [({"id": f"p{i}", "settings_crc": i}, i) for i in range(n_presets)]
    return SimulatedTxDevice(num_modules=num_modules, preset_flash=flash)


def test_tx_slot_i_holds_preset_i_on_every_module():
    tx = _tx()
    assert tx.get_preset_info(1, module=1).context == "research"
    assert tx.get_preset_info(1, module=1).domain("transmitter") is FirmwareDomain.RESEARCH
    assert tx.get_preset_info(2) is None          # past the presets: empty
    with pytest.raises(LIFUDeviceError):
        tx.get_preset_info(10)                    # no such slot
    with pytest.raises(LIFUDeviceError):
        tx.get_preset_info(0, module=2)           # no such module


def test_tx_set_every_slot_then_one():
    tx = _tx()
    tx.set_preset_package(context="clinical", diathermy_sha=SHA, domain="clinical")
    info = tx.get_preset_info(0, module=1)
    assert (info.context, info.diathermy_sha) == ("clinical", SHA)
    assert info.domain("transmitter") is FirmwareDomain.CLINICAL
    tx.set_preset_package(domain=FirmwareDomain.UNKNOWN, slot=1, module=1)
    assert tx.get_preset_info(1, module=1).domain("transmitter") is FirmwareDomain.UNKNOWN
    assert tx.get_preset_info(1, module=1).context == "clinical"   # kept
    assert tx.get_preset_info(1, module=0).domain("transmitter") is FirmwareDomain.CLINICAL
    tx.set_preset_package(domain="clinical")                      # every slot again
    assert tx.get_preset_info(1, module=1).domain("transmitter") is FirmwareDomain.CLINICAL


def test_hv_reports_its_page_header():
    hv = SimulatedHVController()
    hv.set_preset_package(context="clinical", diathermy_sha=SHA, domain="clinical")
    info = hv.get_preset_info()
    assert (info.context, info.diathermy_sha) == ("clinical", SHA)
    assert info.domain("console") is FirmwareDomain.CLINICAL
    assert info.domain("transmitter") is FirmwareDomain.UNKNOWN
    hv.configure_presets([])
    assert hv.get_preset_info() is None


def test_simulated_signed_by_matches_the_trusted_key():
    pytest.importorskip("cryptography")
    from openlifu_sdk.io import LIFUCrypto

    hv = SimulatedHVController()
    hv.set_preset_package(domain="clinical")
    info = hv.get_preset_info()
    assert info.signed_by(LIFUCrypto.trusted_public_key("console", "clinical"))
    assert not info.signed_by(LIFUCrypto.trusted_public_key("console", "research"))
