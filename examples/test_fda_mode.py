"""Test an FDA_MODE transmitter and console against the preset .json they were built from.

An FDA_MODE transmitter has its presets baked into the image and programs the
chips itself; the host only picks one by index. An FDA_MODE console has each
preset's voltage baked in the same way and works out the HV itself: the host
selects the preset and gives it the connected transmitter's sensitivity. Only
input: the folder of preset .json files both images were generated from. HV
stays off unless --run turns it on.

    python examples/test_fda_mode.py --json-preset-directory <folder>
    python examples/test_fda_mode.py --json-preset-directory <folder> --duration 1
    python examples/test_fda_mode.py --console-only
    python examples/test_fda_mode.py --json-preset-directory <folder> --run   # start the sequence, for a scope

--run skips the checks below and runs the preset's sequence once, the way a
host would: it loads the preset with the run length --duration picks, selects
it on the console with this transmitter's sensitivity, turns HV on, starts
the trigger, and turns HV off when the sequence ends or on Ctrl+C. HV comes
on only if the console applies the preset in this run; if it refuses, or
there is no console, the trigger runs with HV off.

Against the transmitter this proves:

  1. The image holds exactly the folder's presets, in the same order.
  2. Each one is the .json's preset, loads on the host's CRC with the run
     length --duration picks, reads back from the chips register for
     register, and sets the trigger to its timing (test_presets.py's
     --verify-preset, on every preset).
  3. The gates hold: an index past the last, a short load, a load with the
     wrong CRC or a run length the preset lacks, and a host register write
     are all refused.
  4. The delay profiles cycle through the preset's execution order once the
     trigger runs.

Against the console, if one is connected (--console-only checks it alone):

  5. Its table holds the folder's presets in the same order, each with the
     .json's settings_crc, voltage and sensitivity (skipped without a folder).
  6. set_voltage and the raw DAC commands are refused, and so is HV on until
     a preset is selected.
  7. A select is refused with the wrong settings_crc, and when the
     transmitter sensitivity it is given puts the voltage outside the
     console's window; inside it, the console applies
     voltage x preset sensitivity / transmitter sensitivity.
  8. With a transmitter connected that has a sensitivity for the preset's
     frequency in its user config, a select with that value does the same.

Each preset's start_C / shutoff_C are part of what (2) compares; the gates
they drive need presets near room temperature, see test_thermal_limits.py.
"""

import argparse
import struct
import sys
import time
from pathlib import Path

from openlifu_sdk.io.exceptions import LIFUDeviceError, LIFUError
from openlifu_sdk.io.LIFUConfig import (
    OW_CONTROLLER,
    OW_HV_FDA_REFUSED,
    OW_HV_NO_PRESET,
    OW_HV_PRESET_CRC,
    OW_HV_RANGE,
    OW_PRESET_LOAD,
    SETTLE_TIME_HV_ON,
)
from openlifu_sdk.io.LIFUInterface import LIFUInterface
from openlifu_sdk.io.LIFUTXPresets import (
    device_sensitivity,
    load_preset,
    module_user_configs,
    regs_crc,
    train_count_selections,
    verify_console_preset,
)

from test_presets import Link, check, device_slots, host_presets, print_inventory, results, status, verify_slot

# How long the trigger runs while the active delay profile is sampled.
RASTER_SECONDS = 2.5
# How often --run asks whether the sequence is over; steady traffic makes the transmitter restart.
RUN_POLL_SECONDS = 1.0
# Why an FDA console refuses a select, for the codes that have a plain answer.
SELECT_REFUSALS = {
    OW_HV_RANGE: "that voltage is outside its window",
    OW_HV_PRESET_CRC: "its preset is not the .json's",
}


def refused(name, fn, code=None):
    """Check fn() is refused by the device, with *code* if one is given."""
    try:
        fn()
    except LIFUDeviceError as e:
        check(name, code is None or e.device_error_code == code, "-> " + str(e)[:110])
        return True
    check(name, False, "accepted")
    return False


def test_gates(tx, e, mc):
    """What an FDA transmitter must refuse, tried on preset *e*."""
    print("\n-- gates --")
    crc, counts = regs_crc(mc), train_count_selections(mc)
    refused("preset index %d, past the last, refused" % e["count"], lambda: tx.get_preset(e["count"]))
    refused("load with a short payload refused", lambda: tx.send_checked(
        packet_type=OW_CONTROLLER, command=OW_PRESET_LOAD, reserved=e["index"], data=struct.pack("<I", 1),
        op="bad_load"))
    refused("load with the wrong regs_crc refused", lambda: tx.load_preset(e["index"], crc ^ 0xDEADBEEF, 0))
    refused("load with a run length the preset lacks refused", lambda: tx.load_preset(e["index"], crc, len(counts)))
    refused("host register write refused", lambda: tx.write_register(0, 0x1B, 0))


