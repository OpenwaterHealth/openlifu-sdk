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
"""QA verification for set_solution() single-profile and multi-profile paths.

Tests verify register-level correctness of delays, apodizations, and pattern
data written to TX7332 devices. Each test calls set_solution(), reads back
hardware registers, and asserts correctness.

Test cases:
    TC1: Single profile, backward compatible (no execution_order).
    TC2: Single profile with explicit execution_order=[1].
    TC3: Multiple delay profiles with shared pulse config.
    TC4: Apodization register verification per profile.
    TC5: Maximum 16 delay profiles.
    TC6: Single-channel scan for oscilloscope verification.
"""

from __future__ import annotations

import sys
import time
import traceback
from typing import Sequence

import numpy as np

from openlifu_sdk import LIFUInterface

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHANNEL_COUNT = 64
CHANNELS_PER_CHIP = 32
VOLTAGE = 20.0

# Default pulse parameters shared by all tests.
DEFAULT_FREQUENCY_HZ = 400e3
DEFAULT_DURATION_S = 5e-3

# Sonication duration per test case (seconds). Longer for tests with more
# profiles so the oscilloscope operator has time to observe cycling.
SONICATION_DURATION_S: dict[str, float] = {
    "TC1": 10.0,
    "TC2": 10.0,
    "TC3": 120.0,
    "TC4": 20.0,
    "TC5": 30.0,
    "TC6": 120.0,
}

# Pattern RAM layout.
PATTERN_DATA_START = 0x120
PATTERN_PROFILE_BLOCK = 4  # Registers per profile block in TX7332 RAM.
PATTERN_DATA_REGS = 2  # Only the first 2 hold actual pattern data.

# TX7332 register addresses.
ADDR_DELAY_SEL = 0x16
ADDR_APODIZATION = 0x1B
ADDR_PATTERN_SEL_G1 = 0x1F
ADDR_PATTERN_SEL_G2 = 0x1E

# ---------------------------------------------------------------------------
# Delay generation
# ---------------------------------------------------------------------------


def generate_per_channel_delays(
    profile_number: int,
    channel_count: int = CHANNEL_COUNT,
    base_step_us: float = 10.0,
    channel_step_ns: float = 100.0,
) -> np.ndarray:
    """Generate a delay vector with unique per-channel values.

    Each profile has a distinct base delay, and each channel within the
    profile adds a small linear offset so that every register value is
    unique and independently verifiable.

    Args:
        profile_number: 1-based profile index.
        channel_count: Number of channels (typically 64).
        base_step_us: Microsecond step between profiles.
        channel_step_ns: Nanosecond increment per channel within a profile.

    Returns:
        1-D array of delays in seconds, shape (channel_count,).
    """
    base_s = (profile_number - 1) * base_step_us * 1e-6
    channel_offsets_s = np.arange(channel_count) * channel_step_ns * 1e-9
    return base_s + channel_offsets_s


def quantize_delays(delays_s: np.ndarray, bf_clk_hz: float) -> np.ndarray:
    """Quantize delays to device resolution (truncation to clock ticks)."""
    return np.floor(delays_s * bf_clk_hz) / bf_clk_hz


# ---------------------------------------------------------------------------
# Apodization helpers
# ---------------------------------------------------------------------------


def make_block_apodizations(
    num_profiles: int,
    channel_count: int = CHANNEL_COUNT,
) -> np.ndarray:
    """Create apodizations with one 16-channel block per profile.

    Profile k enables channels [(k-1)*16 .. k*16), wrapping modulo
    channel_count.

    Returns:
        Array of shape (num_profiles, channel_count).
    """
    apodizations = np.zeros((num_profiles, channel_count), dtype=float)
    for k in range(num_profiles):
        start = (16 * k) % channel_count
        apodizations[k, start:start + 16] = 1.0
    return apodizations


def make_single_channel_apodizations(
    channels: Sequence[int],
    channel_count: int = CHANNEL_COUNT,
) -> np.ndarray:
    """Create apodizations that enable exactly one channel per profile.

    Args:
        channels: 1-based global channel numbers, one per profile.
        channel_count: Total channels.

    Returns:
        Array of shape (len(channels), channel_count).
    """
    apodizations = np.zeros((len(channels), channel_count), dtype=float)
    for i, ch in enumerate(channels):
        apodizations[i, ch - 1] = 1.0
    return apodizations


def make_all_on_apodizations(
    num_profiles: int,
    channel_count: int = CHANNEL_COUNT,
) -> np.ndarray:
    """All channels enabled for every profile."""
    return np.ones((num_profiles, channel_count), dtype=float)


