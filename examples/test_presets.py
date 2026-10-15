"""List, load, delete and verify the presets stored on a PRESET_MODE transmitter.

A PRESET_MODE transmitter keeps its presets in flash slots. Slot N is preset
index N; a preset too big for one slot takes the next one as well. Only the
transmitter is needed, and HV is never touched.

    python examples/test_presets.py --list-presets
    python examples/test_presets.py --load-preset <preset.json>            # first available slot
    python examples/test_presets.py --load-preset <preset.json> --slot 3   # that slot, replacing what it holds
    python examples/test_presets.py --load-preset <folder>                 # every preset in the folder
    python examples/test_presets.py --delete-preset 3                      # or: all
    python examples/test_presets.py --verify-preset 3 --json-preset-directory <folder>
    python examples/test_presets.py --verify-preset all --json-preset-directory <folder> --duration 1

--load-preset puts a preset .json on the device; every write is read back
and compared. Without --slot, a preset the device already holds (same id) is
replaced where it is and any other goes in the first available slot.

--verify-preset proves a stored preset against its .json in
--json-preset-directory, by slot, by id, or all of them:

  1. The device reports the id, settings_crc and regs_crc the host computes
     from the .json, with the same thermal limits and run lengths.
  2. The device accepts a load gated on the host's CRC, with the run length
     --duration picks from the preset's pulse_train_count_selections.
  3. Every register the preset writes reads back from the chips as computed.
  4. The trigger holds the preset's timing and that run length.

The last preset verified is left loaded on the chips.

An FDA_MODE transmitter carries its one preset in its image instead
(generate_presets.py); it is tested with test_fda_mode.py.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from openlifu_sdk.io.exceptions import LIFUCommunicationError, LIFUDeviceError, LIFUNotConnectedError
from openlifu_sdk.io.LIFUConfig import (
    OW_BAD_CRC,
    OW_INVALID_PACKET,
    OW_PRESET_BUSY,
    OW_PRESET_NO_ROOM,
    OW_PRESET_SLOT_TAIL,
)
from openlifu_sdk.io.LIFUInterface import LIFUInterface
from openlifu_sdk.io.LIFUTXDevice import PRESET_SLOT_SIZE, PRESET_SLOTS
from openlifu_sdk.io.LIFUTXPresets import (
    READBACK_MASK,
    compile_preset,
    delete_preset,
    load_preset,
    loaded_registers,
    preset_blob,
    preset_files,
    preset_id,
    preset_modules,
    preset_slots,
    regs_crc,
    store_preset,
    train_count_selections,
    verify_preset,
)

# Why a preset could not be stored or deleted, for the codes that have a plain answer.
REFUSALS = {
    OW_PRESET_BUSY: "the trigger is running; stop it before changing the presets",
    OW_PRESET_NO_ROOM: "this preset needs the next slot too, and that slot is taken or does not exist",
    OW_PRESET_SLOT_TAIL: "that slot holds the second half of the preset in the slot before it",
    OW_BAD_CRC: "the device did not accept the preset as a valid blob",
}

# Most registers TxDevice.read_block takes in one packet.
READ_BLOCK_MAX = 62

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


def reason(err):
    """A device's refusal in a few words: its error code, and the reason the
    firmware sent with it when it sent one. Any other error as it is."""
    code = getattr(err, "device_error_code", None)
    if code is None:
        return str(err)[:200]
    text = (getattr(err, "device_error_data", None) or b"").rstrip(b"\0").decode("ascii", "replace")
    return "error 0x%02X%s" % (code, ': "%s"' % text if text else "")


# The transmitter not answering: it restarts itself under a long burst of traffic.
DROPPED = (LIFUCommunicationError, LIFUNotConnectedError)
# Longest wait for it to come back on USB.
RETURN_SECONDS = 40.0


class Link:
    """The transmitter, reconnected when it drops out mid-run.

    A burst of a few hundred packets can make the transmitter restart; it is
    back on USB some seconds later with nothing loaded. recover() waits for
    it, and every dropout is printed -- it is a real event, just not a preset
    failure.
    """

    def __init__(self):
        self.iface = self.tx = None
        self.hv_connected = False
        self.reconnects = 0
        if not self.open():
            sys.exit("no transmitter connected")

    def open(self):
        self.iface = LIFUInterface(run_async=False, voltage_table_selection="dvt")
        tx_ok, self.hv_connected = self.iface.is_device_connected()
        self.tx = self.iface.txdevice
        return tx_ok

    def __getattr__(self, name):
        return getattr(self.tx, name)

    def recover(self, err):
        """Wait for a transmitter that has stopped answering, and reconnect to it."""
        self.reconnects += 1
        print("   transmitter stopped answering (%s) -- waiting for it to come back" % type(err).__name__, flush=True)
        deadline = time.time() + RETURN_SECONDS
        while True:
            self.iface.close()
            time.sleep(4.0)
            if self.open():
                return
            if time.time() > deadline:
                sys.exit("the transmitter did not come back within %d s; cycle its power" % RETURN_SECONDS)

    def steady(self, step):
        """Run a step that changes the presets; if the transmitter drops out, wait for it and run
        the step once more -- they are all safe to repeat."""
        try:
            return step()
        except DROPPED as e:
            self.recover(e)
            return step()

    def read_registers(self, keys):
        """{(chip, address): value} for *keys*, read a run of consecutive addresses
        at a time: a packet per register is the burst that makes it drop out."""
        keys, got, i = sorted(keys), {}, 0
        while i < len(keys):
            chip, start = keys[i]
            n = 1
            while i + n < len(keys) and n < READ_BLOCK_MAX and keys[i + n] == (chip, start + n):
                n += 1
            for k, value in enumerate(self.tx.read_block(chip, start, n)):
                got[(chip, start + k)] = value
            i += n
        return got


def host_presets(path):
    """Every preset .json at *path* -- one file, or a folder of them -- compiled, in id order."""
    path = Path(path)
    files = preset_files(path)
    if not files:
        sys.exit("no .json presets in %s" % path)
    presets = []
    for f in files:
        raw = f.read_bytes()
        try:
            presets.append(compile_preset(json.loads(raw), preset_id=preset_id(f), settings_bytes=raw))
        except ValueError as e:
            sys.exit("%s is not a usable preset: %s" % (f, e))
    return presets


# --------------------------------------------------------------------------
# What the device holds
# --------------------------------------------------------------------------

def slot_layout(tx):
    """(slots, slot size) as the transmitter reports them, or the
    SDK's own figures from firmware that predates the query."""
    capacity = tx.get_preset_capacity()
    return (capacity["slots"], capacity["slot_size"]) if capacity else (PRESET_SLOTS, PRESET_SLOT_SIZE)


