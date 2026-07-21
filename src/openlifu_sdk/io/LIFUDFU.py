"""LIFU Firmware Update (DFU) support — transmitter and console.

Provides:
  - :func:`stm32_crc32`          — STM32-compatible CRC32
  - :func:`parse_signed_package` — parse/validate a transmitter 'PGK1' package
  - :class:`STM32USBDFU`         — USB DFU client (PyUSB; erase/write/read/version)
  - :class:`STM32I2CDFUviaMaster`— I2C DFU via OW UART master passthrough (modules 1+)
  - :class:`LIFUDFUManager`      — high-level firmware update orchestration:
      * transmitter modules: :meth:`LIFUDFUManager.update_module` (PGK1 packages)
      * console: :meth:`LIFUDFUManager.update_console` (SBSFU signed images from
        LIFUCrypto, with pre-erase validation and anti-downgrade checks)
"""

from __future__ import annotations

import logging
import struct
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from dataclasses import dataclass

from openlifu_sdk.io.LIFUConfig import OW_ERROR, OW_I2C_PASSTHRU

if TYPE_CHECKING:
    from openlifu_sdk.io.LIFUUart import LIFUUart

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional USB DFU dependencies (module 0 only)
# ---------------------------------------------------------------------------
try:
    import usb.core as _usb_core
    import usb.util as _usb_util
    import usb.backend.libusb1 as _usb_libusb1
    _USB_DFU_AVAILABLE = True
except ImportError:
    _usb_core = None
    _usb_util = None
    _usb_libusb1 = None
    _USB_DFU_AVAILABLE = False

try:
    import libusb_package as _libusb_package
except ImportError:
    _libusb_package = None


def _find_bundled_libusb_dll() -> str | None:
    """Return the path to the bundled libusb-1.0.dll for Windows, or None.

    Checks two locations in order:
    1. Installed-package location: ``<openlifu_sdk>/libusb/win64/libusb-1.0.dll``
       (included via setuptools ``package_data`` / ``MANIFEST.in`` entries).
    2. Repository / editable-install location: walks up from this file's
       directory looking for ``libusb-1.0.29/VS2022/{MS64|MS32}/dll/libusb-1.0.dll``.
    """
    if sys.platform != "win32":
        return None
    arch_dir = "win64" if struct.calcsize("P") == 8 else "win32"

    # 1. Installed wheel: <site-packages>/openlifu_sdk/libusb/<arch>/libusb-1.0.dll
    pkg_root = Path(__file__).parent.parent  # .../openlifu_sdk/
    candidate = pkg_root / "libusb" / arch_dir / "libusb-1.0.dll"
    if candidate.is_file():
        return str(candidate)

    # 2. Development / editable install: search up the directory tree
    ms_dir = "MS64" if arch_dir == "win64" else "MS32"
    for parent in Path(__file__).parents:
        dev_candidate = (
            parent / "libusb-1.0.29" / "VS2022" / ms_dir / "dll" / "libusb-1.0.dll"
        )
        if dev_candidate.is_file():
            return str(dev_candidate)

    return None

# ---------------------------------------------------------------------------
# DFU protocol constants (shared by USB and I2C paths)
# ---------------------------------------------------------------------------

# USB DFU virtual addresses (must match usbd_dfu_if.c)
USB_DFU_VERSION_VIRT_ADDR = 0xFFFFFF00
USB_DFU_VERSION_READ_LEN  = 64

# I2C DFU command bytes (must match i2c_dfu_if.h)
I2C_DFU_SLAVE_ADDR      = 0x72
I2C_DFU_CMD_DNLOAD      = 0x01
I2C_DFU_CMD_ERASE       = 0x02
I2C_DFU_CMD_GETSTATUS   = 0x03
I2C_DFU_CMD_MANIFEST    = 0x04
I2C_DFU_CMD_RESET       = 0x05
I2C_DFU_CMD_GETVERSION  = 0x06
I2C_DFU_STATUS_OK       = 0x00
I2C_DFU_STATUS_BUSY     = 0x01
I2C_DFU_STATUS_ERROR    = 0x02
I2C_DFU_STATUS_BAD_ADDR = 0x03
I2C_DFU_STATUS_FLASH_ERR= 0x04
I2C_DFU_STATE_DNBUSY    = 0x01
I2C_DFU_STATE_ERROR     = 0x04
# Maximum data bytes per write_block call.  The enclosing OW_I2C_PASSTHRU UART
# packet carries (1 cmd + 4 addr + 2 len) = 7 bytes of I2C-DFU header, so the
# total packet payload is I2C_DFU_MAX_XFER_SIZE + 7.  The master firmware hard-
# rejects any UART packet with data_len > DATA_MAX_SIZE (2048), so this value
# must be ≤ 2041.  Use 512 for a safe, standard I2C block size.
I2C_DFU_MAX_XFER_SIZE   = 512
I2C_DFU_VERSION_STR_MAX = 32


@dataclass
class DeviceProfile:
    name: str
    transfer_size: int
    version_read_len: int
    program_alignment_bytes: int
    app_default_address: int | None = None
    reset_virt_addr: int | None = 0xFFFFFF08

# Built-in profiles
TRANSMITTER_PROFILE = DeviceProfile(
    name="transmitter",
    transfer_size=1024,
    version_read_len=64,
    program_alignment_bytes=8,
    app_default_address=None,
    reset_virt_addr=0xFFFFFF08,
)

CONSOLE_PROFILE = DeviceProfile(
    name="console",
    transfer_size=1024,
    version_read_len=64,            # matches DFU_VERSION_READ_LEN in usbd_dfu_if.c
    program_alignment_bytes=4,
    app_default_address=0x08010000,  # SBSFU active slot (console memory_map.h)
    reset_virt_addr=0xFFFFFF08,
)

# Console SBSFU active slot: the signed image (320 B header + app @ +0x400)
# is written here; the bootloader verifies and launches it after manifest.
CONSOLE_SLOT_BASE = 0x08010000

# Console flash base — where the bootloader itself lives. Writable only from
# the STM32 ROM DFU (full-flash access); the legacy and secure bootloaders
# both refuse writes to their own region.
CONSOLE_FLASH_BASE = 0x08000000


def find_stm32_programmer_cli() -> str | None:
    """Locate the STM32CubeProgrammer CLI (STM32_Programmer_CLI), or None.

    Checks $STM32_PROGRAMMER_CLI, PATH, and the default Windows/macOS/Linux
    install locations. STM32CubeProgrammer provides a rock-solid USB-DFU
    implementation used for the bootloader-replacement write, where a
    pure-Python DfuSe write against the STM32 ROM loader is unreliable.
    """
    import os
    import shutil

    env = os.environ.get("STM32_PROGRAMMER_CLI")
    if env and Path(env).is_file():
        return env
    exe = "STM32_Programmer_CLI.exe" if sys.platform == "win32" else "STM32_Programmer_CLI"
    onpath = shutil.which(exe)
    if onpath:
        return onpath
    candidates = [
        r"C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin",
        r"C:\Program Files (x86)\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin",
        "/Applications/STMicroelectronics/STM32Cube/STM32CubeProgrammer/STM32CubeProgrammer.app/Contents/MacOs/bin",
        str(Path.home() / "STM32CubeProgrammer" / "bin"),
    ]
    for base in candidates:
        p = Path(base) / exe
        if p.is_file():
            return str(p)
    return None


def bundled_updater_path() -> Path:
    """Path to the RAM-resident legacy-migration updater shipped with the SDK
    (``firmware/updater.bin``).

    This is the one-time, keyless self-updater used by
    :meth:`LIFUDFUManager.migrate_console_legacy`. It needs no signing key: the
    legacy bootloader authenticates it with an HMAC "trust tag" that the SDK
    computes from the updater bytes at run time (``build_legacy_metadata``)."""
    return Path(__file__).parent.parent / "firmware" / "updater.bin"


