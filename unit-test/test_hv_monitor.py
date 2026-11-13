# SPDX-License-Identifier: Apache-2.0
"""SR-025: read the console's HV rail monitor, and simulate it (#118).

Wire contract: console-fw#43 "Host contract". ``OW_POWER_GET_HV``'s reply
``reserved`` carries the latched fault cause; ``OW_POWER_STATUS`` carries a
36-byte v1 detail payload (empty on firmware from before the monitor); HV on
is refused with 0xF0 (OW_HV_FAULT) while a fault is latched, and a select
(FDA) or set_voltage (RUO) clears the latch.
"""

from __future__ import annotations

import struct
import unittest
from unittest.mock import MagicMock

import pytest

from openlifu_sdk.io.exceptions import LIFUDeviceError, LIFUProtocolError
from openlifu_sdk.io.LIFUConfig import (
    HV_FAULT_ADC_FAIL,
    HV_FAULT_HVM_HIGH,
    HV_FAULT_HVM_LOW,
    HV_FAULT_HVP_HIGH,
    HV_FAULT_HVP_LOW,
    HV_FAULT_NAMES,
    HV_FAULT_NONE,
    LIFU_ERR_BAD_PAYLOAD_FORMAT,
    LIFU_ERR_BAD_PAYLOAD_LENGTH,
    OW_ERROR,
    OW_HV_FAULT,
    OW_POWER,
    OW_POWER_GET_HV,
    OW_POWER_HV_ON,
    OW_POWER_SET_HV,
    OW_POWER_STATUS,
    OW_RESP,
)
from openlifu_sdk.io.LIFUHVController import HVController, LIFUHVController
from openlifu_sdk.io.uart import OWUart


def _packet(data: bytes = b"", reserved: int = 0, packet_type: int = OW_RESP) -> MagicMock:
    pkt = MagicMock()
    pkt.packet_type = packet_type
    pkt.data = bytearray(data)
    pkt.data_len = len(data)
    pkt.reserved = reserved
    return pkt


def _mock_uart(component) -> MagicMock:
    uart = MagicMock(spec=OWUart)
    uart.desc = "HV"
    uart.demo_mode = False
    uart.asyncMode = False
    uart.is_connected = True
    component._uart = uart
    return uart


def _status(version=1, fault=HV_FAULT_HVP_HIGH, hv_on=0, flags=0x03, v_set=30.0,
            low=28.5, high=31.5, hvp=0.25, hvm=0.5, hvp_trip=32.0, hvm_trip=30.0,
            ms=1234) -> bytes:
    return struct.pack("<BBBB7fI", version, fault, hv_on, flags, v_set, low, high,
                       hvp, hvm, hvp_trip, hvm_trip, ms)


class TestConstants(unittest.TestCase):

    def test_contract_values(self):
        self.assertEqual(OW_HV_FAULT, 0xF0)
        self.assertEqual(
            HV_FAULT_NAMES,
            {0: "NONE", 1: "HVP_HIGH", 2: "HVP_LOW", 3: "HVM_HIGH", 4: "HVM_LOW", 5: "ADC_FAIL"})


