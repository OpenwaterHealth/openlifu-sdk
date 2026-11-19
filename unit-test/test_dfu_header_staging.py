# SPDX-License-Identifier: Apache-2.0
"""USB DFU against a console bootloader that verifies the signed header
before erasing (open-lifu-console-bl >= 1.1.0, usbd_dfu_if.c).

That bootloader refuses every slot erase and write until the host has
DNLOADed the image's 320-byte SFU header to 0xFFFFFF10 and it has verified.
It advertises the requirement at 0xFFFFFF18 (b"OWC1" + u32 caps, bit 0).
Older bootloaders STALL that read or return flash bytes and must keep the
plain erase-then-write flow. These tests drive STM32USBDFU and the console
update path with a fake transport modelling both kinds of bootloader.
"""

from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest

from openlifu_sdk.io import LIFUCrypto, LIFUDFU
from openlifu_sdk.io.LIFUDFU import (
    CONSOLE_PROFILE,
    CONSOLE_SLOT_BASE,
    TRANSMITTER_PROFILE,
    USB_DFU_CAPS_VIRT_ADDR,
    USB_DFU_HDR_STAGE_VIRT_ADDR,
    USB_DFU_SFU_HEADER_LEN,
    DFUHeaderRefusedError,
    DFUWriteRefusedError,
    LIFUDFUManager,
    STM32USBDFU,
)

SLOT = CONSOLE_SLOT_BASE
DFU_IDLE = 2
CAPS_OK = b"OWC1" + struct.pack("<I", 1)


def signed_image(size: int = 6000, fill: int = 0xAB) -> bytes:
    """Header-shaped bytes: SFU1 magic, then filler. The fake bootloader
    only compares bytes, so no real signature is needed."""
    return b"SFU1" + bytes([fill]) * (size - 4)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(LIFUDFU.time, "sleep", lambda _s: None)


class UsbStall(Exception):
    """Stands in for usb.core.USBError (pipe error / STALL)."""


