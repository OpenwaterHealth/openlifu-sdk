# SPDX-License-Identifier: Apache-2.0
"""Simulated flash entries built on the SR-003 CRC contract (#58).

The simulator tests use these instead of their own CRCs, so they exercise
the same contract as hardware: ``settings_crc`` is the CRC-32 of the preset
file's raw bytes and ``regs_crc`` is the CRC-32 of the compiled blob body
(``LIFUTXPresets.regs_crc``).
"""

from __future__ import annotations

import json

from openlifu_sdk.io.LIFUTXPresets import compile_preset, preset_modules, regs_crc

# The simulated transmitter's default calibration is 2720 at 400 kHz, so this
# preset's 40 V applies unscaled, inside the console's 35-45 V FDA window.
DEFAULTS = {
    "id": "unit",
    "frequency_khz": 400.0,
    "pulse_length_us": 20.0,
    "pulse_interval_ms": 100.0,
    "pulse_count": 10,
    "pulse_train_interval_s": 0,
    "pulse_train_count_selections": [1, 5],
    "delays": [[0.0] * 64],
    "apodization": [[1.0] * 64],
    "start_C": 35,
    "shutoff_C": 75,
    "voltage": 40.0,
    "sensitivity": 2720,
}


def flash_entry(**overrides) -> tuple[dict, int | list[int]]:
    """``(machine_config, regs_crc)`` for a preset file holding DEFAULTS with
    *overrides*: what ``preset_flash`` takes, and what ``load_preset`` is
    given. ``regs_crc`` is one int for a 1x preset (64 elements) and one per
    module for a preset that spans more (128 elements: 2x).
    ``machine_config["settings_crc"]`` is the file's CRC."""
    doc = dict(DEFAULTS, **overrides)
    machine_config = compile_preset(doc, settings_bytes=json.dumps(doc).encode())
    modules = preset_modules(machine_config)
    if modules == 1:
        return machine_config, regs_crc(machine_config)
    return machine_config, [regs_crc(machine_config, m) for m in range(modules)]


def two_module_flash_entry(**overrides) -> tuple[dict, list[int]]:
    """A 2x preset (128 elements), as the vet app ships. Its delays differ per
    element, so each module's part of the preset (and its regs_crc) differs."""
    parts = {"delays": [[i * 1e-8 for i in range(128)]], "apodization": [[1.0] * 128]}
    return flash_entry(**{**parts, **overrides})
