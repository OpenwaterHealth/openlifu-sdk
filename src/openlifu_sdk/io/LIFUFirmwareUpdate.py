"""High-level firmware update for the console and the transmitter.

**Console** (:class:`LIFUFirmwareUpdate`) — one entry point for all three
cases. A console unit is in one of three states, each needing a different
update path:

  - **no bootloader** (app < 1.2.0)     → migrate to the secure bootloader via
    the STM32 ROM DFU (write the combined bootloader+app image).
  - **legacy bootloader** (1.2.0–1.2.5) → migrate via the RAM-resident
    self-updater (the legacy DFU can't reach the ROM loader).
  - **secure bootloader** (≥ 1.2.6)     → normal signed-app update.

:class:`LIFUFirmwareUpdate` auto-detects the state (from the running app
version, or the current DFU environment) and runs the right path. It defaults
to the firmware images bundled with the SDK and needs **no signing keys**:
the legacy updater is authenticated by an HMAC trust tag computed on the fly,
and the secure bootloader verifies the app at boot. An optional ``keys_dir``
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
    CONSOLE_PROFILE,
    DFU_KIND_LEGACY,
    DFU_KIND_NO_DFU,
    DFU_KIND_ROM,
    DFU_KIND_SECURE,
    I2C_DFU_SLAVE_ADDR,
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
    """Signed console app image bundled with the SDK (legacy migration and
    secure app update source)."""
    return _FW_DIR / "openlifu-console-fw-signed.bin"


def bundled_transmitter_signed_app() -> Path:
    """Signed transmitter app image bundled with the SDK (secure app update
    source for the transmitter's USB DFU path)."""
    return _FW_DIR / "openlifu-transmitter-fw-signed.bin"


def bundled_transmitter_production_image() -> Path:
    """Combined bootloader + signed-app transmitter image bundled with the
    SDK (migration source for pre-secure-bootloader units, app <= 2.0.7)."""
    return _FW_DIR / "openlifu-transmitter-fw-production.bin"


def bundled_transmitter_legacy_updater() -> Path:
    """The RAM-resident legacy updater bundled with the SDK — a legacy-slot
    app that EMBEDS the secure bootloader and swaps it in from the inside
    (see transmitter-legacy-updater).

    This is the USB recovery image for a MASTER parked in its legacy
    bootloader's DFU with a dead application: the legacy DFU can reach only
    the app slot, so the bootloader has to replace itself. Keyless — the
    legacy bootloader authenticates it with an HMAC trust tag the SDK
    computes at run time (``build_legacy_metadata``)."""
    return _FW_DIR / "openlifu-transmitter-legacy-updater.bin"


def bundled_transmitter_dfu_stub() -> Path:
    """The RAM-resident I2C DFU stub bundled with the SDK (source for
    migrating a LEGACY-bootloader transmitter slave over I2C — uploaded
    through the legacy DFU as an app, it opens a full-flash I2C DFU so the
    whole production image can be streamed; see stub-code/transmitter)."""
    return _FW_DIR / "openlifu-transmitter-dfu-stub.bin"


def bundled_transmitter_dfu_stub_signed() -> Path:
    """The SFU1-SIGNED build of the RAM-resident I2C DFU stub (source for the
    slave FORCE-PRODUCTION path — installed through the SECURE bootloader's
    I2C DFU like a normal app; signed with the same version as the bundled
    production app so the anti-rollback floor is untouched)."""
    return _FW_DIR / "openlifu-transmitter-dfu-stub-signed.bin"


@dataclass
class UpdateResult:
    """Outcome of an update run."""

    cohort: str            # detected cohort / DFU kind driving the choice
    action: str            # "migrate-rom" | "migrate-legacy" | "app-update"
    summary: str           # human-readable one-liner
    reboot_required: bool  # True if a power-cycle is needed to run the app
    bl_version: str | None = None  # bootloader version string (DFU virtual
                                   # version address), when the path read it


class LIFUFirmwareUpdate:
    """Auto-detecting console firmware updater covering all three unit states.

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

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def detect_cohort(self) -> tuple[str, str | None]:
        """Determine what update path the unit needs.

        Returns ``(cohort, source)`` where cohort is ``COHORT_NONE`` /
        ``COHORT_LEGACY`` / ``COHORT_SECURE`` and source is ``"app"`` (from the
        running app version) or ``"dfu"`` (the unit was already in DFU).

        Raises:
            RuntimeError: Cannot determine the state (no HV controller and no
                DFU device present).
        """
        # Prefer the running app version - it names the true cohort.
        if self.hv is not None:
            try:
                ver = str(self.hv.get_version())
                cohort = infer_console_bootloader_from_app_version(ver)
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
        if source == "dfu" and not in_rom_dfu:
            raise RuntimeError(
                "cannot force a production reflash from bootloader DFU — the "
                "ROM loader is entered from the RUNNING application "
                "(OW_CMD_DFU force switch). Boot the app first, then re-run.")
        prod = (str(production_image) if production_image
                else str(bundled_production_image()))
        self._mgr.migrate_console_full_image(
            prod,
            enter_stm32_rom_dfu_fn=None if in_rom_dfu else self._enter_rom,
            keys_dir=self.keys_dir, vid=self.vid, pid=self.pid,
            libusb_dll=self.libusb_dll, progress_callback=progress_callback)
        return UpdateResult(cohort, "force-production",
                            "Reflashed the full production image (bootloader "
                            "+ app) via STM32 ROM DFU.", reboot_required=True)

    def update(self, *, production_image: str | None = None,
               signed_app: str | None = None, updater_bin: str | None = None,
               force: bool = False, force_production: bool = False,
               progress_callback: Callable | None = None) -> UpdateResult:
        """Detect the unit's state and run the appropriate update.

        Args:
            production_image: Override the combined bootloader+app image
                (no-bootloader / force-production paths). Defaults to the
                bundled production image.
            signed_app: Override the signed app (legacy + secure paths).
                Defaults to the bundled signed app.
            updater_bin: Override the legacy RAM updater. Defaults to the
                bundled updater.
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
            RuntimeError: State undetectable, wrong DFU environment, or a
                write/verify failure.
        """
        if force_production:
            return self._force_production_update(production_image,
                                                 progress_callback)
        cohort, source = self.detect_cohort()
        prod = str(production_image) if production_image else str(bundled_production_image())
        app = str(signed_app) if signed_app else str(bundled_signed_app())
        # When already in DFU we must not trigger another DFU entry.
        in_dfu = source == "dfu"

        if cohort == COHORT_NONE:
            self._mgr.migrate_console_full_image(
                prod,
                enter_stm32_rom_dfu_fn=None if in_dfu else self._enter_rom,
                keys_dir=self.keys_dir, vid=self.vid, pid=self.pid,
                libusb_dll=self.libusb_dll, progress_callback=progress_callback)
            return UpdateResult(cohort, "migrate-rom",
                                "Migrated no-bootloader unit to the secure "
                                "bootloader (full image).", reboot_required=True)

        if cohort == COHORT_LEGACY:
            self._mgr.migrate_console_legacy(
                signed_app=app, updater_bin=updater_bin,
                enter_dfu_fn=None if in_dfu else self._enter_dfu,
                keys_dir=self.keys_dir, vid=self.vid, pid=self.pid,
                libusb_dll=self.libusb_dll, progress_callback=progress_callback)
            return UpdateResult(cohort, "migrate-legacy",
                                "Migrated legacy-bootloader unit to the secure "
                                "bootloader (RAM updater).", reboot_required=True)

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
        """Explain why an app older than 2.0.2 cannot be touched at all.

        DFU entry on the transmitter is a request to the RUNNING application
        (OW_CMD_DFU): it arms the ROM-loader magic word and resets. Apps
        before 2.0.2 have no handler for that command, so every software path
        the SDK could take — plain entry, the reserved=0x77 force switch, the
        bootloader DFUs — dead-ends before it starts. Refusing here keeps the
        unit intact instead of stranding it after a half-attempted entry.
        """
        ver = self._last_app_version or "unknown"
        floor = ".".join(str(p) for p in TRANSMITTER_MIN_DFU_APP_VERSION)
        return (
            f"transmitter app {ver} is older than {floor} and cannot be "
            f"{action}: those builds have no OW_CMD_DFU handler, so no "
            "software request can reboot them into any DFU mode (not the "
            "plain entry, not the reserved=0x77 ROM-DFU force switch). Enter "
            "the STM32 ROM loader in hardware instead — hold BOOT0 while "
            "power-cycling the unit, then re-run this command: it enumerates "
            "as 'STM32 BOOTLOADER' and the full production image (bootloader "
            "+ app) is written from there. Failing that, program it over SWD.")

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
        updater = (Path(updater_bin) if updater_bin
                   else bundled_transmitter_legacy_updater())
        if not updater.is_file():
            raise RuntimeError(self._legacy_dfu_recovery_message(
                f"The legacy updater image that would replace the bootloader "
                f"from inside the app slot is not available ({updater}) — "
                "pass one with updater_bin/--updater to use that path."))
        app = (str(signed_app) if signed_app
               else str(bundled_transmitter_signed_app()))
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
               updater_bin: str | None = None, force: bool = False,
               force_production: bool = False,
               progress_callback: Callable | None = None) -> UpdateResult:
        """Detect the unit's state and run the appropriate update.

        A unit found parked in its LEGACY bootloader's USB DFU (dead app)
        takes the USB legacy-updater recovery path — the bootloader replaces
        itself from the app slot, then the signed app is flashed over the
        secure DFU that comes up. Every other legacy unit (app still running)
        takes the ROM-DFU production-image migration, which also clears the
        anti-rollback floor.

        Args:
            signed_app: Override the signed app image (secure path). Defaults
                to the bundled signed transmitter app.
            production_image: Override the combined bootloader+app image
                (migration / force-production paths). Defaults to the
                bundled production image.
            updater_bin: Override the RAM-resident legacy updater (USB
                legacy-DFU recovery path). Defaults to the bundled updater.
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
            RuntimeError: State undetectable, wrong DFU environment, or a
                write/verify failure.
        """
        if force_production:
            return self._force_production_update(production_image,
                                                 progress_callback)
        state, source = self.detect()
        in_dfu = source == "dfu"
        if state == COHORT_NO_DFU:
            raise RuntimeError(self._no_dfu_message())
        if state == COHORT_LEGACY and in_dfu:
            # Dead app: the ROM loader is unreachable, so the bootloader has
            # to replace itself from the legacy app slot.
            return self._migrate_legacy_usb(signed_app, updater_bin,
                                            progress_callback)

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
                     legacy: bool | None = None, stub_bin: str | None = None,
                     force_production: bool = False,
                     dfu_wait_s: float = 6.0,
                     progress_callback: Callable | None = None) -> UpdateResult:
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
            signed_app: signed app image (secure path); defaults to the
                bundled signed app.
            production_image: combined bootloader+app image (legacy /
                force-production paths); defaults to the bundled production
                image.
            legacy: force the legacy-migration path (True) or the secure path
                (False); ``None`` auto-detects from the slave's app version.
            stub_bin: override the RAM-resident DFU stub (legacy path: raw
                build; force-production path: SFU1-signed build). Defaults to
                the bundled ones.
            force_production: reflash bootloader + app on a SECURE slave via
                the signed DFU stub (see above). Beta/unlocked units only.
            dfu_wait_s: seconds to wait for a slave BL to come up on I2C.

        Raises:
            ValueError: module < 1, or a bad image.
            RuntimeError: master can't see/reach the module, DFU entry failed,
                or an I2C programming failure.
        """
        tx = self._require_tx()
        if module < 1:
            raise ValueError(
                "update_slave targets slave modules (>= 1); use update() for "
                "the master (module 0).")

        if legacy is None:
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
                    f"slave module {module} runs app {ver}, older than "
                    f"{floor}: those builds have no OW_CMD_DFU handler, so the "
                    "master cannot put the module into ANY DFU mode. It is not "
                    "updatable over I2C or as a USB master — enter the STM32 "
                    "ROM loader in hardware (BOOT0 at power-up) with the "
                    "module connected over USB and update it as module 0, or "
                    "program it over SWD.")
            if cohort == COHORT_NONE:
                raise RuntimeError(
                    f"slave module {module} runs app {ver} (2.0.2 - 2.0.3): it "
                    "has NO bootloader, and its DFU entry jumps to the STM32 "
                    "ROM loader, which cannot be reached over the master's I2C "
                    "passthrough. Connect that module as the USB master and "
                    "update it as module 0 (full production image via ROM "
                    "DFU).")
            legacy = cohort == COHORT_LEGACY
            logger.info("Slave %d app %s -> %s bootloader", module, ver,
                        "legacy" if legacy else "secure")

        if legacy:
            return self._update_slave_legacy(module, production_image,
                                             stub_bin, dfu_wait_s,
                                             progress_callback)
        if force_production:
            return self._update_slave_force_production(
                module, production_image, stub_bin, dfu_wait_s,
                progress_callback)
        return self._update_slave_secure(module, signed_app, dfu_wait_s,
                                         progress_callback)

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

    def _update_slave_legacy(self, module: int, production_image: str | None,
                             stub_bin: str | None, dfu_wait_s: float,
                             progress_callback: Callable | None) -> UpdateResult:
        """One-shot legacy migration: the stub is written through the LEGACY
        I2C DFU like any app (its WFM1 metadata is HMAC-trust-tagged on the
        fly); when the legacy BL boots it, it serves a full-flash I2C DFU
        from RAM and the whole production image (secure bootloader + signed
        app) is streamed to the flash base in one pass."""
        tx = self._require_tx()
        prod = (str(production_image) if production_image
                else str(bundled_transmitter_production_image()))
        stub = (str(stub_bin) if stub_bin
                else str(bundled_transmitter_dfu_stub()))
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
                          updater_bin=args.updater, force=args.force,
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

    interface = LIFUInterface(TX_test_mode=False)
    _tx, hv = interface.is_device_connected()
    if not hv:
        print("Console not connected.")
        return 1
    interface.hvcontroller.ping()

    fw = LIFUFirmwareUpdate(hv=interface.hvcontroller, keys_dir=args.keys)
    return _run_update_flow(
        fw, "console", fw.detect_cohort,
        lambda: fw.update(production_image=args.production, signed_app=args.app,
                          updater_bin=args.updater, force=args.force,
                          force_production=args.force_production,
                          progress_callback=_progress),
        args)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m openlifu_sdk.io.LIFUFirmwareUpdate",
        description="Detect the unit's state and update its firmware. "
                    "Console: no-bootloader / legacy / secure paths. "
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
    parser.add_argument("--updater",
                        help="Override the RAM-resident legacy updater used "
                             "when a unit is parked in its LEGACY bootloader's "
                             "DFU with a dead app (console legacy migration; "
                             "transmitter USB legacy recovery).")
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
