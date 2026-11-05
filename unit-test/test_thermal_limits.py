# SPDX-License-Identifier: Apache-2.0
"""Thermal limits (start_C / shutoff_C) as their own state, apart from the load
(transmitter-fw #59).

The wire format of OW_CTRL_GET / SET / CLEAR_THERMAL_LIMITS against a mocked
UART, the FDA whitelist (TxDevice may read the limits, never set or clear
them), and the simulator's copy of the firmware rules: a load arms the
preset's limits and an unload leaves them; the host may set or clear them
while nothing runs, which unloads a loaded preset; armed limits gate start.
"""

from __future__ import annotations

import struct
from unittest.mock import MagicMock

import pytest

from openlifu_sdk.io.exceptions import LIFUDeviceError, LIFUProtocolError
from openlifu_sdk.io.LIFUConfig import (
    OW_CONTROLLER,
    OW_CTRL_CLEAR_THERMAL_LIMITS,
    OW_CTRL_GET_THERMAL_LIMITS,
    OW_CTRL_SET_THERMAL_LIMITS,
    OW_PRESET_BUSY,
    OW_RESP,
    OW_TEMP_TOO_HIGH,
    TX_FDA_PACKETS,
)
from openlifu_sdk.io.LIFUTXDevice import LIFUTxDevice, TxDevice
from openlifu_sdk.io.uart import OWUart


def _reply(source: int, index: int, start_x10: int, shutoff_x10: int):
    pkt = MagicMock()
    pkt.packet_type = OW_RESP
    pkt.data = bytearray(struct.pack("<BBhh", source, index, start_x10, shutoff_x10))
    pkt.data_len = len(pkt.data)
    pkt.reserved = 0
    return pkt


@pytest.fixture
def tx():
    dev = LIFUTxDevice()
    uart = MagicMock(spec=OWUart)
    uart.desc = "TX"
    uart.demo_mode = False
    uart.asyncMode = False
    uart.is_connected = True
    dev._uart = uart
    return dev


def _sent(tx):
    kwargs = tx._uart.send_packet.call_args.kwargs
    return kwargs["packet_type"], kwargs["command"], kwargs.get("data")


# ---- wire format -----------------------------------------------------------


def test_get_decodes_preset_limits(tx):
    tx._uart.send_packet.return_value = _reply(1, 3, 400, 430)
    assert tx.get_thermal_limits() == {
        "source": "preset", "preset_index": 3, "start_c": 40.0, "shutoff_c": 43.0,
    }
    assert _sent(tx)[:2] == (OW_CONTROLLER, OW_CTRL_GET_THERMAL_LIMITS)


def test_get_none_armed_reports_no_values(tx):
    tx._uart.send_packet.return_value = _reply(0, 0xFF, 0, 0)
    assert tx.get_thermal_limits() == {
        "source": "none", "preset_index": None, "start_c": None, "shutoff_c": None,
    }


def test_get_refuses_a_short_or_unknown_reply(tx):
    short = _reply(1, 0, 400, 430)
    short.data = short.data[:5]
    short.data_len = 5
    tx._uart.send_packet.return_value = short
    with pytest.raises(LIFUProtocolError):
        tx.get_thermal_limits()
    tx._uart.send_packet.return_value = _reply(7, 0xFF, 0, 0)
    with pytest.raises(LIFUProtocolError):
        tx.get_thermal_limits()


def test_set_sends_tenths_and_returns_what_is_armed(tx):
    tx._uart.send_packet.return_value = _reply(2, 0xFF, -55, 431)
    got = tx.set_thermal_limits(-5.5, 43.1)
    packet_type, command, data = _sent(tx)
    assert (packet_type, command) == (OW_CONTROLLER, OW_CTRL_SET_THERMAL_LIMITS)
    assert struct.unpack("<hh", bytes(data)) == (-55, 431)
    assert got == {"source": "host", "preset_index": None, "start_c": -5.5, "shutoff_c": 43.1}


@pytest.mark.parametrize("start_c, shutoff_c", [(43.0, 43.0), (45.0, 43.0), (40.05, 43.0), (40.0, 4000.0)])
def test_set_refuses_bad_limits_without_sending(tx, start_c, shutoff_c):
    with pytest.raises(ValueError):
        tx.set_thermal_limits(start_c, shutoff_c)
    tx._uart.send_packet.assert_not_called()


