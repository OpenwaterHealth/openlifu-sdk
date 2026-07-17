"""Rastered-focusing QA: 16 computed delay profiles tracing a circle of foci.

Unlike test_multiple_profiles.py (which uses synthetic per-channel delays for
register verification), this script computes real time-of-flight focusing
delays — the same math as OpenLIFU-TestAPP's ControllerMixin.get_solution() —
so each of the 16 delay profiles steers the beam to one point: profile 1
focuses on-axis at (0, 0, z) as a fixed center reference, and profiles 2-16
trace a circle around it:

    focus_k = (r*cos(theta_k), r*sin(theta_k), z),  theta_k = 2*pi*k/15

    distances = |focus - element_positions|          (mm, from pinmap)
    tof       = distances * 1e-3 / SPEED_OF_SOUND    (s)
    delays    = tof.max() - tof                       (per-profile normalized)

The firmware cycles through the profiles at pulse boundaries
(execution_order=[1..16]), so each sweep marks the center first and then
walks the focal spot around the circle.

Setup mirrors test_multiple_profiles.py: 12 V rail, TX enumeration, and
register readback verification before sonication. Once the profiles are
loaded and verified, sonication is interactive — press Enter to start,
Enter again to stop, and q to quit; the loaded profiles are reused across
restarts without reloading.

Requires pinmap_1x.json (element positions, mm) next to this script.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

import numpy as np

from openlifu_sdk import LIFUInterface

from test_multiple_profiles import (
    CHANNEL_COUNT,
    _print_header,
    _setup_hardware,
    compute_sonication_duration,
    make_all_on_apodizations,
    make_default_pulse,
    verify_apodization_register,
    verify_control_registers,
    verify_delays,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Time-of-flight medium. Matches SPEED_OF_SOUND in OpenLIFU-TestAPP
# (lifu/lifu_constants.py).
SPEED_OF_SOUND_M_S = 1500.0

# Focal pattern: 1 on-axis center focus + (NUM_FOCI - 1) points on a circle.
NUM_FOCI = 16
CIRCLE_RADIUS_MM = 10.0

# Focal depth by test medium; the user picks one at startup ([1] or [2]).
FOCAL_DEPTHS_MM = {
    "phantom": 52.0,   # thermal paper on phantom
    "water": 44.4,     # water-tank test
}

# Element positions (mm) for the 1x (single-module, 64-element) array.
PINMAP_PATH = Path(__file__).resolve().parent / "pinmap_1x.json"

# One pulse per focus per train; 100 trains sweeps the circle 100 times.
CIRCLE_SEQUENCE: dict = {
    "pulse_interval": 0.01,
    "pulse_count": NUM_FOCI*200,
    "pulse_train_interval": 3,
    "pulse_train_count": 1,
}

# ---------------------------------------------------------------------------
# Geometry and delay computation
# ---------------------------------------------------------------------------


def load_element_positions(pinmap_path: Path = PINMAP_PATH) -> np.ndarray:
    """Load element positions (mm) from a flat pinmap JSON.

    Element list order defines the channel order of the delay vectors, the
    same convention OpenLIFU-TestAPP uses when passing delays to
    set_solution().

    Args:
        pinmap_path: Path to the pinmap JSON file.

    Returns:
        Array of shape (n_elements, 3) with element positions in mm.

    Raises:
        FileNotFoundError: If the pinmap file is missing.
        ValueError: For multi-module ("TransducerArray") pinmaps or an
            unexpected element count.
    """
    if not pinmap_path.exists():
        raise FileNotFoundError(
            f"Pinmap not found: {pinmap_path}. Copy pinmap_1x.json next to "
            f"this script (see OpenLIFU-TestAPP repo root)."
        )
    with open(pinmap_path, encoding="utf-8") as f:
        pinmap = json.load(f)

    if pinmap.get("type") == "TransducerArray":
        raise ValueError(
            "Multi-module (TransducerArray) pinmaps are not supported by this "
            "script yet; use the single-module pinmap_1x.json."
        )

    positions = np.array([elem["position"] for elem in pinmap["elements"]],
                         dtype=float)
    if positions.shape != (CHANNEL_COUNT, 3):
        raise ValueError(
            f"Expected {CHANNEL_COUNT} elements with 3-D positions, "
            f"got shape {positions.shape}"
        )
    return positions


def make_foci(
    depth_mm: float,
    radius_mm: float = CIRCLE_RADIUS_MM,
    num_foci: int = NUM_FOCI,
) -> np.ndarray:
    """Generate an on-axis focus followed by a circle of focal points.

    The first focus sits at (0, 0, depth_mm) — a fixed center reference that
    also anchors position calibration (its mark gives the lateral offset of
    the whole pattern; the first circle point at +x gives the rotation). The
    remaining num_foci - 1 points are evenly spaced on the circle, starting
    at angle 0 (+x) and proceeding counter-clockwise.

    Args:
        depth_mm: Focal depth in mm (z, normal to the array face).
        radius_mm: Circle radius in mm (lateral steering distance).
        num_foci: Total number of foci (1 center + num_foci - 1 on the circle).

    Returns:
        Array of shape (num_foci, 3) with focus coordinates in mm.
    """
    n_circle = num_foci - 1
    angles = 2.0 * np.pi * np.arange(n_circle) / n_circle
    circle = np.column_stack([
        radius_mm * np.cos(angles),
        radius_mm * np.sin(angles),
        np.full(n_circle, depth_mm),
    ])
    center = np.array([[0.0, 0.0, depth_mm]])
    return np.vstack([center, circle])


def compute_focus_delays(
    focus_mm: np.ndarray,
    element_positions_mm: np.ndarray,
    speed_of_sound_m_s: float = SPEED_OF_SOUND_M_S,
) -> np.ndarray:
    """Compute per-element focusing delays for a single focal point.

    Same time-of-flight math as OpenLIFU-TestAPP get_solution(): the farthest
    element fires first (zero delay reference is the maximum time of flight).

    Args:
        focus_mm: Focus coordinates (3,) in mm.
        element_positions_mm: Element positions (n, 3) in mm.
        speed_of_sound_m_s: Speed of sound in the medium.

    Returns:
        Delay vector (n,) in seconds, one entry per element in pinmap order.
    """
    distances_mm = np.sqrt(np.sum((focus_mm - element_positions_mm) ** 2, axis=1))
    tof = distances_mm * 1e-3 / speed_of_sound_m_s
    return tof.max() - tof


def compute_circle_delay_profiles(
    element_positions_mm: np.ndarray,
    foci_mm: np.ndarray,
) -> np.ndarray:
    """Compute one delay profile per focal point.

    Returns:
        Array of shape (num_foci, n_elements) in seconds; row k focuses the
        beam at foci_mm[k].
    """
    return np.array([
        compute_focus_delays(focus, element_positions_mm) for focus in foci_mm
    ])


# ---------------------------------------------------------------------------
# Interactive sonication control
# ---------------------------------------------------------------------------


def _poll_key() -> str | None:
    """Non-blocking key check; returns "enter", "q", "temp", or None.

    Uses raw key reads on Windows (msvcrt); falls back to select() on stdin
    elsewhere (type q or t + Enter, bare Enter to start/stop).
    """
    if os.name == "nt":
        import msvcrt
        while msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b"\r", b"\n"):
                return "enter"
            if ch in (b"q", b"Q"):
                return "q"
            if ch in (b"t", b"T"):
                return "temp"
        return None

    import select
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if ready:
        line = sys.stdin.readline().strip().lower()
        if line == "q":
            return "q"
        if line == "t":
            return "temp"
        return "enter"
    return None


def _print_temperature(interface: LIFUInterface) -> None:
    """Read and print the TX and ambient temperatures."""
    try:
        tx_c = interface.txdevice.get_temperature()
    except Exception as exc:
        print(f"  Temperature read failed: {exc}")
        return
    try:
        amb_c = interface.txdevice.get_ambient_temperature()
        print(f"  TX temp: {tx_c:.1f} C   (ambient {amb_c:.1f} C)")
    except Exception:
        print(f"  TX temp: {tx_c:.1f} C")


def _trigger_running(interface: LIFUInterface) -> bool:
    """Report whether the firmware trigger is still running.

    On a comms hiccup, assume still running rather than falsely announcing
    completion.
    """
    try:
        return interface.txdevice.get_trigger_json().get("TriggerStatus") == "RUNNING"
    except Exception:
        return True


def interactive_sonication(interface: LIFUInterface, sequence: dict) -> None:
    """Start/stop sonication on keypress without reloading the solution.

    The delay profiles, apodizations, and profile cycle stay loaded in the
    firmware across start/stop (the cycle config is only cleared by a new
    set_solution), so every start reruns the full circle sweep. While
    sonicating, the trigger status is polled so a sequence that finishes on
    its own is announced instead of leaving the state ambiguous. HV is left
    on between runs for fast restarts.
    """
    duration = compute_sonication_duration(sequence)

    def show_idle_prompt(first: bool = False) -> None:
        print()
        print("  " + "-" * 56)
        if first:
            print("  READY - profiles loaded and verified on the device.")
            print(f"  A full sweep runs ~{duration:.0f}s "
                  f"({duration / 60:.1f} min) if not stopped early.")
        print("  Press [Enter] to START sonication    "
              "[t] TX temperature    [q] quit")
        print("  " + "-" * 56)

    show_idle_prompt(first=True)

    running = False
    last_status_poll = 0.0
    started_at = 0.0
    try:
        while True:
            key = _poll_key()

            if key == "q":
                break

            if key == "temp":
                _print_temperature(interface)

            if key == "enter":
                if not running:
                    print("  Preparing: HV on, waiting for rail to settle "
                          "... ", end="", flush=True)
                    interface.start_sonication()
                    running = True
                    started_at = time.monotonic()
                    last_status_poll = started_at
                    print("done.")
                    print(f"  >>> SONICATING (~{duration:.0f}s sweep)   "
                          f"[Enter] STOP    [t] TX temperature    "
                          f"[q] stop & quit")
                else:
                    interface.stop_sonication(turn_hv_off=False)
                    running = False
                    active = interface.txdevice.get_delay_profile()
                    print(f"  Stopped early at delay profile {active}.")
                    show_idle_prompt()

            # While sonicating, watch for the firmware stopping the trigger:
            # either the sequence completed (TrainCount reached the target)
            # or the firmware aborted early (e.g. thermal shutoff at 75 C).
            if running and (time.monotonic() - last_status_poll) >= 1.0:
                last_status_poll = time.monotonic()
                if not _trigger_running(interface):
                    running = False
                    elapsed = time.monotonic() - started_at
                    active = interface.txdevice.get_delay_profile()
                    trains_done = -1
                    try:
                        trains_done = int(
                            interface.txdevice.get_trigger_json().get("TrainCount", -1))
                    except Exception:
                        pass
                    try:
                        temp_c = interface.txdevice.get_temperature()
                    except Exception:
                        temp_c = float("nan")
                    if trains_done >= sequence["pulse_train_count"]:
                        print(f"  Sweep COMPLETE after {elapsed:.0f}s "
                              f"(ended on profile {active}, TX temp {temp_c:.1f} C).")
                    else:
                        print(f"  !! STOPPED EARLY by firmware after {elapsed:.0f}s "
                              f"at profile {active} (TX temp {temp_c:.1f} C).")
                        print("     Likely thermal shutoff (trips at 75 C, "
                              "re-arms below 70 C) - let the array cool or "
                              "reduce duty cycle / voltage.")
                    show_idle_prompt()

            time.sleep(0.05)
    finally:
        if running:
            interface.stop_sonication(turn_hv_off=False)
            print("  Sonication stopped.")
        print("  Exiting interactive mode.")


# ---------------------------------------------------------------------------
# Test case
# ---------------------------------------------------------------------------


def test_circle_raster(interface: LIFUInterface, num_tx: int,
                       medium: str) -> bool:
    """Sixteen computed delay profiles sweeping a circle of focal points.

    Programs 16 time-of-flight delay profiles (one per focus), verifies the
    delay RAM against the computed values, then hands sonication control to
    the operator.

    Args:
        interface: Connected LIFU interface.
        num_tx: Number of enumerated TX chips.
        medium: Test medium key from FOCAL_DEPTHS_MM ("phantom" or "water");
            selects the focal depth.
    """
    depth_mm = FOCAL_DEPTHS_MM[medium]
    _print_header(
        f"Circle raster ({medium}): center + {NUM_FOCI - 1} circle foci, "
        f"r={CIRCLE_RADIUS_MM:.1f} mm, z={depth_mm:.1f} mm"
    )

    element_positions = load_element_positions()
    foci = make_foci(depth_mm=depth_mm)
    delays = compute_circle_delay_profiles(element_positions, foci)
    apodizations = make_all_on_apodizations(NUM_FOCI)
    pulse = make_default_pulse()
    sequence = dict(CIRCLE_SEQUENCE)
    execution_order = list(range(1, NUM_FOCI + 1))

    # execution_order = [1, 4, 7, 10, 13, 16, 3, 6, 9, 12, 15, 2, 5, 8, 11, 14]

    profile_numbers = list(range(1, NUM_FOCI + 1))

    print("  Foci (mm) and per-profile delay spread:")
    for k, focus in enumerate(foci):
        spread_us = (delays[k].max() - delays[k].min()) * 1e6
        print(f"    profile {k + 1:2d}: focus=({focus[0]:+7.2f}, "
              f"{focus[1]:+7.2f}, {focus[2]:5.1f})  spread={spread_us:5.2f} us")
    duration = compute_sonication_duration(sequence)
    print(f"  One pulse per focus, {sequence['pulse_train_count']} trains "
          f"-> ~{duration / 60:.1f} min total")

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
    print("  Delay verification (computed time-of-flight values):")
    passed &= verify_delays(interface, profile_numbers, delays, num_tx)
    print("  Control register verification (active profile = 1):")
    passed &= verify_control_registers(interface, 1, num_tx)
    print("  Apodization register verification (all channels on):")
    passed &= verify_apodization_register(interface, 1, num_tx)

    if not passed:
        return False

    # Profiles are loaded and verified; hand control to the operator. The
    # active-profile printout after each stop shows where the sweep halted
    # (equals the last execution_order entry after a completed sequence).
    interactive_sonication(interface, sequence)

    return passed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _prompt_test_medium() -> str:
    """Ask the user to select the test medium (focal depth) by number."""
    print()
    print("  Select test medium / focal depth:")
    print(f"    [1] phantom - thermal paper on phantom "
          f"(z = {FOCAL_DEPTHS_MM['phantom']:.1f} mm)")
    print(f"    [2] water   - water-tank test          "
          f"(z = {FOCAL_DEPTHS_MM['water']:.1f} mm)")
    while True:
        choice = input("  Enter 1 or 2: ").strip()
        if choice == "1":
            medium = "phantom"
        elif choice == "2":
            medium = "water"
        else:
            print("  Invalid selection - please enter 1 or 2.")
            continue
        print(f"  Selected: {medium} "
              f"(focal depth {FOCAL_DEPTHS_MM[medium]:.1f} mm)")
        return medium


def main() -> None:
    """Run the circle-raster test and report the result."""
    medium = _prompt_test_medium()

    interface = LIFUInterface()
    num_tx = _setup_hardware(interface)

    # Ensure no stale sonication is active from a previous run.
    interface.stop_sonication(turn_hv_off=False)

    result = "ERROR"
    try:
        result = "PASS" if test_circle_raster(interface, num_tx,
                                              medium=medium) else "FAIL"
    except Exception:
        traceback.print_exc()
    finally:
        # Never leave the array sonicating if the test dies mid-run.
        interface.stop_sonication(turn_hv_off=False)

    print(f"\n{'=' * 60}")
    print(f"  [{result}] circle raster")
    print(f"{'=' * 60}")

    if result != "PASS":
        sys.exit(1)


if __name__ == "__main__":
    main()