def split_console_flash_image(image: bytes) -> tuple[bytes, bytes]:
    """Split a combined full-flash console image (bootloader + signed app,
    starting at 0x08000000) into ``(bootloader_bytes, signed_app_bytes)``.

    The bootloader occupies flash up to the SBSFU slot base (offset
    ``CONSOLE_SLOT_BASE - CONSOLE_FLASH_BASE`` = 0x10000); the signed app
    ('SFU1' header) begins there. Trailing 0xFF fill on the bootloader
    region is trimmed to the last non-blank 2 KB page.

    Raises:
        ValueError: Image too small or no 'SFU1' app header at the slot.
    """
    slot_off = CONSOLE_SLOT_BASE - CONSOLE_FLASH_BASE
    if len(image) <= slot_off:
        raise ValueError(
            f"combined image is {len(image)} B; need > 0x{slot_off:X} "
            "(bootloader region + signed app)")
    if image[slot_off:slot_off + 4] != b"SFU1":
        raise ValueError(
            f"no 'SFU1' signed-app header at slot offset 0x{slot_off:X}; "
            "this does not look like a combined bootloader+app image")

    bl_region = image[:slot_off]
    # Trim trailing blank flash, but keep a whole 2 KB page granularity.
    trimmed = bl_region.rstrip(b"\xFF")
    page = 2048
    bl_len = ((len(trimmed) + page - 1) // page) * page if trimmed else 0
    return bl_region[:bl_len], image[slot_off:]

# ---------------------------------------------------------------------------
# Legacy (non-secure) bootloader image metadata
#
# The legacy F072 bootloader (openlifu-console-bl) boots an app only if a
# metadata block at 0x08007800 authenticates it. Validation accepts EITHER
# an HMAC-SHA256 "trust tag" OR an ECDSA-P256 signature; the trust tag is
# checked first. The HMAC key is SYMMETRIC and embedded in the bootloader
# (main.c g_bl_trust_hmac_key), so a valid metadata block can be produced
# with the trust tag alone - no ECDSA private key is needed (the repo ships
# only the ECDSA public key). The signature field is part of the HMAC input
# but its contents are irrelevant when the trust tag validates, so it is
# left zero.
# ---------------------------------------------------------------------------
LEGACY_META_ADDRESS   = 0x08007800
LEGACY_APP_ADDRESS    = 0x08008000
LEGACY_APP_MAX_SIZE   = 94 * 1024
LEGACY_META_MAGIC     = 0x314D4657   # 'WFM1'
LEGACY_META_VERSION   = 3
LEGACY_META_FLAG_SIG_REQUIRED = 0x0001
LEGACY_META_KEY_ID    = 1

# Trust HMAC key embedded in the legacy bootloader (main.c:58, key_id 1).
LEGACY_TRUST_HMAC_KEY = bytes([
    0x17, 0xB2, 0x05, 0x19, 0x59, 0x0C, 0xFD, 0x78,
    0x10, 0x4F, 0xCE, 0x50, 0x94, 0x91, 0x34, 0x5F,
    0x36, 0xEF, 0xF0, 0x47, 0xD0, 0x32, 0x9E, 0x78,
    0xAC, 0x65, 0x06, 0x51, 0xE6, 0x35, 0xB8, 0x7E,
])

_LEGACY_META_HMAC_INPUT = "<IHHIIII64s"      # magic..signature (88 bytes)
_LEGACY_META_WITHOUT_CRC = "<IHHIIII64s32s"  # + trust_tag (120 bytes)


def build_legacy_metadata(app_bytes: bytes,
                          trust_key: bytes = LEGACY_TRUST_HMAC_KEY,
                          fw_address: int = LEGACY_APP_ADDRESS,
                          key_id: int = LEGACY_META_KEY_ID) -> bytes:
    """Build a legacy-bootloader metadata block (124 bytes) that authenticates
    *app_bytes* via the HMAC trust tag.

    The block is written to ``LEGACY_META_ADDRESS`` (0x08007800) while the app
    goes to ``fw_address`` (0x08008000). Mirrors ``build_metadata_blob`` in the
    legacy repo's ``test/dfu-test.py`` (trust-tag path, zero signature).

    Raises:
        ValueError: App too large for the legacy slot, or bad key length.
    """
    import hashlib
    import hmac

    if len(trust_key) != 32:
        raise ValueError(f"trust key must be 32 bytes, got {len(trust_key)}")
    if not 0 < len(app_bytes) <= LEGACY_APP_MAX_SIZE:
        raise ValueError(
            f"app is {len(app_bytes)} bytes; legacy slot max is {LEGACY_APP_MAX_SIZE}")

    fw_len = len(app_bytes)
    fw_crc = stm32_crc32(app_bytes)
    signature = b"\x00" * 64   # unused: the trust tag authenticates the image

    hmac_input = struct.pack(
        _LEGACY_META_HMAC_INPUT,
        LEGACY_META_MAGIC, LEGACY_META_VERSION, LEGACY_META_FLAG_SIG_REQUIRED,
        fw_address, fw_len, fw_crc, key_id, signature,
    )
    trust_tag = hmac.new(trust_key, hmac_input, hashlib.sha256).digest()

    meta_wo_crc = struct.pack(
        _LEGACY_META_WITHOUT_CRC,
        LEGACY_META_MAGIC, LEGACY_META_VERSION, LEGACY_META_FLAG_SIG_REQUIRED,
        fw_address, fw_len, fw_crc, key_id, signature, trust_tag,
    )
    return meta_wo_crc + struct.pack("<I", stm32_crc32(meta_wo_crc))

# ---------------------------------------------------------------------------
# Console DFU environment detection
#
# All three console DFU environments enumerate as VID:PID 0483:DF11; the USB
# product string tells them apart:
#   STM32 ROM DFU        : "STM32 BOOTLOADER"        (built-in system loader)
#   legacy bootloader    : "LIFU BL DFU 0.0.x"       (non-secure, app @ 0x08008000)
#   secure bootloader    : "OW DFU 1.x.x"            (SBSFU, app slot @ 0x08010000)
#                          "STM32 DownLoad Firmware Update"  (pre-branding
#                          secure builds; version via the DFU virtual address)
# ---------------------------------------------------------------------------

DFU_KIND_ROM     = "stm32-rom"
DFU_KIND_LEGACY  = "legacy-bl"
DFU_KIND_SECURE  = "secure-bl"
DFU_KIND_NONE    = "no-bootloader"
DFU_KIND_UNKNOWN = "unknown"


def infer_console_bootloader_from_app_version(app_version: str) -> str:
    """Infer which bootloader generation a console unit carries from the
    version its RUNNING application reports (before entering any DFU mode).

    Fleet rules:
      app >= 1.2.6           -> secure bootloader (``DFU_KIND_SECURE``)
      1.2.0 <= app < 1.2.6   -> legacy bootloader (``DFU_KIND_LEGACY``)
      app < 1.2.0            -> no bootloader; the app boots directly and can
                                jump to STM32 ROM DFU (``DFU_KIND_NONE``)

    *app_version* accepts plain or git-describe semver ("1.2.6",
    "1.2.6-rc.1-3-gabc", "v1.1.4").

    Raises:
        ValueError: Unparseable version string.
    """
    base = app_version.strip().lstrip("v").split("-")[0].split("+")[0]
    parts = base.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"Invalid app version: {app_version!r} (want 'M.m.p')")
    ver = tuple(int(p) for p in parts)

    if ver >= (1, 2, 6):
        return DFU_KIND_SECURE
    if ver >= (1, 2, 0):
        return DFU_KIND_LEGACY
    return DFU_KIND_NONE

# OW_I2C_PASSTHRU sub-commands (must match firmware if_commands.c handler)
_PASSTHRU_WRITE       = 0x00   # write only
_PASSTHRU_WRITE_READ  = 0x01   # write then delay 5 ms then read

# Signed package format (must match dfu-test.py)
_PKG_MAGIC        = 0x314B4750   # 'PGK1'
_PKG_VERSION      = 1
_PKG_HDR_NOCRC    = "<IHHIIIII"
_PKG_HDR_FULL     = "<IHHIIIIII"


# ---------------------------------------------------------------------------
# Package helpers
# ---------------------------------------------------------------------------

def stm32_crc32(data: bytes, init: int = 0xFFFFFFFF) -> int:
    """Compute CRC32 compatible with the STM32 CRC peripheral (poly=0x04C11DB7)."""
    poly = 0x04C11DB7
    crc = init & 0xFFFFFFFF
    for b in data:
        crc ^= (b & 0xFF) << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ poly) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc


def parse_signed_package(pkg: bytes) -> dict:
    """Parse and integrity-check a signed firmware package.

    Returns a dict with keys: ``fw_address``, ``meta_address``, ``fw``, ``meta``.

    Raises:
        ValueError: If the package is malformed or any CRC fails.
    """
    hdr_size = struct.calcsize(_PKG_HDR_FULL)
    if len(pkg) < hdr_size:
        raise ValueError("signed package too small")

    (magic, version, declared_hdr_size,
     fw_address, fw_len,
     meta_address, meta_len,
     payload_crc, header_crc) = struct.unpack(_PKG_HDR_FULL, pkg[:hdr_size])

    if magic != _PKG_MAGIC:
        raise ValueError(f"signed package magic mismatch: 0x{magic:08X}")
    if version != _PKG_VERSION:
        raise ValueError(f"signed package version mismatch: {version}")
    if declared_hdr_size != hdr_size:
        raise ValueError("signed package header size mismatch")

    calc_hdr_crc = stm32_crc32(pkg[:hdr_size - 4])
    if header_crc != calc_hdr_crc:
        raise ValueError(
            f"header CRC mismatch: pkg=0x{header_crc:08X}, calc=0x{calc_hdr_crc:08X}"
        )

    payload_len = fw_len + meta_len
    payload = pkg[hdr_size:]
    if len(payload) != payload_len:
        raise ValueError(
            f"payload size mismatch: expected {payload_len}, got {len(payload)}"
        )

    calc_payload_crc = stm32_crc32(payload)
    if payload_crc != calc_payload_crc:
        raise ValueError(
            f"payload CRC mismatch: pkg=0x{payload_crc:08X}, calc=0x{calc_payload_crc:08X}"
        )

    return {
        "fw_address":   fw_address,
        "meta_address": meta_address,
        "fw":           payload[:fw_len],
        "meta":         payload[fw_len:],
    }


# ---------------------------------------------------------------------------
# USB DFU client  (module 0)
# ---------------------------------------------------------------------------

