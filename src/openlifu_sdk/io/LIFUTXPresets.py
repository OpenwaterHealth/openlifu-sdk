"""Resolve a preset into the TX7332 register mapping a transmitter image bakes in.

In FDA mode the transmitter firmware -- not the host -- programs the TX7332
chips, so the register values have to be baked into the image.
:func:`compile_preset` produces them for one preset .json.

:func:`preset_blob` serializes a compiled preset into the one binary form the
firmware reads: a pointer-free record (``PRESET_BLOB`` in the firmware's
``presets.h``) whose base registers are stored as runs of consecutive
addresses. Nothing in it depends on where it is stored, so the same bytes can
be baked into an image or written to flash.

:func:`generate_header_files` writes that blob out as an annotated C byte
array. The headers are generated here and copied into the firmware.

Every blob carries two CRC-32s the host verifies against: ``settings_crc``
over the raw source .json, and ``regs_crc`` over the blob's body, which is
everything the firmware executes from (the firmware recomputes that one).

Each preset's ``start_C`` / ``shutoff_C`` go into its TX header: the firmware
refuses a trigger start above the first and stops a run at the second. Its
``voltage`` goes to the console instead, through
:func:`generate_console_presets`: one entry per preset, indexed exactly as the
TX image is, so the host selects preset N on both and never sends a voltage.
"""

from __future__ import annotations

import json
import logging
import math
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
    PRESET_BLOB_MAGIC,
    PRESET_BLOB_VERSION,
    PRESET_ID_MAX,
    PRESET_TEMP_RAW_MAX,
    PRESET_TEMP_RAW_MIN,
    PRESET_TEMP_SCALE,
    PRESET_TRAIN_COUNT_MAX,
    PRESET_TRAIN_SELECTIONS_MAX,
    build_solution_registers,
)

logger = logging.getLogger(__name__)

# Sequence fields the header emits verbatim. Every one is required: the
# generator has no defaults of its own, so a preset must say what it means.
PASSTHROUGH_FIELDS = (
    "pulse_count",
    "pulse_interval_ms",
    "pulse_train_interval_s",
    "pulse_train_count_selections",
)

# Safety limits, just as required: the thermal pair is baked into the TX image,
# the voltage into the console's preset table.
LIMIT_FIELDS = (
    "start_C",
    "shutoff_C",
    "voltage",
)


def train_count_selections(machine_config: Dict) -> List[int]:
    """The run lengths, in pulse trains, the operator may pick from.

    ``pulse_train_count_selections`` from the preset .json, baked into the
    image so OW_PRESET_LOAD can only ever select one by index.

    Raises:
        ValueError: Missing, empty, more than PRESET_TRAIN_SELECTIONS_MAX, or
            a count outside 1..PRESET_TRAIN_COUNT_MAX (the wire format's limits).
    """
    raw = machine_config.get("pulse_train_count_selections")
    counts = [int(x) for x in raw] if raw else []
    if (not 0 < len(counts) <= PRESET_TRAIN_SELECTIONS_MAX
            or any(not 1 <= c <= PRESET_TRAIN_COUNT_MAX for c in counts)):
        raise ValueError("%s: pulse_train_count_selections must be 1..%d counts in 1..%d, got %r"
                         % (machine_config.get("id"), PRESET_TRAIN_SELECTIONS_MAX,
                            PRESET_TRAIN_COUNT_MAX, raw))
    return counts


