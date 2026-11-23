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


#: Each updater's signed version and the bootloader it carries.
UPDATERS = {"console": ("1.2.31", "1.1.0-rc.3"), "transmitter": ("2.0.31", "1.1.0-rc.2")}


def make_firmware(root, keys, domain="research", *, tx=("2.1.0", b"tx-rc8"),
                  console=("1.3.0", b"hv-rc8"), console_domain=None, updaters=None):
    fw = root / "firmware"
    updaters = updaters or UPDATERS
    for board, folder, (version, body), dom in (
            ("console", "openlifu-console-fw", console, console_domain or domain),
            ("transmitter", "openlifu-transmitter-fw", tx, domain)):
        (fw / folder).mkdir(parents=True)
        (fw / folder / f"{folder}-signed.bin").write_bytes(app_image(keys, board, dom, version, body))
    for board, folder in (("console", "open-lifu-console-bl-updater"),
                          ("transmitter", "open-lifu-transmitter-bl-updater")):
        version, bl = updaters[board]
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


def make_presets(root, keys, domain="research", commit=COMMIT, n=RELEASED, name="presets"):
    d = root / name
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
        self.configs = [b"OWCF" + bytes([m]) + b'{"sens":1}' for m in range(len(self.images))]
        #: What get_bootloader_version answers: None like every firmware
        #: before OW_CMD_BL_VERSION.
        self.bootloaders = [None] * len(self.images)

    def get_version(self, module=0):
        return self.versions[module]

    def firmware_domain(self, module=0):
        return FirmwareDomain(self.domains[module])

    def firmware_header(self, module=0):
        return LIFUCrypto.FirmwareHeader.from_bytes(self.images[module])

    def get_bootloader_version(self, module=0):
        return self.bootloaders[module]

    def is_connected(self):
        return True

    def read_config(self, module=0):
        if isinstance(self.configs[module], Exception):
            raise self.configs[module]
        return _Config(self.configs[module])


class _Config:
    def __init__(self, raw):
        self.raw = raw

    def to_wire_bytes(self):
        return self.raw


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


def _from_before_the_signature_check(hv, tx, tx_version="2.0.8", hv_version="1.2.6"):
    """What a field unit answers: its version, and an error for the
    signature check and the header (bench, 2026-11-22: console 1.2.6 and
    transmitter 2.0.8)."""
    hv.versions, hv.domains = [hv_version], ["error"]
    tx.versions = [tx_version] * len(tx.images)
    tx.domains = ["error"] * len(tx.images)
    for board in (hv, tx):
        board.firmware_header = lambda module=0: None
    return hv, tx


def test_a_field_unit_that_cannot_name_its_key_is_updated(release, keys):
    hv, tx = _from_before_the_signature_check(*unit(keys, release))
    plan = ru.ReleaseUpdate(release, hv, tx).plan()
    assert not plan.refusals
    assert [s.action for s in plan.firmware] == ["firmware"] * 3
    assert all("1.0.x bootloader is replaced" in s.detail for s in plan.firmware)


def test_an_fda_field_build_is_refused_before_dfu(release, keys):
    hv, tx = _from_before_the_signature_check(*unit(keys, release), tx_version="2.0.8+fda")
    plan = ru.ReleaseUpdate(release, hv, tx).plan()
    assert [s.target for s in plan.refusals] == ["transmitter master", "transmitter module 1"]
    assert all("FDA build" in s.detail for s in plan.refusals)


def test_a_current_firmware_that_cannot_name_its_key_is_still_refused(release, keys):
    hv, tx = _from_before_the_signature_check(*unit(keys, release), tx_version="2.1.0",
                                              hv_version="1.3.0")
    plan = ru.ReleaseUpdate(release, hv, tx).plan()
    assert len([s for s in plan.refusals if s.board]) == 3
    assert all("cannot tell which key" in s.detail for s in plan.refusals)


def test_a_field_unit_given_a_clinical_release_is_converted(release, keys):
    hv, tx = _from_before_the_signature_check(*unit(keys, release))
    step = ru.firmware_step(ru.read_boards(hv, tx)[1], release.apps["transmitter"], "clinical",
                            "1.1.0-rc.3-clinical")
    assert step.action == "convert" and "1.1.0-rc.3-clinical replaces" in step.detail