def device_slots(tx):
    """Every preset index the device has, in order, as get_preset_slot() gives
    it, the empty ones and the second halves of long presets too: one for an
    FDA_MODE image, every slot for PRESET_MODE. None when the image has no
    presets at all (neither)."""
    try:
        first = tx.get_preset_slot(0)
    except LIFUDeviceError:
        return None
    count = first.get("count") or slot_layout(tx)[0]
    slots = [first]
    for i in range(1, count):
        try:
            slots.append(tx.get_preset_slot(i))
        except LIFUDeviceError as e:
            # Past the last index: an FDA_MODE image has one, which an empty slot 0 does not say.
            if e.device_error_code != OW_INVALID_PACKET:
                raise
            break
    for e, after in zip(slots, slots[1:]):
        if after["state"] == "tail":
            e["slots"] = 2
    return slots


def print_inventory(tx, slots):
    """One row per preset index; for flash slots, how many are free and the empty ones too."""
    if slots is None:
        print("  device reports no presets -- is it running a PRESET_MODE or FDA_MODE image?")
        return
    capacity = tx.get_preset_capacity()
    if capacity is not None:
        print("  slots: %d free of %d (%d kB each)"
              % (capacity["free"], capacity["slots"], capacity["slot_size"] // 1024))
    modules = tx.get_module_count()
    if any(e["state"] == "preset" for e in slots):
        print("  %-4s %-28s %-6s %-9s %-13s %-11s %-13s %s"
              % ("slot", "id", "config", "profiles", "settings_crc", "regs_crc", "start/shutoff", "train counts"))
    for e in slots:
        if e["state"] != "preset":
            print("  %-4d %s" % (e["index"], "(empty)" if e["state"] == "empty"
                                else "(second half of the preset in slot %d)" % (e["index"] - 1)))
            continue
        flags = [] if e["regs_crc"] == e["baked_regs_crc"] else ["stored CRC is 0x%08x" % e["baked_regs_crc"]]
        # 1x, 2x: the modules the preset was made for.
        spans = e.get("modules")
        if spans and spans != modules:
            flags.append("made for %d module(s), %d connected" % (spans, modules))
        if e.get("slots", 1) > 1:
            flags.append("takes %d slots" % e["slots"])
        print("  %-4d %-28s %-6s %-9d 0x%08x    0x%08x  %-13s %s%s"
              % (e["index"], e["id"], "%dx" % spans if spans else "?", e["profile_count"],
                 e["settings_crc"], e["regs_crc"],
                 "-" if e["start_c"] is None else "%g/%g C" % (e["start_c"], e["shutoff_c"]),
                 "/".join(str(c) for c in e["train_counts"]) or "-",
                 "".join("  <-- " + f for f in flags)))
    print("  settings_crc: CRC-32 of the preset's .json file; regs_crc: CRC-32 of what it was converted into")
    unknown = {e.get("modules") for e in slots if e["state"] == "preset"} - set(range(1, 256))
    if None in unknown:
        print("  config ?: this transmitter firmware does not report 1x/2x")
    elif unknown:
        print("  config ?: stored before presets recorded 1x/2x; load the preset again to fill it in")


# --------------------------------------------------------------------------
# --load-preset, --delete-preset
# --------------------------------------------------------------------------

def need_slots(tx):
    if tx.get_preset_capacity() is None:
        sys.exit("this transmitter has no preset slots: it is not a PRESET_MODE image "
                 "(an FDA_MODE image's preset is read-only; store it with a PRESET_MODE image)")


def first_free(slots, need):
    """The first slot with *need* empty slots from it on, or None."""
    states = [e["state"] for e in slots]
    for i in range(len(states) - need + 1):
        if states[i:i + need] == ["empty"] * need:
            return i
    return None


def load_presets(tx, path, slot):
    """--load-preset: store one preset .json, or every one in a folder."""
    need_slots(tx)
    presets = host_presets(path)
    if slot is not None and len(presets) > 1:
        sys.exit("--slot places one preset; %s holds %d, which go to the first available slots" % (path, len(presets)))
    slot_size = slot_layout(tx)[1]
    for mc in presets:
        need = preset_slots(mc, slot_size)
        where, how = slot, "stored"
        if where is None:
            slots = device_slots(tx)
            # Loading a preset again updates it rather than adding a second copy.
            where = next((e["index"] for e in slots if e["state"] == "preset" and e["id"] == mc["id"]), None)
            how = "replaced"
            if where is None:
                where, how = first_free(slots, need), "stored"
            if where is None:
                sys.exit("no room for %s: it needs %d free slot(s) in a row. Delete a preset first." % (mc["id"], need))
        tx.steady(lambda: store_preset(tx, where, mc))
        spans = "" if preset_modules(mc) == 1 else " on each of %d modules" % preset_modules(mc)
        print("  [%2d] %-28s %s: %d bytes, %d slot(s)%s, regs_crc 0x%08x"
              % (where, mc["id"], how, len(preset_blob(mc)), need, spans, regs_crc(mc)))


def delete_presets(tx, which, slot):
    """--delete-preset: one slot, or all of them."""
    need_slots(tx)
    if which == "all":
        # Back to front, so a long preset's second slot is never asked for directly.
        held = [e["index"] for e in reversed(device_slots(tx)) if e["state"] == "preset"]
    else:
        if which is True and slot is None:
            sys.exit("--delete-preset needs a slot: --delete-preset 3, or all")
        try:
            held = [slot if which is True else int(which)]
        except ValueError:
            sys.exit("--delete-preset takes a slot number or all, not %r" % which)
    for index in held:
        tx.steady(lambda: delete_preset(tx, index))
        print("  [%2d] emptied" % index)
    if not held:
        print("  nothing stored")


# --------------------------------------------------------------------------
# --verify-preset
# --------------------------------------------------------------------------

def examine(tx, e, mc, duration):
    """What verify_slot() finds, as (check, passed, detail) rows, and whether the
    preset got loaded. Raises if the transmitter drops out part way."""
    name = "[%d] %s" % (e["index"], e["id"])
    counts = train_count_selections(mc)
    try:
        verify_preset(tx, e["index"], mc)
        same = (e["regs_crc"] == e["baked_regs_crc"] and e["train_counts"] == counts
                and (e.get("modules") or preset_modules(mc)) == preset_modules(mc))
        found = [(name + ": the device reports the .json's preset", same,
                  "settings_crc 0x%08x, regs_crc 0x%08x, run lengths %s"
                  % (e["settings_crc"], e["regs_crc"], e["train_counts"]))]
    except ValueError as err:
        return [(name + ": the device reports the .json's preset", False, str(err))], False
    if not 0 <= duration < len(counts):
        return found + [(name + ": --duration %d is one of its run lengths" % duration, False,
                         "it offers %s" % counts)], False
    try:
        load_preset(tx, e["index"], mc, duration)
    except LIFUDeviceError as err:
        return found + [(name + ": loads on the host's CRC", False, reason(err))], False
    found.append((name + ": loads on the host's CRC", True,
                  "run length %d = %d train(s)" % (duration, counts[duration])))

    chips = tx.enum_tx7332_devices()
    want = {k: v for k, v in loaded_registers(mc).items() if k[0] < chips}
    read = tx.read_registers(want)
    bad = ["chip%d 0x%04x host=0x%08x read=0x%08x" % (k[0], k[1], v, read[k]) for k, v in want.items()
           if (read[k] ^ v) & ~READBACK_MASK.get(k[1], 0)]
    short = "" if len(mc["chips"]) <= chips else ", chips 0-%d of %d" % (chips - 1, len(mc["chips"]))
    found.append((name + ": the chips hold its registers", not bad,
                  "%d of %d%s%s" % (len(want) - len(bad), len(want), short, "".join("; " + b for b in bad[:3]))))

    cfg = tx.get_trigger_json()
    got = (cfg["TriggerFrequencyHz"], cfg["TriggerPulseCount"], cfg["TriggerPulseTrainInterval"],
           cfg["TriggerPulseTrainCount"])
    timing = (mc["trigger"]["frequency_hz"], int(mc["pulse_count"]), mc["trigger"]["pulse_train_interval_us"],
              counts[duration])
    found.append((name + ": the trigger holds its timing", got == timing,
                  "%d Hz, %d pulse(s) per train, train interval %d us, %d train(s)" % got
                  + ("" if got == timing else "; the .json gives %d / %d / %d / %d" % timing)))
    return found, True


def verify_slot(tx, e, mc, duration):
    """One preset on the device against its .json: what it reports, a CRC-gated
    load, the chips read back, and the trigger. True once it has loaded it."""
    # A transmitter that drops out comes back with nothing loaded, so the preset is examined afresh.
    for _ in range(3):
        try:
            found, loaded = examine(tx, e, mc, duration)
            break
        except DROPPED as err:
            tx.recover(err)
    else:
        check("[%d] %s: examined without the transmitter dropping out" % (e["index"], e["id"]), False,
              "it dropped out on each of 3 tries")
        return False
    for row in found:
        check(*row)
    return loaded


def verify_presets(tx, which, directory, duration):
    """--verify-preset: a slot, an id, or all, against the .json files in *directory*."""
    if directory is None:
        sys.exit("--verify-preset needs --json-preset-directory: the folder holding the .json it was loaded from")
    host = host_presets(directory)
    held = [e for e in device_slots(tx) or [] if e["state"] == "preset"]
    if which == "all":
        chosen = held
    else:
        chosen = [e for e in held if str(e["index"]) == which or e["id"] == which]
    if not chosen:
        sys.exit("nothing to verify: no preset %s on the device -- see --list-presets"
                 % ("at all" if which == "all" else "in slot or with id %r" % which))
    print("\n-- verify against %s --" % directory)
    loaded = None
    for e in chosen:
        # The .json with its id, so a file edited since gets its differences reported;
        # failing that the same file under another name.
        mc = next((m for m in host if m["id"] == e["id"]), None) \
            or next((m for m in host if m["settings_crc"] == e["settings_crc"]), None)
        if mc is None:
            check("[%d] %s: has a .json in the folder" % (e["index"], e["id"]), False, "none with its id or settings_crc")
            continue
        if verify_slot(tx, e, mc, duration):
            loaded = e
    if which == "all":
        missing = sorted({m["id"] for m in host} - {e["id"] for e in held})
        if missing:
            print("  in the folder but not on the device: %s" % ", ".join(missing))
    n_ok = sum(results)
    print("\nPRESET VERIFY: %d/%d checks passed (%d transport reconnect(s))%s"
          % (n_ok, len(results), tx.reconnects,
             "" if loaded is None else "; [%d] %s is left loaded" % (loaded["index"], loaded["id"])))
    return 0 if n_ok == len(results) else 1


def status(tx):
    modules = tx.get_module_count()
    return "firmware: %s | config: %dx (%d module%s)" % (tx.get_version(), modules, modules, "" if modules == 1 else "s")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-presets", action="store_true", help="show what is on the device, slot by slot")
    ap.add_argument("--json-preset-directory", type=Path, metavar="DIR",
                    help="folder holding the preset .json file(s) --verify-preset checks against")
    ap.add_argument("--duration", type=int, default=0, metavar="IDX",
                    help="which of a preset's pulse_train_count_selections --verify-preset loads it with "
                         "(default 0, the first)")
    ap.add_argument("--load-preset", type=Path, metavar="JSON",
                    help="store a preset .json on the device, or every preset in a folder")
    ap.add_argument("--delete-preset", nargs="?", const=True, metavar="SLOT",
                    help="empty a slot (or the one --slot names); all empties every slot")
    ap.add_argument("--slot", type=int, metavar="IDX",
                    help="slot for --load-preset (default: where the preset already is, else the first available)")
    ap.add_argument("--verify-preset", metavar="SLOT",
                    help="prove a stored preset against --json-preset-directory: a slot, an id, or all")
    args = ap.parse_args()

    tx = Link()
    print(status(tx))
    changed = args.delete_preset is not None or args.load_preset is not None
    try:
        if args.delete_preset is not None:
            print("\n-- delete --")
            delete_presets(tx, args.delete_preset, args.slot)
        if args.load_preset is not None:
            print("\n-- load --")
            load_presets(tx, args.load_preset, args.slot)
    except LIFUDeviceError as e:
        sys.exit("refused: %s" % (REFUSALS.get(e.device_error_code) or reason(e)))
    except ValueError as e:
        sys.exit("refused: %s" % e)

    # With nothing else asked for, the list is what the script shows.
    if args.list_presets or changed or args.verify_preset is None:
        print("\n-- presets on the device --")
        print_inventory(tx, device_slots(tx))
    if args.verify_preset is not None:
        return verify_presets(tx, args.verify_preset, args.json_preset_directory, args.duration)
    return 0


if __name__ == "__main__":
    sys.exit(main())