def thermal_limits(machine_config: Dict) -> tuple[int, int]:
    """``start_C`` and ``shutoff_C`` as the raw i16 tenths the image bakes.

    Raises:
        ValueError: Missing, finer than the wire format's resolution, out of
            its range, or a start_C not below shutoff_C.
    """
    raw = []
    for name in ("start_C", "shutoff_C"):
        value = machine_config.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("%s: %s must be a number, got %r" % (machine_config.get("id"), name, value))
        scaled = value * PRESET_TEMP_SCALE
        # Refuse rather than round, so the image enforces exactly what the preset says.
        if abs(scaled - round(scaled)) > 1e-6 or not PRESET_TEMP_RAW_MIN <= round(scaled) <= PRESET_TEMP_RAW_MAX:
            raise ValueError("%s: %s=%r is not a multiple of %g C within the wire format's range"
                             % (machine_config.get("id"), name, value, 1.0 / PRESET_TEMP_SCALE))
        raw.append(int(round(scaled)))
    if raw[0] >= raw[1]:
        raise ValueError("%s: start_C (%r) must be below shutoff_C (%r)"
                         % (machine_config.get("id"), machine_config["start_C"], machine_config["shutoff_C"]))
    return raw[0], raw[1]


def preset_voltage(machine_config: Dict) -> float:
    """The HV setpoint the console bakes for this preset: ``voltage`` as float32.

    Raises:
        ValueError: Not a positive number.
    """
    v = machine_config.get("voltage")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise ValueError("%s: voltage must be a positive number, got %r" % (machine_config.get("id"), v))
    return struct.unpack("<f", struct.pack("<f", v))[0]


def compile_preset(preset: Dict, preset_id: str | None = None,
                           settings_bytes: bytes | None = None) -> Dict:
    """Resolve a preset into the register mapping a TX image bakes in.

    The mapping comes from :func:`build_solution_registers`, the same function
    :meth:`TxDevice.set_solution` uses to program a live device, so there is
    only ever one implementation to keep correct.

    Args:
        preset:    Preset document (the operator-facing JSON).
        preset_id: Identifier for the generated config. Defaults to
                   ``preset["id"]``.
        settings_bytes: The raw bytes of the preset .json file. Hashed into
                   ``settings_crc`` so the host can later prove the device was
                   built from the same file it holds. Required to generate a
                   header.

    Returns:
        Dict with ``id``, the passthrough sequence fields, the limit fields
        (``start_C``, ``shutoff_C``, ``voltage``), ``settings_crc``
        (when ``settings_bytes`` was given), ``profile_index``,
        ``execution_order`` and ``chips``. Each chip entry holds ``registers``
        (start address -> run of consecutive values) and ``profiles`` (one
        address -> value dict per delay profile, the registers the firmware
        rewrites as it rasters).

    Raises:
        ValueError: If the preset has no id, lacks a sequence or limit
            field, or is otherwise malformed.
    """
    pid = preset_id if preset_id is not None else preset.get("id")
    if not pid:
        raise ValueError("preset has no 'id' and no preset_id was supplied")

    missing = [k for k in PASSTHROUGH_FIELDS + LIMIT_FIELDS if k not in preset]
    if missing:
        raise ValueError("preset '%s' lacks %s" % (pid, ", ".join(missing)))

    delays = preset["delays"]
    apodizations = preset["apodization"]
    # A missing order means sequential; leave that default to the SDK, which
    # knows the profile count after reshaping a flat single-profile preset.
    order = preset.get("order")
    execution_order = [int(i) for i in order] if order else None

    # Unit conversions only. Amplitude goes through when the preset has one;
    # otherwise build_solution_registers applies its own default.
    pulse = {
        "frequency": preset["frequency_khz"] * 1e3,
        "duration": preset["pulse_length_us"] * 1e-6,
    }
    if "amplitude" in preset:
        pulse["amplitude"] = preset["amplitude"]
    # No train count: the register build only reads what it needs to check
    # rastering fits, and which run length runs is chosen at OW_PRESET_LOAD.
    sequence = {
        "pulse_interval": preset["pulse_interval_ms"] * 1e-3,
        "pulse_count": preset["pulse_count"],
        "pulse_train_interval": preset["pulse_train_interval_s"],
    }

    # A preset with an order starts at its first entry; without one, the SDK's
    # own starting profile applies.
    kwargs = {"execution_order": execution_order}
    if execution_order:
        kwargs["profile_index"] = execution_order[0]
    solution = build_solution_registers(pulse, delays, apodizations, sequence, **kwargs)
    regs = solution["tx_registers"]

    n_profiles = len(regs.configured_delay_profiles())
    logger.info("resolved '%s': %d profile(s), %d chip(s), order=%s",
                pid, n_profiles, regs.num_transmitters, solution["execution_order"])

    captured = {"id": pid}
    if settings_bytes is not None:
        captured["settings_crc"] = zlib.crc32(settings_bytes) & 0xFFFFFFFF
    for name in PASSTHROUGH_FIELDS + LIMIT_FIELDS:
        captured[name] = preset[name]
    # Refuse bad run lengths or limits now, not at emit time.
    train_count_selections(captured)
    thermal_limits(captured)
    preset_voltage(captured)
    # The SDK's normalized values, so the image starts where set_solution would.
    captured["profile_index"] = solution["profile_index"]
    captured["execution_order"] = solution["execution_order"]
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
# The preset blob, and the C headers that carry it
# --------------------------------------------------------------------------

