# SPDX-License-Identifier: Apache-2.0
"""LIFUReleaseUpdate: load a release, plan a unit's update without DFU, run
it in order and read it back.

The release is built here with fresh keys that the SDK is made to trust
(the real private keys are not available); the console and the transmitter
chain are fakes that keep firmware headers and preset slots.
"""

from __future__ import annotations

import hashlib
import json

import pytest

pytest.importorskip("cryptography")

from openlifu_sdk.io import LIFUCrypto  # noqa: E402  # reason: after importorskip
from openlifu_sdk.io import LIFUPresetPackage as pp  # noqa: E402  # reason: after importorskip
from openlifu_sdk.io import LIFUReleaseUpdate as ru  # noqa: E402  # reason: after importorskip
from openlifu_sdk.io.component import FirmwareDomain  # noqa: E402  # reason: after importorskip

COMMIT = "76" * 20
OLD_COMMIT = "83" * 20
RELEASED = 3


# ---------------------------------------------------------------------------
# A release, signed with keys the SDK is made to trust
# ---------------------------------------------------------------------------

@pytest.fixture
def keys(monkeypatch, tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    out = {}
    for board in LIFUCrypto.FIRMWARE_BOARDS:
        for domain in LIFUCrypto.FIRMWARE_DOMAINS:
            key = ec.generate_private_key(ec.SECP256R1())
            d = tmp_path / "keys" / board / domain
            d.mkdir(parents=True)
            (d / "ecdsa_private.pem").write_bytes(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()))
            out[board, domain] = LIFUCrypto.SigningKeys.from_directory(d)
    monkeypatch.setattr(LIFUCrypto, "trusted_public_key",
                        lambda b, d: out[b, d].private_key.public_key())
    monkeypatch.setattr(ru.time, "sleep", lambda _s: None)
    return out


def _sums(folder):
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(folder).as_posix()}"
             for p in sorted(folder.rglob("*")) if p.is_file() and p.name != "SHA256SUMS"]
    (folder / "SHA256SUMS").write_text("\n".join(lines) + "\n")


def app_image(keys, board, domain, version, body=b"app"):
    return LIFUCrypto.sign_firmware_bytes(body * 64, keys[board, domain], version)


def make_firmware(root, keys, domain="research", *, tx=("2.1.0", b"tx-rc8"),
                  console=("1.3.0", b"hv-rc8"), console_domain=None):
    fw = root / "firmware"
    for board, folder, (version, body), dom in (
            ("console", "openlifu-console-fw", console, console_domain or domain),
            ("transmitter", "openlifu-transmitter-fw", tx, domain)):
        (fw / folder).mkdir(parents=True)
        (fw / folder / f"{folder}-signed.bin").write_bytes(app_image(keys, board, dom, version, body))
    for board, folder, version, bl in (("console", "open-lifu-console-bl-updater", "1.2.31", "1.1.0-rc.3"),
                                       ("transmitter", "open-lifu-transmitter-bl-updater", "2.0.31",
                                        "1.1.0-rc.2")):
        (fw / folder).mkdir(parents=True)
        (fw / folder / f"{folder}-signed.bin").write_bytes(
            app_image(keys, board, "research", version, b"shim"))
        (fw / folder / f"{folder}.cdx.json").write_text(json.dumps(
            {"components": [{"name": ru.BOOTLOADER_COMPONENTS[board], "version": bl}]}))
        _sums(fw / folder)
    (fw / "firmware-lock.json").write_text(json.dumps({"firmware": {}}))
    _sums(fw)
    return fw


def preset_package(keys, board, domain="research", commit=COMMIT, body=b"preset"):
    if board == "console":
        return pp.build_preset_package(body * 8, diathermy_sha=commit, context=domain,
                                       keys=keys[board, domain])
    return pp.build_tx_preset(body * 8, diathermy_sha=commit, context=domain,
                              keys=keys[board, domain])


def make_presets(root, keys, domain="research", commit=COMMIT, n=RELEASED):
    d = root / "presets"
    d.mkdir()
    (d / "console-presets.bin").write_bytes(preset_package(keys, "console", domain, commit))
    entries = []
    for slot in range(n):
        (d / f"transmitter-slot{slot:02d}-module0.bin").write_bytes(
            preset_package(keys, "transmitter", domain, commit, body=bytes([slot]) * 8))
        entries.append({"slot": slot, "id": f"p{slot}", "modules": 1})
    (d / "presets-manifest.json").write_text(json.dumps(
        {"context": domain, "signed_with": domain, "diathermy_sha": commit, "presets": entries}))
    _sums(d)
    return d


