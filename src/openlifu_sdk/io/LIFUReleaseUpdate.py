# SPDX-License-Identifier: Apache-2.0
"""Take a unit to a release: firmware and presets, in one call.

A release is what the diathermy release build publishes:

- ``firmware-<tag>.zip``: one folder per firmware repo (the console and
  transmitter applications, the two bootloader updaters), each listed in
  ``SHA256SUMS``, and ``firmware-lock.json``;
- ``research-presets-<tag>.zip`` (or the clinical set): the signed preset
  packages (``build_presets.py``) with ``presets-manifest.json``.

:class:`ReleaseBundle` loads them (a zip or an unpacked folder), checks every
sum and every signature against the SDK's trusted keys, and names the
release's domain: research or clinical, from the key that signed both
applications.

:class:`ReleaseUpdate` then works in three steps:

1. :meth:`~ReleaseUpdate.plan` reads the unit without entering DFU: every
   board's firmware version, signing domain and signed header
   (``firmware_header``), and every preset package header. A board whose
   installed build is the release's (same FwVersion and body hash) is left
   alone; an older one is updated; a newer one, or one in another domain, is
   refused before anything is written.
2. :meth:`~ReleaseUpdate.run` updates the firmware (console, then the
   transmitter slaves, then the master, through
   :mod:`openlifu_sdk.io.LIFUFirmwareUpdate`, which replaces a 1.0.x
   bootloader with the updater on the way), waits for each application to
   come back, then re-reads the presets and writes the ones that differ to
   every physical module, and empties the transmitter slots past the
   released set.
3. Everything is read back and compared with the release.

This version handles research releases on research units. Converting a
research unit to clinical, and clinical presets (written through the
bootloaders), are refused with a message; a clinical unit is never given a
research release.

CLI::

    python -m openlifu_sdk.io.LIFUReleaseUpdate --firmware firmware-v0.2.0-rc8.zip \\
        --presets research-presets-v0.2.0-rc8.zip [--plan] [-y]
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from openlifu_sdk.io import LIFUCrypto
from openlifu_sdk.io import LIFUPresetPackage as pp
from openlifu_sdk.io.LIFUPresetPackage import TX_PRESET_SLOTS

logger = logging.getLogger(__name__)

#: The bundle folder of each board's application and bootloader updater.
APP_DIRS = {"console": "openlifu-console-fw", "transmitter": "openlifu-transmitter-fw"}
UPDATER_DIRS = {"console": "open-lifu-console-bl-updater",
                "transmitter": "open-lifu-transmitter-bl-updater"}
#: The bootloader each updater carries, as its SBOM names it.
BOOTLOADER_COMPONENTS = {"console": "open-lifu-console-bl",
                         "transmitter": "open-lifu-transmitter-bl"}
DOMAINS = ("clinical", "research")
#: Firmware below these versions predates the signature-check command, so
#: firmware_domain() answers with an error. It runs a 1.0.x bootloader, which
#: was only ever built with the research keys (the clinical domain starts at
#: bootloader 1.1.0), and the release update replaces that bootloader first.
LEGACY_BELOW = {"console": (1, 3, 0), "transmitter": (2, 1, 0)}
#: How long an application may take to answer again after its update (a
#: bootloader replacement included; the VM may re-attach USB).
APP_RETURN_TIMEOUT_S = 120.0
APP_POLL_S = 3.0
#: Console 12 V rail off time when power-cycling a stuck transmitter chain.
POWER_CYCLE_OFF_S = 5.0


class ReleaseError(RuntimeError):
    """The release cannot be loaded, or cannot be installed on this unit."""


# ---------------------------------------------------------------------------
# The release bundle
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SignedImage:
    """A signed SBSFU image from the release, and the trusted key that
    signed it."""

    path: Path
    board: str
    header: LIFUCrypto.FirmwareHeader
    domain: str

    @property
    def version(self) -> str:
        return self.header.fw_version_str

    @property
    def build(self) -> str:
        """Short name of the build: the start of its body hash."""
        return self.header.fw_tag.hex()[:12]

    def is_installed(self, installed: LIFUCrypto.FirmwareHeader | None) -> bool:
        """True if *installed* (a unit's ``firmware_header``) is this build."""
        return (installed is not None and installed.fw_version == self.header.fw_version
                and installed.fw_tag == self.header.fw_tag)


@dataclass(frozen=True)
class PresetRelease:
    """The released preset packages: the console page and each transmitter
    slot's image (per module when the manifest has more than one)."""

    root: Path
    context: str
    commit: str
    ids: tuple[str, ...]
    console: bytes
    #: slot -> per-module slot images (one entry: written to every module).
    transmitter: dict[int, tuple[bytes, ...]]

    def tx_image(self, slot: int, module: int) -> bytes:
        images = self.transmitter[slot]
        return images[module] if len(images) > 1 else images[0]

    @property
    def modules(self) -> int:
        """How many modules the set is for; 1 means any number."""
        return max(len(v) for v in self.transmitter.values()) if self.transmitter else 1


@dataclass(frozen=True)
class ReleaseBundle:
    """A verified release: the applications, the bootloader updaters, the
    bootloader versions they carry, and optionally the presets."""

    domain: str
    apps: dict[str, SignedImage]
    updaters: dict[str, SignedImage]
    bootloaders: dict[str, str | None]
    lock: dict
    presets: PresetRelease | None = None

    @classmethod
    def load(cls, firmware: str | Path, presets: str | Path | None = None,
             workdir: str | Path | None = None) -> ReleaseBundle:
        """Load and verify a release (each argument a zip or a folder).

        Raises:
            ReleaseError: A sum does not match, an image is not signed by a
                trusted key, the applications are in different domains, or
                the presets are not the release's domain.
        """
        work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="lifu-release-"))
        fw_root = _find_root(_unpack(Path(firmware), work / "firmware"), "firmware-lock.json")
        verify_sums(fw_root)
        apps = {b: _signed_image(_one(fw_root / d, "*-signed.bin"), b) for b, d in APP_DIRS.items()}
        domains = {img.domain for img in apps.values()}
        if len(domains) != 1:
            raise ReleaseError(f"the applications are signed in different domains: "
                               f"{ {b: i.domain for b, i in apps.items()} }")
        domain = domains.pop()
        updaters = {b: _signed_image(_one(fw_root / d, "*-signed.bin"), b)
                    for b, d in UPDATER_DIRS.items()}
        bootloaders = {b: _sbom_component(_one(fw_root / d, "*.cdx.json"), BOOTLOADER_COMPONENTS[b])
                       for b, d in UPDATER_DIRS.items()}
        lock = json.loads((fw_root / "firmware-lock.json").read_text(encoding="utf-8"))
        preset_release = None
        if presets is not None:
            preset_release = _load_presets(
                _find_root(_unpack(Path(presets), work / "presets"), "presets-manifest.json"), domain)
        return cls(domain=domain, apps=apps, updaters=updaters, bootloaders=bootloaders,
                   lock=lock, presets=preset_release)

    def describe(self) -> list[str]:
        lines = [f"Release domain: {self.domain}"]
        for board, img in self.apps.items():
            lines.append(f"  {board} application {img.version} (build {img.build})")
        for board, img in self.updaters.items():
            lines.append(f"  {board} bootloader updater {img.version}, carries bootloader "
                         f"{self.bootloaders.get(board) or 'unknown'}")
        if self.presets:
            p = self.presets
            lines.append(f"  presets: {len(p.ids)} ({p.context}, diathermy {p.commit[:12]})")
        return lines


