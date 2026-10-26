# SPDX-License-Identifier: Apache-2.0
"""Pause/Resume commands and PAUSED/TIMEOUT status on the real interface classes (SR-023, SR-024)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from openlifu_sdk.io.LIFUConfig import (
    OW_CONTROLLER,
    OW_CTRL_PAUSE_SWTRIG,
    OW_CTRL_RESUME_SWTRIG,
    OW_ERROR,
)
from openlifu_sdk.io.LIFUInterface import DeviceInterface, LIFUInterfaceStatus
from openlifu_sdk.io.exceptions import LIFUDeviceError, LIFUError, LIFUNoTriggerStatusError
from openlifu_sdk.io.LIFUTXDevice import LIFUTxDevice
from openlifu_sdk.io.uart import OWUart
from openlifu_sdk.ui.status_frame import parse_status_string


def _interface():
    interface = DeviceInterface.__new__(DeviceInterface)
    interface._test_mode = False
    interface.status = LIFUInterfaceStatus.STATUS_READY
    interface.txdevice = MagicMock()
    interface.hvcontroller = MagicMock()
    return interface


@pytest.mark.parametrize(
    ("method_name", "command"),
    [
        ("pause_trigger", OW_CTRL_PAUSE_SWTRIG),
        ("resume_trigger", OW_CTRL_RESUME_SWTRIG),
    ],
)
def test_tx_pause_resume_send_commands_and_raise_on_error(method_name, command):
    tx = LIFUTxDevice()
    uart = MagicMock(spec=OWUart)
    uart.desc = "TX"
    uart.is_connected = True
    uart.send_packet.return_value = SimpleNamespace(
        packet_type=0xE3, data=b"\x00", data_len=1, reserved=0,
        print_packet=MagicMock(),
    )
    tx._uart = uart

    assert getattr(tx, method_name)()
    assert uart.send_packet.call_args.kwargs["command"] == command
    assert uart.send_packet.call_args.kwargs["packet_type"] == OW_CONTROLLER

    uart.send_packet.return_value.packet_type = OW_ERROR
    with pytest.raises(LIFUDeviceError):
        getattr(tx, method_name)()


def test_pause_resume_do_not_touch_hv_or_reconfigure_trigger():
    interface = _interface()
    interface.status = LIFUInterfaceStatus.STATUS_RUNNING

    assert interface.pause_sonication()
    assert interface.txdevice.mock_calls == [call.pause_trigger()]
    assert interface.hvcontroller.mock_calls == []

    interface.txdevice.reset_mock()
    assert interface.resume_sonication()
    assert interface.txdevice.mock_calls == [call.resume_trigger()]
    assert interface.hvcontroller.mock_calls == []


def test_status_follows_firmware_not_the_command_sent():
    # SR-023 item 9: the pause lands at the next train boundary, so until
    # the firmware reports PAUSED the status stays RUNNING.
    interface = _interface()
    interface.status = LIFUInterfaceStatus.STATUS_RUNNING
    interface.txdevice.get_trigger_json.return_value = {"TriggerStatus": "RUNNING"}

    interface.pause_sonication()
    assert interface.get_status() == LIFUInterfaceStatus.STATUS_RUNNING

    interface.txdevice.get_trigger_json.return_value = {"TriggerStatus": "PAUSED"}
    assert interface.get_status() == LIFUInterfaceStatus.STATUS_PAUSED

    interface.resume_sonication()
    assert interface.get_status() == LIFUInterfaceStatus.STATUS_PAUSED

    interface.txdevice.get_trigger_json.return_value = {"TriggerStatus": "RUNNING"}
    assert interface.get_status() == LIFUInterfaceStatus.STATUS_RUNNING


@pytest.mark.parametrize("method", ["pause_sonication", "resume_sonication"])
def test_pause_resume_rejection_propagates(method):
    interface = _interface()
    tx_method = getattr(interface.txdevice, method.replace("_sonication", "_trigger"))
    tx_method.side_effect = LIFUDeviceError("rejected")

    with pytest.raises(LIFUDeviceError, match="rejected"):
        getattr(interface, method)()

    assert interface.hvcontroller.mock_calls == []


@pytest.mark.parametrize(
    ("trigger_status", "expected_status"),
    [
        ("PAUSED", LIFUInterfaceStatus.STATUS_PAUSED),
        ("TIMEOUT", LIFUInterfaceStatus.STATUS_TIMEOUT),
    ],
)
def test_is_running_maps_paused_and_timeout(trigger_status, expected_status):
    interface = _interface()
    interface.status = LIFUInterfaceStatus.STATUS_RUNNING
    interface.txdevice.get_trigger_json.return_value = {"TriggerStatus": trigger_status}

    assert interface.get_status() == expected_status


@pytest.mark.parametrize("status", ["PAUSED", "TIMEOUT"])
def test_status_frame_parser_retains_train_counts(status):
    parsed = parse_status_string(
        f"STATUS:{status},MODE:SEQUENCE,PULSE_TRAIN:[3/8],"
        "PULSE:[0/4],TEMP_TX:31.5,TEMP_AMBIENT:25.0"
    )

    assert parsed["status"] == status
    assert parsed["pulse_train_current"] == 3
    assert parsed["pulse_train_total"] == 8


@pytest.mark.parametrize(
    ("trigger_status", "running", "expected_status"),
    [
        ("RUNNING", True, LIFUInterfaceStatus.STATUS_RUNNING),
        ("PAUSED", False, LIFUInterfaceStatus.STATUS_PAUSED),
        ("TIMEOUT", False, LIFUInterfaceStatus.STATUS_TIMEOUT),
        ("STOPPED", False, LIFUInterfaceStatus.STATUS_FINISHED),
    ],
)
def test_is_running_sets_status_from_firmware(trigger_status, running, expected_status):
    interface = _interface()
    interface.status = LIFUInterfaceStatus.STATUS_RUNNING
    interface.txdevice.get_trigger_json.return_value = {"TriggerStatus": trigger_status}

    assert interface.is_running() is running
    assert interface.status == expected_status


def test_is_running_leaves_an_idle_ready_status_alone():
    interface = _interface()
    interface.txdevice.get_trigger_json.return_value = {"TriggerStatus": "STOPPED"}

    assert interface.is_running() is False
    assert interface.status == LIFUInterfaceStatus.STATUS_READY


@pytest.mark.parametrize("trigger_json", [{}, {"TriggerStatus": "BOGUS"}])
def test_is_running_raises_on_missing_or_unknown_status(trigger_json):
    interface = _interface()
    interface.txdevice.get_trigger_json.return_value = trigger_json

    with pytest.raises(LIFUNoTriggerStatusError):
        interface.is_running()


@pytest.mark.parametrize(
    "status",
    [LIFUInterfaceStatus.STATUS_READY, LIFUInterfaceStatus.STATUS_FINISHED,
     LIFUInterfaceStatus.STATUS_TIMEOUT],
)
def test_get_status_reads_the_firmware_only_while_running_or_paused(status):
    interface = _interface()
    interface.status = status

    assert interface.get_status() == status
    interface.txdevice.get_trigger_json.assert_not_called()


def test_get_status_raises_when_the_trigger_read_fails():
    interface = _interface()
    interface.status = LIFUInterfaceStatus.STATUS_PAUSED
    interface.txdevice.get_trigger_json.side_effect = LIFUDeviceError("no answer")

    with pytest.raises(LIFUDeviceError):
        interface.get_status()


def test_start_after_timeout_is_refused_before_hv_is_touched():
    # SR-024: a timed-out sequence is not restarted by Start; Stop first.
    interface = _interface()
    interface.txdevice.get_trigger_json.return_value = {"TriggerStatus": "TIMEOUT"}

    with pytest.raises(LIFUError, match="timeout"):
        interface.start_sonication()

    assert interface.hvcontroller.mock_calls == []
    interface.txdevice.start_trigger.assert_not_called()


@pytest.mark.parametrize("trigger_status", ["PAUSED", "TIMEOUT"])
def test_load_preset_while_a_sequence_is_held_is_refused_untouched(trigger_status):
    # The firmware refuses a preset load while paused; the refusal path
    # would otherwise unload the preset and turn HV off under the pause.
    interface = _interface()
    interface.txdevice.get_trigger_json.return_value = {"TriggerStatus": trigger_status}

    with pytest.raises(LIFUError, match=trigger_status.lower()):
        interface.load_preset(0, 1, 2, 400_000.0)

    assert interface.hvcontroller.mock_calls == []
    assert interface.txdevice.mock_calls == [call.get_trigger_json()]
    assert interface.status == LIFUInterfaceStatus.STATUS_READY


def test_get_status_follows_the_cache_when_idle():
    # Documented: with a cached READY the firmware is not asked, even if
    # it holds a paused sequence; Start and load_preset ask it directly.
    interface = _interface()
    interface.txdevice.get_trigger_json.return_value = {"TriggerStatus": "PAUSED"}

    assert interface.get_status() == LIFUInterfaceStatus.STATUS_READY
    with pytest.raises(LIFUError, match="paused"):
        interface.start_sonication()


def test_start_fails_without_hv_when_the_trigger_status_cannot_be_read():
    interface = _interface()
    interface.txdevice.get_trigger_json.side_effect = LIFUDeviceError("no answer")

    with pytest.raises(LIFUDeviceError):
        interface.start_sonication()

    assert interface.hvcontroller.mock_calls == []
    interface.txdevice.start_trigger.assert_not_called()


@pytest.mark.parametrize(
    "cached_status",
    # READY: the cache was reset (reconnect, load_preset) while the
    # firmware stayed paused; the firmware is asked, not the cache.
    [LIFUInterfaceStatus.STATUS_PAUSED, LIFUInterfaceStatus.STATUS_READY],
)
def test_start_while_paused_is_refused_before_hv_is_touched(cached_status):
    # The firmware refuses Start while paused; refusing it here keeps HV
    # on and the paused sequence resumable.
    interface = _interface()
    interface.status = cached_status
    interface.txdevice.get_trigger_json.return_value = {"TriggerStatus": "PAUSED"}

    with pytest.raises(LIFUError, match="paused"):
        interface.start_sonication()

    assert interface.hvcontroller.mock_calls == []
    interface.txdevice.start_trigger.assert_not_called()