@pytest.fixture
def release(tmp_path, keys):
    return ru.ReleaseBundle.load(make_firmware(tmp_path, keys), make_presets(tmp_path, keys),
                                 workdir=tmp_path / "work")


# ---------------------------------------------------------------------------
# A console and a transmitter chain
# ---------------------------------------------------------------------------

class FakeBoard:
    """Firmware reads for one or more modules: version, domain, header."""

    def __init__(self, images, domain="research"):
        self.images = list(images)
        self.domains = [domain] * len(self.images)
        self.versions = [LIFUCrypto.FirmwareHeader.from_bytes(i).fw_version_str for i in self.images]

    def get_version(self, module=0):
        return self.versions[module]

    def firmware_domain(self, module=0):
        return FirmwareDomain(self.domains[module])

    def firmware_header(self, module=0):
        return LIFUCrypto.FirmwareHeader.from_bytes(self.images[module])

    def is_connected(self):
        return True


class FakeHv(FakeBoard):
    def __init__(self, image, preset=None):
        super().__init__([image])
        self.preset = preset
        self.log = []

    def get_preset_info(self):
        return pp.parse_preset_header(self.preset) if self.preset else None

    def write_presets(self, package):
        self.preset = package
        self.log.append("write console")

    def turn_12v_off(self):
        self.log.append("12v off")

    def turn_12v_on(self):
        self.log.append("12v on")


class FakeTx(FakeBoard):
    def __init__(self, images, slots=None):
        super().__init__(images)
        self.slots = slots or {m: {} for m in range(len(images))}
        self.log = []

    def get_module_count(self):
        return len(self.images)

    def get_preset_info(self, slot, module=0):
        data = self.slots[module].get(slot)
        return pp.parse_preset_header(data) if data else None

    def write_preset(self, slot, data, module=0):
        self.slots[module][slot] = data
        self.log.append(("write", module, slot))

    def delete_preset(self, slot, module=0):
        self.slots[module].pop(slot)
        self.log.append(("delete", module, slot))


def unit(keys, release, *, tx_version="2.1.0", tx_body=b"tx-rc8", modules=2, commit=COMMIT,
         domain="research", hv_version="1.3.0", hv_body=b"hv-rc8"):
    """A unit whose firmware is built from *tx_body* / *hv_body* (the
    release's are "-rc8"; the release's exact image when they match) and
    whose presets are the release's own packages, or packages of another
    diathermy *commit*."""
    def image(board, version, body):
        rel = release.apps[board]
        if (version, body) == (rel.version, b"tx-rc8" if board == "transmitter" else b"hv-rc8"):
            return rel.path.read_bytes()
        return app_image(keys, board, domain, version, body)

    def tx_preset(slot):
        if commit == COMMIT and domain == "research":
            return release.presets.tx_image(slot, 0)
        return preset_package(keys, "transmitter", domain, commit, body=bytes([slot]) * 8)

    slots = {m: {s: tx_preset(s) for s in range(RELEASED)} for m in range(modules)}
    tx = FakeTx([image("transmitter", tx_version, tx_body)] * modules, slots)
    tx.domains = [domain] * modules
    console = (release.presets.console if commit == COMMIT and domain == "research"
               else preset_package(keys, "console", domain, commit))
    hv = FakeHv(image("console", hv_version, hv_body), console)
    hv.domains = [domain]
    return hv, tx


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def test_a_release_loads_with_its_domain_and_bootloaders(release):
    assert release.domain == "research"
    assert release.apps["transmitter"].version == "2.1.0"
    assert release.bootloaders == {"console": "1.1.0-rc.3", "transmitter": "1.1.0-rc.2"}
    assert release.presets.ids == ("p0", "p1", "p2") and release.presets.commit == COMMIT


def test_a_file_that_does_not_match_its_sum_is_refused(tmp_path, keys):
    fw = make_firmware(tmp_path, keys)
    target = fw / "openlifu-transmitter-fw" / "openlifu-transmitter-fw-signed.bin"
    target.write_bytes(target.read_bytes() + b"\x00")
    with pytest.raises(ru.ReleaseError, match="does not match its SHA-256"):
        ru.ReleaseBundle.load(fw)


