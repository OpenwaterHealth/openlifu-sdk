"""Verify the presets baked into a transmitter's FDA_MODE image, on the device.

Only input: the directory of preset .json files the image was generated from.
The host computes each preset's register mapping and CRCs itself and the
device must agree -- nothing is read from the generated headers.

Against a connected transmitter running an FDA_MODE build, this proves:

  1. Every preset the image reports (OW_PRESET_GET) carries the id,
     settings_crc and regs_crc the host computes from the same source .json,
     and the device's live CRC over its own flash tables equals what was baked.
  2. The gates hold: an out-of-range index, a short OW_PRESET_LOAD payload, a
     load with the wrong regs_crc (OW_BAD_CRC), and a host TX7332 register write
     are all refused.
  3. A CRC-gated load puts exactly the host-computed registers on the silicon
     (the chips are read before and after the load), and the delay profiles
     cycle through the preset's execution order once the trigger runs.

Operator modes. --load-preset needs no source .json: the device's own CRC is
used unless --presets is given, in which case the host-computed CRC gates the
load. --run is a sonication the way the application does one: HV set to the
preset's voltage (from --presets) or --voltage, start_sonication (HV on,
settle, trigger), then stop_sonication (trigger off, HV off) after the time.

    python examples/test_presets.py --list-presets
    python examples/test_presets.py --load-preset canine_10.0mm_rastered
    python examples/test_presets.py --presets <dir> --load-preset 3 --run 5     # 5 s at the preset's voltage
    python examples/test_presets.py --load-preset 3 --run 5 --voltage 20        # index works too

Which preset is on the chips right now, from the registers alone: every
preset in the directory is computed on the host and compared with a readback.

    python examples/test_presets.py --presets <dir> --identify

Full verification against the source .json directory:

    python examples/test_presets.py --presets unit-test/device_preset_outputs
    python examples/test_presets.py --presets unit-test/device_preset_outputs --readback 3
"""

import argparse
import json
import math
import struct
import sys
import time
from pathlib import Path

from openlifu_sdk.io.exceptions import LIFUCommunicationError, LIFUDeviceError, LIFUNotConnectedError
from openlifu_sdk.io.LIFUConfig import OW_CONTROLLER, OW_PRESET_LOAD
from openlifu_sdk.io.LIFUInterface import LIFUInterface
from openlifu_sdk.io.LIFUTXPresets import capture_machine_config, preset_files, preset_id, regs_crc, verify_preset

# TX7332 register 0x18 bit 30 is set by the chip itself and never written, so
# a read-back always differs from the table there.
READBACK_MASK = {0x18: 0x40000000}

# How long the trigger runs while the active delay profile is sampled.
RASTER_SECONDS = 2.5

# The trigger config the transmitter firmware programs at power-on (main.c):
# 10 Hz, 5 pulses of 2000 us, 2 trains, continuous mode. Seeing it means the
# device has rebooted since anything was loaded.
BOOT_TRIGGER = {"TriggerFrequencyHz": 10, "TriggerPulseCount": 5, "TriggerPulseWidthUsec": 2000,
                "TriggerPulseTrainCount": 2, "TriggerMode": 1}

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


def expect_error(name, fn, want_sub=None):
    try:
        fn()
        check(name, False, "no error raised")
    except LIFUDeviceError as e:
        msg = str(e)
        check(name, want_sub is None or want_sub.lower() in msg.lower(), "-> " + msg[:110])


class Link:
    """The transmitter, reopened if the transport drops mid-run.

    A few hundred back-to-back register reads can make the transmitter drop
    off the bus; it comes straight back on a fresh connection. Reads go
    through read(), which paces them and resumes after a reconnect, so a long
    read-back finishes instead of aborting the test. Every reconnect is
    printed -- it is a real event, just not a preset failure.
    """

    def __init__(self):
        self.iface = self.tx = None
        self.hv_connected = False
        self.reconnects = 0
        self.open()

    def open(self):
        self.iface = LIFUInterface(run_async=False, voltage_table_selection="dvt")
        tx_ok, self.hv_connected = self.iface.is_device_connected()
        if not tx_ok:
            sys.exit("no transmitter connected")
        self.tx = self.iface.txdevice

    def __getattr__(self, name):
        return getattr(self.tx, name)

    def read(self, chip, addr):
        for _ in range(3):
            try:
                value = self.tx.read_register(chip, addr)
                time.sleep(0.01)
                return value
            except (LIFUCommunicationError, LIFUNotConnectedError) as e:
                self.reconnects += 1
                print("   transport dropped (%s) -- reconnecting and resuming" % type(e).__name__, flush=True)
                time.sleep(2.0)
                self.open()
        raise RuntimeError("read_register keeps failing at chip%d 0x%04x" % (chip, addr))