def test_raster(tx, mc):
    """The loaded preset's delay profiles cycle in its execution order while the trigger runs."""
    print("\n-- trigger (HV off) --")
    order, seen = mc["execution_order"], []
    try:
        tx.start_trigger()
    except LIFUDeviceError as e:
        check("trigger starts", False, str(e)[:150])
        return
    try:
        t0 = time.time()
        while time.time() - t0 < RASTER_SECONDS:
            seen.append(tx.get_delay_profile(0))
            time.sleep(0.03)
    finally:
        tx.stop_trigger()
    check("delay profiles cycle while running", set(seen) == set(order),
          "saw %s, execution order %s" % (sorted(set(seen)), order))


def test_transmitter(tx, host, duration):
    held = [e for e in device_slots(tx) or [] if e["state"] == "preset"]
    if not held:
        sys.exit("device reports no presets -- is it running an FDA_MODE image?")
    if tx.get_preset_capacity() is not None:
        print("note: this transmitter keeps its presets in flash slots (PRESET_MODE); they are not baked in")

    print("\n-- presets in the image --")
    print_inventory(tx, held)
    host_ids, dev_ids = [m["id"] for m in host], [e["id"] for e in held]
    check("the image holds the folder's presets, in order", host_ids == dev_ids,
          "%d on both" % len(held) if host_ids == dev_ids else "folder %s / device %s" % (host_ids, dev_ids))

    print("\n-- each preset against its .json --")
    pairs = [(e, m) for e in held for m in host if m["id"] == e["id"]]
    for e, mc in pairs:
        verify_slot(tx, e, mc, duration)
    if not pairs:
        return
    # The last one verified is the one left loaded.
    test_gates(tx, *pairs[-1])
    try:
        tx.load_preset(pairs[-1][0]["index"], regs_crc(pairs[-1][1]), duration)
    except LIFUDeviceError as e:
        check("preset loads again after the refusals", False, str(e)[:150])
        return
    test_raster(tx, pairs[-1][1])


def select_check(hv, e, sensitivity, why):
    """A select of console preset *e* for a transmitter of *sensitivity*: applied
    as the rescaled voltage if that is inside the window, refused if not."""
    want = e["voltage"] * e["sensitivity"] / sensitivity
    name = "select for %s (sensitivity %g): %.2f V" % (why, sensitivity, want)
    if not e["min_voltage"] <= want <= e["max_voltage"]:
        refused(name + ", outside the window, refused (OW_HV_RANGE)",
                lambda: hv.select_preset(e["index"], e["settings_crc"], sensitivity), OW_HV_RANGE)
        return
    try:
        applied = hv.select_preset(e["index"], e["settings_crc"], sensitivity)
        now = hv.get_preset(e["index"])
        check(name + " applied", abs(applied - want) < 0.01 and now["selected"] == e["index"]
              and now["applied_voltage"] == applied, "the console set %.3f V" % applied)
    except LIFUDeviceError as err:
        check(name + " applied", False, str(err)[:150])