class STM32USBDFU:
    """Minimal STM32 DfuSe USB client using PyUSB.

    Supports Set-Address-Pointer, page erase, memory write and DFU UPLOAD
    (used to read the bootloader version string).

    Requires: ``pip install pyusb``  plus a libusb-1.0 backend.
    """

    # DFU class requests
    DFU_DNLOAD    = 1
    DFU_UPLOAD    = 2
    DFU_GETSTATUS = 3
    DFU_CLRSTATUS = 4
    DFU_ABORT     = 6

    # DfuSe DNLOAD block 0 sub-commands
    CMD_SET_ADDRESS_POINTER = 0x21
    CMD_ERASE               = 0x41

    # DFU state values
    STATE_DFU_DNLOAD_SYNC         = 3
    STATE_DFU_DNLOAD_BUSY         = 4
    STATE_DFU_DNLOAD_IDLE         = 5
    STATE_DFU_MANIFEST_SYNC       = 6
    STATE_DFU_MANIFEST            = 7
    STATE_DFU_MANIFEST_WAIT_RESET = 8
    STATE_DFU_ERROR               = 10

    def __init__(self, vid: int = 0x0483, pid: int = 0xDF11,
                 transfer_size: int = 1024, timeout_ms: int = 4000,
                 libusb_dll: str | None = None,
                 device_profile: "DeviceProfile" | None = None):
        if not _USB_DFU_AVAILABLE:
            raise RuntimeError(
                "PyUSB not available. Install with: pip install pyusb"
            )
        self.vid = vid
        self.pid = pid
        self.transfer_size = transfer_size
        self.timeout_ms = timeout_ms
        self.libusb_dll = libusb_dll
        self.dev = None
        self.intf = None
        self._backend = None
        # device_profile may override transfer_size and provide read_len/alignment
        if device_profile is not None:
            self.transfer_size = int(device_profile.transfer_size)
            self.version_read_len = int(device_profile.version_read_len)
            self.program_alignment = int(device_profile.program_alignment_bytes)
        else:
            self.version_read_len = USB_DFU_VERSION_READ_LEN
            # default alignment (safe): 8 (transmitter L4 uses 8-byte doubleword)
            self.program_alignment = 8

    def _get_backend(self):
        if self._backend is not None:
            return self._backend
        if self.libusb_dll:
            self._backend = _usb_libusb1.get_backend(
                find_library=lambda _: self.libusb_dll
            )
        elif _libusb_package is not None:
            self._backend = _usb_libusb1.get_backend(
                find_library=_libusb_package.find_library
            )
        else:
            bundled_dll = _find_bundled_libusb_dll()
            if bundled_dll:
                logger.debug("Using bundled libusb DLL: %s", bundled_dll)
                self._backend = _usb_libusb1.get_backend(
                    find_library=lambda _: bundled_dll
                )
            else:
                self._backend = _usb_libusb1.get_backend()
        return self._backend

    def open(self) -> "STM32USBDFU":
        self.dev = _usb_core.find(
            idVendor=self.vid, idProduct=self.pid, backend=self._get_backend()
        )
        if self.dev is None:
            raise RuntimeError(
                f"USB DFU device not found: VID=0x{self.vid:04X}, PID=0x{self.pid:04X}"
            )
        self.dev.set_configuration()
        cfg = self.dev.get_active_configuration()
        for intf in cfg:
            if (intf.bInterfaceClass == 0xFE
                    and intf.bInterfaceSubClass == 0x01
                    and intf.bInterfaceProtocol == 0x02):
                self.intf = intf
                break
        if self.intf is None:
            raise RuntimeError("No DFU interface found on USB device")
        try:
            if self.dev.is_kernel_driver_active(self.intf.bInterfaceNumber):
                self.dev.detach_kernel_driver(self.intf.bInterfaceNumber)
        except (NotImplementedError, Exception):
            pass
        _usb_util.claim_interface(self.dev, self.intf.bInterfaceNumber)
        self._clear_error_state()
        return self

    def close(self) -> None:
        if self.dev is not None and self.intf is not None:
            try:
                _usb_util.release_interface(self.dev, self.intf.bInterfaceNumber)
            except Exception:
                pass
            _usb_util.dispose_resources(self.dev)
        self.dev = None
        self.intf = None

    def __enter__(self) -> "STM32USBDFU":
        return self.open()

    def __exit__(self, *args) -> None:
        self.close()

    # --- low-level USB control transfers ---

    def _ctrl_out(self, req: int, value: int, data: bytes = b"") -> int:
        return self.dev.ctrl_transfer(
            0x21, req, value, self.intf.bInterfaceNumber,
            data, timeout=self.timeout_ms
        )

    def _ctrl_in(self, req: int, value: int, length: int) -> bytes:
        return bytes(self.dev.ctrl_transfer(
            0xA1, req, value, self.intf.bInterfaceNumber,
            length, timeout=self.timeout_ms
        ))

    def get_status(self) -> dict:
        raw = self._ctrl_in(self.DFU_GETSTATUS, 0, 6)
        poll_ms = raw[1] | (raw[2] << 8) | (raw[3] << 16)
        return {"status": raw[0], "poll_timeout_ms": poll_ms, "state": raw[4]}

    def clear_status(self) -> None:
        self._ctrl_out(self.DFU_CLRSTATUS, 0, b"")

    def abort(self) -> None:
        self._ctrl_out(self.DFU_ABORT, 0, b"")

    def _clear_error_state(self) -> None:
        for _ in range(3):
            st = self.get_status()
            if st["state"] != self.STATE_DFU_ERROR:
                break
            self.clear_status()

    def _recover_idle(self) -> None:
        for _ in range(4):
            st = self.get_status()
            if st["state"] in (
                self.STATE_DFU_DNLOAD_IDLE,
                self.STATE_DFU_MANIFEST_WAIT_RESET,
            ):
                self.abort()
            elif st["state"] == self.STATE_DFU_ERROR:
                self.clear_status()
            else:
                break

    def _wait_while_busy(self) -> dict:
        busy = {
            self.STATE_DFU_DNLOAD_SYNC,
            self.STATE_DFU_DNLOAD_BUSY,
            self.STATE_DFU_MANIFEST_SYNC,
            self.STATE_DFU_MANIFEST,
        }
        while True:
            st = self.get_status()
            if st["state"] not in busy:
                return st
            time.sleep(max(st["poll_timeout_ms"] / 1000.0, 0.005))

    def _dnload(self, block_num: int, payload: bytes) -> dict:
        self._recover_idle()
        try:
            self._ctrl_out(
                self.DFU_DNLOAD, block_num, bytes(payload) if payload else b""
            )
        except Exception as e:
            if "timeout" not in str(e).lower():
                raise
        return self._wait_while_busy()

    def _set_address(self, address: int) -> None:
        payload = bytes([self.CMD_SET_ADDRESS_POINTER]) + struct.pack("<I", address)
        self._dnload(0, payload)

    def _erase_page(self, address: int) -> None:
        payload = bytes([self.CMD_ERASE]) + struct.pack("<I", address)
        self._dnload(0, payload)

    def get_version(self) -> str:
        """Read bootloader version string via DFU UPLOAD from the virtual address."""
        self._set_address(USB_DFU_VERSION_VIRT_ADDR)
        self.abort()
        raw = self._ctrl_in(self.DFU_UPLOAD, 2, self.version_read_len)
        try:
            self._wait_while_busy()
        except Exception:
            pass
        self.abort()
        return raw.rstrip(b"\x00").decode("ascii", errors="replace")

    def erase_pages(self, start_addr: int, end_addr: int,
                    page_size: int = 2048) -> None:
        """Explicitly page-erase every flash page in [start_addr, end_addr).

        Per-page DfuSe erase is used rather than the DfuSe "mass erase"
        (0x41 with no address): the STM32 F0 ROM loader has been observed to
        silently no-op the mass-erase command, leaving stale flash that then
        corrupts writes. Per-page erase is the reliable primitive (it is what
        write_memory(page_erase=True) uses, and is verified correct)."""
        addr = start_addr & ~(page_size - 1)
        while addr < end_addr:
            self._erase_page(addr)
            addr += page_size

    def read_memory(self, address: int, length: int) -> bytes:
        """Read *length* bytes from target memory via DFU UPLOAD.

        The bootloader's read window applies (the console rejects reads
        outside the application slot).
        """
        self._recover_idle()
        self._set_address(address)
        self.abort()   # back to dfuIDLE so UPLOAD starts at block 2

        out = bytearray()
        block = 2
        while len(out) < length:
            want = min(self.transfer_size, length - len(out))
            chunk = self._ctrl_in(self.DFU_UPLOAD, block, want)
            if not chunk:
                break
            out += chunk
            block += 1
        self.abort()
        return bytes(out[:length])

    def write_memory(self, address: int, data: bytes,
                     page_erase: bool = True,
                     progress_callback: Callable | None = None) -> None:
        """Write data to target flash, optionally erasing each 2 KB page first.

        IMPORTANT: All page erases are performed before any data is written.
        The STM32 DFU middleware (usbd_dfu.c) updates ``data_ptr`` when it
        processes an ERASE command (block 0), which would corrupt the write
        addresses of subsequent data blocks if erases and writes were
        interleaved.  Separating the two phases — erase all required pages
        first, then set the address pointer once and write sequentially —
        matches the behaviour of dfu-test.py and avoids this issue.
        """
        total = len(data)
        page_size = 2048
        # enforce alignment expectations from device: address and chunk lengths
        if (address % getattr(self, "program_alignment", 1)) != 0:
            raise RuntimeError(
                f"write_memory: start address 0x{address:08X} not aligned to "
                f"{self.program_alignment} bytes"
            )

        # Phase 1: erase all required pages up-front (before setting the
        # address pointer for writes).  The ERASE DfuSe command also updates
        # data_ptr in the STM32 middleware, so erases must be completed before
        # any data DNLOAD block is sent.
        if page_erase and data:
            first_page = address & ~(page_size - 1)
            last_page = (address + total - 1) & ~(page_size - 1)
            page = first_page
            while page <= last_page:
                self._erase_page(page)
                page += page_size

        # Phase 2: set address pointer once, then write all data blocks.
        self._recover_idle()
        self._set_address(address)
        block = 2
        written = 0
        for offset in range(0, total, self.transfer_size):
            chunk = data[offset:offset + self.transfer_size]
            # pad final chunk to program_alignment if required by bootloader
            align = getattr(self, "program_alignment", 1)
            if align > 1 and (len(chunk) % align) != 0:
                pad_len = align - (len(chunk) % align)
                chunk = chunk + (b"\xFF" * pad_len)
            self._dnload(block, chunk)
            block += 1
            written += len(chunk)
            if progress_callback:
                progress_callback(written, total, "USB DFU write")
        self.abort()

    def manifest(self) -> None:
        """Send zero-length DNLOAD to trigger DFU manifestation (launches firmware)."""
        self._recover_idle()
        try:
            self._ctrl_out(self.DFU_DNLOAD, 0, b"")
            self._wait_while_busy()
        except Exception:
            pass  # device disconnects during manifest — expected

    def trigger_reset(self, reset_vaddr: int = 0xFFFFFF08) -> None:
        """Reset the device via the bootloader's virtual reset address: a data
        DNLOAD there makes the DFU media handler call NVIC_SystemReset.

        Uses the full DNLOAD path (block 2 + GETSTATUS) - the GETSTATUS is what
        drives the middleware to actually perform the write (and thus the
        reset); a bare control transfer without it does nothing. The device
        drops off USB as it reboots, which is expected."""
        try:
            self._recover_idle()
            self._set_address(reset_vaddr)
            try:
                self._dnload(2, b"\x00\x00\x00\x00")   # writes @vaddr -> reset
            except Exception:
                pass   # device resets mid-transfer / USB drops
        except Exception:
            pass


