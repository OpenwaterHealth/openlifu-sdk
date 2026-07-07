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

FIXED_FREQUENCY_HZ = 400
DURATION_MS = 5
CHANNEL_COUNT = 64
VOLTAGE = 20.0

PROFILE_DELAY_DISPLACEMENT_US = 10.0  # Delay step between profiles for test verification

DEBUG = 0

# Debug aid: keep all channels enabled on every profile to isolate
# physical output issues from apodization masking behavior.
FORCE_ALL_CHANNELS_ON = 0

# Temporary test switch: bypass profile-varying apodizations.
BYPASS_APODIZATIONS = 0

def get_profile_delay_sequence(profile_number: int) -> np.ndarray:
    """Return the profile delay sequence with PROFILE_DELAY_DISPLACEMENT_US us step per profile.

    Profile 1: 0 us
    Profile 2: 10 us
    Profile 3: 20 us
    Profile 4: 30 us
    """
    delay_us = (profile_number - 1) * PROFILE_DELAY_DISPLACEMENT_US
    return np.full((1, CHANNEL_COUNT), delay_us * 1e-6, dtype=float)


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

NUM_DELAY_PROFILES = 4  # Number of delay profiles to program (1-16)

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
    
    # Build a pulse configuration for set_solution.
    pulse = {
        # Set pulse frequency in Hz.
        "frequency": FIXED_FREQUENCY_HZ * 1e3,
        # Set pulse duration in seconds.
        "duration": DURATION_MS * 1e-3,
        # Use full-scale amplitude.
        "amplitude": 1.0,
    }

    # Build delay profile indices: [1, 2, ..., NUM_DELAY_PROFILES]
    profile_numbers = list(range(1, NUM_DELAY_PROFILES + 1))

    # Build one delay row per delay profile.
    delay_rows = [get_profile_delay_sequence(profile_number).reshape(-1)
                  for profile_number in profile_numbers]
    delays = np.array(delay_rows, dtype=float)
    print_delays(delays)

    apodizations = create_apodizations(profile_numbers)
    print_apodizations(apodizations)

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

    # Program and run one explicit execution order for oscilloscope verification.
    # execution order is a dynamic list of however many profiles you want to run
    execution_order = list(range(1, len(profile_numbers) + 1))

    interface.hvcontroller.set_voltage(VOLTAGE)

    print(f"execution_order = {execution_order}")

    # Single pulse config is passed as a dict; set_solution will create one
    # pulse profile (index 1) and map all delay profiles to it automatically.
    interface.txdevice.set_solution(
        pulse=pulse,
        delays=delays,
        apodizations=apodizations,
        sequence=sequence,
        trigger_mode="sequence",
        profile_index=1,
        profile_increment=True,
        execution_order=execution_order,
    )

    if DEBUG:
        read_stored_delay_values(
            interface=interface,
            profile_numbers=profile_numbers,
            num_tx_devices=num_tx_devices,
            delays=delays,
        )
        read_stored_pattern_values(
            interface=interface,
            profile_numbers=profile_numbers,
            num_tx_devices=num_tx_devices,
        )

    interface.start_sonication()

# create apodizations for each profile
def create_apodizations(profile_numbers: list[int]) -> np.ndarray:
    apodizations = []
    for profile_number in profile_numbers:
        if BYPASS_APODIZATIONS:
            # Bypass apodization masking: all channels enabled for every profile.
            apod = np.ones(CHANNEL_COUNT, dtype=float)
            apodizations.append(apod)
            continue
        apod = np.zeros(CHANNEL_COUNT, dtype=float)
        # Enable one 16-channel block per profile: 0-15, 16-31, 32-47, 48-63.
        block_start = (16 * (profile_number - 1)) % CHANNEL_COUNT
        block_end = block_start + 16
        apod[block_start:block_end] = 1.0
        apodizations.append(apod)
    
    if 1:
        print("apodizations:")
        for idx, apod in enumerate(apodizations):
            print(f"Profile {idx + 1}: {apod}")
    return np.array(apodizations, dtype=float)

# print delays in 8x8 grid
def print_delays(delays: np.ndarray) -> None:
    for idx, delay in enumerate(delays):
        print(f"Profile {idx + 1} delays:")
        for row in range(8):
            start = row * 8
            end = start + 8
            print("  " + " ".join(f"{val:.6e}" for val in delay[start:end]))
        print()

