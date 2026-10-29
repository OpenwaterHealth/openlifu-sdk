# SPDX-License-Identifier: Apache-2.0
"""examples/flash.py bakes a preset set into an image, and never stamps a
local build number on an FDA image (#108; operator-interface #192).

Presets reach a device only baked into an image; the operator interface
never writes them. The headers are generated from a preset context folder
compiled as the operator interface compiles it, so the CRCs match.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import zlib
from pathlib import Path

import pytest

from sr003_presets import DEFAULTS

from openlifu_sdk.io.LIFUTXPresets import compile_preset, preset_blob

FLASH = Path(__file__).resolve().parent.parent / "examples" / "flash.py"


@pytest.fixture(scope="module")
def flash():
    spec = importlib.util.spec_from_file_location("flash_example", FLASH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _context(root: Path, ids=("bench_a", "bench_b")) -> Path:
    """A preset context folder: presets.json plus one settings file per id."""
    context = root / "bench"
    for n, preset_id in enumerate(ids):
        (context / preset_id).mkdir(parents=True)
        doc = dict(DEFAULTS, pulse_count=DEFAULTS["pulse_count"] + n)   # a distinct settings_crc each
        (context / preset_id / ("%s_settings.json" % preset_id)).write_text(
            json.dumps(doc, indent=2), encoding="utf-8")
    (context / "presets.json").write_text(json.dumps({"presets": list(ids)}), encoding="utf-8")
    return context


def _tree(root: Path, tracked_header: bool = False) -> Path:
    """A firmware tree: a git repo whose preset headers are not tracked."""
    tree = root / "fw"
    (tree / "Core/Inc/presets").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    if tracked_header:
        (tree / "Core/Inc/presets/fda_preset.h").write_text("// committed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(tree), "add", "Core/Inc/presets/fda_preset.h"], check=True)
    return tree


def test_the_set_follows_the_operator_interface_contract(flash, tmp_path):
    """The OI's contract (SR-003): ids and order from the manifest, and
    settings_crc the CRC-32 of each settings file's exact bytes."""
    context = _context(tmp_path)
    configs = flash.load_preset_set(context)
    assert [mc["id"] for mc in configs] == ["bench_a", "bench_b"]
    for mc in configs:
        raw = (context / mc["id"] / ("%s_settings.json" % mc["id"])).read_bytes()
        assert mc["settings_crc"] == zlib.crc32(raw)
        direct = compile_preset(json.loads(raw), preset_id=mc["id"], settings_bytes=raw)
        assert preset_blob(mc) == preset_blob(direct)   # so its regs_crc too


def _blob_bytes(header: Path, symbol: str) -> bytes:
    """The bytes of the C array *symbol* in a generated header."""
    text = header.read_text(encoding="utf-8")
    body = re.search(r"\b%s\[[^]]*\]\s*=\s*\{(.*?)\};" % re.escape(symbol), text, re.S).group(1)
    body = re.sub(r"//.*", "", body)   # comments name register addresses in hex
    return bytes(int(b, 16) for b in re.findall(r"0x([0-9a-fA-F]{2})", body))


def test_dev_slot_0_is_byte_identical_to_the_fda_preset(flash, tmp_path):
    """#108 done-when: the engineering set's preset 0 is the FDA preset's
    settings file, so its baked blob is the FDA image's, byte for byte."""
    dev_context = _context(tmp_path / "dev", ids=("diathermy", "extended"))
    fda_context = tmp_path / "fda" / "diathermy_ctx"
    (fda_context / "diathermy").mkdir(parents=True)
    shutil.copy2(dev_context / "diathermy" / "diathermy_settings.json", fda_context / "diathermy")
    (fda_context / "presets.json").write_text(json.dumps({"presets": ["diathermy"]}), encoding="utf-8")

    dev_tree, fda_tree = _tree(tmp_path / "dev"), _tree(tmp_path / "fda")
    flash.write_preset_headers(dev_tree, "transmitter", False, dev_context)
    flash.write_preset_headers(fda_tree, "transmitter", True, fda_context)

    fda_blob = _blob_bytes(fda_tree / "Core/Inc/presets/fda_preset.h", "FDA_PRESET_BLOB")
    dev_header = next((dev_tree / "Core/Inc/presets").glob("*diathermy.h"))
    dev_blob = _blob_bytes(dev_header, "PRESET_BENCH_DIATHERMY_BLOB")
    assert dev_blob == fda_blob == preset_blob(flash.load_preset_set(fda_context)[0])


