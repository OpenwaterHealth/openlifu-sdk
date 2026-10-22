# SPDX-License-Identifier: Apache-2.0
"""Generate the FDA preset files for the transmitter and the console.

FDA mode runs one preset, built into each board's FDA image at the end of its
application slot -- transmitter pages 124-125, console page 61 -- so flashing
or updating the image writes it, the bootloader's signature covers it, and the
firmware only reads it. This writes the two headers those images are built
from, both named fda_preset.h:

  transmitter  Core/Inc/presets/fda_preset.h  the preset's blob: registers,
               timing, run lengths, thermal limits
  console      Core/Inc/presets/fda_preset.h  its HV entry: the voltage, the
               transmitter sensitivity that voltage suits, the .json's
               settings_crc

Add them to the firmware branches as needed; an FDA build refuses to
configure without its one.

Exactly one preset .json is taken: a file, or a folder holding just one. It
must carry voltage, sensitivity, start_C and shutoff_C; the generator has no
defaults. Nothing is sent anywhere and no device is needed.

    python examples/generate_presets.py --presets my_preset_settings.json

The firmware trees are looked for beside this repo (openlifu-transmitter-fw
and openlifu-console-fw); --out / --console-out name them if they are
elsewhere, as the checkout or its Core/Inc. presets/ is created there if it is
missing. A tree that is not on this machine gets its header in this repo
instead, under presets/transmitter-presets/ or presets/console-presets/, to be
copied into that firmware's Core/Inc/presets/.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from openlifu_sdk.io.LIFUTXPresets import (
    compile_preset,
    generate_console_fda_preset,
    generate_fda_preset,
    preset_files,
    preset_id,
    preset_sensitivity,
    regs_crc,
    train_count_selections,
)

# examples/ sits one level below the repo root.
SDK_ROOT = Path(__file__).resolve().parent.parent
# The firmware checkouts, where they sit beside this one.
TRANSMITTER_FW = SDK_ROOT.parent / "openlifu-transmitter-fw"
CONSOLE_FW = SDK_ROOT.parent / "openlifu-console-fw"
# Where the headers go for a firmware tree this machine does not have.
TRANSMITTER_FALLBACK = SDK_ROOT / "presets" / "transmitter-presets"
CONSOLE_FALLBACK = SDK_ROOT / "presets" / "console-presets"


def presets_dir(firmware: Path, fallback: Path) -> Path:
    """Where one firmware's preset header goes.

    firmware is its checkout or its include directory; the header belongs in
    presets/ under that include directory. Without it on this machine it goes
    to fallback instead.
    """
    firmware = firmware.resolve()
    # Given the presets/ folder itself, which may not exist yet.
    if firmware.name == "presets":
        firmware = firmware.parent
    if not firmware.is_dir():
        print("%s is not on this machine; writing to %s instead" % (firmware, fallback))
        return fallback
    for inc in (firmware / "Core" / "Inc", firmware / "Inc"):
        if inc.is_dir():
            return inc / "presets"
    return firmware / "presets"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--presets", required=True, type=Path,
                    help="the preset .json, or a folder holding just that one")
    ap.add_argument("--out", type=Path, default=TRANSMITTER_FW,
                    help="transmitter firmware checkout, or its Core/Inc; fda_preset.h goes in "
                         "presets/ there (default: openlifu-transmitter-fw beside this repo)")
    ap.add_argument("--console-out", type=Path, default=CONSOLE_FW,
                    help="console firmware checkout, or its Core/Inc; fda_preset.h goes in "
                         "presets/ there (default: openlifu-console-fw beside this repo)")
    ap.add_argument("--context", help="optional set name, noted in the C symbols' guard")
    args = ap.parse_args()

    files = preset_files(args.presets)
    if not files:
        sys.exit("no preset .json at %s" % args.presets)
    if len(files) != 1:
        sys.exit("FDA mode runs one preset; %s holds %d:\n  %s"
                 % (args.presets, len(files), "\n  ".join(str(f) for f in files)))

    path = files[0]
    raw = path.read_bytes()
    try:
        mc = compile_preset(json.loads(raw), preset_id=preset_id(path), settings_bytes=raw)
        preset_sensitivity(mc)   # the console's entry needs it
    except ValueError as e:
        sys.exit("%s cannot be used; nothing written: %s" % (path, e))
    print("  %s settings_crc=0x%08x regs_crc=0x%08x  %d chip(s), %d profile(s), trains %s, "
          "%g V at sensitivity %g, start %g C, shutoff %g C"
          % (mc["id"], mc["settings_crc"], regs_crc(mc),
             len(mc["chips"]), len(mc["chips"][0]["profiles"]), train_count_selections(mc),
             mc["voltage"], mc["sensitivity"], mc["start_C"], mc["shutoff_C"]))

    try:
        tx = generate_fda_preset(presets_dir(args.out, TRANSMITTER_FALLBACK), mc, args.context)
        con = generate_console_fda_preset(presets_dir(args.console_out, CONSOLE_FALLBACK), mc, args.context)
    except ValueError as e:
        sys.exit("%s cannot be used; nothing written: %s" % (path, e))
    print("wrote %s" % tx)
    print("wrote %s" % con)
    return 0


if __name__ == "__main__":
    sys.exit(main())