class FakeConsoleBootloader(STM32USBDFU):
    """STM32USBDFU with the USB control transfers replaced by a model of the
    console bootloader's DFU media interface.

    caps: what an UPLOAD at 0xFFFFFF18 does - CAPS_OK (>= 1.1.0), "stall"
          (a bootloader that does not serve the address), or other bytes
          (1.0.3-rc.1 returns flash bytes).
    accept_header: whether a staged header verifies.
    header_polls: GETSTATUS replies scripted after the staging DNLOAD -
          "busy" (still checking) or "fail" (no answer); the real result
          follows once they run out.
    reboot_at_write: the n-th accepted slot data write (1-based) reboots
          the bootloader: the block is lost, the handle is dead until
          open(), and the verified header is forgotten.
    lost_write_at: slot offset whose first write does not land, so the
          read-back verify finds that page wrong and repairs it.
    restage: what a header staged after the first one gets - "accept",
          "refuse", or "no answer" (its GETSTATUS poll goes unanswered).
    """

    def __init__(self, caps=CAPS_OK, accept_header=True, header_polls=(),
                 profile=CONSOLE_PROFILE, reboot_at_write=None,
                 lost_write_at=None, restage="accept"):
        self.transfer_size = 1024
        self.program_alignment = 4
        self.timeout_ms = 4000
        self.caps_virt_addr = profile.caps_virt_addr
        self.hdr_stage_virt_addr = profile.hdr_stage_virt_addr
        self._staged_header = None
        self.caps = caps
        self.accept_header = accept_header
        self.scripted_polls = list(header_polls)
        self.header_polls = []          # armed by the staging DNLOAD
        self.verify_before_erase = caps == CAPS_OK
        self.verified = False
        self.state = DFU_IDLE
        self.pointer = None
        self.flash = bytearray(b"\xFF" * CONSOLE_PROFILE.slot_size)
        self.ops = []                 # ("stage"|"erase"|"write"|"caps", detail)
        self.refused = []             # ops the bootloader refused
        self.reboot_at_write = reboot_at_write
        self.lost_write_at = lost_write_at
        self.restage = restage
        self.writes = 0
        self.dead = False

    # -- transport model --------------------------------------------------
    def open(self):
        self.dead = False
        self.state = DFU_IDLE
        return self

    def close(self):
        pass

    def get_status(self):
        if self.dead:
            raise UsbStall("device gone")
        if self.header_polls:
            reply = self.header_polls.pop(0)
            if reply == "fail":
                raise UsbStall("no answer while verifying")
            return {"status": 0, "poll_timeout_ms": 3000,
                    "state": self.STATE_DFU_DNLOAD_BUSY}
        err = self.state == self.STATE_DFU_ERROR
        return {"status": 0x03 if err else 0x00, "poll_timeout_ms": 0,
                "state": self.state}

    def _fail(self, op):
        self.refused.append(op)
        self.state = self.STATE_DFU_ERROR

    def _ctrl_out(self, req, value, data=b""):
        if self.dead:
            raise UsbStall("device gone")
        if req in (self.DFU_CLRSTATUS, self.DFU_ABORT):
            self.state = DFU_IDLE
            return 0
        assert req == self.DFU_DNLOAD
        if value == 0:                                    # DfuSe command
            cmd, addr = data[0], int.from_bytes(data[1:5], "little")
            if cmd == self.CMD_SET_ADDRESS_POINTER:
                self.pointer = addr
                self.state = self.STATE_DFU_DNLOAD_IDLE
            elif cmd == self.CMD_ERASE:
                self.ops.append(("erase", addr))
                if self.verify_before_erase and not self.verified:
                    self._fail(("erase", addr))
                else:
                    off = addr - SLOT
                    self.flash[off:off + 2048] = b"\xFF" * 2048
                    self.state = self.STATE_DFU_DNLOAD_IDLE
            return len(data)
        addr = self.pointer + (value - 2) * self.transfer_size
        if addr == USB_DFU_HDR_STAGE_VIRT_ADDR and self.verify_before_erase:
            restaged = "stage" in self.kinds()
            self.ops.append(("stage", bytes(data)))
            if restaged and self.restage == "no answer":
                self.header_polls = ["fail"]
            else:
                self.header_polls = list(self.scripted_polls)
            accept = self.restage == "accept" if restaged else self.accept_header
            self.verified = accept and len(data) == USB_DFU_SFU_HEADER_LEN
            if not self.verified:
                self._fail(("stage", len(data)))
            else:
                self.state = self.STATE_DFU_DNLOAD_IDLE
            return len(data)
        self.ops.append(("write", addr))
        if self.verify_before_erase and not self.verified:
            self._fail(("write", addr))
            return len(data)
        self.writes += 1
        if self.writes == self.reboot_at_write:
            self.dead, self.verified = True, False
            raise UsbStall("device dropped mid-write")
        off = addr - SLOT
        if off == self.lost_write_at:
            self.lost_write_at = None             # the repair's rewrite lands
        else:
            self.flash[off:off + len(data)] = data
        self.state = self.STATE_DFU_DNLOAD_IDLE
        return len(data)

    def _ctrl_in(self, req, value, length):
        assert req == self.DFU_UPLOAD
        addr = self.pointer + (value - 2) * self.transfer_size
        if addr == USB_DFU_CAPS_VIRT_ADDR:
            self.ops.append(("caps", None))
            if self.caps == "stall":
                self.state = self.STATE_DFU_ERROR
                raise UsbStall("pipe error")
            return bytes(self.caps)[:length]
        off = addr - SLOT
        return bytes(self.flash[off:off + length])

    def kinds(self):
        return [k for k, _ in self.ops]


# --------------------------------------------------------------------------
# Capability probe
# --------------------------------------------------------------------------

def test_capability_word_is_read_behind_the_magic():
    assert FakeConsoleBootloader().read_capabilities() == 1


@pytest.mark.parametrize("caps", [
    b"\x00\x50\x00\x20\xC1\x01\x01\x08",          # 1.0.3-rc.1: flash bytes
    b"OWC0" + struct.pack("<I", 1),               # wrong magic
    b"OWC1",                                      # short read
])
def test_capability_bytes_without_the_magic_mean_none(caps):
    assert FakeConsoleBootloader(caps=caps).read_capabilities() == 0


def test_stalled_capability_read_is_cleared_and_means_none():
    dfu = FakeConsoleBootloader(caps="stall")
    assert dfu.read_capabilities() == 0
    assert dfu.state == DFU_IDLE                  # left usable, not in dfuERROR


def test_magic_without_the_staging_bit_needs_no_staging():
    dfu = FakeConsoleBootloader(caps=b"OWC1" + struct.pack("<I", 0))
    assert not dfu.requires_header_staging()