def load_presets(src):
    """(id, machine_config) per preset in image order, plus each preset's voltage from its .json."""
    out, voltages = [], {}
    for p in preset_files(src):
        raw = p.read_bytes()
        doc = json.loads(raw)
        pid = preset_id(p)
        out.append((pid, capture_machine_config(None, doc, preset_id=pid, settings_bytes=raw)))
        voltages[pid] = float(doc.get("voltage", 0.0))
    return out, voltages


def device_presets(tx):
    """Every preset the image reports, by index; [] when the image has none (not FDA_MODE)."""
    try:
        first = tx.get_preset(0)
    except LIFUDeviceError:
        return []
    return [first] + [tx.get_preset(i) for i in range(1, first["count"])]


def print_inventory(entries):
    if not entries:
        print("  device reports no presets -- is it running an FDA_MODE image?")
        return
    print("  %-4s %-28s %-6s %-9s %-11s %-11s %s"
          % ("idx", "id", "chips", "profiles", "settings", "regs(live)", "regs(baked)"))
    for e in entries:
        flag = "" if e["regs_crc"] == e["baked_regs_crc"] else "  <-- live CRC != baked CRC"
        print("  %-4d %-28s %-6d %-9d 0x%08x  0x%08x  0x%08x%s"
              % (e["index"], e["id"], e["chip_count"], e["profile_count"],
                 e["settings_crc"], e["regs_crc"], e["baked_regs_crc"], flag))


def silicon_addresses(mc):
    """(chip, addr) -> value for everything presets_load() writes: base registers, starting profile on top."""
    want = {}
    for chip, cfg in enumerate(mc["chips"]):
        for start, values in cfg["registers"].items():
            for i, v in enumerate(values):
                want[(chip, start + i)] = v
        for addr, v in cfg["profiles"][mc["profile_index"] - 1].items():
            want[(chip, addr)] = v
    return want


def read_silicon(tx, keys):
    return {k: tx.read(*k) for k in keys}


def find_preset(inventory, key):
    """The device's entry for a preset id, or for an index."""
    for e in inventory:
        if e["id"] == key:
            return e
    if key.isdigit() and int(key) < len(inventory):
        return inventory[int(key)]
    sys.exit("no preset %r on the device -- see --list-presets" % key)


def load_one(tx, inventory, presets, key, train_count):
    """CRC-gated load of one preset; the host's own CRC when --presets was given."""
    e = find_preset(inventory, key)
    host = dict(presets).get(e["id"])
    if host is not None:
        crc = regs_crc(host)
        if crc != e["regs_crc"] or host["settings_crc"] != e["settings_crc"]:
            sys.exit("host and device disagree on %s: regs 0x%08x vs 0x%08x, settings 0x%08x vs 0x%08x"
                     % (e["id"], crc, e["regs_crc"], host["settings_crc"], e["settings_crc"]))
        source = "host-computed"
    elif presets:
        sys.exit("%s is not among the host presets -- not loading it unverified" % e["id"])
    else:
        crc = e["regs_crc"]
        source = "the device's own; no --presets to check against"
    tx.load_preset(e["index"], crc, train_count=train_count)
    print("  loaded [%d] %s  regs_crc 0x%08x (%s)  %d train(s)" % (e["index"], e["id"], crc, source, train_count))
    return e


def trigger_config(tx):
    """The device's trigger config and its train period in seconds."""
    cfg = tx.get_trigger_json()
    train_us = cfg["TriggerPulseTrainInterval"]
    period = train_us * 1e-6 if train_us else cfg["TriggerPulseCount"] / float(cfg["TriggerFrequencyHz"])
    return cfg, period


