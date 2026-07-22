"""Pure-Python driver for the STM32 ROM (system-memory) bootloader USB DFU.

Flashes an MCU over the ST DfuSe protocol (AN3156; VID 0x0483, PID 0xDF11)
with no external tools — no STM32CubeProgrammer, no dfu-util. Only PyUSB and
a libusb backend are required (the SDK bundles a Windows libusb DLL).

This targets the **ROM loader** (the immutable bootloader in system memory,
reached e.g. via ``TxDevice.enter_stm32_rom_dfu()``), which can write ALL of
flash including the bootloader region — the tool for full production-image
migrations. It also works against the custom SBSFU bootloaders' DFU, but for
those prefer the higher-level ``LIFUDFUManager`` paths, which add signed-image
validation and anti-downgrade checks.

Reliability notes (why a naive DfuSe write corrupts on the ROM loader, and
what this driver does instead):

  - Every command is driven through GETSTATUS and the advertised
    ``bwPollTimeout`` is honored before the next request — the ROM loader
    executes erase/program during the DNBUSY window and must not be
    interrupted.
  - Each data block is written with an explicit SET_ADDRESS + block-number 2,
    so the write address never depends on the device's negotiated
    wTransferSize — no silent address drift.
  - Erase is per-page/sector (the DfuSe mass-erase has been observed to
    silently no-op on some ROM loaders).
  - An optional verify pass reads everything back over DFU_UPLOAD and
    compares byte-for-byte.

Progress: every phase reports through ``progress_callback(done, total,
label)`` (labels ``"ROM DFU erase"``, ``"ROM DFU write"``, ``"ROM DFU
verify"``) — the same shape the SDK's UI progress bars already consume.

Typical use::

    from openlifu_sdk.io.STM32DFU import STM32DFU

    with STM32DFU() as dfu:
        dfu.flash("production.bin", address=0x08000000,
                  progress_callback=lambda d, t, l: print(l, d, t))

CLI::

    python -m openlifu_sdk.io.STM32DFU info
    python -m openlifu_sdk.io.STM32DFU flash image.bin --address 0x08000000
    python -m openlifu_sdk.io.STM32DFU read  out.bin --address 0x08000000 --length 0x1000
    python -m openlifu_sdk.io.STM32DFU leave --address 0x08000000
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

try:
    import usb.core as _usb_core
    import usb.util as _usb_util
    import usb.backend.libusb1 as _usb_libusb1
    _USB_AVAILABLE = True
except ImportError:            # pragma: no cover - environment dependent
    _usb_core = None
    _usb_util = None
    _usb_libusb1 = None
    _USB_AVAILABLE = False

try:
    import libusb_package as _libusb_package
except ImportError:            # pragma: no cover - optional
    _libusb_package = None


class STM32DFUError(RuntimeError):
    """DfuSe protocol or device error."""


# DFU class requests (USB DFU 1.1)
_REQ_DNLOAD    = 1
_REQ_UPLOAD    = 2
_REQ_GETSTATUS = 3
_REQ_CLRSTATUS = 4
_REQ_ABORT     = 6

# DFU device states
_STATE_DFU_IDLE         = 2
_STATE_DNBUSY           = 4
_STATE_DNLOAD_IDLE      = 5
_STATE_MANIFEST         = 7
_STATE_UPLOAD_IDLE      = 9
_STATE_ERROR            = 10

# DfuSe command bytes (sent as DNLOAD block 0)
_CMD_SET_ADDRESS = 0x21
_CMD_ERASE       = 0x41

# bStatus error names (AN3156 / DFU 1.1) for readable failures
_STATUS_NAMES = {
    0x00: "OK", 0x01: "errTARGET", 0x02: "errFILE", 0x03: "errWRITE",
    0x04: "errERASE", 0x05: "errCHECK_ERASED", 0x06: "errPROG",
    0x07: "errVERIFY", 0x08: "errADDRESS", 0x09: "errNOTDONE",
    0x0A: "errFIRMWARE", 0x0B: "errVENDOR (read-protected? RDP)",
    0x0C: "errUSBR", 0x0D: "errPOR", 0x0E: "errUNKNOWN", 0x0F: "errSTALLEDPKT",
}


class STM32DFU:
    """Minimal, careful DfuSe client for the STM32 ROM bootloader.

    Args:
        vid/pid: USB IDs (default: the ST DFU 0483:DF11).
        transfer_size: Max bytes per data block (default 1024 — always safe;
            each block is individually addressed so this need not match the
            device's wTransferSize).
        page_size: Flash erase granularity (default 2048 — STM32F0/L4 pages.
            Pass e.g. 128*1024 for STM32H7 sectors).
        libusb_dll: Optional explicit libusb-1.0 DLL path (Windows).
    """

    def __init__(self, vid: int = 0x0483, pid: int = 0xDF11,
                 transfer_size: int = 1024, page_size: int = 2048,
                 timeout_ms: int = 5000, libusb_dll: str | None = None):
        if not _USB_AVAILABLE:
            raise STM32DFUError(
                "PyUSB not available. Install with: pip install pyusb")
        self.vid = vid
        self.pid = pid
        self.transfer_size = transfer_size
        self.page_size = page_size
        self.timeout_ms = timeout_ms
        self.libusb_dll = libusb_dll
        self.dev = None
        self.intf = None
        self._backend = None

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    def _get_backend(self):
        if self._backend is not None:
            return self._backend
        if self.libusb_dll:
            self._backend = _usb_libusb1.get_backend(
                find_library=lambda _: self.libusb_dll)
        elif _libusb_package is not None:
            self._backend = _usb_libusb1.get_backend(
                find_library=_libusb_package.find_library)
        else:
            from openlifu_sdk.io.LIFUDFU import _find_bundled_libusb_dll
            bundled = _find_bundled_libusb_dll()
            if bundled:
                self._backend = _usb_libusb1.get_backend(
                    find_library=lambda _: bundled)
            else:
                self._backend = _usb_libusb1.get_backend()
        if self._backend is None:
            raise STM32DFUError(
                "libusb backend not available — install libusb or pass "
                "libusb_dll pointing to a valid libusb-1.0 library")
        return self._backend

    def open(self) -> "STM32DFU":
        self.dev = _usb_core.find(idVendor=self.vid, idProduct=self.pid,
                                  backend=self._get_backend())
        if self.dev is None:
            raise STM32DFUError(
                f"STM32 DFU device not found (VID=0x{self.vid:04X}, "
                f"PID=0x{self.pid:04X}). Put the MCU in ROM DFU mode first.")
        self.dev.set_configuration()
        cfg = self.dev.get_active_configuration()
        for intf in cfg:
            if intf.bInterfaceClass == 0xFE and intf.bInterfaceSubClass == 0x01:
                self.intf = intf
                break
        if self.intf is None:
            raise STM32DFUError("No DFU interface on the USB device")
        try:
            if self.dev.is_kernel_driver_active(self.intf.bInterfaceNumber):
                self.dev.detach_kernel_driver(self.intf.bInterfaceNumber)
        except (NotImplementedError, Exception):
            pass
        _usb_util.claim_interface(self.dev, self.intf.bInterfaceNumber)
        self._recover_idle()
        return self

    def close(self) -> None:
        if self.dev is not None:
            try:
                _usb_util.release_interface(self.dev, self.intf.bInterfaceNumber)
            except Exception:
                pass
            _usb_util.dispose_resources(self.dev)
        self.dev = None
        self.intf = None

    def __enter__(self) -> "STM32DFU":
        return self.open()

    def __exit__(self, *args) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Low-level protocol
    # ------------------------------------------------------------------

    def _ctrl_out(self, req: int, value: int, data: bytes = b"") -> int:
        return self.dev.ctrl_transfer(0x21, req, value,
                                      self.intf.bInterfaceNumber, data,
                                      timeout=self.timeout_ms)

    def _ctrl_in(self, req: int, value: int, length: int) -> bytes:
        return bytes(self.dev.ctrl_transfer(0xA1, req, value,
                                            self.intf.bInterfaceNumber,
                                            length, timeout=self.timeout_ms))

    def get_status(self) -> tuple[int, int, int]:
        """Return ``(bStatus, bwPollTimeout_ms, bState)``.

        One transient-failure retry: the STM32L43x/44x V9.1 ROM loader with
        version-information ID 0xFF has a CRS clock erratum that can answer
        setup packets with a spurious STALL (AN2606 Table 165).
        """
        try:
            raw = self._ctrl_in(_REQ_GETSTATUS, 0, 6)
        except Exception:
            time.sleep(0.005)
            raw = self._ctrl_in(_REQ_GETSTATUS, 0, 6)
        return raw[0], raw[1] | (raw[2] << 8) | (raw[3] << 16), raw[4]

    def _clear_status(self) -> None:
        self._ctrl_out(_REQ_CLRSTATUS, 0, b"")

    def _abort(self) -> None:
        self._ctrl_out(_REQ_ABORT, 0, b"")

    def _recover_idle(self) -> None:
        """Bring the DFU state machine back to dfuIDLE from any state."""
        for _ in range(3):
            status, _poll, state = self.get_status()
            if state == _STATE_DFU_IDLE:
                return
            if state == _STATE_ERROR:
                self._clear_status()
            else:
                self._abort()
        _s, _p, state = self.get_status()
        if state != _STATE_DFU_IDLE:
            raise STM32DFUError(f"could not reach dfuIDLE (state {state})")

    def _wait_command_done(self, op: str) -> None:
        """Drive a pending DNLOAD (command or data) to completion.

        The ROM loader executes the operation during the DNBUSY window
        entered by GETSTATUS; the advertised bwPollTimeout MUST elapse before
        the next request or the operation can be corrupted.
        """
        deadline = time.monotonic() + max(self.timeout_ms / 1000.0, 30.0)
        while True:
            status, poll_ms, state = self.get_status()
            if state == _STATE_ERROR or status != 0:
                name = _STATUS_NAMES.get(status, f"0x{status:02X}")
                self._clear_status()
                raise STM32DFUError(f"{op} failed: device reports {name}")
            if state == _STATE_DNBUSY:
                if time.monotonic() > deadline:
                    raise STM32DFUError(f"{op} timed out in dfuDNBUSY")
                time.sleep(max(poll_ms, 1) / 1000.0)
                continue
            return  # dfuDNLOAD_IDLE (or dfuIDLE)

    def _dnload(self, block: int, payload: bytes, op: str) -> None:
        self._ctrl_out(_REQ_DNLOAD, block, payload)
        self._wait_command_done(op)

    def _set_address(self, address: int) -> None:
        cmd = bytes([_CMD_SET_ADDRESS]) + address.to_bytes(4, "little")
        self._dnload(0, cmd, f"SET_ADDRESS 0x{address:08X}")

    # ------------------------------------------------------------------
    # Flash operations
    # ------------------------------------------------------------------

    def erase_pages(self, address: int, length: int,
                    progress_callback: Callable | None = None) -> None:
        """Page/sector-erase every flash page covering ``[address,
        address+length)``. Reports progress as (pages_done, pages_total)."""
        first = address & ~(self.page_size - 1)
        last = (address + length - 1) & ~(self.page_size - 1)
        total = (last - first) // self.page_size + 1
        for i in range(total):
            page = first + i * self.page_size
            cmd = bytes([_CMD_ERASE]) + page.to_bytes(4, "little")
            self._dnload(0, cmd, f"ERASE 0x{page:08X}")
            if progress_callback:
                progress_callback(i + 1, total, "ROM DFU erase")

    def write(self, address: int, data: bytes,
              progress_callback: Callable | None = None) -> None:
        """Program ``data`` at ``address`` (no erase — see :meth:`flash`).

        Each chunk is individually addressed (SET_ADDRESS + block 2), so the
        write layout is exact regardless of the device's wTransferSize.
        """
        total = len(data)
        offset = 0
        while offset < total:
            chunk = data[offset:offset + self.transfer_size]
            advance = len(chunk)
            # AN2606 erratum (STM32L43x/44x ROM loader V9.1, ID 0xFF): a DFU
            # write > 256 B that is not a multiple of 8 B corrupts flash.
            # Pad the final chunk to an 8-byte boundary with 0xFF — blank
            # flash, and it cannot spill past the erased region (a non-8-
            # aligned image never ends exactly on a page boundary).
            if len(chunk) % 8:
                chunk = chunk + b"\xFF" * (8 - len(chunk) % 8)
            self._set_address(address + offset)
            self._dnload(2, chunk, f"WRITE 0x{address + offset:08X}")
            offset += advance
            if progress_callback:
                progress_callback(offset, total, "ROM DFU write")

    def read(self, address: int, length: int,
             progress_callback: Callable | None = None) -> bytes:
        """Read ``length`` bytes from ``address`` via DFU_UPLOAD."""
        out = bytearray()
        while len(out) < length:
            want = min(self.transfer_size, length - len(out))
            self._set_address(address + len(out))
            self._abort()          # UPLOAD must start from dfuIDLE, block 2
            chunk = self._ctrl_in(_REQ_UPLOAD, 2, want)
            if not chunk:
                raise STM32DFUError(
                    f"short UPLOAD at 0x{address + len(out):08X} "
                    "(read-protected flash?)")
            out += chunk
            self._abort()
            if progress_callback:
                progress_callback(len(out), length, "ROM DFU verify")
        return bytes(out[:length])

    def leave(self, address: int = 0x08000000) -> None:
        """Exit DFU: set the start address and send a zero-length DNLOAD so
        the ROM loader manifests and jumps/resets into the new firmware. The
        device drops off USB — that is expected and tolerated."""
        try:
            self._recover_idle()
            self._set_address(address)
            self._ctrl_out(_REQ_DNLOAD, 0, b"")
            self.get_status()      # drives the manifest; device may vanish
        except Exception:
            pass

    # ------------------------------------------------------------------
    # High-level: erase + write + verify + leave
    # ------------------------------------------------------------------

    def flash(self, image: str | Path | bytes, address: int = 0x08000000,
              *, erase: bool = True, verify: bool = True, leave: bool = True,
              progress_callback: Callable | None = None) -> None:
        """Full firmware update: erase the covered pages, program the image,
        read it back to verify, and leave DFU so the MCU boots it.

        Args:
            image: Path to a raw ``.bin``, or the image bytes.
            address: Flash load address (default 0x08000000).
            erase/verify/leave: Phase switches (all default on).
            progress_callback: ``(done, total, label)`` per phase, labels
                "ROM DFU erase" / "ROM DFU write" / "ROM DFU verify".

        Raises:
            STM32DFUError: Any protocol/device failure, or verify mismatch.
        """
        data = image if isinstance(image, bytes) else Path(image).read_bytes()
        if not data:
            raise STM32DFUError("image is empty")
        logger.info("ROM DFU flash: %d bytes @ 0x%08X (erase=%s verify=%s)",
                    len(data), address, erase, verify)

        self._recover_idle()
        if erase:
            self.erase_pages(address, len(data), progress_callback)
        self.write(address, data, progress_callback)
        if verify:
            readback = self.read(address, len(data), progress_callback)
            if readback != data:
                diff = next(i for i in range(len(data))
                            if readback[i] != data[i])
                raise STM32DFUError(
                    f"verify FAILED at 0x{address + diff:08X} "
                    f"(wrote 0x{data[diff]:02X}, read 0x{readback[diff]:02X})")
            logger.info("ROM DFU verify: %d bytes OK", len(data))
        if leave:
            self.leave(address)
        logger.info("ROM DFU flash complete")


# ---------------------------------------------------------------------------
# CLI:  python -m openlifu_sdk.io.STM32DFU <command> [options]
# ---------------------------------------------------------------------------

def _print_progress(done: int, total: int, label: str) -> None:
    pct = 100 * done // total if total else 100
    bar = "#" * (pct // 4)
    print(f"\r  {label}: [{bar:<25}] {pct:3d}%  ({done:,}/{total:,})",
          end="", flush=True)
    if done >= total:
        print()


def _cli_flash(dfu: STM32DFU, args) -> int:
    p = Path(args.image)
    if not p.is_file():
        print(f"ERROR: file not found: {p}")
        return 2
    dfu.flash(p, address=args.address, verify=not args.no_verify,
              leave=not args.no_leave, progress_callback=_print_progress)
    print(f"Flashed {p.stat().st_size:,} bytes at 0x{args.address:08X}"
          + ("" if args.no_leave else " — device left DFU to boot it"))
    return 0


def _cli_read(dfu: STM32DFU, args) -> int:
    data = dfu.read(args.address, args.length,
                    progress_callback=_print_progress)
    Path(args.out).write_bytes(data)
    print(f"Read {len(data):,} bytes from 0x{args.address:08X} -> {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m openlifu_sdk.io.STM32DFU",
        description="Pure-Python STM32 ROM bootloader USB DFU flasher "
                    "(DfuSe, 0483:DF11). Put the MCU in ROM DFU mode first.")
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=0x0483)
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=0xDF11)
    parser.add_argument("--page-size", type=lambda x: int(x, 0), default=2048,
                        help="Flash erase granularity (default 2048; use "
                             "0x20000 for STM32H7).")
    parser.add_argument("--libusb-dll", help="Explicit libusb-1.0 DLL path.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_flash = sub.add_parser("flash", help="Erase + write + verify + leave")
    p_flash.add_argument("image", help="Raw .bin image")
    p_flash.add_argument("--address", type=lambda x: int(x, 0),
                         default=0x08000000)
    p_flash.add_argument("--no-verify", action="store_true")
    p_flash.add_argument("--no-leave", action="store_true",
                         help="Stay in DFU after flashing.")

    p_read = sub.add_parser("read", help="Dump flash to a file")
    p_read.add_argument("out", help="Output file")
    p_read.add_argument("--address", type=lambda x: int(x, 0),
                        default=0x08000000)
    p_read.add_argument("--length", type=lambda x: int(x, 0), required=True)

    p_leave = sub.add_parser("leave", help="Exit DFU and boot the firmware")
    p_leave.add_argument("--address", type=lambda x: int(x, 0),
                         default=0x08000000)

    sub.add_parser("info", help="Show DFU device status")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        with STM32DFU(vid=args.vid, pid=args.pid, page_size=args.page_size,
                      libusb_dll=args.libusb_dll) as dfu:
            if args.cmd == "flash":
                return _cli_flash(dfu, args)
            if args.cmd == "read":
                return _cli_read(dfu, args)
            if args.cmd == "leave":
                dfu.leave(args.address)
                print("Leave sent — device rebooting into its firmware.")
                return 0
            status, poll, state = dfu.get_status()
            print(f"DFU device 0x{args.vid:04X}:0x{args.pid:04X} — "
                  f"status={_STATUS_NAMES.get(status, status)} state={state} "
                  f"poll={poll} ms")
            return 0
    except STM32DFUError as e:
        print(f"\nERROR: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
