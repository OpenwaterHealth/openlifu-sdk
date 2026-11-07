# SPDX-License-Identifier: Apache-2.0
"""High-level firmware update for the console and the transmitter.

**Console** (:class:`LIFUFirmwareUpdate`) — one entry point for every case. A
console unit is in one of four states, each needing a different update path:

  - **no DFU** (app < 1.1.0)            → REFUSED: the DFU request never
    reaches a DFU environment, so no software update path exists.
  - **no bootloader** (1.1.0–1.2.2)     → a plain DFU request reboots into the
    STM32 ROM DFU; write the combined bootloader+app production image.
  - **legacy bootloader** (1.2.3–1.2.5) → REFUSED: its DFU can write only
    the app slot, and the RAM-resident self-updater that used to replace the
    bootloader from there needed the legacy bootloader's shared HMAC key,
    which this SDK no longer carries (SR-021). Recover with BOOT0 (STM32 ROM
    DFU, then re-run) or SWD; a beta (unlocked) unit whose app runs can
    instead take ``force_production`` (STM32 ROM loader, no key).
  - **secure bootloader** (≥ 1.2.6)     → normal signed-app update.

:class:`LIFUFirmwareUpdate` auto-detects the state (from the running app
version, or the current DFU environment) and runs the right path. It defaults
to the firmware images bundled with the SDK and needs **no signing keys**:
the secure bootloader verifies the app at boot. An optional ``keys_dir``
only adds an ECDSA app-signature pre-check before flashing.

Typical use::

    from openlifu_sdk.io.LIFUInterface import LIFUInterface
    from openlifu_sdk.io.LIFUFirmwareUpdate import LIFUFirmwareUpdate

    interface = LIFUInterface(TX_test_mode=False)
    fw = LIFUFirmwareUpdate(hv=interface.hvcontroller)
    result = fw.update()          # detect + update; uses bundled images
    print(result.summary)

**Transmitter** (:class:`LIFUTransmitterFirmwareUpdate`) — auto-detects the
module 0 (USB master) unit's state and updates it:

  - **secure bootloader** (app >= 2.0.8) → standard USB DFU: the signed app
    is written to the active slot and verified by the bootloader at boot.
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
    REFUSED: the ROM loader is out of reach without a running app, and the
    legacy DFU can write only the app slot. Recover with BOOT0 or SWD.

Slave modules (index >= 1) are updated over I2C through the master with
:meth:`LIFUTransmitterFirmwareUpdate.update_slave`: secure-bootloader slaves
get the signed app streamed to their slot. Legacy-bootloader slaves (app
2.0.4 - 2.0.7) are refused: the one-shot I2C migration needed the legacy
bootloader's shared HMAC key, which this SDK no longer carries (SR-021).
Connect such a module, and any on app 2.0.2 - 2.0.3 (no bootloader), as the
USB master and update it as module 0. Slaves below 2.0.2 cannot be updated
at all without BOOT0 or SWD.

No signing keys are needed::

    fw = LIFUTransmitterFirmwareUpdate(tx=interface.txdevice)
    result = fw.update()          # uses the bundled signed transmitter app
    print(result.summary)

NOTE: the console migration paths are for unlocked (beta) units only; after
RDP/FDA lockdown the force-ROM-DFU switch is inert and the bootloader is not
erasable.
"""

from __future__ import annotations

import logging
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
    LIFUDFUManager,
    TRANSMITTER_MIN_DFU_APP_VERSION,
    TRANSMITTER_PROFILE,
    infer_console_bootloader_from_app_version,
    infer_transmitter_bootloader_from_app_version,
)

logger = logging.getLogger(__name__)

_FW_DIR = Path(__file__).parent.parent / "firmware"

# Cohort constants (align with infer_console_bootloader_from_app_version)
COHORT_NONE = "no-bootloader"
COHORT_LEGACY = "legacy-bl"
COHORT_SECURE = "secure-bl"
# Transmitter only: app too old to enter ANY DFU mode (< 2.0.2 — no OW_CMD_DFU
# handler). Not updatable by this SDK; see LIFUDFU.DFU_KIND_NO_DFU.
COHORT_NO_DFU = DFU_KIND_NO_DFU