def _unpack(path: Path, dest: Path) -> Path:
    if path.is_dir():
        return path
    if not zipfile.is_zipfile(path):
        raise ReleaseError(f"{path}: neither a folder nor a zip file")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            target = (dest / name).resolve()
            if not target.is_relative_to(dest.resolve()):
                raise ReleaseError(f"{path}: entry {name!r} escapes the archive")
        z.extractall(dest)
    return dest


def _find_root(folder: Path, marker: str) -> Path:
    found = sorted(folder.rglob(marker))
    if len(found) != 1:
        raise ReleaseError(f"{folder}: expected one {marker}, found {len(found)}")
    return found[0].parent


def _one(folder: Path, pattern: str) -> Path:
    found = sorted(folder.glob(pattern))
    if len(found) != 1:
        raise ReleaseError(f"{folder}: expected one {pattern}, found {len(found)}")
    return found[0]


def verify_sums(root: Path) -> int:
    """Check every ``SHA256SUMS`` under *root*. Returns the number of files
    checked.

    Raises:
        ReleaseError: A listed file is missing or does not match, or a
            ``SHA256SUMS`` names a path outside its folder.
    """
    checked = 0
    for sums in sorted(root.rglob("SHA256SUMS")):
        for line in sums.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            digest, name = line.split(maxsplit=1)
            target = (sums.parent / name.lstrip("*")).resolve()
            if not target.is_relative_to(sums.parent.resolve()):
                raise ReleaseError(f"{sums}: {name!r} is outside its folder")
            if not target.is_file():
                raise ReleaseError(f"{sums}: {name} is missing")
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest.lower():
                raise ReleaseError(f"{sums}: {name} does not match its SHA-256")
            checked += 1
    if not checked:
        raise ReleaseError(f"{root}: no SHA256SUMS")
    return checked