def test_transmitter_profile_never_probes():
    dfu = FakeConsoleBootloader(profile=TRANSMITTER_PROFILE)
    assert dfu.read_capabilities() == 0
    assert not dfu.prepare_slot_write(signed_image())
    assert dfu.ops == []                          # nothing sent to the device


# --------------------------------------------------------------------------
# Staging, then the normal write
# --------------------------------------------------------------------------

def test_header_is_staged_before_the_first_erase():
    image = signed_image()
    dfu = FakeConsoleBootloader()
    assert dfu.prepare_slot_write(image)
    dfu.write_memory(SLOT, image, erase_bytes=CONSOLE_PROFILE.slot_size)
    kinds = dfu.kinds()
    assert kinds[:2] == ["caps", "stage"]
    assert dfu.ops[1][1] == image[:USB_DFU_SFU_HEADER_LEN]
    assert "stage" not in kinds[2:]
    assert kinds.index("erase") > kinds.index("stage")
    assert dfu.refused == []
    assert bytes(dfu.flash[:len(image)]) == image


def test_without_staging_the_new_bootloader_refuses_the_write():
    # The failure SDK 2.1.0-rc.6 hit: erase refused (block-0 errors are
    # tolerated), then the first data block refused.
    dfu = FakeConsoleBootloader()
    with pytest.raises(DFUWriteRefusedError, match="offset 0x0"):
        dfu.write_memory(SLOT, signed_image())
    assert dfu.refused[0][0] == "erase"


@pytest.mark.parametrize("caps", ["stall", b"\xFF" * 8])
def test_older_bootloader_keeps_the_plain_flow(caps):
    image = signed_image()
    dfu = FakeConsoleBootloader(caps=caps)
    assert not dfu.prepare_slot_write(image)
    dfu.write_memory(SLOT, image)
    assert "stage" not in dfu.kinds()
    assert dfu.refused == []
    assert bytes(dfu.flash[:len(image)]) == image


def test_refused_header_raises_before_anything_is_erased():
    dfu = FakeConsoleBootloader(accept_header=False)
    with pytest.raises(DFUHeaderRefusedError, match="installed firmware is intact") as exc:
        dfu.prepare_slot_write(signed_image())
    assert isinstance(exc.value, ValueError)      # update_console's pre-flight refusal
    assert isinstance(exc.value, DFUWriteRefusedError)
    assert "erase" not in dfu.kinds() and "write" not in dfu.kinds()
    assert dfu._staged_header is None
    assert dfu.state == DFU_IDLE


def test_header_check_rides_out_busy_and_unanswered_polls():
    dfu = FakeConsoleBootloader(header_polls=["busy", "fail", "fail", "busy"])
    dfu.stage_header(signed_image())
    assert dfu.verified and dfu.header_polls == [] and dfu.scripted_polls


def test_header_check_that_never_finishes_times_out(monkeypatch):
    clock = iter(range(0, 1000, 5))               # 5 s per poll
    monkeypatch.setattr(LIFUDFU.time, "monotonic", lambda: next(clock))
    dfu = FakeConsoleBootloader(header_polls=["fail"] * 50)
    with pytest.raises(TimeoutError, match="last poll failed"):
        dfu.stage_header(signed_image())


def test_unsigned_image_is_refused_locally():
    dfu = FakeConsoleBootloader()
    with pytest.raises(ValueError, match="not a signed image"):
        dfu.stage_header(b"\x00" * 1024)
    assert "stage" not in dfu.kinds()


def test_reopened_device_gets_the_header_staged_again(monkeypatch):
    image = signed_image()
    dfu = FakeConsoleBootloader()
    dfu.prepare_slot_write(image)
    # The bus fault rebooted the bootloader: the handle is dead and the
    # verified header is forgotten.
    calls = {"n": 0}
    real_recover = dfu._recover_idle

    def dead_once():
        calls["n"] += 1
        if calls["n"] == 1:
            dfu.verified = False
            raise UsbStall("handle dead")
        real_recover()

    monkeypatch.setattr(dfu, "_recover_idle", dead_once)
    dfu._resync_session(SLOT, 0x400)
    assert dfu.kinds().count("stage") == 2
    assert dfu.verified
    assert dfu.pointer == SLOT + 0x400            # resumes where it failed


# --------------------------------------------------------------------------
# Console update path
# --------------------------------------------------------------------------