def c_ident(text: str) -> str:
    """Upper-case C identifier for a preset id or context name."""
    ident = re.sub(r"[^0-9A-Za-z]+", "_", str(text)).strip("_").upper()
    if not ident:
        raise ValueError(f"cannot build a C identifier from {text!r}")
    if ident[0].isdigit():
        ident = "P_" + ident
    return ident


def _trig_hz(machine_config: Dict) -> int:
    """The integer trigger rate the image bakes for ``pulse_interval_ms``."""
    interval_ms = float(machine_config["pulse_interval_ms"])
    if interval_ms <= 0:
        raise ValueError("%s: pulse_interval_ms must be positive, got %r"
                         % (machine_config.get("id"), interval_ms))
    return round(1000.0 / interval_ms)


Segments = List[tuple]  # (label, bytes) pieces of a blob, in order


def _body_segments(machine_config: Dict) -> Segments:
    """The blob's body, piece by labelled piece -- what regs_crc covers.

    Base registers go out as runs of consecutive addresses (start, count,
    values), which is how the register model already packs them; the handful
    each profile switch rewrites are scattered, so those stay address/value
    pairs.
    """
    chips = machine_config["chips"]
    order = machine_config["execution_order"]
    n_profiles = len(chips[0]["profiles"]) if chips else 0
    counts = train_count_selections(machine_config)
    out: Segments = [
        ("chips, profiles, starting profile, execution order length",
         struct.pack("<BBBB", len(chips), n_profiles, int(machine_config["profile_index"]), len(order))),
        ("execution order", bytes(int(i) for i in order)),
        ("trigger: Hz, pulses per train, train interval in us",
         struct.pack("<III", _trig_hz(machine_config), int(machine_config["pulse_count"]),
                     round(float(machine_config["pulse_train_interval_s"]) * 1e6))),
        ("run lengths: how many, then each in pulse trains",
         struct.pack("<B%dI" % len(counts), len(counts), *counts)),
        ("start_C, shutoff_C in tenths of a degree", struct.pack("<hh", *thermal_limits(machine_config))),
    ]
    for ci, chip in enumerate(chips):
        runs = sorted(chip["registers"].items())
        out.append(("chip %d base registers: %d run(s)" % (ci, len(runs)), struct.pack("<H", len(runs))))
        for addr, values in runs:
            out.append(("0x%04x x %d" % (addr, len(values)),
                        struct.pack("<HH%dI" % len(values), addr, len(values),
                                    *(v & 0xFFFFFFFF for v in values))))
        for pi, profile in enumerate(chip["profiles"]):
            pairs = sorted(profile.items())
            out.append(("chip %d profile %d: %d register(s), address then value" % (ci, pi + 1, len(pairs)),
                        struct.pack("<H", len(pairs))
                        + b"".join(struct.pack("<HI", a, v & 0xFFFFFFFF) for a, v in pairs)))
    return out


