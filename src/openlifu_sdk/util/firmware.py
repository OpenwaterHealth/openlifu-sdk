"""Firmware discovery + GitHub release downloads.

Two flavours of firmware files are recognized:

* **Bundled** -- shipped with the wheel under ``openlifu_sdk/firmware/``
  with the canonical filenames ``openlifu-console-fw.signed.bin`` and
  ``openlifu-transmitter-fw.signed.bin``.
* **Downloaded** -- pulled at runtime from the public GitHub releases of
  the firmware repos into ``openlifu_sdk/firmware/downloads/`` with a
  versioned filename (e.g. ``openlifu-console-fw_v2_0_8.signed.bin``).

The "current" path/version reported by :func:`get_console_firmware_path`
/ :func:`get_transmitter_firmware_path` (and the matching ``_version``
helpers) is the newest of the bundled + downloaded set, so once a
downloaded build is in place all downstream consumers automatically
prefer it without code changes.

``requests`` is imported lazily because it lives in the SDK's ``ui``
optional extra; only the download / latest-release helpers actually
need it.
"""
from __future__ import annotations

import logging
import re
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

CONSOLE_FIRMWARE_URL = "https://github.com/OpenwaterHealth/openlifu-console-fw"
TRANSMITTER_FIRMWARE_URL = "https://github.com/OpenwaterHealth/openlifu-transmitter-fw"

FIRMWARE_DIR_REL = '../firmware'


class FirmwareNetworkError(RuntimeError):
    """GitHub could not be reached for a firmware lookup or download.

    Raised by the firmware-update helpers in this module so callers can
    surface a clean "check your internet connection" message without
    having to special-case the underlying ``requests``/``urllib3``
    exception chain. Always raised with ``from None`` so the offending
    DNS/socket traceback is suppressed.
    """

DOWNLOADED_FIRMWARE_DIR_REL = '../firmware/downloads'

# Canonical bundled filenames (kept for backward compatibility).
CONSOLE_FIRMWARE_FILENAME = 'openlifu-console-fw.signed.bin'
TRANSMITTER_FIRMWARE_FILENAME = 'openlifu-transmitter-fw.signed.bin'

# Basenames used to compose versioned download filenames and to match
# release assets on GitHub.
CONSOLE_FIRMWARE_BASENAME = 'openlifu-console-fw'
TRANSMITTER_FIRMWARE_BASENAME = 'openlifu-transmitter-fw'


# =============================================================================
# Filesystem helpers
# =============================================================================

def _firmware_dir() -> Path:
    return (Path(__file__).parent / FIRMWARE_DIR_REL).resolve()


def _downloads_dir() -> Path:
    return (Path(__file__).parent / DOWNLOADED_FIRMWARE_DIR_REL).resolve()


def _get_firmware_version(filename: Path | str) -> str:
    """Extract the first MAJOR.MINOR.PATCH triple embedded in a firmware blob."""
    if isinstance(filename, str):
        filename = Path(filename)
    data = filename.read_bytes()
    match = re.search(rb'\d+\.\d+\.\d+', data)
    if match:
        return match.group().decode()
    raise ValueError(f"No firmware version found in {filename}")


def _parse_version(version_str: str | None) -> tuple[int, int, int] | None:
    """Parse the first ``MAJOR.MINOR[.PATCH]`` triple out of a string.

    Returns a 3-tuple of ints so comparisons across all call sites are
    type-consistent regardless of where the input came from. ``None`` if
    no version-looking substring is present.
    """
    if not version_str:
        return None
    m = re.search(r'(\d+)\.(\d+)(?:\.(\d+))?', str(version_str))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def _artifact_filename(basename: str, version: str) -> str:
    """Versioned filename used inside ``firmware/downloads/``.

    Example: ``_artifact_filename('openlifu-console-fw', '2.0.8')``
    -> ``'openlifu-console-fw_v2_0_8.signed.bin'``.
    """
    return f'{basename}_v{version.lstrip("vV").replace(".", "_")}.signed.bin'


def _iter_local_firmware(basename: str, bundled_filename: str):
    """Yield ``(path, version_str)`` for every local file matching ``basename``.

    Searches both the bundled firmware directory and the downloads
    directory. Files whose version cannot be extracted are skipped with
    a warning so a malformed download does not break version discovery.
    """
    candidates: list[Path] = []
    bundled = _firmware_dir() / bundled_filename
    if bundled.is_file():
        candidates.append(bundled)
    downloads = _downloads_dir()
    if downloads.is_dir():
        for p in downloads.iterdir():
            if p.is_file() and p.name.startswith(basename) and p.name.endswith('.signed.bin'):
                candidates.append(p)
    for path in candidates:
        try:
            yield path, _get_firmware_version(path)
        except (OSError, ValueError) as e:
            logger.warning("Skipping unreadable firmware file %s: %s", path, e)


