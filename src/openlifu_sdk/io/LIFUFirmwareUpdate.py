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

**Transmitter** (:class:`LIFUTransmitterFirmwareUpdate`) — standard USB DFU
update of the module 0 (USB master) application on the secure bootloader
(open-lifu-transmitter-bl). The signed app is written to the active slot and
verified by the bootloader at boot; no signing keys are needed. Legacy
(non-secure bootloader) units and I2C slave-module updates are not covered
here — use ``TxDevice.update_firmware`` for those::

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
    DFU_KIND_ROM,
    DFU_KIND_SECURE,
    LIFUDFUManager,
    TRANSMITTER_PROFILE,
    infer_console_bootloader_from_app_version,
)

logger = logging.getLogger(__name__)

_FW_DIR = Path(__file__).parent.parent / "firmware"

# Cohort constants (align with infer_console_bootloader_from_app_version)
COHORT_NONE = "no-bootloader"
COHORT_LEGACY = "legacy-bl"
COHORT_SECURE = "secure-bl"


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

    def update(self, *, production_image: str | None = None,
               signed_app: str | None = None, updater_bin: str | None = None,
               force: bool = False,
               progress_callback: Callable | None = None) -> UpdateResult:
        """Detect the unit's state and run the appropriate update.

        Args:
            production_image: Override the combined bootloader+app image
                (no-bootloader path). Defaults to the bundled production image.
            signed_app: Override the signed app (legacy + secure paths).
                Defaults to the bundled signed app.
            updater_bin: Override the legacy RAM updater. Defaults to the
                bundled updater.
            force: For the secure app-update path, flash even if the image
                version is below the installed one (still subject to the
                bootloader's anti-rollback floor at boot).

        Returns:
            :class:`UpdateResult`.

        Raises:
            ValueError: Bad image / downgrade refused.
            RuntimeError: State undetectable, wrong DFU environment, or a
                write/verify failure.
        """
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
    """Transmitter firmware updater — standard USB DFU on the secure bootloader.

    Covers the module 0 (USB master) update path for units running the secure
    bootloader (open-lifu-transmitter-bl): reboot the running application into
    DFU, write the SBSFU signed image to the active slot with pre-erase
    validation and anti-downgrade checks, and let the bootloader verify it at
    boot. Needs no signing keys; an optional *keys_dir* only adds an ECDSA
    signature pre-check before flashing.

    Not covered here (yet): migration of legacy (non-secure bootloader) units,
    and I2C slave-module updates — use ``TxDevice.update_firmware`` for those.

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

    def detect(self) -> tuple[str, str]:
        """Determine how the unit will be updated.

        Returns ``(state, source)`` where state is ``COHORT_SECURE`` and
        source is ``"app"`` (application responding; DFU entry will be
        triggered) or ``"dfu"`` (the unit is already in the secure
        bootloader's USB DFU).

        Raises:
            RuntimeError: No responding application and no secure-bootloader
                DFU device present (a legacy or ROM DFU environment is
                refused — this path only writes SFU1 signed images).
        """
        if self.tx is not None:
            try:
                ver = str(self.tx.get_version())
                logger.info("Detected transmitter app version %s", ver)
                return COHORT_SECURE, "app"
            except Exception as e:
                logger.info("Transmitter app version read failed (%s); "
                            "checking DFU state", e)

        # detect_console_dfu_kind reads only the generic USB product string /
        # DFU version, so it identifies the transmitter environments too.
        kind, dfu_ver = self._mgr.detect_console_dfu_kind(
            vid=self.vid, pid=self.pid, libusb_dll=self.libusb_dll)
        if kind != DFU_KIND_SECURE:
            raise RuntimeError(
                f"Transmitter is not in the secure bootloader's DFU (found "
                f"{kind!r}). This path only supports the secure bootloader; "
                "connect the running app, or use TxDevice.update_firmware for "
                "legacy units.")
        logger.info("Detected secure-bootloader DFU %s", dfu_ver)
        return COHORT_SECURE, "dfu"

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
            RuntimeError: No responding application and no secure-bootloader
                DFU present, or the DFU device did not enumerate.
        """
        _state, source = self.detect()
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

    def update(self, *, signed_app: str | None = None, force: bool = False,
               progress_callback: Callable | None = None) -> UpdateResult:
        """Update the transmitter application over standard USB DFU.

        Args:
            signed_app: Override the signed app image. Defaults to the bundled
                signed transmitter app.
            force: Flash even if the image version is below the installed one
                (still subject to the bootloader's anti-rollback floor at
                boot).

        Returns:
            :class:`UpdateResult`.

        Raises:
            ValueError: Bad image / downgrade refused.
            RuntimeError: Wrong DFU environment, or a write/verify failure.
        """
        state, source = self.detect()
        app = str(signed_app) if signed_app else str(bundled_transmitter_signed_app())
        in_dfu = source == "dfu"

        bl_version = self._mgr.update_transmitter(
            app, enter_dfu_fn=None if in_dfu else self._enter_dfu,
            keys_dir=self.keys_dir, force=force, vid=self.vid, pid=self.pid,
            libusb_dll=self.libusb_dll, progress_callback=progress_callback)
        return UpdateResult(state, "app-update",
                            "Updated the transmitter application on the "
                            "secure bootloader.", reboot_required=True,
                            bl_version=bl_version)

    def _require_tx(self):
        if self.tx is None:
            raise RuntimeError(
                "a TxDevice is required to trigger DFU entry (or put the "
                "unit in DFU first and re-run)")
        return self.tx

    def _enter_dfu(self) -> None:
        self._require_tx().enter_dfu(module=0)


# ---------------------------------------------------------------------------
# CLI:  python -m openlifu_sdk.io.LIFUFirmwareUpdate [options]
# ---------------------------------------------------------------------------

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
                        help="Console only: override the combined bootloader+app image.")
    parser.add_argument("--app", help="Override the signed app image.")
    parser.add_argument("--keys", help="Optional keys dir to pre-validate the app signature.")
    parser.add_argument("--force", action="store_true",
                        help="Secure path only: flash even if not newer.")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Skip the confirmation prompt.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from openlifu_sdk.io.LIFUInterface import LIFUInterface

    def confirm(label: str) -> bool:
        if args.yes:
            return True
        try:
            resp = input(f"Proceed with the update for '{label}'? [y/N] ").strip().lower()
        except EOFError:
            resp = ""
        return resp in ("y", "yes")

    def progress(w: int, t: int, label: str) -> None:
        pct = 100 * w // t if t else 100
        print(f"\r  {label}: {w:,}/{t:,} ({pct}%)", end="", flush=True)

    if args.device == "transmitter":
        # A running app is preferred (it lets us trigger DFU entry), but a
        # unit already sitting in the secure bootloader's DFU works too.
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
        try:
            state, source = fw.detect()
        except RuntimeError as e:
            print(f"Could not determine transmitter state: {e}")
            return 1
        print(f"Transmitter state: {state} (from {source})")
        if args.detect:
            return 0
        if args.bl_version:
            try:
                print(f"Bootloader version: {fw.get_bootloader_version()}")
            except RuntimeError as e:
                print(f"Could not read bootloader version: {e}")
                return 1
            return 0
        if not confirm(state):
            print("Aborted.")
            return 1
        try:
            result = fw.update(signed_app=args.app, force=args.force,
                               progress_callback=progress)
        except (ValueError, RuntimeError) as e:
            print(f"\nUPDATE FAILED: {e}")
            return 1
        print(f"\n{result.summary}")
        if result.bl_version:
            print(f"Bootloader version: {result.bl_version}")
        if result.reboot_required:
            print("Power-cycle the transmitter to boot the new application.")
        return 0

    interface = LIFUInterface(TX_test_mode=False)
    _tx, hv = interface.is_device_connected()
    if not hv:
        print("Console not connected.")
        return 1
    interface.hvcontroller.ping()

    fw = LIFUFirmwareUpdate(hv=interface.hvcontroller, keys_dir=args.keys)
    try:
        cohort, source = fw.detect_cohort()
    except RuntimeError as e:
        print(f"Could not determine console state: {e}")
        return 1
    print(f"Console state: {cohort} (from {source})")
    if args.detect:
        return 0
    if args.bl_version:
        try:
            print(f"Bootloader version: {fw.get_bootloader_version()}")
        except RuntimeError as e:
            print(f"Could not read bootloader version: {e}")
            return 1
        return 0

    if not confirm(cohort):
        print("Aborted.")
        return 1

    try:
        result = fw.update(production_image=args.production, signed_app=args.app,
                           force=args.force, progress_callback=progress)
    except (ValueError, RuntimeError) as e:
        print(f"\nUPDATE FAILED: {e}")
        return 1

    print(f"\n{result.summary}")
    if result.bl_version:
        print(f"Bootloader version: {result.bl_version}")
    if result.reboot_required:
        print("Power-cycle the console to boot the new application.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
