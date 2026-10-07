"""Generate the preset headers the transmitter and console FDA_MODE images are built from.

Reads every preset .json in a directory, resolves each into TX7332 registers
with the SDK's own register math, and writes one C header per preset plus
the preset_table.h that indexes them into the transmitter firmware tree at
Core/Inc/presets/. Build one of its FDA presets from there.

It also writes the console's preset_hv_table.h: each preset's voltage, indexed
exactly as the transmitter's presets are. An FDA_MODE console applies preset
N's voltage when the host selects N and refuses any voltage the host sends
itself. That goes into the console firmware tree at Core/Inc/presets/; build
its FDA preset too.

Every preset must carry voltage, start_C and shutoff_C; the generator has no
defaults. Presets missing any are listed together and nothing is written.

The registers come from the SDK's own register math (the same code
set_solution programs a live device with); nothing is sent anywhere, so this
takes milliseconds and needs no transmitter.

    python examples/generate_presets.py --presets sample_json

The firmware trees are looked for beside this repo (openlifu-transmitter-fw
and openlifu-console-fw); --out / --console-out name them if they are
elsewhere, as the checkout or its Core/Inc. presets/ is created there if it is
missing. A firmware tree that is not on this machine gets its headers in this
repo instead, under presets/transmitter-presets/ or presets/console-presets/,
to be copied into that firmware's Core/Inc/presets/.
"""

import argparse
import json
import sys
from pathlib import Path

from openlifu_sdk.io.LIFUTXPresets import (
    compile_preset,
    generate_console_presets,
    generate_preset_set,
    preset_files,
    preset_id,
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
    """Where one firmware's preset headers go.

    firmware is its checkout or its include directory; the headers belong in
    presets/ under that include directory. Without it on this machine they go
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
    ap.add_argument("--presets", required=True, type=Path, help="directory of preset .json files")
    ap.add_argument("--out", type=Path, default=TRANSMITTER_FW,
                    help="transmitter firmware checkout, or its Core/Inc; the headers go in "
                         "presets/ there (default: openlifu-transmitter-fw beside this repo)")
    ap.add_argument("--console-out", type=Path, default=CONSOLE_FW,
                    help="console firmware checkout, or its Core/Inc; preset_hv_table.h goes in "
                         "presets/ there (default: openlifu-console-fw beside this repo)")
    ap.add_argument("--context", help="optional set name prefixed onto the C symbols and header names")
    args = ap.parse_args()

    files = preset_files(args.presets)
    if not files:
        sys.exit("no .json presets in %s" % args.presets)
    context = args.context

    configs, errors = [], []
    for index, path in enumerate(files):
        raw = path.read_bytes()
        try:
            mc = compile_preset(json.loads(raw), preset_id=preset_id(path), settings_bytes=raw)
        except ValueError as e:
            errors.append("  [%2d] %s" % (index, e))
            continue
        configs.append(mc)
        print("  [%2d] %-28s settings_crc=0x%08x regs_crc=0x%08x  %d chip(s), %d profile(s), trains %s, "
              "%g V, start %g C, shutoff %g C"
              % (index, mc["id"], mc["settings_crc"], regs_crc(mc),
                 len(mc["chips"]), len(mc["chips"][0]["profiles"]), train_count_selections(mc),
                 mc["voltage"], mc["start_C"], mc["shutoff_C"]))
    # A partial set would renumber the presets after a bad one, so write none.
    if errors:
        sys.exit("%d of %d preset(s) cannot be baked; nothing written:\n%s"
                 % (len(errors), len(files), "\n".join(errors)))

    out = presets_dir(args.out, TRANSMITTER_FALLBACK)
    written = generate_preset_set(out, configs, context)
    print("wrote %d transmitter header(s) to %s" % (len(written), out))
    path = generate_console_presets(presets_dir(args.console_out, CONSOLE_FALLBACK), configs, context)
    volts = sorted({mc["voltage"] for mc in configs})
    print("wrote %s: %d preset voltage(s), %s"
          % (path, len(configs), "all %g V" % volts[0] if len(volts) == 1
             else "%g..%g V" % (volts[0], volts[-1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