def _best_local_firmware(basename: str, bundled_filename: str) -> tuple[Path, str] | None:
    """Return ``(path, version)`` of the highest-version local firmware, or None."""
    best: tuple[Path, str] | None = None
    best_parsed = None
    for path, version_str in _iter_local_firmware(basename, bundled_filename):
        parsed = _parse_version(version_str)
        if parsed is None:
            continue
        if best is None or parsed > best_parsed:
            best = (path, version_str)
            best_parsed = parsed
    return best


# =============================================================================
# Public: path + version lookup
# =============================================================================

def get_console_firmware_path() -> Path:
    """Path to the newest console firmware available (bundled or downloaded)."""
    best = _best_local_firmware(CONSOLE_FIRMWARE_BASENAME, CONSOLE_FIRMWARE_FILENAME)
    if best is None:
        raise FileNotFoundError("No console firmware file found")
    return best[0]


def get_transmitter_firmware_path() -> Path:
    """Path to the newest transmitter firmware available (bundled or downloaded)."""
    best = _best_local_firmware(TRANSMITTER_FIRMWARE_BASENAME, TRANSMITTER_FIRMWARE_FILENAME)
    if best is None:
        raise FileNotFoundError("No transmitter firmware file found")
    return best[0]


def get_console_firmware_version() -> str:
    """Version of the newest console firmware available locally."""
    best = _best_local_firmware(CONSOLE_FIRMWARE_BASENAME, CONSOLE_FIRMWARE_FILENAME)
    if best is None:
        raise FileNotFoundError("No console firmware file found")
    return best[1]


def get_transmitter_firmware_version() -> str:
    """Version of the newest transmitter firmware available locally."""
    best = _best_local_firmware(TRANSMITTER_FIRMWARE_BASENAME, TRANSMITTER_FIRMWARE_FILENAME)
    if best is None:
        raise FileNotFoundError("No transmitter firmware file found")
    return best[1]


# =============================================================================
# GitHub release polling
# =============================================================================

def _check_latest_release_version(repo_url: str, current_version: str) -> str:
    """Return the newer of ``current_version`` and the latest GitHub release tag.

    Raises :class:`FirmwareNetworkError` if GitHub is unreachable so
    callers can distinguish "no newer version available" from "could
    not check". HTTP non-200 responses (rate limits, missing release)
    fall back to ``current_version`` and only log a warning.
    """
    import requests

    api_url = repo_url.replace("github.com", "api.github.com/repos") + "/releases/latest"
    try:
        response = requests.get(api_url, timeout=10)
    except requests.RequestException as e:
        # Suppress the urllib3 -> requests chain so callers don't dump
        # a 60-line socket traceback into the UI / logs.
        logger.warning("Could not reach %s: %s", api_url, e)
        raise FirmwareNetworkError(
            f"Could not reach GitHub at {api_url}"
        ) from None
    if response.status_code != 200:
        logger.warning("Latest-release lookup for %s returned HTTP %d",
                       repo_url, response.status_code)
        return current_version
    latest_tag = response.json().get("tag_name", "") or ""
    latest_clean = latest_tag.lstrip('vV')
    parsed_latest = _parse_version(latest_clean)
    parsed_current = _parse_version(current_version)
    if parsed_latest is None:
        return current_version
    if parsed_current is None or parsed_latest > parsed_current:
        return latest_clean
    return current_version


def check_latest_console_firmware_version() -> str:
    current_version = get_console_firmware_version()
    return _check_latest_release_version(CONSOLE_FIRMWARE_URL, current_version)


def check_latest_transmitter_firmware_version() -> str:
    current_version = get_transmitter_firmware_version()
    return _check_latest_release_version(TRANSMITTER_FIRMWARE_URL, current_version)


# =============================================================================
# Downloads
# =============================================================================

def _select_release_asset(assets: list, basename: str, version: str) -> dict | None:
    """Pick the best-matching firmware asset from a release's asset list.

    Tries a few common naming conventions in order:
      1. The versioned name produced by :func:`_artifact_filename`.
      2. ``{basename}.signed.bin`` (unversioned canonical name).
      3. Any ``.signed.bin`` asset whose name contains ``basename``.
    """
    if not assets:
        return None
    versioned = _artifact_filename(basename, version)
    canonical = f'{basename}.signed.bin'
    by_name = {a.get("name", ""): a for a in assets if isinstance(a, dict)}
    if versioned in by_name:
        return by_name[versioned]
    if canonical in by_name:
        return by_name[canonical]
    for name, asset in by_name.items():
        if name.endswith('.signed.bin') and basename in name:
            return asset
    return None


