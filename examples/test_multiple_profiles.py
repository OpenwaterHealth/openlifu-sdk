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
    ADDRESS_DELAY_SEL,
    ADDRESS_APODIZATION,
)

FIXED_FREQUENCY_HZ = 400
DURATION_MS = 5
CHANNEL_COUNT = 64
VOLTAGE = 20.0
MAX_NUM_PROFILES = 16

# Formatting
SEPARATOR_LINE_WIDTH = 80

# TX7332 Register field definitions
BF_PROF_SEL_G1_SHIFT = 28  # Bits 28-31 for G1 delay profile selector
BF_PROF_SEL_G2_SHIFT = 12  # Bits 12-15 for G2 delay profile selector
BF_PROF_SEL_FIELD_MASK = 0x0F  # 4-bit profile field (0-15 for profiles 1-16)
# DELAY_REG_TR_SW_DEL_PRESERVE_MASK = 0x0FFF0FFF  # Preserve TR_SW_DEL timing fields
REGISTER_0X18_READ_COMPARE_MASK = 0x07FFFFFF  # Ignore bits 31-27 on read per datasheet

# Short timing for integrated verification (keep total active trigger time < 10s)
REG_PROPAGATION_DELAY_S = 0.025
TRIGGER_RUN_S = 0.40
MAX_ACTIVE_TRIGGER_TIME_S = 60.0

def get_profile_delay_sequence(profile_number: int) -> np.ndarray:
    """Return the profile delay sequence from the commented multi-profile section.

    Profiles 2 and 4: all channels delayed by 100 us.
    Profiles 1 and 3: all channels set to 0 us.
    """
    if profile_number in (2, 4):
        return np.full((1, CHANNEL_COUNT), 100e-6, dtype=float)
    return np.zeros((1, CHANNEL_COUNT), dtype=float)


def read_profile_delays_all_chips(interface: LIFUInterface,
                                  profile_number: int,
                                  num_tx_devices: int) -> np.ndarray:
    """Read and concatenate delay profile values from all TX chips (64 channels total)."""
    all_delays = []
    for i in range(num_tx_devices):
        result = interface.txdevice.read_delay_profile_value(
            profile_number=profile_number,
            identifier=i,
            units="s",
        )
        print(f"delay profile readback for tx={i}, profile={profile_number}")
        print(f"  raw_registers[0:4]={result['raw_registers'][:4]}")
        print(f"  delays ({len(result['delays'])} channels)={result['delays']}")
        all_delays.extend(result["delays"])
    # for chip_idx in range(num_tx_devices):
    #     result = interface.txdevice.read_delay_profile_value(
    #         profile_number=profile_number,
    #         identifier=chip_idx,
    #         units="s",
    #     )
    #     print(f"delay profile readback for tx={chip_idx}, profile={profile_number}")
    #     print(f"  raw_registers[0:4]={result['raw_registers'][:4]}")
    #     print(f"  delays ({len(result['delays'])} channels)={result['delays']}")
    #     all_delays.extend(result["delays"])
    return np.array(all_delays, dtype=float)


def quantize_delays_to_device(delays_seconds: np.ndarray, bf_clk_hz: float) -> np.ndarray:
    """Match device packing behavior: integer ticks via truncation to floor."""
    return np.floor(delays_seconds * bf_clk_hz) / bf_clk_hz


MAX_REGISTER_READ_COUNT = 62  # Device/API read_block limit

# Debug aid: keep all channels enabled on every profile to isolate
# physical output issues from apodization masking behavior.
FORCE_ALL_CHANNELS_ON = False

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

