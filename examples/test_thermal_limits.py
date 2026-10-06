"""On-device test of the FDA thermal limits: start_C gates a trigger start, shutoff_C ends a run.

Needs a transmitter running an FDA_MODE image built from --presets, and
presets whose start_C sits near the bench temperature -- operating limits
such as 35 / 75 C are nowhere near it. make-ladder writes such a set from one
real preset: byte-identical source plus copies whose only change is start_C
stepping across the bench range and shutoff_C a fixed band above it.

    python examples/test_thermal_limits.py make-ladder --source <preset .json> --out thermal_test
    python examples/generate_presets.py --presets thermal_test   # then build + flash the TX
    python examples/test_thermal_limits.py run --presets thermal_test

run proves, picking presets by the temperature the TX reports right then:
  1. With nothing loaded since power-up, a start is refused (OW_NO_PRESET).
  2. A preset whose start_C is below the TX temperature refuses to start
     (OW_TEMP_TOO_HIGH) and nothing runs.
  3. A preset whose start_C is above it starts, runs until the TX warms to
     its shutoff_C, and stops itself long before its selected run length.
  4. Once stopped hot, the same preset refuses to restart until it cools
     back to start_C.

HV must be off: pulsing the TX7332s alone warms the bench board roughly
1 C/min, enough to reach a shutoff_C one degree above the start.
"""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from openlifu_sdk.io.exceptions import LIFUDeviceError
from openlifu_sdk.io.LIFUConfig import OW_NO_PRESET, OW_TEMP_TOO_HIGH
from openlifu_sdk.io.LIFUInterface import LIFUInterface
from openlifu_sdk.io.LIFUTXPresets import compile_preset, preset_files, preset_id, regs_crc, verify_preset

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


def make_ladder(args):
    out = args.out
    if out.exists():
        sys.exit("%s exists; pick a new --out" % out)
    out.mkdir(parents=True)
    shutil.copy(args.source, out / args.source.name)
    doc = json.loads(args.source.read_bytes())
    n = int(round((args.to - args.start) / args.step)) + 1
    for i in range(n):
        start = round(args.start + i * args.step, 1)
        d = dict(doc, start_C=start, shutoff_C=round(start + args.band, 1),
                 label="thermal ladder start %.1f C" % start)
        (out / ("ladder_%02d_settings.json" % i)).write_text(json.dumps(d))
    print("wrote %s plus %d ladder preset(s) to %s: start_C %.1f..%.1f C, shutoff_C start + %.1f C"
          % (args.source.name, n, out, args.start, args.start + (n - 1) * args.step, args.band))
    return 0


def refused(fn):
    """The device error code fn() was refused with, or None if it was accepted."""
    try:
        fn()
    except LIFUDeviceError as e:
        return e.device_error_code, str(e)
    return None, ""


def running(tx):
    return tx.get_trigger_json().get("TriggerStatus", "").upper() == "RUNNING"


def run(args):
    # Closing releases the SDK's cross-process hardware lock for the next run.
    with LIFUInterface(run_async=False) as iface:
        return run_on(iface, args)