def _blob_segments(machine_config: Dict) -> Segments:
    """A whole blob as labelled pieces: the header, then :func:`_body_segments`."""
    if "settings_crc" not in machine_config:
        raise ValueError("%s has no settings_crc; pass settings_bytes to compile_preset"
                         % machine_config["id"])
    pid = str(machine_config["id"])
    # Printable ASCII only: the firmware reports it as text and it lands in C comments.
    if not 1 <= len(pid) <= PRESET_ID_MAX or any(not 0x20 <= ord(ch) <= 0x7E for ch in pid):
        raise ValueError("preset id %r must be 1..%d printable ASCII characters" % (pid, PRESET_ID_MAX))
    ident = pid.encode("ascii")
    body = _body_segments(machine_config)
    body_bytes = b"".join(data for _, data in body)
    header_len = len(PRESET_BLOB_MAGIC) + 16 + len(ident)
    return [
        ("magic, version, id length, reserved",
         PRESET_BLOB_MAGIC + struct.pack("<BBH", PRESET_BLOB_VERSION, len(ident), 0)),
        ("total length, settings_crc, regs_crc",
         struct.pack("<III", header_len + len(body_bytes), machine_config["settings_crc"],
                     zlib.crc32(body_bytes) & 0xFFFFFFFF)),
        ('id "%s"' % pid, ident),
    ] + body


def preset_blob(machine_config: Dict) -> bytes:
    """Serialize a compiled preset into the blob the firmware reads.

    The layout is ``PRESET_BLOB`` in the firmware's ``presets.h``. It is
    little-endian with no padding and no pointers.

    Raises:
        ValueError: No settings_crc (pass settings_bytes to compile_preset),
            or an id the format cannot carry.
    """
    return b"".join(data for _, data in _blob_segments(machine_config))


def regs_crc(machine_config: Dict) -> int:
    """CRC-32 the firmware will compute for this preset: over the blob's body."""
    return zlib.crc32(b"".join(data for _, data in _body_segments(machine_config))) & 0xFFFFFFFF


def parse_preset_blob(blob: bytes) -> Dict:
    """Decode a preset blob, the way the firmware's loader walks it.

    Returns ``id``, ``settings_crc``, ``regs_crc`` (as stored), ``body_crc``
    (computed here), ``profile_index``, ``execution_order``, ``trig_hz``,
    ``trig_count``, ``trig_train_us``, ``train_counts``, ``start_c_x10``,
    ``shutoff_c_x10`` and ``chips`` in the shape :func:`compile_preset`
    returns them.

    Raises:
        ValueError: Not a blob this version understands, or truncated.
    """
    blob = bytes(blob)
    pos = 0

    def take(fmt):
        nonlocal pos
        size = struct.calcsize(fmt)
        if pos + size > len(blob):
            raise ValueError("preset blob is truncated at byte %d of %d" % (pos, len(blob)))
        values = struct.unpack_from(fmt, blob, pos)
        pos += size
        return values

    magic, version, id_len, _reserved = take("<4sBBH")
    if magic != PRESET_BLOB_MAGIC or version != PRESET_BLOB_VERSION:
        raise ValueError("not a version %d preset blob (magic %r, version %d)"
                         % (PRESET_BLOB_VERSION, magic, version))
    total_len, settings_crc, stored_crc = take("<III")
    if total_len != len(blob):
        raise ValueError("preset blob says %d bytes but is %d" % (total_len, len(blob)))
    (ident,) = take("<%ds" % id_len)
    body_start = pos
    n_chips, n_profiles, profile_index, order_len = take("<BBBB")
    order = list(take("<%dB" % order_len))
    trig_hz, trig_count, trig_train_us = take("<III")
    (n_counts,) = take("<B")
    counts = list(take("<%dI" % n_counts))
    start, shutoff = take("<hh")
    chips = []
    for _ in range(n_chips):
        registers = {}
        (n_runs,) = take("<H")
        for _ in range(n_runs):
            addr, n = take("<HH")
            registers[addr] = list(take("<%dI" % n))
        profiles = []
        for _ in range(n_profiles):
            (n_pairs,) = take("<H")
            profiles.append(dict(take("<HI") for _ in range(n_pairs)))
        chips.append({"registers": registers, "profiles": profiles})
    if pos != len(blob):
        raise ValueError("preset blob has %d stray byte(s) after its last chip" % (len(blob) - pos))
    return {"id": ident.decode("ascii"), "settings_crc": settings_crc, "regs_crc": stored_crc,
            "body_crc": zlib.crc32(blob[body_start:]) & 0xFFFFFFFF,
            "profile_index": profile_index, "execution_order": order,
            "trig_hz": trig_hz, "trig_count": trig_count, "trig_train_us": trig_train_us,
            "train_counts": counts, "start_c_x10": start, "shutoff_c_x10": shutoff, "chips": chips}