# ---------------------------------------------------------------------------
# Shared pulse / sequence builders
# ---------------------------------------------------------------------------


def make_default_pulse() -> dict:
    """Standard pulse config used across tests."""
    return {
        "frequency": DEFAULT_FREQUENCY_HZ,
        "duration": DEFAULT_DURATION_S,
        "amplitude": 1.0,
    }


def make_default_sequence() -> dict:
    """Standard trigger sequence used across tests."""
    # return {
    #     "pulse_interval": 0.5,
    #     "pulse_count": 20,
    #     "pulse_train_interval": 0,
    #     "pulse_train_count": 3,
    # }

    return {
        "pulse_interval": .1,
        "pulse_count": 16,
        "pulse_train_interval": 0,
        "pulse_train_count": 5,
    }

# ---------------------------------------------------------------------------
# Register readback and verification
# ---------------------------------------------------------------------------


def verify_delays(
    interface: LIFUInterface,
    profile_numbers: list[int],
    expected_delays: np.ndarray,
    num_tx: int,
) -> bool:
    """Read delay RAM for each profile and verify against expected values.

    Returns True if all profiles match within one clock tick.
    """
    bf_clk_hz = interface.txdevice.tx_registers.bf_clk
    tick_s = 1.0 / bf_clk_hz
    passed = True

    for idx, profile_number in enumerate(profile_numbers):
        readback = _read_delays_all_chips(
            interface, profile_number, num_tx,
        )
        expected = quantize_delays(expected_delays[idx], bf_clk_hz)

        if readback.size != CHANNEL_COUNT:
            print(f"  [FAIL] profile {profile_number}: "
                  f"expected {CHANNEL_COUNT} channels, got {readback.size}")
            passed = False
            continue

        abs_err = np.abs(readback - expected)
        max_err = float(np.max(abs_err))
        max_err_ch = int(np.argmax(abs_err))

        if np.any(abs_err > tick_s + 1e-15):
            mismatches = np.where(abs_err > (tick_s + 1e-15))[0]
            print(f"  [FAIL] profile {profile_number}: "
                  f"{len(mismatches)} channel(s) exceed 1-tick tolerance")
            for ch in mismatches[:5]:
                print(f"    ch{ch + 1}: expected={expected[ch]:.9e}  "
                      f"read={readback[ch]:.9e}  err={abs_err[ch]:.3e}")
            passed = False
        else:
            print(f"  [OK] profile {profile_number}: all {CHANNEL_COUNT} "
                  f"delays match (max_err={max_err:.3e} at ch{max_err_ch + 1})")

    return passed


def verify_pattern_ram(
    interface: LIFUInterface,
    profile_numbers: list[int],
    num_tx: int,
) -> bool:
    """Verify pattern RAM data registers are identical across all slots.

    Only compares the first PATTERN_DATA_REGS registers (the meaningful ones).
    Returns True if all match.
    """
    reference = None
    passed = True

    print("  Pattern RAM (data registers only):")
    for profile_number in profile_numbers:
        start_addr = (
            PATTERN_DATA_START + (profile_number - 1) * PATTERN_PROFILE_BLOCK
        )
        for txi in range(num_tx):
            regs = interface.txdevice.read_block(
                identifier=txi,
                start_address=start_addr,
                count=PATTERN_PROFILE_BLOCK,
            )
            data_regs = regs[:PATTERN_DATA_REGS]
            hex_str = " ".join(f"0x{r:08X}" for r in data_regs)

            if reference is None:
                reference = data_regs
                print(f"    profile {profile_number} tx={txi}: "
                      f"{hex_str} (reference)")
            elif data_regs != reference:
                ref_hex = " ".join(f"0x{r:08X}" for r in reference)
                print(f"    profile {profile_number} tx={txi}: "
                      f"{hex_str} [FAIL] differs from reference {ref_hex}")
                passed = False
            else:
                print(f"    profile {profile_number} tx={txi}: "
                      f"{hex_str} [OK]")

    return passed