def run_for(tx, seconds, voltage):
    """A sonication as the application does it: HV to *voltage*, start_sonication,
    watch the active delay profile, stop_sonication after *seconds*."""
    cfg, period = trigger_config(tx)
    trains = cfg["TriggerPulseTrainCount"]
    print("  trigger: %d Hz, %d pulse(s)/train, train period %.4g s, %d train(s) = %.4g s of output"
          % (cfg["TriggerFrequencyHz"], cfg["TriggerPulseCount"], period, trains, trains * period))
    if trains * period < seconds:
        print("  note: the sequence ends after %.4g s, before the %.4g s run is up" % (trains * period, seconds))
    hv = tx.iface.hvcontroller
    hv.set_voltage(voltage)
    print("  HV set to %.2f V; start_sonication (HV on, settle, trigger)" % voltage, flush=True)
    seen = []
    t0 = time.time()
    try:
        tx.iface.start_sonication(turn_hv_on=True, wait_for_settle=True)
        t0 = time.time()
        print("  running: HV %.2f V measured" % hv.get_voltage(), flush=True)
        while time.time() - t0 < seconds:
            seen.append(tx.get_delay_profile(0))
            time.sleep(0.25)
    finally:
        tx.iface.stop_sonication(turn_hv_off=True)
    print("  ran %.2f s; stop_sonication (trigger off, HV off: hv_on=%s); active delay profiles seen: %s"
          % (time.time() - t0, hv.get_hv_status(), sorted(set(seen))))


def operate(tx, inventory, presets, voltages, args):
    print("\n-- load / run --")
    voltage = None
    if args.run:
        if not tx.hv_connected:
            sys.exit("--run needs the console (HV controller) connected")
        voltage = args.voltage
        if voltage is None and args.load_preset is not None:
            voltage = voltages.get(find_preset(inventory, args.load_preset)["id"])
        if not voltage:
            sys.exit("--run needs a voltage: --voltage, or --presets so the preset's own voltage is used")
    if args.load_preset is not None:
        load_one(tx, inventory, presets, args.load_preset, train_count=1)
        if args.run:
            # The preset fixes the timing; the run length is the host's, so size
            # the train count to outlast the requested run.
            _, period = trigger_config(tx)
            trains = int(math.ceil(args.run / period)) + 1
            if trains > 1:
                load_one(tx, inventory, presets, args.load_preset, train_count=trains)
    if args.run:
        run_for(tx, args.run, voltage)
    return 0


def preset_timing(mc):
    """(Hz, pulses per train, train interval us) the preset bakes into the trigger."""
    interval_ms = float(mc["pulse_interval_ms"])
    return (round(1000.0 / interval_ms) if interval_ms else 0, int(mc["pulse_count"]),
            round(float(mc.get("pulse_train_interval_s", 0)) * 1e6))


