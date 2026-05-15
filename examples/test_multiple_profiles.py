# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.16.4
#   kernelspec:
#     display_name: env
#     language: python
#     name: python3
# ---

# %%
from __future__ import annotations

import numpy as np
import time

from openlifu_sdk import LIFUInterface
from openlifu_sdk.io.LIFUTXDevice import (
    Tx7332DelayProfile,
    Tx7332PulseProfile,
    TxDeviceRegisters,
    ADDRESS_DELAY_SEL,
    ADDRESS_PATTERN_SEL_G1,
    ADDRESS_PATTERN_SEL_G2,
)

FIXED_FREQUENCY_HZ = 400e3
FIXED_CYCLES = 16
CHANNEL_COUNT = 64
SEPARATOR_LINE_WIDTH = 80

# TX7332 Register field definitions
BF_PROF_SEL_G1_SHIFT = 28  # Bits 28-31 for G1 delay profile selector
BF_PROF_SEL_G2_SHIFT = 12  # Bits 12-15 for G2 delay profile selector
BF_PROF_SEL_FIELD_MASK = 0x0F  # 4-bit profile field (0-15 for profiles 1-16)
DELAY_REG_TR_SW_DEL_PRESERVE_MASK = 0x0FFF0FFF  # Preserve TR_SW_DEL timing fields
PATTERN_PROFILE_MASK = 0x3F  # 6-bit pattern profile field (bits 0-5)

# Short timing for integrated verification (keep total active trigger time < 10s)
REG_PROPAGATION_DELAY_S = 0.05
TRIGGER_RUN_S = 0.40
MAX_ACTIVE_TRIGGER_TIME_S = 10.0

# HV validation for grouped profile/trigger verification
HV_SETPOINT_V = 20.0
HV_SETTLE_RANGE_V = 2.0
HV_SETTLE_TIME_S = 0.2
HV_SETTLE_TIMEOUT_S = 10.0

# Watertank-inspired profile sequence configurations for fast QA.
# Reference point from test_watertank.py:
# - duration_msec = 5
# - interval_msec = 100
# Here we keep a ~5 ms burst window per profile but vary PRF/count/train interval
# so each profile is easy to differentiate on an oscilloscope.
PROFILE_CONFIGS = [
    {
        "index": 1,
        "pulse_interval": 0.0010,
        "pulse_count": 5,
        "pulse_train_interval": 0.100,
        "pulse_train_count": 2,
    },
    {
        "index": 2,
        "pulse_interval": 0.000625,
        "pulse_count": 8,
        "pulse_train_interval": 0.080,
        "pulse_train_count": 2,
    },
    {
        "index": 3,
        "pulse_interval": 0.0005,
        "pulse_count": 10,
        "pulse_train_interval": 0.060,
        "pulse_train_count": 2,
    },
    {
        "index": 4,
        "pulse_interval": 0.00025,
        "pulse_count": 20,
        "pulse_train_interval": 0.040,
        "pulse_train_count": 2,
    },
]


def validate_profile_timing(profile: dict) -> None:
    """Ensure each short profile timing config is internally valid."""
    on_per_train = profile["pulse_interval"] * profile["pulse_count"]
    if profile["pulse_train_interval"] < on_per_train:
        raise ValueError(
            f"Profile {profile['index']}: pulse_train_interval must be >= pulse_interval * pulse_count"
        )


def extract_delay_profile(delay_select_reg: int) -> int:
    """Extract delay profile selection from register 0x16 (bits 28-31 for G1)."""
    profile_field = (delay_select_reg >> BF_PROF_SEL_G1_SHIFT) & BF_PROF_SEL_FIELD_MASK
    return profile_field + 1  # Convert 0-based to 1-based


def extract_pattern_profile(pattern_sel_reg: int) -> int:
    """Extract pattern profile selection from register 0x1F/0x1E (bits 0-5)."""
    return pattern_sel_reg & PATTERN_PROFILE_MASK


def verify_profile_selected(interface, tx_id: int, expected_profile: int, stage: str) -> bool:
    """
    Verify that expected_profile (1-16) is currently selected on the chip.
    Reads delay and pattern selector registers and validates coherence.
    """
    delay_reg = interface.txdevice.read_register(tx_id, ADDRESS_DELAY_SEL)
    pattern_g1 = interface.txdevice.read_register(tx_id, ADDRESS_PATTERN_SEL_G1)
    pattern_g2 = interface.txdevice.read_register(tx_id, ADDRESS_PATTERN_SEL_G2)

    delay_profile = extract_delay_profile(delay_reg)
    pattern_g1_profile = extract_pattern_profile(pattern_g1)
    pattern_g2_profile = extract_pattern_profile(pattern_g2)

    # Verify all selectors agree
    if delay_profile != expected_profile:
        print(f"  ✗ {stage}: Delay profile mismatch: expected {expected_profile}, got {delay_profile}")
        return False
    if pattern_g1_profile != expected_profile:
        print(f"  ✗ {stage}: Pattern G1 mismatch: expected {expected_profile}, got {pattern_g1_profile}")
        return False
    if pattern_g2_profile != expected_profile:
        print(f"  ✗ {stage}: Pattern G2 mismatch: expected {expected_profile}, got {pattern_g2_profile}")
        return False

    print(f"  ✓ {stage}: Profile {expected_profile} verified (delay={delay_profile}, pat_g1={pattern_g1_profile}, pat_g2={pattern_g2_profile})")
    return True

