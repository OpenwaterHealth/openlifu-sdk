# SPDX-License-Identifier: Apache-2.0
"""Build (optionally) and flash the transmitter or console firmware over ST-LINK.

    python examples/flash.py                              # the one board wired to a probe
    python examples/flash.py --board console --build      # bump the build number, rebuild, then flash
    python examples/flash.py --board transmitter --preset Release-FDA-BareMetal --build
    python examples/flash.py --build --no-increment       # rebuild without bumping the counter
    python examples/flash.py --full-erase                 # erase the whole chip, then flash
    python examples/flash.py --image lifu-transmitter-fw.bin   # flash a downloaded file as-is
    python examples/flash.py --list-probes                # show the probes and their boards, flash nothing
    python examples/flash.py --board transmitter --preset Release-FDA-BareMetal --build --presets path/to/fda
    python examples/flash.py --board console --preset Release-FDA-BareMetal --build --presets path/to/test

The board is found from the chip each probe is wired to: an STM32L43x/L44x
(device ID 0x435) is the transmitter, an STM32F07x (0x448) the console. With
both wired up, name one with --board, or the probe with --probe-sn. The
firmware trees are looked for beside this repo, and --transmitter-fw /
--console-fw name them if they are elsewhere.

There are two kinds of image (operator-interface #192, Peter and Alex,
2026-10-29). A plain image (a preset without FDA_MODE) has every command and
no presets; each --build of one bumps a counter in that tree's
build/.localbuild and bakes it into FW_VERSION as "+bN", so the version the
device reports back confirms the new image took. A presets-only image (an
FDA_MODE preset) links the presets of one preset context and reports that
context as its build suffix, "+fda" or "+test"; it is never stamped, because
the host matches its version exactly.

--presets DIR bakes a preset context into a presets-only image: DIR is a
preset context folder (presets.json listing the presets, each in
<id>/<id>_settings.json), and its folder name is the image's suffix
(-DFW_BUILD_VARIANT). Each preset is compiled as the operator interface
compiles it, so the CRCs in the image are the ones the host verifies. This is
how presets reach a device: the operator interface never writes them. One
preset is linked as fda_preset.h; several as the transmitter's preset table
and the console's HV table, both in Core/Inc/presets/, which the firmware
links once transmitter-fw#50 and console-fw#21 are in. A plain image takes no
presets. The images this builds are unsigned; signed images come from the
diathermy repo's firmware/build_firmware.py.

The chip is programmed under reset, so its firmware and watchdog are not
running while the download is. The console's watchdog, about five seconds,
would otherwise restart it mid-download, and its probes need the slow 50 kHz
default clock. Only the pages the image covers are erased, so stored presets
and the config page stay as they were. --full-erase erases the whole chip
first, bootloader and config page included.

--image flashes a file instead of a build. A .hex or .elf goes where its own
addresses say. A .bin goes by what it holds: a signed image (it starts with the
bootloader's SFU1 header) at the slot, 0x08010000, an unsigned slot build at
0x08010400, anything else at 0x08000000, including a full production image
with the bootloader. --address overrides that. A .hex or .bin must also suit
the board on the probe, judged by the stack pointer its vector table starts
with.

A preset that is not bare metal links the image for the secure bootloader's
slot, which only boots it once it is signed, and the keys are not on a bench
PC. The script asks before flashing one unsigned, and before flashing a signed
image, which needs the bootloader already on the chip.
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path



@dataclass(frozen=True)
class Board:
    device_id: str
    chip: str
    ram_top: int           # end of RAM, the stack pointer its builds start with
    tree: str              # firmware checkout, beside this repo by default
    default_preset: str
    freq_khz: int | None   # None leaves the SWD clock to the probe
    after_flash: str | None


BOARDS = {
    "transmitter": Board("0x435", "STM32L43x/L44x", 0x2000C000, "openlifu-transmitter-fw", "Debug-BareMetal", None,
                         "If the transmitter does not show up on USB, cycle its 12 V: after an ST-LINK "
                         "flash it can start in the chip's own loader instead of the firmware."),
    "console": Board("0x448", "STM32F07x", 0x20004000, "openlifu-console-fw", "Release-BareMetal", 50, None),
}

FLASH_BASE = 0x08000000
SLOT_BASE = 0x08010000     # both bootloaders' application slot
SLOT_HEADER = 0x400        # the signed header ahead of the image in the slot
RAM_BASE = 0x20000000
KINDS = {
    "full": "bootloader and signed application",
    "signed": "signed, for the bootloader's slot",
    "slot": "unsigned, for the bootloader's slot",
    "bare metal": "bare metal",
}
# Enough to reach the signed header of a full image's application.
HEAD = SLOT_BASE - FLASH_BASE + SLOT_HEADER

WORKSPACE = Path(__file__).resolve().parent.parent.parent
CUBE_PLUGINS = r"C:\ST\STM32CubeIDE_*\STM32CubeIDE\plugins"
VERSION_LINE = re.compile(rb'(?m)^(#define FW_VERSION[ \t]+")([^"]*?)(\+b\d+)?(")')


def find_programmer() -> str:
    found = shutil.which("STM32_Programmer_CLI")
    if found:
        return found
    candidates = [Path(os.environ.get(var, "")) / r"STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe"
                  for var in ("ProgramFiles", "ProgramFiles(x86)")]
    # Fall back to the copy bundled with STM32CubeIDE.
    candidates += [Path(p) for p in glob.glob(CUBE_PLUGINS + r"\*cubeprogrammer*\tools\bin\STM32_Programmer_CLI.exe")]
    for exe in candidates:
        if exe.is_file():
            return str(exe)
    sys.exit("STM32_Programmer_CLI.exe not found. Install STM32CubeProgrammer.")


def add_toolchain_to_path() -> None:
    # arm-none-eabi-gcc and ninja ship inside CubeIDE under versioned folder names.
    for pattern in ("*externaltools.gnu-tools-for-stm32*", "*externaltools.ninja*"):
        for tools in glob.glob(CUBE_PLUGINS + "\\" + pattern + r"\tools\bin"):
            if tools not in os.environ["PATH"]:
                os.environ["PATH"] = tools + os.pathsep + os.environ["PATH"]
    for tool in ("arm-none-eabi-gcc", "ninja", "cmake"):
        if not shutil.which(tool):
            sys.exit("%s not found on PATH or in an STM32CubeIDE install." % tool)


def cmake_presets(repo: Path) -> dict:
    """Each buildable configure preset's cache variables, with what it
    inherits resolved (its own values win, then its parents' in order)."""
    doc = json.loads((repo / "CMakePresets.json").read_text(encoding="utf-8"))
    configure = {c["name"]: c for c in doc["configurePresets"]}

    def variables(name: str) -> dict:
        preset = configure[name]
        parents = preset.get("inherits", [])
        merged = {}
        for parent in reversed([parents] if isinstance(parents, str) else parents):
            merged.update(variables(parent))
        merged.update(preset.get("cacheVariables", {}))
        return merged

    names = {b["name"] for b in doc.get("buildPresets", [])}
    return {name: variables(name) for name in configure if name in names}


CMAKE_TRUE = {"1", "ON", "YES", "Y", "TRUE"}
CMAKE_FALSE = {"", "0", "OFF", "NO", "N", "FALSE", "IGNORE", "NOTFOUND"}


def cache_flag(variables: dict, name: str) -> bool:
    """A boolean cache variable as CMake reads it. CMakePresets.json allows a
    bool, a string or {"type": ..., "value": ...}. A value CMake would not
    read as a plain true or false is refused rather than guessed at, since
    FDA_MODE decides what an image holds and how it is versioned."""
    value = variables.get(name, False)
    if isinstance(value, dict):
        value = value.get("value", False)
    if isinstance(value, bool):
        return value
    text = str(value).upper()
    if text in CMAKE_TRUE:
        return True
    if text in CMAKE_FALSE or text.endswith("-NOTFOUND"):
        return False
    sys.exit("cannot read %s=%r as true or false" % (name, value))


def step_build_number(repo: Path, increment: bool) -> int:
    # One counter for every preset of a tree. It lives under build/, so it is
    # gitignored and deleting build/ starts it again at 1.
    counter = repo / "build" / ".localbuild"
    try:
        n = int(counter.read_bytes().decode("utf-8-sig").strip())
    except (OSError, ValueError):
        n = 0
    n += increment
    counter.parent.mkdir(exist_ok=True)
    counter.write_text(str(n), encoding="utf-8")
    return n


def version_header(repo: Path, preset: str) -> Path:
    return repo / "build" / preset / "generated" / "version.h"


def stamp_build_number(repo: Path, preset: str, number: int | None) -> None:
    # CMake only regenerates version.h at configure time, so the build number
    # is written straight into the generated header, replacing any earlier one.
    # None removes one, so an FDA image reports exactly its version.
    header = version_header(repo, preset)
    if not header.is_file():
        sys.exit("version.h not generated at %s" % header)
    text = header.read_bytes()
    stamp = b"" if number is None else b"+b%d" % number
    patched, hits = VERSION_LINE.subn(lambda m: m.group(1) + m.group(2) + stamp + m.group(4), text)
    if not hits:
        sys.exit("could not find FW_VERSION in %s" % header)
    header.write_bytes(patched)


PRESET_DIR = Path("Core/Inc/presets")


def load_preset_set(context: Path) -> list:
    """The compiled presets a preset context folder lists, in its order: each
    settings file compiled from its exact bytes, with its id from the manifest,
    as the operator interface's loader does."""
    from openlifu_sdk.io.LIFUTXPresets import compile_preset

    try:
        manifest = json.loads((context / "presets.json").read_text(encoding="utf-8"))
        ids = manifest.get("presets") if isinstance(manifest, dict) else None
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
            sys.exit("%s: 'presets' must be a non-empty list of ids" % (context / "presets.json"))
        configs = []
        for preset_id in ids:
            raw = (context / preset_id / ("%s_settings.json" % preset_id)).read_bytes()
            configs.append(compile_preset(json.loads(raw), preset_id=preset_id, settings_bytes=raw))
    except (OSError, ValueError, KeyError, TypeError) as e:
        sys.exit("cannot load the preset set in %s: %s" % (context, e))
    return configs


def write_preset_headers(repo: Path, name: str, presets_only: bool, context: Path) -> list:
    """Generate the board's preset headers for the set in *context*, for a
    presets-only image; a plain image takes none. One preset is linked as
    fda_preset.h, several as the board's preset table. The headers are
    generated in a staging folder first, so a set that is refused or fails
    to generate leaves the tree's headers as they were. Once the swap
    starts, the old headers are gone."""
    from openlifu_sdk.io.LIFUTXPresets import (
        generate_console_fda_preset,
        generate_console_presets,
        generate_fda_preset,
        generate_preset_set,
    )

    if not presets_only:
        sys.exit("a plain image links no presets; --presets needs an FDA_MODE (presets-only) preset")
    listed = subprocess.run(["git", "-C", str(repo), "ls-files", PRESET_DIR.as_posix()],
                            capture_output=True, text=True)
    if listed.returncode != 0:
        sys.exit("cannot tell whether %s tracks its preset headers (git: %s)"
                 % (repo, listed.stderr.strip() or listed.returncode))
    if listed.stdout.split():
        sys.exit("%s tracks its own preset headers (%s); refusing to overwrite them"
                 % (repo, ", ".join(listed.stdout.split())))
    configs = load_preset_set(context)
    with tempfile.TemporaryDirectory() as staging:
        try:
            if len(configs) == 1:
                generate = generate_fda_preset if name == "transmitter" else generate_console_fda_preset
                generate(staging, configs[0], context.name)
            elif name == "transmitter":
                generate_preset_set(staging, configs, context.name)
            else:
                generate_console_presets(staging, configs, context.name)
        except ValueError as e:
            sys.exit("cannot generate the %s preset headers: %s" % (name, e))
        out = repo / PRESET_DIR
        out.mkdir(parents=True, exist_ok=True)
        for old in out.glob("*.h"):
            old.unlink()   # a header left from another set must not be linked in
        for new in Path(staging).glob("*.h"):
            shutil.copy2(new, out / new.name)
    print("Presets    : %s from %s (+%s, %d preset(s))" % (", ".join(mc["id"] for mc in configs),
                                                       context, context.name, len(configs)))
    if len(configs) > 1:
        print("             The image links several presets only with transmitter-fw#50 /\n"
              "             console-fw#21; an older tree's presets-only build needs fda_preset.h.")
    return configs


def build(repo: Path, preset: str, increment: bool, stamp: bool = True,
          variant: str | None = None) -> None:
    """Configure and build *preset*. A presets-only image is not stamped
    and reports *variant*, its preset context, as its build suffix."""
    add_toolchain_to_path()
    if stamp:
        number = step_build_number(repo, increment)
        print("Building   : %s (local build b%d)" % (preset, number))
    else:
        number = None
        print("Building   : %s (presets-only: no local build number, so it reports its exact version)"
              % preset)
    # Configure first, on its own: left to ninja, the regeneration would rewrite
    # version.h mid-build and drop the stamp. cmake reads presets from the cwd.
    suffix = ["-DFW_BUILD_VARIANT=%s" % variant] if variant else []
    subprocess.run(["cmake", "--preset", preset, *suffix], cwd=repo, check=True)
    stamp_build_number(repo, preset, number)
    subprocess.run(["cmake", "--build", "--preset", preset], cwd=repo, check=True)


def newest_hex(repo: Path, preset: str) -> Path:
    images = sorted((repo / "build" / preset).glob("*.hex"), key=lambda p: p.stat().st_mtime)
    if not images:
        sys.exit("No .hex in %s. Run with --build first." % (repo / "build" / preset))
    return images[-1]


def report_version(repo: Path, preset: str, image: Path) -> None:
    # Checked against the .bin, not the header, so a stale build cannot pass
    # for a fresh one.
    header = version_header(repo, preset)
    match = VERSION_LINE.search(header.read_bytes()) if header.is_file() else None
    if not match:
        return
    version = match.group(2) + (match.group(3) or b"")
    binary = image.with_suffix(".bin")
    stale = "" if binary.is_file() and version in binary.read_bytes() else "  (NOT found in the .bin - image may be stale)"
    print("Version    : %s%s" % (version.decode(), stale))


def probe_serials(cli: str) -> list:
    out = subprocess.run([cli, "-l", "stlink"], capture_output=True, text=True).stdout
    return re.findall(r"ST-LINK SN\s*:\s*(\S+)", out)


def target_id(cli: str, serial: str) -> str:
    """Device ID of the chip a probe is wired to, or "" if it cannot be read."""
    out = subprocess.run([cli, "-c", "port=SWD", "sn=" + serial, "mode=HOTPLUG"],
                         capture_output=True, text=True).stdout
    match = re.search(r"Device ID\s*:\s*(0x[0-9A-Fa-f]+)", out)
    return match.group(1).lower() if match else ""


def read_hex_head(path: Path, size: int = HEAD) -> tuple[int, bytes]:
    """The lowest address in an Intel HEX file, and the first *size* bytes from it."""
    upper, records = 0, []
    for line in path.read_text().splitlines():
        if not line.startswith(":"):
            continue
        rec = bytes.fromhex(line[1:].strip())
        kind, data = rec[3], rec[4:4 + rec[0]]
        if kind == 0:
            records.append((upper + int.from_bytes(rec[1:3], "big"), data))
        elif kind in (2, 4):   # extended segment / linear address
            upper = int.from_bytes(data, "big") << (4 if kind == 2 else 16)
    if not records:
        sys.exit("%s holds no data" % path)
    base = min(a for a, _ in records)
    head = bytearray(b"\xff" * size)
    for a, data in records:
        if a - base < size:
            head[a - base:a - base + len(data)] = data[:size - (a - base)]
    return base, bytes(head)


def inspect_image(path: Path, address: int | None) -> tuple[str, int | None, bytes]:
    """(kind, load address, first bytes) of an image: kind is "full", "signed", "slot",
    "bare metal", or "unchecked" for an .elf, whose addresses the programmer reads itself."""
    suffix = path.suffix.lower()
    if suffix == ".elf":
        return "unchecked", None, b""
    if suffix == ".hex":
        base, head = read_hex_head(path)
    elif suffix == ".bin":
        base, head = None, path.read_bytes()[:HEAD]
    else:
        sys.exit("%s is not a .hex, .bin or .elf" % path)
    if head[:4] == b"SFU1":
        kind = "signed"
    elif head[SLOT_BASE - FLASH_BASE:SLOT_BASE - FLASH_BASE + 4] == b"SFU1":
        kind = "full"
    else:
        # A slot build's reset handler sits past the signed header, a bare-metal one below the slot.
        kind = "slot" if int.from_bytes(head[4:8], "little") >= SLOT_BASE + SLOT_HEADER else "bare metal"
    if base is None:
        base = address if address is not None else {
            "full": FLASH_BASE, "signed": SLOT_BASE, "slot": SLOT_BASE + SLOT_HEADER, "bare metal": FLASH_BASE}[kind]
    return kind, base, head


def describe_image(path: Path, kind: str, address: int | None, head: bytes, name: str, board: Board) -> None:
    """Print what the image is, and stop if it is for the other board."""
    print("Image      : %s" % path)
    if kind == "unchecked":
        print("Kind       : not checked (an .elf)")
        return
    print("Kind       : %s, at 0x%08X" % (KINDS[kind], address))
    vectors = head[SLOT_HEADER:] if kind == "signed" else head
    sp = int.from_bytes(vectors[:4], "little")
    other = next((n for n, b in BOARDS.items() if n != name and b.ram_top == sp), None)
    if not RAM_BASE < sp <= board.ram_top or other:
        sys.exit("This image is not for the %s on this probe: it starts its stack at 0x%08X%s."
                 % (name, sp, ", as %s builds do" % other if other else ", outside its RAM"))
    header = {"signed": 0, "full": SLOT_BASE - FLASH_BASE}.get(kind)
    if header is not None:
        v = int.from_bytes(head[header + 6:header + 8], "little")
        print("Version    : %d.%d.%d (from the signed header)" % (v >> 11, (v >> 5) & 0x3F, v & 0x1F))


def confirm_full_erase(name: str) -> bool:
    print("\nWARNING: --full-erase wipes the whole %s flash: any bootloader, any stored\n"
          "         presets and the user config (calibration). Only the new image is\n"
          "         written back.\n" % name)
    if ask("Erase the whole chip?"):
        return True
    print("Aborted.")
    return False


def board_for(device_id: str) -> str | None:
    return next((name for name, b in BOARDS.items() if b.device_id == device_id), None)


def describe(targets: dict) -> str:
    return ", ".join("%s -> %s" % (sn, board_for(dev) or dev or "no target") for sn, dev in targets.items())


def pick_target(cli: str, wanted_sn: str, wanted_board: str | None) -> tuple[str, str]:
    """(probe serial, board) to flash: the probe asked for, else the one wired to the board asked for,
    else the only probe wired to either board."""
    serials = probe_serials(cli)
    if not serials:
        sys.exit("No ST-LINK probe found.")
    if wanted_sn:
        if wanted_sn not in serials:
            sys.exit("Probe %s is not attached (found: %s)." % (wanted_sn, ", ".join(serials)))
        dev = target_id(cli, wanted_sn)
        board = board_for(dev)
        if board is None:
            sys.exit("Probe %s is wired to %s, which is neither board." % (wanted_sn, dev or "no target"))
        if wanted_board and board != wanted_board:
            sys.exit("Probe %s is wired to the %s, not the %s." % (wanted_sn, board, wanted_board))
        return wanted_sn, board
    targets = {sn: target_id(cli, sn) for sn in serials}
    found = [(sn, board_for(dev)) for sn, dev in targets.items()
             if board_for(dev) and (wanted_board is None or board_for(dev) == wanted_board)]
    if len(found) == 1:
        return found[0]
    if not found:
        sys.exit("No probe is wired to %s. Found: %s"
                 % ("the " + wanted_board if wanted_board else "a transmitter or console", describe(targets)))
    if wanted_board:
        sys.exit("More than one probe is wired to the %s, so pick one with --probe-sn. Found: %s"
                 % (wanted_board, describe(targets)))
    sys.exit("Probes are wired to more than one board (%s), so pick one with --board transmitter "
             "or --board console." % describe(targets))


def ask(question: str) -> bool:
    return input(question + " [y/N] ").strip().lower().startswith("y")


SLOT_WARNING = ("\nWARNING: %s is linked for the secure bootloader's slot, which boots it only\n"
                "         once it is signed. Flashed unsigned, the unit will not boot until a\n"
                "         signed image is installed. For bench work use a bare-metal build.\n")


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)   # keep our lines in order with the programmer's
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", choices=sorted(BOARDS), help="board to flash (default: the one wired to a probe)")
    ap.add_argument("--preset", help="CMake preset to flash (default: transmitter %s, console %s)"
                    % (BOARDS["transmitter"].default_preset, BOARDS["console"].default_preset))
    ap.add_argument("--build", action="store_true", help="rebuild the preset before flashing")
    ap.add_argument("--no-increment", action="store_true", help="with --build, reuse the previous local build number")
    ap.add_argument("--presets", type=Path, metavar="DIR",
                    help="with --build of an FDA_MODE preset, link this preset context folder's "
                         "presets into the image, which reports the folder's name as its suffix")
    ap.add_argument("--image", type=Path, metavar="FILE",
                    help="flash this .hex, .bin or .elf as-is instead of a build")
    ap.add_argument("--address", type=lambda s: int(s, 0), metavar="ADDR",
                    help="where a .bin --image goes (default: worked out from what it holds)")
    ap.add_argument("--probe-sn", default="", help="serial number of the probe to use")
    ap.add_argument("--list-probes", action="store_true", help="show the attached probes and their boards, then exit")
    ap.add_argument("--freq", type=int, help="SWD clock in kHz (default: transmitter the probe's, console 50)")
    ap.add_argument("--full-erase", action="store_true",
                    help="erase the whole chip before flashing, bootloader and user config included")
    ap.add_argument("--force", action="store_true",
                    help="flash a bootloader-slot image, or --full-erase, without asking")
    ap.add_argument("--transmitter-fw", type=Path, metavar="DIR",
                    help="transmitter firmware checkout (default: openlifu-transmitter-fw beside this repo)")
    ap.add_argument("--console-fw", type=Path, metavar="DIR",
                    help="console firmware checkout (default: openlifu-console-fw beside this repo)")
    args = ap.parse_args()
    if args.image and (args.build or args.no_increment or args.preset or args.presets):
        ap.error("--image flashes a file as-is, so it cannot go with --build, --no-increment, --preset or --presets")
    if args.presets and not args.build:
        ap.error("--presets bakes presets into a build, so it needs --build")
    if args.presets and not (args.presets / "presets.json").is_file():
        ap.error("%s has no presets.json" % args.presets)
    if args.address is not None and (args.image is None or args.image.suffix.lower() != ".bin"):
        ap.error("--address only applies to a .bin --image")

    cli = find_programmer()
    print("Programmer : %s" % cli)
    if args.list_probes:
        serials = probe_serials(cli)
        if not serials:
            print("  no probes")
        for sn in serials:
            dev = target_id(cli, sn)
            print("  %s -> %s%s" % (sn, dev or "no target", " (%s)" % board_for(dev) if board_for(dev) else ""))
        return 0

    serial, name = pick_target(cli, args.probe_sn, args.board)
    board = BOARDS[name]
    print("Board      : %s (%s, probe %s)" % (name, board.chip, serial))

    if args.image:
        image = args.image.resolve()
        if not image.is_file():
            sys.exit("%s does not exist" % image)
        kind, address, head = inspect_image(image, args.address)
        describe_image(image, kind, address, head, name, board)
        if kind == "slot" and not args.force:
            print(SLOT_WARNING % image.name)
            if not ask("Flash it anyway?"):
                print("Aborted.")
                return 1
        if kind == "signed":
            if args.full_erase:
                sys.exit("--full-erase would remove the bootloader a signed image boots from.")
            if not args.force:
                print("\nWARNING: a signed image goes in the bootloader's slot and boots only if the\n"
                      "         secure bootloader is already on the chip. A bare-metal unit will not\n"
                      "         boot afterwards.\n")
                if not ask("Is the bootloader on this %s?" % name):
                    print("Aborted.")
                    return 1
        if args.full_erase and not args.force and not confirm_full_erase(name):
            return 1
    else:
        repo = (getattr(args, name + "_fw") or WORKSPACE / board.tree).resolve()
        if not (repo / "CMakePresets.json").is_file():
            sys.exit("%s is not the %s firmware tree; pass --%s-fw" % (repo, name, name))
        presets = cmake_presets(repo)
        preset = args.preset or board.default_preset
        if preset not in presets:
            sys.exit("%s has no preset %r. It has: %s" % (repo, preset, ", ".join(sorted(presets))))
        slot_preset = not cache_flag(presets[preset], "BARE_METAL")
        fda_preset = cache_flag(presets[preset], "FDA_MODE")
        # Asked before building, so the answer is not kept waiting on the compiler.
        if slot_preset and not args.force:
            print(SLOT_WARNING % ("'%s'" % preset))
            if not ask("Flash it anyway?"):
                print("Aborted.")
                return 1
        if args.full_erase and not args.force and not confirm_full_erase(name):
            return 1
        if args.presets:
            write_preset_headers(repo, name, fda_preset, args.presets.resolve())
        elif args.build and fda_preset:
            print("Presets    : none given, so this links whatever %s the tree already has"
                  % (PRESET_DIR / "fda_preset.h").as_posix())
        if args.build:
            build(repo, preset, not args.no_increment, stamp=not fda_preset,
                  variant=args.presets.resolve().name if args.presets else None)
        image = newest_hex(repo, preset)
        kind, address, head = inspect_image(image, None)
        describe_image(image, kind, address, head, name, board)
        report_version(repo, preset, image)
        if kind in ("slot", "bare metal") and (kind == "slot") != slot_preset:
            print("WARNING: this is a %s image, but %s is a %s preset. The build may be stale,\n"
                  "         so rebuild with --build." % (kind, preset, "bootloader" if slot_preset else "bare-metal"))

    connect = ["port=SWD", "sn=" + serial, "mode=UR", "reset=HWrst"]
    freq = args.freq if args.freq is not None else board.freq_khz
    if freq:
        connect.append("freq=%d" % freq)
    erase = ["-e", "all"] if args.full_erase else []
    # A .bin carries no addresses, so the programmer is told where it goes.
    download = ["-d", str(image)] + (["0x%08X" % address] if image.suffix.lower() == ".bin" else [])
    print("Programming: probe %s, %s%s" % (serial, " ".join(connect[2:]), ", full chip erase" if erase else ""))
    if subprocess.run([cli, "-c", *connect, *erase, *download, "--verify", "-rst"]).returncode != 0:
        sys.exit("programming failed. Check the SWD wiring and that the %s is powered." % name)
    print("Flashed OK.")
    if board.after_flash:
        print(board.after_flash)
    return 0


if __name__ == "__main__":
    sys.exit(main())