def identify(tx, presets):
    """Which host preset is on the chips: read every register any preset writes
    plus the trigger timing, then see whose table is fully consistent with it."""
    chips = tx.enum_tx7332_devices()
    keys = set()
    for _, mc in presets:
        keys |= {k for k in silicon_addresses(mc) if k[0] < chips}
    print("  reading %d registers from %d chip(s)" % (len(keys), chips), flush=True)
    got = read_silicon(tx, sorted(keys))
    cfg = tx.get_trigger_json()
    timing = (cfg["TriggerFrequencyHz"], cfg["TriggerPulseCount"], cfg["TriggerPulseTrainInterval"])
    print("  trigger: %d Hz, %d pulse(s)/train, train interval %d us" % timing)

    rows = []
    for pid, mc in presets:
        want = {k: v for k, v in silicon_addresses(mc).items() if k[0] < chips}
        # The trigger ISR rewrites the per-profile registers as it cycles, so
        # those may hold any of the preset's profiles, not just the starting one.
        ctrl = {}
        for chip, chip_cfg in enumerate(mc["chips"][:chips]):
            for prof, regs in enumerate(chip_cfg["profiles"], start=1):
                for addr, v in regs.items():
                    ctrl.setdefault((chip, addr), {}).setdefault(v, prof)
        match, active = 0, set()
        for k, v in want.items():
            if k in ctrl:
                if got[k] in ctrl[k]:
                    match += 1
                    if k[1] == 0x16:
                        active.add(ctrl[k][got[k]])
            else:
                m = READBACK_MASK.get(k[1], 0)
                if (got[k] & ~m) == (v & ~m):
                    match += 1
        timing_ok = preset_timing(mc) == timing
        rows.append((match == len(want) and timing_ok, len(want), match, timing_ok, pid, sorted(active),
                     len(mc["chips"])))
    rows.sort(key=lambda r: (r[0], r[2] / r[1], r[3], r[1]), reverse=True)

    full = [r for r in rows if r[0]]
    print("  %-28s %-10s %s" % ("preset", "registers", "timing"))
    for ok, total, match, timing_ok, pid, active, nchips in rows[:len(full) + 3]:
        note = " (chips 0-%d of %d)" % (chips - 1, nchips) if nchips > chips else ""
        prof = ", delay profile %s active" % "/".join(map(str, active)) if ok and active else ""
        print("  %-28s %3d/%-3d    %s%s%s" % (pid, match, total, "same" if timing_ok else "differs", prof, note))
    if len(full) == 1:
        print("  loaded: %s" % full[0][4])
    elif full:
        print("  consistent with %d presets (%s): their registers and timing coincide on these chips"
              % (len(full), ", ".join(r[4] for r in full)))
    else:
        print("  no preset matches what is on the chips; closest is %s (%d/%d)" % (rows[0][4], rows[0][2], rows[0][1]))
        if all(cfg.get(k) == v for k, v in BOOT_TRIGGER.items()):
            print("  the trigger holds the firmware's power-on config: the transmitter has rebooted since the last load")
    return 0 if len(full) == 1 else 1