def _download_artifact(
    repo_url: str,
    version: str,
    basename: str,
    target_dir: Path | str | None = None,
) -> Path | None:
    """Download the firmware asset for ``version`` from a GitHub release.

    Tries both ``v{version}`` and ``{version}`` release tags. The asset
    is written to a tempfile in ``target_dir`` and renamed into place so
    a partial download does not masquerade as a complete firmware file.
    Returns the local :class:`~pathlib.Path` on success, ``None`` on any
    failure (network, missing tag, missing asset, unparseable contents).
    """
    import requests

    target = Path(target_dir) if target_dir is not None else _downloads_dir()
    target.mkdir(parents=True, exist_ok=True)
    version_clean = version.lstrip('vV')

    api_base = repo_url.replace("github.com", "api.github.com/repos") + "/releases/tags/"
    release_json = None
    for tag in (f"v{version_clean}", version_clean):
        try:
            r = requests.get(api_base + tag, timeout=10)
        except requests.RequestException as e:
            logger.warning("Release lookup %s%s failed: %s", api_base, tag, e)
            raise FirmwareNetworkError(
                f"Could not reach GitHub at {api_base}{tag}"
            ) from None
        if r.status_code == 200:
            release_json = r.json()
            break
    if release_json is None:
        logger.warning("No release found for %s at %s", repo_url, version_clean)
        return None

    asset = _select_release_asset(release_json.get("assets", []), basename, version_clean)
    if asset is None:
        logger.warning("No matching firmware asset for %s in release %s",
                       basename, version_clean)
        return None

    download_url = asset.get("browser_download_url")
    if not download_url:
        logger.warning("Asset %r has no browser_download_url", asset.get("name"))
        return None

    final_path = target / _artifact_filename(basename, version_clean)
    tmp_path: Path | None = None
    try:
        with requests.get(download_url, stream=True, timeout=60) as dl:
            dl.raise_for_status()
            with tempfile.NamedTemporaryFile(
                mode='wb', dir=target, prefix='.dl-', suffix='.tmp', delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                for chunk in dl.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        tmp.write(chunk)
    except requests.RequestException as e:
        logger.error("Download of %s failed: %s", download_url, e)
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise FirmwareNetworkError(
            f"Could not download firmware from {download_url}"
        ) from None

    try:
        _get_firmware_version(tmp_path)
    except (OSError, ValueError) as e:
        logger.error("Downloaded file %s does not look like firmware: %s", tmp_path, e)
        tmp_path.unlink(missing_ok=True)
        return None

    shutil.move(str(tmp_path), str(final_path))
    logger.info("Downloaded %s firmware v%s -> %s", basename, version_clean, final_path)
    return final_path


def download_latest_console_firmware() -> Path | None:
    """Fetch the latest console firmware release if newer than what's local.

    Returns the path to the downloaded artifact, or ``None`` if no
    update was needed or the download failed.
    """
    latest_version = check_latest_console_firmware_version()
    current_version = get_console_firmware_version()
    parsed_latest = _parse_version(latest_version)
    parsed_current = _parse_version(current_version)
    if parsed_latest is None:
        return None
    if parsed_current is not None and parsed_latest <= parsed_current:
        logger.info("Console firmware already up to date (have %s, latest %s)",
                    current_version, latest_version)
        return None
    return _download_artifact(
        CONSOLE_FIRMWARE_URL, latest_version, CONSOLE_FIRMWARE_BASENAME,
    )


def download_latest_transmitter_firmware() -> Path | None:
    """Fetch the latest transmitter firmware release if newer than what's local.

    Returns the path to the downloaded artifact, or ``None`` if no
    update was needed or the download failed.
    """
    latest_version = check_latest_transmitter_firmware_version()
    current_version = get_transmitter_firmware_version()
    parsed_latest = _parse_version(latest_version)
    parsed_current = _parse_version(current_version)
    if parsed_latest is None:
        return None
    if parsed_current is not None and parsed_latest <= parsed_current:
        logger.info("Transmitter firmware already up to date (have %s, latest %s)",
                    current_version, latest_version)
        return None
    return _download_artifact(
        TRANSMITTER_FIRMWARE_URL, latest_version, TRANSMITTER_FIRMWARE_BASENAME,
    )


__all__ = [
    "CONSOLE_FIRMWARE_BASENAME",
    "CONSOLE_FIRMWARE_FILENAME",
    "CONSOLE_FIRMWARE_URL",
    "DOWNLOADED_FIRMWARE_DIR_REL",
    "FIRMWARE_DIR_REL",
    "FirmwareNetworkError",
    "TRANSMITTER_FIRMWARE_BASENAME",
    "TRANSMITTER_FIRMWARE_FILENAME",
    "TRANSMITTER_FIRMWARE_URL",
    "check_latest_console_firmware_version",
    "check_latest_transmitter_firmware_version",
    "download_latest_console_firmware",
    "download_latest_transmitter_firmware",
    "get_console_firmware_path",
    "get_console_firmware_version",
    "get_transmitter_firmware_path",
    "get_transmitter_firmware_version",
]

