"""SDK-version pre-flight check and Incompatible-version dialog.

Apps pin a minimum SDK version (e.g. ``MIN_SDK_VERSION = "1.0.7"``) and
call :func:`check_sdk_version` early in startup. If the installed SDK
is older, they call :func:`show_incompatible_version_dialog` to surface
the error before any hardware access.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def parse_sdk_version(version_str: str):
    """Parse an openlifu-sdk version string into a comparable object.

    Prefers :class:`packaging.version.Version` (full PEP 440 support
    including local version segments like ``1.0.7.dev3+g1a2b3c4``).
    Falls back to a regex on ``MAJOR.MINOR.PATCH`` if ``packaging``
    isn't installed or the string is malformed.

    Returns ``None`` if no leading numeric version can be extracted.
    """
    if not version_str:
        return None
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return Version(version_str)
        except InvalidVersion:
            pass
    except ImportError:
        pass
    m = re.match(r"\s*(\d+)\.(\d+)(?:\.(\d+))?", str(version_str))
    if not m:
        return None
    return tuple(int(p) if p is not None else 0 for p in m.groups())


def check_sdk_version(min_version: str) -> Tuple[bool, str, str]:
    """Verify the installed openlifu-sdk meets ``min_version``.

    Returns ``(ok, installed_version, message)``. ``ok`` is True when
    the installed version parses and is ``>= min_version``. The message
    is human-readable and suitable for surfacing to the user.
    """
    try:
        from openlifu_sdk import LIFUInterface

        installed = LIFUInterface.get_sdk_version()
    except Exception as exc:  # pragma: no cover - defensive
        return False, "unknown", f"Could not determine openlifu-sdk version: {exc}"

    parsed_installed = parse_sdk_version(installed)
    parsed_min = parse_sdk_version(min_version)
    if parsed_installed is None or parsed_min is None:
        return (
            False,
            installed,
            f"Could not parse openlifu-sdk version '{installed}' "
            f"(minimum required: {min_version}).",
        )
    try:
        ok = parsed_installed >= parsed_min
    except TypeError:
        return (
            False,
            installed,
            f"Could not compare openlifu-sdk version '{installed}' "
            f"to minimum '{min_version}'.",
        )
    if ok:
        return True, installed, f"openlifu-sdk {installed} (>= {min_version})"
    return (
        False,
        installed,
        f"openlifu-sdk {installed} is older than the required minimum {min_version}. "
        f"Please upgrade with: pip install --upgrade 'openlifu-sdk>={min_version}'",
    )


def show_incompatible_version_dialog(
    message: str,
    title: str = "Incompatible openlifu-sdk version",
    parent=None,
) -> None:
    """Pop a modal QMessageBox with the version mismatch message.

    Caller is expected to ``sys.exit()`` afterward. Safe to call before
    a QApplication exists (a temporary one is spun up if needed). Falls
    back to ``stderr`` if QtWidgets isn't importable.
    """
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox

        QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(parent, title, f"{message}\n\nThe application will now exit.")
    except Exception:
        print(f"ERROR: {message}", file=sys.stderr)


def enforce_min_sdk_version(
    min_version: str,
    title: str = "Incompatible openlifu-sdk version",
    exit_code: int = 2,
) -> str:
    """Convenience: check + dialog + ``sys.exit`` on failure.

    Returns the installed version string on success. Apps that want
    custom logging can use :func:`check_sdk_version` directly.
    """
    ok, installed, message = check_sdk_version(min_version)
    if ok:
        logger.info(message)
        return installed
    logger.error(message)
    show_incompatible_version_dialog(message, title=title)
    sys.exit(exit_code)


__all__ = [
    "check_sdk_version",
    "enforce_min_sdk_version",
    "parse_sdk_version",
    "show_incompatible_version_dialog",
]