def test_fda_mode_is_read_through_preset_inheritance(flash, tmp_path):
    """An FDA preset that inherits FDA_MODE must still count as FDA."""
    (tmp_path / "CMakePresets.json").write_text(json.dumps({
        "configurePresets": [
            {"name": "fda-base", "hidden": True, "cacheVariables": {"FDA_MODE": "ON"}},
            {"name": "bare", "hidden": True, "cacheVariables": {"BARE_METAL": "ON"}},
            {"name": "Release-FDA-BareMetal", "inherits": ["fda-base", "bare"],
             "cacheVariables": {"CMAKE_BUILD_TYPE": "Release"}},
            {"name": "Release", "cacheVariables": {"CMAKE_BUILD_TYPE": "Release"}},
        ],
        "buildPresets": [{"name": "Release-FDA-BareMetal"}, {"name": "Release"}],
    }), encoding="utf-8")
    presets = flash.cmake_presets(tmp_path)
    assert presets["Release-FDA-BareMetal"] == {
        "FDA_MODE": "ON", "BARE_METAL": "ON", "CMAKE_BUILD_TYPE": "Release"}
    assert "FDA_MODE" not in presets["Release"]
    assert set(presets) == {"Release-FDA-BareMetal", "Release"}   # only buildable presets are offered


@pytest.mark.parametrize("value, expected", [
    ("ON", True), ("on", True), ("TRUE", True), ("1", True), (True, True),
    ({"type": "BOOL", "value": "ON"}, True), ("OFF", False), (False, False),
    ({"type": "BOOL", "value": "FALSE"}, False), ("X-NOTFOUND", False)])
def test_fda_mode_is_read_as_cmake_reads_it(flash, value, expected):
    """CMakePresets.json may write a boolean as a bool, a string or an
    object; an FDA preset must not be taken for a DEV one."""
    assert flash.cache_flag({"FDA_MODE": value}, "FDA_MODE") is expected
    assert flash.cache_flag({}, "FDA_MODE") is False


def test_an_unreadable_fda_mode_is_refused(flash):
    with pytest.raises(SystemExit, match="cannot read FDA_MODE"):
        flash.cache_flag({"FDA_MODE": "maybe"}, "FDA_MODE")


@pytest.mark.parametrize("fda", [True, False], ids=["fda", "dev"])
def test_main_bakes_and_builds_by_the_presets_fda_mode(flash, tmp_path, monkeypatch, fda):
    """#108 done-when, end to end through main(): an FDA preset gets one
    preset and no build number; a DEV preset gets the set and one."""
    tree = tmp_path / "fw"
    tree.mkdir()
    (tree / "CMakePresets.json").write_text(json.dumps({
        "configurePresets": [{"name": "P", "cacheVariables": {
            "FDA_MODE": {"type": "BOOL", "value": "ON" if fda else "OFF"}, "BARE_METAL": "ON"}}],
        "buildPresets": [{"name": "P"}],
    }), encoding="utf-8")
    context = _context(tmp_path, ids=("only",))
    calls = {}

    class Built(Exception):
        pass

    def newest_hex(*_):
        raise Built

    monkeypatch.setattr(flash, "find_programmer", lambda: "cli")
    monkeypatch.setattr(flash, "pick_target", lambda *_: ("sn", "transmitter"))
    monkeypatch.setattr(flash, "write_preset_headers",
                        lambda repo, name, is_fda, ctx: calls.update(headers=(name, is_fda, ctx)))
    monkeypatch.setattr(flash, "build",
                        lambda repo, preset, increment, stamp=True: calls.update(stamp=stamp))
    monkeypatch.setattr(flash, "newest_hex", newest_hex)
    monkeypatch.setattr("sys.argv", ["flash.py", "--board", "transmitter", "--preset", "P", "--build",
                                     "--presets", str(context), "--transmitter-fw", str(tree)])
    with pytest.raises(Built):
        flash.main()
    assert calls == {"headers": ("transmitter", fda, context.resolve()), "stamp": not fda}