def test_a_field_unit_before_the_secure_bootloader_is_not_converted(release, keys):
    hv, tx = _from_before_the_signature_check(*unit(keys, release), hv_version="1.2.5")
    step = ru.firmware_step(ru.read_boards(hv, tx)[0], release.apps["console"], "clinical",
                            "1.1.0-rc.3-clinical")
    assert step.action == "refuse" and "predates the secure bootloader" in step.detail


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
        self.targets = {}
        rel = self
        rel_app = {b: i.path.read_bytes() for b, i in release.apps.items()}

        class Console:
            def __init__(self, hv=None, **_):
                pass

            def update(self, **kw):
                rel.order.append("console")
                rel.targets["console"] = kw.get("bl_target")
                hv.images[0] = rel_app["console"]
                return type("R", (), {"summary": "console updated"})()

        class Transmitter:
            def __init__(self, tx=None, **_):
                pass

            def update_slave(self, module, **kw):
                rel.order.append(f"module {module}")
                rel.targets[f"module {module}"] = kw.get("bl_target")
                if module == fail_slave:
                    raise RuntimeError("bootloader refused the image")
                tx.images[module] = rel_app["transmitter"]
                return type("R", (), {"summary": f"module {module} updated"})()

            def update(self, **kw):
                rel.order.append("master")
                rel.targets["master"] = kw.get("bl_target")
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


def test_a_field_unit_gets_the_updaters_bootloader(release, keys, fake_updaters):
    hv, tx = unit(keys, release, tx_version="2.0.8", hv_version="1.2.6")
    fakes = fake_updaters(release, hv, tx)
    up = ru.ReleaseUpdate(release, hv, tx)
    up.run(up.plan())
    assert fakes.targets == {"console": "1.1.0-rc.3", "module 1": "1.1.0-rc.2",
                             "master": "1.1.0-rc.2"}


def test_an_updater_signed_below_the_installed_app_is_not_tried(release, keys,
                                                                fake_updaters):
    """The test release's updaters are signed 1.2.31 / 2.0.31: a unit on
    1.3.0 / 2.1.0 would refuse them, so its bootloader is kept (no target)."""
    hv, tx = unit(keys, release, tx_body=b"tx-rc7", hv_body=b"hv-rc7")
    fakes = fake_updaters(release, hv, tx)
    seen = []
    up = ru.ReleaseUpdate(release, hv, tx, report=seen.append)
    up.run(up.plan())
    assert fakes.targets == {"console": None, "module 1": None, "master": None}
    assert any("keeps its bootloader" in line and "2.0.31" in line for line in seen)


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


# ---------------------------------------------------------------------------
# User configuration: kept across the update
# ---------------------------------------------------------------------------

def test_a_changed_user_configuration_fails_the_read_back(release, keys, fake_updaters):
    hv, tx = unit(keys, release, tx_body=b"tx-rc7")
    fake_updaters(release, hv, tx)
    update = ru.ReleaseUpdate(release, hv, tx)
    plan = update.plan()
    tx.configs[1] = b"OWCF\x01{}"                  # module 1 lost its configuration
    result = update.run(plan)
    assert not result.ok
    assert "[FAIL] transmitter module 1: user configuration CHANGED" in result.lines
    assert "[ok] console: user configuration unchanged" in result.lines


def test_a_configuration_unreadable_before_is_skipped_not_failed(release, keys):
    """Old firmware (a field unit) may not answer the read: nothing to
    compare, so it is reported and does not fail the update."""
    hv, tx = unit(keys, release)
    hv.configs[0] = RuntimeError("no reply")
    update = ru.ReleaseUpdate(release, hv, tx)
    update.plan()
    hv.configs[0] = b"OWCF{}"
    result = update.verify()
    assert result.ok
    assert any(line.startswith("[skip] console: user configuration could not be read before")
               and "no reply" in line for line in result.lines)