def _signed_image(path: Path, board: str) -> SignedImage:
    data = path.read_bytes()
    header = LIFUCrypto.FirmwareHeader.from_bytes(data)
    if header.magic != LIFUCrypto.SFU_MAGIC:
        raise ReleaseError(f"{path.name}: not a signed image")
    for domain in DOMAINS:
        if LIFUCrypto.verify_header_signature(data, LIFUCrypto.trusted_public_key(board, domain)):
            return SignedImage(path=path, board=board, header=header, domain=domain)
    raise ReleaseError(f"{path.name}: not signed by a trusted {board} key")


def _sbom_component(path: Path, name: str) -> str | None:
    sbom = json.loads(path.read_text(encoding="utf-8"))
    for component in sbom.get("components", []):
        if component.get("name") == name:
            return component.get("version")
    return None


def _load_presets(root: Path, domain: str) -> PresetRelease:
    verify_sums(root)
    manifest = json.loads((root / "presets-manifest.json").read_text(encoding="utf-8"))
    context, commit = manifest["context"], manifest["diathermy_sha"]
    console = (root / "console-presets.bin").read_bytes()
    _check_package(console, "console", domain, context, commit, "console-presets.bin")
    transmitter = {}
    for entry in manifest["presets"]:
        slot = int(entry["slot"])
        names = [f"transmitter-slot{slot:02d}-module{m}.bin" for m in range(int(entry["modules"]))]
        images = tuple((root / n).read_bytes() for n in names)
        for name, image in zip(names, images):
            _check_package(image, "transmitter", domain, context, commit, name)
        transmitter[slot] = images
    if sorted(transmitter) != list(range(len(transmitter))):
        raise ReleaseError(f"{root}: preset slots are not 0..{len(transmitter) - 1}")
    return PresetRelease(root=root, context=context, commit=commit,
                         ids=tuple(e["id"] for e in manifest["presets"]),
                         console=console, transmitter=transmitter)


def _check_package(data: bytes, board: str, domain: str, context: str, commit: str,
                   name: str) -> None:
    header = pp.parse_preset_header(data)
    if not pp.verify_preset_package(data, LIFUCrypto.trusted_public_key(board, domain)):
        raise ReleaseError(f"{name}: not signed with the {domain} {board} key")
    if header.context != context or header.diathermy_sha != commit:
        raise ReleaseError(f"{name}: context {header.context} / diathermy "
                           f"{header.diathermy_sha[:12]}, the manifest says {context} / {commit[:12]}")


