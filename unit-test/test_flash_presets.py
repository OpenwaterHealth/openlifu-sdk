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
import subprocess
from pathlib import Path

import pytest

from sr003_presets import DEFAULTS

from openlifu_sdk.io.LIFUTXPresets import compile_preset, regs_crc

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


def test_the_set_compiles_as_the_operator_interface_does(flash, tmp_path):
    """settings_crc over the file's exact bytes, id from the manifest, in order."""
    context = _context(tmp_path)
    configs = flash.load_preset_set(context)
    assert [mc["id"] for mc in configs] == ["bench_a", "bench_b"]
    for mc in configs:
        raw = (context / mc["id"] / ("%s_settings.json" % mc["id"])).read_bytes()
        want = compile_preset(json.loads(raw), preset_id=mc["id"], settings_bytes=raw)
        assert (mc["settings_crc"], regs_crc(mc)) == (want["settings_crc"], regs_crc(want))


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
    header.write_text('#define FW_VERSION "2.0.8-4-g0bad0e8+fda"\n', encoding="utf-8")
    monkeypatch.setattr(flash, "add_toolchain_to_path", lambda: None)
    monkeypatch.setattr(flash.subprocess, "run", lambda *a, **k: None)
    flash.build(tree, "Release", True, stamp=stamp)
    text = header.read_text(encoding="utf-8")
    assert ("+b1" in text) is stamp
    assert (tree / "build/.localbuild").exists() is stamp