def test_a_configuration_unreadable_after_the_update_fails(release, keys):
    hv, tx = unit(keys, release)
    update = ru.ReleaseUpdate(release, hv, tx)
    update.plan()
    hv.configs[0] = RuntimeError("no reply")
    result = update.verify()
    assert not result.ok
    assert any(line.startswith("[FAIL] console: user configuration could not be read back")
               for line in result.lines)


# ---------------------------------------------------------------------------
# Research -> clinical conversion
# ---------------------------------------------------------------------------

CLINICAL_BL = {"console": "1.1.0-rc.3-clinical", "transmitter": "1.1.0-rc.3-clinical"}


def make_clinical_firmware(root, keys, bootloaders=None):
    """The diathermy clinical-firmware layout: clinical-firmware/ with the
    lock and one folder per repo, each with its own SHA256SUMS (no top-level
    one); the updaters are research-signed and carry the clinical bootloader."""
    fw = root / "clinical-firmware"
    for board, folder, version, body in (("console", "openlifu-console-fw", "1.3.0", b"hv-rc8"),
                                         ("transmitter", "openlifu-transmitter-fw", "2.1.0",
                                          b"tx-rc8")):
        (fw / folder).mkdir(parents=True)
        (fw / folder / f"{folder}-v1-signed.bin").write_bytes(
            app_image(keys, board, "clinical", version, body))
        (fw / folder / f"{folder}-v1-production.bin").write_bytes(b"production")
        _sums(fw / folder)
    for board, folder in (("console", "open-lifu-console-bl-updater"),
                          ("transmitter", "open-lifu-transmitter-bl-updater")):
        (fw / folder).mkdir(parents=True)
        (fw / folder / f"{folder}-v1-clinical-signed.bin").write_bytes(
            app_image(keys, board, "research", "31.63.30", b"shim"))
        (fw / folder / f"{folder}-v1-clinical.cdx.json").write_text(json.dumps(
            {"components": [{"name": ru.BOOTLOADER_COMPONENTS[board],
                             "version": (bootloaders or CLINICAL_BL)[board]}]}))
        _sums(fw / folder)
    (fw / "firmware-lock.json").write_text(json.dumps({"firmware": {}}))
    return fw


@pytest.fixture
def clinical(tmp_path, keys):
    return ru.ReleaseBundle.load(make_clinical_firmware(tmp_path, keys),
                                 make_presets(tmp_path, keys, "clinical", name="clinical-presets"),
                                 workdir=tmp_path / "work")


def research_unit(keys, release, modules=2):
    """A research unit on other firmware and other presets than *release*."""
    return unit(keys, release, tx_body=b"tx-rc7", hv_body=b"hv-rc7", commit=OLD_COMMIT,
                modules=modules)


def test_a_clinical_release_loads(clinical):
    assert clinical.domain == "clinical"
    assert {b: i.domain for b, i in clinical.updaters.items()} == {"console": "research",
                                                                   "transmitter": "research"}
    assert clinical.bootloaders == CLINICAL_BL
    assert clinical.presets.context == "clinical"


def test_clinical_presets_not_signed_clinical_are_refused_at_load(tmp_path, keys):
    fw = make_clinical_firmware(tmp_path, keys)
    with pytest.raises(ru.ReleaseError, match="not signed with the clinical"):
        ru.ReleaseBundle.load(fw, make_presets(tmp_path, keys, "research"))


def test_a_research_unit_given_a_clinical_release_is_converted(clinical, keys):
    hv, tx = research_unit(keys, clinical)
    plan = ru.ReleaseUpdate(clinical, hv, tx).plan()
    assert plan.refusals == []
    assert [s.action for s in plan.firmware] == ["convert"] * 3
    assert all("1.1.0-rc.3-clinical replaces the research one" in s.detail
               and "RDP1" in s.detail for s in plan.firmware)
    assert {s.action for s in plan.presets} == {"write-presets-dfu"}
    assert [(s.module, s.slot) for s in plan.presets] == [(None, None)] + [
        (m, s) for m in range(2) for s in range(RELEASED)]