def verify_control_registers(
    interface: LIFUInterface,
    expected_profile_index: int,
    num_tx: int,
) -> bool:
    """Verify DELAY_SEL and PATTERN_SEL reflect the expected active profile.

    Both selectors are 0-based in the TX7332 hardware.
    """
    expected_delay_g1 = (expected_profile_index - 1) << 28
    expected_delay_g2 = (expected_profile_index - 1) << 12
    expected_delay_sel = expected_delay_g1 | expected_delay_g2
    expected_pat_sel = expected_profile_index - 1
    passed = True

    for txi in range(num_tx):
        delay_sel = interface.txdevice.read_register(txi, ADDR_DELAY_SEL)
        pat_g1 = interface.txdevice.read_register(txi, ADDR_PATTERN_SEL_G1)
        pat_g2 = interface.txdevice.read_register(txi, ADDR_PATTERN_SEL_G2)

        ok_delay = delay_sel == expected_delay_sel
        ok_pat = (pat_g1 == expected_pat_sel) and (pat_g2 == expected_pat_sel)

        status = "[OK]" if (ok_delay and ok_pat) else "[FAIL]"
        print(f"    tx={txi}: DELAY_SEL=0x{delay_sel:08X} "
              f"PAT_G1=0x{pat_g1:08X} PAT_G2=0x{pat_g2:08X}  {status}")

        if not ok_delay:
            print(f"      expected DELAY_SEL=0x{expected_delay_sel:08X}")
            passed = False
        if not ok_pat:
            print(f"      expected PAT_SEL=0x{expected_pat_sel:08X}")
            passed = False

    return passed


def verify_apodization_register(
    interface: LIFUInterface,
    profile: int,
    num_tx: int,
) -> bool:
    """Read the APODIZATION register from each TX chip and compare.

    Uses the SDK's own get_delay_control_registers() to obtain the expected
    register value, then compares against hardware readback.
    """
    passed = True
    for txi in range(num_tx):
        expected_regs = (
            interface.txdevice.tx_registers.transmitters[txi]
            .get_delay_control_registers(profile)
        )
        expected = expected_regs[ADDR_APODIZATION]
        actual = interface.txdevice.read_register(txi, ADDR_APODIZATION)

        if actual == expected:
            print(f"    tx={txi}: APOD=0x{actual:08X}  [OK]")
        else:
            print(f"    tx={txi}: APOD=0x{actual:08X}  "
                  f"expected=0x{expected:08X}  [FAIL]")
            diff = actual ^ expected
            differing_bits = [b for b in range(32) if diff & (1 << b)]
            print(f"      differing bits: {differing_bits}")
            passed = False

    return passed


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_delays_all_chips(
    interface: LIFUInterface,
    profile_number: int,
    num_tx: int,
) -> np.ndarray:
    """Read delay profile values from all TX chips in global channel order.

    The SDK maps txi=0 to global channels 32-63 and txi=1 to global
    channels 0-31 (mirroring Tx7332System.add_delay_profile). This
    function places each chip's readback at the correct global positions.
    """
    total = num_tx * CHANNELS_PER_CHIP
    all_delays = np.zeros(total, dtype=float)
    for txi in range(num_tx):
        result = interface.txdevice.read_delay_profile_value(
            profile_number=profile_number,
            identifier=txi,
            units="s",
        )
        module = txi // 2
        chip = (txi + 1) % 2
        start = module * CHANNELS_PER_CHIP * 2 + chip * CHANNELS_PER_CHIP
        all_delays[start:start + CHANNELS_PER_CHIP] = result["delays"]
    return all_delays


def _setup_hardware(interface: LIFUInterface) -> int:
    """Ensure 12 V rail and TX enumeration are ready. Returns num_tx."""
    if not interface.hvcontroller.get_12v_status():
        interface.hvcontroller.turn_12v_on()
        time.sleep(2)

    if interface.txdevice.tx_registers is None:
        num_tx = interface.txdevice.enum_tx7332_devices()
    else:
        num_tx = interface.txdevice.tx_registers.num_transmitters

    interface.hvcontroller.set_voltage(VOLTAGE)
    return num_tx


def _print_header(name: str) -> None:
    """Print a test case header."""
    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print(f"{'=' * 60}")