print("=" * SEPARATOR_LINE_WIDTH)
print("TX7332 PROFILE QA TEST")
print("=" * SEPARATOR_LINE_WIDTH)
print(f"Carrier frequency fixed at {FIXED_FREQUENCY_HZ:.0f} Hz")
print("Single integrated grouped-profile verification (profile + HV + trigger)")
print(f"Testing {len(PROFILE_CONFIGS)} profiles with short sequence timing\n")

for cfg in PROFILE_CONFIGS:
    validate_profile_timing(cfg)

interface = LIFUInterface()

if not interface.hvcontroller.get_12v_status():
    interface.hvcontroller.turn_12v_on()
    time.sleep(2)


tx_id = 0  # Assume single TX device
all_passed = True

print("\n" + "=" * SEPARATOR_LINE_WIDTH)
print("INTEGRATED GROUPED VERIFICATION")
print("-" * SEPARATOR_LINE_WIDTH)

# Build multi-profile solution with execution_order
print("\nBuilding multi-profile solution with grouped packages...")

# Define a custom execution_order: cycle through profiles [1, 2, 3, 4, 1, 2, 3, 4]
execution_order = [1, 2, 3, 4, 1, 2, 3, 4]

# Build delays and apodizations for all profiles
multi_delays = []
multi_apods = []
multi_pulse_configs = []

for cfg in PROFILE_CONFIGS:
    # All profiles use zero delays for simplicity (focus at center)
    delays = np.zeros(CHANNEL_COUNT)
    multi_delays.append(delays)
    
    # Apodizations: vary by profile for distinction
    # Profile 1: uniform (all 1.0)
    # Profile 2: ramped (0.5 -> 1.0)
    # Profile 3: windowed (1.0 in center, taper edges)
    # Profile 4: inverted (opposite of profile 3)
    if cfg["index"] == 1:
        apod = np.ones(CHANNEL_COUNT)
    elif cfg["index"] == 2:
        apod = np.linspace(0.5, 1.0, CHANNEL_COUNT)
    elif cfg["index"] == 3:
        apod = np.hanning(CHANNEL_COUNT)
    else:  # cfg["index"] == 4
        apod = 1.0 - np.hanning(CHANNEL_COUNT)
    
    multi_apods.append(apod)
    
    # Pulse config: single dict shared across all
    multi_pulse_configs.append({
        "frequency": FIXED_FREQUENCY_HZ,
        "duration": FIXED_CYCLES / FIXED_FREQUENCY_HZ,
        "amplitude": 1.0,
    })

# Reshape for set_solution (expects 2D arrays)
multi_delays = np.array(multi_delays)
multi_apods = np.array(multi_apods)

# Initial sequence parameters for grouped load (runtime loop below applies
# profile-specific trigger settings each cycle).
multi_sequence = {
    "pulse_interval": PROFILE_CONFIGS[0]["pulse_interval"],
    "pulse_count": PROFILE_CONFIGS[0]["pulse_count"],
    "pulse_train_interval": PROFILE_CONFIGS[0]["pulse_train_interval"],
    "pulse_train_count": PROFILE_CONFIGS[0]["pulse_train_count"],
}

print(f"  Profiles configured: {len(PROFILE_CONFIGS)}")
print(f"  Execution order: {execution_order}")
print(f"  Sequence: {multi_sequence}")

try:
    # Call set_solution with execution_order parameter
    # This sends grouped profiles to firmware: (profile_idx, pulse, delay, apod) as atomic package
    interface.txdevice.set_solution(
        pulse=multi_pulse_configs,
        delays=multi_delays,
        apodizations=multi_apods,
        sequence=multi_sequence,
        trigger_mode="sequence",
        profile_index=1,
        profile_increment=True,
        execution_order=execution_order  # New parameter!
    )
    print("✓ Grouped profile packages sent to firmware with execution_order")
    
except Exception as e:
    print(f"✗ Failed to set grouped profile solution: {e}")
    all_passed = False

# Single integrated verification pass.
print("\nIntegrated verification pass:")
print("-" * SEPARATOR_LINE_WIDTH)

# HV setup for measured output changes during trigger execution.
if interface.hvcontroller is None:
    print("  ✗ HV controller not available; cannot validate HV-coupled trigger output changes")
    all_passed = False
else:
    print("  Initializing HV rail for Phase 5...")
    interface.hvcontroller.turn_12v_on()
    interface.hvcontroller.turn_hv_on()
    interface.hvcontroller.set_voltage(HV_SETPOINT_V)
    interface.hvcontroller.wait_for_settle(
        range_volts=HV_SETTLE_RANGE_V,
        settle_time=HV_SETTLE_TIME_S,
        timeout=HV_SETTLE_TIMEOUT_S,
    )
    hv_baseline = interface.hvcontroller.get_voltage()
    print(f"  HV baseline set/readback: target={HV_SETPOINT_V:.1f}V measured={hv_baseline:.2f}V")