# ---------------------------------------------------------------------------
# The unit
# ---------------------------------------------------------------------------

@dataclass
class Board:
    """One board's firmware as the unit reports it: the console, or one
    transmitter module."""

    name: str
    board: str
    module: int
    version: str
    domain: str
    header: LIFUCrypto.FirmwareHeader | None


@dataclass
class PresetState:
    """What each preset store holds: a header, None (empty) or an error."""

    console: Any
    #: module -> slot -> header / None / Exception
    transmitter: dict[int, dict[int, Any]]


def read_boards(hv: Any, tx: Any) -> list[Board]:
    """The console, then every transmitter module. Reads only, no DFU."""
    boards = [_read_board("console", "console", hv, 0)]
    for module in range(tx.get_module_count()):
        name = "transmitter master" if module == 0 else f"transmitter module {module}"
        boards.append(_read_board(name, "transmitter", tx, module))
    return boards


def _read_board(name: str, board: str, device: Any, module: int) -> Board:
    return Board(name=name, board=board, module=module,
                 version=str(device.get_version(module=module)),
                 domain=device.firmware_domain(module).value,
                 header=device.firmware_header(module))


def read_presets(hv: Any, tx: Any, modules: int) -> PresetState:
    return PresetState(
        console=_try(hv.get_preset_info),
        transmitter={m: {s: _try(lambda s=s, m=m: tx.get_preset_info(s, module=m))
                         for s in range(TX_PRESET_SLOTS)} for m in range(modules)})


def _try(read: Callable[[], Any]) -> Any:
    try:
        return read()
    except Exception as e:  # noqa: BLE001 - recorded; the plan treats it as unknown
        return e


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

@dataclass
class Step:
    """One thing the update does (or found already done)."""

    target: str
    action: str          # "firmware" | "write-presets" | "clear-slot" | "none" | "refuse"
    detail: str
    board: Board | None = None
    slot: int | None = None
    module: int | None = None


@dataclass
class ReleasePlan:
    bundle: ReleaseBundle
    boards: list[Board]
    firmware: list[Step] = field(default_factory=list)
    presets: list[Step] = field(default_factory=list)

    @property
    def refusals(self) -> list[Step]:
        return [s for s in self.firmware + self.presets if s.action == "refuse"]

    @property
    def work(self) -> list[Step]:
        return [s for s in self.firmware + self.presets if s.action not in ("none", "refuse")]

    def describe(self) -> list[str]:
        lines = [f"{s.target}: {s.action} - {s.detail}" for s in self.firmware]
        lines += [f"{s.target}: {s.action} - {s.detail}" for s in self.presets]
        return lines


def _version_tuple(text: str) -> tuple[int, int, int] | None:
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def installed_fw_version(board: Board) -> int | None:
    """The board's FwVersion: from its signed header, or from the version it
    reports (a field unit from before the signature check)."""
    if board.header is not None:
        return board.header.fw_version
    v = _version_tuple(board.version)
    return (v[0] << 11) | (v[1] << 5) | v[2] if v else None


def is_legacy(board: Board) -> bool:
    """True for firmware from before the signature check (see
    :data:`LEGACY_BELOW`): it cannot say which key signed it."""
    version = _version_tuple(board.version)
    return (board.domain == "error" and version is not None
            and version < LEGACY_BELOW[board.board])


def _legacy_step(board: Board, image: SignedImage, domain: str) -> Step:
    """A field unit from before the signature check: research by
    construction. Its bootloader still checks every image it is given, so a
    wrong guess is refused in DFU and the unit is returned to its app."""
    if "fda" in board.version.lower():
        return Step(board.name, "refuse", f"runs {board.version}, an FDA build that refuses DFU "
                    "from its application; nothing will be written", board)
    if domain != "research":
        return Step(board.name, "refuse", f"runs {board.version} (research, from before the "
                    "signature check): converting a research unit to clinical is not supported "
                    "by this release update yet", board)
    return Step(board.name, "firmware", f"{board.version} (research, from before the signature "
                f"check) -> {image.version} ({image.build}); its 1.0.x bootloader is replaced "
                "first in DFU", board)