class TestGetHvMonitor(unittest.TestCase):
    """get_hv_monitor(): one OW_POWER_GET_HV, the fault in reserved."""

    def test_fault_is_the_reply_reserved_byte(self):
        hv = HVController()
        uart = _mock_uart(hv)
        uart.send_packet.return_value = _packet(struct.pack("<f", 29.987), reserved=HV_FAULT_HVM_LOW)
        self.assertEqual(hv.get_hv_monitor(),
                         {"voltage": 29.99, "fault": HV_FAULT_HVM_LOW, "fault_name": "HVM_LOW"})
        sent = uart.send_packet.call_args.kwargs
        self.assertEqual((sent["packet_type"], sent["command"]), (OW_POWER, OW_POWER_GET_HV))
        uart.send_packet.assert_called_once()

    def test_no_fault(self):
        hv = HVController()
        _mock_uart(hv).send_packet.return_value = _packet(struct.pack("<f", 0.0))
        got = hv.get_hv_monitor()
        self.assertEqual((got["fault"], got["fault_name"]), (HV_FAULT_NONE, "NONE"))

    def test_unknown_cause_is_named_not_dropped(self):
        hv = HVController()
        _mock_uart(hv).send_packet.return_value = _packet(struct.pack("<f", 0.0), reserved=9)
        got = hv.get_hv_monitor()
        self.assertEqual((got["fault"], got["fault_name"]), (9, "UNKNOWN(9)"))

    def test_bad_length(self):
        hv = HVController()
        _mock_uart(hv).send_packet.return_value = _packet(b"\x00\x00")
        with self.assertRaises(LIFUProtocolError) as ctx:
            hv.get_hv_monitor()
        self.assertEqual(ctx.exception.code, LIFU_ERR_BAD_PAYLOAD_LENGTH)


class TestGetHvMonitorStatus(unittest.TestCase):
    """get_hv_monitor_status(): the 36-byte OW_POWER_STATUS v1 payload."""

    def test_good_payload(self):
        for component in (HVController(), LIFUHVController()):   # FDA's whitelist allows it
            with self.subTest(component=type(component).__name__):
                uart = _mock_uart(component)
                uart.send_packet.return_value = _packet(_status())
                got = component.get_hv_monitor_status()
                self.assertEqual(uart.send_packet.call_args.kwargs["command"], OW_POWER_STATUS)
                self.assertEqual(
                    {k: got[k] for k in ("version", "fault", "fault_name", "hv_on", "armed",
                                         "preset_window", "ms_since_trip")},
                    {"version": 1, "fault": HV_FAULT_HVP_HIGH, "fault_name": "HVP_HIGH",
                     "hv_on": False, "armed": True, "preset_window": True, "ms_since_trip": 1234})
                for key, want in (("v_set", 30.0), ("window_low", 28.5), ("window_high", 31.5),
                                  ("hvp", 0.25), ("hvm", 0.5), ("hvp_at_trip", 32.0),
                                  ("hvm_at_trip", 30.0)):
                    self.assertAlmostEqual(got[key], want, places=5, msg=key)

    def test_flag_bits_are_independent(self):
        hv = HVController()
        uart = _mock_uart(hv)
        for flags, armed, preset_window in ((0, False, False), (1, True, False), (2, False, True)):
            uart.send_packet.return_value = _packet(_status(flags=flags, hv_on=1, fault=0))
            got = hv.get_hv_monitor_status()
            self.assertEqual((got["armed"], got["preset_window"], got["hv_on"]),
                             (armed, preset_window, True))

    def test_fda_sends_status_with_reserved_zero_only(self):
        hv = HVController()
        uart = _mock_uart(hv)
        with self.assertRaisesRegex(ValueError, "only be read"):
            hv.send_checked(OW_POWER_STATUS, packet_type=OW_POWER, reserved=1)
        uart.send_packet.assert_not_called()

    def test_empty_payload_is_no_monitor(self):
        hv = HVController()
        _mock_uart(hv).send_packet.return_value = _packet(b"")
        self.assertIsNone(hv.get_hv_monitor_status())

    def test_short_and_long_payloads_raise(self):
        hv = HVController()
        uart = _mock_uart(hv)
        for data in (_status()[:35], _status() + b"\x00"):
            uart.send_packet.return_value = _packet(data)
            with self.assertRaises(LIFUProtocolError) as ctx:
                hv.get_hv_monitor_status()
            self.assertEqual(ctx.exception.code, LIFU_ERR_BAD_PAYLOAD_LENGTH)

    def test_unknown_version_raises_whatever_its_length(self):
        hv = HVController()
        uart = _mock_uart(hv)
        for data in (_status(version=2), _status(version=2) + b"\x00" * 8, b"\x02"):
            uart.send_packet.return_value = _packet(data)
            with self.assertRaises(LIFUProtocolError) as ctx:
                hv.get_hv_monitor_status()
            self.assertEqual(ctx.exception.code, LIFU_ERR_BAD_PAYLOAD_FORMAT)

    def test_a_fault_clears_the_cached_hv_on(self):
        for fault, still_on in ((HV_FAULT_NONE, True), (HV_FAULT_HVP_HIGH, False)):
            hv = HVController()
            uart = _mock_uart(hv)
            hv.is_hv_on = True
            uart.send_packet.return_value = _packet(_status(fault=fault, hv_on=int(still_on)))
            hv.get_hv_monitor_status()
            self.assertEqual(hv.is_hv_on, still_on)
            hv.is_hv_on = True
            uart.send_packet.return_value = _packet(struct.pack("<f", 0.0), reserved=fault)
            hv.get_hv_monitor()
            self.assertEqual(hv.is_hv_on, still_on)


