# SPDX-License-Identifier: Apache-2.0
"""Resolve a preset into the TX7332 register mapping a transmitter runs from.

In the preset modes the transmitter firmware -- not the host -- programs the
TX7332 chips, from presets stored in its flash pages, so the register values
have to be on the device.
:func:`compile_preset` produces them for one preset .json.

Each transmitter module programs its own TX7332 chips, so a preset is built
as one blob per module it spans, each holding that module's chips and nothing
else. A single-module preset is one blob; either way a module stores and
loads only its own part.

:func:`preset_blob` serializes a compiled preset into the one binary form the
firmware reads: a pointer-free record (``PRESET_BLOB`` in the firmware's
``presets.h``) whose base registers are stored as runs of consecutive
addresses. Nothing in it depends on where it is stored.

:func:`generate_header_files` writes that blob out as an annotated C byte
array, for reading; no firmware build uses it any more.

Every blob carries two CRC-32s the host verifies against: ``settings_crc``
over the raw source .json, and ``regs_crc`` over the blob's body, which is
everything the firmware executes from (the firmware recomputes that one).

Each preset's ``start_C`` / ``shutoff_C`` go into its blob: the firmware
refuses a trigger start above the first and stops a run at the second. So
does its ``treatment_timeout_s``: the firmware ends a run as TIMEOUT that
many seconds after Start, pauses included. Its
``voltage`` and ``sensitivity`` go to the console instead, through
:func:`generate_console_presets`: one entry per preset, indexed as the
transmitter's presets are. The host selects preset N on both and gives the console the
connected transmitter's sensitivity (:func:`device_sensitivity`); the console
rescales the voltage itself, and the host never sends one.
"""

from __future__ import annotations

import json
import logging
import math
import numbers
import re
import struct
import zlib
from pathlib import Path
from typing import Dict, List, Sequence

from openlifu_sdk.io.exceptions import LIFUError
from openlifu_sdk.io.LIFUConfig import LIFU_ERR_MODULE_COUNT_MISMATCH
from openlifu_sdk.io.LIFUTXDevice import (
    PRESET_BLOB_MAGIC,
    PRESET_BLOB_VERSION,
    PRESET_ID_MAX,
    PRESET_SLOT_SIZE,
    PRESET_TEMP_RAW_MAX,
    PRESET_TEMP_RAW_MIN,
    PRESET_TEMP_SCALE,
    PRESET_TIMEOUT_MAX_S,
    PRESET_TIMEOUT_MIN_S,
    PRESET_TRAIN_COUNT_MAX,
    PRESET_TRAIN_SELECTIONS_MAX,
    TRANSMITTERS_PER_MODULE,
    build_solution_registers,
    resolve_trigger_timing,
)
from openlifu_sdk.io.LIFUUserConfig import sensitivity_at_frequency

logger = logging.getLogger(__name__)

# Sequence fields the header emits verbatim. Every one is required: the
# generator has no defaults of its own, so a preset must say what it means.
PASSTHROUGH_FIELDS = (
    "pulse_count",
    "pulse_interval_ms",
    "pulse_train_interval_s",
    "pulse_train_count_selections",
)

# Safety limits, just as required: the thermal pair and the treatment timeout
# go to the transmitter, the voltage into the console's preset table.
LIMIT_FIELDS = (
    "start_C",
    "shutoff_C",
    "treatment_timeout_s",
    "voltage",
)


