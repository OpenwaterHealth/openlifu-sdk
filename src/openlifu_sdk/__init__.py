from __future__ import annotations

try:
    # Written by setuptools-scm at build time (version_file in pyproject.toml);
    # reflects the git tag the distribution was built from.
    from openlifu_sdk._version import __version__
except ImportError:
    # Source-tree / editable use without a build: fall back to the installed
    # distribution metadata, or a placeholder when not installed at all.
    try:
        from importlib.metadata import PackageNotFoundError, version

        __version__ = version("openlifu-sdk")
    except PackageNotFoundError:
        __version__ = "0.0.0+unknown"

from openlifu_sdk.io.LIFUInterface import LIFUInterface, LIFUInterfaceStatus

__all__ = [
    "LIFUInterface",
    "LIFUInterfaceStatus",
    "__version__",
]