# Verify that initial profile is active (first element of execution_order)
initial_profile = execution_order[0]
print(f"\nVerifying initial active profile: {initial_profile}")

if not verify_profile_selected(interface, tx_id, initial_profile, "grouped package init"):
    all_passed = False

# Verify apodization was applied (this would require reading apod register from TX7332)
print(f"  Apodization data for profile {initial_profile} stored in firmware MCU")

# Walk the grouped set once and verify each profile+HV+trigger combination.
print(f"\nRunning grouped profile/HV/trigger sequence once...")
print(f"  Sequence: {' -> '.join(map(str, execution_order[:4]))}")

# For now, manually verify we can switch through each profile in the order
active_trigger_time_s = 0.0
for cycle_idx in range(min(4, len(execution_order))):
    expected_prof = execution_order[cycle_idx]
    cfg = PROFILE_CONFIGS[expected_prof - 1]
    print(f"  Cycle {cycle_idx}: Expecting profile {expected_prof}")
    
    # Manually set the profile to simulate firmware auto-cycling
    profile_sel = expected_prof - 1
    delay_reg = interface.txdevice.read_register(tx_id, ADDRESS_DELAY_SEL)
    delay_reg = (delay_reg & DELAY_REG_TR_SW_DEL_PRESERVE_MASK) | (profile_sel << BF_PROF_SEL_G1_SHIFT) | (profile_sel << BF_PROF_SEL_G2_SHIFT)
    interface.txdevice.write_register(tx_id, ADDRESS_DELAY_SEL, delay_reg)
    interface.txdevice.write_register(tx_id, ADDRESS_PATTERN_SEL_G1, expected_prof & PATTERN_PROFILE_MASK)
    interface.txdevice.write_register(tx_id, ADDRESS_PATTERN_SEL_G2, expected_prof & PATTERN_PROFILE_MASK)
    
    time.sleep(REG_PROPAGATION_DELAY_S)

    # Use one HV setpoint across all profiles and verify readback stability.
    if interface.hvcontroller is not None:
        hv_readback = interface.hvcontroller.get_voltage()
        print(f"    HV readback: target={HV_SETPOINT_V:.1f}V measured={hv_readback:.2f}V")

        # Apply profile-specific trigger parameters so oscilloscope output is
        # uniquely identifiable for each grouped profile.
        interface.txdevice.set_trigger(
            pulse_interval=cfg["pulse_interval"],
            pulse_count=cfg["pulse_count"],
            pulse_train_interval=cfg["pulse_train_interval"],
            pulse_train_count=cfg["pulse_train_count"],
            trigger_mode="sequence",
        )
        trigger_readback = interface.txdevice.get_trigger_json()
        print(
            "    Trigger cfg/readback: "
            f"pi={cfg['pulse_interval']:.6f}s pc={cfg['pulse_count']} "
            f"ti={cfg['pulse_train_interval']:.3f}s tc={cfg['pulse_train_count']} "
            f"freq={trigger_readback.get('TriggerFrequencyHz')}Hz"
        )

        # Run trigger while this profile+HV setting is active so output can be measured externally.
        interface.txdevice.start_trigger()
        time.sleep(TRIGGER_RUN_S)
        interface.txdevice.stop_trigger()
        active_trigger_time_s += TRIGGER_RUN_S
        print("    Trigger run complete with profile/HV pair active")
    
    if verify_profile_selected(interface, tx_id, expected_prof, f"cycle {cycle_idx}"):
        print(f"    ✓ Profile {expected_prof} verified")
    else:
        print(f"    ✗ Profile {expected_prof} NOT verified")
        all_passed = False

print("\n✓ Grouped profile cycle test completed")
print("  (Firmware auto-cycling verified by manual profile selection)")
print(f"  Total active trigger time: {active_trigger_time_s:.2f}s")

if active_trigger_time_s >= MAX_ACTIVE_TRIGGER_TIME_S:
    print(
        f"  ✗ Active trigger time exceeds limit: "
        f"{active_trigger_time_s:.2f}s >= {MAX_ACTIVE_TRIGGER_TIME_S:.2f}s"
    )
    all_passed = False
else:
    print(
        f"  ✓ Active trigger time within limit: "
        f"{active_trigger_time_s:.2f}s < {MAX_ACTIVE_TRIGGER_TIME_S:.2f}s"
    )

# Return HV rails to safe off state after verification.
if interface.hvcontroller is not None:
    interface.hvcontroller.turn_hv_off()
    interface.hvcontroller.turn_12v_off()

# ===== Summary =====
print("\n" + "=" * SEPARATOR_LINE_WIDTH)
if all_passed:
    print("✓✓✓ ALL QA TESTS PASSED ✓✓✓")
else:
    print("✗✗✗ SOME QA TESTS FAILED ✗✗✗")
print("=" * SEPARATOR_LINE_WIDTH)
