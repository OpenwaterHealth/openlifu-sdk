from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from openlifu_sdk.io.LIFUConfig import (
    OW_CTRL_PAUSE_SWTRIG,
    OW_CTRL_RESUME_SWTRIG,
    OW_ERROR,
)
from openlifu_sdk.io.LIFUInterface import DeviceInterface, LIFUInterfaceStatus
from openlifu_sdk.io.exceptions import LIFUDeviceError
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

    uart.send_packet.return_value.packet_type = OW_ERROR
    with pytest.raises(LIFUDeviceError):
        getattr(tx, method_name)()


def test_pause_resume_do_not_touch_hv_or_reconfigure_trigger():
    interface = _interface()
    interface.status = LIFUInterfaceStatus.STATUS_RUNNING

    assert interface.pause_sonication()
    assert interface.txdevice.pause_trigger.call_count == 1
    assert interface.hvcontroller.mock_calls == []

    assert interface.resume_sonication()
    assert interface.txdevice.resume_trigger.call_count == 1
    assert interface.txdevice.set_trigger.call_count == 0
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