def test_a_dev_transmitter_build_gets_the_whole_set(flash, tmp_path):
    tree = _tree(tmp_path)
    flash.write_preset_headers(tree, "transmitter", False, _context(tmp_path))
    table = (tree / "Core/Inc/presets/preset_table.h").read_text(encoding="utf-8")
    assert "#define PRESET_COUNT 2u" in table
    assert len(list((tree / "Core/Inc/presets").glob("*.h"))) == 3   # two presets + the table


def test_a_dev_console_build_gets_the_whole_hv_table(flash, tmp_path):
    tree = _tree(tmp_path)
    flash.write_preset_headers(tree, "console", False, _context(tmp_path))
    assert (tree / "Core/Inc/presets/preset_hv_table.h").is_file()


@pytest.mark.parametrize("board", ["transmitter", "console"])
def test_an_fda_build_gets_its_one_preset(flash, tmp_path, board):
    tree = _tree(tmp_path)
    flash.write_preset_headers(tree, board, True, _context(tmp_path, ids=("fda",)))
    assert [p.name for p in (tree / "Core/Inc/presets").glob("*.h")] == ["fda_preset.h"]


def test_an_fda_build_refuses_more_than_one_preset(flash, tmp_path):
    with pytest.raises(SystemExit, match="an FDA image holds one preset"):
        flash.write_preset_headers(_tree(tmp_path), "transmitter", True, _context(tmp_path))


def test_headers_from_another_set_are_removed(flash, tmp_path):
    tree = _tree(tmp_path)
    stale = tree / "Core/Inc/presets/fda_preset.h"
    stale.write_text("// from an earlier FDA build\n", encoding="utf-8")
    flash.write_preset_headers(tree, "transmitter", False, _context(tmp_path))
    assert not stale.exists()


def test_a_refused_set_leaves_the_tree_as_it_was(flash, tmp_path):
    tree = _tree(tmp_path)
    kept = tree / "Core/Inc/presets/fda_preset.h"
    kept.write_text("// the last FDA build\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="an FDA image holds one preset"):
        flash.write_preset_headers(tree, "transmitter", True, _context(tmp_path))
    assert kept.read_text(encoding="utf-8") == "// the last FDA build\n"


def test_a_tree_git_cannot_read_is_refused(flash, tmp_path):
    """The tracked-header guard fails closed."""
    tree = tmp_path / "exported"
    (tree / "Core/Inc/presets").mkdir(parents=True)
    with pytest.raises(SystemExit, match="cannot tell whether"):
        flash.write_preset_headers(tree, "transmitter", False, _context(tmp_path))


def test_a_tracked_header_is_not_overwritten(flash, tmp_path):
    tree = _tree(tmp_path, tracked_header=True)
    with pytest.raises(SystemExit, match="tracks its own preset headers"):
        flash.write_preset_headers(tree, "transmitter", True, _context(tmp_path, ids=("fda",)))
    assert (tree / "Core/Inc/presets/fda_preset.h").read_text(encoding="utf-8") == "// committed\n"


@pytest.mark.parametrize("stamp", [True, False], ids=["dev", "fda"])
def test_only_a_non_fda_build_is_stamped(flash, tmp_path, monkeypatch, stamp):
    """#192 D2: an FDA image must report exactly its released version."""
    tree = tmp_path / "fw"
    header = tree / "build/Release/generated/version.h"
    header.parent.mkdir(parents=True)
    # As left by an earlier stamped build of the same preset.
    header.write_text('#define FW_VERSION "2.0.8-4-g0bad0e8+fda+b7"\n', encoding="utf-8")
    monkeypatch.setattr(flash, "add_toolchain_to_path", lambda: None)
    monkeypatch.setattr(flash.subprocess, "run", lambda *a, **k: None)
    flash.build(tree, "Release", True, stamp=stamp)
    text = header.read_text(encoding="utf-8")
    assert text == ('#define FW_VERSION "2.0.8-4-g0bad0e8+fda%s"\n' % ("+b1" if stamp else ""))
    assert (tree / "build/.localbuild").exists() is stamp