def firmware_step(board: Board, image: SignedImage, domain: str) -> Step:
    """What *board* needs to run the release's *image*."""
    if is_legacy(board):
        return _legacy_step(board, image, domain)
    if board.domain != domain:
        return Step(board.name, "refuse", _domain_refusal(board, domain), board)
    if board.header is None:
        return Step(board.name, "refuse", f"runs {board.version} with no signed header to "
                    "compare; nothing will be written", board)
    if image.is_installed(board.header):
        return Step(board.name, "none", f"already runs the release build {image.version} "
                    f"({image.build})", board)
    if image.header.fw_version < board.header.fw_version:
        return Step(board.name, "refuse", f"runs {board.version}, newer than the release's "
                    f"{image.version}: a downgrade is refused before DFU", board)
    return Step(board.name, "firmware", f"{board.version} -> {image.version} ({image.build}); "
                "an old bootloader is replaced first in DFU", board)


def _domain_refusal(board: Board, domain: str) -> str:
    if board.domain == "clinical":
        return ("runs clinical firmware: a research release is never installed on a clinical "
                "unit (return it through an RMA)")
    if board.domain == "research" and domain == "clinical":
        return ("runs research firmware: converting a research unit to clinical is not "
                "supported by this release update yet")
    return (f"cannot tell which key signed its firmware ({board.domain}); nothing will be "
            "written")


def preset_steps(state: PresetState, presets: PresetRelease, domain: str) -> list[Step]:
    """The preset writes and slot clears that take *state* to *presets*."""
    if domain != "research":
        return [Step("presets", "refuse", "clinical presets are written through the "
                     "bootloaders; not supported by this release update yet")]
    released = len(presets.transmitter)
    modules = len(state.transmitter)
    if presets.modules > 1 and presets.modules != modules:
        return [Step("presets", "refuse", f"the presets are for {presets.modules} modules, "
                     f"the transmitter has {modules}")]
    steps = [_package_step("console page", state.console, presets.console, None, None)]
    for m in range(modules):
        for slot in range(TX_PRESET_SLOTS):
            have = state.transmitter[m][slot]
            target = f"transmitter module {m} slot {slot:02d}"
            if slot < released:
                steps.append(_package_step(target, have, presets.tx_image(slot, m), slot, m))
            elif have is not None:
                steps.append(Step(target, "clear-slot", "holds a preset outside the release"
                                  if not isinstance(have, Exception) else f"unreadable ({have})",
                                  slot=slot, module=m))
    return steps


def _package_step(target: str, have: Any, want: bytes, slot: int | None,
                  module: int | None) -> Step:
    want_header = pp.parse_preset_header(want)
    if _same_package(have, want_header):
        return Step(target, "none", f"holds the release's package ({want_header.diathermy_sha[:12]})",
                    slot=slot, module=module)
    if have is None:
        why = "empty"
    elif isinstance(have, Exception):
        why = f"unreadable ({have})"
    else:
        why = f"holds {have.context} / diathermy {have.diathermy_sha[:12]}"
    return Step(target, "write-presets", f"{why} -> {want_header.context} / diathermy "
                f"{want_header.diathermy_sha[:12]}", slot=slot, module=module)


def _same_package(have: Any, want: pp.PresetHeader) -> bool:
    """Same signed header: same body hash, commit, context and signature."""
    signed = LIFUCrypto.HEADER_AUTH_LEN + LIFUCrypto.HEADER_SIGN_LEN
    return isinstance(have, pp.PresetHeader) and have.header[:signed] == want.header[:signed]


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

@dataclass
class ReleaseResult:
    ok: bool
    lines: list[str]


