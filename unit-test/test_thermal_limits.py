# SPDX-License-Identifier: Apache-2.0
"""Thermal limits (start_C / shutoff_C) and the treatment timeout as their own
state, apart from the load (transmitter-fw #59).

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
    OW_ERROR,
    OW_FDA_REFUSED,
    OW_PRESET_BUSY,
    OW_RESP,
    OW_TEMP_TOO_HIGH,
    TX_FDA_PACKETS,
)
from openlifu_sdk.io.LIFUTXDevice import LIFUTxDevice, TxDevice
from openlifu_sdk.io.uart import OWUart


def _reply(source: int, index: int, start_x10: int, shutoff_x10: int, timeout_s: int = 0):
    pkt = MagicMock()
    pkt.packet_type = OW_RESP
    pkt.data = bytearray(struct.pack("<BBhhH", source, index, start_x10, shutoff_x10, timeout_s))
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
    # Only the master runs the trigger: every limits command addresses module 0.
    assert kwargs.get("addr", 0) == 0
    return kwargs["packet_type"], kwargs["command"], kwargs.get("data")


# ---- wire format -----------------------------------------------------------


def test_get_decodes_preset_limits(tx):
    tx._uart.send_packet.return_value = _reply(1, 3, 400, 430, 1800)
    assert tx.get_thermal_limits() == {
        "source": "preset", "preset_index": 3, "start_c": 40.0, "shutoff_c": 43.0,
        "treatment_timeout_s": 1800,
    }
    assert _sent(tx)[:2] == (OW_CONTROLLER, OW_CTRL_GET_THERMAL_LIMITS)


def test_get_none_armed_reports_no_values(tx):
    tx._uart.send_packet.return_value = _reply(0, 0xFF, 0, 0)
    assert tx.get_thermal_limits() == {
        "source": "none", "preset_index": None, "start_c": None, "shutoff_c": None,
        "treatment_timeout_s": 0,
    }


def test_get_refuses_a_short_or_unknown_reply(tx):
    short = _reply(1, 0, 400, 430)
    # The 6-byte reply of a pre-timeout build is short too.
    short.data = short.data[:6]
    short.data_len = 6
    tx._uart.send_packet.return_value = short
    with pytest.raises(LIFUProtocolError):
        tx.get_thermal_limits()
    tx._uart.send_packet.return_value = _reply(7, 0xFF, 0, 0)
    with pytest.raises(LIFUProtocolError):
        tx.get_thermal_limits()


def test_set_sends_tenths_and_returns_what_is_armed(tx):
    tx._uart.send_packet.return_value = _reply(2, 0xFF, -55, 431, 65535)
    got = tx.set_thermal_limits(-5.5, 43.1, 65535)
    packet_type, command, data = _sent(tx)
    assert (packet_type, command) == (OW_CONTROLLER, OW_CTRL_SET_THERMAL_LIMITS)
    assert struct.unpack("<hhH", bytes(data)) == (-55, 431, 65535)
    assert got == {"source": "host", "preset_index": None, "start_c": -5.5, "shutoff_c": 43.1,
                   "treatment_timeout_s": 65535}


def test_set_timeout_0_disables_it(tx):
    tx._uart.send_packet.return_value = _reply(2, 0xFF, 400, 430, 0)
    assert tx.set_thermal_limits(40.0, 43.0, 0)["treatment_timeout_s"] == 0
    assert struct.unpack("<hhH", bytes(_sent(tx)[2])) == (400, 430, 0)


@pytest.mark.parametrize("start_c, shutoff_c, timeout_s", [
    (43.0, 43.0, 60), (45.0, 43.0, 60), (40.05, 43.0, 60), (40.0, 4000.0, 60),
    (40.0, 43.0, -1), (40.0, 43.0, 65536), (40.0, 43.0, 1.5), (40.0, 43.0, True), (40.0, 43.0, None),
])
def test_set_refuses_bad_limits_without_sending(tx, start_c, shutoff_c, timeout_s):
    with pytest.raises(ValueError):
        tx.set_thermal_limits(start_c, shutoff_c, timeout_s)
    tx._uart.send_packet.assert_not_called()


def test_set_has_no_default_timeout(tx):
    with pytest.raises(TypeError):
        # pylint: disable-next=no-value-for-parameter  # reason: leaving the timeout out is the case under test
        tx.set_thermal_limits(40.0, 43.0)
    tx._uart.send_packet.assert_not_called()


@pytest.mark.parametrize("source, index, start_x10, shutoff_x10, timeout_s", [
    (0, 0xFF, 0, 0, 1800),      # none armed, yet a timeout
    (0, 0xFF, 400, 430, 0),     # none armed, yet limits
    (0, 3, 0, 0, 0),            # none armed, yet a preset index
    (1, 0xFF, 400, 430, 1800),  # a preset's, with no index
    (1, 3, 400, 430, 0),        # a preset's, with no timeout
    (2, 3, 400, 430, 0),        # a host's, with a preset index
    (2, 0xFF, 430, 430, 0),     # start_C not below shutoff_C
])
def test_get_refuses_a_reply_that_breaks_the_firmware_rules(tx, source, index, start_x10, shutoff_x10,
                                                            timeout_s):
    tx._uart.send_packet.return_value = _reply(source, index, start_x10, shutoff_x10, timeout_s)
    with pytest.raises(LIFUProtocolError):
        tx.get_thermal_limits()


def test_get_refuses_an_overlong_reply(tx):
    long = _reply(2, 0xFF, 400, 430, 60)
    long.data = long.data + bytearray(2)
    long.data_len = len(long.data)
    tx._uart.send_packet.return_value = long
    with pytest.raises(LIFUProtocolError):
        tx.get_thermal_limits()


@pytest.mark.parametrize("reply", [
    (2, 0xFF, 400, 431, 60),   # shutoff_C not what was asked
    (2, 0xFF, 400, 430, 0),    # the timeout dropped
    (1, 0, 400, 430, 60),      # a preset's, not the host's
])
def test_set_refuses_a_reply_that_is_not_what_was_asked(tx, reply):
    tx._uart.send_packet.return_value = _reply(*reply)
    with pytest.raises(LIFUProtocolError):
        tx.set_thermal_limits(40.0, 43.0, 60)


def test_clear_refuses_a_reply_with_limits_still_armed(tx):
    tx._uart.send_packet.return_value = _reply(2, 0xFF, 400, 430, 60)
    with pytest.raises(LIFUProtocolError):
        tx.clear_thermal_limits()


def test_get_refuses_data_shorter_than_its_length(tx):
    # data_len says 8, the bytes are fewer: a protocol error, not struct.error.
    reply = _reply(2, 0xFF, 400, 430, 60)
    reply.data = reply.data[:5]
    tx._uart.send_packet.return_value = reply
    with pytest.raises(LIFUProtocolError):
        tx.get_thermal_limits()


@pytest.mark.parametrize("code", [OW_PRESET_BUSY, OW_FDA_REFUSED])
def test_set_and_clear_surface_the_firmware_refusal(tx, code):
    refused = _reply(0, 0xFF, 0, 0)
    refused.packet_type = OW_ERROR
    refused.reserved = code
    refused.data = bytearray()
    refused.data_len = 0
    tx._uart.send_packet.return_value = refused
    for call in (lambda: tx.set_thermal_limits(40.0, 43.0, 60), tx.clear_thermal_limits):
        with pytest.raises(LIFUDeviceError) as e:
            call()
        assert e.value.device_error_code == code


def test_clear(tx):
    tx._uart.send_packet.return_value = _reply(0, 0xFF, 0, 0)
    assert tx.clear_thermal_limits()["source"] == "none"
    assert _sent(tx)[:2] == (OW_CONTROLLER, OW_CTRL_CLEAR_THERMAL_LIMITS)


# ---- FDA whitelist -----------------------------------------------------------


def test_fda_txdevice_reads_the_limits_through_its_whitelist(tx):
    # The FDA class itself, end to end through ALLOWED_PACKETS and the mocked UART.
    fda = TxDevice()
    fda._uart = tx._uart
    fda._uart.send_packet.return_value = _reply(1, 2, 400, 430, 1800)
    assert fda.get_thermal_limits() == {
        "source": "preset", "preset_index": 2, "start_c": 40.0, "shutoff_c": 43.0,
        "treatment_timeout_s": 1800,
    }
    assert _sent(fda)[:2] == (OW_CONTROLLER, OW_CTRL_GET_THERMAL_LIMITS)


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

    mc, regs = flash_entry(start_C=40.0, shutoff_C=43.0, treatment_timeout_s=1800)
    return SimulatedLIFUTxDevice(preset_flash=[(mc, regs)]), mc, regs


def _set_temp(dev, celsius):
    dev._modules[0].temp_c = celsius
    dev._modules[0].read_temp = lambda: celsius


def test_sim_load_arms_and_unload_keeps_the_limits(sim):
    dev, mc, regs = sim
    assert dev.get_thermal_limits()["source"] == "none"
    dev.load_preset(0, regs, settings_crc=mc["settings_crc"])
    armed = dev.get_thermal_limits()
    assert armed == {"source": "preset", "preset_index": 0, "start_c": 40.0, "shutoff_c": 43.0,
                     "treatment_timeout_s": 1800}
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
    armed = dev.set_thermal_limits(30.0, 45.0, 600)
    assert (armed["source"], armed["treatment_timeout_s"]) == ("host", 600)
    with pytest.raises(LIFUDeviceError) as e:
        dev.start_trigger()
    assert e.value.device_error_code == OW_TEMP_TOO_HIGH
    before = dev.set_thermal_limits(30.0, 45.0, 0)
    assert before["treatment_timeout_s"] == 0
    for bad in ((31.0, 46.0, -1), (46.0, 45.0, 600), (31.05, 46.0, 600)):
        with pytest.raises(ValueError):
            dev.set_thermal_limits(*bad)
        assert dev.get_thermal_limits() == before, "a refused set changes nothing"

    cleared = dev.clear_thermal_limits()
    assert (cleared["source"], cleared["treatment_timeout_s"]) == ("none", 0)
    dev.start_trigger()
    assert dev.get_trigger()["trigger_status"] == "RUNNING"
    dev.stop_trigger()


def test_sim_set_and_clear_unload_a_loaded_preset(sim):
    dev, mc, regs = sim
    dev.load_preset(0, regs, settings_crc=mc["settings_crc"])
    assert dev.get_loaded_preset()[0] == 0
    armed = dev.set_thermal_limits(30.0, 35.0, 0)
    assert (armed["source"], armed["start_c"], armed["shutoff_c"]) == ("host", 30.0, 35.0)
    assert armed["treatment_timeout_s"] == 0, "the preset's timeout goes with its limits"
    assert dev.get_loaded_preset() == (None, None)

    dev.load_preset(0, regs, settings_crc=mc["settings_crc"])
    assert dev.get_thermal_limits()["source"] == "preset"
    assert dev.clear_thermal_limits()["source"] == "none"
    assert dev.get_loaded_preset() == (None, None)


@pytest.mark.parametrize("paused", [False, True], ids=["running", "paused"])
def test_sim_set_and_clear_refused_while_running(sim, paused):
    # A paused sequence resumes on its load and limits, so it counts as
    # running here (openlifu-transmitter-fw#60).
    dev, mc, regs = sim
    assert dev.set_thermal_limits(30.0, 35.0, 600)["source"] == "host"
    armed = dev.get_thermal_limits()
    _set_temp(dev, 25.0)
    dev.start_trigger()
    if paused:
        dev.pause_trigger()
    for call in (lambda: dev.set_thermal_limits(31.0, 36.0, 600),
                 lambda: dev.set_thermal_limits(30.0, 35.0, 0), dev.clear_thermal_limits):
        with pytest.raises(LIFUDeviceError) as e:
            call()
        assert e.value.device_error_code == OW_PRESET_BUSY
    assert dev.get_thermal_limits() == armed
    dev.stop_trigger()
    assert dev.clear_thermal_limits()["source"] == "none"


def test_sim_resume_held_to_the_armed_shutoff(sim):
    # The armed shutoff_C, not the preset's: it outlasts the load.
    dev, mc, regs = sim
    dev.set_thermal_limits(30.0, 35.0, 600)
    _set_temp(dev, 25.0)
    dev.start_trigger()
    dev.pause_trigger()
    # A paused sequence is resumed or stopped, not started again.
    with pytest.raises(LIFUDeviceError):
        dev.start_trigger()
    _set_temp(dev, 36.0)
    with pytest.raises(LIFUDeviceError) as e:
        dev.resume_trigger()
    assert e.value.device_error_code == OW_TEMP_TOO_HIGH
    _set_temp(dev, 34.0)
    assert dev.resume_trigger()
    dev.stop_trigger()


def test_sim_invalid_set_unloads_nothing(sim):
    # Refused before anything is sent, as on the real device, so the preset stays.
    dev, mc, regs = sim
    dev.load_preset(0, regs, settings_crc=mc["settings_crc"])
    armed = dev.get_thermal_limits()
    for bad in ((46.0, 45.0, 600), (31.0, 46.0, -1)):
        with pytest.raises(ValueError):
            dev.set_thermal_limits(*bad)
    assert dev.get_loaded_preset()[0] == 0
    assert dev.get_thermal_limits() == armed


def test_sim_refused_set_unloads_nothing(sim):
    # As the firmware: OW_PRESET_BUSY is checked before the unload.
    dev, mc, regs = sim
    dev.load_preset(0, regs, settings_crc=mc["settings_crc"])
    _set_temp(dev, 25.0)
    dev.start_trigger()
    with pytest.raises(LIFUDeviceError) as e:
        dev.set_thermal_limits(30.0, 35.0, 0)
    assert e.value.device_error_code == OW_PRESET_BUSY
    assert dev.get_loaded_preset()[0] == 0
    assert dev.get_thermal_limits()["source"] == "preset"
    dev.stop_trigger()


def test_sim_a_slave_load_arms_nothing():
    # Only the master's limits act, so only its load arms them.
    pytest.importorskip("openlifu_sdk.ui._qt")
    from sr003_presets import two_module_flash_entry

    from openlifu_sdk.ui.simulated_interface import SimulatedLIFUTxDevice

    mc, regs = two_module_flash_entry(start_C=40.0, shutoff_C=43.0, treatment_timeout_s=1800)
    dev = SimulatedLIFUTxDevice(num_modules=2, preset_flash=[(mc, regs)])
    dev.load_preset(0, regs[1], module=1, settings_crc=mc["settings_crc"])
    assert dev.get_thermal_limits()["source"] == "none"
    dev.load_preset(0, regs[0], module=0, settings_crc=mc["settings_crc"])
    assert dev.get_thermal_limits()["source"] == "preset"


def test_sim_soft_reset_clears_the_limits(sim):
    dev, mc, regs = sim
    dev.set_thermal_limits(30.0, 35.0, 600)
    dev.soft_reset(module=1)
    assert dev.get_thermal_limits()["source"] == "host", "a slave's reset leaves the master's"
    dev.soft_reset()
    assert dev.get_thermal_limits() == {"source": "none", "preset_index": None, "start_c": None,
                                        "shutoff_c": None, "treatment_timeout_s": 0}


@pytest.mark.parametrize("field, value", [("treatment_timeout_s", 0), ("treatment_timeout_s", None),
                                          ("start_C", None), ("shutoff_C", None)])
def test_sim_load_refuses_a_preset_with_a_disabled_gate(field, value):
    # The firmware refuses such a blob, so a load never arms a disabled gate.
    pytest.importorskip("openlifu_sdk.ui._qt")
    from sr003_presets import flash_entry

    from openlifu_sdk.ui.simulated_interface import SimulatedLIFUTxDevice

    mc, regs = flash_entry(start_C=40.0, shutoff_C=43.0, treatment_timeout_s=1800)
    mc = dict(mc, **{field: value})
    dev = SimulatedLIFUTxDevice(preset_flash=[(mc, regs)])
    with pytest.raises(LIFUDeviceError):
        dev.load_preset(0, regs, settings_crc=mc["settings_crc"])
    assert dev.get_loaded_preset() == (None, None)
    assert dev.get_thermal_limits()["source"] == "none"


def test_fda_sim_resume_held_to_the_armed_shutoff_after_an_unload():
    # Unloaded while paused, the FDA sim still holds Resume to the armed shutoff_C.
    pytest.importorskip("openlifu_sdk.ui._qt")
    from sr003_presets import flash_entry

    from openlifu_sdk.ui.simulated_interface import SimulatedTxDevice

    mc, regs = flash_entry(start_C=40.0, shutoff_C=43.0, treatment_timeout_s=1800)
    dev = SimulatedTxDevice(preset_flash=[(mc, regs)])
    dev.load_preset(0, regs, settings_crc=mc["settings_crc"])
    _set_temp(dev, 35.0)
    dev.start_trigger()
    dev.pause_trigger()
    dev.unload_preset()
    assert dev.get_thermal_limits()["shutoff_c"] == 43.0
    _set_temp(dev, 43.0)
    with pytest.raises(LIFUDeviceError) as e:
        dev.resume_trigger()
    assert e.value.device_error_code == OW_TEMP_TOO_HIGH
    _set_temp(dev, 42.9)
    assert dev.resume_trigger()
    dev.stop_trigger()