# print apodizations in 8x8 grid
def print_apodizations(apodizations: np.ndarray) -> None:
    for idx, apod in enumerate(apodizations):
        print(f"Profile {idx + 1} apodization:")
        for row in range(8):
            start = row * 8
            end = start + 8
            print("  " + " ".join(f"{int(val)}" for val in apod[start:end]))
        print()

def read_stored_delay_values(interface: LIFUInterface,
                             profile_numbers: list[int],
                             num_tx_devices: int,
                             delays: np.ndarray) -> np.ndarray:
    # Implement the function to read stored delay values from the device
    bf_clk_hz = interface.txdevice.tx_registers.bf_clk
    tick_s = 1.0 / bf_clk_hz
    all_readback_delays = []

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

        all_readback_delays.append(readback_delays)

    return np.array(all_readback_delays, dtype=float)


PATTERN_DATA_START = 0x120
PATTERN_PROFILE_REGS = 4  # 4 registers per pattern profile block in TX7332 RAM
# Only the first 2 registers hold actual pattern period data; the rest are
# unused padding within the block and may contain stale RAM values.
PATTERN_DATA_REGS = 2


def read_stored_pattern_values(
    interface: LIFUInterface,
    profile_numbers: list[int],
    num_tx_devices: int,
) -> None:
    """Read pattern RAM for each profile from each TX chip and compare across profiles.

    Only the first PATTERN_DATA_REGS registers per profile slot contain
    meaningful pattern data; the remaining registers in the 4-register block
    are unused and may hold stale values.
    """
    # Read all profiles from all chips first.
    all_data: dict[int, list[list[int]]] = {}  # profile -> [chip0_regs, chip1_regs, ...]
    for profile_number in profile_numbers:
        start_addr = PATTERN_DATA_START + (profile_number - 1) * PATTERN_PROFILE_REGS
        chips = []
        for txi in range(num_tx_devices):
            regs = interface.txdevice.read_block(
                identifier=txi,
                start_address=start_addr,
                count=PATTERN_PROFILE_REGS,
            )
            chips.append(regs)
        all_data[profile_number] = chips

    # Print and compare.
    reference_data_regs = None
    print("\n========== PATTERN RAM READBACK ==========")
    for profile_number in profile_numbers:
        for txi, regs in enumerate(all_data[profile_number]):
            hex_regs = [f"0x{r:08X}" for r in regs]
            print(f"  pattern profile {profile_number}, tx={txi}: {hex_regs}")

        # Use profile 1 / chip 0 as the reference (data registers only).
        if reference_data_regs is None:
            reference_data_regs = all_data[profile_number][0][:PATTERN_DATA_REGS]

        # Compare only the meaningful data registers against the reference.
        for txi, regs in enumerate(all_data[profile_number]):
            data_regs = regs[:PATTERN_DATA_REGS]
            if data_regs != reference_data_regs:
                print(
                    f"  [WARN] profile {profile_number} tx={txi} pattern data DIFFERS from reference"
                )
                ref_hex = [f"0x{r:08X}" for r in reference_data_regs]
                cur_hex = [f"0x{r:08X}" for r in data_regs]
                print(f"    reference: {ref_hex}")
                print(f"    actual:    {cur_hex}")
            else:
                print(f"  [OK] profile {profile_number} tx={txi} matches reference")

    # Also read and print the control registers (DELAY_SEL, APODIZATION, PATTERN_SEL).
    print("\n========== CONTROL REGISTER READBACK ==========")
    for txi in range(num_tx_devices):
        delay_sel = interface.txdevice.read_register(txi, 0x16)
        apod = interface.txdevice.read_register(txi, 0x1B)
        pat_sel_g1 = interface.txdevice.read_register(txi, 0x1F)
        pat_sel_g2 = interface.txdevice.read_register(txi, 0x1E)
        print(
            f"  tx={txi}: DELAY_SEL=0x{delay_sel:08X}  APOD=0x{apod:08X}  "
            f"PAT_SEL_G1=0x{pat_sel_g1:08X}  PAT_SEL_G2=0x{pat_sel_g2:08X}"
        )


if __name__ == "__main__":
    main()
# %%