'''
def validate_profile_timing(profile: dict) -> None:
    """Ensure each short profile timing config is internally valid."""
    on_per_train = profile["pulse_interval"] * profile["pulse_count"]
    if profile["pulse_train_interval"] < on_per_train:
        raise ValueError(
            f"Profile {profile['index']}: pulse_train_interval must be >= pulse_interval * pulse_count"
        )


# def read_active_delay_profile(delay_select_reg: int) -> int:
#     """Extract delay profile selection from register 0x16 (bits 28-31 for G1)."""
#     profile_field = (delay_select_reg >> BF_PROF_SEL_G1_SHIFT) & BF_PROF_SEL_FIELD_MASK
#     return profile_field + 1  # Convert 0-based to 1-based

def read_active_delay_profile(interface, num_tx_devices: int) -> int:
    """Read the active delay profile for a specific chip via the interface."""
    
    profiles = []
    for i in range(num_tx_devices):
        profiles.append(interface.txdevice.tx_registers.get_active_delay_profile(i))

    if any(v != profiles[0] for v in profiles[1:]):
        raise RuntimeError(
            f"Active delay profile mismatch across chips: {profiles}"
        )
    return profiles[0]  # Return the active profile of the first chip

def verify_delay_profile_selected(interface, expected_profile: int, stage: str, num_tx_devices: int) -> list[int]:
    """Read delay profile on all chips and verify they match.

    Returns list of per-chip delay profile values.
    Raises RuntimeError if chip values do not match each other.
    """
    delay_profiles = []
    for chip_idx in range(num_tx_devices):
        # delay_reg = interface.txdevice.read_register(chip_idx, ADDRESS_DELAY_SEL)
        delay_profiles.append(interface.txdevice.tx_registers.get_active_delay_profile(chip_idx))

    if any(v != delay_profiles[0] for v in delay_profiles[1:]):
        raise RuntimeError(
            f"{stage}: chip delay profile mismatch across chips: {delay_profiles}"
        )

    if delay_profiles[0] != expected_profile:
        print(
            f"  [FAIL] {stage}: Delay profile mismatch: "
            f"expected {expected_profile}, got {delay_profiles}"
        )
    else:
        print(
            f"  [OK] {stage}: Delay profile {expected_profile} verified "
            f"(delay={delay_profiles})"
        )
    return delay_profiles


def verify_apodization_register(interface, expected_value: int, stage: str, num_tx_devices: int) -> list[int]:
    """Read apodization register on all chips and verify they match.

    Returns list of per-chip apodization values.
    Raises RuntimeError if chip values do not match each other.
    """
    apod_values = []
    for chip_idx in range(num_tx_devices):
        apod_values.append(interface.txdevice.read_register(chip_idx, ADDRESS_APODIZATION))

    if any(v != apod_values[0] for v in apod_values[1:]):
        raise RuntimeError(
            f"{stage}: chip apodization mismatch across chips: "
            f"{[f'0x{v:08X}' for v in apod_values]}"
        )

    if apod_values[0] != expected_value:
        print(
            f"  [FAIL] {stage}: Apodization mismatch: "
            f"expected 0x{expected_value:08X}, got {[f'0x{v:08X}' for v in apod_values]}"
        )
    else:
        print(
            f"  [OK] {stage}: Apodization verified "
            f"({[f'0x{v:08X}' for v in apod_values]})"
        )
    return apod_values


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
        print("[FAIL] Failed to write all registers to hardware")
        return False
    print("[OK] Register write complete")

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
                    f"    [FAIL] 0x{start_addr:04X}..0x{start_addr + len(expected_vals) - 1:04X} read failed"
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
                            "      [FAIL] mismatch expected "
                            f"0x{expected:08X} (masked=0x{expected_cmp:08X})"
                        )
                    else:
                        print(f"      [FAIL] mismatch expected 0x{expected:08X}")
                    readback_ok = False

    if readback_ok:
        print("[OK] Register readback complete: all values match configured data")
    else:
        print("[FAIL] Register readback complete: mismatches detected")

    return readback_ok

print("=" * SEPARATOR_LINE_WIDTH)
print("TX7332 MULTIPLE PROFILE AND APODIZATIONS QA TEST")
print("=" * SEPARATOR_LINE_WIDTH)
print(f"Frequency: {FIXED_FREQUENCY_HZ:.0f} kHz")
print(f"Testing {len(PROFILE_CONFIGS)} profiles\n")

for cfg in PROFILE_CONFIGS:
    validate_profile_timing(cfg)

interface = LIFUInterface()

if not interface.hvcontroller.get_12v_status():
    interface.hvcontroller.turn_12v_on()
    time.sleep(2)

if interface.txdevice.tx_registers is None:
    num_tx_devices = interface.txdevice.enum_tx7332_devices()
else:
    num_tx_devices = interface.txdevice.tx_registers.num_transmitters
print(f"num_tx_devices={num_tx_devices}")


tx_id = 0  # Representative chip index for summary readouts.
all_passed = True

# Build multi-profile solution
print("\nBuilding multiple profiles...")

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
    
    # Apodization is provided as logical 0.0/1.0 values at script level.
    # Register-level active-low inversion happens in SDK/firmware mapping.
    apod = np.zeros(CHANNEL_COUNT, dtype=float)

    if FORCE_ALL_CHANNELS_ON:
        apod[:] = 1.0
    elif cfg["index"] == 1:
        # Profile 1: all channels enabled.
        apod[:] = 1.0
    elif cfg["index"] == 2:
        # Profile 2: alternating channels on across full aperture.
        # This keeps chip-0 and chip-1 masks identical.
        apod[0::2] = 1.0
    elif cfg["index"] == 3:
        # Profile 3: inverse alternating channels on across full aperture.
        # This keeps chip-0 and chip-1 masks identical.
        apod[1::2] = 1.0
    else:  # cfg["index"] == 4
        # Profile 4: all channels enabled.
        apod[:] = 1.0

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
# print(f"  Profiles pulse configurations: {multi_pulse_configs}")

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
    print("[OK] Grouped profile packages sent to firmware")
    
except Exception as e:
    print(f"[FAIL] Failed to set grouped profile solution: {e}")
    all_passed = False

# try:
#     if not write_and_readback_all_registers(interface):
#         all_passed = False
# except Exception as e:
#     print(f"[FAIL] Failed during write/readback stage: {e}")
#     all_passed = False

# Single integrated verification pass.
# print("\nIntegrated verification pass:")
# print("-" * SEPARATOR_LINE_WIDTH)

# HV setup for measured output changes during trigger execution.
if interface.hvcontroller is None:
    print("  [FAIL] HV controller not available; cannot validate HV-coupled trigger output changes")
    all_passed = False
else:
    print("  Turning on HV...")
    interface.hvcontroller.turn_12v_on()
    # interface.hvcontroller.turn_hv_on()
    # interface.hvcontroller.set_voltage(VOLTAGE)
    # interface.hvcontroller.wait_for_settle()
    hv_baseline = interface.hvcontroller.get_voltage()
    print(f"  HV readback: target={VOLTAGE:.1f}V measured={hv_baseline:.2f}V")




# Verify that initial profile is active (first configured profile)
initial_profile = 1
print(f"\nVerifying initial active profile: {initial_profile}")

init_delay_profiles = verify_delay_profile_selected(
    interface,
    initial_profile,
    "grouped package init",
    num_tx_devices,
)
print(f"  Initial per-chip delay profiles: {init_delay_profiles}")
if init_delay_profiles[0] != initial_profile:
    all_passed = False

# # Verify apodization was applied (this would require reading apod register from TX7332)
# print(f"  Apodization data for profile {initial_profile} stored in firmware MCU")

# Walk the grouped set once and verify each profile+HV+trigger combination.
print(f"\nRunning grouped profile/HV/trigger sequence once...")
profile_sequence = [cfg["index"] for cfg in PROFILE_CONFIGS]
print(f"  Sequence: {' -> '.join(map(str, profile_sequence))}")

# Configure trigger once - delay profile switching does not change trigger timing.
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
    num_tx_chips = num_tx_devices
    if len(delay_data_list) != num_tx_chips or len(delay_ctrl_list) != num_tx_chips:
        print(
            "  [FAIL] Enumerated TX chip count does not match register data: "
            f"enum={num_tx_chips}, delay_data={len(delay_data_list)}, delay_ctrl={len(delay_ctrl_list)}"
        )
        all_passed = False
        break

    # Store expected value from first chip and enforce both chips match.
    expected_apod_by_profile[profile] = delay_ctrl_list[0][ADDRESS_APODIZATION]
    expected_delay_sel_by_profile[profile] = delay_ctrl_list[0][ADDRESS_DELAY_SEL]

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

for chip_idx in range(num_tx_devices):
    interface.txdevice.commit_profile_ram(chip_idx)
print("  [OK] Preload committed to profile RAM for all chips")

# Force starting point after preload.
for chip_idx in range(num_tx_devices):
        interface.txdevice.write_register(chip_idx, ADDRESS_DELAY_SEL, expected_delay_sel_by_profile[initial_profile])
time.sleep(REG_PROPAGATION_DELAY_S)

# Switch through each profile by changing selector only, repeating the sequence 10 times.
SEQUENCE_REPEAT_COUNT = 10
active_trigger_time_s = 0.0
for cycle_idx in range(len(PROFILE_CONFIGS) * SEQUENCE_REPEAT_COUNT):
    expected_prof = profile_sequence[(cycle_idx + 1) % len(profile_sequence)]
    print(f"  Cycle {cycle_idx}: Switching to next profile {expected_prof}")

    pre_delay_reg = interface.txdevice.read_register(tx_id, ADDRESS_DELAY_SEL)
    pre_apod_reg = interface.txdevice.read_register(tx_id, ADDRESS_APODIZATION)
    pre_delay_profile = read_active_delay_profile(interface, num_tx_devices)
    print(
        "    Active before write: "
        f"delay={pre_delay_profile}, apod=0x{pre_apod_reg:08X}"
    )

    # Write delay selector to all TX chips (tell each chip which profile slot is active)
    for chip_idx in range(num_tx_devices):
        print(
            f"    Writing delay selector TX{chip_idx}: "
            f"0x{expected_delay_sel_by_profile[expected_prof]:08X}"
        )
        interface.txdevice.write_register(chip_idx, ADDRESS_DELAY_SEL, expected_delay_sel_by_profile[expected_prof])

    time.sleep(REG_PROPAGATION_DELAY_S)

    post_delay_reg = interface.txdevice.read_register(tx_id, ADDRESS_DELAY_SEL)
    post_apod_reg = interface.txdevice.read_register(tx_id, ADDRESS_APODIZATION)
    post_delay_profile = read_active_delay_profile(interface, num_tx_devices)
    print(
        "    Active after write: "
        f"delay={post_delay_profile}, apod=0x{post_apod_reg:08X}"
    )

    if post_delay_profile == pre_delay_profile:
        print("    [FAIL] Delay profile selector did not change after write")
        all_passed = False
    else:
        print("    [OK] Delay profile selector changed after write")

    if post_apod_reg != expected_apod_by_profile[expected_prof]:
        print(
            f"    [FAIL] Apodization register mismatch after switch (chip {tx_id}): "
            f"expected 0x{expected_apod_by_profile[expected_prof]:08X}, got 0x{post_apod_reg:08X}"
        )
        all_passed = False
    else:
        print(f"    [OK] Apodization register matches expected profile value (chip {tx_id})")

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
    
    delay_profiles = verify_delay_profile_selected(interface, expected_prof, f"cycle {cycle_idx}", num_tx_devices)
    print(f"    Cycle {cycle_idx} per-chip delay profiles: {delay_profiles}")
    if delay_profiles[0] != expected_prof:
        all_passed = False

    apod_values = verify_apodization_register(
        interface, expected_apod_by_profile[expected_prof], f"cycle {cycle_idx}", num_tx_devices
    )
    print(f"    Cycle {cycle_idx} per-chip apodization: {[f'0x{v:08X}' for v in apod_values]}")
    if apod_values[0] != expected_apod_by_profile[expected_prof]:
        all_passed = False

print("\n[OK] Grouped profile cycle test completed")
print("  (Firmware auto-cycling verified by manual profile selection)")
print(f"  Total active trigger time: {active_trigger_time_s:.2f}s")

if active_trigger_time_s >= MAX_ACTIVE_TRIGGER_TIME_S:
    print(
        f"  [FAIL] Active trigger time exceeds limit: "
        f"{active_trigger_time_s:.2f}s >= {MAX_ACTIVE_TRIGGER_TIME_S:.2f}s"
    )
    all_passed = False
else:
    print(
        f"  [OK] Active trigger time within limit: "
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

'''

