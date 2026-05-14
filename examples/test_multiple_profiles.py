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
ACTIVE_SECONDS = 3.0
OFF_SECONDS = 3.0
ROUNDS = 4

# TX7332 Register field definitions
BF_PROF_SEL_G1_SHIFT = 28  # Bits 28-31 for G1 delay profile selector
BF_PROF_SEL_G2_SHIFT = 12  # Bits 12-15 for G2 delay profile selector
BF_PROF_SEL_FIELD_MASK = 0x0F  # 4-bit profile field (0-15 for profiles 1-16)
DELAY_REG_TR_SW_DEL_PRESERVE_MASK = 0x0FFF0FFF  # Preserve TR_SW_DEL timing fields
PATTERN_PROFILE_MASK = 0x3F  # 6-bit pattern profile field (bits 0-5)

# Timing delays for test phases
REG_PROPAGATION_DELAY_S = 0.1  # Allow time for register writes to propagate
RAPID_SWITCH_DELAY_S = 0.05  # Short delay between rapid profile switches
PHASE3_SWITCH_CYCLES = 2  # Number of full cycles through all profiles in phase 3

# Profile array sizing
CHANNEL_COUNT = 64  # Number of delay/apodization channels per profile

# Output formatting
SEPARATOR_LINE_WIDTH = 80

# Profile distinction is created only with trigger sequence parameters.
# Each profile keeps pulse_interval * pulse_count * pulse_train_count = 3.0s.
PROFILE_CONFIGS = [
    {
        "index": 1,
        "pulse_interval": 0.010,
        "pulse_count": 100,
        "pulse_train_interval": 1.0,
        "pulse_train_count": 3,
    },
    {
        "index": 2,
        "pulse_interval": 0.001,
        "pulse_count": 100,
        "pulse_train_interval": 0.5,
        "pulse_train_count": 6,
    },
    {
        "index": 3,
        "pulse_interval": 0.002,
        "pulse_count": 150,
        "pulse_train_interval": 0.3,
        "pulse_train_count": 10,
    },
    {
        "index": 4,
        "pulse_interval": 0.005,
        "pulse_count": 80,
        "pulse_train_interval": 0.6,
        "pulse_train_count": 5,
    },
]


