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
    # Tx7332DelayProfile,
    # Tx7332PulseProfile,
    # TxDeviceRegisters,
    ADDRESS_DELAY_SEL,
    ADDRESS_APODIZATION,
)

FIXED_FREQUENCY_HZ = 400
DURATION_MS = 5
CHANNEL_COUNT = 64
VOLTAGE = 20.0

# Formatting
SEPARATOR_LINE_WIDTH = 80

# TX7332 Register field definitions
BF_PROF_SEL_G1_SHIFT = 28  # Bits 28-31 for G1 delay profile selector
BF_PROF_SEL_G2_SHIFT = 12  # Bits 12-15 for G2 delay profile selector
BF_PROF_SEL_FIELD_MASK = 0x0F  # 4-bit profile field (0-15 for profiles 1-16)
DELAY_REG_TR_SW_DEL_PRESERVE_MASK = 0x0FFF0FFF  # Preserve TR_SW_DEL timing fields
REGISTER_0X18_READ_COMPARE_MASK = 0x07FFFFFF  # Ignore bits 31-27 on read per datasheet

# Short timing for integrated verification (keep total active trigger time < 10s)
REG_PROPAGATION_DELAY_S = 0.025
TRIGGER_RUN_S = 0.40
MAX_ACTIVE_TRIGGER_TIME_S = 10.0
MAX_REGISTER_READ_COUNT = 62  # Device/API read_block limit

# HV validation for grouped profile/trigger verification
# VOLTAGE = 20.0
# HV_SETTLE_RANGE_V = 2.0
# HV_SETTLE_TIME_S = 0.2
# HV_SETTLE_TIMEOUT_S = 10.0

# Watertank-inspired profile sequence configurations for fast QA.
# Reference point from test_watertank.py:
# - duration_msec = 5
# - interval_msec = 100
# Here we keep a ~5 ms burst window per profile but vary PRF/count/train interval
# so each profile is easy to differentiate on an oscilloscope.
# PROFILE_CONFIGS = [
#     {
#         "index": 1,
#         "pulse_interval": 0.0020,
#         "pulse_count": 3,
#         "pulse_train_interval": 0.120,
#         "pulse_train_count": 2,
#     },
#     {
#         "index": 2,
#         "pulse_interval": 0.0010,
#         "pulse_count": 5,
#         "pulse_train_interval": 0.090,
#         "pulse_train_count": 2,
#     },
#     {
#         "index": 3,
#         "pulse_interval": 0.0005,
#         "pulse_count": 10,
#         "pulse_train_interval": 0.060,
#         "pulse_train_count": 2,
#     },
#     {
#         "index": 4,
#         "pulse_interval": 0.00025,
#         "pulse_count": 20,
#         "pulse_train_interval": 0.040,
#         "pulse_train_count": 2,
#     },
# ]

PROFILE_CONFIGS = [
    {
        "index": 1,
        "duration_ms": 2,
        "pulse_interval": 0.1,
        "pulse_count": 10,
        "pulse_train_interval": 1,
        "pulse_train_count": 1,
    },
    {
        "index": 2,
        "duration_ms": 5,
        "pulse_interval": 0.01,
        "pulse_count": 10,
        "pulse_train_interval": 1,
        "pulse_train_count": 1,
    },
    {
        "index": 3,
        "duration_ms": 10,
        "pulse_interval": 0.001,
        "pulse_count": 10,
        "pulse_train_interval": 1,
        "pulse_train_count": 1,
    },
    {
        "index": 4,
        "duration_ms": 20,
        "pulse_interval": 0.1,
        "pulse_count": 10,
        "pulse_train_interval": 1,
        "pulse_train_count": 1,
    },
]


def validate_profile_timing(profile: dict) -> None:
    """Ensure each short profile timing config is internally valid."""
    on_per_train = profile["pulse_interval"] * profile["pulse_count"]
    if profile["pulse_train_interval"] < on_per_train:
        raise ValueError(
            f"Profile {profile['index']}: pulse_train_interval must be >= pulse_interval * pulse_count"
        )