def test_applications_in_different_domains_are_refused(tmp_path, keys):
    fw = make_firmware(tmp_path, keys, console_domain="clinical")
    with pytest.raises(ru.ReleaseError, match="different domains"):
        ru.ReleaseBundle.load(fw)


def test_presets_from_another_domain_are_refused(tmp_path, keys):
    fw = make_firmware(tmp_path, keys)
    with pytest.raises(ru.ReleaseError, match="not signed with the research"):
        ru.ReleaseBundle.load(fw, make_presets(tmp_path, keys, domain="clinical"))


def test_a_zipped_release_loads(tmp_path, keys):
    import shutil

    make_firmware(tmp_path, keys)
    archive = shutil.make_archive(str(tmp_path / "firmware-vX"), "zip", tmp_path, "firmware")
    bundle = ru.ReleaseBundle.load(archive, workdir=tmp_path / "work")
    assert bundle.domain == "research" and bundle.presets is None


# ---------------------------------------------------------------------------
# The plan (no DFU)
# ---------------------------------------------------------------------------

def _actions(steps):
    return [(s.target, s.action) for s in steps]


def test_a_unit_on_the_release_needs_nothing(release, keys):
    hv, tx = unit(keys, release)
    plan = ru.ReleaseUpdate(release, hv, tx).plan()
    assert plan.work == [] and plan.refusals == []


def test_an_equal_version_but_another_build_is_updated(release, keys):
    """Every 2.1.0-rc.N signs as 2.1.0: the body hash tells rc.7 from rc.8."""
    hv, tx = unit(keys, release, tx_body=b"tx-rc7")
    plan = ru.ReleaseUpdate(release, hv, tx).plan()
    assert _actions(plan.firmware) == [("console", "none"), ("transmitter master", "firmware"),
                                       ("transmitter module 1", "firmware")]


def test_a_field_unit_is_updated(release, keys):
    hv, tx = unit(keys, release, tx_version="2.0.8", hv_version="1.2.6")
    plan = ru.ReleaseUpdate(release, hv, tx).plan()
    assert {s.action for s in plan.firmware} == {"firmware"}


def test_a_newer_unit_is_refused_before_dfu(release, keys):
    hv, tx = unit(keys, release, tx_version="2.1.1")
    plan = ru.ReleaseUpdate(release, hv, tx).plan()
    assert [s.target for s in plan.refusals] == ["transmitter master", "transmitter module 1"]
    with pytest.raises(ru.ReleaseError, match="downgrade"):
        ru.ReleaseUpdate(release, hv, tx).run(plan)
    assert tx.log == [] and hv.log == []


def test_a_research_release_is_never_put_on_a_clinical_unit(release, keys):
    hv, tx = unit(keys, release, domain="clinical")
    plan = ru.ReleaseUpdate(release, hv, tx).plan()
    assert all("clinical" in s.detail for s in plan.refusals if s.board)
    assert len([s for s in plan.refusals if s.board]) == 3


def test_presets_from_another_build_are_rewritten_on_every_module(release, keys):
    hv, tx = unit(keys, release, commit=OLD_COMMIT)
    plan = ru.ReleaseUpdate(release, hv, tx).plan()
    writes = [s for s in plan.presets if s.action == "write-presets"]
    assert len(writes) == 1 + 2 * RELEASED          # the console page + each module's slots
    assert all(OLD_COMMIT[:12] in s.detail for s in writes)


def test_a_preset_past_the_released_set_is_cleared(release, keys):
    hv, tx = unit(keys, release)
    tx.slots[1][13] = preset_package(keys, "transmitter", body=b"stray")
    plan = ru.ReleaseUpdate(release, hv, tx).plan()
    assert _actions(plan.work) == [("transmitter module 1 slot 13", "clear-slot")]


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

def test_presets_are_written_and_read_back(release, keys):
    hv, tx = unit(keys, release, commit=OLD_COMMIT)
    tx.slots[0][14] = preset_package(keys, "transmitter", body=b"stray")
    update = ru.ReleaseUpdate(release, hv, tx)
    result = update.run(update.plan())
    assert result.ok, result.lines
    assert hv.log == ["write console"]
    assert ("delete", 0, 14) in tx.log
    assert sorted(e for e in tx.log if e[0] == "write") == sorted(
        ("write", m, s) for m in range(2) for s in range(RELEASED))