def test_console(hv, host=None, tx=None):
    """An FDA_MODE console applies only a selected preset's voltage, rescaled to
    the transmitter it is told about and inside its window. HV stays off."""
    print("\n-- console HV presets --")
    if hv is None:
        print("  [SKIP] console not connected")
        return
    if hv.get_hv_status():
        check("console HV off for the preset checks", False, "HV is on; not touching the setpoint")
        return
    first = hv.get_preset(0)
    if first is None:
        check("console is an FDA_MODE image", False, "it has no preset table: it takes whatever voltage the host sets")
        return
    table = [first] + [hv.get_preset(i) for i in range(1, first["count"])]
    low, high = first["min_voltage"], first["max_voltage"]
    check("console reports its presets and window", 0 < low <= high,
          "%d preset(s), %g to %g V" % (len(table), low, high))
    for e in table:
        print("  [%d] %-28s %.2f V at sensitivity %g, settings_crc 0x%08x"
              % (e["index"], e["id"], e["voltage"], e["sensitivity"], e["settings_crc"]))
    if host is not None:
        ids = [m["id"] for m in host]
        check("the console holds the folder's presets, in order", [e["id"] for e in table] == ids,
              "%d on both" % len(ids) if [e["id"] for e in table] == ids
              else "folder %s / console %s" % (ids, [e["id"] for e in table]))
        for e, mc in zip(table, host):
            try:
                verify_console_preset(hv, e["index"], mc)
                check("console [%d] %s is the .json's" % (e["index"], e["id"]), True)
            except ValueError as err:
                check("console [%d] %s is the .json's" % (e["index"], e["id"]), False, str(err))

    refused("set_voltage refused (OW_HV_FDA_REFUSED)", lambda: hv.set_voltage(low), OW_HV_FDA_REFUSED)
    refused("raw DAC write refused (OW_HV_FDA_REFUSED)", lambda: hv.set_dacs(100, 100, 0, 0), OW_HV_FDA_REFUSED)
    # Only tried while the console says nothing is selected, which is when it must refuse.
    if first["selected"] is None:
        if not refused("HV on with no preset selected refused (OW_HV_NO_PRESET)", hv.turn_hv_on, OW_HV_NO_PRESET):
            hv.turn_hv_off()
    else:
        print("  [SKIP] a preset is already selected; power-cycle the console to test the no-preset gate")

    e = table[0]
    refused("select with the wrong settings_crc refused (OW_HV_PRESET_CRC)",
            lambda: hv.select_preset(e["index"], e["settings_crc"] ^ 0xDEADBEEF, e["sensitivity"]), OW_HV_PRESET_CRC)
    refused("select with a sensitivity of 0 refused", lambda: hv.select_preset(e["index"], e["settings_crc"], 0.0))
    # Transmitters that would need a volt past each end of the window, then one in the middle of it.
    for volts in (low - 1.0, high + 1.0, (low + high) / 2):
        select_check(hv, e, e["voltage"] * e["sensitivity"] / volts, "a transmitter needing %g V" % volts)

    # Last, so the setpoint left behind is the connected transmitter's when it has one.
    if tx is None or host is None:
        return
    mc = next((m for m in host if m["settings_crc"] == e["settings_crc"]), None)
    sensitivity = None if mc is None else device_sensitivity(mc, module_user_configs(tx))
    if sensitivity is None:
        print("  [SKIP] this transmitter's user config has no sensitivity at the preset's frequency: "
              "it cannot be selected for until it is calibrated")
    else:
        select_check(hv, e, sensitivity, "this transmitter")


def console_voltage(tx, e, mc):
    """Select preset *e* on the console for this transmitter: (volts, None) once
    the console has applied it, else (None, why HV has to stay off)."""
    if not tx.hv_connected:
        return None, "no console connected"
    hv = tx.iface.hvcontroller
    sensitivity = device_sensitivity(mc, module_user_configs(tx))
    if sensitivity is None:
        return None, "this transmitter's user config has no sensitivity at the preset's frequency"
    try:
        entry = hv.get_preset(e["index"])
        if entry is None:
            return None, "the console is not an FDA_MODE image"
        print("  console [%d] %s: %.2f V at sensitivity %g; this transmitter's is %g, so %.2f V (window %g to %g V)"
              % (entry["index"], entry["id"], entry["voltage"], entry["sensitivity"], sensitivity,
                 entry["voltage"] * entry["sensitivity"] / sensitivity, entry["min_voltage"], entry["max_voltage"]))
        return hv.select_preset(e["index"], mc["settings_crc"], sensitivity), None
    except LIFUDeviceError as err:
        return None, "the console refused the select: %s" % SELECT_REFUSALS.get(err.device_error_code, str(err)[:110])