def test_a_stray_research_preset_is_not_cleared_on_a_converted_board(clinical, keys):
    """The updater erases every preset: nothing to clear."""
    hv, tx = research_unit(keys, clinical)
    tx.slots[1][13] = preset_package(keys, "transmitter", body=b"stray")
    plan = ru.ReleaseUpdate(clinical, hv, tx).plan()
    assert "clear-slot" not in {s.action for s in plan.presets} and not plan.refusals


def test_an_updater_without_a_clinical_bootloader_is_not_used_to_convert(tmp_path, keys):
    bundle = ru.ReleaseBundle.load(
        make_clinical_firmware(tmp_path, keys, {"console": "1.1.0-rc.3", "transmitter": "1.1.0"}),
        make_presets(tmp_path, keys, "clinical"), workdir=tmp_path / "work")
    hv, tx = research_unit(keys, bundle)
    plan = ru.ReleaseUpdate(bundle, hv, tx).plan()
    assert len([s for s in plan.refusals if s.board]) == 3
    assert all("not a clinical one" in s.detail for s in plan.refusals if s.board)


def test_a_conversion_without_the_clinical_presets_is_refused(tmp_path, keys):
    bundle = ru.ReleaseBundle.load(make_clinical_firmware(tmp_path, keys), workdir=tmp_path / "w")
    hv, tx = research_unit(keys, bundle)
    plan = ru.ReleaseUpdate(bundle, hv, tx).plan()
    assert [s.target for s in plan.refusals] == ["presets"]


def _with_clinical_presets(hv, tx, bundle):
    hv.preset = bundle.presets.console
    for m in tx.slots:
        tx.slots[m] = {s: bundle.presets.tx_image(s, m) for s in range(RELEASED)}
    return hv, tx


def test_a_clinical_unit_on_the_clinical_release_needs_nothing(clinical, keys):
    hv, tx = _with_clinical_presets(*unit(keys, clinical, domain="clinical"), clinical)
    plan = ru.ReleaseUpdate(clinical, hv, tx).plan()
    assert plan.work == [] and plan.refusals == []


def test_an_older_clinical_unit_gets_a_firmware_update(clinical, keys):
    hv, tx = _with_clinical_presets(*unit(keys, clinical, domain="clinical", tx_body=b"tx-rc7"),
                                    clinical)
    plan = ru.ReleaseUpdate(clinical, hv, tx).plan()
    assert [s.action for s in plan.firmware] == ["none", "firmware", "firmware"]
    assert plan.refusals == []


def test_clinical_presets_are_not_written_to_a_clinical_unit_yet(clinical, keys):
    hv, tx = unit(keys, clinical, domain="clinical", commit=OLD_COMMIT)
    plan = ru.ReleaseUpdate(clinical, hv, tx).plan()
    assert [s.action for s in plan.firmware] == ["none"] * 3
    assert len(plan.refusals) == 1 + 2 * RELEASED
    assert all("clinical preset-only update not supported yet" in s.detail for s in plan.refusals)


def test_a_clinical_unit_is_never_given_a_research_release(release, keys):
    hv, tx = unit(keys, release, domain="clinical")
    plan = ru.ReleaseUpdate(release, hv, tx).plan()
    assert plan.refusals and all("RMA" in s.detail for s in plan.refusals if s.board)


class _FakeDfu:
    """The bootloader DFU writers a before_app uses: they check the key they
    are given and store the package on the fake board."""

    def __init__(self, hv, tx, log):
        self.hv, self.tx, self.log = hv, tx, log

    def _check(self, package, public_key):
        if not pp.verify_preset_package(package, public_key):
            raise ValueError("Preset is not signed by the given key")

    def program_console_presets(self, package, public_key=None, **_):
        self._check(package, public_key)
        self.hv.preset = package
        self.log.append(("console page",))

    def program_transmitter_preset(self, slot, package, public_key=None, **_):
        self._check(package, public_key)
        self.tx.slots[0][slot] = package
        self.log.append(("master slot", slot))

    def program_transmitter_slave_preset_i2c(self, slot, package, i2c_addr, public_key=None, **_):
        self._check(package, public_key)
        self.tx.slots[i2c_addr - 0x40][slot] = package
        self.log.append(("slave slot", i2c_addr - 0x40, slot))