def test_a_read_back_mismatch_is_reported(release, keys):
    hv, tx = unit(keys, release, commit=OLD_COMMIT)
    tx.write_preset = lambda slot, data, module=0: tx.log.append(("dropped", module, slot))
    update = ru.ReleaseUpdate(release, hv, tx)
    result = update.run(update.plan())
    assert not result.ok
    assert any(line.startswith("[FAIL] transmitter module 1 slot 00") for line in result.lines)


class _FakeUpdaters:
    """Stands in for LIFUFirmwareUpdate / LIFUTransmitterFirmwareUpdate:
    records the order, installs the release image on the fake board."""

    def __init__(self, release, hv, tx, fail_slave=None):
        self.order = []
        rel = self
        rel_app = {b: i.path.read_bytes() for b, i in release.apps.items()}

        class Console:
            def __init__(self, hv=None, **_):
                pass

            def update(self, **_):
                rel.order.append("console")
                hv.images[0] = rel_app["console"]
                return type("R", (), {"summary": "console updated"})()

        class Transmitter:
            def __init__(self, tx=None, **_):
                pass

            def update_slave(self, module, **_):
                rel.order.append(f"module {module}")
                if module == fail_slave:
                    raise RuntimeError("bootloader refused the image")
                tx.images[module] = rel_app["transmitter"]
                return type("R", (), {"summary": f"module {module} updated"})()

            def update(self, **_):
                rel.order.append("master")
                tx.images[0] = rel_app["transmitter"]
                return type("R", (), {"summary": "master updated"})()

        self.Console, self.Transmitter = Console, Transmitter


@pytest.fixture
def fake_updaters(monkeypatch):
    from openlifu_sdk.io import LIFUDFU, LIFUFirmwareUpdate

    def install(release, hv, tx, **kw):
        fakes = _FakeUpdaters(release, hv, tx, **kw)
        monkeypatch.setattr(LIFUFirmwareUpdate, "LIFUFirmwareUpdate", fakes.Console)
        monkeypatch.setattr(LIFUFirmwareUpdate, "LIFUTransmitterFirmwareUpdate", fakes.Transmitter)
        monkeypatch.setattr(LIFUDFU.LIFUDFUManager, "abort_dfu",
                            lambda self, **_: fakes.order.append("abort dfu"))
        return fakes
    return install


def test_firmware_goes_console_then_slaves_then_master(release, keys, fake_updaters):
    hv, tx = unit(keys, release, modules=3, tx_body=b"tx-rc7", hv_body=b"hv-rc7")
    fakes = fake_updaters(release, hv, tx)
    update = ru.ReleaseUpdate(release, hv, tx)
    result = update.run(update.plan())
    assert fakes.order == ["console", "module 1", "module 2", "master"]
    assert result.ok, result.lines


def test_a_failed_slave_update_returns_the_chain_to_its_application(release, keys,
                                                                     fake_updaters, monkeypatch):
    hv, tx = unit(keys, release, tx_body=b"tx-rc7")
    fakes = fake_updaters(release, hv, tx, fail_slave=1)
    answers = iter([False, True])          # silent after the abort, back after 12 V
    monkeypatch.setattr(ru.ReleaseUpdate, "_app_answers", staticmethod(lambda *_: next(answers)))
    update = ru.ReleaseUpdate(release, hv, tx)
    with pytest.raises(ru.ReleaseError, match="after a power-cycle"):
        update.run(update.plan())
    assert fakes.order == ["module 1", "abort dfu"]        # the master was not touched
    assert hv.log == ["12v off", "12v on"]


def test_a_refused_image_takes_the_unit_out_of_dfu():
    """A bootloader that checks the header before erasing refuses at the
    first block: the installed application is intact and is booted again."""
    from openlifu_sdk.io.LIFUDFU import DFUWriteRefusedError
    from openlifu_sdk.io.LIFUFirmwareUpdate import _program_or_abort

    aborted = []

    def program(_image):
        raise DFUWriteRefusedError("refused")

    with pytest.raises(DFUWriteRefusedError):
        _program_or_abort(program, "app.bin", lambda: aborted.append(True), entered=True)
    assert aborted == [True]