class TestRuoSetVoltageAfterATrip(unittest.TestCase):
    """SET_HV clears the latch, so set_voltage must not re-send HV on
    over a trip the host has not read yet."""

    def test_a_latched_fault_refuses_the_set_and_keeps_the_latch(self):
        hv = LIFUHVController()
        uart = _mock_uart(hv)
        hv.is_hv_on = True
        hv.supply_voltage = 25.0
        uart.send_packet.return_value = _packet(struct.pack("<f", 0.0), reserved=HV_FAULT_HVP_HIGH)
        with self.assertRaises(LIFUDeviceError) as ctx:
            hv.set_voltage(30.0)
        self.assertEqual(ctx.exception.device_error_code, OW_HV_FAULT)
        self.assertIn("HVP_HIGH", str(ctx.exception))
        # Only the GET_HV went out: no SET_HV to clear the latch, no HV on.
        sent = [c.kwargs["command"] for c in uart.send_packet.call_args_list]
        self.assertEqual(sent, [OW_POWER_GET_HV])
        self.assertFalse(hv.is_hv_on)
        self.assertEqual(hv.supply_voltage, 25.0)
        # A set with HV now known off goes through (and, on the console, clears it).
        uart.send_packet.return_value = _packet()
        hv.set_voltage(30.0)
        sent = [c.kwargs["command"] for c in uart.send_packet.call_args_list]
        self.assertEqual(sent, [OW_POWER_GET_HV, OW_POWER_SET_HV])

    def test_reasserts_with_no_fault(self):
        hv = LIFUHVController()
        uart = _mock_uart(hv)
        hv.is_hv_on = True
        uart.send_packet.side_effect = [_packet(struct.pack("<f", 30.0)), _packet(), _packet()]
        hv.set_voltage(30.0)
        sent = [c.kwargs["command"] for c in uart.send_packet.call_args_list]
        self.assertEqual(sent, [OW_POWER_GET_HV, OW_POWER_SET_HV, OW_POWER_HV_ON])

    def test_hv_off_skips_the_fault_read(self):
        hv = LIFUHVController()
        uart = _mock_uart(hv)
        uart.send_packet.return_value = _packet()
        hv.set_voltage(30.0)
        sent = [c.kwargs["command"] for c in uart.send_packet.call_args_list]
        self.assertEqual(sent, [OW_POWER_SET_HV])


class TestTurnHvOnRefusal(unittest.TestCase):

    def test_0xf0_surfaces_as_device_error_with_the_code(self):
        for component in (HVController(), LIFUHVController()):
            with self.subTest(component=type(component).__name__):
                _mock_uart(component).send_packet.return_value = _packet(
                    b"HV fault latched", reserved=OW_HV_FAULT, packet_type=OW_ERROR)
                with self.assertRaises(LIFUDeviceError) as ctx:
                    component.turn_hv_on()
                self.assertEqual(ctx.exception.device_error_code, OW_HV_FAULT)
                self.assertIn(b"HV fault latched", ctx.exception.device_error_data)


