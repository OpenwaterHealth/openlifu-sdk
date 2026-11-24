# SPDX-License-Identifier: Apache-2.0
"""High-level firmware update for the console and the transmitter.

**Console** (:class:`LIFUFirmwareUpdate`) — one entry point for every case. A
console unit is in one of four states, each needing a different update path:

  - **no DFU** (app < 1.1.0)            → REFUSED: the DFU request never
    reaches a DFU environment, so no software update path exists.
  - **no bootloader** (1.1.0–1.2.2)     → a plain DFU request reboots into the
    STM32 ROM DFU; write the combined bootloader+app production image.
  - **legacy bootloader** (1.2.3–1.2.5) → migrate via the RAM-resident
    self-updater (the legacy DFU can't reach the ROM loader).
  - **secure bootloader** (≥ 1.2.6)     → normal signed-app update; a
    bootloader older than 1.1.0 is first replaced by the bootloader updater
    shim (see :func:`bootloader_needs_migration`).

:class:`LIFUFirmwareUpdate` auto-detects the state (from the running app
version, or the current DFU environment) and runs the right path. The SDK
carries no firmware: the caller passes the images (the signed app, the
bootloader updater, the production image) from the firmware releases. It needs
**no signing keys**:
the legacy updater is authenticated by an HMAC trust tag computed on the fly,
and the secure bootloader verifies the app at boot. An optional ``keys_dir``
only adds an ECDSA app-signature pre-check before flashing.

Typical use::

    from openlifu_sdk.io.LIFUInterface import LIFUInterface
    from openlifu_sdk.io.LIFUFirmwareUpdate import LIFUFirmwareUpdate

    interface = LIFUInterface(TX_test_mode=False)
    fw = LIFUFirmwareUpdate(hv=interface.hvcontroller)
    result = fw.update(signed_app="openlifu-console-fw-signed.bin",
                       bl_updater="open-lifu-console-bl-updater-signed.bin")
    print(result.summary)

**Transmitter** (:class:`LIFUTransmitterFirmwareUpdate`) — auto-detects the
module 0 (USB master) unit's state and updates it:

  - **secure bootloader** (app >= 2.0.8) → standard USB DFU: the signed app
    is written to the active slot and verified by the bootloader at boot. A
    bootloader older than 1.1.0 is first replaced by the bootloader updater
    shim (master over USB, slaves over I2C).
  - **legacy bootloader** (app 2.0.4 - 2.0.7) → migrate via the STM32 ROM
    DFU (OW_CMD_DFU reserved=0x77 force switch): mass-erase and write the
    combined bootloader+app production image.
  - **no bootloader** (app 2.0.2 - 2.0.3) → same production-image migration,
    but entered with a PLAIN OW_CMD_DFU (those apps ignore the reserved byte
    and always reboot into the STM32 ROM DFU).
  - **no DFU at all** (app < 2.0.2) → REFUSED: those builds have no
    OW_CMD_DFU handler, so no software request can reboot them into a DFU
    environment. Enter the STM32 ROM loader in hardware (BOOT0) and re-run,
    or program over SWD.
  - **legacy bootloader, dead app** (unit already parked in "LIFU BL DFU") →
    USB-only recovery: the ROM loader is out of reach without a running app,
    so the RAM-resident legacy updater is written into the legacy app slot and
    replaces the bootloader from the inside; the signed app is then flashed
    over the secure DFU that comes up.

Slave modules (index >= 1) are updated over I2C through the master with
:meth:`LIFUTransmitterFirmwareUpdate.update_slave`: secure-bootloader slaves
get the signed app streamed to their slot; legacy-bootloader slaves (app
2.0.4 - 2.0.7) are migrated in ONE SHOT — a small RAM-resident DFU stub is
written through the legacy I2C DFU, and the full production image
(bootloader + app) is then streamed through the stub's full-flash I2C DFU.
Slaves on app 2.0.2 - 2.0.3 (no bootloader) cannot be updated over I2C —
connect them as the USB master and update as module 0. Slaves below 2.0.2
cannot be updated at all without BOOT0 or SWD.

No signing keys are needed::

    fw = LIFUTransmitterFirmwareUpdate(tx=interface.txdevice)
    result = fw.update(signed_app="openlifu-transmitter-fw-signed.bin",
                       bl_updater="open-lifu-transmitter-bl-updater-signed.bin")
    print(result.summary)

NOTE: the console migration paths are for unlocked (beta) units only; after
RDP/FDA lockdown the force-ROM-DFU switch is inert and the bootloader is not
erasable.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from openlifu_sdk.io.LIFUDFU import (
    CONSOLE_MIN_DFU_APP_VERSION,
    CONSOLE_PROFILE,
    DFU_KIND_LEGACY,
    DFU_KIND_NO_DFU,
    DFU_KIND_ROM,
    DFU_KIND_SECURE,
    I2C_DFU_SLAVE_ADDR,
    LIFUDFUManager,
    STM32I2CDFUviaMaster,
    TRANSMITTER_MIN_DFU_APP_VERSION,
    TRANSMITTER_PROFILE,
    DFUWriteRefusedError,
    infer_console_bootloader_from_app_version,
    infer_transmitter_bootloader_from_app_version,
)
from openlifu_sdk.io.exceptions import LIFUCommunicationError

logger = logging.getLogger(__name__)


def _request_dfu(enter: Callable[[], Any], what: str) -> None:
    """Ask the running application to reboot into DFU.

    The application resets into its bootloader as soon as it takes the
    command, usually before its reply reaches the host, so no reply is the
    normal case, not a failure: the caller goes on to wait for the DFU device
    to enumerate, and that wait reports a unit that never got there. A device
    error reply (e.g. a refusal) is still raised.
    """
    try:
        enter()
    except LIFUCommunicationError:
        logger.info("%s: no reply to the DFU request; the application is "
                    "rebooting into DFU, waiting for it to enumerate", what)


# Cohort constants (align with infer_console_bootloader_from_app_version)
COHORT_NONE = "no-bootloader"
COHORT_LEGACY = "legacy-bl"
COHORT_SECURE = "secure-bl"
# Transmitter only: app too old to enter ANY DFU mode (< 2.0.2 — no OW_CMD_DFU
# handler). Not updatable by this SDK; see LIFUDFU.DFU_KIND_NO_DFU.
COHORT_NO_DFU = DFU_KIND_NO_DFU









# Bootloader migration (open-lifu-{console,transmitter}-bl-updater). A unit on
# a secure bootloader older than this first gets the bootloader updater shim:
# a one-shot application, signed with the research key the old bootloader
# trusts, that rewrites the bootloader with the one it carries, erases the
# old slot, the preset area and the anti-rollback floor, and resets into the
# NEW bootloader's DFU (USB on a console or transmitter master, I2C on a
# slave). The application is then installed there. Research (RDP level 0)
# units only: the shim refuses to touch a protected unit and comes back in the
# old bootloader's DFU, and clinical units are built with the new bootloader.
MIGRATE_BELOW_BL = (1, 1, 0)
# How long the shim may take to rewrite the bootloader, clean up and come back
# in the new bootloader's DFU (a few seconds on the bench; generous for the
# VM re-attaching USB).
MIGRATION_TIMEOUT_S = 90.0
# Slave migration, after the updater is written over I2C: wait this long
# before the first re-enumeration, then poll this slowly. The old bootloader
# verifies and launches the updater, which rewrites the bootloader and erases
# about 95 pages before the new bootloader's I2C DFU answers: re-enumerating
# while the slave cannot answer makes the master's discovery time out, and on
# master firmware up to 2.1.0-rc.4 a later enumeration after such a timeout
# hangs the master until its watchdog resets it (openlifu-transmitter-fw#80).
SLAVE_MIGRATION_SETTLE_S = 20.0
SLAVE_MIGRATION_POLL_S = 10.0
# After a slave I2C update, the version read can come back as the "v0.0.0"
# placeholder (empty reply) for a few seconds while the slave application
# starts. Only the version READ is retried (one enumeration, never repeated:
# see above), every SLAVE_VERSION_POLL_S, for at most SLAVE_VERSION_TIMEOUT_S.
# Bounded because a master on firmware 2.0.8 truncates forwarded version
# replies and never reports a valid slave version.
SLAVE_VERSION_TIMEOUT_S = 24.0
SLAVE_VERSION_POLL_S = 2.0
# Work done in the new bootloader's DFU before the application (``before_app``,
# e.g. clinical presets): each bootloader preset write ends in a reset, and the
# bootloader, with its slot still empty, comes back in DFU. USB: wait this long
# for the device to drop off the bus, then up to PRESET_DFU_TIMEOUT_S for it to
# enumerate again (generous: the VM re-attaches USB). I2C slave: wait
# SLAVE_PRESET_SETTLE_S before re-enumerating (the bootloader only restarts,
# nothing is erased; re-enumerating while it cannot answer hangs a master on
# firmware up to 2.1.0-rc.4, see SLAVE_MIGRATION_SETTLE_S), then poll every
# SLAVE_MIGRATION_POLL_S for at most MIGRATION_TIMEOUT_S.
PRESET_RESET_SETTLE_S = 3.0
PRESET_DFU_TIMEOUT_S = 90.0
SLAVE_PRESET_SETTLE_S = 5.0


@dataclass
class DfuContext:
    """What a ``before_app`` callback gets: the board is in its NEW
    bootloader's DFU (after a bootloader replacement), its application slot is
    empty, and the application is written once the callback returns. The
    callback must leave the board in that DFU (a bootloader with an empty slot
    stays in DFU after a reset; re-acquire it after each reset).

    Attributes:
        device: ``"console"``, ``"transmitter master"`` or
            ``"transmitter module N"``.
        module: 0 for the console and the transmitter master, N for a slave.
        mgr: The :class:`LIFUDFUManager` the update uses (for a slave, built
            on the master's UART, as the I2C writers need).
        vid, pid, libusb_dll: The USB DFU device (console, master); None on a
            slave.
        wait_dfu: USB only: after the board reset, wait for its bootloader DFU
            again; returns the bootloader version.
        i2c_addr: Slave only: the bootloader's current I2C DFU address.
        reacquire: Slave only: after the slave reset, re-enumerate the chain
            until the slave is a bootloader node again; updates
            :attr:`i2c_addr` and returns it.
    """

    device: str
    module: int
    mgr: Any
    vid: int | None = None
    pid: int | None = None
    libusb_dll: str | None = None
    wait_dfu: Callable[[], str] | None = None
    i2c_addr: int | None = None
    reacquire: Callable[[], int] | None = None


def _usb_wait_dfu(get_bl: Callable[[float], str]) -> Callable[[], str]:
    """``DfuContext.wait_dfu`` for a USB board: let the reset take the device
    off the bus, then wait for its bootloader DFU."""
    def wait() -> str:
        time.sleep(PRESET_RESET_SETTLE_S)
        return get_bl(PRESET_DFU_TIMEOUT_S)
    return wait


def _refuse_before_app(before_app: Callable | None, path: str) -> None:
    if before_app is not None:
        raise RuntimeError(
            f"before_app needs the secure-bootloader path; this unit takes the "
            f"{path} path. Nothing was written.")


def _bl_version_tuple(bl_version: str | None) -> tuple[int, int, int] | None:
    m = re.match(r"\s*v?(\d+)\.(\d+)\.(\d+)", bl_version or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _is_real_version(version: str | None) -> bool:
    """True for a parseable X.Y.Z version other than the SDK's "v0.0.0"
    placeholder for an empty / invalid version reply."""
    v = _bl_version_tuple(version)
    return v is not None and v != (0, 0, 0)


def _bl_release_key(version: str | None) -> tuple[int, ...] | None:
    """An ordering key for a bootloader version: ``1.1.0-rc.2`` <
    ``1.1.0-rc.3`` < ``1.1.0`` < ``1.1.1``. Build metadata (``+...``) and a
    ``clinical`` variant suffix (``1.1.0-rc.3-clinical``, ``1.1.0-clinical``)
    are ignored: the clinical build of a bootloader orders as that release.
    None if *version* does not parse."""
    m = re.match(r"\s*v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", version or "")
    if not m:
        return None
    base = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    pre = re.sub(r"(?:^|[.-])clinical$", "", m.group(4) or "", flags=re.IGNORECASE)
    if not pre:
        return (*base, 1, 0)
    rc = re.match(r"rc\.?(\d+)", pre)
    return (*base, 0, int(rc.group(1)) if rc else 0)


def bootloader_needs_migration(bl_version: str | None, target: str | None = None) -> bool:
    """True if the bootloader updater shim must replace this secure bootloader
    before the application is installed: a bootloader older than
    :data:`MIGRATE_BELOW_BL` (console 1.0.x, transmitter 1.0.x), or, with
    *target* (the bootloader the updater carries, from its SBOM), one older
    than *target*. A version that cannot be parsed is left alone (False)."""
    v = _bl_version_tuple(bl_version)
    if v is None:
        return False
    if v < MIGRATE_BELOW_BL:
        return True
    have, want = _bl_release_key(bl_version), _bl_release_key(target)
    return have is not None and want is not None and have < want


def _migration_floor(target: str | None) -> str:
    return target or ".".join(map(str, MIGRATE_BELOW_BL))




def _require_image(path: str | Path | None, what: str, option: str) -> str:
    """The SDK carries no firmware: every image comes from the caller (a
    firmware release). Refuse a missing one before anything is written."""
    if not path:
        raise ValueError(f"no {what} given: pass {option} (the image from the "
                         "firmware release). Nothing was written.")
    if not Path(path).is_file():
        raise ValueError(f"{what} not found: {path}. Nothing was written.")
    return str(path)


def _resolve_bl_updater(bl_updater: str | None, device: str, bl_version: str) -> str:
    if not bl_updater:
        raise RuntimeError(
            f"{device} bootloader {bl_version} must be replaced before this "
            "application can be installed, and no bootloader updater image was "
            "given. Pass the open-lifu-*-bl-updater signed image (bl_updater= / "
            "--bl-updater). Nothing was written.")
    return _require_image(bl_updater, "bootloader updater image", "bl_updater= / --bl-updater")


def _install_with_migration(device: str, get_bl: Callable[[float], str],
                            program: Callable[[str], None],
                            abort: Callable[[], None], app: str,
                            bl_updater: Callable[[str], str], entered: bool,
                            slot_empty: Callable[[], bool] | None = None,
                            replace: bool = False,
                            bl_target: str | None = None,
                            before_app: Callable[[], None] | None = None
                            ) -> tuple[str, bool]:
    """Install *app* through the secure bootloader's USB DFU, replacing an old
    bootloader with the updater shim first.

    *get_bl(timeout_s)* waits for the DFU device and returns its bootloader
    version; *program(image)* writes a signed image (pre-erase checks, raises
    ValueError on refusal); *abort()* resets the unit out of DFU; *bl_updater
    (bl_version)* returns the shim image path. *replace* installs the updater
    whatever the bootloader's version (a research unit converted to clinical:
    same version, other key). *slot_empty()* tells the new bootloader's DFU
    (the updater erased the slot) from the old one (it holds the updater).
    *bl_target* is the bootloader the updater carries: an older one is
    replaced too (see :func:`bootloader_needs_migration`). *before_app()*,
    only when the bootloader was replaced, runs in the new bootloader's DFU
    before the application is written (see :class:`DfuContext`).
    Returns ``(bootloader version, migrated)``.
    """
    bl = get_bl(45.0)
    logger.info("%s bootloader version: %s", device, bl)
    if not (replace or bootloader_needs_migration(bl, bl_target)):
        _program_or_abort(program, app, abort, entered)
        return bl, False

    shim = bl_updater(bl)
    logger.info("%s bootloader %s is older than %s: installing the bootloader "
                "updater first (%s)", device, bl, _migration_floor(bl_target),
                Path(shim).name)
    _program_or_abort(program, shim, abort, entered)
    new_bl = _wait_for_migrated_bootloader(device, get_bl, bl, slot_empty, bl_target)
    logger.info("%s bootloader replaced: %s -> %s; installing the application",
                device, bl, new_bl)
    # The new bootloader starts with an empty slot: nothing to fall back to,
    # so a refusal here (or in before_app) leaves the unit in DFU for a retry.
    if before_app is not None:
        before_app()
    program(app)
    return new_bl, True


def _program_or_abort(program: Callable[[str], None], image: str,
                      abort: Callable[[], None], entered: bool) -> None:
    try:
        program(image)
    except (ValueError, DFUWriteRefusedError):
        # Pre-flight refusal, or the bootloader refused the image at its
        # first block (open-lifu-*-bl >= 1.1.0 checks the header before it
        # erases): nothing was erased. If this call put the unit into DFU,
        # boot the installed application back up.
        if entered:
            abort()
        raise


def _wait_for_migrated_bootloader(device: str, get_bl: Callable[[float], str],
                                  old_bl: str,
                                  slot_empty: Callable[[], bool] | None = None,
                                  bl_target: str | None = None) -> str:
    """After the shim was installed, wait for the NEW bootloader's DFU: a
    bootloader of at least :data:`MIGRATE_BELOW_BL` (and *bl_target*, if
    given) with an empty slot (the
    updater erases the slot it ran from; the old bootloader still holds it)."""
    deadline = time.monotonic() + MIGRATION_TIMEOUT_S
    seen = old_bl
    while time.monotonic() < deadline:
        time.sleep(3.0)
        try:
            seen = get_bl(max(5.0, deadline - time.monotonic()))
        except RuntimeError:
            continue
        if bootloader_needs_migration(seen, bl_target):
            continue
        try:
            if slot_empty is None or slot_empty():
                return seen
        except RuntimeError:
            continue
    raise RuntimeError(
        f"{device}: the bootloader updater was installed on bootloader {old_bl} "
        f"but no newer bootloader came up within {MIGRATION_TIMEOUT_S:.0f} s "
        f"(last seen: {seen}). If the unit is back in bootloader {old_bl}'s "
        "DFU, the updater refused it (protected unit) and nothing was "
        "changed. If it does not enumerate and its LEDs blink, the bootloader "
        "rewrite failed: recover it over SWD.")


def _reopen(device: Any) -> None:
    """Reopen a component's serial port if it was lost. The SDK reopens a
    port from LIFUInterface's monitor, which an update run does not start; a
    transmitter master resets (and its USB port drops) when its watchdog
    fires."""
    if not device.is_connected() and device.connect():
        logger.info("Reopened the %s port", getattr(getattr(device, "_uart", None), "desc", "device"))


def _no_dfu_message(device: str, app_version: str | None,
                    floor: tuple[int, ...], action: str = "updated") -> str:
    """Message for a unit whose application predates DFU support entirely.

    DFU entry is a request to the RUNNING application (OW_CMD_DFU): it arms
    the bootloader/ROM-loader path and resets. Apps below *floor* never reboot
    into a DFU environment in response, so every software path — plain entry,
    the reserved=0x77 ROM-DFU force switch, any bootloader DFU — dead-ends
    before it starts. Refusing keeps the unit running instead of stranding it
    after a half-attempted entry.
    """
    ver = app_version or "unknown"
    floor_str = ".".join(str(p) for p in floor)
    return (
        f"{device} app {ver} is older than {floor_str} and cannot be {action}: "
        "those builds do not reboot into any DFU mode when asked, so no "
        "software update path exists (not the plain OW_CMD_DFU entry, not the "
        "reserved=0x77 ROM-DFU force switch). Recover the unit in hardware — "
        "enter the STM32 ROM loader with BOOT0 at power-up where the board "
        "exposes it, then re-run this command (it enumerates as 'STM32 "
        "BOOTLOADER' and the full production image is written from there) — "
        "or program it over SWD.")


@dataclass
class UpdateResult:
    """Outcome of an update run."""

    cohort: str            # detected cohort / DFU kind driving the choice
    action: str            # "migrate-rom" | "migrate-legacy" | "app-update"
    summary: str           # human-readable one-liner
    reboot_required: bool  # True if the operator must power-cycle the unit
                           # to run the new app (False when it resets into
                           # the new app by itself)
    bl_version: str | None = None  # bootloader version string (DFU virtual
                                   # version address), when the path read it


class LIFUFirmwareUpdate:
    """Auto-detecting console firmware updater covering every unit state.

    From a RUNNING application the app version names the cohort, a DFU request
    is issued, and whichever DFU environment the unit lands in gets the image
    that belongs to it:

      - **app >= 1.2.6** (current bootloader) → its own DFU → signed app.
      - **app 1.2.3 - 1.2.5** (legacy bootloader) → the legacy DFU, which can
        only write the app slot → the RAM-resident updater swaps in the
        current bootloader, then the signed app is flashed.
      - **app 1.1.0 - 1.2.2** (no bootloader) → a plain DFU request reboots
        straight into the STM32 ROM DFU → combined production image.
      - **app < 1.1.0** → REFUSED: the DFU request never reaches a DFU
        environment, so there is no software update path (BOOT0 or SWD).

    A unit that is ALREADY in DFU is identified from its USB product string
    and takes the same image mapping without any DFU request: STM32 ROM DFU →
    production image, legacy bootloader DFU → updater + signed app, current
    bootloader DFU → signed app.

    Args:
        hv: A connected ``HVController`` (e.g. ``interface.hvcontroller``) used
            to read the running app version and to trigger DFU entry. May be
            omitted only if the unit is already in a DFU mode.
        keys_dir: Optional keys directory to ECDSA-validate the signed app
            before flashing. Not required for any path.
        libusb_dll: Optional explicit libusb-1.0 DLL path (Windows).
    """

    def __init__(self, hv: Any = None, keys_dir: str | None = None,
                 libusb_dll: str | None = None,
                 vid: int = 0x0483, pid: int = 0xDF11):
        self.hv = hv
        self.keys_dir = keys_dir
        self.libusb_dll = libusb_dll
        self.vid = vid
        self.pid = pid
        self._mgr = LIFUDFUManager()
        # Version string the running app last reported, for error messages.
        self._last_app_version: str | None = None

    def _no_dfu_message(self, action: str = "updated") -> str:
        """Why a console app older than 1.1.0 cannot be touched at all; see
        :func:`_no_dfu_message`."""
        return _no_dfu_message("console", self._last_app_version,
                               CONSOLE_MIN_DFU_APP_VERSION, action)

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def detect_cohort(self) -> tuple[str, str | None]:
        """Determine what update path the unit needs.

        Returns ``(cohort, source)`` where cohort is ``COHORT_NO_DFU`` (app <
        1.1.0 — no DFU environment is reachable, so :meth:`update` refuses),
        ``COHORT_NONE`` (app 1.1.0 - 1.2.2, no bootloader), ``COHORT_LEGACY``
        (app 1.2.3 - 1.2.5, first custom bootloader) or ``COHORT_SECURE`` (app
        >= 1.2.6, current bootloader), and source is ``"app"`` (from the
        running app version) or ``"dfu"`` (the unit was already in DFU).

        A unit already in DFU is classified from its USB product string alone:
        "STM32 BOOTLOADER" -> ``COHORT_NONE``, "LIFU BL DFU x.y.z" ->
        ``COHORT_LEGACY``, "OW DFU x.y.z" -> ``COHORT_SECURE``. ``COHORT_NO_DFU``
        can only come from an app version — a unit sitting in a DFU has, by
        definition, reached one.

        Raises:
            RuntimeError: Cannot determine the state (no HV controller and no
                DFU device present).
        """
        # Prefer the running app version - it names the true cohort.
        if self.hv is not None:
            try:
                ver = str(self.hv.get_version())
                cohort = infer_console_bootloader_from_app_version(ver)
                self._last_app_version = ver
                logger.info("Detected app version %s -> cohort %s", ver, cohort)
                return cohort, "app"
            except Exception as e:
                logger.info("App version read failed (%s); checking DFU state", e)

        # Fall back to the DFU environment (unit already in a bootloader DFU).
        kind, dfu_ver = self._mgr.detect_console_dfu_kind(
            vid=self.vid, pid=self.pid, libusb_dll=self.libusb_dll)
        mapping = {
            DFU_KIND_ROM: COHORT_NONE,
            DFU_KIND_LEGACY: COHORT_LEGACY,
            DFU_KIND_SECURE: COHORT_SECURE,
        }
        if kind not in mapping:
            raise RuntimeError(
                f"Cannot determine console state (DFU kind {kind!r}). Connect "
                "the running app, or put the unit in a known DFU mode.")
        logger.info("Detected DFU environment %s %s -> cohort %s",
                    kind, dfu_ver, mapping[kind])
        return mapping[kind], "dfu"

    # ------------------------------------------------------------------
    # Bootloader (DFU) version
    # ------------------------------------------------------------------

    def get_bootloader_version(self) -> str:
        """Read the console bootloader's version string (its git describe,
        served over DFU at the virtual version address).

        If the unit is already in DFU, the version is read directly. If the
        application is running, the unit is rebooted into DFU, the version is
        read, and the bootloader is asked to reset back into the application
        via its virtual reset address (bootloaders without the reset hook —
        open-lifu-console-bl <= 1.0.2 — stay in DFU until a power-cycle).

        Raises:
            RuntimeError: State undetectable, or the DFU device did not
                enumerate.
        """
        cohort, source = self.detect_cohort()
        if cohort == COHORT_NO_DFU:
            raise RuntimeError(self._no_dfu_message("put into DFU"))
        if cohort == COHORT_NONE:
            raise RuntimeError(
                "unit has no bootloader (STM32 ROM DFU) — there is no "
                "bootloader version to read")
        in_dfu = source == "dfu"
        if not in_dfu:
            logger.info("Entering DFU to read the bootloader version...")
            self._enter_dfu()
            time.sleep(2.0)
        try:
            return self._mgr.get_console_bootloader_version(
                vid=self.vid, pid=self.pid, libusb_dll=self.libusb_dll)
        finally:
            if not in_dfu:
                self._mgr.abort_dfu(vid=self.vid, pid=self.pid,
                                    libusb_dll=self.libusb_dll,
                                    profile=CONSOLE_PROFILE)

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def _force_production_update(self, production_image: str | None,
                                 progress_callback: Callable | None
                                 ) -> UpdateResult:
        """Force a FULL production reflash (bootloader + signed app) via the
        STM32 ROM DFU, regardless of the unit's cohort — the path for rolling
        a NEW BOOTLOADER onto a unit that is already on the secure bootloader.

        The running application is forced into the ROM loader with the
        OW_CMD_DFU hidden switch (reserved=0x77). Beta/unlocked units only:
        after RDP/FDA lockdown the bootloader flash is protected and the
        switch is inert, so the flow aborts at the ROM-DFU gate without
        touching flash.
        """
        cohort, source = self.detect_cohort()
        in_rom_dfu = source == "dfu" and cohort == COHORT_NONE
        if cohort == COHORT_NO_DFU:
            raise RuntimeError(self._no_dfu_message("reflashed"))
        if source == "dfu" and not in_rom_dfu:
            raise RuntimeError(
                "cannot force a production reflash from bootloader DFU — the "
                "ROM loader is entered from the RUNNING application "
                "(OW_CMD_DFU force switch). Boot the app first, then re-run.")
        prod = _require_image(production_image, "production image (bootloader + signed app)", "production_image= / --production")
        # Apps without a bootloader (1.1.0 - 1.2.2) predate the reserved=0x77
        # force switch; their plain DFU request already lands in the ROM
        # loader, which is where this path needs to be.
        enter = self._enter_dfu if cohort == COHORT_NONE else self._enter_rom
        self._mgr.migrate_console_full_image(
            prod,
            enter_stm32_rom_dfu_fn=None if in_rom_dfu else enter,
            keys_dir=self.keys_dir, vid=self.vid, pid=self.pid,
            libusb_dll=self.libusb_dll, progress_callback=progress_callback)
        return UpdateResult(cohort, "force-production",
                            "Reflashed the full production image (bootloader "
                            "+ app) via STM32 ROM DFU.", reboot_required=True)

    def update(self, *, production_image: str | None = None,
               signed_app: str | None = None, updater_bin: str | None = None,
               force: bool = False, force_production: bool = False,
               bl_updater: str | None = None, replace_bootloader: bool = False,
               progress_callback: Callable | None = None,
               bl_target: str | None = None,
               before_app: Callable[[DfuContext], None] | None = None
               ) -> UpdateResult:
        """Detect the unit's state and run the appropriate update.

        The DFU environment the unit reaches decides the image: STM32 ROM DFU
        (app 1.1.0 - 1.2.2, entered with a plain DFU request) takes the
        combined production image; the legacy bootloader's DFU (app
        1.2.3 - 1.2.5) takes the RAM updater and then the signed app; the
        current bootloader's DFU (app >= 1.2.6) takes the signed app alone.
        Apps below 1.1.0 are refused — they never reach a DFU environment.

        Args:
            production_image: The combined bootloader+app image
                (no-bootloader / force-production paths).
            signed_app: The signed app (legacy + secure paths).
            updater_bin: The legacy RAM updater (legacy path).
            force: For the secure app-update path, skip the SDK's check that
                the image version is not below the installed one. The
                bootloader's anti-rollback still applies: a console
                bootloader >= 1.1.0 refuses the downgrade before erasing;
                elsewhere the floor may reject it at boot.
            force_production: Reflash the FULL production image (bootloader +
                app) via STM32 ROM DFU regardless of cohort — see
                :meth:`_force_production_update`. Beta/unlocked units only.
            bl_updater: Override the bootloader updater shim installed first
                on a secure bootloader older than 1.1.0 (see
                :func:`bootloader_needs_migration`). Required when the
                bootloader is replaced.
            bl_target: The bootloader *bl_updater* carries (its SBOM). A
                secure bootloader older than it is replaced too, so a 1.1.x
                unit moves to a newer bootloader. None keeps the 1.1.0 rule.
            before_app: Called with a :class:`DfuContext` in the new
                bootloader's USB DFU, after the bootloader was replaced and
                before the application is written (e.g. to write clinical
                presets through the bootloader). Not called when the
                bootloader is kept. Secure-bootloader path only: any other
                path refuses before anything is written.

        Returns:
            :class:`UpdateResult` (``action`` ``"bl-migrate"`` when the
            bootloader was replaced first).

        Raises:
            ValueError: Bad image / downgrade refused.
            RuntimeError: State undetectable, wrong DFU environment, no
                updater image for an old bootloader, the new bootloader did not
                come up, or a write/verify failure.
        """
        if force_production:
            _refuse_before_app(before_app, "force-production")
            return self._force_production_update(production_image,
                                                 progress_callback)
        cohort, source = self.detect_cohort()
        if cohort == COHORT_NO_DFU:
            raise RuntimeError(self._no_dfu_message())
        # When already in DFU we must not trigger another DFU entry.
        in_dfu = source == "dfu"

        if cohort != COHORT_SECURE:
            _refuse_before_app(before_app, cohort)
        if cohort == COHORT_NONE:
            prod = _require_image(production_image, "production image (bootloader + signed app)", "production_image= / --production")
            # App 1.1.0 - 1.2.2 (or already sitting in the STM32 ROM DFU). A
            # PLAIN DFU request is what these apps understand — they have no
            # bootloader to intercept it and no reserved=0x77 force switch, so
            # the request reboots them straight into the ROM loader.
            self._mgr.migrate_console_full_image(
                prod,
                enter_stm32_rom_dfu_fn=None if in_dfu else self._enter_dfu,
                keys_dir=self.keys_dir, vid=self.vid, pid=self.pid,
                libusb_dll=self.libusb_dll, progress_callback=progress_callback)
            return UpdateResult(cohort, "migrate-rom",
                                "Migrated no-bootloader unit to the current "
                                "bootloader (full production image via STM32 "
                                "ROM DFU).", reboot_required=True)

        if cohort == COHORT_LEGACY:
            app = _require_image(signed_app, "signed application image", "signed_app= / --app")
            self._mgr.migrate_console_legacy(
                signed_app=app, updater_bin=updater_bin,
                enter_dfu_fn=None if in_dfu else self._enter_dfu,
                keys_dir=self.keys_dir, vid=self.vid, pid=self.pid,
                libusb_dll=self.libusb_dll, progress_callback=progress_callback)
            return UpdateResult(cohort, "migrate-legacy",
                                "Migrated legacy-bootloader unit to the secure "
                                "bootloader (RAM updater).", reboot_required=True)

        if cohort == COHORT_SECURE:
            app = _require_image(signed_app, "signed application image", "signed_app= / --app")
            return self._update_secure(cohort, app, in_dfu, force, bl_updater,
                                       progress_callback, replace_bootloader, bl_target,
                                       before_app)

        raise RuntimeError(f"unhandled cohort: {cohort}")

    def _update_secure(self, cohort: str, app: str, in_dfu: bool, force: bool,
                       bl_updater: str | None,
                       progress_callback: Callable | None,
                       replace_bootloader: bool = False,
                       bl_target: str | None = None,
                       before_app: Callable[[DfuContext], None] | None = None
                       ) -> UpdateResult:
        """The secure-bootloader path of :meth:`update`: the signed app over
        the bootloader's DFU, replacing a 1.0.x bootloader through the updater
        shim first."""
        if not in_dfu:
            logger.info("Requesting console DFU mode...")
            self._enter_dfu()
            time.sleep(2.0)
        mgr, kw = self._mgr, dict(vid=self.vid, pid=self.pid,
                                  libusb_dll=self.libusb_dll)

        def get_bl(t: float) -> str:
            return mgr.get_console_bootloader_version(timeout_s=t, **kw)

        ctx = DfuContext("console", 0, mgr, wait_dfu=_usb_wait_dfu(get_bl), **kw)
        bl_version, migrated = _install_with_migration(
            "Console",
            get_bl=get_bl,
            program=lambda img: mgr.program_console(
                img, keys_dir=self.keys_dir, force=force,
                progress_callback=progress_callback, **kw),
            abort=lambda: mgr.abort_dfu(profile=CONSOLE_PROFILE, **kw),
            app=app,
            bl_updater=lambda bl: _resolve_bl_updater(
                bl_updater, "Console", bl),
            entered=not in_dfu,
            slot_empty=lambda: mgr.get_console_installed_version(**kw) is None,
            replace=replace_bootloader, bl_target=bl_target,
            before_app=(lambda: before_app(ctx)) if before_app else None)
        # The manifest resets the unit and the bootloader verifies and
        # launches the new application: no power-cycle (see
        # LIFUDFUManager._program_slot_image).
        if migrated:
            return UpdateResult(cohort, "bl-migrate",
                                f"Replaced the bootloader (now {bl_version}) "
                                "with the bootloader updater, then installed "
                                "the application; the unit is rebooting into "
                                "the new app.", reboot_required=False,
                                bl_version=bl_version)
        return UpdateResult(cohort, "app-update",
                            "Updated the application on the secure "
                            "bootloader; the unit is rebooting into the new "
                            "app.", reboot_required=False,
                            bl_version=bl_version)

    # ------------------------------------------------------------------
    # DFU-entry callables (raise a clear error if no HV controller)
    # ------------------------------------------------------------------

    def _require_hv(self):
        if self.hv is None:
            raise RuntimeError(
                "an HV controller is required to trigger DFU entry (or put the "
                "unit in DFU first and re-run)")
        return self.hv

    def _enter_dfu(self) -> None:
        _request_dfu(self._require_hv().enter_dfu, "Console")

    def _enter_rom(self) -> None:
        _request_dfu(self._require_hv().enter_stm32_rom_dfu, "Console")


class LIFUTransmitterFirmwareUpdate:
    """Transmitter firmware updater — auto-detects the module 0 (USB master)
    unit's state and runs the right path:

      - **secure bootloader** (app >= 2.0.8) → standard USB DFU update:
        reboot the running application into DFU, write the SBSFU signed image
        to the active slot with pre-erase validation and anti-downgrade
        checks, and let the bootloader verify it at boot.
      - **legacy bootloader** (app 2.0.4 - 2.0.7) → migrate: force the STM32
        ROM DFU loader via the OW_CMD_DFU hidden switch (reserved=0x77,
        added in 2.0.4), mass-erase, and write the combined bootloader +
        signed-app production image at the flash base (beta/unlocked units
        only).
      - **no bootloader** (app 2.0.2 - 2.0.3, bare-metal at 0x08000000) →
        same production-image migration, but entered with a PLAIN OW_CMD_DFU:
        those apps ignore the reserved byte and always reboot straight into
        the STM32 ROM DFU (there is no other bootloader to enter).
      - **no DFU support** (app < 2.0.2) → :meth:`update` REFUSES. OW_CMD_DFU
        did not exist yet, so the unit cannot be rebooted into any DFU mode
        from software; it needs BOOT0 (STM32 ROM loader) or SWD. Refusing
        keeps it running instead of stranding it mid-attempt.

    Both migration paths need the RUNNING app to reach the ROM loader. A unit
    whose app is corrupt instead sits in its LEGACY bootloader's own USB DFU
    ("LIFU BL DFU x.y.z"); :meth:`detect` reports ``(COHORT_LEGACY, "dfu")``
    and :meth:`update` recovers it over USB alone:

      - **legacy bootloader DFU, dead app** → write the RAM-resident legacy
        updater (which embeds the secure bootloader) into the legacy app slot
        with an HMAC-trust-tagged WFM1 block, reset so the legacy BL boots it
        and it swaps in the secure bootloader, then flash the signed app over
        the secure DFU that comes up. This path leaves the anti-rollback floor
        page alone, so follow it with ``--force-production`` from the running
        app to roll the newest bootloader.

    Slave modules (index >= 1) are updated over I2C through the master with
    :meth:`update_slave` — the master puts the slave into its bootloader's
    I2C DFU and the images are streamed over the master's I2C passthrough.
    Secure-bootloader slaves get the signed app; LEGACY-bootloader slaves
    (app 2.0.4 - 2.0.7) are migrated in one shot via the RAM-resident DFU
    stub (bootloader + app from the production image). Slaves on app 2.0.2 -
    2.0.3 have NO bootloader and cannot be updated over I2C at all (their
    DFU entry jumps to the STM32 ROM loader, which does not speak our I2C
    protocol) — connect such a module as the USB master and update it as
    module 0. Slaves below 2.0.2 are refused outright (BOOT0 or SWD).

    Needs no signing keys; an optional *keys_dir* only adds an ECDSA
    signature pre-check before flashing.

    Args:
        tx: A connected ``TxDevice`` (e.g. ``interface.txdevice``) used to read
            the running app version and to trigger DFU entry. May be omitted
            only if the unit is already in USB DFU mode.
        keys_dir: Optional keys directory to ECDSA-validate the signed app
            before flashing. Not required.
        libusb_dll: Optional explicit libusb-1.0 DLL path (Windows).
    """

    def __init__(self, tx: Any = None, keys_dir: str | None = None,
                 libusb_dll: str | None = None,
                 vid: int = 0x0483, pid: int = 0xDF11):
        self.tx = tx
        self.keys_dir = keys_dir
        self.libusb_dll = libusb_dll
        self.vid = vid
        self.pid = pid
        self._mgr = LIFUDFUManager()
        # (kind, version) of the last DFU environment seen by detect(), used to
        # name the environment in recovery messages. None until a DFU probe ran.
        self._last_dfu: tuple[str, str] | None = None
        # Version string the running app last reported, for the same purpose.
        self._last_app_version: str | None = None

    def detect(self) -> tuple[str, str]:
        """Determine what update path the unit needs.

        Returns ``(cohort, source)`` where cohort is ``COHORT_SECURE`` (app
        >= 2.0.8, secure bootloader installed), ``COHORT_LEGACY`` (app
        2.0.4 - 2.0.7, legacy bootloader — production-image migration via the
        reserved=0x77 ROM-DFU force switch), ``COHORT_NONE`` (app 2.0.2 -
        2.0.3, bare-metal, no bootloader — production-image migration via a
        plain DFU entry, which already lands in the STM32 ROM loader) or
        ``COHORT_NO_DFU`` (app < 2.0.2 — no OW_CMD_DFU handler, so no DFU
        mode is reachable from software and :meth:`update` refuses), and
        source is ``"app"`` (from the running app version) or ``"dfu"`` (the
        unit was already in DFU).

        A unit found in the LEGACY bootloader's own USB DFU ("LIFU BL DFU
        x.y.z") reports ``(COHORT_LEGACY, "dfu")``. That is the corrupt- or
        missing-application case: the legacy BL refused to boot the slot and
        parked in DFU. It is detected here so the state can be reported, but
        it CANNOT be updated over USB — see :meth:`update`.

        Raises:
            RuntimeError: No responding application and no recognizable DFU
                device present.
        """
        if self.tx is not None:
            try:
                ver = str(self.tx.get_version())
                cohort = infer_transmitter_bootloader_from_app_version(ver)
                self._last_app_version = ver
                logger.info("Detected transmitter app version %s -> cohort %s",
                            ver, cohort)
                return cohort, "app"
            except Exception as e:
                logger.info("Transmitter app version read failed (%s); "
                            "checking DFU state", e)

        # detect_console_dfu_kind reads only the generic USB product string /
        # DFU version, so it identifies the transmitter environments too.
        kind, dfu_ver = self._mgr.detect_console_dfu_kind(
            vid=self.vid, pid=self.pid, libusb_dll=self.libusb_dll)
        self._last_dfu = (kind, dfu_ver)
        if kind == DFU_KIND_SECURE:
            logger.info("Detected secure-bootloader DFU %s", dfu_ver)
            return COHORT_SECURE, "dfu"
        if kind == DFU_KIND_ROM:
            logger.info("Detected STM32 ROM DFU")
            return COHORT_NONE, "dfu"
        if kind == DFU_KIND_LEGACY:
            # The unit is parked in the LEGACY bootloader's own USB DFU —
            # normally because its application is missing or corrupt, so the
            # app-side ROM-DFU force switch is unreachable. Report the cohort
            # honestly; update() explains the recovery.
            logger.info("Detected legacy-bootloader DFU %s", dfu_ver)
            return COHORT_LEGACY, "dfu"
        raise RuntimeError(
            f"Cannot determine transmitter state (DFU kind {kind!r}). "
            "Connect the running app, or put the unit in a known DFU mode.")

    def _no_dfu_message(self, action: str = "updated") -> str:
        """Why a transmitter app older than 2.0.2 (no OW_CMD_DFU handler)
        cannot be touched at all; see :func:`_no_dfu_message`."""
        return _no_dfu_message("transmitter", self._last_app_version,
                               TRANSMITTER_MIN_DFU_APP_VERSION, action)

    def _legacy_dfu_recovery_message(self, why: str) -> str:
        """Explain, when the USB legacy-updater migration is unavailable, why
        a master parked in the LEGACY bootloader's DFU cannot be updated over
        USB and how to recover it by other means.

        The secure bootloader can only be installed by something with
        full-flash access. The STM32 ROM loader has it but is entered from the
        RUNNING application (OW_CMD_DFU reserved=0x77) — exactly what is
        missing when the legacy BL parks in DFU. The legacy DFU's own write
        window is just the app slot (0x08010000) plus its WFM1 metadata page.
        The RAM-resident DFU stub that rescues legacy SLAVES is no help
        either: it serves I2C only (1.6 KB, no USB stack) and needs a healthy
        master app to broker it. That leaves the legacy updater (which
        replaces the bootloader from inside the slot), BOOT0, or SWD.
        """
        ver = (self._last_dfu[1] if self._last_dfu else "") or "unknown version"
        return (
            f"the transmitter is parked in its LEGACY bootloader's USB DFU "
            f"(LIFU BL DFU {ver}) — its application is missing or corrupt, so "
            "the app-side OW_CMD_DFU force switch that reaches the STM32 ROM "
            f"loader is gone. {why} The legacy bootloader can only write the "
            "app slot (0x08010000) and its metadata page, never its own "
            "region, so nothing else here can install the secure bootloader. "
            "Recover by entering the STM32 ROM loader in hardware: hold BOOT0 "
            "while power-cycling the unit, then re-run this command — it will "
            "enumerate as 'STM32 BOOTLOADER' and the full production image "
            "(bootloader + app) is written from there. Alternatively connect "
            "the module as a SLAVE on a healthy master and migrate it over "
            "I2C with --module <n>.")

    def _migrate_legacy_usb(self, signed_app: str | None,
                            updater_bin: str | None,
                            progress_callback: Callable | None) -> UpdateResult:
        """Rescue a master parked in the legacy bootloader's USB DFU: write
        the RAM-resident legacy updater into the legacy app slot so it swaps
        in the secure bootloader from the inside, then flash the signed app
        over the secure DFU that comes up. See
        :meth:`LIFUDFUManager.migrate_transmitter_legacy_usb`."""
        updater = Path(_require_image(updater_bin, "legacy updater image", "updater_bin= / --updater"))
        if not updater.is_file():
            raise RuntimeError(self._legacy_dfu_recovery_message(
                f"The legacy updater image that would replace the bootloader "
                f"from inside the app slot is not available ({updater}) — "
                "pass one with updater_bin/--updater to use that path."))
        app = _require_image(signed_app, "signed application image", "signed_app= / --app")
        bl_version = self._mgr.migrate_transmitter_legacy_usb(
            signed_app=app, updater_bin=str(updater), enter_dfu_fn=None,
            keys_dir=self.keys_dir, vid=self.vid, pid=self.pid,
            libusb_dll=self.libusb_dll, progress_callback=progress_callback)
        return UpdateResult(
            COHORT_LEGACY, "migrate-legacy-usb",
            "Recovered a legacy-bootloader transmitter over USB: the updater "
            "swapped in the secure bootloader and the signed app was flashed. "
            "Run --force-production once the app boots to roll the newest "
            "bootloader and clear any stale anti-rollback floor.",
            reboot_required=True, bl_version=bl_version)

    def get_bootloader_version(self) -> str:
        """Read the transmitter bootloader's version string (its git
        describe, served over DFU at the virtual version address).

        If the unit is already in DFU, the version is read directly. If the
        application is running, the unit is rebooted into DFU, the version is
        read, and the bootloader is asked to reset back into the application
        via its virtual reset address (bootloaders without the reset hook —
        open-lifu-transmitter-bl <= 1.0.1-rc.1 — stay in DFU until a
        power-cycle).

        Raises:
            RuntimeError: State undetectable, the unit has no secure
                bootloader, or the DFU device did not enumerate.
        """
        state, source = self.detect()
        if state == COHORT_NO_DFU:
            raise RuntimeError(self._no_dfu_message("put into DFU"))
        if state == COHORT_LEGACY and source == "dfu" and self._last_dfu:
            # Already in the legacy BL's DFU: its version came from the USB
            # product string, so report that rather than claiming there is
            # nothing to read.
            return f"LIFU BL DFU {self._last_dfu[1]}".strip()
        if state in (COHORT_NONE, COHORT_LEGACY):
            raise RuntimeError(
                "unit has no secure bootloader (app <= 2.0.7) — there is no "
                "bootloader version to read; run update() to migrate it first")
        in_dfu = source == "dfu"
        if not in_dfu:
            logger.info("Entering DFU to read the bootloader version...")
            self._enter_dfu()
            time.sleep(2.0)
        try:
            return self._mgr.get_transmitter_bootloader_version(
                vid=self.vid, pid=self.pid, libusb_dll=self.libusb_dll)
        finally:
            if not in_dfu:
                self._mgr.abort_dfu(vid=self.vid, pid=self.pid,
                                    libusb_dll=self.libusb_dll,
                                    profile=TRANSMITTER_PROFILE)

    def _force_production_update(self, production_image: str | None,
                                 progress_callback: Callable | None
                                 ) -> UpdateResult:
        """Force a FULL production reflash (bootloader + signed app) via the
        STM32 ROM DFU, regardless of the unit's cohort — the path for rolling
        a NEW BOOTLOADER onto a unit that is already on the secure bootloader.

        The running application is forced into the ROM loader with the
        OW_CMD_DFU hidden switch (reserved=0x77). Beta/unlocked units only:
        after RDP/FDA lockdown the bootloader flash is protected and the
        switch is inert, so the flow aborts at the ROM-DFU gate without
        touching flash.
        """
        state, source = self.detect()
        in_rom_dfu = source == "dfu" and state == COHORT_NONE
        if state == COHORT_NO_DFU:
            raise RuntimeError(self._no_dfu_message("reflashed"))
        if source == "dfu" and state == COHORT_LEGACY:
            raise RuntimeError(self._legacy_dfu_recovery_message(
                "A forced production reflash cannot start here. Re-run "
                "WITHOUT --force-production to recover the unit with the "
                "legacy updater first, then force-production from the "
                "running app to roll the newest bootloader."))
        if source == "dfu" and not in_rom_dfu:
            raise RuntimeError(
                "cannot force a production reflash from bootloader DFU — the "
                "ROM loader is entered from the RUNNING application "
                "(OW_CMD_DFU force switch). Boot the app first, then re-run.")
        prod = _require_image(production_image, "production image (bootloader + signed app)", "production_image= / --production")
        # No-bootloader apps (<= 2.0.3) ignore the reserved byte — a plain DFU
        # entry already reboots into the ROM loader. All later apps need the
        # 0x77 force switch to bypass their bootloader's DFU.
        enter = self._enter_dfu if state == COHORT_NONE else self._enter_rom
        self._mgr.migrate_transmitter_full_image(
            prod,
            enter_stm32_rom_dfu_fn=None if in_rom_dfu else enter,
            keys_dir=self.keys_dir, vid=self.vid, pid=self.pid,
            libusb_dll=self.libusb_dll, progress_callback=progress_callback)
        return UpdateResult(state, "force-production",
                            "Reflashed the full production image (bootloader "
                            "+ app) via STM32 ROM DFU; the unit is rebooting "
                            "into the new app.", reboot_required=False)

    def update(self, *, signed_app: str | None = None,
               production_image: str | None = None,
               updater_bin: str | None = None, force: bool = False,
               force_production: bool = False,
               bl_updater: str | None = None, replace_bootloader: bool = False,
               progress_callback: Callable | None = None,
               bl_target: str | None = None,
               before_app: Callable[[DfuContext], None] | None = None
               ) -> UpdateResult:
        """Detect the unit's state and run the appropriate update.

        A unit found parked in its LEGACY bootloader's USB DFU (dead app)
        takes the USB legacy-updater recovery path — the bootloader replaces
        itself from the app slot, then the signed app is flashed over the
        secure DFU that comes up. Every other legacy unit (app still running)
        takes the ROM-DFU production-image migration, which also clears the
        anti-rollback floor.

        Args:
            signed_app: The signed app image (secure path).
            production_image: The combined bootloader+app image
                (migration / force-production paths).
            updater_bin: The RAM-resident legacy updater (USB legacy-DFU
                recovery path).
            force: Secure path only: flash even if the image version is below
                the installed one (still subject to the bootloader's
                anti-rollback floor at boot).
            force_production: Reflash the FULL production image (bootloader +
                app) via STM32 ROM DFU regardless of cohort — see
                :meth:`_force_production_update`. Beta/unlocked units only.
            bl_updater: The bootloader updater shim installed first on a
                secure bootloader older than 1.1.0, or on any bootloader with
                *replace_bootloader*.
            replace_bootloader: Install *bl_updater* even on a current
                bootloader (a research unit converted to clinical).
            bl_target: The bootloader *bl_updater* carries (its SBOM). A
                secure bootloader older than it is replaced too, so a 1.1.x
                unit moves to a newer bootloader. None keeps the 1.1.0 rule.
            before_app: Called with a :class:`DfuContext` in the new
                bootloader's USB DFU, after the bootloader was replaced and
                before the application is written (e.g. to write clinical
                presets through the bootloader). Not called when the
                bootloader is kept. Secure-bootloader path only: any other
                path refuses before anything is written.

        Returns:
            :class:`UpdateResult` (``action`` ``"bl-migrate"`` when the
            bootloader was replaced first).

        Raises:
            ValueError: Bad image / downgrade refused.
            RuntimeError: State undetectable, wrong DFU environment, no
                updater image for an old bootloader, the new bootloader did not
                come up, or a write/verify failure.
        """
        if force_production:
            _refuse_before_app(before_app, "force-production")
            return self._force_production_update(production_image,
                                                 progress_callback)
        state, source = self.detect()
        in_dfu = source == "dfu"
        if state != COHORT_SECURE:
            _refuse_before_app(before_app, state)
        if state == COHORT_NO_DFU:
            raise RuntimeError(self._no_dfu_message())
        if state == COHORT_LEGACY and in_dfu:
            # Dead app: the ROM loader is unreachable, so the bootloader has
            # to replace itself from the legacy app slot.
            return self._migrate_legacy_usb(signed_app, updater_bin,
                                            progress_callback)

        if state in (COHORT_NONE, COHORT_LEGACY):
            prod = _require_image(production_image, "production image (bootloader + signed app)", "production_image= / --production")
            # COHORT_NONE (<= 2.0.3): the app ignores the reserved byte and a
            # plain DFU entry reboots straight into the STM32 ROM loader.
            # COHORT_LEGACY (2.0.4 - 2.0.7): a plain entry would land in the
            # LEGACY bootloader's DFU, so use the reserved=0x77 force switch.
            enter = self._enter_dfu if state == COHORT_NONE else self._enter_rom
            self._mgr.migrate_transmitter_full_image(
                prod,
                enter_stm32_rom_dfu_fn=None if in_dfu else enter,
                keys_dir=self.keys_dir, vid=self.vid, pid=self.pid,
                libusb_dll=self.libusb_dll,
                progress_callback=progress_callback)
            # The ROM DFU driver leaves DFU after the verified write, so the
            # secure bootloader boots the new app without a power-cycle.
            return UpdateResult(state, "migrate-rom",
                                "Migrated pre-secure transmitter to the "
                                "secure bootloader (full production image); "
                                "the unit is rebooting into the new app.",
                                reboot_required=False)

        app = _require_image(signed_app, "signed application image", "signed_app= / --app")
        return self._update_secure(state, app, in_dfu, force, bl_updater,
                                   progress_callback, replace_bootloader, bl_target,
                                   before_app)

    def _update_secure(self, state: str, app: str, in_dfu: bool, force: bool,
                       bl_updater: str | None,
                       progress_callback: Callable | None,
                       replace_bootloader: bool = False,
                       bl_target: str | None = None,
                       before_app: Callable[[DfuContext], None] | None = None
                       ) -> UpdateResult:
        """The secure-bootloader path of :meth:`update`: the signed app over
        the master's USB DFU, replacing a 1.0.x bootloader through the updater
        shim first."""
        if not in_dfu:
            logger.info("Requesting transmitter DFU mode...")
            self._enter_dfu()
            time.sleep(2.0)
        mgr, kw = self._mgr, dict(vid=self.vid, pid=self.pid,
                                  libusb_dll=self.libusb_dll)

        def get_bl(t: float) -> str:
            return mgr._wait_for_dfu_kind(DFU_KIND_SECURE, timeout_s=t, **kw)

        ctx = DfuContext("transmitter master", 0, mgr, wait_dfu=_usb_wait_dfu(get_bl), **kw)
        bl_version, migrated = _install_with_migration(
            "Transmitter",
            get_bl=get_bl,
            program=lambda img: mgr.program_transmitter(
                img, keys_dir=self.keys_dir, force=force,
                progress_callback=progress_callback, **kw),
            abort=lambda: mgr.abort_dfu(profile=TRANSMITTER_PROFILE, **kw),
            app=app,
            bl_updater=lambda bl: _resolve_bl_updater(
                bl_updater, "Transmitter", bl),
            entered=not in_dfu,
            slot_empty=lambda: mgr.get_transmitter_installed_version(**kw) is None,
            replace=replace_bootloader, bl_target=bl_target,
            before_app=(lambda: before_app(ctx)) if before_app else None)
        # The manifest resets the master and the bootloader verifies and
        # launches the new application: no power-cycle.
        if migrated:
            return UpdateResult(state, "bl-migrate",
                                f"Replaced the transmitter bootloader (now "
                                f"{bl_version}) with the bootloader updater, "
                                "then installed the application; the unit is "
                                "rebooting into the new app.",
                                reboot_required=False, bl_version=bl_version)
        return UpdateResult(state, "app-update",
                            "Updated the transmitter application on the "
                            "secure bootloader; the unit is rebooting into "
                            "the new app.", reboot_required=False,
                            bl_version=bl_version)

    def slave_module_version(self, module: int) -> tuple[str, int]:
        """Return ``(app_version, mode)`` for a slave module as the master
        currently sees it.

        Does NOT re-enumerate: on transmitter firmware up to 2.1.0-rc.4 a
        runtime re-enumeration hangs every slave that is running its
        application (openlifu-transmitter-fw#80), and fielded units run it.
        """
        from openlifu_sdk.io.LIFUConfig import NODE_MODE_UNKNOWN
        tx = self._require_tx()
        try:
            mode = tx.get_module_mode(module)
        except Exception:
            mode = NODE_MODE_UNKNOWN
        try:
            ver = str(tx.get_version(module=module))
        except Exception:
            ver = ""
        return ver, mode

    def update_slave(self, module: int, *, signed_app: str | None = None,
                     production_image: str | None = None,
                     legacy: bool | None = None, stub_bin: str | None = None,
                     force_production: bool = False,
                     bl_updater: str | None = None,
                     replace_bootloader: bool = False,
                     dfu_wait_s: float = 6.0,
                     progress_callback: Callable | None = None,
                     bl_target: str | None = None,
                     before_app: Callable[[DfuContext], None] | None = None
                     ) -> UpdateResult:
        """Update a SLAVE transmitter module (index >= 1) over I2C, through
        the master. The master brokers everything on the one-wire + I2C buses.

        Two paths, chosen by the slave's bootloader generation:

          - **secure** (app >= 2.0.8): stream the SFU1 signed app to the
            slave's secure bootloader over I2C.
          - **legacy** (app 2.0.4 - 2.0.7): one-shot migration. Write the
            small RAM-resident DFU stub (+ its HMAC-trust-tagged WFM1
            metadata) over the LEGACY I2C DFU; the legacy BL boots the stub,
            which serves a FULL-FLASH I2C DFU from RAM; then stream the whole
            production image (secure bootloader + signed app) to 0x08000000
            in one pass. Bootloader and application are updated together.

        With ``force_production=True`` a SECURE-bootloader slave is given the
        same full-image treatment — the path for rolling a NEW BOOTLOADER
        onto an already-migrated slave: the SIGNED build of the DFU stub is
        installed through the secure I2C DFU like a normal app (the
        bootloader verifies it at boot), the stub opens the full-flash DFU,
        and the whole production image is streamed. A legacy slave with
        ``force_production=True`` just takes the normal legacy path (which
        already reflashes the bootloader).

        Slaves on app 2.0.2 - 2.0.3 (no bootloader at all) CANNOT be updated
        over I2C — their DFU entry jumps to the STM32 ROM loader, which does
        not speak this I2C protocol. Auto-detect raises a clear error for
        them: connect such a module as the USB master and update it as module
        0. Slaves below 2.0.2 have no DFU entry at all and are refused
        outright — BOOT0 (ROM loader over USB) or SWD only.

        Args:
            module: slave module index (>= 1). Module 0 is the USB master —
                use :meth:`update`.
            signed_app: signed app image (secure path).
            production_image: combined bootloader+app image (legacy /
                force-production paths).
            legacy: force the legacy-migration path (True) or the secure path
                (False); ``None`` auto-detects from the slave's app version.
            stub_bin: the RAM-resident DFU stub (legacy path: raw build;
                force-production path: SFU1-signed build).
            force_production: reflash bootloader + app on a SECURE slave via
                the signed DFU stub (see above). Beta/unlocked units only.
            dfu_wait_s: seconds to wait for a slave BL to come up on I2C.
            bl_updater, replace_bootloader, bl_target: as in :meth:`update`.
            before_app: Called with a :class:`DfuContext` (``mgr``,
                ``i2c_addr``, ``reacquire``) in the new bootloader's I2C DFU,
                after the bootloader was replaced and before the application
                is written. Not called when the bootloader is kept. Secure
                path only: the legacy and force-production paths refuse it
                before anything is written.

        Raises:
            ValueError: module < 1, or a bad image.
            RuntimeError: master can't see/reach the module, DFU entry failed,
                or an I2C programming failure.
        """
        self._require_tx()
        if module < 1:
            raise ValueError(
                "update_slave targets slave modules (>= 1); use update() for "
                "the master (module 0).")

        if legacy is None:
            # A slave already parked in its bootloader has no application to
            # ask; the secure path installs straight into its DFU.
            legacy = (False if self._slave_in_bootloader(module)
                      else self._slave_path_is_legacy(module))

        if legacy or force_production:
            _refuse_before_app(before_app, "legacy" if legacy else "force-production")
        if legacy:
            return self._update_slave_legacy(module, production_image,
                                             stub_bin, dfu_wait_s,
                                             progress_callback)
        if force_production:
            return self._update_slave_force_production(
                module, production_image, stub_bin, dfu_wait_s,
                progress_callback)
        return self._update_slave_secure(module, signed_app, dfu_wait_s,
                                         progress_callback, bl_updater,
                                         replace_bootloader, bl_target, before_app)

    def _slave_path_is_legacy(self, module: int) -> bool:
        """Choose the slave's update path from its running app version:
        ``True`` for the legacy one-shot migration (app 2.0.4 - 2.0.7),
        ``False`` for the secure I2C app update (>= 2.0.8).

        Raises RuntimeError for the two cohorts that cannot be reached over
        the master's I2C passthrough at all — apps 2.0.2 - 2.0.3 (no
        bootloader: DFU entry lands in the STM32 ROM loader, which does not
        speak this protocol) and apps < 2.0.2 (no OW_CMD_DFU handler, so no
        DFU mode is reachable from software at all) — and when the version
        cannot be read to make the choice.
        """
        tx = self._require_tx()
        try:
            ver = str(tx.get_version(module=module))
        except Exception as e:
            raise RuntimeError(
                f"could not read slave {module} version to choose the "
                f"update path ({e}); pass legacy=True or legacy=False.")
        cohort = infer_transmitter_bootloader_from_app_version(ver)
        if cohort == COHORT_NO_DFU:
            floor = ".".join(str(p) for p in TRANSMITTER_MIN_DFU_APP_VERSION)
            raise RuntimeError(
                f"slave module {module} runs app {ver}, older than {floor}: "
                "those builds have no OW_CMD_DFU handler, so the master "
                "cannot put the module into ANY DFU mode. It is not updatable "
                "over I2C or as a USB master — enter the STM32 ROM loader in "
                "hardware (BOOT0 at power-up) with the module connected over "
                "USB and update it as module 0, or program it over SWD.")
        if cohort == COHORT_NONE:
            raise RuntimeError(
                f"slave module {module} runs app {ver} (2.0.2 - 2.0.3): it has "
                "NO bootloader, and its DFU entry jumps to the STM32 ROM "
                "loader, which cannot be reached over the master's I2C "
                "passthrough. Connect that module as the USB master and update "
                "it as module 0 (full production image via ROM DFU).")
        legacy = cohort == COHORT_LEGACY
        logger.info("Slave %d app %s -> %s bootloader", module, ver,
                    "legacy" if legacy else "secure")
        return legacy

    def _slave_enter_dfu(self, module: int, dfu_wait_s: float) -> int:
        """Confirm the master sees *module*, put it into its bootloader's I2C
        DFU, re-enumerate, and return its assigned I2C address. Raises if the
        module isn't seen or didn't enter DFU."""
        from openlifu_sdk.io.LIFUConfig import NODE_MODE_BOOTLOADER
        tx = self._require_tx()
        count = tx.get_module_count()
        logger.info("Master enumerates %d module(s)", count)
        if module >= count:
            raise RuntimeError(
                f"master does not see module {module} (only {count} "
                "enumerated). Ensure the slave is powered and on the one-wire "
                "chain, then reset the master.")
        logger.info("Requesting DFU on slave module %d...", module)
        tx.enter_dfu(module=module)
        logger.info("Waiting %.0fs for the slave bootloader to start I2C DFU...",
                    dfu_wait_s)
        time.sleep(dfu_wait_s)
        tx.enumerate_modules()
        mode = tx.get_module_mode(module)
        if mode != NODE_MODE_BOOTLOADER:
            raise RuntimeError(
                f"module {module} reports mode {mode}, expected BOOTLOADER "
                f"({NODE_MODE_BOOTLOADER}) — the slave did not enter DFU.")
        addr = tx.get_module_i2c_addr(module)
        logger.info("Slave module %d is in bootloader DFU at I2C 0x%02X",
                    module, addr)
        return addr

    def _slave_post_boot_version(self, module: int, dfu_wait_s: float) -> str:
        """Wait for the slave app to boot and read its version back (best
        effort, never raises). Returns the version, or ``""`` when no valid
        one was read.

        Enumerates ONCE, then retries only the version read (see
        :data:`SLAVE_VERSION_TIMEOUT_S`) until it is a real X.Y.Z version.
        """
        tx = self._require_tx()
        logger.info("Waiting %.0fs for the slave app to boot...", dfu_wait_s)
        time.sleep(dfu_wait_s)
        try:
            _reopen(tx)
            tx.enumerate_modules()
        except Exception as e:
            logger.warning("Slave %d post-update re-enumeration failed (%s) — "
                           "it may still be verifying/booting", module, e)
            return ""
        deadline = time.monotonic() + SLAVE_VERSION_TIMEOUT_S
        while True:
            try:
                ver = str(tx.get_version(module=module))
            except Exception as e:
                ver = f"<{e}>"
            if _is_real_version(ver):
                logger.info("Slave module %d app version: %s", module, ver)
                return ver
            if time.monotonic() >= deadline:
                break
            time.sleep(SLAVE_VERSION_POLL_S)
        logger.warning("Slave %d did not report a valid app version within "
                       "%.0f s (last reply: %s) — it may still be booting, or "
                       "the master firmware (2.0.8) truncates forwarded "
                       "version replies", module, SLAVE_VERSION_TIMEOUT_S, ver)
        return ""

    def _update_slave_secure(self, module: int, signed_app: str | None,
                             dfu_wait_s: float,
                             progress_callback: Callable | None,
                             bl_updater: str | None = None,
                             replace_bootloader: bool = False,
                             bl_target: str | None = None,
                             before_app: Callable[[DfuContext], None] | None = None
                             ) -> UpdateResult:
        tx = self._require_tx()
        app = _require_image(signed_app, "signed application image", "signed_app= / --app")
        if self._slave_in_bootloader(module):
            i2c_addr = tx.get_module_i2c_addr(module)
            logger.info("Slave %d is already in its bootloader's I2C DFU at 0x%02X",
                        module, i2c_addr)
        else:
            i2c_addr = self._slave_enter_dfu(module, dfu_wait_s)
        mgr = LIFUDFUManager(uart=tx.uart)
        bl = STM32I2CDFUviaMaster(tx.uart, i2c_addr=i2c_addr).get_version()
        logger.info("Slave %d bootloader version: %s", module, bl)
        migrated = replace_bootloader or bootloader_needs_migration(bl, bl_target)
        if migrated:
            shim = _resolve_bl_updater(bl_updater, f"Slave {module}", bl)
            logger.info("Slave %d bootloader %s is older than %s: installing "
                        "the bootloader updater first (%s)", module, bl,
                        _migration_floor(bl_target), Path(shim).name)
            mgr.program_transmitter_slave_i2c(
                shim, i2c_addr=i2c_addr, keys_dir=self.keys_dir,
                progress_callback=progress_callback)
            i2c_addr, new_bl = self._slave_wait_migrated_bootloader(
                module, bl, dfu_wait_s, bl_target)
            logger.info("Slave %d bootloader replaced: %s -> %s; installing "
                        "the application", module, bl, new_bl)
            if before_app is not None:
                i2c_addr = self._slave_before_app(module, mgr, i2c_addr, before_app)
        bl_version = mgr.program_transmitter_slave_i2c(
            app, i2c_addr=i2c_addr, keys_dir=self.keys_dir,
            progress_callback=progress_callback)
        app_version = self._slave_post_boot_version(module, dfu_wait_s)
        summary = (f"Updated slave transmitter module {module} over I2C"
                   + (f" (bootloader replaced: {bl} -> {bl_version})" if migrated else "")
                   + (f" (now {app_version})" if app_version else "."))
        return UpdateResult(COHORT_SECURE,
                            "bl-migrate-i2c" if migrated else "app-update-i2c",
                            summary, reboot_required=False, bl_version=bl_version)

    def _slave_wait_migrated_bootloader(self, module: int, old_bl: str,
                                        dfu_wait_s: float,
                                        bl_target: str | None = None) -> tuple[int, str]:
        """After the shim was installed on a slave, wait for the NEW
        bootloader's I2C DFU. Returns ``(i2c_addr, version)``.

        The I2C DFU has no read command, so this goes by the bootloader's
        version: a 1.0.x bootloader replaced by 1.1.x shows. With
        ``replace_bootloader`` (same version, other key) the version does not
        tell them apart; the application written next does: a bootloader
        refuses an image not signed with its key, so if the updater was
        refused that write fails. Waits :data:`SLAVE_MIGRATION_SETTLE_S`
        before the first re-enumeration and polls every
        :data:`SLAVE_MIGRATION_POLL_S` (see there), reopening the master's
        port if it was lost.
        """
        logger.info("Slave %d: waiting %.0f s for the updater to replace the "
                    "bootloader", module, SLAVE_MIGRATION_SETTLE_S)
        addr, seen = self._slave_wait_bootloader(
            module, max(SLAVE_MIGRATION_SETTLE_S, dfu_wait_s),
            lambda v: not bootloader_needs_migration(v, bl_target))
        if addr is not None:
            return addr, seen
        seen = seen or old_bl
        raise RuntimeError(
            f"slave module {module}: the bootloader updater was installed on "
            f"bootloader {old_bl} but no newer bootloader came up within "
            f"{MIGRATION_TIMEOUT_S:.0f} s (last seen: {seen}). If the slave is "
            f"back in bootloader {old_bl}, the updater refused it (protected "
            "unit) and nothing was changed; if it no longer answers, recover "
            "it over SWD.")

    def _slave_wait_bootloader(self, module: int, settle_s: float,
                               accept: Callable[[str], bool]
                               ) -> tuple[int | None, str | None]:
        """Wait *settle_s*, then re-enumerate every :data:`SLAVE_MIGRATION_POLL_S`
        (reopening a lost master port) until *module* is a bootloader node whose
        I2C DFU version *accept* takes, for at most :data:`MIGRATION_TIMEOUT_S`.
        Returns ``(i2c_addr, version)``, or ``(None, last version seen)`` at the
        deadline."""
        from openlifu_sdk.io.LIFUConfig import NODE_MODE_BOOTLOADER
        tx = self._require_tx()
        deadline = time.monotonic() + MIGRATION_TIMEOUT_S
        seen = None
        time.sleep(settle_s)
        while True:
            try:
                _reopen(tx)
                count = tx.enumerate_modules()
                if module < count and tx.get_module_mode(module) == NODE_MODE_BOOTLOADER:
                    addr = tx.get_module_i2c_addr(module)
                    seen = STM32I2CDFUviaMaster(tx.uart, i2c_addr=addr).get_version()
                    if accept(seen):
                        return addr, seen
            except Exception as e:  # noqa: BLE001 - keep polling until the deadline
                logger.info("Slave %d not back yet (%s)", module, e)
            if time.monotonic() + SLAVE_MIGRATION_POLL_S > deadline:
                return None, seen
            time.sleep(SLAVE_MIGRATION_POLL_S)

    def _slave_before_app(self, module: int, mgr: Any, i2c_addr: int,
                          before_app: Callable[[DfuContext], None]) -> int:
        """Run *before_app* in the slave's new bootloader DFU; returns the I2C
        address the slave has when it returns (a reset re-assigns it)."""
        ctx = DfuContext(f"transmitter module {module}", module, mgr, i2c_addr=i2c_addr)

        def reacquire() -> int:
            addr, seen = self._slave_wait_bootloader(module, SLAVE_PRESET_SETTLE_S,
                                                     lambda _v: True)
            if addr is None:
                raise RuntimeError(
                    f"slave module {module} did not come back in its bootloader's "
                    f"I2C DFU within {MIGRATION_TIMEOUT_S:.0f} s after a reset "
                    f"(last seen: {seen}); its application slot is empty, so it "
                    "stays in DFU: re-run the update")
            ctx.i2c_addr = addr
            return addr

        ctx.reacquire = reacquire
        before_app(ctx)
        return ctx.i2c_addr

    def _slave_in_bootloader(self, module: int) -> bool:
        """True when the master already lists *module* as a bootloader node
        (parked in its I2C DFU: an interrupted update, or a migration whose
        application was never installed)."""
        from openlifu_sdk.io.LIFUConfig import NODE_MODE_BOOTLOADER
        try:
            return self._require_tx().get_module_mode(module) == NODE_MODE_BOOTLOADER
        except Exception:  # noqa: BLE001 - unknown: take the normal path
            return False

    def update_chain(self, *, signed_app: str | None = None,
                     bl_updater: str | None = None, replace_bootloader: bool = False,
                     dfu_wait_s: float = 6.0,
                     progress_callback: Callable | None = None,
                     bl_target: str | None = None,
                     before_app: Callable[[DfuContext], None] | None = None
                     ) -> list[UpdateResult]:
        """Update every module of the chain: each slave over I2C first, while
        the master still runs the firmware that brokers it, then the master
        over USB. Old bootloaders are replaced on the way (see
        :func:`bootloader_needs_migration`).

        Note: updating a slave re-enumerates the chain once it is in its
        bootloader. On firmware up to 2.1.0-rc.4 that hangs any OTHER slave
        still running its application (openlifu-transmitter-fw#80); it comes
        back after its watchdog reset and is picked up by the next
        re-enumeration, which the next slave's update does.

        *before_app* is passed to every module's update (its
        :class:`DfuContext` names the module).

        Returns:
            One :class:`UpdateResult` per module, slaves first, master last.
        """
        tx = self._require_tx()
        count = tx.get_module_count()
        results = [self.update_slave(m, signed_app=signed_app,
                                     bl_updater=bl_updater,
                                     replace_bootloader=replace_bootloader,
                                     dfu_wait_s=dfu_wait_s,
                                     progress_callback=progress_callback,
                                     bl_target=bl_target, before_app=before_app)
                   for m in range(1, count)]
        results.append(self.update(signed_app=signed_app, bl_updater=bl_updater,
                                   replace_bootloader=replace_bootloader,
                                   progress_callback=progress_callback,
                                   bl_target=bl_target, before_app=before_app))
        return results

    def _update_slave_legacy(self, module: int, production_image: str | None,
                             stub_bin: str | None, dfu_wait_s: float,
                             progress_callback: Callable | None) -> UpdateResult:
        """One-shot legacy migration: the stub is written through the LEGACY
        I2C DFU like any app (its WFM1 metadata is HMAC-trust-tagged on the
        fly); when the legacy BL boots it, it serves a full-flash I2C DFU
        from RAM and the whole production image (secure bootloader + signed
        app) is streamed to the flash base in one pass."""
        tx = self._require_tx()
        prod = _require_image(production_image, "production image (bootloader + signed app)", "production_image= / --production")
        stub = _require_image(stub_bin, "DFU stub image", "stub_bin=")
        mgr = LIFUDFUManager(uart=tx.uart)

        # Phase 1: put the slave in its LEGACY bootloader's I2C DFU. Unlike
        # the secure BL, the legacy BL has NO one-wire back-channel: after DFU
        # entry the master's enumeration cannot see the module or report its
        # mode, and no address is assigned — the legacy BL just listens at the
        # fixed default DFU address (0x72). So confirm the module while the
        # app still runs, request DFU, then poll 0x72 directly.
        logger.info("Slave %d: one-shot legacy migration (DFU stub + full "
                    "production image) over I2C", module)
        count = tx.get_module_count()
        logger.info("Master enumerates %d module(s)", count)
        if module >= count:
            raise RuntimeError(
                f"master does not see module {module} (only {count} "
                "enumerated). Ensure the slave is powered and on the one-wire "
                "chain, then reset the master.")
        logger.info("Requesting DFU on slave module %d...", module)
        tx.enter_dfu(module=module)
        logger.info("Waiting %.0fs for the legacy bootloader to start I2C "
                    "DFU at 0x%02X...", dfu_wait_s, I2C_DFU_SLAVE_ADDR)
        time.sleep(dfu_wait_s)
        legacy_ver = mgr.wait_transmitter_slave_dfu(timeout_s=30.0)
        logger.info("Legacy slave BL DFU is up (%s); writing the stub...",
                    legacy_ver)

        # Write the DFU stub + WFM1 metadata over the LEGACY I2C DFU; the
        # reset boots the stub (validated by the legacy BL's trust tag).
        legacy_bl = mgr.program_transmitter_slave_legacy_i2c(
            stub, i2c_addr=I2C_DFU_SLAVE_ADDR,
            progress_callback=progress_callback)
        logger.info("Legacy slave BL was %s; DFU stub written.", legacy_bl)

        # Phase 2: the stub has no one-wire back-channel, so it listens at the
        # DEFAULT DFU address (0x72) — poll it directly until it answers.
        logger.info("Waiting for the RAM-resident DFU stub to come up...")
        stub_version = mgr.wait_transmitter_slave_stub(timeout_s=30.0)
        logger.info("DFU stub is up (%s); streaming the production image "
                    "(bootloader + app)...", stub_version)

        # Phase 3: full-chip erase + write the production image at 0x08000000.
        # From here until the write completes the slave must stay powered.
        mgr.program_transmitter_slave_production_i2c(
            prod, keys_dir=self.keys_dir, progress_callback=progress_callback)

        app_version = self._slave_post_boot_version(module, dfu_wait_s)
        summary = (f"Migrated slave transmitter module {module} to the secure "
                   f"bootloader and app in one shot over I2C"
                   + (f" (now {app_version})" if app_version else "."))
        return UpdateResult(COHORT_SECURE, "migrate-legacy-i2c", summary,
                            reboot_required=False, bl_version=legacy_bl)

    def _update_slave_force_production(self, module: int,
                                       production_image: str | None,
                                       stub_bin: str | None,
                                       dfu_wait_s: float,
                                       progress_callback: Callable | None
                                       ) -> UpdateResult:
        """Force a FULL production reflash (bootloader + app) on a
        SECURE-bootloader slave — the path for rolling a NEW BOOTLOADER onto
        an already-migrated slave over I2C.

        The raw stub cannot be used here (the secure BL only boots
        SFU1-signed images), so the SIGNED stub build is installed through
        the secure I2C DFU like a normal app update; the BL verifies it at
        boot and launches it, the stub opens its full-flash I2C DFU at 0x72,
        and the whole production image is streamed to 0x08000000. The signed
        stub must carry the production app's FwVersion, so the
        anti-rollback floor is not disturbed (boot check is version >=
        floor). Beta/unlocked units only.
        """
        tx = self._require_tx()
        prod = _require_image(production_image, "production image (bootloader + signed app)", "production_image= / --production")
        stub = _require_image(stub_bin, "signed DFU stub image", "stub_bin=")
        mgr = LIFUDFUManager(uart=tx.uart)

        # Phase 1: install the SIGNED stub into the slot via the secure BL's
        # I2C DFU (enumerated address); the reset boots it after signature
        # verification.
        logger.info("Slave %d: FORCE-PRODUCTION reflash (signed DFU stub + "
                    "full production image) over I2C", module)
        addr = self._slave_enter_dfu(module, dfu_wait_s)
        secure_bl = mgr.program_transmitter_slave_i2c(
            stub, i2c_addr=addr, keys_dir=self.keys_dir,
            progress_callback=progress_callback)
        logger.info("Secure slave BL was %s; signed DFU stub installed.",
                    secure_bl)

        # Phase 2: the stub listens at the default DFU address (0x72).
        logger.info("Waiting for the RAM-resident DFU stub to come up...")
        stub_version = mgr.wait_transmitter_slave_stub(timeout_s=45.0)
        logger.info("DFU stub is up (%s); streaming the production image "
                    "(bootloader + app)...", stub_version)

        # Phase 3: full-chip erase + write the production image. If this
        # phase is aborted the stub keeps serving DFU from RAM (retry while
        # powered); if the unit is instead power-cycled BEFORE the write, the
        # secure BL just boots the stub again — rerun via wait + program.
        mgr.program_transmitter_slave_production_i2c(
            prod, keys_dir=self.keys_dir, progress_callback=progress_callback)

        app_version = self._slave_post_boot_version(module, dfu_wait_s)
        summary = (f"Reflashed slave transmitter module {module} with the "
                   f"full production image (bootloader + app) over I2C"
                   + (f" (now {app_version})" if app_version else "."))
        return UpdateResult(COHORT_SECURE, "force-production-i2c", summary,
                            reboot_required=False, bl_version=secure_bl)

    def _require_tx(self):
        if self.tx is None:
            raise RuntimeError(
                "a TxDevice is required to trigger DFU entry (or put the "
                "unit in DFU first and re-run)")
        return self.tx

    def _enter_dfu(self) -> None:
        _request_dfu(lambda: self._require_tx().enter_dfu(module=0), "Transmitter")

    def _enter_rom(self) -> None:
        _request_dfu(lambda: self._require_tx().enter_stm32_rom_dfu(module=0), "Transmitter")


# ---------------------------------------------------------------------------
# CLI:  python -m openlifu_sdk.io.LIFUFirmwareUpdate [options]
# ---------------------------------------------------------------------------

def _progress(written: int, total: int, label: str) -> None:
    pct = 100 * written // total if total else 100
    print(f"\r  {label}: {written:,}/{total:,} ({pct}%)", end="", flush=True)


def _confirm(label: str, yes: bool) -> bool:
    if yes:
        return True
    try:
        resp = input(f"Proceed with the update for '{label}'? [y/N] ").strip().lower()
    except EOFError:
        resp = ""
    return resp in ("y", "yes")


def _run_update_flow(fw: Any, label: str, detect_fn: Callable,
                     update_fn: Callable, args: Any) -> int:
    """Shared CLI flow for both devices: detect the unit's state, service the
    query-only flags (--detect / --bl-version), then confirm and update."""
    try:
        state, source = detect_fn()
    except RuntimeError as e:
        print(f"Could not determine {label} state: {e}")
        return 1
    print(f"{label.capitalize()} state: {state} (from {source})")
    if args.detect:
        return 0
    # Refuse before prompting: nothing here can reach a DFU mode (transmitter
    # app < 2.0.2), so there is no update to confirm.
    if state == COHORT_NO_DFU:
        print(f"CANNOT UPDATE: {fw._no_dfu_message()}")
        return 1
    if args.bl_version:
        try:
            print(f"Bootloader version: {fw.get_bootloader_version()}")
        except RuntimeError as e:
            print(f"Could not read bootloader version: {e}")
            return 1
        return 0

    confirm_label = state
    if args.force_production:
        confirm_label = f"{state} — FULL production reflash (bootloader + app)"
    if not _confirm(confirm_label, args.yes):
        print("Aborted.")
        return 1

    try:
        result = update_fn()
    except (ValueError, RuntimeError) as e:
        print(f"\nUPDATE FAILED: {e}")
        return 1

    print(f"\n{result.summary}")
    if result.bl_version:
        print(f"Bootloader version: {result.bl_version}")
    if result.reboot_required:
        print(f"Power-cycle the {label} to boot the new application.")
    return 0


def _run_transmitter(args: Any) -> int:
    from openlifu_sdk.io.LIFUInterface import LIFUInterface

    # A running app is preferred (it lets us trigger DFU entry), but a unit
    # already sitting in the secure bootloader's DFU works too.
    tx = None
    try:
        interface = LIFUInterface(TX_test_mode=False)
        tx_connected, _hv = interface.is_device_connected()
        if tx_connected:
            interface.txdevice.ping()
            tx = interface.txdevice
    except Exception as e:
        logger.info("Transmitter app connection failed (%s); the unit "
                    "must already be in USB DFU mode", e)
    if tx is None:
        print("Transmitter app not connected — checking for a unit "
              "already in USB DFU mode.")

    fw = LIFUTransmitterFirmwareUpdate(tx=tx, keys_dir=args.keys)
    if args.all:
        return _run_transmitter_chain(fw, args)
    if args.module and args.module > 0:
        return _run_transmitter_slave(fw, args)
    return _run_update_flow(
        fw, "transmitter", fw.detect,
        lambda: fw.update(signed_app=args.app,
                          production_image=args.production,
                          updater_bin=args.updater, force=args.force,
                          force_production=args.force_production,
                          bl_updater=args.bl_updater,
                          replace_bootloader=args.replace_bootloader,
                          progress_callback=_progress),
        args)


def _run_transmitter_chain(fw: Any, args: Any) -> int:
    """CLI flow for --all: every slave over I2C, then the master."""
    if fw.tx is None:
        print("--all needs the transmitter application running on the master.")
        return 1
    count = fw.tx.get_module_count()
    if not _confirm(f"all {count} transmitter module(s): slaves 1-{count - 1} "
                    "over I2C, then the master", args.yes):
        print("Aborted.")
        return 1
    try:
        results = fw.update_chain(signed_app=args.app, bl_updater=args.bl_updater,
                                  replace_bootloader=args.replace_bootloader,
                                  progress_callback=_progress)
    except (ValueError, RuntimeError) as e:
        print(f"\nUPDATE FAILED: {e}")
        return 1
    for r in results:
        print(f"\n{r.summary}")
    return 0


def _run_transmitter_slave(fw: Any, args: Any) -> int:
    """CLI flow for updating a SLAVE transmitter module (>= 1) over I2C."""
    module = args.module
    label = f"transmitter slave module {module}"
    if args.detect or args.bl_version:
        try:
            ver, mode = fw.slave_module_version(module)
        except (RuntimeError, ValueError) as e:
            print(f"Could not query {label}: {e}")
            return 1
        print(f"{label}: app version {ver or 'unknown'} (mode {mode})")
        return 0
    legacy = True if args.legacy else None   # None = auto-detect
    if legacy:
        label += " — LEGACY one-shot migration (DFU stub + bootloader + app)"
    elif args.force_production:
        label += " — FULL production reflash (signed DFU stub + bootloader + app)"
    if not _confirm(label, args.yes):
        print("Aborted.")
        return 1
    try:
        result = fw.update_slave(module, signed_app=args.app,
                                 production_image=args.production,
                                 legacy=legacy,
                                 force_production=args.force_production,
                                 bl_updater=args.bl_updater,
                                 replace_bootloader=args.replace_bootloader,
                                 progress_callback=_progress)
    except (ValueError, RuntimeError) as e:
        print(f"\nUPDATE FAILED: {e}")
        return 1
    print(f"\n{result.summary}")
    if result.bl_version:
        print(f"Slave bootloader version: {result.bl_version}")
    return 0


def _run_console(args: Any) -> int:
    from openlifu_sdk.io.LIFUInterface import LIFUInterface

    # A running app is preferred (it lets us trigger DFU entry), but a unit
    # already sitting in one of the DFU environments works too — its USB
    # product string names the bootloader and picks the image.
    hvc = None
    try:
        interface = LIFUInterface(TX_test_mode=False)
        _tx, hv_connected = interface.is_device_connected()
        if hv_connected:
            interface.hvcontroller.ping()
            hvc = interface.hvcontroller
    except Exception as e:
        logger.info("Console app connection failed (%s); the unit must "
                    "already be in USB DFU mode", e)
    if hvc is None:
        print("Console app not connected — checking for a unit already in "
              "USB DFU mode.")

    fw = LIFUFirmwareUpdate(hv=hvc, keys_dir=args.keys)
    return _run_update_flow(
        fw, "console", fw.detect_cohort,
        lambda: fw.update(production_image=args.production, signed_app=args.app,
                          updater_bin=args.updater, force=args.force,
                          force_production=args.force_production,
                          bl_updater=args.bl_updater,
                          replace_bootloader=args.replace_bootloader,
                          progress_callback=_progress),
        args)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m openlifu_sdk.io.LIFUFirmwareUpdate",
        description="Detect the unit's state and update its firmware. "
                    "Console: no-bootloader / legacy / secure paths. "
                    "Transmitter: standard USB DFU on the secure bootloader. "
                    "The images come from the firmware releases (--app, --bl-updater, "
                    "--production); no signing keys are needed.")
    parser.add_argument("--device", choices=("console", "transmitter"),
                        default="console",
                        help="Which unit to update (default: console).")
    parser.add_argument("--module", type=int, default=0,
                        help="Transmitter only: module index. 0 (default) is "
                             "the USB master; >= 1 targets a SLAVE module, "
                             "updated over I2C through the master (specify "
                             "which slave when more than one is connected).")
    parser.add_argument("--detect", action="store_true",
                        help="Report the detected state and exit — no flashing.")
    parser.add_argument("--bl-version", action="store_true",
                        help="Report the bootloader (DFU) version and exit — "
                             "no flashing. If the app is running, the unit "
                             "round-trips through DFU and resets back into "
                             "the app (needs a bootloader with the reset "
                             "hook; older ones stay in DFU until "
                             "power-cycle).")
    parser.add_argument("--production",
                        help="Override the combined bootloader+app image "
                             "(migration paths).")
    parser.add_argument("--app", help="Override the signed app image.")
    parser.add_argument("--updater",
                        help="Override the RAM-resident legacy updater used "
                             "when a unit is parked in its LEGACY bootloader's "
                             "DFU with a dead app (console legacy migration; "
                             "transmitter USB legacy recovery).")
    parser.add_argument("--bl-updater",
                        help="Override the bootloader updater shim (signed "
                             "open-lifu-*-bl-updater image) installed first "
                             "on a secure bootloader older than 1.1.0. "
                             "Required when the bootloader is replaced.")
    parser.add_argument("--replace-bootloader", action="store_true",
                        help="Install the bootloader updater (--bl-updater) even "
                             "on a current bootloader: the bootloader it "
                             "carries replaces the installed one (e.g. a "
                             "research unit converted to clinical).")
    parser.add_argument("--all", action="store_true",
                        help="Transmitter only: update every module, slaves "
                             "over I2C first, then the master.")
    parser.add_argument("--keys", help="Optional keys dir to pre-validate the app signature.")
    parser.add_argument("--force", action="store_true",
                        help="Secure path only: flash even if not newer.")
    parser.add_argument("--legacy", action="store_true",
                        help="Transmitter slave only (--module >= 1): force the "
                             "legacy->secure ONE-SHOT migration path (write the "
                             "RAM-resident DFU stub over the legacy I2C DFU, "
                             "then stream the full production image — "
                             "bootloader + app — through the stub). Omit to "
                             "auto-detect from the slave's app version.")
    parser.add_argument("--force-production", action="store_true",
                        help="Force a FULL production reflash (bootloader + "
                             "app) regardless of the unit's state — the path "
                             "for installing a NEW bootloader. Module 0: the "
                             "running app is forced into the STM32 ROM "
                             "loader (OW_CMD_DFU reserved=0x77). Transmitter "
                             "slave (--module >= 1): the SIGNED DFU stub is "
                             "installed through the secure I2C DFU, then the "
                             "production image is streamed through its "
                             "full-flash DFU. Beta/unlocked units only: on "
                             "RDP/FDA-locked units the bootloader flash is "
                             "protected and these paths are inert.")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Skip the confirmation prompt.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.device == "transmitter":
        return _run_transmitter(args)
    return _run_console(args)


if __name__ == "__main__":
    raise SystemExit(main())