class ReleaseUpdate:
    """Plan and run the update of one unit (console + transmitter chain) to
    a :class:`ReleaseBundle`.

    Args:
        bundle: The verified release.
        hv: The connected console (``interface.hvcontroller``).
        tx: The connected transmitter master (``interface.txdevice``).
        progress_callback: Passed to the DFU writes (written, total, label).
        report: Called with one line per step as it happens.
    """

    def __init__(self, bundle: ReleaseBundle, hv: Any, tx: Any, *,
                 progress_callback: Callable | None = None,
                 report: Callable[[str], None] | None = None):
        self.bundle = bundle
        self.hv = hv
        self.tx = tx
        self.progress_callback = progress_callback
        self.report = report or logger.info

    # -- plan ---------------------------------------------------------------

    def plan(self) -> ReleasePlan:
        """Read the unit (no DFU) and decide every step."""
        boards = read_boards(self.hv, self.tx)
        plan = ReleasePlan(self.bundle, boards)
        plan.firmware = [firmware_step(b, self.bundle.apps[b.board], self.bundle.domain)
                         for b in boards]
        if self.bundle.presets is not None:
            state = read_presets(self.hv, self.tx, len(boards) - 1)
            plan.presets = preset_steps(state, self.bundle.presets, self.bundle.domain)
        return plan

    # -- run ----------------------------------------------------------------

    def run(self, plan: ReleasePlan) -> ReleaseResult:
        """Run *plan*: firmware, then presets (re-planned from a fresh read,
        since a bootloader replacement erases them), then the read-back.

        Raises:
            ReleaseError: The plan has refusals (nothing is written), or a
                step failed (the message says where each board was left).
        """
        if plan.refusals:
            raise ReleaseError("refused before anything was written:\n  "
                               + "\n  ".join(f"{s.target}: {s.detail}" for s in plan.refusals))
        self._run_firmware([s for s in plan.firmware if s.action == "firmware"])
        if self.bundle.presets is not None:
            modules = self.tx.get_module_count()
            state = read_presets(self.hv, self.tx, modules)
            steps = preset_steps(state, self.bundle.presets, self.bundle.domain)
            self._run_presets([s for s in steps if s.action in ("write-presets", "clear-slot")])
        return self.verify()

    def _run_firmware(self, steps: list[Step]) -> None:
        from openlifu_sdk.io.LIFUFirmwareUpdate import (
            LIFUFirmwareUpdate,
            LIFUTransmitterFirmwareUpdate,
        )

        console = [s for s in steps if s.board.board == "console"]
        slaves = sorted((s for s in steps if s.board.board == "transmitter" and s.board.module > 0),
                        key=lambda s: s.board.module)
        master = [s for s in steps if s.board.board == "transmitter" and s.board.module == 0]
        kw = {"progress_callback": self.progress_callback}
        if console:
            self.report("console: updating the firmware")
            r = LIFUFirmwareUpdate(hv=self.hv).update(
                signed_app=str(self.bundle.apps["console"].path),
                bl_updater=str(self.bundle.updaters["console"].path),
                bl_target=self._bl_target(console[0].board), **kw)
            self.report(f"console: {r.summary}")
            self._wait_app(self.hv, "console")
            self._transmitter_back_after_console()
        fw = LIFUTransmitterFirmwareUpdate(tx=self.tx)
        app = str(self.bundle.apps["transmitter"].path)
        updater = str(self.bundle.updaters["transmitter"].path)
        for step in slaves:
            self.report(f"{step.target}: updating the firmware over I2C")
            self._guard(step.target, lambda m=step.board.module, b=step.board: fw.update_slave(
                m, signed_app=app, bl_updater=updater, bl_target=self._bl_target(b), **kw))
        if master:
            self.report("transmitter master: updating the firmware over USB")
            self._guard("transmitter master", lambda: fw.update(
                signed_app=app, bl_updater=updater,
                bl_target=self._bl_target(master[0].board), **kw))
            self._wait_app(self.tx, "transmitter")

    def _bl_target(self, board: Board) -> str | None:
        """The bootloader the release's updater carries, if the updater can
        run on *board*: a bootloader refuses an image signed below the
        installed application, so an updater signed below it is not tried
        (the board keeps its bootloader; a 1.0.x one is still replaced, as
        the updater is always above a 1.0.x unit's application)."""
        target = self.bundle.bootloaders.get(board.board)
        updater = self.bundle.updaters[board.board]
        installed = installed_fw_version(board)
        if target and installed is not None and updater.header.fw_version < installed:
            self.report(f"{board.name}: keeps its bootloader: the release's bootloader "
                        f"updater is signed {updater.version}, below the installed "
                        f"{board.version} (a 1.1.x bootloader would refuse it)")
            return None
        return target

    def _transmitter_back_after_console(self) -> None:
        """The console powers the transmitter chain (12 V rail); after the
        console rebooted, make sure the rail is on and the master answers."""
        if self._app_answers(self.tx, 15.0):
            return
        self.report("transmitter: no answer after the console update; turning on 12 V")
        self.hv.turn_12v_on()
        self._wait_app(self.tx, "transmitter")

    def _guard(self, target: str, update: Callable[[], Any]) -> None:
        """Run one transmitter update; on failure bring the chain back to
        its applications before reporting it."""
        try:
            result = update()
        except Exception as e:
            self.report(f"{target}: FAILED ({e}); returning the transmitter to its application")
            where = self._recover_transmitter()
            raise ReleaseError(f"{target}: {e}\nAfter recovery: {where}") from e
        self.report(f"{target}: {result.summary}")

    def _recover_transmitter(self) -> str:
        """Best effort: leave the master's DFU, then, if its application does
        not answer, power-cycle the chain through the console's 12 V rail."""
        from openlifu_sdk.io.LIFUDFU import TRANSMITTER_PROFILE, LIFUDFUManager

        try:
            LIFUDFUManager().abort_dfu(profile=TRANSMITTER_PROFILE)
        except Exception as e:  # noqa: BLE001 - best effort
            logger.info("abort_dfu: %s", e)
        if self._app_answers(self.tx, 30.0):
            return f"transmitter application answering ({self.tx.get_version()})"
        self.report("transmitter: no answer; power-cycling the chain (console 12 V)")
        try:
            self.hv.turn_12v_off()
            time.sleep(POWER_CYCLE_OFF_S)
            self.hv.turn_12v_on()
        except Exception as e:  # noqa: BLE001 - reported below
            return f"power-cycle failed ({e}); the transmitter does not answer"
        if self._app_answers(self.tx, APP_RETURN_TIMEOUT_S):
            return f"transmitter application answering after a power-cycle ({self.tx.get_version()})"
        return "the transmitter does not answer: check it is not in DFU, then power-cycle it"

    def _wait_app(self, device: Any, what: str) -> None:
        if not self._app_answers(device, APP_RETURN_TIMEOUT_S):
            raise ReleaseError(f"{what}: the application did not answer within "
                               f"{APP_RETURN_TIMEOUT_S:.0f} s after its update")
        self.report(f"{what}: application answering ({device.get_version()})")

    @staticmethod
    def _app_answers(device: Any, timeout_s: float) -> bool:
        from openlifu_sdk.io.LIFUFirmwareUpdate import _is_real_version, _reopen

        deadline = time.monotonic() + timeout_s
        while True:
            try:
                _reopen(device)
                if _is_real_version(str(device.get_version())):
                    return True
            except Exception as e:  # noqa: BLE001 - keep polling until the deadline
                logger.info("waiting for the application: %s", e)
            if time.monotonic() >= deadline:
                return False
            time.sleep(APP_POLL_S)

    def _run_presets(self, steps: list[Step]) -> None:
        presets = self.bundle.presets
        for step in steps:
            if step.module is None:
                self.hv.write_presets(presets.console)
            elif step.action == "write-presets":
                self.tx.write_preset(step.slot, presets.tx_image(step.slot, step.module),
                                     module=step.module)
            elif self.tx.get_preset_info(step.slot, module=step.module) is not None:
                # A slot freed by clearing the one before it reads empty.
                self.tx.delete_preset(step.slot, module=step.module)
            self.report(f"{step.target}: {step.action} done")

    # -- read-back ----------------------------------------------------------

    def verify(self) -> ReleaseResult:
        """Read everything back and compare it with the release."""
        boards = read_boards(self.hv, self.tx)
        lines, ok = [], True
        for board in boards:
            image = self.bundle.apps[board.board]
            good = image.is_installed(board.header) and board.domain == self.bundle.domain
            ok &= good
            lines.append(f"[{'ok' if good else 'FAIL'}] {board.name}: {board.version} "
                         f"({board.domain})")
        if self.bundle.presets is not None:
            state = read_presets(self.hv, self.tx, len(boards) - 1)
            for step in preset_steps(state, self.bundle.presets, self.bundle.domain):
                good = step.action == "none"
                ok &= good
                if not good or step.module is None or step.slot < len(self.bundle.presets.transmitter):
                    lines.append(f"[{'ok' if good else 'FAIL'}] {step.target}: {step.detail}")
        return ReleaseResult(ok=ok, lines=lines)