# ---- simulator -----------------------------------------------------------------

CAUSES = (HV_FAULT_HVP_HIGH, HV_FAULT_HVP_LOW, HV_FAULT_HVM_HIGH, HV_FAULT_HVM_LOW, HV_FAULT_ADC_FAIL)


@pytest.fixture
def fda_hv():
    pytest.importorskip("openlifu_sdk.ui._qt")
    from openlifu_sdk.ui.simulated_interface import SimulatedHVController
    hv = SimulatedHVController()
    hv.select_preset(0, 0x12345678, 2720.0)   # the default preset: 40 V in 35-45 V
    hv.turn_hv_on()
    return hv


@pytest.fixture
def ruo_hv():
    pytest.importorskip("openlifu_sdk.ui._qt")
    from openlifu_sdk.ui.simulated_interface import SimulatedLIFUHVController
    hv = SimulatedLIFUHVController()
    hv.set_voltage(30.0)
    hv.turn_hv_on()
    return hv


def test_sim_has_the_real_parsers_keys(fda_hv):
    hv = HVController()
    uart = _mock_uart(hv)
    uart.send_packet.return_value = _packet(_status())
    real_status = hv.get_hv_monitor_status()
    uart.send_packet.return_value = _packet(struct.pack("<f", 0.0))
    real_monitor = hv.get_hv_monitor()
    assert fda_hv.get_hv_monitor_status().keys() == real_status.keys()
    assert fda_hv.get_hv_monitor().keys() == real_monitor.keys()


def test_sim_healthy_reads(fda_hv):
    assert fda_hv.get_hv_monitor() == {"voltage": 40.0, "fault": 0, "fault_name": "NONE"}
    status = fda_hv.get_hv_monitor_status()
    assert (status["version"], status["fault"], status["hv_on"], status["armed"],
            status["preset_window"]) == (1, 0, True, True, True)
    # 5 % of 40 V is 38.0-42.0 V, inside the record's 35-45 V.
    assert status["window_low"] == pytest.approx(38.0)
    assert status["window_high"] == pytest.approx(42.0)
    assert (status["hvp_at_trip"], status["hvm_at_trip"], status["ms_since_trip"]) == (0.0, 0.0, 0)


@pytest.mark.parametrize("cause", CAUSES)
def test_sim_inject_turns_hv_off_and_latches(fda_hv, cause):
    fda_hv.inject_hv_fault(cause)
    assert fda_hv.get_hv_status() is False
    assert fda_hv.get_hv_monitor() == {"voltage": 0.0, "fault": cause,
                                       "fault_name": HV_FAULT_NAMES[cause]}
    status = fda_hv.get_hv_monitor_status()
    assert (status["fault"], status["hv_on"], status["armed"]) == (cause, False, False)
    hvp, hvm = status["hvp_at_trip"], status["hvm_at_trip"]
    low, high = status["window_low"], status["window_high"]
    # The at-trip rail that names the cause is on the side of the window it names.
    expected_outside = {HV_FAULT_HVP_HIGH: hvp > high, HV_FAULT_HVP_LOW: hvp < low,
                        HV_FAULT_HVM_HIGH: hvm > high, HV_FAULT_HVM_LOW: hvm < low,
                        HV_FAULT_ADC_FAIL: (hvp, hvm) == (0.0, 0.0)}
    assert expected_outside[cause]