def bundled_production_image() -> Path:
    """Combined bootloader + signed-app image bundled with the SDK
    (no-bootloader migration source)."""
    return _FW_DIR / "openlifu-console-fw-production.bin"


def bundled_signed_app() -> Path:
    """Signed console app image bundled with the SDK (secure app update
    source)."""
    return _FW_DIR / "openlifu-console-fw-signed.bin"


def bundled_transmitter_signed_app() -> Path:
    """Signed transmitter app image bundled with the SDK (secure app update
    source for the transmitter's USB DFU path)."""
    return _FW_DIR / "openlifu-transmitter-fw-signed.bin"


def bundled_transmitter_production_image() -> Path:
    """Combined bootloader + signed-app transmitter image bundled with the
    SDK (migration source for pre-secure-bootloader units, app <= 2.0.7)."""
    return _FW_DIR / "openlifu-transmitter-fw-production.bin"


def bundled_transmitter_dfu_stub_signed() -> Path:
    """The SFU1-SIGNED build of the RAM-resident I2C DFU stub (source for the
    slave FORCE-PRODUCTION path — installed through the SECURE bootloader's
    I2C DFU like a normal app; signed with the same version as the bundled
    production app so the anti-rollback floor is untouched)."""
    return _FW_DIR / "openlifu-transmitter-dfu-stub-signed.bin"


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


def _no_legacy_path_message(device: str, where: str, *, app_running: bool = False) -> str:
    """Why a *device* on the LEGACY (non-secure) bootloader is refused.

    Its DFU can write only the app slot, never the bootloader's own flash.
    The paths that used to get round that wrote an interim image into the
    slot and authorized it with the legacy bootloader's HMAC trust tag, whose
    key is shared and public; SR-021 removed that key from the SDK, and with
    it every legacy-bootloader update path. With the app *running*, a beta
    (unlocked) unit can still be reflashed whole through the STM32 ROM
    loader (``force_production``), which needs no key.
    """
    rom = ("If it is a beta (unlocked) unit, --force-production reflashes the "
           "full production image through the STM32 ROM loader instead (run "
           "the ROM-DFU dwell check first). Otherwise recover" if app_running
           else "Recover")
    return (
        f"the {device} is on the LEGACY bootloader ({where}). This SDK has "
        "no legacy-bootloader update path: its DFU can write only the app "
        "slot, and the interim updaters that replaced the bootloader from "
        "there needed its shared HMAC trust key, which was removed (SR-021). "
        f"{rom} the unit in hardware: hold BOOT0 while power-cycling it, so it "
        "enumerates as 'STM32 BOOTLOADER', and re-run this command (the full "
        "production image is written from there); or program it over SWD.")


@dataclass
class UpdateResult:
    """Outcome of an update run."""

    cohort: str            # detected cohort / DFU kind driving the choice
    action: str            # "migrate-rom" | "force-production" | "app-update" | ...
    summary: str           # human-readable one-liner
    reboot_required: bool  # True if a power-cycle is needed to run the app
    bl_version: str | None = None  # bootloader version string (DFU virtual
                                   # version address), when the path read it