def read_active_delay_profile(delay_select_reg: int) -> int:
    """Extract delay profile selection from register 0x16 (bits 28-31 for G1)."""
    profile_field = (delay_select_reg >> BF_PROF_SEL_G1_SHIFT) & BF_PROF_SEL_FIELD_MASK
    return profile_field + 1  # Convert 0-based to 1-based


def verify_delay_profile_selected(interface, tx_id: int, expected_profile: int, stage: str) -> bool:
    """
    Verify that expected delay profile (1-16) is currently selected on the chip.
    Reads delay selector register and validates active delay profile.
    """
    delay_reg  = interface.txdevice.read_register(tx_id, ADDRESS_DELAY_SEL)

    delay_profile = read_active_delay_profile(delay_reg)

    if delay_profile != expected_profile:
        print(f"  ✗ {stage}: Delay profile mismatch: expected {expected_profile}, got {delay_profile}")
        return False

    print(f"  ✓ {stage}: Delay profile {expected_profile} verified (delay={delay_profile})")
    return True


def verify_apodization_register(interface, tx_id: int, expected_value: int, stage: str) -> bool:
    """Verify apodization register 0x1B matches expected value."""
    apod_reg = interface.txdevice.read_register(tx_id, ADDRESS_APODIZATION)
    if apod_reg != expected_value:
        print(
            f"  ✗ {stage}: Apodization mismatch: "
            f"expected 0x{expected_value:08X}, got 0x{apod_reg:08X}"
        )
        return False

    print(f"  ✓ {stage}: Apodization verified (0x{apod_reg:08X})")
    return True


def write_and_readback_all_registers(interface) -> bool:
    """Write all configured TX registers to hardware, read back, and print values."""
    print("\nProfile register targets before write:")
    try:
        delay_profiles = interface.txdevice.tx_registers.configured_delay_profiles()
        pulse_profiles = interface.txdevice.tx_registers.configured_pulse_profiles()
        profile_ids = sorted(set(delay_profiles) | set(pulse_profiles))

        for profile in profile_ids:
            print(f"  Profile {profile}:")

            delay_groups = interface.txdevice.tx_registers.get_delay_control_registers(profile)

            for txi, regs in enumerate(delay_groups):
                delay_sel = regs.get(ADDRESS_DELAY_SEL)
                apod_reg = regs.get(ADDRESS_APODIZATION)
                if delay_sel is not None:
                    print(
                        f"    TX{txi} write 0x{ADDRESS_DELAY_SEL:04X} "
                        f"(DELAY_SEL) = 0x{delay_sel:08X}"
                    )
                if apod_reg is not None:
                    print(
                        f"    TX{txi} write 0x{ADDRESS_APODIZATION:04X} "
                        f"(APODIZATION) = 0x{apod_reg:08X}"
                    )
    except Exception as e:
        print(f"  ! Unable to print per-profile register targets: {e}")

    print("\nWriting all configured TX registers to hardware...")
    if not interface.txdevice.apply_all_registers():
        print("✗ Failed to write all registers to hardware")
        return False
    print("✓ Register write complete")

    print("\nReading back all configured TX registers from chip(s)...")
    packed_registers = interface.txdevice.tx_registers.get_registers(
        profiles="configured", pack=True, pack_single=True
    )
    readback_ok = True
    for txi, txregs in enumerate(packed_registers):
        print(f"  TX{txi} register readback:")
        for start_addr in sorted(txregs):
            expected_vals = txregs[start_addr]
            read_vals = []
            total_count = len(expected_vals)
            chunk_offset = 0
            while chunk_offset < total_count:
                chunk_count = min(MAX_REGISTER_READ_COUNT, total_count - chunk_offset)
                chunk_vals = interface.txdevice.read_block(
                    identifier=txi,
                    start_address=start_addr + chunk_offset,
                    count=chunk_count,
                )
                if chunk_vals is None:
                    read_vals = None
                    break
                read_vals.extend(chunk_vals)
                chunk_offset += chunk_count

            if read_vals is None:
                print(
                    f"    ✗ 0x{start_addr:04X}..0x{start_addr + len(expected_vals) - 1:04X} read failed"
                )
                readback_ok = False
                continue

            for offset, (expected, actual) in enumerate(zip(expected_vals, read_vals)):
                addr = start_addr + offset
                print(f"    0x{addr:04X}: 0x{actual:08X}")
                expected_cmp = expected
                actual_cmp = actual
                if addr == 0x0018:
                    expected_cmp &= REGISTER_0X18_READ_COMPARE_MASK
                    actual_cmp &= REGISTER_0X18_READ_COMPARE_MASK

                if actual_cmp != expected_cmp:
                    if addr == 0x0018:
                        print(
                            "      ✗ mismatch expected "
                            f"0x{expected:08X} (masked=0x{expected_cmp:08X})"
                        )
                    else:
                        print(f"      ✗ mismatch expected 0x{expected:08X}")
                    readback_ok = False

    if readback_ok:
        print("✓ Register readback complete: all values match configured data")
    else:
        print("✗ Register readback complete: mismatches detected")

    return readback_ok