def run_on(iface, args):
    tx_ok, hv_ok = iface.is_device_connected()
    if not tx_ok:
        sys.exit("no transmitter connected")
    if hv_ok and iface.hvcontroller.get_hv_status():
        sys.exit("HV is on; this test pulses the TX and must run with HV off")
    tx = iface.txdevice
    print("firmware:", tx.get_version())

    presets = []
    for i, p in enumerate(preset_files(args.presets)):
        raw = p.read_bytes()
        mc = compile_preset(json.loads(raw), preset_id=preset_id(p), settings_bytes=raw)
        verify_preset(tx, i, mc)   # same image, same limits, or nothing below means anything
        presets.append((i, mc))
    print("host and device agree on %d preset(s)" % len(presets))

    print("\n-- nothing loaded --")
    code, msg = refused(tx.start_trigger)
    if code is None:
        tx.stop_trigger()
    if code == OW_NO_PRESET:
        check("start with no preset loaded refused (OW_NO_PRESET)", True, "-> " + msg)
    else:
        # Started, or refused on temperature: either way something is loaded.
        print("  [SKIP] a preset is already loaded; power-cycle the TX to test the no-preset gate")

    t = tx.get_temperature()
    print("\n-- TX at %.2f C --" % t)

    below = [(i, mc) for i, mc in presets if mc["start_C"] <= t - args.margin]
    if not below:
        print("  [SKIP] no preset with start_C below %.2f C" % (t - args.margin))
    else:
        i, mc = max(below, key=lambda e: e[1]["start_C"])
        tx.load_preset(i, regs_crc(mc), 0)
        code, msg = refused(tx.start_trigger)
        check("[%d] %s (start_C %g) refuses at %.2f C" % (i, mc["id"], mc["start_C"], t),
              code == OW_TEMP_TOO_HIGH and not running(tx), "-> " + msg)

    above = [(i, mc) for i, mc in presets if mc["start_C"] >= t + args.margin]
    if not above:
        print("  [SKIP] no preset with start_C above %.2f C" % (t + args.margin))
        return finish()
    i, mc = min(above, key=lambda e: (e[1]["start_C"], e[1]["shutoff_C"]))
    longest = len(mc["pulse_train_count_selections"]) - 1
    tx.load_preset(i, regs_crc(mc), longest)
    code, msg = refused(tx.start_trigger)
    check("[%d] %s (start_C %g, shutoff_C %g) starts at %.2f C"
          % (i, mc["id"], mc["start_C"], mc["shutoff_C"], t), code is None, msg)
    if code is not None:
        return finish()

    # Run length: trains of pulse_count pulses at pulse_interval, back to back.
    planned = mc["pulse_train_count_selections"][longest] * mc["pulse_count"] * mc["pulse_interval_ms"] / 1000.0
    t0, trace, stopped = time.time(), [], False
    try:
        while time.time() - t0 < args.timeout:
            temp = tx.get_temperature()
            trace.append((round(time.time() - t0, 1), temp))
            if not running(tx):
                stopped = True
                break
            time.sleep(1.0)
    finally:
        if not stopped:
            tx.stop_trigger()
    elapsed = time.time() - t0
    # The firmware trips on its own once-a-second reading, which can land
    # between the poll's temperature read and its status read: read it again.
    last = tx.get_temperature()
    trace.append((round(time.time() - t0, 1), last))
    print("   temperature trace (s, C):", trace[::max(1, len(trace) // 20)] + trace[-1:])
    check("run stopped itself at shutoff_C", stopped and last >= mc["shutoff_C"],
          "after %.0f s of a %.0f s run, TX %.2f C, shutoff_C %g" % (elapsed, planned, last, mc["shutoff_C"]))

    code, msg = refused(tx.start_trigger)
    if code is None:
        tx.stop_trigger()
    check("restart while above start_C refused (OW_TEMP_TOO_HIGH)", code == OW_TEMP_TOO_HIGH, "-> " + msg)
    return finish()


def finish():
    n_ok = sum(results)
    print("\nTHERMAL LIMIT TEST: %d/%d checks passed" % (n_ok, len(results)))
    return 0 if results and n_ok == len(results) else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    mk = sub.add_parser("make-ladder", help="write a bench preset set with start_C near room temperature")
    mk.add_argument("--source", required=True, type=Path, help="real preset .json to copy")
    mk.add_argument("--out", required=True, type=Path, help="new directory for the set")
    mk.add_argument("--start", type=float, default=28.0, help="lowest start_C, C (default 28)")
    mk.add_argument("--to", type=float, default=34.0, help="highest start_C, C (default 34)")
    mk.add_argument("--step", type=float, default=0.5, help="start_C step, C (default 0.5)")
    mk.add_argument("--band", type=float, default=1.0, help="shutoff_C - start_C, C (default 1)")
    rn = sub.add_parser("run", help="test the gates on a connected FDA transmitter")
    rn.add_argument("--presets", required=True, type=Path, help="the preset directory the image was built from")
    rn.add_argument("--margin", type=float, default=0.3,
                    help="how far start_C must sit from the TX temperature to be picked, C (default 0.3)")
    rn.add_argument("--timeout", type=float, default=600.0, help="longest to wait for the shutoff, s (default 600)")
    args = ap.parse_args()
    return make_ladder(args) if args.cmd == "make-ladder" else run(args)


if __name__ == "__main__":
    sys.exit(main())