@pytest.fixture
def console_path(monkeypatch, tmp_path):
    """_program_slot_image with the image check, the installed-version read
    and the USB device faked. Returns (image path, holder for the fake)."""
    image = signed_image(8000)
    path = tmp_path / "console-signed.bin"
    path.write_bytes(image)
    report = SimpleNamespace(ok=True, structural_ok=True,
                             header=SimpleNamespace(fw_version=2144, fw_version_str="1.3.0"),
                             describe=lambda: "")
    monkeypatch.setattr(LIFUCrypto, "validate_signed_image", lambda *_a, **_k: report)
    monkeypatch.setattr(LIFUDFUManager, "_get_installed_slot_version",
                        lambda *_a, **_k: 2144)
    holder = {}

    def make(**kwargs):
        dev = FakeConsoleBootloader(**holder.get("kwargs", {}))
        holder["dev"] = dev
        return dev

    monkeypatch.setattr(LIFUDFU, "STM32USBDFU", make)
    return path, image, holder


def test_console_update_stages_then_writes_and_verifies(console_path):
    path, image, holder = console_path
    LIFUDFUManager().program_console(str(path))
    dev = holder["dev"]
    assert dev.kinds()[:2] == ["caps", "stage"]
    assert dev.refused == []
    assert bytes(dev.flash[:len(image)]) == image


def test_console_update_on_an_older_bootloader_is_unchanged(console_path):
    path, image, holder = console_path
    holder["kwargs"] = {"caps": "stall"}
    LIFUDFUManager().program_console(str(path))
    dev = holder["dev"]
    assert "stage" not in dev.kinds()
    assert bytes(dev.flash[:len(image)]) == image


def test_refused_header_boots_the_app_back(console_path, monkeypatch):
    path, _image, holder = console_path
    holder["kwargs"] = {"accept_header": False}
    mgr = LIFUDFUManager()
    monkeypatch.setattr(mgr, "get_console_bootloader_version",
                        lambda **_k: "1.1.0-rc.0+research")
    aborted = []
    monkeypatch.setattr(mgr, "abort_dfu", lambda **k: aborted.append(k))
    with pytest.raises(DFUHeaderRefusedError):
        mgr.update_console(str(path), enter_dfu_fn=lambda: None, dfu_wait_s=0)
    assert aborted and aborted[0]["profile"] is CONSOLE_PROFILE
    assert "erase" not in holder["dev"].kinds()


def test_a_restage_refused_mid_write_leaves_the_unit_in_dfu(console_path, monkeypatch):
    # The bootloader reboots on the third block, after the whole slot was
    # erased, and refuses the header staged again on the re-opened device.
    path, _image, holder = console_path
    holder["kwargs"] = {"reboot_at_write": 3, "restage": "refuse"}
    mgr = LIFUDFUManager()
    monkeypatch.setattr(mgr, "get_console_bootloader_version",
                        lambda **_k: "1.1.0-rc.0+research")
    aborted = []
    monkeypatch.setattr(mgr, "abort_dfu", lambda **k: aborted.append(k))
    with pytest.raises(RuntimeError, match="already erased") as exc:
        mgr.update_console(str(path), enter_dfu_fn=lambda: None, dfu_wait_s=0)
    assert not isinstance(exc.value, ValueError)  # not a pre-flight refusal
    assert isinstance(exc.value.__cause__, DFUHeaderRefusedError)
    assert "intact" not in str(exc.value)
    assert aborted == []                          # no reset into an erased slot
    dev = holder["dev"]
    assert dev.kinds().count("stage") == 2 and "erase" in dev.kinds()


def test_a_restage_timeout_during_repair_is_not_reported_as_success(console_path, monkeypatch):
    # The block at 0x800 is lost, so the read-back repairs its page; the
    # bootloader reboots on that rewrite (write 9 of 8 + 2) and never
    # answers the header staged again.
    path, _image, holder = console_path
    holder["kwargs"] = {"lost_write_at": 0x800, "reboot_at_write": 9,
                        "restage": "no answer"}
    monkeypatch.setattr(LIFUDFU, "USB_DFU_HDR_VERIFY_TIMEOUT_S", 0)
    with pytest.raises(RuntimeError, match="already erased") as exc:
        LIFUDFUManager().program_console(str(path))
    assert isinstance(exc.value.__cause__, TimeoutError)
    dev = holder["dev"]
    assert dev.kinds().count("stage") == 2
    assert dev.writes == 9                        # the repair started, then dropped