def run_sequence(tx, host, duration):
    """--run: load the image's preset and run its sequence once, to look at on a
    scope. HV comes on only if the console applies the preset for this
    transmitter in this run; otherwise the trigger runs with HV off."""
    held = [e for e in device_slots(tx) or [] if e["state"] == "preset"]
    pair = next(((e, m) for e in held for m in host if m["id"] == e["id"]), None)
    if pair is None:
        sys.exit("nothing to run: the image holds %s, the folder %s"
                 % ([e["id"] for e in held], [m["id"] for m in host]))
    e, mc = pair
    counts = train_count_selections(mc)
    if not 0 <= duration < len(counts):
        sys.exit("--duration %d is not one of this preset's run lengths: it offers %s" % (duration, counts))
    hv = tx.iface.hvcontroller if tx.hv_connected else None
    if hv is not None and hv.get_hv_status():
        sys.exit("console HV is already on; turn it off before --run")

    print("\n-- run [%d] %s --" % (e["index"], e["id"]))
    try:
        load_preset(tx, e["index"], mc, duration)
    except LIFUDeviceError as err:
        check("the preset loads on the host's CRC", False, str(err)[:150])
        return
    check("the preset loads on the host's CRC", True, "run length %d = %d train(s)" % (duration, counts[duration]))
    cfg = tx.get_trigger_json()
    period = cfg["TriggerPulseTrainInterval"] * 1e-6 or cfg["TriggerPulseCount"] / cfg["TriggerFrequencyHz"]
    total = cfg["TriggerPulseTrainCount"] * period
    print("  trigger: %d Hz, %d pulse(s) per train, a train every %.4g s, %d train(s): %.4g s in all"
          % (cfg["TriggerFrequencyHz"], cfg["TriggerPulseCount"], period, cfg["TriggerPulseTrainCount"], total))

    volts, why = console_voltage(tx, e, mc)
    print("  HV: " + ("%.2f V, applied by the console" % volts if volts is not None
                      else "stays OFF, trigger only -- " + why), flush=True)
    ended, t0 = False, time.time()
    try:
        if volts is not None:
            hv.turn_hv_on()
            hv.wait_for_settle(timeout=SETTLE_TIME_HV_ON)
            print("  HV on: %.2f V measured" % hv.get_voltage(), flush=True)
        tx.start_trigger()
        t0 = time.time()
        print("  running; Ctrl+C stops it", flush=True)
        while tx.iface.is_running():
            time.sleep(RUN_POLL_SECONDS)
        ended = True
    except KeyboardInterrupt:
        print("  stopped by Ctrl+C")
    except LIFUError as err:
        check("the sequence ran", False, str(err)[:150])
    finally:
        ran = time.time() - t0
        # HV first, so it comes off even if the transmitter has stopped answering.
        try:
            if volts is not None:
                hv.turn_hv_off()
        finally:
            tx.steady(lambda: tx.stop_trigger())
    if ended:
        # The last train may end up to a period early; anything shorter was cut off (a thermal trip, a restart).
        check("the sequence ran its full length", ran >= total - period - RUN_POLL_SECONDS,
              "about %.0f s; the trigger gives %.4g s" % (ran, total))
    if volts is not None:
        check("console HV is off again", not tx.iface.hvcontroller.get_hv_status())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json-preset-directory", type=Path, metavar="DIR",
                    help="folder of preset .json files the transmitter image was generated from")
    ap.add_argument("--duration", type=int, default=0, metavar="IDX",
                    help="which of each preset's pulse_train_count_selections to load it with (default 0, the first)")
    ap.add_argument("--console-only", action="store_true",
                    help="check just the console; no transmitter needed, and the .json folder is optional")
    ap.add_argument("--run", action="store_true",
                    help="skip the checks: load the preset and run its sequence once, HV on if the console "
                         "applies the preset for this transmitter (Ctrl+C stops it)")
    args = ap.parse_args()
    if args.run and args.console_only:
        ap.error("--run needs the transmitter; it cannot go with --console-only")

    host = None if args.json_preset_directory is None else host_presets(args.json_preset_directory)
    if host is not None:
        print("host: %d preset(s) in %s" % (len(host), args.json_preset_directory))
    reconnects = 0
    if args.console_only:
        with LIFUInterface(run_async=False) as iface:
            if not iface.is_device_connected()[1]:
                sys.exit("no console connected")
            print("console:", iface.hvcontroller.get_version())
            test_console(iface.hvcontroller, host)
    else:
        if host is None:
            sys.exit("--json-preset-directory is required (or --console-only)")
        tx = Link()
        print(status(tx), "| console:", tx.iface.hvcontroller.get_version() if tx.hv_connected else "not connected")
        if args.run:
            run_sequence(tx, host, args.duration)
        else:
            test_transmitter(tx, host, args.duration)
            test_console(tx.iface.hvcontroller if tx.hv_connected else None, host, tx)
        reconnects = tx.reconnects

    n_ok = sum(results)
    print("\nFDA MODE %s: %d/%d checks passed (%d transport reconnect(s))"
          % ("RUN" if args.run else "TEST", n_ok, len(results), reconnects))
    return 0 if results and n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