class LIFUFirmwareUpdate:
    """Auto-detecting console firmware updater covering every unit state.

    From a RUNNING application the app version names the cohort, a DFU request
    is issued, and whichever DFU environment the unit lands in gets the image
    that belongs to it:

      - **app >= 1.2.6** (current bootloader) → its own DFU → signed app.
      - **app 1.2.3 - 1.2.5** (legacy bootloader) → REFUSED: the legacy DFU
        can only write the app slot, and this SDK has no legacy migration
        (SR-021). BOOT0 or SWD.
      - **app 1.1.0 - 1.2.2** (no bootloader) → a plain DFU request reboots
        straight into the STM32 ROM DFU → combined production image.
      - **app < 1.1.0** → REFUSED: the DFU request never reaches a DFU
        environment, so there is no software update path (BOOT0 or SWD).

    A unit that is ALREADY in DFU is identified from its USB product string
    and takes the same image mapping without any DFU request: STM32 ROM DFU →
    production image, current bootloader DFU → signed app (legacy bootloader
    DFU → refused).

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
        touching flash. This is also the one path left for a LEGACY-bootloader
        console whose app runs (no key involved; see
        :meth:`LIFUDFUManager.dwell_rom_dfu_check`). One parked in the legacy
        DFU is refused: the switch needs the app.
        """
        cohort, source = self.detect_cohort()
        in_rom_dfu = source == "dfu" and cohort == COHORT_NONE
        if cohort == COHORT_NO_DFU:
            raise RuntimeError(self._no_dfu_message("reflashed"))
        if cohort == COHORT_LEGACY and source == "dfu":
            raise RuntimeError(_no_legacy_path_message("console", "its DFU"))
        if source == "dfu" and not in_rom_dfu:
            raise RuntimeError(
                "cannot force a production reflash from bootloader DFU — the "
                "ROM loader is entered from the RUNNING application "
                "(OW_CMD_DFU force switch). Boot the app first, then re-run.")
        prod = (str(production_image) if production_image
                else str(bundled_production_image()))
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
               signed_app: str | None = None,
               force: bool = False, force_production: bool = False,
               progress_callback: Callable | None = None) -> UpdateResult:
        """Detect the unit's state and run the appropriate update.

        The DFU environment the unit reaches decides the image: STM32 ROM DFU
        (app 1.1.0 - 1.2.2, entered with a plain DFU request) takes the
        combined production image; the current bootloader's DFU (app
        >= 1.2.6) takes the signed app alone. Apps below 1.1.0 are refused —
        they never reach a DFU environment — and so are legacy-bootloader
        units (app 1.2.3 - 1.2.5): this SDK has no legacy migration (SR-021).

        Args:
            production_image: Override the combined bootloader+app image
                (no-bootloader / force-production paths). Defaults to the
                bundled production image.
            signed_app: Override the signed app (secure path). Defaults to
                the bundled signed app.
            force: For the secure app-update path, flash even if the image
                version is below the installed one (still subject to the
                bootloader's anti-rollback floor at boot).
            force_production: Reflash the FULL production image (bootloader +
                app) via STM32 ROM DFU regardless of cohort — see
                :meth:`_force_production_update`. Beta/unlocked units only.

        Returns:
            :class:`UpdateResult`.

        Raises:
            ValueError: Bad image / downgrade refused.
            RuntimeError: State undetectable, a legacy-bootloader unit, wrong
                DFU environment, or a write/verify failure.
        """
        if force_production:
            return self._force_production_update(production_image,
                                                 progress_callback)
        cohort, source = self.detect_cohort()
        if cohort == COHORT_NO_DFU:
            raise RuntimeError(self._no_dfu_message())
        if cohort == COHORT_LEGACY:
            raise RuntimeError(_no_legacy_path_message(
                "console", "app 1.2.3 - 1.2.5" if source == "app" else "its DFU",
                app_running=source == "app"))
        prod = str(production_image) if production_image else str(bundled_production_image())
        app = str(signed_app) if signed_app else str(bundled_signed_app())
        # When already in DFU we must not trigger another DFU entry.
        in_dfu = source == "dfu"

        if cohort == COHORT_NONE:
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

        if cohort == COHORT_SECURE:
            bl_version = self._mgr.update_console(
                app, enter_dfu_fn=None if in_dfu else self._enter_dfu,
                keys_dir=self.keys_dir, force=force, vid=self.vid, pid=self.pid,
                libusb_dll=self.libusb_dll, progress_callback=progress_callback)
            return UpdateResult(cohort, "app-update",
                                "Updated the application on the secure "
                                "bootloader.", reboot_required=True,
                                bl_version=bl_version)

        raise RuntimeError(f"unhandled cohort: {cohort}")

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
        self._require_hv().enter_dfu()

    def _enter_rom(self) -> None:
        self._require_hv().enter_stm32_rom_dfu()


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
    and :meth:`update` REFUSES it: the legacy DFU can write only the app
    slot, and this SDK has no legacy-bootloader path (SR-021). BOOT0 or SWD.

    Slave modules (index >= 1) are updated over I2C through the master with
    :meth:`update_slave` — the master puts the slave into its bootloader's
    I2C DFU and the images are streamed over the master's I2C passthrough.
    Only secure-bootloader slaves are updated (signed app, or the signed
    DFU stub + production image with ``force_production``); LEGACY-bootloader
    slaves (app 2.0.4 - 2.0.7) are refused — update them as the USB master
    (module 0). Slaves on app 2.0.2 -
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
        it CANNOT be updated by this SDK — see :meth:`update`.

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

    def _legacy_dfu_where(self) -> str:
        """Name the legacy DFU environment :meth:`detect` found, for the
        refusal message."""
        ver = (self._last_dfu[1] if self._last_dfu else "") or "unknown version"
        return f"parked in its USB DFU, LIFU BL DFU {ver}, with a dead app"

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
            raise RuntimeError(_no_legacy_path_message(
                "transmitter", self._legacy_dfu_where()))
        if source == "dfu" and not in_rom_dfu:
            raise RuntimeError(
                "cannot force a production reflash from bootloader DFU — the "
                "ROM loader is entered from the RUNNING application "
                "(OW_CMD_DFU force switch). Boot the app first, then re-run.")
        prod = (str(production_image) if production_image
                else str(bundled_transmitter_production_image()))
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
               force: bool = False,
               force_production: bool = False,
               progress_callback: Callable | None = None) -> UpdateResult:
        """Detect the unit's state and run the appropriate update.

        A legacy-bootloader unit whose app still runs takes the ROM-DFU
        production-image migration, which also clears the anti-rollback
        floor. One found parked in its LEGACY bootloader's USB DFU (dead app)
        is refused: the ROM loader is out of reach without the app, and this
        SDK has no legacy-bootloader path (SR-021) — BOOT0 or SWD.

        Args:
            signed_app: Override the signed app image (secure path). Defaults
                to the bundled signed transmitter app.
            production_image: Override the combined bootloader+app image
                (migration / force-production paths). Defaults to the
                bundled production image.
            force: Secure path only: flash even if the image version is below
                the installed one (still subject to the bootloader's
                anti-rollback floor at boot).
            force_production: Reflash the FULL production image (bootloader +
                app) via STM32 ROM DFU regardless of cohort — see
                :meth:`_force_production_update`. Beta/unlocked units only.

        Returns:
            :class:`UpdateResult`.

        Raises:
            ValueError: Bad image / downgrade refused.
            RuntimeError: State undetectable, a master parked in the legacy
                bootloader's DFU, wrong DFU environment, or a write/verify
                failure.
        """
        if force_production:
            return self._force_production_update(production_image,
                                                 progress_callback)
        state, source = self.detect()
        in_dfu = source == "dfu"
        if state == COHORT_NO_DFU:
            raise RuntimeError(self._no_dfu_message())
        if state == COHORT_LEGACY and in_dfu:
            # Dead app: the ROM loader is unreachable from software.
            raise RuntimeError(_no_legacy_path_message(
                "transmitter", self._legacy_dfu_where()))

        if state in (COHORT_NONE, COHORT_LEGACY):
            prod = (str(production_image) if production_image
                    else str(bundled_transmitter_production_image()))
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

        app = str(signed_app) if signed_app else str(bundled_transmitter_signed_app())
        bl_version = self._mgr.update_transmitter(
            app, enter_dfu_fn=None if in_dfu else self._enter_dfu,
            keys_dir=self.keys_dir, force=force, vid=self.vid, pid=self.pid,
            libusb_dll=self.libusb_dll, progress_callback=progress_callback)
        return UpdateResult(state, "app-update",
                            "Updated the transmitter application on the "
                            "secure bootloader.", reboot_required=True,
                            bl_version=bl_version)

    def slave_module_version(self, module: int) -> tuple[str, int]:
        """Return ``(app_version, mode)`` for a slave module as the master
        currently sees it (re-enumerates first so the report is fresh)."""
        from openlifu_sdk.io.LIFUConfig import NODE_MODE_UNKNOWN
        tx = self._require_tx()
        tx.enumerate_modules()
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
                     stub_bin: str | None = None,
                     force_production: bool = False,
                     dfu_wait_s: float = 6.0,
                     progress_callback: Callable | None = None) -> UpdateResult:
        """Update a SLAVE transmitter module (index >= 1) over I2C, through
        the master. The master brokers everything on the one-wire + I2C buses.

        Only a **secure-bootloader** slave (app >= 2.0.8) is updated here:
        the SFU1 signed app is streamed to its bootloader over I2C. A
        **legacy-bootloader** slave (app 2.0.4 - 2.0.7) is refused: its
        one-shot I2C migration needed the legacy bootloader's shared HMAC
        key, which this SDK no longer carries (SR-021). Connect it as the
        USB master and update it as module 0 (ROM-DFU production image).

        With ``force_production=True`` a SECURE-bootloader slave is given the
        same full-image treatment — the path for rolling a NEW BOOTLOADER
        onto an already-migrated slave: the SIGNED build of the DFU stub is
        installed through the secure I2C DFU like a normal app (the
        bootloader verifies it at boot), the stub opens the full-flash DFU,
        and the whole production image is streamed.

        Slaves on app 2.0.2 - 2.0.3 (no bootloader at all) CANNOT be updated
        over I2C — their DFU entry jumps to the STM32 ROM loader, which does
        not speak this I2C protocol. Auto-detect raises a clear error for
        them: connect such a module as the USB master and update it as module
        0. Slaves below 2.0.2 have no DFU entry at all and are refused
        outright — BOOT0 (ROM loader over USB) or SWD only.

        Args:
            module: slave module index (>= 1). Module 0 is the USB master —
                use :meth:`update`.
            signed_app: signed app image (secure path); defaults to the
                bundled signed app.
            production_image: combined bootloader+app image
                (force-production path); defaults to the bundled production
                image.
            stub_bin: override the SFU1-signed RAM-resident DFU stub
                (force-production path). Defaults to the bundled one.
            force_production: reflash bootloader + app on a SECURE slave via
                the signed DFU stub (see above). Beta/unlocked units only.
            dfu_wait_s: seconds to wait for a slave BL to come up on I2C.

        Raises:
            ValueError: module < 1, or a bad image.
            RuntimeError: the slave is not on the secure bootloader, the
                master can't see/reach the module, DFU entry failed, or an
                I2C programming failure.
        """
        self._require_tx()
        if module < 1:
            raise ValueError(
                "update_slave targets slave modules (>= 1); use update() for "
                "the master (module 0).")

        self._require_secure_slave(module)
        if force_production:
            return self._update_slave_force_production(
                module, production_image, stub_bin, dfu_wait_s,
                progress_callback)
        return self._update_slave_secure(module, signed_app, dfu_wait_s,
                                         progress_callback)

    def _require_secure_slave(self, module: int) -> None:
        """Check from its running app version that the slave is on the
        secure bootloader (app >= 2.0.8), the only one this SDK updates over
        I2C.

        Raises RuntimeError for the cohorts that cannot be updated over the
        master's I2C passthrough — apps 2.0.4 - 2.0.7 (legacy bootloader: no
        legacy path, SR-021), 2.0.2 - 2.0.3 (no bootloader: DFU entry lands
        in the STM32 ROM loader, which does not speak this protocol) and
        < 2.0.2 (no OW_CMD_DFU handler at all) — and when the version cannot
        be read.
        """
        tx = self._require_tx()
        try:
            ver = str(tx.get_version(module=module))
        except Exception as e:  # reason: any read failure means the bootloader is unknown; refused, cause chained
            raise RuntimeError(
                f"could not read slave {module} version to check its "
                f"bootloader ({e}).") from e
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
        if cohort == COHORT_LEGACY:
            raise RuntimeError(
                f"slave module {module} runs app {ver} (2.0.4 - 2.0.7): it is "
                "on the LEGACY bootloader, and this SDK has no I2C migration "
                "for it (the one-shot path needed the legacy bootloader's "
                "shared HMAC key, removed for SR-021). Connect that module as "
                "the USB master and update it as module 0 (full production "
                "image via ROM DFU).")
        logger.info("Slave %d app %s -> secure bootloader", module, ver)

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
        effort)."""
        tx = self._require_tx()
        logger.info("Waiting %.0fs for the slave app to boot...", dfu_wait_s)
        time.sleep(dfu_wait_s)
        try:
            tx.enumerate_modules()
            ver = str(tx.get_version(module=module))
            logger.info("Slave module %d app version: %s", module, ver)
            return ver
        except Exception as e:
            logger.warning("Slave %d post-update version read failed (%s) — "
                           "it may still be verifying/booting", module, e)
            return ""

    def _update_slave_secure(self, module: int, signed_app: str | None,
                             dfu_wait_s: float,
                             progress_callback: Callable | None) -> UpdateResult:
        tx = self._require_tx()
        app = str(signed_app) if signed_app else str(bundled_transmitter_signed_app())
        i2c_addr = self._slave_enter_dfu(module, dfu_wait_s)
        mgr = LIFUDFUManager(uart=tx.uart)
        bl_version = mgr.program_transmitter_slave_i2c(
            app, i2c_addr=i2c_addr, keys_dir=self.keys_dir,
            progress_callback=progress_callback)
        app_version = self._slave_post_boot_version(module, dfu_wait_s)
        summary = (f"Updated slave transmitter module {module} over I2C"
                   + (f" (now {app_version})" if app_version else "."))
        return UpdateResult(COHORT_SECURE, "app-update-i2c", summary,
                            reboot_required=False, bl_version=bl_version)

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
        stub carries the SAME FwVersion as the bundled production app, so the
        anti-rollback floor is not disturbed (boot check is version >=
        floor). Beta/unlocked units only.
        """
        tx = self._require_tx()
        prod = (str(production_image) if production_image
                else str(bundled_transmitter_production_image()))
        stub = (str(stub_bin) if stub_bin
                else str(bundled_transmitter_dfu_stub_signed()))
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
        self._require_tx().enter_dfu(module=0)

    def _enter_rom(self) -> None:
        self._require_tx().enter_stm32_rom_dfu(module=0)


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
    if args.module and args.module > 0:
        return _run_transmitter_slave(fw, args)
    return _run_update_flow(
        fw, "transmitter", fw.detect,
        lambda: fw.update(signed_app=args.app,
                          production_image=args.production,
                          force=args.force,
                          force_production=args.force_production,
                          progress_callback=_progress),
        args)


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
    if args.force_production:
        label += " — FULL production reflash (signed DFU stub + bootloader + app)"
    if not _confirm(label, args.yes):
        print("Aborted.")
        return 1
    try:
        result = fw.update_slave(module, signed_app=args.app,
                                 production_image=args.production,
                                 force_production=args.force_production,
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
                          force=args.force,
                          force_production=args.force_production,
                          progress_callback=_progress),
        args)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m openlifu_sdk.io.LIFUFirmwareUpdate",
        description="Detect the unit's state and update its firmware. "
                    "Console: no-bootloader / secure paths. "
                    "Transmitter: standard USB DFU on the secure bootloader. "
                    "Uses the SDK-bundled images and needs no signing keys.")
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
    parser.add_argument("--keys", help="Optional keys dir to pre-validate the app signature.")
    parser.add_argument("--force", action="store_true",
                        help="Secure path only: flash even if not newer.")
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
