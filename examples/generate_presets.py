"""Generate the preset headers a transmitter FDA_MODE image is built from.

Reads every preset .json in a directory, resolves each into TX7332 registers
with the SDK's own register math, and writes one C header per preset plus
the preset_table.h that indexes them. Copy the output into the firmware tree
at Core/Inc/presets/<set>/ and build with -DFDA_MODE=<set>.

The registers come from the SDK's own register math (the same code
set_solution programs a live device with); nothing is sent anywhere, so this
takes milliseconds and needs no transmitter.

    python examples/generate_presets.py --presets presets/vet

Output goes to generated_presets/<set>/ at the top of this repo unless --out
is given; the set name, baked into the C symbols, defaults to the name of the
--presets directory. Copy generated_presets/<set>/ into the firmware tree as
Core/Inc/presets/<set>/.
"""

import argparse
import json
import sys
from pathlib import Path

from openlifu_sdk.io.LIFUTXPresets import (
    capture_machine_config,
    generate_preset_set,
    preset_files,
    preset_id,
    regs_crc,
)

# examples/ sits one level below the repo root.
SDK_ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--presets", required=True, type=Path, help="directory of preset .json files")
    ap.add_argument("--out", type=Path,
                    help="directory to write the headers into (default: generated_presets/<set>/ in this repo)")
    ap.add_argument("--context", help="preset set name for the C symbols (default: the --presets directory name)")
    args = ap.parse_args()

    files = preset_files(args.presets)
    if not files:
        sys.exit("no .json presets in %s" % args.presets)
    context = args.context or args.presets.resolve().name
    out = args.out or SDK_ROOT / "generated_presets" / context

    configs = []
    for index, path in enumerate(files):
        raw = path.read_bytes()
        mc = capture_machine_config(None, json.loads(raw), preset_id=preset_id(path), settings_bytes=raw)
        configs.append(mc)
        print("  [%2d] %-28s settings_crc=0x%08x regs_crc=0x%08x  %d chip(s), %d profile(s)"
              % (index, mc["id"], mc["settings_crc"], regs_crc(mc),
                 len(mc["chips"]), len(mc["chips"][0]["profiles"])))

    written = generate_preset_set(out, configs, context)
    print("wrote %d header(s) for set '%s' to %s" % (len(written), context, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