@pytest.fixture
def converting_updaters(monkeypatch):
    """Fake updaters that convert: the updater erases the board's presets,
    the before_app runs in a DfuContext over _FakeDfu, then the clinical app."""
    from openlifu_sdk.io import LIFUFirmwareUpdate as fu

    def install(release, hv, tx):
        rec = {"kwargs": {}, "log": []}
        dfu = _FakeDfu(hv, tx, rec["log"])
        app = {b: i.path.read_bytes() for b, i in release.apps.items()}

        def context(name, module):
            if module == 0:
                return fu.DfuContext(name, 0, dfu, wait_dfu=lambda: rec["log"].append("wait"))
            return fu.DfuContext(name, module, dfu, i2c_addr=0x40 + module,
                                 reacquire=lambda: rec["log"].append("reacquire") or 0x40 + module)

        def convert(name, board, module, kw):
            rec["kwargs"][name] = kw
            rec["log"].append(("updater", name))
            if board is hv:
                hv.preset = None
            else:
                tx.slots[module] = {}
            kw["before_app"](context(name, module))
            rec["log"].append(("app", name))
            board.images[module] = app["console" if board is hv else "transmitter"]
            board.domains[module] = "clinical"
            return type("R", (), {"summary": f"{name} converted"})()

        class Console:
            def __init__(self, hv=None, **_):
                pass

            def update(self, **kw):
                return convert("console", hv, 0, kw)

        class Transmitter:
            def __init__(self, tx=None, **_):
                pass

            def update_slave(self, module, **kw):
                return convert(f"transmitter module {module}", tx, module, kw)

            def update(self, **kw):
                return convert("transmitter master", tx, 0, kw)

        monkeypatch.setattr(fu, "LIFUFirmwareUpdate", Console)
        monkeypatch.setattr(fu, "LIFUTransmitterFirmwareUpdate", Transmitter)
        return rec
    return install


def test_a_conversion_replaces_the_bootloader_and_writes_the_presets_before_the_app(
        clinical, keys, converting_updaters):
    hv, tx = research_unit(keys, clinical)
    rec = converting_updaters(clinical, hv, tx)
    update = ru.ReleaseUpdate(clinical, hv, tx)
    result = update.run(update.plan())
    assert result.ok, result.lines
    for name, kw in rec["kwargs"].items():
        assert kw["replace_bootloader"] is True and kw["bl_target"] is None, name
        board = "console" if name == "console" else "transmitter"
        assert kw["bl_updater"] == str(clinical.updaters[board].path)
    slots = range(RELEASED)
    assert rec["log"] == (
        [("updater", "console"), ("console page",), "wait", ("app", "console")]
        + [("updater", "transmitter module 1")]
        + [e for s in slots for e in (("slave slot", 1, s), "reacquire")]
        + [("app", "transmitter module 1"), ("updater", "transmitter master")]
        + [e for s in slots for e in (("master slot", s), "wait")]
        + [("app", "transmitter master")])
    assert hv.log == [] and tx.log == []        # nothing through the clinical applications
    assert "[ok] transmitter module 1: user configuration unchanged" in result.lines


def test_a_conversion_checks_each_preset_against_the_clinical_key(clinical, keys,
                                                                   converting_updaters):
    """The writers are given the clinical key: a package the research key
    signed is refused before anything is staged."""
    hv, tx = research_unit(keys, clinical)
    converting_updaters(clinical, hv, tx)
    update = ru.ReleaseUpdate(clinical, hv, tx)
    plan = update.plan()
    object.__setattr__(clinical.presets, "console", preset_package(keys, "console"))
    with pytest.raises(ValueError, match="not signed by the given key"):
        update.run(plan)


# ---------------------------------------------------------------------------
# Bootloader-only update: the application is the release's, the bootloader
# (read from the application) is older than the one the updater carries
# ---------------------------------------------------------------------------