def test_clear(tx):
    tx._uart.send_packet.return_value = _reply(0, 0xFF, 0, 0)
    assert tx.clear_thermal_limits()["source"] == "none"
    assert _sent(tx)[:2] == (OW_CONTROLLER, OW_CTRL_CLEAR_THERMAL_LIMITS)


# ---- FDA whitelist -----------------------------------------------------------


def test_fda_txdevice_may_read_but_not_change_the_limits():
    allowed = TX_FDA_PACKETS[OW_CONTROLLER]
    assert OW_CTRL_GET_THERMAL_LIMITS in allowed
    assert OW_CTRL_SET_THERMAL_LIMITS not in allowed
    assert OW_CTRL_CLEAR_THERMAL_LIMITS not in allowed
    assert hasattr(TxDevice, "get_thermal_limits")
    assert not hasattr(TxDevice, "set_thermal_limits")
    assert not hasattr(TxDevice, "clear_thermal_limits")


# ---- simulator -----------------------------------------------------------------


@pytest.fixture
def sim():
    pytest.importorskip("openlifu_sdk.ui._qt")
    from sr003_presets import flash_entry

    from openlifu_sdk.ui.simulated_interface import SimulatedLIFUTxDevice

    mc, regs = flash_entry(start_C=40.0, shutoff_C=43.0)
    return SimulatedLIFUTxDevice(preset_flash=[(mc, regs)]), mc, regs


def _set_temp(dev, celsius):
    dev._modules[0].temp_c = celsius
    dev._modules[0].read_temp = lambda: celsius


def test_sim_load_arms_and_unload_keeps_the_limits(sim):
    dev, mc, regs = sim
    assert dev.get_thermal_limits()["source"] == "none"
    dev.load_preset(0, regs, settings_crc=mc["settings_crc"])
    armed = dev.get_thermal_limits()
    assert armed == {"source": "preset", "preset_index": 0, "start_c": 40.0, "shutoff_c": 43.0}
    dev.unload_preset()
    assert dev.get_thermal_limits() == armed

    # Still gating with nothing loaded.
    _set_temp(dev, 41.0)
    with pytest.raises(LIFUDeviceError) as e:
        dev.start_trigger()
    assert e.value.device_error_code == OW_TEMP_TOO_HIGH


def test_sim_host_set_and_clear(sim):
    dev, _, _ = sim
    _set_temp(dev, 35.0)
    assert dev.set_thermal_limits(30.0, 45.0)["source"] == "host"
    with pytest.raises(LIFUDeviceError) as e:
        dev.start_trigger()
    assert e.value.device_error_code == OW_TEMP_TOO_HIGH

    assert dev.clear_thermal_limits()["source"] == "none"
    dev.start_trigger()
    assert dev.get_trigger()["trigger_status"] == "RUNNING"
    dev.stop_trigger()


def test_sim_set_and_clear_unload_a_loaded_preset(sim):
    dev, mc, regs = sim
    dev.load_preset(0, regs, settings_crc=mc["settings_crc"])
    assert dev.get_loaded_preset()[0] == 0
    armed = dev.set_thermal_limits(30.0, 35.0)
    assert (armed["source"], armed["start_c"], armed["shutoff_c"]) == ("host", 30.0, 35.0)
    assert dev.get_loaded_preset() == (None, None)

    dev.load_preset(0, regs, settings_crc=mc["settings_crc"])
    assert dev.get_thermal_limits()["source"] == "preset"
    assert dev.clear_thermal_limits()["source"] == "none"
    assert dev.get_loaded_preset() == (None, None)


def test_sim_set_and_clear_refused_while_running(sim):
    dev, mc, regs = sim
    assert dev.set_thermal_limits(30.0, 35.0)["source"] == "host"
    armed = dev.get_thermal_limits()
    _set_temp(dev, 25.0)
    dev.start_trigger()
    for call in (lambda: dev.set_thermal_limits(31.0, 36.0), dev.clear_thermal_limits):
        with pytest.raises(LIFUDeviceError) as e:
            call()
        assert e.value.device_error_code == OW_PRESET_BUSY
    assert dev.get_thermal_limits() == armed
    dev.stop_trigger()
    assert dev.clear_thermal_limits()["source"] == "none"