def verify(tx, inventory, presets, args):
    host_ids = [pid for pid, _ in presets]
    dev_ids = [e["id"] for e in inventory]
    for pid in sorted(set(dev_ids) - set(host_ids)):
        check("device preset %s exists on host" % pid, False, "not in %s" % args.presets)
    for pid in sorted(set(host_ids) - set(dev_ids)):
        check("host preset %s exists on device" % pid, False, "not baked into this image")
    check("same presets in the same order", host_ids == dev_ids,
          "%d on both" % len(dev_ids) if host_ids == dev_ids else "host %s / device %s" % (host_ids, dev_ids))

    print("\n-- readback: id / settings_crc / regs_crc --")
    for i, (pid, mc) in enumerate(presets):
        if i >= len(inventory):
            check("get_preset[%d] %s" % (i, pid), False, "not on the device")
            continue
        got = inventory[i]
        host_crc = regs_crc(mc)
        ok = (got["id"] == pid and got["count"] == len(presets)
              and got["settings_crc"] == mc["settings_crc"]
              and got["regs_crc"] == got["baked_regs_crc"] == host_crc)
        check("get_preset[%d] %s" % (i, pid), ok,
              "settings=0x%08x regs live=0x%08x baked=0x%08x host=0x%08x"
              % (got["settings_crc"], got["regs_crc"], got["baked_regs_crc"], host_crc))
        try:
            verify_preset(tx, i, mc)
            check("verify_preset[%d]" % i, True)
        except ValueError as e:
            check("verify_preset[%d]" % i, False, str(e))

    print("\n-- gates --")
    expect_error("get_preset(%d) out of range" % len(inventory), lambda: tx.get_preset(len(inventory)))
    expect_error("load with 4-byte payload rejected", lambda: tx.send_checked(
        packet_type=OW_CONTROLLER, command=OW_PRESET_LOAD, reserved=0, data=struct.pack("<I", 1), op="bad_load"))
    expect_error("load with wrong regs_crc refused (OW_BAD_CRC)",
                 lambda: tx.load_preset(0, regs_crc(presets[0][1]) ^ 0xDEADBEEF, 1), "0xfd")
    expect_error("host register write refused in FDA mode", lambda: tx.write_register(0, 0x1B, 0))

    print("\n-- load + readback --")
    idx = args.readback
    if not 0 <= idx < len(presets):
        sys.exit("--readback %d: the host has %d preset(s)" % (idx, len(presets)))
    pid, mc = presets[idx]
    chips = tx.enum_tx7332_devices()
    if len(mc["chips"]) > chips:
        sys.exit("--readback %d: %s needs %d chips, this transmitter has %d" % (idx, pid, len(mc["chips"]), chips))
    want = silicon_addresses(mc)
    before = read_silicon(tx, want)
    print("   read %d registers from the chips before the load" % len(before))
    tx.load_preset(idx, regs_crc(mc), train_count=50)
    check("load_preset(%d %s, correct crc)" % (idx, pid), True)
    after = read_silicon(tx, want)
    bad = 0
    for (chip, addr), w in want.items():
        m = READBACK_MASK.get(addr, 0)
        if (after[(chip, addr)] & ~m) != (w & ~m):
            bad += 1
            if bad <= 5:
                print("      mismatch chip%d 0x%04x host=0x%08x read=0x%08x" % (chip, addr, w, after[(chip, addr)]))
    check("silicon == host-computed registers after the load", bad == 0, "%d registers" % len(want))
    print("   the load changed %d of %d registers" % (sum(1 for k in want if before[k] != after[k]), len(want)))

    order = mc["execution_order"]
    tx.start_trigger()
    t0 = time.time()
    seen = []
    while time.time() - t0 < RASTER_SECONDS:
        seen.append(tx.get_delay_profile(0))
        time.sleep(0.03)
    tx.stop_trigger()
    print("   active delay profile samples:", seen)
    check("profiles cycle while running", set(seen) == set(order),
          "saw %s, execution order %s" % (sorted(set(seen)), order))

    n_ok = sum(results)
    print("\nFDA PRESET DEVICE TEST: %d/%d checks passed (%d transport reconnect(s))"
          % (n_ok, len(results), tx.reconnects))
    return 0 if n_ok == len(results) else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--presets", type=Path,
                    help="directory of preset .json files: the verification source, optional for --load-preset")
    ap.add_argument("--list-presets", action="store_true", help="show the presets on the device and exit")
    ap.add_argument("--load-preset", metavar="ID", help="CRC-gated load of one preset, by id or index")
    ap.add_argument("--run", type=float, metavar="SECONDS",
                    help="sonicate for this long (HV on, trigger, HV off), after --load-preset or on whatever is loaded")
    ap.add_argument("--voltage", type=float, metavar="V",
                    help="HV voltage for --run (default: the preset's own voltage, which needs --presets)")
    ap.add_argument("--identify", action="store_true",
                    help="read the chips and say which preset from --presets is on them")
    ap.add_argument("--readback", type=int, default=1, metavar="IDX",
                    help="verification: the preset to load and read back from the chips (default 1)")
    args = ap.parse_args()

    tx = Link()
    print("firmware:", tx.get_version(), "| chips:", tx.enum_tx7332_devices(),
          "| console:", tx.iface.hvcontroller.get_version() if tx.hv_connected else "not connected")
    inventory = device_presets(tx)
    if args.list_presets:
        print("\n-- presets on the device --")
        print_inventory(inventory)
        return 0 if inventory else 1
    if not inventory:
        sys.exit("device reports no presets -- is it running an FDA_MODE image?")
    print("device: %d preset(s)" % len(inventory))

    presets, voltages = load_presets(args.presets) if args.presets else ([], {})
    if args.presets and not presets:
        sys.exit("no .json presets in %s" % args.presets)
    if presets:
        print("host: %d preset(s) in %s" % (len(presets), args.presets))

    if args.identify:
        if not presets:
            sys.exit("--identify needs --presets to compute the candidates from")
        print("\n-- identify: what is on the chips --")
        return identify(tx, presets)
    if args.load_preset is not None or args.run:
        return operate(tx, inventory, presets, voltages, args)
    if not presets:
        sys.exit("--presets is required to verify (or use --list-presets, --load-preset, --run)")
    return verify(tx, inventory, presets, args)


if __name__ == "__main__":
    sys.exit(main())