#: A release whose updaters can run on a unit on its applications (signed
#: at the application's version) and carry the final bootloader.
BL_UPDATERS = {"console": ("1.3.0", "1.1.0"), "transmitter": ("2.1.0", "1.1.0")}
RC_BOOTLOADERS = {"console": "1.1.0-rc.3", "transmitter": "1.1.0-rc.2"}


@pytest.fixture
def bl_release(tmp_path, keys):
    return ru.ReleaseBundle.load(make_firmware(tmp_path, keys, updaters=BL_UPDATERS),
                                 make_presets(tmp_path, keys), workdir=tmp_path / "work")


def on_bootloaders(hv, tx, console=RC_BOOTLOADERS["console"],
                   transmitter=RC_BOOTLOADERS["transmitter"]):
    """What the unit's applications report for their bootloader."""
    hv.bootloaders = [console]
    tx.bootloaders = [transmitter] * len(tx.images)
    return hv, tx


def test_the_plan_records_each_boards_bootloader(bl_release, keys):
    hv, tx = on_bootloaders(*unit(keys, bl_release))
    tx.bootloaders[1] = None
    plan = ru.ReleaseUpdate(bl_release, hv, tx).plan()
    assert [b.bootloader for b in plan.boards] == ["1.1.0-rc.3", "1.1.0-rc.2", None]


def test_an_older_bootloader_under_the_release_app_is_replaced(bl_release, keys):
    hv, tx = on_bootloaders(*unit(keys, bl_release))
    plan = ru.ReleaseUpdate(bl_release, hv, tx).plan()
    assert _actions(plan.firmware) == [("console", "bootloader"),
                                       ("transmitter master", "bootloader"),
                                       ("transmitter module 1", "bootloader")]
    assert "bootloader 1.1.0-rc.3 -> 1.1.0" in plan.firmware[0].detail
    assert "same application 1.3.0" in plan.firmware[0].detail
    assert "bootloader 1.1.0-rc.2 -> 1.1.0" in plan.firmware[1].detail
    assert plan.refusals == [] and len(plan.work) == 3
    # The presets are the release's at plan time: rewritten only after the run.
    assert {s.action for s in plan.presets} == {"none"}


@pytest.mark.parametrize("reported", ["1.1.0", "1.1.0+g1234abc", "1.1.1", "1.2.0-rc.1"])
def test_an_equal_or_newer_bootloader_is_kept(bl_release, keys, reported):
    hv, tx = on_bootloaders(*unit(keys, bl_release), reported, reported)
    plan = ru.ReleaseUpdate(bl_release, hv, tx).plan()
    assert plan.work == []


def test_an_unknown_bootloader_is_kept(bl_release, keys):
    """Firmware from before OW_CMD_BL_VERSION: as before, nothing to do."""
    hv, tx = unit(keys, bl_release)
    plan = ru.ReleaseUpdate(bl_release, hv, tx).plan()
    assert plan.work == []
    assert all(s.detail.startswith("already runs the release build") and "kept" not in s.detail
               for s in plan.firmware)


@pytest.mark.parametrize("reported", ["1.1.0-rc.1-dirty", "1.0.1-3-gb241b0f-dirty",
                                      "1.1.0-rc.3-4-g0123abc", "garbage", ""])
def test_a_development_bootloader_is_treated_as_unknown(bl_release, keys, reported):
    """Untagged / dev bootloaders report git-describe text: not ordered
    against a release, so no bootloader step (and no crash)."""
    hv, tx = on_bootloaders(*unit(keys, bl_release), reported, reported)
    plan = ru.ReleaseUpdate(bl_release, hv, tx).plan()
    assert plan.work == []
    assert all("kept" not in s.detail for s in plan.firmware)


@pytest.mark.parametrize("version, release", [
    ("1.1.0", True), ("1.1.0-rc.3", True), ("1.1.0-rc.3-clinical", True),
    ("1.1.0+g1234", True), ("1.0.1", True), (None, False), ("1.1.0-rc.1-dirty", False),
    ("1.0.1-3-gb241b0f-dirty", False), ("1.1", False)])
def test_release_bootloader_versions(version, release):
    assert ru.is_release_bootloader(version) is release


