"""Resolve an operator preset into the machine configuration a TX image bakes in.

In FDA mode the transmitter firmware -- not the host -- programs the TX7332
chips, so the register values have to exist before the host is in the loop.
:func:`calculate_machine_config` runs the SDK's own register math over a preset
document and returns the result as a plain dict, which a header generator turns
into C tables for the firmware build.

The register math itself is not reimplemented here: this is a wrapper over
``TxDeviceRegisters``, the same model :meth:`LIFUTXDevice.set_solution` programs
a live device from. What lives here is the layer above it -- how a preset
document maps onto pulse and delay profiles, and which registers the firmware
needs per profile in order to raster.

:func:`generate_header` writes a machine config out as a C header. The headers
are generated here and copied into the firmware tree by hand, so the firmware
build itself needs neither Python nor this SDK.

The register data is emitted as ``{address, value}`` tables in the same shape
as the firmware's ``demo.c`` ``reg_values``, so the loader writes them the same
way. Only one descriptor type is needed to iterate them generically, from the
firmware's ``presets.h``::

    #define ARRAY_COUNT(a) (sizeof(a) / sizeof((a)[0]))
    typedef struct { const uint32_t (*regs)[2]; uint16_t count; } PresetRegList;
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Sequence

import numpy as np

from openlifu_sdk.io.LIFUTXDevice import (
    ADDRESS_APODIZATION,
    ADDRESS_DELAY_SEL,
    ADDRESS_PATTERN_SEL_G1,
    ADDRESS_PATTERN_SEL_G2,
    DEFAULT_PATTERN_DUTY_CYCLE,
    MIN_PROFILE_SWITCH_INTERVAL,
    NUM_CHANNELS,
    PATTERN_PROFILE_SELECT_MASK,
    VALID_DELAY_PROFILES,
    Tx7332DelayProfile,
    Tx7332PulseProfile,
    TxDeviceRegisters,
)

logger = logging.getLogger(__name__)

# Preset fields copied into the machine config as-is. Anything the firmware or
# the operator interface needs at runtime has to be listed here, because the
# preset document itself is not baked into the image.
PASSTHROUGH_FIELDS = (
    "label",
    "voltage",
    "voltage_range",
    "sensitivity",
    "depth_mm",
    "frequency_khz",
    "start_C",
    "shutoff_C",
    "pulse_length_us",
    "pulse_interval_ms",
    "pulse_count",
    "pulse_train_interval_s",
    "pulse_train_count_selections",
)

# Amplitude scales the pattern duty cycle. Presets that do not carry one are
# full-amplitude.
DEFAULT_PULSE_AMPLITUDE = 1.0


def calculate_machine_config(preset: Dict, preset_id: str | None = None) -> Dict:
    """Resolve a preset document into a bakeable machine configuration.

    Args:
        preset:     Preset document (the operator-facing JSON), with at least
                    ``delays``, ``apodization``, ``frequency_khz`` and
                    ``pulse_length_us``.
        preset_id:  Identifier for the generated config. Defaults to
                    ``preset["id"]``.

    Returns:
        Dict with the passthrough preset fields plus:

        ``id``
            Identifier the host asks for over OW_PRESET_LOAD.
        ``profile_index``
            1-based delay profile active before the first trigger.
        ``execution_order``
            1-based delay profile indices the firmware cycles through.
        ``chips``
            One entry per TX7332, each ``{"registers": ..., "profiles": ...}``.
            ``registers`` maps a start address to a run of consecutive values
            for bulk writes; ``profiles`` holds one address->value dict per
            delay profile, the registers the firmware rewrites as it rasters.

    Raises:
        ValueError: If the preset is malformed or its sequence cannot raster.
    """
    pid = preset_id if preset_id is not None else preset.get("id")
    if not pid:
        raise ValueError("preset has no 'id' and no preset_id was supplied")

    delays = np.array(preset["delays"], dtype=float)
    if delays.ndim == 1:
        delays = delays.reshape(1, -1)
    apodizations = np.array(preset["apodization"], dtype=float)
    if apodizations.ndim == 1:
        apodizations = apodizations.reshape(1, -1)

    if delays.shape != apodizations.shape:
        raise ValueError(f"delays {delays.shape} and apodization {apodizations.shape} disagree")

    n_profiles, n_elements = delays.shape
    if n_profiles == 0:
        raise ValueError("At least one profile row is required")
    if n_profiles > len(VALID_DELAY_PROFILES):
        raise ValueError(
            f"Too many profile rows ({n_profiles}). Max supported is {len(VALID_DELAY_PROFILES)}"
        )
    if n_elements % NUM_CHANNELS:
        raise ValueError(f"{n_elements} elements is not a whole number of TX7332 chips")

    execution_order = list(preset.get("order") or range(1, n_profiles + 1))
    for idx in execution_order:
        if not isinstance(idx, int) or idx < 1 or idx > n_profiles:
            raise ValueError(
                f"order contains invalid profile index {idx}. Must be in 1-{n_profiles}"
            )
    profile_index = execution_order[0]

    frequency = preset["frequency_khz"] * 1e3
    duration = preset["pulse_length_us"] * 1e-6
    amplitude = preset.get("amplitude", DEFAULT_PULSE_AMPLITUDE)

    # Rastering is driven off the trigger, so the sequence has to leave the
    # firmware time to switch profiles between bursts.
    if len(execution_order) > 1:
        pulse_count = preset["pulse_count"]
        if pulse_count % len(execution_order):
            raise ValueError(
                f"pulse_count ({pulse_count}) must be divisible by the number of profiles "
                f"in order ({len(execution_order)}), so each profile gets a whole number "
                f"of consecutive pulses."
            )
        dead_time = (preset["pulse_interval_ms"] * 1e-3) - duration
        if dead_time < MIN_PROFILE_SWITCH_INTERVAL:
            raise ValueError(
                f"pulse_interval ({preset['pulse_interval_ms']:.1f} ms) must exceed the pulse "
                f"duration ({duration * 1e3:.1f} ms) by at least "
                f"{MIN_PROFILE_SWITCH_INTERVAL * 1e6:.0f} us so the firmware can switch "
                f"profiles after the burst ends."
            )

    regs = TxDeviceRegisters(num_transmitters=n_elements // NUM_CHANNELS)

    # One pulse profile per delay profile slot: the firmware cycles the pattern
    # selector in lockstep with the delay selector, so every slot the execution
    # order can reach needs valid pattern RAM. The pattern itself is identical
    # across slots, only the duty cycle tracks the loudest apodization.
    duty_cycle = DEFAULT_PATTERN_DUTY_CYCLE * float(np.max(apodizations)) * amplitude
    for dp in range(n_profiles):
        regs.add_pulse_profile(Tx7332PulseProfile(
            profile=dp + 1,
            frequency=frequency,
            cycles=int(duration * frequency),
            duty_cycle=duty_cycle,
        ))
        regs.add_delay_profile(Tx7332DelayProfile(
            profile=dp + 1,
            delays=delays[dp, :],
            apodizations=apodizations[dp, :],
        ))

    regs.activate_delay_profile(profile_index)
    regs.activate_pulse_profile(profile_index)

    logger.info(
        "machine config '%s': %d profile(s), %d chip(s), order=%s",
        pid, n_profiles, regs.num_transmitters, execution_order,
    )

    machine_config = {"id": pid}
    for name in PASSTHROUGH_FIELDS:
        if name in preset:
            machine_config[name] = preset[name]
    machine_config["profile_index"] = profile_index
    machine_config["execution_order"] = execution_order
    machine_config["chips"] = _chip_configs(regs, n_profiles)
    return machine_config


def _chip_configs(regs: TxDeviceRegisters, n_profiles: int) -> List[Dict]:
    """Split the register model into one bakeable entry per TX7332."""
    packed = regs.get_registers(pack=True, pack_single=True)

    # Per-profile registers, gathered per chip: the delay selector and
    # apodization the SDK computes, plus the 0-based pattern selector for both
    # groups. These are the writes the firmware repeats as it rasters.
    per_profile = []
    for profile in range(1, n_profiles + 1):
        control = regs.get_delay_control_registers(profile)
        pattern_sel = (profile - 1) & PATTERN_PROFILE_SELECT_MASK
        per_profile.append([
            {
                ADDRESS_DELAY_SEL: int(chip[ADDRESS_DELAY_SEL]),
                ADDRESS_APODIZATION: int(chip[ADDRESS_APODIZATION]),
                ADDRESS_PATTERN_SEL_G1: pattern_sel,
                ADDRESS_PATTERN_SEL_G2: pattern_sel,
            }
            for chip in control
        ])

    chips = []
    for chip_index, chip_regs in enumerate(packed):
        registers = {
            int(addr): [int(v) for v in (values if isinstance(values, list) else [values])]
            for addr, values in sorted(chip_regs.items())
        }
        chips.append({
            "registers": registers,
            "profiles": [profile[chip_index] for profile in per_profile],
        })
    return chips


# --------------------------------------------------------------------------
# C header generation
# --------------------------------------------------------------------------

# Voltage, sensitivity and the thermal limits are deliberately not emitted: the
# transmitter firmware has no handler for them (there is no OW_CTRL_SET_HV
# implementation), so they would be dead weight in the image. Run length is not
# emitted either -- it arrives with OW_PRESET_LOAD.
def c_ident(text: str) -> str:
    """Upper-case C identifier for a preset id or context name."""
    ident = re.sub(r"[^0-9A-Za-z]+", "_", str(text)).strip("_").upper()
    if not ident:
        raise ValueError(f"cannot build a C identifier from {text!r}")
    if ident[0].isdigit():
        ident = "P_" + ident
    return ident


def _emit_pairs(lines: List[str], name: str, pairs: Sequence[Sequence[int]]) -> None:
    """Emit an {address, value} table in the shape demo.c uses."""
    lines.append("static const uint32_t %s[%u][2] = {" % (name, len(pairs)))
    for addr, value in pairs:
        lines.append("\t{ 0x%04x, 0x%08x }," % (addr, value & 0xFFFFFFFF))
    lines.append("};")


def _flatten(registers: Dict[int, List[int]]) -> List[List[int]]:
    """Expand {start address: run of values} into one pair per register."""
    pairs = []
    for addr in sorted(registers):
        for offset, value in enumerate(registers[addr]):
            pairs.append([addr + offset, value])
    return pairs


def generate_header(filename, machine_config: Dict, context: str | None = None) -> Path:
    """Write a machine config out as a C header for the firmware build.

    Args:
        filename:       Destination path. Parent directories are created.
        machine_config: Result of :func:`calculate_machine_config`.
        context:        Optional preset-set name, prefixed onto the generated
                        symbols so two sets can coexist in one tree.

    Returns:
        The path written.
    """
    path = Path(filename)
    sym = c_ident(machine_config["id"])
    if context:
        sym = "%s_%s" % (c_ident(context), sym)
    sym = "PRESET_%s" % sym
    guard = "%s_H_" % sym

    chips = machine_config["chips"]
    order = machine_config["execution_order"]
    n_profiles = len(chips[0]["profiles"]) if chips else 0

    lines = [
        "// Generated by openlifu_sdk from preset '%s'." % machine_config["id"],
        "// Do not edit: regenerate with the SDK and copy the result here.",
        "#ifndef %s" % guard,
        "#define %s" % guard,
        "",
        '#include "presets.h"',
        "",
        '#define %s_ID "%s"' % (sym, machine_config["id"]),
        "#define %s_CHIPS %uu" % (sym, len(chips)),
        "#define %s_PROFILES %uu" % (sym, n_profiles),
        "#define %s_PROFILE_INDEX %uu" % (sym, machine_config["profile_index"]),
        "#define %s_EXEC_LEN %uu" % (sym, len(order)),
    ]

    # Trigger timing the preset defines, so loading it configures the sequence.
    # Run length is not here: it arrives with OW_PRESET_LOAD.
    lines.append("#define %s_TRIG_COUNT %uu" % (sym, int(machine_config["pulse_count"])))
    interval_ms = float(machine_config["pulse_interval_ms"])
    lines.append("#define %s_TRIG_HZ %uu" % (sym, round(1000.0 / interval_ms) if interval_ms else 0))
    lines.append("#define %s_TRIG_TRAIN_US %uu"
                 % (sym, round(float(machine_config.get("pulse_train_interval_s", 0)) * 1e6)))

    lines += ["", "// Delay profile execution order used during raster cycling.",
              "static const uint8_t %s_EXEC_ORDER[%s_EXEC_LEN] = { %s };"
              % (sym, sym, ", ".join(str(int(i)) for i in order))]

    # {address, value} tables, written in order like demo.c's reg_values.
    for ci, chip in enumerate(chips):
        lines += ["", "// Chip %d base configuration." % ci]
        _emit_pairs(lines, "%s_C%d" % (sym, ci), _flatten(chip["registers"]))

        lines += ["", "// Chip %d: rewritten on each profile switch." % ci]
        for pi, profile in enumerate(chip["profiles"]):
            _emit_pairs(lines, "%s_C%d_P%d" % (sym, ci, pi),
                        [[addr, profile[addr]] for addr in sorted(profile)])

    lines += ["", "// Tables the loader walks: base is one row per chip,",
              "// profile regs are chip-major -- [chip * PROFILES + profile].",
              "static const PresetRegList %s_BASE[%s_CHIPS] = {" % (sym, sym)]
    for ci, chip in enumerate(chips):
        lines.append("\t{ %s_C%d, ARRAY_COUNT(%s_C%d) }," % (sym, ci, sym, ci))
    lines.append("};")

    lines.append("static const PresetRegList %s_PROFILE_REGS[%s_CHIPS * %s_PROFILES] = {"
                 % (sym, sym, sym))
    for ci, chip in enumerate(chips):
        for pi in range(len(chip["profiles"])):
            lines.append("\t{ %s_C%d_P%d, ARRAY_COUNT(%s_C%d_P%d) },"
                         % (sym, ci, pi, sym, ci, pi))
    lines.append("};")

    lines += ["", "#endif  // %s" % guard, ""]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("wrote %s (%d chips, %d profiles)", path, len(chips), n_profiles)
    return path


def generate_preset_set(directory, presets: Sequence[Dict], context: str) -> List[Path]:
    """Write a whole preset set: one header per preset plus the table.

    Args:
        directory: Destination directory for the generated headers.
        presets:   Machine configs, in the order the host indexes them.
        context:   Preset-set name, used in the symbols and the table guard.

    Returns:
        Every path written, the table header last.
    """
    if not presets:
        raise ValueError("a preset set needs at least one preset")

    out = Path(directory)
    written = [generate_header(out / ("%s.h" % _header_stem(mc, context)), mc, context)
               for mc in presets]

    ctx = c_ident(context)
    guard = "PRESET_TABLE_%s_H_" % ctx
    lines = [
        "// Generated by openlifu_sdk for preset set '%s'." % context,
        "// Do not edit: regenerate with the SDK and copy the result here.",
        "#ifndef %s" % guard,
        "#define %s" % guard,
        "",
        '#include "presets.h"',
        "",
    ]
    lines += ['#include "presets/%s/%s.h"' % (context, _header_stem(mc, context))
              for mc in presets]
    lines += [
        "",
        '#define PRESET_CONTEXT_NAME "%s"' % context,
        "#define PRESET_COUNT %uu" % len(presets),
        "",
        "static const Preset preset_table[PRESET_COUNT] = {",
    ]
    for mc in presets:
        sym = "PRESET_%s_%s" % (ctx, c_ident(mc["id"]))
        lines.append(
            "\t{ %s_ID, %s_BASE, %s_PROFILE_REGS, %s_EXEC_ORDER,\n"
            "\t  %s_CHIPS, %s_PROFILES, %s_PROFILE_INDEX, %s_EXEC_LEN,\n"
            "\t  %s_TRIG_HZ, %s_TRIG_COUNT, %s_TRIG_TRAIN_US },"
            % (sym, sym, sym, sym, sym, sym, sym, sym, sym, sym, sym)
        )
    lines += ["};", "", "#endif  // %s" % guard, ""]

    table = out / "preset_table.h"
    table.parent.mkdir(parents=True, exist_ok=True)
    table.write_text("\n".join(lines), encoding="utf-8")
    logger.info("wrote %s (%d preset(s))", table, len(presets))
    written.append(table)
    return written


def _header_stem(machine_config: Dict, context: str) -> str:
    return ("preset_%s_%s" % (c_ident(context), c_ident(machine_config["id"]))).lower()