def train_count_selections(machine_config: Dict) -> List[int]:
    """The run lengths, in pulse trains, the operator may pick from.

    ``pulse_train_count_selections`` from the preset .json, stored with the
    preset so OW_PRESET_LOAD can only ever select one by index.

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


def treatment_timeout(machine_config: Dict) -> int:
    """``treatment_timeout_s`` as the u16 the blob carries: whole seconds from
    Start, pauses included, after which the firmware ends the run as TIMEOUT.

    Raises:
        ValueError: Missing, not a whole number, or outside
            PRESET_TIMEOUT_MIN_S..PRESET_TIMEOUT_MAX_S.
    """
    value = machine_config.get("treatment_timeout_s")
    # Refuse rather than round, as for the thermal limits; 1800.0 is whole, 1800.5 is not.
    whole = (not isinstance(value, bool) and isinstance(value, (int, float))
             and math.isfinite(value) and value == int(value))
    if not whole or not PRESET_TIMEOUT_MIN_S <= int(value) <= PRESET_TIMEOUT_MAX_S:
        raise ValueError("%s: treatment_timeout_s must be whole seconds in %d..%d, got %r"
                         % (machine_config.get("id"), PRESET_TIMEOUT_MIN_S, PRESET_TIMEOUT_MAX_S, value))
    return int(value)


def preset_voltage(machine_config: Dict) -> float:
    """The HV voltage the console bakes for this preset, as float32: what a
    transmitter of the preset's sensitivity needs.

    Raises:
        ValueError: Not a positive number.
    """
    v = machine_config.get("voltage")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise ValueError("%s: voltage must be a positive number, got %r" % (machine_config.get("id"), v))
    return struct.unpack("<f", struct.pack("<f", v))[0]


def module_regs_crcs(regs_crc) -> List[int]:
    """*regs_crc* as given to ``DeviceInterface.load_preset``: one integer for a
    one-module preset, or a sequence with one per module (:func:`regs_crc`
    of each). Any integral type is accepted (``numpy.uint32`` included).

    Raises:
        TypeError: Not an integer or a non-empty sequence of integers (a
            ``bool`` is not a CRC).
    """
    def integral(value) -> bool:
        return isinstance(value, numbers.Integral) and not isinstance(value, bool)

    if integral(regs_crc):
        return [int(regs_crc)]
    try:
        crcs = [] if isinstance(regs_crc, (str, bytes)) else list(regs_crc)
    except TypeError:
        crcs = []
    if not crcs or not all(integral(c) for c in crcs):
        raise TypeError(f"regs_crc must be an integer or a non-empty sequence of integers, got {regs_crc!r}")
    return [int(c) for c in crcs]


def preset_voltage_range(machine_config: Dict) -> tuple[float, float] | None:
    """The preset's own HV range, ``voltage_range`` = [low, high] volts, or
    None if it has none.

    Preset data: the range the applied (sensitivity-rescaled) voltage may
    take for this preset. The host checks it: pass it to
    ``DeviceInterface.load_preset(voltage_range=...)``, which refuses a select
    whose applied voltage is outside it. The console checks it too: a
    presets-only image carries it in each PRESET_HV_RECORD v2 and holds every
    select to it, under the image's 45 V ceiling (console-fw#21; Peter,
    2026-10-30: a ceiling only, no fixed floor).

    Raises:
        ValueError: Not two finite positive numbers with low <= high.
    """
    rng = machine_config.get("voltage_range")
    if rng is None:
        return None
    ok = (isinstance(rng, (list, tuple)) and len(rng) == 2
          and all(not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) and v > 0
                  for v in rng)
          and rng[0] <= rng[1])
    if not ok:
        raise ValueError("%s: voltage_range must be [low, high] positive volts, got %r"
                         % (machine_config.get("id"), rng))
    return float(rng[0]), float(rng[1])


def preset_sensitivity(machine_config: Dict) -> float:
    """The transmitter sensitivity the preset's voltage suits, as the console
    bakes it: ``sensitivity`` as float32.

    Raises:
        ValueError: Missing, or not a positive number.
    """
    v = machine_config.get("sensitivity")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise ValueError("%s: sensitivity must be a positive number, got %r" % (machine_config.get("id"), v))
    return struct.unpack("<f", struct.pack("<f", v))[0]


def module_user_configs(txdevice) -> List[Dict]:
    """Each connected module's user config as a dict, in module order; {} for
    one whose config is not JSON."""
    configs = []
    for module in range(txdevice.get_module_count()):
        try:
            configs.append(json.loads(txdevice.read_config(module=module).get_json_str()))
        except ValueError:
            configs.append({})
    return configs


def device_sensitivity(preset: Dict, user_configs: Sequence[Dict]) -> float | None:
    """The connected transmitter's sensitivity at *preset*'s frequency
    (:func:`~openlifu_sdk.io.LIFUUserConfig.sensitivity_at_frequency`, the one
    lookup the SDK has), or None if no module lists that frequency.
    *user_configs* is each module's user config (:func:`module_user_configs`).
    What HVController.select_preset is given."""
    return sensitivity_at_frequency(user_configs, float(preset.get("frequency_khz", 0.0)) * 1e3)


def adjusted_voltage(preset: Dict, user_configs: Sequence[Dict]) -> Dict:
    """The HV voltage for *preset* on the connected transmitter, worked out on
    the host: for a console that is not an FDA_MODE image and takes a voltage.
    An FDA_MODE console does this sum itself (HVController.select_preset).

    A preset's ``voltage`` is right for a transmitter of the preset's own
    ``sensitivity``. One that is more or less sensitive needs proportionally
    less or more: ``voltage * sensitivity / transmitter sensitivity``
    (:func:`device_sensitivity`). *preset* is the preset document or what
    :func:`compile_preset` made of it.

    Returns ``voltage`` (what to set), ``preset_voltage`` and ``adjusted``.
    When adjusted, also ``preset_sensitivity`` and ``device_sensitivity``.
    When not, ``voltage`` is the preset's own and ``reason`` says what was
    missing -- never silently.

    Raises:
        ValueError: The preset's voltage is not a positive number.
    """
    preset_voltage(preset)
    raw = float(preset["voltage"])
    out = {"voltage": raw, "preset_voltage": raw, "adjusted": False}
    sens = preset.get("sensitivity")
    if isinstance(sens, bool) or not isinstance(sens, (int, float)) or not sens > 0:
        return {**out, "reason": "the preset has no sensitivity"}
    device = device_sensitivity(preset, user_configs)
    if device is None:
        return {**out, "reason": "no module has a sensitivity at %g Hz"
                                 % (float(preset.get("frequency_khz", 0.0)) * 1e3)}
    return {"voltage": raw * float(sens) / device, "preset_voltage": raw, "adjusted": True,
            "preset_sensitivity": float(sens), "device_sensitivity": device}


def compile_preset(preset: Dict, preset_id: str | None = None,
                           settings_bytes: bytes | None = None) -> Dict:
    """Resolve a preset into the register mapping a TX image bakes in.

    The mapping comes from :func:`build_solution_registers` and the trigger
    timing from :func:`resolve_trigger_timing`, the same functions
    :meth:`LIFUTxDevice.set_solution` uses to program a live device, so there is
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
        (``start_C``, ``shutoff_C``, ``treatment_timeout_s``, ``voltage``), ``frequency_khz`` and
        ``sensitivity`` when the preset has one, ``settings_crc``
        (when ``settings_bytes`` was given), ``trigger`` (``frequency_hz`` and
        ``pulse_train_interval_us``, as the device runs them),
        ``profile_index``, ``execution_order`` and ``chips``. Each chip entry
        holds ``registers``
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
    # Module inversion goes through when the preset states it, as amplitude does.
    if "module_invert" in preset:
        kwargs["module_invert"] = preset["module_invert"]
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
    # What adjusted_voltage() works from; a preset without a sensitivity is not adjusted.
    captured["frequency_khz"] = preset["frequency_khz"]
    if "sensitivity" in preset:
        captured["sensitivity"] = preset["sensitivity"]
    # Preset data only (not in the blob, so neither CRC changes): consoles that are not
    # FDA images check the applied voltage against it.
    if preset.get("voltage_range") is not None:
        captured["voltage_range"] = list(preset_voltage_range(preset))
    # Refuse bad run lengths or limits now, not at emit time.
    counts = train_count_selections(captured)
    thermal_limits(captured)
    treatment_timeout(captured)
    preset_voltage(captured)
    # The timing set_trigger would send, for the longest run the preset offers.
    captured["trigger"] = resolve_trigger_timing(
        sequence["pulse_interval"],
        pulse_count=sequence["pulse_count"],
        pulse_train_interval=sequence["pulse_train_interval"],
        pulse_train_count=max(counts),
    )
    # The SDK's normalized values, so the image starts where set_solution would.
    captured["profile_index"] = solution["profile_index"]
    captured["execution_order"] = solution["execution_order"]
    captured["chips"] = _chip_configs(regs, solution["profile_registers"])
    return captured