def _run_sonication(
    interface: LIFUInterface,
    tc_name: str,
    description: str,
) -> None:
    """Start sonication, wait for the configured duration, then stop."""
    duration = SONICATION_DURATION_S.get(tc_name, 5.0)
    print(f"\n  Sonication: {description}")
    print(f"  Duration: {duration}s — observe on oscilloscope.")
    interface.start_sonication()
    time.sleep(duration)
    interface.stop_sonication()
    print("  Sonication stopped.")


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_single_profile_backward_compat(
    interface: LIFUInterface,
    num_tx: int,
) -> bool:
    """TC1: Single delay profile, no execution_order (backward compatible).

    Verifies the n=1 path that existing callers (burn_in_test, single_pulse,
    Slicer app) rely on. No grouped profile cycle is sent to firmware.
    """
    _print_header("TC1: Single profile - backward compatible (all channels on)")

    delays = generate_per_channel_delays(profile_number=1).reshape(1, -1)
    apodizations = make_all_on_apodizations(num_profiles=1)
    pulse = make_default_pulse()
    sequence = make_default_sequence()

    interface.txdevice.set_solution(
        pulse=pulse,
        delays=delays,
        apodizations=apodizations,
        sequence=sequence,
        trigger_mode="sequence",
        profile_index=1,
        profile_increment=False,
    )

    passed = True
    print("  Delay verification:")
    passed &= verify_delays(interface, [1], delays, num_tx)
    print("  Control register verification:")
    passed &= verify_control_registers(interface, 1, num_tx)
    print("  Apodization register verification:")
    passed &= verify_apodization_register(interface, 1, num_tx)
    print("  Pattern RAM verification:")
    passed &= verify_pattern_ram(interface, [1], num_tx)

    if passed:
        _run_sonication(interface, "TC1", "1 profile, all 64 channels on")

    return passed


def test_single_profile_with_execution_order(
    interface: LIFUInterface,
    num_tx: int,
) -> bool:
    """TC2: Single profile with explicit execution_order=[1].

    Verifies n=1 still takes the single-profile path even when
    execution_order is provided.
    """
    _print_header("TC2: Single profile with execution_order=[1]")

    delays = generate_per_channel_delays(profile_number=1).reshape(1, -1)
    apodizations = make_all_on_apodizations(num_profiles=1)
    pulse = make_default_pulse()
    sequence = make_default_sequence()

    interface.txdevice.set_solution(
        pulse=pulse,
        delays=delays,
        apodizations=apodizations,
        sequence=sequence,
        trigger_mode="sequence",
        profile_index=1,
        profile_increment=False,
        execution_order=[1],
    )

    passed = True
    print("  Delay verification:")
    passed &= verify_delays(interface, [1], delays, num_tx)
    print("  Control register verification:")
    passed &= verify_control_registers(interface, 1, num_tx)
    print("  Apodization register verification:")
    passed &= verify_apodization_register(interface, 1, num_tx)

    if passed:
        _run_sonication(interface, "TC2", "1 profile with execution_order=[1]")

    return passed


def test_multi_profile_shared_pulse(
    interface: LIFUInterface,
    num_tx: int,
) -> bool:
    """TC3: Four delay profiles with distinct per-channel delays.

    All profiles share one pulse config and have all channels enabled.
    Verifies that each profile's delay RAM has unique, correct values and
    that pattern RAM is replicated identically across all slots.
    """
    _print_header("TC3: Multi-profile shared pulse - 4 profiles, all channels on")

    num_profiles = 4
    profile_numbers = list(range(1, num_profiles + 1))

    delays = np.array([
        generate_per_channel_delays(p) for p in profile_numbers
    ])
    apodizations = make_all_on_apodizations(num_profiles)
    pulse = make_default_pulse()
    sequence = make_default_sequence()
    execution_order = list(profile_numbers)
    # execution_order = [1,4,1,4,2,3,2,3]

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

    passed = True
    print("  Delay verification (per-channel unique values):")
    passed &= verify_delays(interface, profile_numbers, delays, num_tx)
    print("  Control register verification (active profile = 1):")
    passed &= verify_control_registers(interface, 1, num_tx)
    print("  Apodization register verification (all channels on):")
    passed &= verify_apodization_register(interface, 1, num_tx)
    print("  Pattern RAM verification (all slots identical):")
    passed &= verify_pattern_ram(interface, profile_numbers, num_tx)

    if passed:
        _run_sonication(interface, "TC3", "4 profiles cycling, all channels on")

    return passed


def test_apodization_per_profile(
    interface: LIFUInterface,
    num_tx: int,
) -> bool:
    """TC4: Four profiles with 16-channel block apodizations.

    Verifies that the initial active profile's apodization register is
    correctly written. Each profile enables a different 16-channel block.
    """
    _print_header("TC4: Apodization register - 4 profiles, 16-channel blocks")

    num_profiles = 4
    profile_numbers = list(range(1, num_profiles + 1))

    delays = np.array([
        generate_per_channel_delays(p) for p in profile_numbers
    ])
    apodizations = make_block_apodizations(num_profiles)
    pulse = make_default_pulse()
    sequence = make_default_sequence()
    execution_order = list(profile_numbers)

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

    passed = True
    print("  Delay verification:")
    passed &= verify_delays(interface, profile_numbers, delays, num_tx)
    print("  Apodization register for active profile (profile 1):")
    print(f"    Expected enabled channels: 0-15")
    passed &= verify_apodization_register(interface, 1, num_tx)

    if passed:
        _run_sonication(
            interface, "TC4",
            "4 profiles cycling, 16-channel blocks (ch0-15, 16-31, 32-47, 48-63)",
        )

    return passed