def test_sim_refuses_hv_on_while_latched_then_select_clears(fda_hv):
    fda_hv.inject_hv_fault(HV_FAULT_HVP_HIGH)
    with pytest.raises(LIFUDeviceError) as excinfo:
        fda_hv.turn_hv_on()
    assert excinfo.value.device_error_code == OW_HV_FAULT
    assert fda_hv.get_hv_status() is False
    # A refused select does not clear the latch; one that succeeds does.
    with pytest.raises(LIFUDeviceError):
        fda_hv.select_preset(0, 0xBAD, 2720.0)
    assert fda_hv.get_hv_monitor()["fault"] == HV_FAULT_HVP_HIGH
    fda_hv.select_preset(0, 0x12345678, 2720.0)
    assert fda_hv.get_hv_monitor()["fault"] == HV_FAULT_NONE
    assert fda_hv.get_hv_monitor_status()["ms_since_trip"] == 0
    fda_hv.turn_hv_on()
    assert fda_hv.get_hv_status() is True


def test_sim_hv_off_does_not_clear(fda_hv):
    fda_hv.inject_hv_fault(HV_FAULT_ADC_FAIL)
    fda_hv.turn_hv_off()
    assert fda_hv.get_hv_monitor()["fault"] == HV_FAULT_ADC_FAIL


def test_sim_deselect_clears(fda_hv):
    fda_hv.inject_hv_fault(HV_FAULT_HVM_HIGH)
    fda_hv.deselect_preset()
    assert fda_hv.get_hv_monitor()["fault"] == HV_FAULT_NONE


def test_sim_ruo_set_voltage_clears(ruo_hv):
    ruo_hv.inject_hv_fault(HV_FAULT_HVP_LOW)
    with pytest.raises(LIFUDeviceError) as excinfo:
        ruo_hv.turn_hv_on()
    assert excinfo.value.device_error_code == OW_HV_FAULT
    status = ruo_hv.get_hv_monitor_status()
    assert status["preset_window"] is False
    assert status["window_low"] == pytest.approx(28.5)
    ruo_hv.set_voltage(30.0)
    assert ruo_hv.get_hv_monitor()["fault"] == HV_FAULT_NONE
    ruo_hv.turn_hv_on()


def test_sim_ruo_select_does_not_clear(ruo_hv):
    """console-fw#43 names select / deselect as clearing on an FDA image
    and set_voltage on an RUO one; the simulator clears on no more."""
    ruo_hv.inject_hv_fault(HV_FAULT_HVM_LOW)
    ruo_hv.select_preset(0, 0x12345678, 2720.0)
    ruo_hv.deselect_preset()
    assert ruo_hv.get_hv_monitor()["fault"] == HV_FAULT_HVM_LOW


@pytest.mark.parametrize("cause", CAUSES)
def test_sim_inject_with_hv_off_keeps_the_setpoints_rails(fda_hv, cause):
    """A fault latched earlier: the at-trip rails are the setpoint's, so
    the rail the cause names is still outside the window and the other
    is inside it."""
    fda_hv.turn_hv_off()
    fda_hv.inject_hv_fault(cause)
    status = fda_hv.get_hv_monitor_status()
    low, high = status["window_low"], status["window_high"]
    rails = {"HVP": status["hvp_at_trip"], "HVM": status["hvm_at_trip"]}
    name = HV_FAULT_NAMES[cause]
    for rail, value in rails.items():
        if name == "ADC_FAIL":
            assert value == 0.0
        elif name.startswith(rail):
            assert (value > high) if name.endswith("HIGH") else (value < low)
        else:
            assert low <= value <= high


def test_sim_inject_refuses_with_no_setpoint():
    pytest.importorskip("openlifu_sdk.ui._qt")
    from openlifu_sdk.ui.simulated_interface import SimulatedLIFUHVController
    hv = SimulatedLIFUHVController()
    with pytest.raises(ValueError, match="no HV setpoint"):
        hv.inject_hv_fault(HV_FAULT_HVP_HIGH)
    assert hv.get_hv_monitor()["fault"] == HV_FAULT_NONE


@pytest.mark.parametrize("cause", (HV_FAULT_NONE, 6, -1))
def test_sim_inject_refuses_a_non_cause(fda_hv, cause):
    with pytest.raises(ValueError):
        fda_hv.inject_hv_fault(cause)
    assert fda_hv.get_hv_status() is True