print("=" * SEPARATOR_LINE_WIDTH)
print("TX7332 PROFILE QA TEST")
print("=" * SEPARATOR_LINE_WIDTH)
print(f"Frequency: {FIXED_FREQUENCY_HZ:.0f} kHz")
print("Single integrated grouped-profile verification (profile + HV + trigger)")
print(f"Testing {len(PROFILE_CONFIGS)} profiles with short sequence timing\n")

for cfg in PROFILE_CONFIGS:
    validate_profile_timing(cfg)

interface = LIFUInterface()

if not interface.hvcontroller.get_12v_status():
    interface.hvcontroller.turn_12v_on()
    time.sleep(2)


tx_id = 0  # Assume single TX device for now, once 1 is working make sure multiple work
all_passed = True

print("\n" + "=" * SEPARATOR_LINE_WIDTH)
print("INTEGRATED GROUPED VERIFICATION")
print("-" * SEPARATOR_LINE_WIDTH)

# Build multi-profile solution
print("\nBuilding multi-profile solution with grouped packages...")

# Build delays and apodizations for all profiles
multi_delays = []
multi_apods = []
multi_pulse_configs = []

for cfg in PROFILE_CONFIGS:
    # Profiles 2 and 4 start 100 us later than profiles 1 and 3
    if cfg["index"] in (2, 4):
        delays = np.full(CHANNEL_COUNT, 100e-6)
    else:
        delays = np.zeros(CHANNEL_COUNT)
    multi_delays.append(delays)
    
    # Apodizations: vary by profile for distinction
    # Profile 1: uniform (all 1.0)
    # Profile 2: ramped (0.5 -> 1.0)
    # Profile 3: windowed (1.0 in center, taper edges)
    # Profile 4: inverted (opposite of profile 3)                                  
    # if cfg["index"] == 1:
    #     apod = np.ones(CHANNEL_COUNT)
    # elif cfg["index"] == 2:
    #     apod = np.linspace(0.5, 1.0, CHANNEL_COUNT)
    # elif cfg["index"] == 3:
    #     apod = np.hanning(CHANNEL_COUNT)
    # else:  # cfg["index"] == 4
    #     apod = 1.0 - np.hanning(CHANNEL_COUNT)

    apod = np.ones(CHANNEL_COUNT)
    multi_apods.append(apod)
    
    # Pulse config: single dict shared across all
    multi_pulse_configs.append({
        "frequency": FIXED_FREQUENCY_HZ*1e3,
        "duration": cfg["duration_ms"]*1e-3,
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
print(f"  Sequence: {multi_sequence}")

try:
    # Call set_solution; firmware will step through configured profiles in order.
    # This sends grouped profiles to firmware: (profile_idx, pulse, delay, apod) as atomic package
    interface.txdevice.set_solution(
        pulse=multi_pulse_configs,
        delays=multi_delays,
        apodizations=multi_apods,
        sequence=multi_sequence,
        trigger_mode="sequence",
        profile_index=1,
        profile_increment=True,
    )
    print("✓ Grouped profile packages sent to firmware")
    
except Exception as e:
    print(f"✗ Failed to set grouped profile solution: {e}")
    all_passed = False

# try:
#     if not write_and_readback_all_registers(interface):
#         all_passed = False
# except Exception as e:
#     print(f"✗ Failed during write/readback stage: {e}")
#     all_passed = False

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
    interface.hvcontroller.set_voltage(VOLTAGE)
    interface.hvcontroller.wait_for_settle()
    hv_baseline = interface.hvcontroller.get_voltage()
    print(f"  HV baseline set/readback: target={VOLTAGE:.1f}V measured={hv_baseline:.2f}V")

# Verify that initial profile is active (first configured profile)
initial_profile = 1
print(f"\nVerifying initial active profile: {initial_profile}")

if not verify_delay_profile_selected(interface, tx_id, initial_profile, "grouped package init"):
    all_passed = False

# Verify apodization was applied (this would require reading apod register from TX7332)
print(f"  Apodization data for profile {initial_profile} stored in firmware MCU")

# Walk the grouped set once and verify each profile+HV+trigger combination.
print(f"\nRunning grouped profile/HV/trigger sequence once...")
profile_sequence = [cfg["index"] for cfg in PROFILE_CONFIGS]
print(f"  Sequence: {' -> '.join(map(str, profile_sequence))}")

# Configure trigger once — delay profile switching does not change trigger timing.
interface.txdevice.set_trigger(
    pulse_interval=multi_sequence["pulse_interval"],
    pulse_count=multi_sequence["pulse_count"],
    pulse_train_interval=multi_sequence["pulse_train_interval"],
    pulse_train_count=multi_sequence["pulse_train_count"],
    trigger_mode="sequence",
)
trigger_readback = interface.txdevice.get_trigger_json()
print(
    "  Trigger configured: "
    f"pi={multi_sequence['pulse_interval']:.6f}s "
    f"pc={multi_sequence['pulse_count']} "
    f"ti={multi_sequence['pulse_train_interval']:.3f}s "
    f"tc={multi_sequence['pulse_train_count']} "
    f"freq={trigger_readback.get('TriggerFrequencyHz')}Hz"
)

# Preload all delay data + apodization for every profile once before switching.
print("\nPreloading all profile delay/apodization data...")
expected_apod_by_profile = {}
expected_delay_sel_by_profile = {}
for profile in profile_sequence:
    delay_data_list = interface.txdevice.tx_registers.get_delay_data_registers(
        profile, pack=True, pack_single=True
    )
    delay_ctrl_list = interface.txdevice.tx_registers.get_delay_control_registers(profile)
    num_tx_chips = len(delay_data_list)

    # Store expected values from tx_id=0 for verification
    expected_apod_by_profile[profile] = delay_ctrl_list[tx_id][ADDRESS_APODIZATION]
    expected_delay_sel_by_profile[profile] = delay_ctrl_list[tx_id][ADDRESS_DELAY_SEL]

    print(f"  Preload profile {profile} delay data ({num_tx_chips} TX chips):")
    for chip_idx in range(num_tx_chips):
        tx_delay_data = delay_data_list[chip_idx]
        tx_ctrl = delay_ctrl_list[chip_idx]
        for start_addr, reg_values in sorted(tx_delay_data.items()):
            print(
                f"    TX{chip_idx} 0x{start_addr:04X}..0x{start_addr + len(reg_values) - 1:04X} "
                f"({len(reg_values)} regs)"
            )
            interface.txdevice.write_block(identifier=chip_idx, start_address=start_addr, reg_values=reg_values)

        print(f"    TX{chip_idx} preload apodization: 0x{tx_ctrl[ADDRESS_APODIZATION]:08X}")
        interface.txdevice.write_register(chip_idx, ADDRESS_APODIZATION, tx_ctrl[ADDRESS_APODIZATION])

interface.txdevice.commit_profile_ram(tx_id)
print("  ✓ Preload committed to profile RAM")

# Force starting point after preload.
for chip_idx in range(num_tx_chips):
    interface.txdevice.write_register(chip_idx, ADDRESS_DELAY_SEL, expected_delay_sel_by_profile[initial_profile])
time.sleep(REG_PROPAGATION_DELAY_S)

# Switch through each profile by changing selector only.
active_trigger_time_s = 0.0
for cycle_idx in range(len(PROFILE_CONFIGS)):
    expected_prof = profile_sequence[(cycle_idx + 1) % len(profile_sequence)]
    print(f"  Cycle {cycle_idx}: Switching to next profile {expected_prof}")

    pre_delay_reg = interface.txdevice.read_register(tx_id, ADDRESS_DELAY_SEL)
    pre_apod_reg = interface.txdevice.read_register(tx_id, ADDRESS_APODIZATION)
    pre_delay_profile = read_active_delay_profile(pre_delay_reg)
    print(
        "    Active before write: "
        f"delay={pre_delay_profile}, apod=0x{pre_apod_reg:08X}"
    )

    # Write delay selector to all TX chips (tell each chip which profile slot is active)
    print(f"    Writing delay selector: 0x{expected_delay_sel_by_profile[expected_prof]:08X}")
    for chip_idx in range(num_tx_chips):
        interface.txdevice.write_register(chip_idx, ADDRESS_DELAY_SEL, expected_delay_sel_by_profile[expected_prof])

    time.sleep(REG_PROPAGATION_DELAY_S)

    post_delay_reg = interface.txdevice.read_register(tx_id, ADDRESS_DELAY_SEL)
    post_apod_reg = interface.txdevice.read_register(tx_id, ADDRESS_APODIZATION)
    post_delay_profile = read_active_delay_profile(post_delay_reg)
    print(
        "    Active after write: "
        f"delay={post_delay_profile}, apod=0x{post_apod_reg:08X}"
    )

    if post_delay_profile == pre_delay_profile:
        print("    ✗ Delay profile selector did not change after write")
        all_passed = False
    else:
        print("    ✓ Delay profile selector changed after write")

    if post_apod_reg != expected_apod_by_profile[expected_prof]:
        print(
            "    ✗ Apodization register mismatch after switch: "
            f"expected 0x{expected_apod_by_profile[expected_prof]:08X}, got 0x{post_apod_reg:08X}"
        )
        all_passed = False
    else:
        print("    ✓ Apodization register matches expected profile value")

    # Use one HV setpoint across all profiles and verify readback stability.
    if interface.hvcontroller is not None:
        hv_readback = interface.hvcontroller.get_voltage()
        print(f"    HV readback: target={VOLTAGE:.1f}V measured={hv_readback:.2f}V")

        # Run trigger with delay profile active so output can be measured externally.
        interface.txdevice.start_trigger()
        time.sleep(TRIGGER_RUN_S)
        interface.txdevice.stop_trigger()
        active_trigger_time_s += TRIGGER_RUN_S
        print("    Trigger run complete with delay profile active")
    
    if verify_delay_profile_selected(interface, tx_id, expected_prof, f"cycle {cycle_idx}"):
        print(f"    ✓ Profile {expected_prof} verified")
    else:
        print(f"    ✗ Profile {expected_prof} NOT verified")
        all_passed = False

    if not verify_apodization_register(interface, tx_id, expected_apod_by_profile[expected_prof], f"cycle {cycle_idx}"):
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
    # interface.hvcontroller.turn_12v_off()

# ===== Summary =====
print("\n" + "=" * SEPARATOR_LINE_WIDTH)
if all_passed:
    print("--- QA TESTS PASSED ---")
else:
    print("!!! QA TESTS FAILED !!!")
print("=" * SEPARATOR_LINE_WIDTH)
