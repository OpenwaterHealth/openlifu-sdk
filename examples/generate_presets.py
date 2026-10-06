"""Generate the preset headers the transmitter and console FDA_MODE images are built from.

Reads every preset .json in a directory, resolves each into TX7332 registers
with the SDK's own register math, and writes one C header per preset plus
the preset_table.h that indexes them. Copy the output into the transmitter
firmware tree at Core/Inc/presets/ and build one of its FDA presets.

It also writes the console's preset_hv_table.h: each preset's voltage, indexed
exactly as the transmitter's presets are. An FDA_MODE console applies preset
N's voltage when the host selects N and refuses any voltage the host sends
itself. Copy that into the console firmware tree at Core/Inc/presets/ and
build its FDA preset too.

Every preset must carry voltage, start_C and shutoff_C; the generator has no
defaults. Presets missing any are listed together and nothing is written.

The registers come from the SDK's own register math (the same code
set_solution programs a live device with); nothing is sent anywhere, so this
takes milliseconds and needs no transmitter.

    python examples/generate_presets.py --presets sample_json

Output goes to generated_presets/transmitter/ and generated_presets/console/
at the top of this repo unless --out / --console-out are given; each holds
what belongs in that firmware's Core/Inc/presets/.
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--presets", required=True, type=Path, help="directory of preset .json files")
    ap.add_argument("--out", type=Path,
                    help="directory to write the transmitter headers into "
                         "(default: generated_presets/transmitter/ in this repo)")
    ap.add_argument("--console-out", type=Path,
                    help="directory to write the console's preset_hv_table.h into "
                         "(default: generated_presets/console/ in this repo)")
    ap.add_argument("--context", help="optional set name prefixed onto the C symbols and header names")
    args = ap.parse_args()

    files = preset_files(args.presets)
    if not files:
        sys.exit("no .json presets in %s" % args.presets)
    context = args.context
    out = args.out or SDK_ROOT / "generated_presets" / "transmitter"
    console_out = args.console_out or SDK_ROOT / "generated_presets" / "console"

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

    written = generate_preset_set(out, configs, context)
    print("wrote %d transmitter header(s) to %s" % (len(written), out))
    path = generate_console_presets(console_out, configs, context)
    volts = sorted({mc["voltage"] for mc in configs})
    print("wrote %s: %d preset voltage(s), %s"
          % (path, len(configs), "all %g V" % volts[0] if len(volts) == 1
             else "%g..%g V" % (volts[0], volts[-1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
