"""Capture a preset's TX7332 register mapping off connected hardware.

In FDA mode the transmitter firmware -- not the host -- programs the TX7332
chips, so the register values have to be baked into the image. They are
captured by programming a real transmitter: :func:`capture_machine_config`
hands the preset to :meth:`LIFUTXDevice.set_solution`, the same call that
configures a device for a live treatment, and then reads back the register
model it produced.

Nothing about the register mapping is computed here. That is the point -- the
values come from :func:`build_solution_registers`, the same function that
programs a live device, so there is no second implementation to drift. Pass a
transmitter to program and capture off real hardware (how release artifacts are
built); pass None to compute only, for development and CI.

:func:`generate_header` writes a captured config out as a C header. The headers
are generated here and copied into the firmware tree by hand, so the firmware
build itself needs neither Python nor this SDK.

The register data is emitted as ``{address, value}`` tables in the same shape
as the firmware's ``demo.c`` ``reg_values``, so the loader writes them the same
way. Only one descriptor type is needed to iterate them generically, from the
firmware's ``presets.h``::

    #define ARRAY_COUNT(a) (sizeof(a) / sizeof((a)[0]))
    typedef struct { const uint32_t (*regs)[2]; uint16_t count; } PresetRegList;

Every header also carries two CRC-32s the host verifies against: ``SETTINGS_CRC``
over the raw source .json, and ``REGS_CRC`` over the emitted tables in the order
:func:`_crc_stream` defines (the firmware recomputes that one from flash).
"""

from __future__ import annotations

import logging
import re
import struct
import zlib
from pathlib import Path
from typing import Dict, List, Sequence

from openlifu_sdk.io.LIFUTXDevice import (
    ADDRESS_APODIZATION,
    ADDRESS_DELAY_SEL,
    ADDRESS_PATTERN_SEL_G1,
    ADDRESS_PATTERN_SEL_G2,
    PATTERN_PROFILE_SELECT_MASK,
    build_solution_registers,
)

logger = logging.getLogger(__name__)

# Preset fields copied into the captured config as-is, for the header to emit.
PASSTHROUGH_FIELDS = (
    "pulse_count",
    "pulse_interval_ms",
    "pulse_train_interval_s",
)

# Amplitude scales the pattern duty cycle. Presets that do not carry one are
# full-amplitude.
DEFAULT_PULSE_AMPLITUDE = 1.0

# Trains to configure while capturing. Run length is not preset data -- it
# arrives with OW_PRESET_LOAD -- but set_solution needs a value for set_trigger.
CAPTURE_PULSE_TRAIN_COUNT = 1


def capture_machine_config(txdevice, preset: Dict, preset_id: str | None = None,
                           settings_bytes: bytes | None = None) -> Dict:
    """Resolve a preset into the register mapping a TX image bakes in.

    The mapping comes from :func:`build_solution_registers`, the same function
    :meth:`TxDevice.set_solution` uses to program a live device, so there is
    only ever one implementation to keep correct.

    Args:
        txdevice:  A ``TxDevice`` to program, or None to only compute. Passing
                   a device runs the full :meth:`set_solution` path, which
                   programs the chips and lets the result be read back --
                   that is how release artifacts are captured. Passing None
                   computes the same registers with no device at all.
        preset:    Preset document (the operator-facing JSON).
        preset_id: Identifier for the generated config. Defaults to
                   ``preset["id"]``.
        settings_bytes: The raw bytes of the preset .json file. Hashed into
                   ``settings_crc`` so the host can later prove the device was
                   built from the same file it holds. Required to generate a
                   header.

    Returns:
        Dict with ``id``, the passthrough sequence fields, ``settings_crc``
        (when ``settings_bytes`` was given), ``profile_index``,
        ``execution_order`` and ``chips``. Each chip entry holds ``registers``
        (start address -> run of consecutive values) and ``profiles`` (one
        address -> value dict per delay profile, the registers the firmware
        rewrites as it rasters).

    Raises:
        ValueError: If the preset has no id or is malformed.
        LIFUError:  On any device failure, including a chip-count mismatch.
    """
    pid = preset_id if preset_id is not None else preset.get("id")
    if not pid:
        raise ValueError("preset has no 'id' and no preset_id was supplied")

    delays = preset["delays"]
    apodizations = preset["apodization"]
    execution_order = list(preset.get("order") or range(1, len(delays) + 1))

    pulse = {
        "frequency": preset["frequency_khz"] * 1e3,
        "duration": preset["pulse_length_us"] * 1e-6,
        "amplitude": preset.get("amplitude", DEFAULT_PULSE_AMPLITUDE),
    }
    sequence = {
        "pulse_interval": preset["pulse_interval_ms"] * 1e-3,
        "pulse_count": preset["pulse_count"],
        "pulse_train_interval": preset.get("pulse_train_interval_s", 0),
        "pulse_train_count": CAPTURE_PULSE_TRAIN_COUNT,
    }

    if txdevice is not None:
        # Programs the device for real, then captures what it was given. A bad
        # preset fails here rather than silently baking.
        txdevice.set_solution(
            pulse=pulse,
            delays=delays,
            apodizations=apodizations,
            sequence=sequence,
            profile_index=execution_order[0],
            execution_order=execution_order,
        )
        regs = txdevice.tx_registers
    else:
        regs = build_solution_registers(
            pulse,
            delays,
            apodizations,
            sequence,
            profile_index=execution_order[0],
            execution_order=execution_order,
        )["tx_registers"]

    n_profiles = len(regs.configured_delay_profiles())
    logger.info("captured '%s': %d profile(s), %d chip(s), order=%s",
                pid, n_profiles, regs.num_transmitters, execution_order)

    captured = {"id": pid}
    if settings_bytes is not None:
        captured["settings_crc"] = zlib.crc32(settings_bytes) & 0xFFFFFFFF
    for name in PASSTHROUGH_FIELDS:
        if name in preset:
            captured[name] = preset[name]
    captured["profile_index"] = execution_order[0]
    captured["execution_order"] = execution_order
    captured["chips"] = _chip_configs(regs, n_profiles)
    return captured


