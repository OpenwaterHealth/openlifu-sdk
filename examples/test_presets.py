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
  3. A CRC-gated load puts exactly the host-computed registers on the silicon,
     and the delay profiles cycle through the preset's execution order once the
     trigger runs.

    python examples/test_presets.py --presets unit-test/device_preset_outputs
"""

import argparse
import json
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
        self.tx = None
        self.reconnects = 0
        self.open()

    def open(self):
        iface = LIFUInterface(run_async=False, voltage_table_selection="dvt")
        if not iface.is_device_connected()[0]:
            sys.exit("no transmitter connected")
        self.tx = iface.txdevice

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
    """(id, machine_config) per preset, in the order the image indexes them."""
    out = []
    for p in preset_files(src):
        raw = p.read_bytes()
        pid = preset_id(p)
        out.append((pid, capture_machine_config(None, json.loads(raw), preset_id=pid, settings_bytes=raw)))
    return out


def expected_silicon(mc, chip):
    """What presets_load() leaves on one chip: base registers with the starting profile on top."""
    expect = {}
    for start, values in mc["chips"][chip]["registers"].items():
        for i, v in enumerate(values):
            expect[start + i] = v
    expect.update(mc["chips"][chip]["profiles"][mc["profile_index"] - 1])
    return expect


def readback_vs_host(tx, presets, i):
    pid, mc = presets[i]
    bad = total = 0
    for chip in range(len(mc["chips"])):
        for addr, want in expected_silicon(mc, chip).items():
            got = tx.read(chip, addr)
            m = READBACK_MASK.get(addr, 0)
            total += 1
            if (got & ~m) != (want & ~m):
                bad += 1
                if bad <= 5:
                    print("      mismatch chip%d 0x%04x host=0x%08x read=0x%08x" % (chip, addr, want, got))
    check("silicon == host-computed registers after load_preset(%d)" % i, bad == 0, "%d registers" % total)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--presets", required=True, type=Path, help="directory of preset .json files")
    ap.add_argument("--load", type=int, nargs=2, default=(0, 5), metavar="IDX",
                    help="two preset indices to load and read back (default 0 5)")
    ap.add_argument("--raster-seconds", type=float, default=2.5, help="how long to run the trigger while sampling")
    args = ap.parse_args()

    presets = load_presets(args.presets)
    if not presets:
        sys.exit("no .json presets in %s" % args.presets)

    tx = Link()
    print("firmware:", tx.get_version(), "| chips:", tx.enum_tx7332_devices(), "| presets:", len(presets))

    print("\n-- readback: id / settings_crc / regs_crc --")
    for i, (pid, mc) in enumerate(presets):
        got = tx.get_preset(i)
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
    expect_error("get_preset(%d) out of range" % len(presets), lambda: tx.get_preset(len(presets)))
    expect_error("load with 4-byte payload rejected", lambda: tx.send_checked(
        packet_type=OW_CONTROLLER, command=OW_PRESET_LOAD, reserved=0, data=struct.pack("<I", 1), op="bad_load"))
    expect_error("load with wrong regs_crc refused (OW_BAD_CRC)",
                 lambda: tx.load_preset(0, regs_crc(presets[0][1]) ^ 0xDEADBEEF, 1), "0xfd")
    expect_error("host register write refused in FDA mode", lambda: tx.write_register(0, 0x1B, 0))

    print("\n-- load + raster --")
    first, second = args.load
    tx.load_preset(first, regs_crc(presets[first][1]), train_count=50)
    check("load_preset(%d, correct crc)" % first, True)
    readback_vs_host(tx, presets, first)

    order = presets[first][1]["execution_order"]
    tx.start_trigger()
    t0 = time.time()
    seen = []
    while time.time() - t0 < args.raster_seconds:
        seen.append(tx.get_delay_profile(0))
        time.sleep(0.03)
    tx.stop_trigger()
    print("   active delay profile samples:", seen)
    check("profiles cycle while running", set(seen) == set(order),
          "saw %s, execution order %s" % (sorted(set(seen)), order))

    tx.load_preset(second, regs_crc(presets[second][1]), train_count=1)
    check("load_preset(%d, correct crc)" % second, True)
    readback_vs_host(tx, presets, second)

    n_ok = sum(results)
    print("\nFDA PRESET DEVICE TEST: %d/%d checks passed (%d transport reconnect(s))"
          % (n_ok, len(results), tx.reconnects))
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