def main():
    # Create the hardware interface used by this short readback test.
    interface = LIFUInterface()
    # Ensure 12V rail is on before programming profiles.
    if not interface.hvcontroller.get_12v_status():
        # Enable the 12V rail when it is currently off.
        interface.hvcontroller.turn_12v_on()
        # Allow hardware power rail to settle.
        time.sleep(2)

    # Enumerate TX chips if not already enumerated in this session.
    if interface.txdevice.tx_registers is None:
        # Query hardware and create register model.
        num_tx_devices = interface.txdevice.enum_tx7332_devices()
    else:
        # Reuse previously discovered device count.
        num_tx_devices = interface.txdevice.tx_registers.num_transmitters
    # Print detected device count for visibility.
    print(f"num_tx_devices={num_tx_devices}")

    print("testing new get and set profile functions")

    # print("before setting profile:")

    # print(f"pattern profile: {interface.txdevice.get_pattern_profile()}")
    # print(f"delay profile: {interface.txdevice.get_delay_profile()}")
    '''
        for i in range(MAX_NUM_PROFILES):
            print(f"i is {i}")
            print(f"Pattern profile is currently: {interface.txdevice.get_pattern_profile()}")
            interface.txdevice.set_pattern_profile(profile=i)
            print(f"Set pattern profile to {i}")
            print(f"New pattern profile: {interface.txdevice.get_pattern_profile()}")

            print(f"Delay profile: {interface.txdevice.get_delay_profile()}")
            interface.txdevice.set_delay_profile(profile=i)
            print(f"Set delay profile to {i}")
            print(f"New delay profile: {interface.txdevice.get_delay_profile()}")
    '''
    # Build a pulse configuration for set_solution.
    pulse = {
        # Set pulse frequency in Hz.
        "frequency": FIXED_FREQUENCY_HZ * 1e3,
        # Set pulse duration in seconds.
        "duration": DURATION_MS * 1e-3,
        # Use full-scale amplitude.
        "amplitude": 1.0,
    }
    # Program and verify exactly 4 profiles, matching the commented test section.
    profile_numbers = [1, 2, 3, 4]
    # Build one delay row per profile.
    delay_rows = [get_profile_delay_sequence(profile_number).reshape(-1)
                  for profile_number in profile_numbers]
    delays = np.array(delay_rows, dtype=float)

    print("delays to be written:")
    for idx, profile_number in enumerate(profile_numbers):
        print(f"  profile {profile_number}: first 8 channels={delays[idx][:8]}")

    # Build one apodization row per profile (all channels enabled).
    apodizations = []


    # make every other profile have alternating channels on/off for testing
    for cfg in PROFILE_CONFIGS:
        profile = cfg["index"]
        if profile % 2 == 0:
            apod = np.zeros(CHANNEL_COUNT, dtype=float)
        else:
            apod = np.ones(CHANNEL_COUNT, dtype=float)
        apodizations.append(apod)

    print("apodizations to be written:")
    for idx, profile_number in enumerate(profile_numbers):
        print(f"  profile {profile_number}: first 8 channels={apodizations[idx][:8]}")

    # Build a minimal trigger sequence dictionary required by set_solution.
    sequence = {
        # Time between pulses in seconds.
        "pulse_interval": 0.1,
        # Number of pulses per train.
        "pulse_count": 10,
        # Time between pulse trains in seconds.
        "pulse_train_interval": 1.0,
        # Number of pulse trains.
        "pulse_train_count": 1,
    }

    # Program all 4 profiles through set_solution.

    interface.txdevice.set_solution(
        # Provide pulse configuration.
        pulse=pulse,
        # Provide delay table.
        delays=delays,
        # Provide apodization table.
        apodizations=apodizations,
        # Provide trigger sequence.
        sequence=sequence,
        # Use sequence trigger mode.
        trigger_mode="sequence",
        # Activate profile 1 after programming.
        profile_index=1,
        # Enable increment so firmware can cycle profiles in order.
        profile_increment=True,
        # Explicit execution order for this 4-profile test.
        execution_order=profile_numbers,
    )

    # Commit profile RAM and apply all registers (matching commented section).
    interface.txdevice.commit_profile_ram()
    interface.txdevice.apply_all_registers()
    time.sleep(0.025)

    # Pre-compute delay selector register values for each profile.
    delay_sel_by_profile = {}
    for profile_number in profile_numbers:
        interface.txdevice.tx_registers.activate_delay_profile(profile_number) # this doesn't do anything??
        delay_ctrl_list = interface.txdevice.tx_registers.get_delay_control_registers()
        delay_sel_by_profile[profile_number] = delay_ctrl_list[0][ADDRESS_DELAY_SEL]

    interface.hvcontroller.set_voltage(VOLTAGE)
    interface.hvcontroller.turn_hv_on()

    while True:
        for profile_number in profile_numbers:
            # Write delay selector register directly to all TX devices (matching commented section).
            # for chip_idx in range(num_tx_devices):
            #     interface.txdevice.write_register(chip_idx, ADDRESS_DELAY_SEL, delay_sel_by_profile[profile_number])
            print("Current selected delay profile:", interface.txdevice.get_delay_profile())
            print("Setting delay profile to:", profile_number)
            interface.txdevice.set_delay_profile(profile=profile_number)
            print("New selected delay profile:", interface.txdevice.get_delay_profile())
            
            time.sleep(0.025)
            interface.txdevice.start_trigger()
            time.sleep(0.1)
            print(f"Profile {profile_number} triggered")
            time.sleep(1)
            interface.txdevice.stop_trigger()


    print("\n[OK] 4-profile test complete. Oscilloscope should show delay variations:")
    print("  Profiles 1, 3: 0 delay")
    print("  Profiles 2, 4: 100 us delay")

    bf_clk_hz = interface.txdevice.tx_registers.bf_clk
    tick_s = 1.0 / bf_clk_hz

    # Read back and verify each of the 4 profiles independently.
    for idx, profile_number in enumerate(profile_numbers):
        print(f"\nDelay profile readback for profile {profile_number}:")
        readback_delays = read_profile_delays_all_chips(
            interface=interface,
            profile_number=profile_number,
            num_tx_devices=num_tx_devices,
        )
        written_delays = delays[idx]
        expected_quantized = quantize_delays_to_device(written_delays, bf_clk_hz)

        if readback_delays.size != CHANNEL_COUNT:
            raise RuntimeError(
                f"Profile {profile_number}: expected {CHANNEL_COUNT} readback delays, got {readback_delays.size}"
            )

        abs_err = np.abs(readback_delays - expected_quantized)
        max_err = float(np.max(abs_err))
        max_err_idx = int(np.argmax(abs_err))

        print("full 64-channel compare (quantized):")
        print(f"  bf_clk_hz={bf_clk_hz}")
        print(f"  tick_s={tick_s:.3e}")
        print(f"  max_abs_error={max_err:.3e} at channel={max_err_idx + 1}")

        if np.any(abs_err > (tick_s + 1e-15)):
            mismatches = np.where(abs_err > (tick_s + 1e-15))[0]
            print(f"  [FAIL] profile {profile_number} mismatch count={len(mismatches)}")
            for ch_idx in mismatches[:10]:
                print(
                    f"    ch{ch_idx + 1}: written={written_delays[ch_idx]:.9e}, "
                    f"expected_quantized={expected_quantized[ch_idx]:.9e}, "
                    f"read={readback_delays[ch_idx]:.9e}, err={abs_err[ch_idx]:.3e}"
                )
            raise RuntimeError(
                f"Profile {profile_number}: delay readback mismatch exceeds one BF clock tick"
            )

        print(f"  [OK] profile {profile_number}: all 64 delays match quantized expected values")

if __name__ == "__main__":
    main()
# %%