# ---------------------------------------------------------------------------
# CLI:  python -m openlifu_sdk.io.LIFUReleaseUpdate --firmware ... [--presets ...]
# ---------------------------------------------------------------------------

def connect(timeout_s: float = 20.0):
    """Open the console and the transmitter master (research or clinical
    firmware). Returns ``(hv, tx)``.

    Raises:
        ReleaseError: Either does not answer within *timeout_s*.
    """
    from openlifu_sdk.io.LIFUInterface import LIFUInterface

    deadline = time.monotonic() + timeout_s
    while True:
        interface = LIFUInterface(TX_test_mode=False)
        tx_ok, hv_ok = interface.is_device_connected()
        if tx_ok and hv_ok:
            return interface.hvcontroller, interface.txdevice
        if time.monotonic() >= deadline:
            raise ReleaseError(f"console connected: {hv_ok}, transmitter connected: {tx_ok}. "
                               "Both must run their application.")
        time.sleep(1.0)


def run_cli(bundle: ReleaseBundle, *, plan_only: bool, yes: bool,
            out: Callable[[str], None] = print,
            confirm: Callable[[str], bool] | None = None) -> int:
    """The shared command-line flow (also used by openlifu-fw-updater)."""
    for line in bundle.describe():
        out(line)
    hv, tx = connect()
    update = ReleaseUpdate(bundle, hv, tx, report=out)
    plan = update.plan()
    out("Plan:")
    for line in plan.describe():
        out(f"  {line}")
    if plan.refusals:
        out("REFUSED: nothing will be written.")
        return 2
    if not plan.work:
        out("The unit already runs this release.")
        return 0
    if plan_only:
        return 0
    if not yes and not (confirm or _confirm)(f"Apply {len(plan.work)} step(s)?"):
        out("Aborted.")
        return 1
    try:
        result = update.run(plan)
    except ReleaseError as e:
        out(f"UPDATE FAILED: {e}")
        return 1
    out("Read-back:")
    for line in result.lines:
        out(f"  {line}")
    out("ALL OK" if result.ok else "READ-BACK MISMATCH")
    return 0 if result.ok else 1


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m openlifu_sdk.io.LIFUReleaseUpdate",
                                     description="Take a unit to a release: firmware and presets.")
    parser.add_argument("--firmware", required=True, help="firmware-<tag>.zip, or its folder")
    parser.add_argument("--presets", help="<context>-presets-<tag>.zip, or its folder")
    parser.add_argument("--plan", action="store_true", help="show the plan and write nothing")
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask before writing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    try:
        bundle = ReleaseBundle.load(args.firmware, args.presets)
        return run_cli(bundle, plan_only=args.plan, yes=args.yes)
    except ReleaseError as e:
        print(f"ERROR: {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
