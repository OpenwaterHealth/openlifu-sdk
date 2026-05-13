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
)

FIXED_FREQUENCY_HZ = 400e3
FIXED_CYCLES = 16
ACTIVE_SECONDS = 3.0
OFF_SECONDS = 3.0
ROUNDS = 4

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


print("=== TX Profile Load/Switch Scope Validation ===")
print(f"Carrier frequency fixed at {FIXED_FREQUENCY_HZ:.0f} Hz")
print("Expected sequence: Profile 1 (3s on, 1s off) -> Profile 2 -> Profile 3, repeating")

for cfg in PROFILE_CONFIGS:
    validate_profile_timing(cfg)

interface = LIFUInterface()
txm = TxDeviceRegisters()

# Load three delay profiles (all valid and selectable; waveform differentiation comes from timing).
for cfg in PROFILE_CONFIGS:
    delays = np.zeros(64)
    apodizations = np.ones(64)
    txm.add_delay_profile(Tx7332DelayProfile(cfg["index"], delays, apodizations))

# Load three pulse profiles with fixed 400kHz parameters.
for cfg in PROFILE_CONFIGS:
    txm.add_pulse_profile(Tx7332PulseProfile(cfg["index"], FIXED_FREQUENCY_HZ, FIXED_CYCLES))

# Activate all loaded profiles so they can be selected at runtime.
for cfg in PROFILE_CONFIGS:
    txm.activate_delay_profile(cfg["index"])
    txm.activate_pulse_profile(cfg["index"])

interface.txdevice.tx_registers = txm
interface.txdevice.apply_all_registers()

print("\nLoaded profiles:")
for cfg in PROFILE_CONFIGS:
    pulse = txm.get_pulse_profile(cfg["index"])
    print(
        f"  Profile {cfg['index']}: {pulse.frequency:.0f} Hz, cycles={pulse.cycles}, "
        f"pulse_interval={cfg['pulse_interval']}s, pulse_count={cfg['pulse_count']}, "
        f"train_interval={cfg['pulse_train_interval']}s, train_count={cfg['pulse_train_count']}"
    )

print("\nStarting scope test...")
print("Operator note: trigger your scope on TX output and observe 3 unique cadence blocks with 1s quiet gaps.")

for round_idx in range(ROUNDS):
    print(f"\n=== Round {round_idx + 1}/{ROUNDS} ===")
    for cfg in PROFILE_CONFIGS:
        index = cfg["index"]
        interface.txdevice.set_delay_profile_select(index)
        interface.txdevice.set_pattern_profile_select(index)

        # Mirror prodreqs-style sequence fields: pulse interval/count + train interval/count.
        interface.txdevice.set_trigger(
            pulse_interval=cfg["pulse_interval"],
            pulse_count=cfg["pulse_count"],
            pulse_train_interval=cfg["pulse_train_interval"],
            pulse_train_count=cfg["pulse_train_count"],
            trigger_mode="sequence",
        )

        trigger_readback = interface.txdevice.get_trigger_json()
        print(
            f"[{time.strftime('%H:%M:%S')}] Profile {index} ON: "
            f"f={FIXED_FREQUENCY_HZ:.0f}Hz, pi={cfg['pulse_interval']}s, pc={cfg['pulse_count']}, "
            f"ti={cfg['pulse_train_interval']}s, tc={cfg['pulse_train_count']}, "
            f"device_freq={trigger_readback.get('TriggerFrequencyHz')}Hz"
        )

        interface.txdevice.start_trigger()
        time.sleep(ACTIVE_SECONDS)
        interface.txdevice.stop_trigger()

        print(f"[{time.strftime('%H:%M:%S')}] Profile {index} OFF for {OFF_SECONDS}s")
        time.sleep(OFF_SECONDS)

print("\nScope profile switching test complete.")