def _chip_configs(regs, profile_registers: List[List[Dict]]) -> List[Dict]:
    """Split the programmed register model into one bakeable entry per TX7332.

    profile_registers is the solution's ``[profile - 1][chip]`` set: the writes
    the firmware repeats as it rasters.
    """
    packed = regs.get_registers(pack=True, pack_single=True)

    chips = []
    for chip_index, chip_regs in enumerate(packed):
        registers = {
            int(addr): [int(v) for v in (values if isinstance(values, list) else [values])]
            for addr, values in sorted(chip_regs.items())
        }
        chips.append({
            "registers": registers,
            "profiles": [dict(profile[chip_index]) for profile in profile_registers],
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


Segments = List[tuple]  # (label, bytes) pieces of a blob, in order


def preset_modules(machine_config: Dict) -> int:
    """Transmitter modules the preset spans: its chips, TRANSMITTERS_PER_MODULE to a module."""
    return -(-len(machine_config["chips"]) // TRANSMITTERS_PER_MODULE)


def _module_chips(machine_config: Dict, module: int) -> List[Dict]:
    """The chips module *module* programs, out of the whole preset's."""
    first = module * TRANSMITTERS_PER_MODULE
    chips = machine_config["chips"][first:first + TRANSMITTERS_PER_MODULE] if module >= 0 else []
    if not chips:
        raise ValueError("%s spans %d module(s); it has nothing for module %d"
                         % (machine_config["id"], preset_modules(machine_config), module))
    return chips


def _body_segments(machine_config: Dict, module: int = 0) -> Segments:
    """The blob's body, piece by labelled piece -- what regs_crc covers.

    Base registers go out as runs of consecutive addresses (start, count,
    values), which is how the register model already packs them; the handful
    each profile switch rewrites are scattered, so those stay address/value
    pairs. Only module *module*'s chips go in; everything else is the same
    in every module's blob.
    """
    chips = _module_chips(machine_config, module)
    first_chip = module * TRANSMITTERS_PER_MODULE
    order = machine_config["execution_order"]
    trigger = machine_config["trigger"]
    n_profiles = len(chips[0]["profiles"]) if chips else 0
    counts = train_count_selections(machine_config)
    out: Segments = [
        ("chips, profiles, starting profile, execution order length",
         struct.pack("<BBBB", len(chips), n_profiles, int(machine_config["profile_index"]), len(order))),
        ("execution order", bytes(int(i) for i in order)),
        ("trigger: Hz, pulses per train, train interval in us",
         struct.pack("<III", trigger["frequency_hz"], int(machine_config["pulse_count"]),
                     trigger["pulse_train_interval_us"])),
        ("run lengths: how many, then each in pulse trains",
         struct.pack("<B%dI" % len(counts), len(counts), *counts)),
        ("start_C, shutoff_C in tenths of a degree", struct.pack("<hh", *thermal_limits(machine_config))),
        ("treatment_timeout_s: seconds from Start, pauses included",
         struct.pack("<H", treatment_timeout(machine_config))),
    ]
    for ci, chip in enumerate(chips, start=first_chip):
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


def _blob_segments(machine_config: Dict, module: int = 0) -> Segments:
    """One module's blob as labelled pieces: the header, then :func:`_body_segments`."""
    if "settings_crc" not in machine_config:
        raise ValueError("%s has no settings_crc; pass settings_bytes to compile_preset"
                         % machine_config["id"])
    pid = str(machine_config["id"])
    # Printable ASCII only: the firmware reports it as text and it lands in C comments.
    if not 1 <= len(pid) <= PRESET_ID_MAX or any(not 0x20 <= ord(ch) <= 0x7E for ch in pid):
        raise ValueError("preset id %r must be 1..%d printable ASCII characters" % (pid, PRESET_ID_MAX))
    ident = pid.encode("ascii")
    body = _body_segments(machine_config, module)
    body_bytes = b"".join(data for _, data in body)
    header_len = len(PRESET_BLOB_MAGIC) + 16 + len(ident)
    return [
        ("magic, version, id length, this module, modules the preset spans",
         PRESET_BLOB_MAGIC + struct.pack("<BBBB", PRESET_BLOB_VERSION, len(ident), module,
                                         preset_modules(machine_config))),
        ("total length, settings_crc, regs_crc",
         struct.pack("<III", header_len + len(body_bytes), machine_config["settings_crc"],
                     zlib.crc32(body_bytes) & 0xFFFFFFFF)),
        ('id "%s"' % pid, ident),
    ] + body


def preset_blob(machine_config: Dict, module: int = 0) -> bytes:
    """Serialize one module's part of a compiled preset into the blob the firmware reads.

    The layout is ``PRESET_BLOB`` in the firmware's ``presets.h``. It is
    little-endian with no padding and no pointers. *module* picks whose
    chips it holds; see :func:`preset_modules` for how many there are.

    Raises:
        ValueError: No settings_crc (pass settings_bytes to compile_preset),
            an id the format cannot carry, or a module the preset does not span.
    """
    return b"".join(data for _, data in _blob_segments(machine_config, module))


def regs_crc(machine_config: Dict, module: int = 0) -> int:
    """CRC-32 module *module* will compute for this preset: over its blob's body."""
    return zlib.crc32(b"".join(data for _, data in _body_segments(machine_config, module))) & 0xFFFFFFFF


def parse_preset_blob(blob: bytes) -> Dict:
    """Decode a preset blob, the way the firmware's loader walks it.

    Returns ``id``, ``module`` and ``modules`` (whose part this is, of how
    many; ``modules`` is 0 in a blob written before it was recorded),
    ``settings_crc``, ``regs_crc`` (as stored), ``body_crc``
    (computed here), ``profile_index``, ``execution_order``, ``trig_hz``,
    ``trig_count``, ``trig_train_us``, ``train_counts``, ``start_c_x10``,
    ``shutoff_c_x10``, ``treatment_timeout_s`` and ``chips`` in the shape
    :func:`compile_preset` returns them.

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

    magic, version, id_len, module, modules = take("<4sBBBB")
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
    (timeout_s,) = take("<H")
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
    return {"id": ident.decode("ascii"), "module": module, "modules": modules,
            "settings_crc": settings_crc, "regs_crc": stored_crc,
            "body_crc": zlib.crc32(blob[body_start:]) & 0xFFFFFFFF,
            "profile_index": profile_index, "execution_order": order,
            "trig_hz": trig_hz, "trig_count": trig_count, "trig_train_us": trig_train_us,
            "train_counts": counts, "start_c_x10": start, "shutoff_c_x10": shutoff,
            "treatment_timeout_s": timeout_s, "chips": chips}


def generate_header_files(filename, machine_config: Dict, context: str | None = None,
                          symbol: str | None = None, section: str | None = None) -> Path:
    """Write a compiled preset out as a C header: its blob, as an annotated byte
    array -- one array per module the preset spans.

    Voltage and sensitivity stay out: the TX has no handler for them, and the
    console applies the voltage (:func:`generate_console_presets`). The run
    length is emitted only as the list of choices; which one runs arrives with
    OW_PRESET_LOAD.

    Args:
        filename:       Destination path. Parent directories are created.
        machine_config: Result of :func:`compile_preset`.
        context:        Optional preset-set name, prefixed onto the generated
                        symbols so two sets can coexist in one tree.
        symbol:         The array's name, instead of one made from the id.
        section:        A linker section to put the array in (``.fda_preset``
                        for FDA mode), kept even if nothing refers to it.

    Returns:
        The path written.
    """
    path = Path(filename)
    sym = _symbol(machine_config, context)
    guard = "%s_H_" % sym

    chips = machine_config["chips"]
    n_profiles = len(chips[0]["profiles"]) if chips else 0
    modules = preset_modules(machine_config)
    size = 0

    lines = [
        "// Generated by openlifu_sdk from preset '%s'." % machine_config["id"],
        "// Do not edit: regenerate with the SDK and copy the result here.",
        "#ifndef %s" % guard,
        "#define %s" % guard,
        "",
        "#include <stdint.h>",
        "",
    ]
    for module in range(modules):
        segments = _blob_segments(machine_config, module)
        length = sum(len(data) for _, data in segments)
        size += length
        lines += [
            "// %s%d chip(s), %d profile(s), start_C %g, shutoff_C %g, treatment_timeout_s %d,"
            % ("" if modules == 1 else "Module %d of %d: " % (module, modules),
               len(_module_chips(machine_config, module)), n_profiles,
               machine_config["start_C"], machine_config["shutoff_C"], treatment_timeout(machine_config)),
            "// regs_crc 0x%08x." % regs_crc(machine_config, module),
            "// One preset blob, laid out as PRESET_BLOB in presets.h: each comment",
            "// names the bytes that follow it.",
        ] + ([
            "// The linker puts it at the end of the application slot (the FDA_PRESET",
            "// region of the FDA linker scripts), so it is written with the image, the",
            "// bootloader's signature covers it, and the firmware only reads it.",
        ] if section else []) + [
            "%sstatic const uint8_t %s[%u] = {"
            % ('__attribute__((section("%s"), used))\n' % section if section else "",
               symbol or _blob_symbol(sym, module), length),
        ]
        for label, data in segments:
            lines.append("\t// %s" % label)
            lines += ["\t" + " ".join("0x%02x," % b for b in data[i:i + 16]) for i in range(0, len(data), 16)]
        lines += ["};", ""]
    lines += ["#endif  // %s" % guard, ""]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("wrote %s (%d chips, %d profiles, %d bytes)", path, len(chips), n_profiles, size)
    return path


def generate_preset_set(output_directory, presets: Sequence[Dict], context: str | None = None) -> List[Path]:
    """Write a whole preset set: one header per preset plus the table.

    The firmware keeps them all in Core/Inc/presets/, so the table includes
    each header as ``presets/<name>.h``. It has one column per module: each
    module takes its own part of a preset.

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

    modules = max(preset_modules(mc) for mc in presets)
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
        "// The most modules any preset here spans.",
        "#define PRESET_MODULES %uu" % modules,
        "",
        "// Index order is the order the host loads them by. A module programs only",
        "// its own chips, so each preset has one blob per module it spans.",
        "static const PresetBlob preset_table[PRESET_COUNT][PRESET_MODULES] = {",
    ]
    for mc in presets:
        names = [_blob_symbol(_symbol(mc, context), m) for m in range(preset_modules(mc))]
        cells = ["{ %s, sizeof(%s) }" % (name, name) for name in names]
        cells += ["{ 0, 0 }"] * (modules - len(names))
        lines.append("\t{ %s }," % ", ".join(cells))
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


def _blob_symbol(symbol: str, module: int) -> str:
    """C name of one module's blob: module 0 keeps the plain name."""
    return "%s_BLOB" % symbol if module == 0 else "%s_M%d_BLOB" % (symbol, module)


def _header_stem(machine_config: Dict, context: str | None) -> str:
    return _symbol(machine_config, context).lower()


def _table_guard(name: str, context: str | None) -> str:
    return "%s_%sH_" % (name, c_ident(context) + "_" if context else "")


def _generated_by(context: str | None) -> str:
    return "// Generated by openlifu_sdk%s." % (" for preset set '%s'" % context if context else "")


def _c_float(value: float) -> str:
    """A C float literal that parses back to exactly this float32."""
    text = "%.9g" % value
    # A bare integer cannot take the f suffix.
    return (text if "." in text or "e" in text else text + ".0") + "f"


def generate_console_presets(output_directory, presets: Sequence[Dict], context: str | None = None) -> Path:
    """Write the console's ``preset_hv_table.h`` for a preset set: one
    PRESET_HV_RECORD v2 per preset (:func:`console_preset_record`), back to
    back as ``PRESET_HV_TABLE`` in the ``.fda_preset`` section (page 61), in
    the same order as :func:`generate_preset_set` gives the transmitter, so
    index N is the same preset on both (console-fw#21). A presets-only console
    applies record N's voltage, rescaled from its sensitivity to the connected
    transmitter's, when the host selects N with that preset's settings_crc,
    and holds it to that record's own voltage_range.

    Args:
        output_directory: Destination directory, Core/Inc/presets/ in the
                          console tree once copied.
        presets:          Compiled presets, in the order the host indexes them.
        context:          Optional set name, as for :func:`generate_preset_set`.

    Returns:
        The path written.

    Raises:
        ValueError: No presets, or one without a settings_crc, voltage,
            sensitivity or voltage_range, or with an unusable id.
    """
    if not presets:
        raise ValueError("a preset set needs at least one preset")
    records = [console_preset_record(mc, version=2) for mc in presets]
    table = b"".join(records)
    guard = _table_guard("PRESET_HV_TABLE", context)
    lines = [
        _generated_by(context),
        "// Do not edit: regenerate with the SDK and copy the result here.",
        "#ifndef %s" % guard,
        "#define %s" % guard,
        "",
        "#include <stdint.h>",
        "",
    ]
    if context:
        lines.append('#define PRESET_HV_CONTEXT_NAME "%s"' % context)
    lines += [
        "#define PRESET_HV_COUNT %uu" % len(presets),
        "",
        "// N PRESET_HV_RECORD v2 records back to back, in slot order (index 0 first),",
        "// indexed exactly as the transmitter image's preset_table:",
    ]
    lines += ["//   %u: %s, %g V in [%g, %g], settings_crc 0x%08x"
              % ((i, mc["id"], preset_voltage(mc)) + preset_voltage_range(mc) + (mc["settings_crc"],))
              for i, mc in enumerate(presets)]
    lines += [
        '__attribute__((section(".fda_preset"), used))',
        "static const uint8_t PRESET_HV_TABLE[%u] = {" % len(table),
    ]
    lines += ["\t" + " ".join("0x%02x," % b for b in table[i:i + 16]) for i in range(0, len(table), 16)]
    lines += ["};", "", "#endif  // %s" % guard, ""]

    path = Path(output_directory) / "preset_hv_table.h"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("wrote %s (%d preset(s), %d bytes)", path, len(presets), len(table))
    return path


def generate_fda_preset(output_directory, machine_config: Dict, context: str | None = None) -> Path:
    """Write the transmitter's FDA preset header, ``fda_preset.h``: the blob as
    ``FDA_PRESET_BLOB``, in the ``.fda_preset`` section the FDA linker scripts
    put at the end of the application slot (pages 124-125). The image carries
    it, so flashing or updating the image writes it, and the FDA firmware only
    reads it. FDA mode runs one preset, on a 1x.

    Raises:
        ValueError: The preset spans more than one module, or has no settings_crc.
    """
    if preset_modules(machine_config) != 1:
        raise ValueError("%s spans %d modules; FDA mode runs a 1x preset"
                         % (machine_config["id"], preset_modules(machine_config)))
    return generate_header_files(Path(output_directory) / "fda_preset.h", machine_config, context,
                                 symbol="FDA_PRESET_BLOB", section=".fda_preset")


def generate_console_fda_preset(output_directory, machine_config: Dict, context: str | None = None) -> Path:
    """Write the console's FDA preset header, ``fda_preset.h``: the preset's HV
    record (:func:`console_preset_record`, v2, carrying its voltage_range) as
    ``FDA_PRESET_HV``, in the ``.fda_preset`` section the console's FDA linker
    scripts put at the end of its application slot (page 61).

    Raises:
        ValueError: No settings_crc, voltage, sensitivity or voltage_range, or
            an unusable id.
    """
    record = console_preset_record(machine_config, version=2)
    guard = "FDA_PRESET_HV_H_"
    lines = [
        "// Generated by openlifu_sdk from preset '%s'." % machine_config["id"],
        "// Do not edit: regenerate with the SDK and copy the result here.",
        "#ifndef %s" % guard,
        "#define %s" % guard,
        "",
        "#include <stdint.h>",
        "",
        "// %g V in [%g, %g] for a transmitter of sensitivity %g, settings_crc 0x%08x."
        % ((preset_voltage(machine_config),) + preset_voltage_range(machine_config)
           + (preset_sensitivity(machine_config), machine_config["settings_crc"])),
        "// Laid out as PRESET_HV_RECORD v2 in hv_presets.h.",
        "// The linker puts it at the end of the application slot (the FDA_PRESET",
        "// region of the FDA linker scripts), so it is written with the image, the",
        "// bootloader's signature covers it, and the firmware only reads it.",
        '__attribute__((section(".fda_preset"), used))',
        "static const uint8_t FDA_PRESET_HV[%u] = {" % len(record),
    ]
    lines += ["\t" + " ".join("0x%02x," % b for b in record[i:i + 16]) for i in range(0, len(record), 16)]
    lines += ["};", "", "#endif  // %s" % guard, ""]
    path = Path(output_directory) / "fda_preset.h"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("wrote %s (%d bytes)", path, len(record))
    return path


# The console's half of a preset, as its presets-only image carries it.
CONSOLE_PRESET_MAGIC = b"OWHV"
CONSOLE_PRESET_VERSION = 1


def console_preset_record(machine_config: Dict, version: int = CONSOLE_PRESET_VERSION) -> bytes:
    """The console's half of a preset as a presets-only console reads it from
    flash (PRESET_HV_RECORD in its hv_presets.h): the preset's voltage and the
    sensitivity it suits, each as float32, the .json's settings_crc and the
    id, closed by a CRC-32 of all of that. :func:`generate_console_fda_preset`
    writes a v1 record into the console's FDA image.

    Version 2 (console-fw#21) inserts the preset's voltage_range, as float32
    low and high, after the sensitivity; :func:`generate_console_presets`
    writes v2 records.

    Raises:
        ValueError: No settings_crc, voltage or sensitivity, an unusable id,
            an unknown version, or a v2 record for a preset without a
            voltage_range.
    """
    if "settings_crc" not in machine_config:
        raise ValueError("%s has no settings_crc; pass settings_bytes to compile_preset"
                         % machine_config["id"])
    pid = str(machine_config["id"])
    if not 1 <= len(pid) <= PRESET_ID_MAX or any(not 0x20 <= ord(ch) <= 0x7E for ch in pid):
        raise ValueError("preset id %r must be 1..%d printable ASCII characters" % (pid, PRESET_ID_MAX))
    if version not in (1, 2):
        raise ValueError("console preset record version must be 1 or 2, got %r" % (version,))
    head = (CONSOLE_PRESET_MAGIC + bytes([version, len(pid), 0, 0])
            + struct.pack("<Iff", machine_config["settings_crc"], preset_voltage(machine_config),
                          preset_sensitivity(machine_config)))
    if version == 2:
        rng = preset_voltage_range(machine_config)
        if rng is None:
            raise ValueError("%s has no voltage_range; a v2 console record carries it" % pid)
        head += struct.pack("<ff", *rng)
    head += pid.encode("ascii")
    return head + struct.pack("<I", zlib.crc32(head) & 0xFFFFFFFF)


def verify_console_preset(hvcontroller, index: int, machine_config: Dict) -> Dict:
    """Prove the console's preset *index* is the one the host holds.

    Asks the console (OW_POWER_PRESET_GET) and compares id, settings_crc and
    the exact float32 voltage and sensitivity against *machine_config*, which
    must carry settings_crc. Returns the console's reply on success.

    Raises:
        ValueError: The console has no preset table (not an FDA_MODE image),
            or with every field that disagrees.
    """
    got = hvcontroller.get_preset(index)
    if got is None:
        raise ValueError("console has no preset table: not an FDA_MODE image")
    want = {"id": machine_config["id"], "settings_crc": machine_config["settings_crc"],
            "voltage": preset_voltage(machine_config), "sensitivity": preset_sensitivity(machine_config)}
    bad = ["%s: console=%s host=%s" % (k, (hex(got[k]) if isinstance(got[k], int) else got[k]),
                                        (hex(v) if isinstance(v, int) else v))
           for k, v in want.items() if got[k] != v]
    if bad:
        raise ValueError("console preset %d verification failed: %s" % (index, "; ".join(bad)))
    return got


def verify_preset(txdevice, index: int, machine_config: Dict) -> Dict:
    """Prove the device's preset *index* is the one the host holds.

    Asks the device (OW_PRESET_GET) and compares id, settings_crc, regs_crc
    and the thermal limits against *machine_config* -- captured locally with
    settings_bytes so both CRCs are known. Returns the device's reply on
    success. The reply is the first module's: the others are proven when
    they load, which each refuses unless its own regs_crc matches.

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


def preset_slots(machine_config: Dict, slot_size: int = PRESET_SLOT_SIZE) -> int:
    """How many of a PRESET_MODE module's slots this preset takes: 1, or
    2 if a module's blob is over *slot_size* (pass the device's own, from
    get_preset_capacity). A module stores only its own part, so this does not
    grow with the number of modules.

    Raises:
        ValueError: A blob is over two slots and cannot be stored.
    """
    size = max(len(preset_blob(machine_config, m)) for m in range(preset_modules(machine_config)))
    if size > 2 * slot_size:
        raise ValueError("%s: blob is %d bytes; a stored preset is at most %d"
                         % (machine_config["id"], size, 2 * slot_size))
    return 2 if size > slot_size else 1


def store_preset(txdevice, slot: int, machine_config: Dict) -> Dict:
    """Write a compiled preset to a PRESET_MODE slot and prove it took.

    Uploads each module its own part, then reads the slot back
    (:func:`verify_preset`), so a success means the device holds this preset
    under index *slot*. Returns the device's description of it. A preset
    whose module count differs from the transmitter's is refused before
    anything is written, as DeviceInterface.load_preset refuses it.

    Raises:
        ValueError: The blob cannot be stored, or the slot does not read back
            as this preset.
        LIFUError: The preset's module count differs from the transmitter's
            (LIFU_ERR_MODULE_COUNT_MISMATCH).
        LIFUDeviceError: The device refused the write (see write_preset).
    """
    preset_slots(machine_config)
    for module in _device_modules(txdevice, machine_config):
        txdevice.write_preset(slot, preset_blob(machine_config, module), module)
    return verify_preset(txdevice, slot, machine_config)


def load_preset(txdevice, index: int, machine_config: Dict, duration_index: int = 0) -> None:
    """Program every module from its part of preset *index* (OW_PRESET_LOAD).

    Each module is given its own regs_crc and the preset file's settings_crc,
    and refuses unless both are what it holds, so a success means every
    module loaded this preset. If one refuses, the others are unloaded again:
    the preset is loaded on all of them or on none.

    Raises:
        ValueError: *machine_config* has no settings_crc.
        LIFUError: The preset's module count differs from the transmitter's
            (LIFU_ERR_MODULE_COUNT_MISMATCH); nothing is sent, so
            whatever was loaded before stays loaded.
        LIFUDeviceError: A module refused (see TxDevice.load_preset).
    """
    if "settings_crc" not in machine_config:
        raise ValueError("%s has no settings_crc; pass settings_bytes to compile_preset"
                         % machine_config["id"])
    modules = list(_device_modules(txdevice, machine_config))
    try:
        for module in modules:
            txdevice.load_preset(index, regs_crc(machine_config, module), duration_index, module,
                                 settings_crc=machine_config["settings_crc"])
    except LIFUError:
        for module in modules:
            try:
                txdevice.unload_preset(module)
            except LIFUError:
                logger.warning("load_preset: module %d could not be unloaded", module)
        raise


def delete_preset(txdevice, slot: int) -> None:
    """Empty a PRESET_MODE slot on every module (OW_PRESET_DELETE): each keeps its
    own part of the preset there. Already empty is fine.

    Raises:
        LIFUDeviceError: A module refused (see TxDevice.delete_preset).
    """
    for module in range(txdevice.get_module_count()):
        txdevice.delete_preset(slot, module)


# TX7332 register 0x18 bit 30 is set by the chip itself and never written, so
# a read-back always differs from the blob there.
READBACK_MASK = {0x18: 0x40000000}


def loaded_registers(machine_config: Dict) -> Dict[tuple, int]:
    """What the chips hold once this preset is loaded: (chip, address) -> value,
    the base registers with the starting profile's on top. Chips count across
    the whole transmitter. Compare a read-back through READBACK_MASK."""
    want = {}
    for chip, cfg in enumerate(machine_config["chips"]):
        for start, values in cfg["registers"].items():
            for i, value in enumerate(values):
                want[(chip, start + i)] = value
        for addr, value in cfg["profiles"][machine_config["profile_index"] - 1].items():
            want[(chip, addr)] = value
    return want


def _device_modules(txdevice, machine_config: Dict) -> range:
    """The modules to address: every one the preset spans, which must be every
    one the transmitter has. A preset on a transmitter with more or fewer
    modules would be half stored or half loaded, so it is refused."""
    wanted, present = preset_modules(machine_config), txdevice.get_module_count()
    if present != wanted:
        raise LIFUError("%s spans %d transmitter module(s); the TX reports %d"
                        % (machine_config["id"], wanted, present),
                        code=LIFU_ERR_MODULE_COUNT_MISMATCH)
    return range(wanted)


# --------------------------------------------------------------------------
# Preset source files
# --------------------------------------------------------------------------

def preset_files(path) -> List[Path]:
    """The preset .json files at *path*, in the order the image indexes them.

    *path* is one preset file, taken as it is, or a directory. In a directory
    a preset is any JSON document carrying ``delays`` and ``apodization``,
    found either directly in it or one level down -- the application keeps
    each preset in its own folder next to its plot -- and anything else
    there, such as a constants.json, is skipped.

    Generation and the on-device test must agree on the order -- it is the
    index the host passes to OW_PRESET_LOAD -- so both take it from here.
    Natural order on the id: digit runs compare as numbers, so canine_5.0mm
    sorts before canine_10.0mm.
    """
    path = Path(path)
    if path.is_file():
        return [path]
    found = []
    for candidate in list(path.glob("*.json")) + list(path.glob("*/*.json")):
        try:
            doc = json.loads(candidate.read_bytes())
        except ValueError:
            continue
        if isinstance(doc, dict) and "delays" in doc and "apodization" in doc:
            found.append(candidate)

    def key(candidate):
        return [int(t) if t.isdigit() else t.lower() for t in re.split("([0-9]+)", preset_id(candidate))]
    return sorted(found, key=key)


def preset_id(path) -> str:
    """A preset id from its file name: the stem minus any trailing ``_settings``."""
    stem = Path(path).stem
    return stem[:-len("_settings")] if stem.endswith("_settings") else stem