def _chip_configs(regs, n_profiles: int) -> List[Dict]:
    """Split the programmed register model into one bakeable entry per TX7332."""
    packed = regs.get_registers(pack=True, pack_single=True)

    # Per-profile registers: the delay selector and apodization the device was
    # programmed with, plus the 0-based pattern selector for both groups. These
    # are the writes the firmware repeats as it rasters.
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


def _crc_stream(machine_config: Dict) -> bytes:
    """The bytes regs_crc covers -- PRESET_CRC_STREAM in the firmware's presets.h.

    presets_regs_crc() walks the same stream from flash, so the two must stay
    identical: header fields, execution order, trigger timing, then per chip
    the base pairs and each profile's pairs, exactly as generate_header emits
    them.
    """
    chips = machine_config["chips"]
    order = machine_config["execution_order"]
    n_profiles = len(chips[0]["profiles"]) if chips else 0
    interval_ms = float(machine_config["pulse_interval_ms"])
    out = bytearray(struct.pack("<BBBB", len(chips), n_profiles,
                                int(machine_config["profile_index"]), len(order)))
    out += bytes(int(i) for i in order)
    out += struct.pack("<III", round(1000.0 / interval_ms) if interval_ms else 0,
                       int(machine_config["pulse_count"]),
                       round(float(machine_config.get("pulse_train_interval_s", 0)) * 1e6))
    for chip in chips:
        for pairs in [_flatten(chip["registers"])] + [
                [[a, prof[a]] for a in sorted(prof)] for prof in chip["profiles"]]:
            out += struct.pack("<H", len(pairs))
            for addr, value in pairs:
                out += struct.pack("<HI", addr, value & 0xFFFFFFFF)
    return bytes(out)


def regs_crc(machine_config: Dict) -> int:
    """CRC-32 the firmware will compute for this preset from its own flash."""
    return zlib.crc32(_crc_stream(machine_config)) & 0xFFFFFFFF


def generate_header(filename, machine_config: Dict, context: str | None = None) -> Path:
    """Write a machine config out as a C header for the firmware build.

    Args:
        filename:       Destination path. Parent directories are created.
        machine_config: Result of :func:`capture_machine_config`.
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

    # What the host verifies against: the source file, and these very tables.
    if "settings_crc" not in machine_config:
        raise ValueError("%s has no settings_crc; pass settings_bytes to capture_machine_config"
                         % machine_config["id"])
    lines += ["", "// CRC-32 of the source .json, and of the tables below (PRESET_CRC_STREAM).",
              "#define %s_SETTINGS_CRC 0x%08xu" % (sym, machine_config["settings_crc"]),
              "#define %s_REGS_CRC 0x%08xu" % (sym, regs_crc(machine_config))]

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
        presets:   Captured configs, in the order the host indexes them.
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
            "\t  %s_TRIG_HZ, %s_TRIG_COUNT, %s_TRIG_TRAIN_US,\n"
            "\t  %s_SETTINGS_CRC, %s_REGS_CRC },"
            % ((sym,) * 13)
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


def verify_preset(txdevice, index: int, machine_config: Dict) -> Dict:
    """Prove the device's preset *index* is the one the host holds.

    Asks the device (OW_PRESET_GET) and compares id, settings_crc and regs_crc
    against *machine_config* -- captured locally with settings_bytes so both
    CRCs are known. Returns the device's reply on success.

    Raises:
        ValueError: With every field that disagrees, if any do.
    """
    got = txdevice.get_preset(index)
    want = {"id": machine_config["id"],
            "settings_crc": machine_config["settings_crc"],
            "regs_crc": regs_crc(machine_config)}
    bad = ["%s: device=%s host=%s" % (k, (hex(got[k]) if isinstance(got[k], int) else got[k]),
                                       (hex(v) if isinstance(v, int) else v))
           for k, v in want.items() if got[k] != v]
    if bad:
        raise ValueError("preset %d verification failed: %s" % (index, "; ".join(bad)))
    return got