def validate_profile_timing(profile: dict) -> None:
    """Ensure profile timing is valid and matches the 3s active requirement."""
    on_per_train = profile["pulse_interval"] * profile["pulse_count"]
    if profile["pulse_train_interval"] < on_per_train:
        raise ValueError(
            f"Profile {profile['index']}: pulse_train_interval must be >= pulse_interval * pulse_count"
        )

    total_active = profile["pulse_train_interval"] * profile["pulse_train_count"]
    if abs(total_active - ACTIVE_SECONDS) > 1e-9:
        raise ValueError(
            f"Profile {profile['index']}: total active time must be {ACTIVE_SECONDS}s, got {total_active}s"
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
print(f"Testing {len(PROFILE_CONFIGS)} profiles with register-level verification\n")

for cfg in PROFILE_CONFIGS:
    validate_profile_timing(cfg)

interface = LIFUInterface()
txm = TxDeviceRegisters()

# ===== PHASE 1: Profile Load Verification =====
print("PHASE 1: Loading profiles to on-chip RAM...")
print("-" * SEPARATOR_LINE_WIDTH)

# Load all profiles
for cfg in PROFILE_CONFIGS:
    delays = np.zeros(64)
    apodizations = np.ones(64)
    delays = np.zeros(CHANNEL_COUNT)
    apodizations = np.ones(CHANNEL_COUNT)
    txm.add_delay_profile(Tx7332DelayProfile(cfg["index"], delays, apodizations))

for cfg in PROFILE_CONFIGS:
    txm.add_pulse_profile(Tx7332PulseProfile(cfg["index"], FIXED_FREQUENCY_HZ, FIXED_CYCLES))

# Activate all profiles
for cfg in PROFILE_CONFIGS:
    txm.activate_delay_profile(cfg["index"])
    txm.activate_pulse_profile(cfg["index"])

interface.txdevice.tx_registers = txm
interface.txdevice.apply_all_registers()

print("✓ All profiles loaded to on-chip RAM")
print("\nProfile configuration summary:")
for cfg in PROFILE_CONFIGS:
    pulse = txm.get_pulse_profile(cfg["index"])
    print(
        f"  Profile {cfg['index']}: f={pulse.frequency:.0f}Hz, cycles={pulse.cycles}, "
        f"pi={cfg['pulse_interval']:.3f}s, pc={cfg['pulse_count']}, "
        f"ti={cfg['pulse_train_interval']:.3f}s, tc={cfg['pulse_train_count']}"
    )

# ===== PHASE 2: Profile Selection and Verification =====
print("\n" + "=" * SEPARATOR_LINE_WIDTH)
print("PHASE 2: Profile Selection and Verification")
print("-" * SEPARATOR_LINE_WIDTH)

tx_id = 0  # Assume single TX device
all_passed = True

for cfg in PROFILE_CONFIGS:
    profile_idx = cfg["index"]
    print(f"\nTest: Set profile {profile_idx}, then verify...")
    
    # Set delay profile selector (register 0x16, bits 28-31 and 12-15)
    profile_sel = profile_idx - 1  # Convert 1-based to 0-based
    delay_reg = interface.txdevice.read_register(tx_id, ADDRESS_DELAY_SEL)
    # Preserve TR_SW_DEL fields, update only BF_PROF_SEL bits
    delay_reg = (delay_reg & DELAY_REG_TR_SW_DEL_PRESERVE_MASK) | (profile_sel << BF_PROF_SEL_G1_SHIFT) | (profile_sel << BF_PROF_SEL_G2_SHIFT)
    interface.txdevice.write_register(tx_id, ADDRESS_DELAY_SEL, delay_reg)
    
    # Set pattern profile selectors (registers 0x1F and 0x1E, bits 0-5)
    interface.txdevice.write_register(tx_id, ADDRESS_PATTERN_SEL_G1, profile_idx & PATTERN_PROFILE_MASK)
    interface.txdevice.write_register(tx_id, ADDRESS_PATTERN_SEL_G2, profile_idx & PATTERN_PROFILE_MASK)
    
    # Small delay for register propagation
    time.sleep(REG_PROPAGATION_DELAY_S)
    
    # Verify the selection
    if not verify_profile_selected(interface, tx_id, profile_idx, f"immediate readback"):
        all_passed = False

print("\n" + "=" * SEPARATOR_LINE_WIDTH)
print("PHASE 3: Profile Switching (Rapid Sequential)")
print("-" * SEPARATOR_LINE_WIDTH)

# Test rapid switching between profiles
switch_count = 0
for _ in range(PHASE3_SWITCH_CYCLES):  # Full cycles through all profiles
    for cfg in PROFILE_CONFIGS:
        profile_idx = cfg["index"]
        profile_sel = profile_idx - 1
        
        # Write delay selector
        delay_reg = interface.txdevice.read_register(tx_id, ADDRESS_DELAY_SEL)
        delay_reg = (delay_reg & DELAY_REG_TR_SW_DEL_PRESERVE_MASK) | (profile_sel << BF_PROF_SEL_G1_SHIFT) | (profile_sel << BF_PROF_SEL_G2_SHIFT)
        interface.txdevice.write_register(tx_id, ADDRESS_DELAY_SEL, delay_reg)
        
        # Write pattern selectors
        interface.txdevice.write_register(tx_id, ADDRESS_PATTERN_SEL_G1, profile_idx & PATTERN_PROFILE_MASK)
        interface.txdevice.write_register(tx_id, ADDRESS_PATTERN_SEL_G2, profile_idx & PATTERN_PROFILE_MASK)
        
        time.sleep(RAPID_SWITCH_DELAY_S)
        
        if verify_profile_selected(interface, tx_id, profile_idx, f"switch #{switch_count}"):
            switch_count += 1
        else:
            all_passed = False

print(f"\n✓ Completed {switch_count} profile switches with verification")

# ===== PHASE 4: Profile Switching During Trigger (Functional Test) =====
print("\n" + "=" * SEPARATOR_LINE_WIDTH)
print("PHASE 4: Profile Switching with Trigger Verification")
print("-" * SEPARATOR_LINE_WIDTH)

for cfg in PROFILE_CONFIGS:
    index = cfg["index"]
    print(f"\nProfile {index}: Set active and configure trigger...")
    
    # Set profile selectors
    profile_sel = index - 1
    delay_reg = interface.txdevice.read_register(tx_id, ADDRESS_DELAY_SEL)
    delay_reg = (delay_reg & DELAY_REG_TR_SW_DEL_PRESERVE_MASK) | (profile_sel << BF_PROF_SEL_G1_SHIFT) | (profile_sel << BF_PROF_SEL_G2_SHIFT)
    interface.txdevice.write_register(tx_id, ADDRESS_DELAY_SEL, delay_reg)
    interface.txdevice.write_register(tx_id, ADDRESS_PATTERN_SEL_G1, index & PATTERN_PROFILE_MASK)
    interface.txdevice.write_register(tx_id, ADDRESS_PATTERN_SEL_G2, index & PATTERN_PROFILE_MASK)

    # Verify selection before trigger
    if not verify_profile_selected(interface, tx_id, index, "pre-trigger"):
        all_passed = False

    # Configure and start trigger
    interface.txdevice.set_trigger(
        pulse_interval=cfg["pulse_interval"],
        pulse_count=cfg["pulse_count"],
        pulse_train_interval=cfg["pulse_train_interval"],
        pulse_train_count=cfg["pulse_train_count"],
        trigger_mode="sequence",
    )

    trigger_readback = interface.txdevice.get_trigger_json()
    print(
        f"  Trigger: pi={cfg['pulse_interval']}s, pc={cfg['pulse_count']}, "
        f"ti={cfg['pulse_train_interval']}s, tc={cfg['pulse_train_count']}, "
        f"device_freq={trigger_readback.get('TriggerFrequencyHz')}Hz"
    )

    interface.txdevice.start_trigger()
    time.sleep(ACTIVE_SECONDS)
    interface.txdevice.stop_trigger()
    
    # Verify selection after trigger
    if not verify_profile_selected(interface, tx_id, index, "post-trigger"):
        all_passed = False

    print(f"  Sleeping {OFF_SECONDS}s before next profile...")
    time.sleep(OFF_SECONDS)

# ===== Summary =====
print("\n" + "=" * SEPARATOR_LINE_WIDTH)
if all_passed:
    print("✓✓✓ ALL QA TESTS PASSED ✓✓✓")
else:
    print("✗✗✗ SOME QA TESTS FAILED ✗✗✗")
print("=" * SEPARATOR_LINE_WIDTH)