def generate_header_files(filename, machine_config: Dict, context: str | None = None) -> Path:
    """Write a compiled preset out as a C header: its blob, as an annotated byte array.

    Voltage and sensitivity stay out: the TX has no handler for them, and the
    console applies the voltage (:func:`generate_console_presets`). The run
    length is emitted only as the list of choices; which one runs arrives with
    OW_PRESET_LOAD.

    Args:
        filename:       Destination path. Parent directories are created.
        machine_config: Result of :func:`compile_preset`.
        context:        Optional preset-set name, prefixed onto the generated
                        symbols so two sets can coexist in one tree.

    Returns:
        The path written.
    """
    path = Path(filename)
    sym = _symbol(machine_config, context)
    guard = "%s_H_" % sym

    chips = machine_config["chips"]
    n_profiles = len(chips[0]["profiles"]) if chips else 0
    segments = _blob_segments(machine_config)
    size = sum(len(data) for _, data in segments)

    lines = [
        "// Generated by openlifu_sdk from preset '%s'." % machine_config["id"],
        "// Do not edit: regenerate with the SDK and copy the result here.",
        "#ifndef %s" % guard,
        "#define %s" % guard,
        "",
        "#include <stdint.h>",
        "",
        "// %d chip(s), %d profile(s), start_C %g, shutoff_C %g, regs_crc 0x%08x."
        % (len(chips), n_profiles, machine_config["start_C"], machine_config["shutoff_C"],
           regs_crc(machine_config)),
        "// One preset blob, laid out as PRESET_BLOB in presets.h: each comment",
        "// names the bytes that follow it.",
        "static const uint8_t %s_BLOB[%u] = {" % (sym, size),
    ]
    for label, data in segments:
        lines.append("\t// %s" % label)
        lines += ["\t" + " ".join("0x%02x," % b for b in data[i:i + 16]) for i in range(0, len(data), 16)]
    lines += ["};", "", "#endif  // %s" % guard, ""]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("wrote %s (%d chips, %d profiles, %d bytes)", path, len(chips), n_profiles, size)
    return path