# ---------------------------------------------------------------------------
# I2C DFU client via OW master passthrough  (modules 1+)
# ---------------------------------------------------------------------------

class STM32I2CDFUviaMaster:
    """I2C DFU client that routes all I2C transactions through the USB-master
    module via the ``OW_I2C_PASSTHRU`` UART packet type.

    The master firmware receives the passthrough request and executes the raw
    I2C write (and optional read) on the global I2C bus toward the slave DFU
    bootloader at *i2c_addr* (default 0x72).

    Packet wire format used::

        packetType = OW_I2C_PASSTHRU (0xE9)
        addr       = 7-bit I2C slave address
        command    = 0x00  write-only
                   = 0x01  write, 5 ms delay, read back <reserved> bytes
        reserved   = number of bytes to read back (command 0x01 only, max 255)
        data       = raw bytes to write
    """

    def __init__(self, uart: "LIFUUart",
                 i2c_addr: int = I2C_DFU_SLAVE_ADDR,
                 write_read_delay_s: float = 0.005):
        self._uart = uart
        self._addr = i2c_addr
        self._wr_delay = write_read_delay_s

    # --- low-level transport primitives ---

    def _write(self, payload: bytes) -> None:
        """Send a write-only passthrough packet to the I2C slave."""
        r = self._uart.send_packet(
            packet_id=None,
            packet_type=OW_I2C_PASSTHRU,
            command=_PASSTHRU_WRITE,
            addr=self._addr,
            reserved=0,
            data=payload,
        )
        if r is None or r.packet_type == OW_ERROR:
            raise RuntimeError(
                f"I2C passthrough write failed (addr=0x{self._addr:02X}, "
                f"payload={payload[:8].hex()}...)"
            )

    def _exchange(self, payload: bytes, read_len: int,
                  pre_read_delay_s: float | None = None) -> bytes:
        """Write *payload* to the I2C slave, wait, then read *read_len* bytes back.

        The firmware inserts a fixed 5 ms gap between write and read.
        An optional extra host-side delay can be added via *pre_read_delay_s*
        (not usually needed).
        """
        if pre_read_delay_s and pre_read_delay_s > 0:
            time.sleep(pre_read_delay_s)

        r = self._uart.send_packet(
            packet_id=None,
            packet_type=OW_I2C_PASSTHRU,
            command=_PASSTHRU_WRITE_READ,
            addr=self._addr,
            reserved=read_len,
            data=payload,
        )
        if r is None or r.packet_type == OW_ERROR:
            raise RuntimeError(
                f"I2C passthrough exchange failed (addr=0x{self._addr:02X}, "
                f"want_rx={read_len})"
            )
        return bytes(r.data[:read_len]) if (r.data and len(r.data) >= read_len) \
               else bytes(read_len)

    # --- DFU protocol commands ---

    def get_status(self) -> dict:
        """Send CMD_GETSTATUS and return status/state."""
        raw = self._exchange(bytes([I2C_DFU_CMD_GETSTATUS]), 2)
        return {"status": raw[0], "state": raw[1]}

    def _wait_while_busy(self, timeout_s: float = 10.0) -> dict:
        _ERROR_STATUSES = (I2C_DFU_STATUS_ERROR, I2C_DFU_STATUS_BAD_ADDR, I2C_DFU_STATUS_FLASH_ERR)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            st = self.get_status()
            if st["state"] == I2C_DFU_STATE_ERROR or st["status"] in _ERROR_STATUSES:
                raise RuntimeError(
                    f"I2C DFU error: status=0x{st['status']:02X}, "
                    f"state=0x{st['state']:02X}"
                )
            if (st["status"] != I2C_DFU_STATUS_BUSY
                    and st["state"] != I2C_DFU_STATE_DNBUSY):
                return st
            time.sleep(0.020)
        raise TimeoutError(f"I2C DFU timed out after {timeout_s:.0f} s")

    def erase_page(self, address: int) -> None:
        """Erase the flash page containing *address*."""
        self._write(struct.pack("<BI", I2C_DFU_CMD_ERASE, address))
        self._wait_while_busy(timeout_s=10.0)

    def mass_erase(self) -> None:
        """Erase the entire application flash region (sentinel addr = 0xFFFFFFFF)."""
        self._write(struct.pack("<BI", I2C_DFU_CMD_ERASE, 0xFFFFFFFF))
        self._wait_while_busy(timeout_s=120.0)

    def write_block(self, address: int, data: bytes) -> None:
        """Program one block (≤ ``I2C_DFU_MAX_XFER_SIZE`` bytes)."""
        if not data:
            return
        payload = struct.pack("<BIH", I2C_DFU_CMD_DNLOAD, address, len(data)) + data
        self._write(payload)
        self._wait_while_busy(timeout_s=10.0)

    def write_memory(self, address: int, data: bytes,
                     progress_callback: Callable | None = None) -> None:
        """Write arbitrary-length data in ``I2C_DFU_MAX_XFER_SIZE``-byte chunks."""
        total = len(data)
        written = 0
        for offset in range(0, total, I2C_DFU_MAX_XFER_SIZE):
            chunk = data[offset:offset + I2C_DFU_MAX_XFER_SIZE]
            self.write_block(address + offset, chunk)
            written += len(chunk)
            if progress_callback:
                progress_callback(written, total, "I2C DFU write")

    def manifest(self) -> None:
        """Send CMD_MANIFEST to finalise the download and lock flash."""
        self._write(bytes([I2C_DFU_CMD_MANIFEST]))
        self._wait_while_busy(timeout_s=10.0)

    def reset(self) -> None:
        """Send CMD_RESET; the device reboots immediately (no response)."""
        self._write(bytes([I2C_DFU_CMD_RESET]))

    def get_version(self) -> str:
        """Read the null-terminated bootloader version string."""
        read_len = 2 + I2C_DFU_VERSION_STR_MAX
        raw = self._exchange(bytes([I2C_DFU_CMD_GETVERSION]), read_len)
        if raw[0] not in (I2C_DFU_STATUS_OK, I2C_DFU_STATUS_BUSY):
            raise RuntimeError(
                f"I2C DFU GETVERSION failed: status=0x{raw[0]:02X}"
            )
        return raw[2:].split(b"\x00")[0].decode("ascii", errors="replace")


# ---------------------------------------------------------------------------
# High-level firmware update manager
# ---------------------------------------------------------------------------