def test_an_updater_signed_below_the_installed_app_keeps_the_bootloader(release, keys):
    """The default test release's updaters (1.2.31 / 2.0.31) would be refused."""
    hv, tx = on_bootloaders(*unit(keys, release))
    plan = ru.ReleaseUpdate(release, hv, tx).plan()
    assert plan.work == []
    assert "its bootloader 1.1.0-rc.3 is kept" not in plan.firmware[0].detail  # same version
    hv.bootloaders = ["1.0.3"]
    plan = ru.ReleaseUpdate(release, hv, tx).plan()
    assert plan.work == []
    assert "its bootloader 1.0.3 is kept" in plan.firmware[0].detail
    assert "signed 1.2.31, below the installed 1.3.0" in plan.firmware[0].detail


def test_without_presets_to_rewrite_the_bootloader_is_kept(bl_release, keys, tmp_path):
    """The updater erases the presets: a firmware-only release would leave
    the unit without them."""
    firmware_only = ru.ReleaseBundle.load(tmp_path / "firmware", workdir=tmp_path / "work2")
    hv, tx = on_bootloaders(*unit(keys, bl_release))
    plan = ru.ReleaseUpdate(firmware_only, hv, tx).plan()
    assert plan.work == []
    assert all("no presets to rewrite" in s.detail for s in plan.firmware)


def test_a_clinical_unit_keeps_its_bootloader(clinical, keys):
    """A clinical bootloader refuses the research-signed updater."""
    hv, tx = _with_clinical_presets(*unit(keys, clinical, domain="clinical"), clinical)
    on_bootloaders(hv, tx, "1.1.0-rc.2-clinical", "1.1.0-rc.2-clinical")
    plan = ru.ReleaseUpdate(clinical, hv, tx).plan()
    assert plan.work == [] and plan.refusals == []
    assert all("its bootloader 1.1.0-rc.2-clinical is kept" in s.detail
               and "clinical bootloader takes no research-signed" in s.detail
               for s in plan.firmware)


def test_mixed_firmware_and_bootloader_steps(bl_release, keys):
    hv, tx = on_bootloaders(*unit(keys, bl_release, modules=3))
    tx.images[2] = app_image(keys, "transmitter", "research", "2.1.0", b"tx-rc7")
    tx.bootloaders[2] = None                      # rc.7 cannot report it
    plan = ru.ReleaseUpdate(bl_release, hv, tx).plan()
    assert _actions(plan.firmware) == [("console", "bootloader"),
                                       ("transmitter master", "bootloader"),
                                       ("transmitter module 1", "bootloader"),
                                       ("transmitter module 2", "firmware")]


class _BootloaderUpdaters:
    """Stands in for the firmware updaters: in DFU, a bootloader older than
    *bl_target* is replaced and the presets are erased (as the updater does),
    then the given application is installed."""

    def __init__(self, hv, tx, replaces=True):
        from openlifu_sdk.io.LIFUFirmwareUpdate import bootloader_needs_migration

        self.calls = []
        rec = self

        def install(name, board, module, kw):
            rec.calls.append((name, kw))
            if replaces and bootloader_needs_migration(board.bootloaders[module], kw["bl_target"]):
                board.bootloaders[module] = kw["bl_target"]
                if board is hv:
                    hv.preset = None
                else:
                    tx.slots[module] = {}
            with open(kw["signed_app"], "rb") as f:
                board.images[module] = f.read()
            return type("R", (), {"summary": f"{name} done"})()

        class Console:
            def __init__(self, hv=None, **_):
                pass

            def update(self, **kw):
                return install("console", hv, 0, kw)

        class Transmitter:
            def __init__(self, tx=None, **_):
                pass

            def update_slave(self, module, **kw):
                return install(f"transmitter module {module}", tx, module, kw)

            def update(self, **kw):
                return install("transmitter master", tx, 0, kw)

        self.Console, self.Transmitter = Console, Transmitter


