"""High-level console firmware update — one entry point for all three cases.

A console unit is in one of three states, each needing a different update path:

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

NOTE: the migration paths are for unlocked (beta) units only; after RDP/FDA
lockdown the force-ROM-DFU switch is inert and the bootloader is not erasable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from openlifu_sdk.io.LIFUDFU import (
    DFU_KIND_LEGACY,
    DFU_KIND_ROM,
    DFU_KIND_SECURE,
    LIFUDFUManager,
    bundled_updater_path,
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


@dataclass
class UpdateResult:
    """Outcome of an update run."""

    cohort: str            # detected cohort / DFU kind driving the choice
    action: str            # "migrate-rom" | "migrate-legacy" | "app-update"
    summary: str           # human-readable one-liner
    reboot_required: bool  # True if a power-cycle is needed to run the app


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
            self._mgr.update_console(
                app, enter_dfu_fn=None if in_dfu else self._enter_dfu,
                keys_dir=self.keys_dir, force=force, vid=self.vid, pid=self.pid,
                libusb_dll=self.libusb_dll, progress_callback=progress_callback)
            return UpdateResult(cohort, "app-update",
                                "Updated the application on the secure "
                                "bootloader.", reboot_required=True)

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


# ---------------------------------------------------------------------------
# CLI:  python -m openlifu_sdk.io.LIFUFirmwareUpdate [options]
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m openlifu_sdk.io.LIFUFirmwareUpdate",
        description="Detect the console's state and update its firmware "
                    "(no-bootloader / legacy / secure). Uses the SDK-bundled "
                    "images and needs no signing keys.")
    parser.add_argument("--detect", action="store_true",
                        help="Report the detected state and exit — no flashing.")
    parser.add_argument("--production", help="Override the combined bootloader+app image.")
    parser.add_argument("--app", help="Override the signed app image.")
    parser.add_argument("--keys", help="Optional keys dir to pre-validate the app signature.")
    parser.add_argument("--force", action="store_true",
                        help="Secure path only: flash even if not newer.")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Skip the confirmation prompt.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from openlifu_sdk.io.LIFUInterface import LIFUInterface

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

    if not args.yes:
        try:
            resp = input(f"Proceed with the update for '{cohort}'? [y/N] ").strip().lower()
        except EOFError:
            resp = ""
        if resp not in ("y", "yes"):
            print("Aborted.")
            return 1

    def progress(w: int, t: int, label: str) -> None:
        pct = 100 * w // t if t else 100
        print(f"\r  {label}: {w:,}/{t:,} ({pct}%)", end="", flush=True)

    try:
        result = fw.update(production_image=args.production, signed_app=args.app,
                           force=args.force, progress_callback=progress)
    except (ValueError, RuntimeError) as e:
        print(f"\nUPDATE FAILED: {e}")
        return 1

    print(f"\n{result.summary}")
    if result.reboot_required:
        print("Power-cycle the console to boot the new application.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