class LIFUDFUManager:
    """Orchestrates firmware updates for a single LIFU transmitter module.

    Usage::

        from openlifu_sdk.io.LIFUDFU import LIFUDFUManager
        mgr = LIFUDFUManager(uart=txdevice.uart)
        mgr.update_module(
            module=1,
            package_file="path/to/lifu-transmitter-fw.bin.signed.bin",
            enter_dfu_fn=txdevice.enter_dfu,
        )
    """

    def __init__(self, uart: "LIFUUart | None" = None):
        """*uart* is only needed for the I2C passthrough paths (transmitter
        modules 1+); USB-only use (console, transmitter module 0) may omit it."""
        self._uart = uart

    # --- per-transport helpers ---

    def get_bootloader_version_usb(self, vid: int = 0x0483, pid: int = 0xDF11,
                                   libusb_dll: str | None = None) -> str:
        """Read bootloader version string from module 0 via USB DFU."""
        with STM32USBDFU(vid=vid, pid=pid, libusb_dll=libusb_dll) as dfu:
            return dfu.get_version()

    def get_bootloader_version_i2c(self, i2c_addr: int = I2C_DFU_SLAVE_ADDR) -> str:
        """Read bootloader version string from a slave module via I2C passthrough."""
        dfu = STM32I2CDFUviaMaster(uart=self._uart, i2c_addr=i2c_addr)
        return dfu.get_version()

    def program_usb(self, package_file: str,
                    vid: int = 0x0483, pid: int = 0xDF11,
                    libusb_dll: str | None = None,
                    device_type: str = "transmitter",
                    progress_callback: Callable | None = None) -> None:
        """Program a signed package to module 0 via USB DFU.

        The module must already be in DFU bootloader mode.
        """
        with open(package_file, "rb") as f:
            pkg_blob = f.read()
        pkg = parse_signed_package(pkg_blob)

        logger.info(
            "USB DFU: fw %d B @ 0x%08X, meta %d B @ 0x%08X",
            len(pkg["fw"]), pkg["fw_address"],
            len(pkg["meta"]), pkg["meta_address"],
        )
        if device_type not in ("transmitter", "console"):
            raise ValueError(
                f"Unknown device_type {device_type!r}; expected 'transmitter' or 'console'."
            )
        profile = TRANSMITTER_PROFILE if device_type == "transmitter" else CONSOLE_PROFILE
        with STM32USBDFU(vid=vid, pid=pid, libusb_dll=libusb_dll,
                         device_profile=profile) as dfu:
            dfu.write_memory(
                pkg["fw_address"], pkg["fw"],
                page_erase=True, progress_callback=progress_callback
            )
            dfu.write_memory(
                pkg["meta_address"], pkg["meta"],
                page_erase=True, progress_callback=progress_callback
            )
            logger.info("USB DFU: sending manifest...")
            dfu.manifest()
        logger.info("USB DFU: programming complete.")

    # --- console (SBSFU signed image) path ---

    def detect_console_dfu_kind(self, vid: int = 0x0483, pid: int = 0xDF11,
                                libusb_dll: str | None = None
                                ) -> tuple[str, str]:
        """Identify which DFU environment the enumerated device is running.

        Returns ``(kind, version)`` where kind is one of ``DFU_KIND_ROM``,
        ``DFU_KIND_LEGACY``, ``DFU_KIND_SECURE`` or ``DFU_KIND_UNKNOWN``, and
        version is the bootloader version string when one can be determined
        ("" for the ROM loader / unknown).

        The primary discriminator is the USB product string (read without a
        single DFU transaction, so it is safe on all three environments).
        Pre-branding secure bootloaders report the generic CubeMX string, so
        for those the version is read via the DFU virtual version address.

        Raises:
            RuntimeError: No DFU device enumerated, or backend unavailable.
        """
        probe = STM32USBDFU(vid=vid, pid=pid, libusb_dll=libusb_dll)
        dev = _usb_core.find(idVendor=vid, idProduct=pid,
                             backend=probe._get_backend())
        if dev is None:
            raise RuntimeError(
                f"No USB DFU device found (VID=0x{vid:04X}, PID=0x{pid:04X})")
        try:
            product = _usb_util.get_string(dev, dev.iProduct) or ""
        finally:
            _usb_util.dispose_resources(dev)
        logger.info("DFU product string: %r", product)

        # Normalize runs of whitespace: the STM32 ROM loader reports
        # "STM32  BOOTLOADER" (two spaces) on many parts.
        norm = " ".join(product.split())
        if norm.startswith("STM32 BOOTLOADER"):
            return (DFU_KIND_ROM, "")
        if norm.startswith("LIFU BL DFU"):
            return (DFU_KIND_LEGACY, norm.removeprefix("LIFU BL DFU").strip())
        if norm.startswith("OW DFU"):
            return (DFU_KIND_SECURE, norm.removeprefix("OW DFU").strip())
        if norm.startswith("STM32 DownLoad Firmware Update"):
            # Pre-branding secure bootloader: confirm via the version probe
            try:
                with STM32USBDFU(vid=vid, pid=pid, libusb_dll=libusb_dll,
                                 device_profile=CONSOLE_PROFILE) as dfu:
                    return (DFU_KIND_SECURE, dfu.get_version())
            except Exception as e:
                logger.warning("Secure-BL version probe failed: %s", e)
                return (DFU_KIND_SECURE, "")
        return (DFU_KIND_UNKNOWN, "")

    def _wait_for_dfu_kind(self, expected: str, vid: int = 0x0483,
                           pid: int = 0xDF11, libusb_dll: str | None = None,
                           timeout_s: float = 40.0) -> str:
        """Poll until the console DFU environment settles on *expected* kind.

        Tolerant of the transient states across a reboot (device absent, wrong
        kind briefly, USB re-enumeration): keeps trying until the expected kind
        is seen or *timeout_s* elapses. Returns the version string.
        """
        deadline = time.monotonic() + timeout_s
        last = None
        while time.monotonic() < deadline:
            try:
                kind, ver = self.detect_console_dfu_kind(
                    vid=vid, pid=pid, libusb_dll=libusb_dll)
                last = (kind, ver)
                if kind == expected:
                    return ver
            except Exception as e:
                last = str(e)
            time.sleep(1.0)
        raise RuntimeError(
            f"Timed out waiting for DFU kind {expected!r} after {timeout_s:.0f}s "
            f"(last seen: {last!r})")

    def get_console_bootloader_version(self, vid: int = 0x0483, pid: int = 0xDF11,
                                       libusb_dll: str | None = None,
                                       timeout_s: float = 30.0) -> str:
        """Wait for the console DFU device to enumerate (up to *timeout_s*)
        and return its bootloader version string (the bootloader's git
        describe, read from the DFU virtual version address).

        Raises:
            RuntimeError: Device did not enumerate within the timeout.
        """
        return self._wait_for_usb_dfu(
            vid=vid, pid=pid, libusb_dll=libusb_dll,
            timeout_s=timeout_s, device_profile=CONSOLE_PROFILE,
        )

    def get_console_installed_version(self, vid: int = 0x0483, pid: int = 0xDF11,
                                      libusb_dll: str | None = None) -> int | None:
        """Read the FwVersion of the image currently installed in the console's
        active slot, or None if the slot holds no valid SBSFU header.

        The console must be in USB DFU mode.
        """
        from openlifu_sdk.io import LIFUCrypto

        with STM32USBDFU(vid=vid, pid=pid, libusb_dll=libusb_dll,
                         device_profile=CONSOLE_PROFILE) as dfu:
            hdr_bytes = dfu.read_memory(CONSOLE_SLOT_BASE,
                                        LIFUCrypto.HEADER_TOTAL_LEN)
        try:
            header = LIFUCrypto.FirmwareHeader.from_bytes(hdr_bytes)
        except LIFUCrypto.LIFUCryptoError:
            return None
        if header.magic != LIFUCrypto.SFU_MAGIC:
            return None
        return header.fw_version

    def program_console(self, signed_image: str,
                        keys_dir: str | None = None,
                        force: bool = False,
                        vid: int = 0x0483, pid: int = 0xDF11,
                        libusb_dll: str | None = None,
                        progress_callback: Callable | None = None) -> None:
        """Program a console SBSFU signed image (from LIFUCrypto / the
        bootloader signing tools) into the active slot via USB DFU.

        Pre-flight checks run BEFORE any flash erase, so a rejected image
        leaves the installed firmware untouched (unlike naive erase-first
        flashers, which strand the board with an empty slot when the
        bootloader's anti-rollback refuses the new image at boot):

          1. The image is validated locally (structure, sizes, SHA-256 tag;
             plus the ECDSA signature when *keys_dir* is given).
          2. The installed slot header is read back over DFU and a version
             DOWNGRADE is refused unless *force* is set. Note this compares
             against the installed image only - the bootloader's persistent
             anti-rollback floor is not DFU-readable and remains the final
             authority at boot.

        The console must already be in USB DFU mode.

        Raises:
            ValueError: Image invalid, or downgrade without *force*.
            RuntimeError: DFU device/communication problems.
        """
        from openlifu_sdk.io import LIFUCrypto

        image = Path(signed_image).read_bytes()

        report = LIFUCrypto.validate_signed_image(image, keys_dir=keys_dir)
        if not (report.ok or (keys_dir is None and report.structural_ok)):
            raise ValueError(
                f"Refusing to flash invalid image {signed_image}:\n"
                + report.describe()
            )
        new_version = report.header.fw_version
        logger.info("Console image: version %d (%s), %d bytes",
                    new_version, report.header.fw_version_str, len(image))

        installed = self.get_console_installed_version(
            vid=vid, pid=pid, libusb_dll=libusb_dll)
        if installed is not None:
            logger.info("Installed image: version %d (%s)",
                        installed, LIFUCrypto.decode_fw_version(installed))
            if new_version < installed and not force:
                raise ValueError(
                    f"Downgrade refused before erase: image version {new_version} "
                    f"({report.header.fw_version_str}) is below the installed "
                    f"version {installed} "
                    f"({LIFUCrypto.decode_fw_version(installed)}). "
                    "Pass force=True to flash anyway (the bootloader's "
                    "anti-rollback floor may still reject it at boot, leaving "
                    "the slot empty)."
                )

        with STM32USBDFU(vid=vid, pid=pid, libusb_dll=libusb_dll,
                         device_profile=CONSOLE_PROFILE) as dfu:
            dfu.write_memory(CONSOLE_SLOT_BASE, image, page_erase=True,
                             progress_callback=progress_callback)
            logger.info("Console DFU: sending manifest (device will reset "
                        "and the bootloader will verify the image)...")
            dfu.manifest()
        logger.info("Console DFU: programming complete.")

    def update_console(self, signed_image: str,
                       enter_dfu_fn: Callable | None = None,
                       keys_dir: str | None = None,
                       force: bool = False,
                       vid: int = 0x0483, pid: int = 0xDF11,
                       libusb_dll: str | None = None,
                       dfu_wait_s: float = 2.0,
                       dfu_enum_timeout_s: float = 30.0,
                       progress_callback: Callable | None = None) -> str:
        """High-level console firmware update.

        Optionally calls *enter_dfu_fn()* (e.g. ``interface.hvcontroller.
        enter_dfu``) to reboot the running application into the bootloader,
        waits for the DFU device to enumerate, then runs
        :meth:`program_console` with its pre-erase validation and
        anti-downgrade checks.

        Returns:
            The console bootloader's version string.
        """
        if enter_dfu_fn is not None:
            logger.info("Requesting console DFU mode...")
            enter_dfu_fn()
            if dfu_wait_s > 0:
                time.sleep(dfu_wait_s)

        bl_version = self.get_console_bootloader_version(
            vid=vid, pid=pid, libusb_dll=libusb_dll,
            timeout_s=dfu_enum_timeout_s,
        )
        logger.info("Console bootloader version: %s", bl_version)

        self.program_console(
            signed_image, keys_dir=keys_dir, force=force,
            vid=vid, pid=pid, libusb_dll=libusb_dll,
            progress_callback=progress_callback,
        )
        return bl_version

    def migrate_console_rom_dfu(self, bootloader_bin: str, signed_app: str,
                                keys_dir: str | None = None,
                                vid: int = 0x0483, pid: int = 0xDF11,
                                libusb_dll: str | None = None,
                                verify_rom: bool = True,
                                progress_callback: Callable | None = None) -> None:
        """Migrate a console that is in **STM32 ROM DFU** to the secure
        bootloader, in a single DFU session.

        Intended for field units with NO bootloader (app < 1.2.0): the app
        jumps to the ROM system loader, which - unlike the legacy or secure
        bootloaders - can write the entire flash. This writes the new secure
        bootloader at 0x08000000 and the signed application at the SBSFU slot
        0x08010000, so that after a power cycle the secure bootloader verifies
        and launches the app.

        Args:
            bootloader_bin: Raw secure bootloader binary
                (open-lifu-console-bl build/<cfg>/lifu-console-bl.bin).
            signed_app: SBSFU signed application image (from LIFUCrypto).
            keys_dir: Optional keys dir to fully verify *signed_app* first.
            verify_rom: Ignored (CubeProgrammer verifies its own download);
                kept for backwards compatibility.

        Raises:
            ValueError: Signed app invalid, or bootloader overlaps the slot.
            RuntimeError: Not in ROM DFU, CubeProgrammer missing, or a write
                failure.
        """
        from openlifu_sdk.io import LIFUCrypto

        del verify_rom   # CubeProgrammer's -v handles verification
        bl_image = Path(bootloader_bin).read_bytes()
        app_image = Path(signed_app).read_bytes()

        # The app must be a valid signed SBSFU image, or the freshly written
        # bootloader would reject it and strand the unit with no fallback.
        report = LIFUCrypto.validate_signed_image(app_image, keys_dir=keys_dir)
        if not (report.ok or (keys_dir is None and report.structural_ok)):
            raise ValueError(
                f"Refusing to migrate: invalid signed app:\n{report.describe()}")

        # Combine the two regions into one full-flash image so it can be
        # written with STM32CubeProgrammer, whose USB-DFU implementation is
        # verified byte-correct on the STM32 ROM loader (the pure-Python DfuSe
        # writer is NOT reliable there). The bootloader lives at 0x08000000
        # and the signed app at the slot base 0x08010000.
        slot_off = CONSOLE_SLOT_BASE - CONSOLE_FLASH_BASE
        if len(bl_image) > slot_off:
            raise ValueError(
                f"bootloader ({len(bl_image)} B) overlaps the app slot at "
                f"0x{slot_off:X}")
        combined = bytearray(b"\xFF" * slot_off)
        combined[:len(bl_image)] = bl_image
        combined += app_image

        kind, _ = self.detect_console_dfu_kind(vid=vid, pid=pid,
                                               libusb_dll=libusb_dll)
        if kind != DFU_KIND_ROM:
            raise RuntimeError(
                f"Expected STM32 ROM DFU ('STM32 BOOTLOADER'), found {kind!r}. "
                "This migration path is only for no-bootloader units in ROM DFU.")

        cli = find_stm32_programmer_cli()
        if cli is None:
            raise RuntimeError(
                "STM32CubeProgrammer (STM32_Programmer_CLI) not found - it is "
                "required for the bootloader-replacement write over STM32 ROM "
                "DFU. Install it, add it to PATH, or set $STM32_PROGRAMMER_CLI.")

        import tempfile
        with tempfile.TemporaryDirectory() as td:
            img_path = Path(td) / "console_full.bin"
            img_path.write_bytes(bytes(combined))
            logger.info("ROM DFU: writing bootloader (%d B) + signed app "
                        "(%d B) as one image @ 0x%08X",
                        len(bl_image), len(app_image), CONSOLE_FLASH_BASE)
            self._cubeprog_write_full_image(cli, str(img_path), progress_callback)

        logger.info("ROM DFU migration complete. Power-cycle the console: the "
                    "secure bootloader will verify and launch the app.")

    def migrate_console(self, bootloader_bin: str, signed_app: str,
                        enter_stm32_rom_dfu_fn: Callable | None = None,
                        keys_dir: str | None = None,
                        vid: int = 0x0483, pid: int = 0xDF11,
                        libusb_dll: str | None = None,
                        dfu_wait_s: float = 3.0,
                        dfu_enum_timeout_s: float = 30.0,
                        progress_callback: Callable | None = None) -> None:
        """End-to-end console bootloader migration for a beta unit.

        Because every application build honours the hidden force-STM32-ROM-DFU
        switch, ALL cohorts converge on the same path - no interim app:

          no-bootloader (<1.2.0), legacy BL (1.2.0-1.2.5), secure BL (>=1.2.6)
            --enter_stm32_rom_dfu_fn()-->  STM32 ROM DFU
            --migrate_console_rom_dfu()-->  new secure BL + signed app

        Args:
            bootloader_bin: Raw secure bootloader binary.
            signed_app:     SBSFU signed application image (LIFUCrypto).
            enter_stm32_rom_dfu_fn: Callable that forces the running app into
                STM32 ROM DFU, e.g. ``interface.hvcontroller.enter_stm32_rom_dfu``.
                Omit if the unit is already in ROM DFU.
            keys_dir:       Optional keys dir to fully verify the signed app.

        Raises:
            ValueError: Signed app invalid.
            RuntimeError: ROM DFU not reached, or write/verify failure.

        NOTE: only for unlocked beta units. Once RDP/FDA lockdown is applied,
        the force switch is inert and the bootloader region is not erasable.
        """
        if enter_stm32_rom_dfu_fn is not None:
            logger.info("Forcing the console into STM32 ROM DFU...")
            enter_stm32_rom_dfu_fn()
            if dfu_wait_s > 0:
                time.sleep(dfu_wait_s)

        # Wait for the ROM loader to enumerate, then confirm it really is ROM
        # DFU before writing the bootloader region.
        self._wait_for_usb_dfu(vid=vid, pid=pid, libusb_dll=libusb_dll,
                               timeout_s=dfu_enum_timeout_s,
                               device_profile=CONSOLE_PROFILE)
        kind, ver = self.detect_console_dfu_kind(vid=vid, pid=pid,
                                                 libusb_dll=libusb_dll)
        logger.info("DFU environment: %s %s", kind, ver)
        if kind != DFU_KIND_ROM:
            raise RuntimeError(
                f"Console did not enter STM32 ROM DFU (found {kind!r}). "
                "The app may lack the force switch, or the unit is locked.")

        self.migrate_console_rom_dfu(
            bootloader_bin, signed_app, keys_dir=keys_dir,
            vid=vid, pid=pid, libusb_dll=libusb_dll,
            progress_callback=progress_callback,
        )

    def migrate_console_legacy(self, signed_app: str,
                               updater_bin: str | None = None,
                               enter_dfu_fn: Callable | None = None,
                               keys_dir: str | None = None,
                               vid: int = 0x0483, pid: int = 0xDF11,
                               libusb_dll: str | None = None,
                               dfu_wait_s: float = 3.0,
                               dfu_enum_timeout_s: float = 30.0,
                               updater_wait_s: float = 6.0,
                               progress_callback: Callable | None = None) -> None:
        """Migrate a LEGACY-bootloader console (app 1.2.0–1.2.5) to the secure
        bootloader, over USB only.

        Legacy units cannot reach the STM32 ROM DFU (their bootloader
        intercepts the force request, and its ~5 s IWDG would kill a ROM-DFU
        write). Instead the legacy bootloader's own DFU flashes a RAM-resident
        self-updater into the app slot; the legacy BL boots it and it rewrites
        the bootloader region from RAM. Sequence:

          1. enter_dfu_fn()  -> normal DFU (the legacy BL's own DFU).
          2. Write the legacy trust-tag metadata (built here) to 0x08007800
             and the updater to 0x08008000, over the legacy DFU; verify.
          3. Trigger a reset -> legacy BL boots the updater -> it replaces the
             bootloader with the secure BL -> resets into secure DFU.
          4. Flash the signed app over the secure DFU (program_console).

        Args:
            signed_app: SBSFU signed application image.
            updater_bin: The console-legacy-updater binary (embeds the new
                secure bootloader; links at 0x08008000). Defaults to the
                updater bundled with the SDK (``bundled_updater_path()``) - the
                one-time, keyless self-updater; pass a path only to override it.
            enter_dfu_fn: Callable that reboots the running app into DFU, e.g.
                ``interface.hvcontroller.enter_dfu``.
            keys_dir: Optional keys dir to validate the signed app's ECDSA
                signature before flashing. The migration needs no signing key:
                the updater is authenticated by an HMAC trust tag (computed
                here), and the secure bootloader re-verifies the app at boot.

        Raises:
            ValueError: Invalid signed app or updater.
            RuntimeError: Wrong DFU environment, or a write/verify failure.

        NOTE: beta/unlocked units only. The bootloader self-replacement is the
        single irreversible step — keep the unit powered throughout.
        """
        from openlifu_sdk.io import LIFUCrypto

        updater_path = Path(updater_bin) if updater_bin else bundled_updater_path()
        if not updater_path.is_file():
            raise ValueError(f"Updater not found: {updater_path}")
        updater = updater_path.read_bytes()
        app_image = Path(signed_app).read_bytes()
        report = LIFUCrypto.validate_signed_image(app_image, keys_dir=keys_dir)
        if not (report.ok or (keys_dir is None and report.structural_ok)):
            raise ValueError(
                f"Refusing to migrate: invalid signed app:\n{report.describe()}")
        metadata = build_legacy_metadata(updater)
        logger.info("Legacy migration: updater %d B, metadata %d B, app v%d",
                    len(updater), len(metadata), report.header.fw_version)

        # --- Step 1: enter the legacy bootloader's DFU ---
        if enter_dfu_fn is not None:
            logger.info("Requesting DFU (legacy bootloader)...")
            enter_dfu_fn()
            if dfu_wait_s > 0:
                time.sleep(dfu_wait_s)
        self._wait_for_usb_dfu(vid=vid, pid=pid, libusb_dll=libusb_dll,
                               timeout_s=dfu_enum_timeout_s,
                               device_profile=CONSOLE_PROFILE)
        kind, ver = self.detect_console_dfu_kind(vid=vid, pid=pid,
                                                 libusb_dll=libusb_dll)
        if kind != DFU_KIND_LEGACY:
            raise RuntimeError(
                f"Expected the legacy bootloader DFU, found {kind!r} {ver!r}. "
                "Use migrate_console_full_image for no-bootloader/ROM units.")
        logger.info("In legacy bootloader DFU %s", ver)

        # --- Step 2: write updater + metadata over the legacy DFU, verify ---
        with STM32USBDFU(vid=vid, pid=pid, libusb_dll=libusb_dll,
                         device_profile=CONSOLE_PROFILE) as dfu:
            dfu.write_memory(LEGACY_META_ADDRESS, metadata, page_erase=True,
                             progress_callback=progress_callback)
            dfu.write_memory(LEGACY_APP_ADDRESS, updater, page_erase=True,
                             progress_callback=progress_callback)
            # Read-back verify (the legacy BL DFU supports UPLOAD reliably).
            rb_app = dfu.read_memory(LEGACY_APP_ADDRESS, len(updater))
            rb_meta = dfu.read_memory(LEGACY_META_ADDRESS, len(metadata))
            if rb_app != updater or rb_meta != metadata:
                raise RuntimeError(
                    "Legacy DFU write verify FAILED (updater/metadata mismatch) "
                    "- aborting before reset; the old app is still intact.")
            logger.info("Updater + metadata written and verified.")

            # --- Step 3: reset -> legacy BL boots the updater ---
            logger.info("Resetting; the updater will replace the bootloader...")
            dfu.trigger_reset()

        # --- Step 4: updater runs (replaces BL, resets into secure DFU) ---
        # The updater does real flash work and there are two resets
        # (legacy BL -> updater -> secure BL) plus USB re-enumeration, so wait
        # for the secure-BL DFU to settle rather than a single fixed delay.
        logger.info("Waiting %.0fs for the updater to replace the bootloader...",
                    updater_wait_s)
        time.sleep(updater_wait_s)
        try:
            ver = self._wait_for_dfu_kind(DFU_KIND_SECURE, vid=vid, pid=pid,
                                          libusb_dll=libusb_dll,
                                          timeout_s=dfu_enum_timeout_s)
        except RuntimeError as e:
            raise RuntimeError(
                f"After the updater ran, the secure bootloader DFU did not "
                f"appear ({e}). The bootloader replacement may have failed; "
                "recover via SWD.") from e
        logger.info("Secure bootloader is up (%s); flashing the signed app...", ver)

        # --- Step 5: flash the signed app over the secure DFU ---
        self.program_console(signed_app, keys_dir=keys_dir, vid=vid, pid=pid,
                             libusb_dll=libusb_dll,
                             progress_callback=progress_callback)
        logger.info("Legacy migration complete. Power-cycle to boot the app.")

    def migrate_console_full_image(self, combined_image: str,
                                   enter_stm32_rom_dfu_fn: Callable | None = None,
                                   keys_dir: str | None = None,
                                   vid: int = 0x0483, pid: int = 0xDF11,
                                   libusb_dll: str | None = None,
                                   dfu_wait_s: float = 3.0,
                                   dfu_enum_timeout_s: float = 30.0,
                                   progress_callback: Callable | None = None) -> None:
        """Migrate a console by MASS-ERASING the chip and writing the whole
        combined production image (bootloader + signed app) at the flash base
        in one contiguous write, via STM32 ROM DFU.

        This is the recommended path for legacy-bootloader units: the full
        erase wipes the legacy metadata page, user-config page and any stale
        anti-rollback state, leaving a clean slate, and the single verbatim
        write of the production .bin is simpler and less error-prone than
        splitting it into separate bootloader/app regions.

        Args:
            combined_image: The full-flash production image beginning at
                0x08000000 (bootloader region + 'SFU1' signed app at offset
                0x10000), e.g. ``openlifu-console-fw-prod_vX.bin``.
            keys_dir: Optional keys dir to fully verify the embedded signed app.

        Raises:
            ValueError: Image is not a valid combined bootloader+app image.
            RuntimeError: ROM DFU not reached, or a write failure.

        NOTE: beta/unlocked units only. After RDP/FDA lockdown the force
        switch is inert and the flash cannot be mass-erased over DFU.
        """
        from openlifu_sdk.io import LIFUCrypto

        image = Path(combined_image).read_bytes()
        # Structural sanity: must be a combined image, and its embedded signed
        # app must be valid (the new bootloader authenticates it at boot).
        _, app_bytes = split_console_flash_image(image)
        report = LIFUCrypto.validate_signed_image(app_bytes, keys_dir=keys_dir)
        if not (report.ok or (keys_dir is None and report.structural_ok)):
            raise ValueError(
                f"Refusing to migrate: embedded app invalid:\n{report.describe()}")
        logger.info("Combined image %d B; embedded app v%d (%s)",
                    len(image), report.header.fw_version,
                    report.header.fw_version_str)

        if enter_stm32_rom_dfu_fn is not None:
            logger.info("Forcing the console into STM32 ROM DFU...")
            enter_stm32_rom_dfu_fn()
            if dfu_wait_s > 0:
                time.sleep(dfu_wait_s)

        self._wait_for_usb_dfu(vid=vid, pid=pid, libusb_dll=libusb_dll,
                               timeout_s=dfu_enum_timeout_s,
                               device_profile=CONSOLE_PROFILE)
        kind, _ = self.detect_console_dfu_kind(vid=vid, pid=pid,
                                               libusb_dll=libusb_dll)
        if kind != DFU_KIND_ROM:
            raise RuntimeError(
                f"Console did not enter STM32 ROM DFU (found {kind!r}). "
                "The app may lack the force switch, or the unit is locked.")

        # The bootloader-region write over the STM32 ROM loader is done with
        # STM32CubeProgrammer: its USB-DFU implementation is verified
        # byte-correct on this ROM loader, whereas the pure-Python DfuSe
        # write is not reliable here. A mass-erase + verified download of the
        # whole image gives the clean-slate result in one step.
        cli = find_stm32_programmer_cli()
        if cli is None:
            raise RuntimeError(
                "STM32CubeProgrammer (STM32_Programmer_CLI) not found - it is "
                "required for the bootloader-replacement write over STM32 ROM "
                "DFU. Install it, add it to PATH, or set $STM32_PROGRAMMER_CLI.")
        self._cubeprog_write_full_image(cli, combined_image, progress_callback)

        logger.info("Full-image migration complete. Power-cycle the console: "
                    "the secure bootloader will verify and launch the app.")

    @staticmethod
    def _cubeprog_write_full_image(cli: str, image_path: str,
                                   progress_callback: Callable | None) -> None:
        """Mass-erase and write+verify a full-flash image at 0x08000000 over
        USB DFU using STM32CubeProgrammer. Raises RuntimeError on failure."""
        import subprocess

        cmd = [cli, "-c", "port=USB1", "-e", "all",
               "-d", str(Path(image_path)), "0x08000000", "-v"]
        logger.info("Running STM32CubeProgrammer: %s", " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True)
        out = (proc.stdout or "") + (proc.stderr or "")
        for line in out.splitlines():
            if any(k in line for k in ("Download", "verified", "Erasing",
                                       "Error", "erased", "complete")):
                logger.info("  cubeprog: %s", line.strip())
        if proc.returncode != 0 or "Download verified successfully" not in out:
            raise RuntimeError(
                "STM32CubeProgrammer USB-DFU write/verify failed "
                f"(rc={proc.returncode}). Output tail:\n"
                + "\n".join(out.splitlines()[-15:]))
        if progress_callback:
            n = Path(image_path).stat().st_size
            progress_callback(n, n, "CubeProgrammer USB-DFU write+verify")

    def dwell_rom_dfu_check(self, enter_stm32_rom_dfu_fn: Callable | None = None,
                            seconds: float = 30.0,
                            vid: int = 0x0483, pid: int = 0xDF11,
                            libusb_dll: str | None = None,
                            dfu_wait_s: float = 3.0) -> bool:
        """Force STM32 ROM DFU (if *enter_stm32_rom_dfu_fn* is given) and then
        watch that the device stays enumerated in ROM DFU for *seconds*.

        This is the pre-migration safety check for legacy-bootloader units:
        the legacy bootloader arms a ~5 s IWDG before launching the app, and
        if that watchdog keeps running after the app jumps to ROM DFU it would
        reset the unit mid-write. A stable dwell (no disappearance, kind stays
        ROM) means the migration window is safe.

        Returns:
            True if the device stayed in ROM DFU for the whole window.
        """
        if enter_stm32_rom_dfu_fn is not None:
            logger.info("Forcing STM32 ROM DFU for dwell check...")
            enter_stm32_rom_dfu_fn()
            if dfu_wait_s > 0:
                time.sleep(dfu_wait_s)

        probe = STM32USBDFU(vid=vid, pid=pid, libusb_dll=libusb_dll)
        backend = probe._get_backend()

        deadline = time.monotonic() + seconds
        checks = 0
        rom = 0
        absent = 0
        other_products: set[str] = set()
        while time.monotonic() < deadline:
            dev = _usb_core.find(idVendor=vid, idProduct=pid, backend=backend)
            if dev is not None:
                try:
                    product = _usb_util.get_string(dev, dev.iProduct) or ""
                finally:
                    _usb_util.dispose_resources(dev)
                if " ".join(product.split()).startswith("STM32 BOOTLOADER"):
                    rom += 1
                else:
                    other_products.add(product)
            else:
                absent += 1
            checks += 1
            time.sleep(1.0)

        # Interpret: settle over the first few probes, then classify.
        ok = (rom > 0 and absent <= 3 and not other_products)
        if other_products:
            logger.warning(
                "Dwell check: device is NOT in STM32 ROM DFU - it enumerated as "
                "%s. The app did not reach the ROM loader (it likely lacks the "
                "force-STM32 switch, or its bootloader intercepts the request). "
                "This is NOT an IWDG reset; the device is stable in the wrong "
                "DFU.", ", ".join(repr(p) for p in sorted(other_products)))
        elif absent > 3:
            logger.warning(
                "Dwell check: device kept dropping off the bus (%d/%d probes "
                "absent) - possible IWDG reset loop; do NOT USB-migrate this "
                "unit.", absent, checks)
        else:
            logger.info("Dwell check: STABLE in STM32 ROM DFU "
                        "(%d/%d probes) - safe to migrate.", rom, checks)
        return ok

    def program_i2c(self, package_file: str,
                    i2c_addr: int = I2C_DFU_SLAVE_ADDR,
                    progress_callback: Callable | None = None) -> None:
        """Program a signed package to a slave module via I2C passthrough.

        The slave must already be in DFU bootloader mode at *i2c_addr*.

        Sequence (mirrors dfu-i2c-test.py program-package):
          1. Mass-erase the application flash region.
          2. Erase the metadata page explicitly (it is outside the app region
             and is NOT touched by mass-erase).
          3. Write the firmware payload.
          4. Write the metadata blob.
          5. Send CMD_MANIFEST.
        """
        with open(package_file, "rb") as f:
            pkg_blob = f.read()
        pkg = parse_signed_package(pkg_blob)

        logger.info(
            "I2C DFU: fw %d B @ 0x%08X, meta %d B @ 0x%08X",
            len(pkg["fw"]), pkg["fw_address"],
            len(pkg["meta"]), pkg["meta_address"],
        )
        dfu = STM32I2CDFUviaMaster(uart=self._uart, i2c_addr=i2c_addr)
        logger.info("I2C DFU: mass erasing application region...")
        dfu.mass_erase()
        logger.info("I2C DFU: erasing metadata page @ 0x%08X...", pkg["meta_address"])
        dfu.erase_page(pkg["meta_address"])
        dfu.write_memory(
            pkg["fw_address"], pkg["fw"],
            progress_callback=progress_callback
        )
        logger.info("I2C DFU: writing metadata...")
        dfu.write_memory(
            pkg["meta_address"], pkg["meta"]
        )
        logger.info("I2C DFU: sending manifest...")
        dfu.manifest()
        logger.info("I2C DFU: programming complete.")

    def _wait_for_usb_dfu(self, vid: int, pid: int, libusb_dll: str | None,
                           timeout_s: float = 30.0, poll_interval_s: float = 1.0,
                           device_profile: "DeviceProfile" | None = None) -> str:
        """Poll for the USB DFU device until it enumerates or *timeout_s* elapses.

        Returns the bootloader version string once the device is found.
        Raises RuntimeError if the device does not appear within the timeout.
        """
        # Pre-flight: verify the libusb backend can be loaded before entering
        # the poll loop.  If the DLL is missing or the path is wrong this fails
        # immediately with a clear message instead of silently timing out.
        _probe = STM32USBDFU(vid=vid, pid=pid, libusb_dll=libusb_dll,
                     device_profile=device_profile)
        backend = _probe._get_backend()
        if backend is None:
            raise RuntimeError(
                "libusb backend not available — install libusb or pass --libusb-dll "
                "pointing to a valid libusb-1.0.dll."
            )

        deadline = time.monotonic() + timeout_s
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            # Phase 1: check if the DFU device has appeared (no I/O yet).
            try:
                dev = _usb_core.find(idVendor=vid, idProduct=pid, backend=backend)
            except Exception as e:
                logger.warning("USB DFU find error (attempt %d): %s", attempt, e)
                time.sleep(poll_interval_s)
                continue

            if dev is None:
                remaining = deadline - time.monotonic()
                logger.debug(
                    "USB DFU not found yet (attempt %d, %.0f s remaining)...",
                    attempt, max(remaining, 0)
                )
                time.sleep(poll_interval_s)
                continue

            # Phase 2: device is present — open it and read the version string.
            elapsed = timeout_s - (deadline - time.monotonic())
            logger.info(
                "USB DFU device found after %.1f s (attempt %d)", elapsed, attempt
            )
            try:
                with STM32USBDFU(vid=vid, pid=pid, libusb_dll=libusb_dll,
                                 device_profile=device_profile) as dfu:
                    version = dfu.get_version()
                return version
            except Exception as e:
                # Device enumerated but version read failed (e.g. DFU state    
                # machine not ready yet or bootloader doesn't support virtual
                # version address).  Log visibly and return a placeholder so
                # the update can still proceed.
                logger.warning(
                    "USB DFU device found but version read failed: %s — "
                    "proceeding with version='unknown'", e
                )
                return "unknown"

        raise RuntimeError(
            f"USB DFU device (VID=0x{vid:04X}, PID=0x{pid:04X}) did not "
            f"enumerate within {timeout_s:.0f} s"
        )

    def update_module(self,
                      module: int,
                      package_file: str,
                      enter_dfu_fn: Callable,
                      vid: int = 0x0483,
                      pid: int = 0xDF11,
                      libusb_dll: str | None = None,
                      i2c_addr: int = I2C_DFU_SLAVE_ADDR,
                      dfu_wait_s: float = 3.0,
                      dfu_enum_timeout_s: float = 30.0,
                      device_type: str = "transmitter",
                      progress_callback: Callable | None = None) -> None:
        """High-level firmware update for a single module.

        Steps:
         1. Call *enter_dfu_fn(module=module)* to reboot into the bootloader.
         2. Wait *dfu_wait_s* seconds (initial settling delay).
         3. For module 0: poll for the USB DFU device until it enumerates
            (up to *dfu_enum_timeout_s*) then program.
            For modules 1+: poll the I2C DFU slave via passthrough then program.
         4. Program the signed package.

        Module 0 (USB master) uses USB DFU.
        Modules 1+ use I2C DFU through the master's ``OW_I2C_PASSTHRU`` path,
        writing to *i2c_addr* (default 0x72).

        Args:
            module:              Physical module index (0 = USB master).
            package_file:        Path to the signed firmware package.
            enter_dfu_fn:        Callable that triggers DFU mode, e.g.
                                 ``txdevice.enter_dfu``.
            vid:                 USB VID for module 0 USB DFU.
            pid:                 USB PID for module 0 USB DFU.
            libusb_dll:          Optional path to libusb-1.0.dll (Windows).
            i2c_addr:            I2C DFU slave address for modules 1+.
            dfu_wait_s:          Initial settling delay after DFU-enter (default 3 s).
            dfu_enum_timeout_s:  Total time to wait for the bootloader to appear
                                 (default 30 s).  Includes *dfu_wait_s*.
            progress_callback:   Optional ``(written, total, label)`` callable.

        Raises:
            RuntimeError: If DFU entry cannot be verified or programming fails.
        """
        logger.info("Requesting DFU mode on module %d...", module)
        if device_type == "transmitter":
            # Transmitter modules (including module 0 master) use the module-aware DFU entry
            enter_dfu_fn(module=module)
        elif device_type == "console":
            # Console/host DFU is only valid for the USB master (module 0)
            if module != 0:
                raise ValueError(
                    f"Console DFU is only supported for module 0; got module {module}"
                )
            enter_dfu_fn()
        else:
            raise ValueError(f"Unsupported device_type {device_type!r} for DFU entry")

        if dfu_wait_s > 0:
            logger.info("Initial DFU settling delay: %.1f s...", dfu_wait_s)
            time.sleep(dfu_wait_s)

        if module == 0:
            logger.info(
                "Waiting for USB DFU device (timeout %ds)...", dfu_enum_timeout_s
            )
            try:
                profile = TRANSMITTER_PROFILE if device_type == "transmitter" else CONSOLE_PROFILE
                bl_version = self._wait_for_usb_dfu(
                    vid=vid, pid=pid, libusb_dll=libusb_dll,
                    timeout_s=dfu_enum_timeout_s, device_profile=profile,
                )
            except RuntimeError as e:
                raise RuntimeError(
                    f"Module 0 did not enter USB DFU mode: {e}"
                ) from e
            logger.info("USB DFU bootloader version: %s", bl_version)
            self.program_usb(
                package_file, vid=vid, pid=pid,
                libusb_dll=libusb_dll,
                device_type=device_type,
                progress_callback=progress_callback,
            )
        else:
            logger.info(
                "Verifying I2C DFU entry (module %d, addr=0x%02X via master)...",
                module, i2c_addr,
            )
            try:
                bl_version = self.get_bootloader_version_i2c(i2c_addr=i2c_addr)
            except Exception as e:
                raise RuntimeError(
                    f"Module {module} did not enter I2C DFU mode at "
                    f"0x{i2c_addr:02X}: {e}"
                ) from e
            if not bl_version:
                raise RuntimeError(
                    f"Module {module} I2C DFU bootloader returned an empty version string"
                )
            logger.info("I2C DFU bootloader version: %s", bl_version)
            self.program_i2c(
                package_file, i2c_addr=i2c_addr,
                progress_callback=progress_callback,
            )

        logger.info("Firmware update complete for module %d.", module)