def test_single_channel_scan(
    interface: LIFUInterface,
    num_tx: int,
) -> bool:
    """TC6: Sequential single-channel profiles for oscilloscope verification.

    Creates one profile per selected channel. Each profile enables exactly
    one channel. After programming, starts sonication so the user can
    observe each channel activating in sequence on an oscilloscope.

    Channels tested: 1, 2, 63, 64.
    """
    _print_header("TC6: Single-channel scan - 8 profiles, one channel each")

    scan_channels = [1, 2, 63, 64]
    num_profiles = len(scan_channels)
    profile_numbers = list(range(1, num_profiles + 1))

    delays = np.array([
        generate_per_channel_delays(p) for p in profile_numbers
    ])
    apodizations = make_single_channel_apodizations(scan_channels)
    pulse = make_default_pulse()
    sequence = make_default_sequence()
    execution_order = list(profile_numbers)

    print(f"  Channels under test: {scan_channels}")
    print(f"  Profiles: {num_profiles}, execution_order: {execution_order}")

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

    passed = True
    print("  Delay verification:")
    passed &= verify_delays(interface, profile_numbers, delays, num_tx)
    print("  Apodization register for active profile "
          "(profile 1, ch 1 only):")
    passed &= verify_apodization_register(interface, 1, num_tx)
    print("  Pattern RAM verification:")
    passed &= verify_pattern_ram(interface, profile_numbers, num_tx)

    if passed:
        _run_sonication(
            interface, "TC6",
            f"8 profiles cycling, one channel each: {scan_channels}",
        )

    return passed


def test_max_profiles(
    interface: LIFUInterface,
    num_tx: int,
) -> bool:
    """TC5: Maximum 16 delay profiles.

    Verifies the boundary condition with all 16 profile slots populated.
    """
    _print_header("TC5: Maximum profiles - 16 profiles, all channels on")

    num_profiles = 16
    profile_numbers = list(range(1, num_profiles + 1))

    delays = np.array([
        generate_per_channel_delays(p) for p in profile_numbers
    ])
    apodizations = make_all_on_apodizations(num_profiles)
    pulse = make_default_pulse()
    sequence = make_default_sequence()
    execution_order = list(profile_numbers)

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

    passed = True
    print("  Delay verification (16 profiles x 64 channels):")
    passed &= verify_delays(interface, profile_numbers, delays, num_tx)
    print("  Control register verification:")
    passed &= verify_control_registers(interface, 1, num_tx)
    print("  Pattern RAM verification (all 16 slots):")
    passed &= verify_pattern_ram(interface, profile_numbers, num_tx)

    if passed:
        _run_sonication(interface, "TC5", "16 profiles cycling, all channels on")

    return passed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Run all test cases and report results."""
    interface = LIFUInterface()
    num_tx = _setup_hardware(interface)

    # Ensure no stale sonication or auto-cycle is active from a previous run.
    interface.stop_sonication(turn_hv_off=False)

    tests = [
        ("TC1", test_single_profile_backward_compat),
        ("TC2", test_single_profile_with_execution_order),
        ("TC3", test_multi_profile_shared_pulse),
        ("TC4", test_apodization_per_profile),
        ("TC5", test_max_profiles),
        ("TC6", test_single_channel_scan),
    ]

    test_multi_profile_shared_pulse(interface, num_tx)

    results: dict[str, str] = {}
    for name, test_fn in tests:
        try:
            passed = test_fn(interface, num_tx)
            results[name] = "PASS" if passed else "FAIL"
        except Exception:
            traceback.print_exc()
            results[name] = "ERROR"

    # Summary.
    print(f"\n{'=' * 60}")
    print("  TEST SUMMARY")
    print(f"{'=' * 60}")
    for name, result in results.items():
        marker = "PASS" if result == "PASS" else "FAIL"
        print(f"  [{marker}] {name}: {result}")

    total = len(results)
    passed_count = sum(1 for r in results.values() if r == "PASS")
    print(f"\n  {passed_count}/{total} passed")

    if passed_count < total:
        sys.exit(1)


if __name__ == "__main__":
    main()
# %%