@pytest.fixture
def bl_updaters(monkeypatch):
    from openlifu_sdk.io import LIFUFirmwareUpdate as fu

    def install(hv, tx, **kw):
        fakes = _BootloaderUpdaters(hv, tx, **kw)
        monkeypatch.setattr(fu, "LIFUFirmwareUpdate", fakes.Console)
        monkeypatch.setattr(fu, "LIFUTransmitterFirmwareUpdate", fakes.Transmitter)
        return fakes
    return install


def test_a_bootloader_step_runs_like_a_firmware_step(bl_release, keys, bl_updaters):
    """Same path, same order: the release's bootloader as the target and the
    same signed application, console, then the slaves, then the master."""
    hv, tx = on_bootloaders(*unit(keys, bl_release, modules=3))
    fakes = bl_updaters(hv, tx)
    seen = []
    update = ru.ReleaseUpdate(bl_release, hv, tx, report=seen.append)
    update.run(update.plan())
    assert [name for name, _ in fakes.calls] == ["console", "transmitter module 1",
                                                 "transmitter module 2", "transmitter master"]
    for name, kw in fakes.calls:
        board = "console" if name == "console" else "transmitter"
        assert kw["bl_target"] == "1.1.0", name
        assert kw["signed_app"] == str(bl_release.apps[board].path), name
        assert kw["bl_updater"] == str(bl_release.updaters[board].path), name
        assert not kw.get("replace_bootloader") and "before_app" not in kw, name
    assert "console: replacing the bootloader (the same application is re-installed)" in seen


def test_the_presets_erased_by_a_bootloader_step_are_rewritten_and_read_back(
        bl_release, keys, bl_updaters):
    hv, tx = on_bootloaders(*unit(keys, bl_release))
    bl_updaters(hv, tx)
    update = ru.ReleaseUpdate(bl_release, hv, tx)
    plan = update.plan()
    assert {s.action for s in plan.presets} == {"none"}     # correct before the run
    result = update.run(plan)
    assert result.ok, result.lines
    assert hv.log == ["write console"]
    assert sorted(e for e in tx.log if e[0] == "write") == sorted(
        ("write", m, s) for m in range(2) for s in range(RELEASED))
    assert hv.preset == bl_release.presets.console
    assert all(tx.slots[m][s] == bl_release.presets.tx_image(s, m)
               for m in range(2) for s in range(RELEASED))
    for name in ("console", "transmitter master", "transmitter module 1"):
        assert f"[ok] {name}: bootloader 1.1.0" in result.lines
    assert f"[ok] console page: holds the release's package ({COMMIT[:12]})" in result.lines


def test_a_bootloader_that_was_not_replaced_fails_the_read_back(bl_release, keys, bl_updaters):
    hv, tx = on_bootloaders(*unit(keys, bl_release))
    bl_updaters(hv, tx, replaces=False)
    update = ru.ReleaseUpdate(bl_release, hv, tx)
    result = update.run(update.plan())
    assert not result.ok
    assert "[FAIL] console: bootloader 1.1.0-rc.3, expected 1.1.0" in result.lines
    assert "[FAIL] transmitter module 1: bootloader 1.1.0-rc.2, expected 1.1.0" in result.lines


def test_a_mixed_run_updates_both_and_restores_the_presets(bl_release, keys, bl_updaters):
    """The console only needs its bootloader; the transmitter needs its
    application (and its bootloader is compared in DFU, as before)."""
    hv, tx = on_bootloaders(*unit(keys, bl_release, tx_body=b"tx-rc7"), transmitter="1.1.0-rc.2")
    tx.bootloaders = [None, None]                 # rc.7 cannot report it
    fakes = bl_updaters(hv, tx)
    update = ru.ReleaseUpdate(bl_release, hv, tx)
    plan = update.plan()
    assert [s.action for s in plan.firmware] == ["bootloader", "firmware", "firmware"]
    result = update.run(plan)
    assert result.ok, result.lines
    assert [name for name, _ in fakes.calls] == ["console", "transmitter module 1",
                                                 "transmitter master"]
    assert hv.log == ["write console"]           # erased by the console's bootloader step
    assert "[ok] console: bootloader 1.1.0" in result.lines
    assert not any("transmitter master: bootloader" in line for line in result.lines)