def generate_preset_set(output_directory, presets: Sequence[Dict], context: str | None = None) -> List[Path]:
    """Write a whole preset set: one header per preset plus the table.

    The firmware keeps them all in Core/Inc/presets/, so the table includes
    each header as ``presets/<name>.h``.

    Args:
        output_directory: Destination directory for the generated headers.
        presets:   Captured configs, in the order the host indexes them.
        context:   Optional set name, prefixed onto the symbols and header names.

    Returns:
        Every path written, the table header last.
    """
    if not presets:
        raise ValueError("a preset set needs at least one preset")

    out = Path(output_directory)
    stems = [_header_stem(mc, context) for mc in presets]
    # Without a set name, a preset called "table" would land on the table itself.
    if "preset_table" in stems:
        raise ValueError("preset id '%s' collides with preset_table.h"
                         % presets[stems.index("preset_table")]["id"])
    written = [generate_header_files(out / ("%s.h" % stem), mc, context)
               for stem, mc in zip(stems, presets)]

    guard = _table_guard("PRESET_TABLE", context)
    lines = [
        _generated_by(context),
        "// Do not edit: regenerate with the SDK and copy the result here.",
        "#ifndef %s" % guard,
        "#define %s" % guard,
        "",
        '#include "presets.h"',
        "",
    ]
    lines += ['#include "presets/%s.h"' % stem for stem in stems]
    lines.append("")
    if context:
        lines.append('#define PRESET_CONTEXT_NAME "%s"' % context)
    lines += [
        "#define PRESET_COUNT %uu" % len(presets),
        "",
        "// Index order is the order the host loads them by.",
        "static const PresetBlob preset_table[PRESET_COUNT] = {",
    ]
    for mc in presets:
        sym = _symbol(mc, context)
        lines.append("\t{ %s_BLOB, sizeof(%s_BLOB) }," % (sym, sym))
    lines += ["};", "", "#endif  // %s" % guard, ""]

    table = out / "preset_table.h"
    table.parent.mkdir(parents=True, exist_ok=True)
    table.write_text("\n".join(lines), encoding="utf-8")
    logger.info("wrote %s (%d preset(s))", table, len(presets))
    written.append(table)
    return written


def _symbol(machine_config: Dict, context: str | None) -> str:
    """C symbol prefix for one preset: its id, behind the set name if there is one."""
    sym = c_ident(machine_config["id"])
    if context:
        sym = "%s_%s" % (c_ident(context), sym)
    return "PRESET_%s" % sym


def _header_stem(machine_config: Dict, context: str | None) -> str:
    return _symbol(machine_config, context).lower()


def _table_guard(name: str, context: str | None) -> str:
    return "%s_%sH_" % (name, c_ident(context) + "_" if context else "")


def _generated_by(context: str | None) -> str:
    return "// Generated by openlifu_sdk%s." % (" for preset set '%s'" % context if context else "")


def generate_console_presets(output_directory, presets: Sequence[Dict], context: str | None = None) -> Path:
    """Write the console's ``preset_hv_table.h`` for a preset set.

    One entry per preset -- id, voltage, settings_crc -- in the same order as
    :func:`generate_preset_set` gives the transmitter, so index N is the same
    preset on both. An FDA_MODE console applies entry N's voltage when the
    host selects N (with that preset's settings_crc) and refuses any voltage
    the host sends itself. Each voltage is emitted as its exact float32.

    Args:
        output_directory: Destination directory, Core/Inc/presets/ in the
                          console tree once copied.
        presets:          Compiled presets, in the order the host indexes them.
        context:          Optional set name, as for :func:`generate_preset_set`.

    Returns:
        The path written.
    """
    if not presets:
        raise ValueError("a preset set needs at least one preset")
    for mc in presets:
        if "settings_crc" not in mc:
            raise ValueError("%s has no settings_crc; pass settings_bytes to compile_preset" % mc["id"])
    volts = [preset_voltage(mc) for mc in presets]
    guard = _table_guard("PRESET_HV_TABLE", context)
    lines = [
        _generated_by(context),
        "// Do not edit: regenerate with the SDK and copy the result here.",
        "#ifndef %s" % guard,
        "#define %s" % guard,
        "",
        '#include "hv_presets.h"',
        "",
    ]
    if context:
        lines.append('#define PRESET_HV_CONTEXT_NAME "%s"' % context)
    lines += [
        "#define PRESET_HV_COUNT %uu" % len(presets),
        "",
        "// One HV setpoint per preset, indexed exactly as the transmitter image's",
        "// preset_table: OW_POWER_PRESET_SELECT N applies entry N's voltage.",
    ]
    if len(set(volts)) == 1:
        lines.append("// Every preset in this set uses %.9g V." % volts[0])
    lines.append("static const HvPreset hv_preset_table[PRESET_HV_COUNT] = {")
    lines += ['\t{ "%s", %.9gf, 0x%08xu },' % (mc["id"], v, mc["settings_crc"])
              for mc, v in zip(presets, volts)]
    lines += ["};", "", "#endif  // %s" % guard, ""]

    path = Path(output_directory) / "preset_hv_table.h"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("wrote %s (%d preset(s), %.9g..%.9g V)", path, len(presets), min(volts), max(volts))
    return path


def verify_preset(txdevice, index: int, machine_config: Dict) -> Dict:
    """Prove the device's preset *index* is the one the host holds.

    Asks the device (OW_PRESET_GET) and compares id, settings_crc, regs_crc
    and the thermal limits against *machine_config* -- captured locally with
    settings_bytes so both CRCs are known. Returns the device's reply on
    success.

    Raises:
        ValueError: With every field that disagrees, if any do.
    """
    got = txdevice.get_preset(index)
    start, shutoff = thermal_limits(machine_config)
    want = {"id": machine_config["id"],
            "settings_crc": machine_config["settings_crc"],
            "regs_crc": regs_crc(machine_config),
            "start_c": start / PRESET_TEMP_SCALE,
            "shutoff_c": shutoff / PRESET_TEMP_SCALE}
    bad = ["%s: device=%s host=%s" % (k, (hex(got[k]) if isinstance(got[k], int) else got[k]),
                                       (hex(v) if isinstance(v, int) else v))
           for k, v in want.items() if got[k] != v]
    if bad:
        raise ValueError("preset %d verification failed: %s" % (index, "; ".join(bad)))
    return got


def verify_console_preset(hvcontroller, index: int, machine_config: Dict) -> Dict:
    """Prove the console's preset *index* is the one the host holds.

    Asks the console (OW_POWER_PRESET_GET) and compares id, settings_crc and
    the exact float32 voltage against *machine_config*, which must carry
    settings_crc. Returns the console's reply on success.

    Raises:
        ValueError: The console has no preset table (not an FDA_MODE image),
            or with every field that disagrees.
    """
    got = hvcontroller.get_preset(index)
    if got is None:
        raise ValueError("console has no preset table: not an FDA_MODE image")
    want = {"id": machine_config["id"],
            "settings_crc": machine_config["settings_crc"],
            "voltage": preset_voltage(machine_config)}
    bad = ["%s: console=%s host=%s" % (k, (hex(got[k]) if isinstance(got[k], int) else got[k]),
                                        (hex(v) if isinstance(v, int) else v))
           for k, v in want.items() if got[k] != v]
    if bad:
        raise ValueError("console preset %d verification failed: %s" % (index, "; ".join(bad)))
    return got


# --------------------------------------------------------------------------
# Preset source files
# --------------------------------------------------------------------------

def preset_files(input_directory) -> List[Path]:
    """The preset .json files under *input_directory*, in the order the image indexes them.

    A preset is any JSON document carrying ``delays`` and ``apodization``,
    found either directly in the directory or one level down -- the
    application keeps each preset in its own folder next to its plot -- and
    anything else there, such as a constants.json, is skipped.

    Generation and the on-device test must agree on the order -- it is the
    index the host passes to OW_PRESET_LOAD -- so both take it from here.
    Natural order on the id: digit runs compare as numbers, so canine_5.0mm
    sorts before canine_10.0mm.
    """
    directory = Path(input_directory)
    found = []
    for path in list(directory.glob("*.json")) + list(directory.glob("*/*.json")):
        try:
            doc = json.loads(path.read_bytes())
        except ValueError:
            continue
        if isinstance(doc, dict) and "delays" in doc and "apodization" in doc:
            found.append(path)

    def key(path):
        return [int(t) if t.isdigit() else t.lower() for t in re.split("([0-9]+)", preset_id(path))]
    return sorted(found, key=key)


def preset_id(path) -> str:
    """A preset id from its file name: the stem minus any trailing ``_settings``."""
    stem = Path(path).stem
    return stem[:-len("_settings")] if stem.endswith("_settings") else stem
