# SPDX-License-Identifier: Apache-2.0
"""Console preset page over USB DFU (open-lifu-console-bl >= 1.1.0-rc.2).

That bootloader's slot stops at page 60; page 61 holds a signed preset
package. It advertises this with capability bit 1 and takes the page only
after the package's PRS1 header, DNLOADed to 0xFFFFFF20, verifies. These tests
drive the SDK against a fake of that bootloader: an application update must
leave page 61 alone, and a preset write must stage before it erases.
"""

from __future__ import annotations

import struct

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from openlifu_sdk.io import LIFUCrypto, LIFUDFU  # noqa: E402
from openlifu_sdk.io import LIFUPresetPackage as pp  # noqa: E402
from openlifu_sdk.io.LIFUDFU import (  # noqa: E402
    CONSOLE_PROFILE, USB_DFU_PRESET_STAGE_VIRT_ADDR, DFUHeaderRefusedError,
    LIFUDFUManager,
)
from test_dfu_header_staging import (  # noqa: E402
    CAPS_OK, SLOT, FakeConsoleBootloader, console_path, signed_image,  # noqa: F401
)

PRESET = CONSOLE_PROFILE.preset_base
PRESET_END = PRESET + CONSOLE_PROFILE.preset_size
CAPS_PRESET = b"OWC1" + struct.pack("<I", 3)


class FakePresetBootloader(FakeConsoleBootloader):
    """The console bootloader with the preset page: page-61 erase and write
    only after a PRS1 header was staged at 0xFFFFFF20 and accepted."""

    def __init__(self, accept_preset=True, caps=CAPS_PRESET, **kwargs):
        super().__init__(caps=caps, **kwargs)
        self.verify_before_erase = True
        self.accept_preset = accept_preset
        self.preset_verified = False
        self.manifested = False

    def _ctrl_out(self, req, value, data=b""):
        if req == self.DFU_DNLOAD and value == 0 and data and data[0] == self.CMD_ERASE:
            addr = int.from_bytes(data[1:5], "little")
            if PRESET <= addr < PRESET_END:
                self.ops.append(("erase", addr))
                if not self.preset_verified:
                    self._fail(("erase", addr))
                else:
                    self.flash[addr - SLOT:addr - SLOT + 2048] = b"\xFF" * 2048
                    self.state = self.STATE_DFU_DNLOAD_IDLE
                return len(data)
        if req == self.DFU_DNLOAD and value >= 2:
            addr = self.pointer + (value - 2) * self.transfer_size
            if addr == USB_DFU_PRESET_STAGE_VIRT_ADDR:
                self.ops.append(("stage_preset", bytes(data)))
                self.preset_verified = self.accept_preset and bytes(data[:4]) == b"PRS1"
                if not self.preset_verified:
                    self._fail(("stage_preset", len(data)))
                else:
                    self.state = self.STATE_DFU_DNLOAD_IDLE
                return len(data)
            if PRESET <= addr < PRESET_END:
                self.ops.append(("write", addr))
                if not self.preset_verified:
                    self._fail(("write", addr))
                else:
                    self.flash[addr - SLOT:addr - SLOT + len(data)] = data
                    self.state = self.STATE_DFU_DNLOAD_IDLE
                return len(data)
        if req == self.DFU_DNLOAD and not data:
            self.manifested = True
        return super()._ctrl_out(req, value, data)


def _package(tmp_path, name="k", body=None):
    priv = ec.generate_private_key(ec.SECP256R1())
    keys = LIFUCrypto.SigningKeys(keys_dir=tmp_path / name, private_key=priv,
                                  public_key=priv.public_key())
    body = body or pp.console_preset_body([
        {"id": "soft", "settings_crc": 0x1234, "voltage": 40, "sensitivity": 3728,
         "voltage_range": [35, 45]}])
    data = pp.build_preset_package(body, diathermy_sha="ab" * 20, context="test", keys=keys)
    return data, keys.public_key


# --------------------------------------------------------------------------
# The slot erase follows the bootloader
# --------------------------------------------------------------------------

@pytest.mark.parametrize("caps, expected", [
    (CAPS_PRESET, PRESET - SLOT),                 # 0xE800: pages 32-60
    (CAPS_OK, CONSOLE_PROFILE.slot_size),         # 1.1.0-rc.0/rc.1: pages 32-61
    (b"\xFF" * 8, CONSOLE_PROFILE.slot_size),     # 1.0.x: no capability word
])
def test_slot_erase_stops_at_the_preset_page_only_when_there_is_one(caps, expected):
    assert FakePresetBootloader(caps=caps).slot_erase_bytes() == expected


def test_an_application_update_leaves_the_presets(console_path, monkeypatch, tmp_path):  # noqa: F811
    path, image, holder = console_path
    package, _key = _package(tmp_path)
    dev = FakePresetBootloader()
    dev.flash[PRESET - SLOT:PRESET - SLOT + len(package)] = package
    monkeypatch.setattr(LIFUDFU, "STM32USBDFU", lambda **_k: dev)
    LIFUDFUManager().program_console(str(path))
    assert not [a for k, a in dev.ops if k == "erase" and PRESET <= a < PRESET_END]
    assert bytes(dev.flash[:len(image)]) == image
    assert bytes(dev.flash[PRESET - SLOT:PRESET - SLOT + len(package)]) == package


# --------------------------------------------------------------------------
# Writing the preset page
# --------------------------------------------------------------------------

@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def make(**_k):
        holder["dev"] = holder.get("dev") or FakePresetBootloader(**holder.get("kwargs", {}))
        return holder["dev"]
    monkeypatch.setattr(LIFUDFU, "STM32USBDFU", make)
    return holder


def test_preset_write_stages_then_erases_only_page_61(fake, tmp_path):
    package, key = _package(tmp_path)
    LIFUDFUManager().program_console_presets(package, public_key=key)
    dev = fake["dev"]
    kinds = dev.kinds()
    assert kinds.index("stage_preset") < kinds.index("erase")
    assert dev.ops[kinds.index("stage_preset")][1] == package[:320]
    assert [a for k, a in dev.ops if k == "erase"] == [PRESET]
    assert bytes(dev.flash[PRESET - SLOT:PRESET - SLOT + len(package)]) == package
    assert dev.manifested


def test_a_refused_package_leaves_the_page(fake, tmp_path):
    fake["kwargs"] = {"accept_preset": False}
    package, _key = _package(tmp_path)
    with pytest.raises(DFUHeaderRefusedError, match="installed presets are intact"):
        LIFUDFUManager().program_console_presets(package)
    assert "erase" not in fake["dev"].kinds() and "write" not in fake["dev"].kinds()


def test_a_bootloader_without_the_preset_page_is_refused_before_staging(fake, tmp_path):
    fake["kwargs"] = {"caps": CAPS_OK}
    package, _key = _package(tmp_path)
    with pytest.raises(RuntimeError, match="no preset page"):
        LIFUDFUManager().program_console_presets(package)
    assert "stage_preset" not in fake["dev"].kinds()


def test_pre_flight_wrong_key_never_opens_the_device(fake, tmp_path):
    package, _key = _package(tmp_path, "a")
    _other, other_key = _package(tmp_path, "b")
    with pytest.raises(ValueError, match="not signed by the given key"):
        LIFUDFUManager().program_console_presets(package, public_key=other_key)
    assert "dev" not in fake


def test_pre_flight_changed_body_never_opens_the_device(fake, tmp_path):
    package, _key = _package(tmp_path)
    bad = bytearray(package)
    bad[-1] ^= 0x01
    with pytest.raises(ValueError, match="does not match its signed hash"):
        LIFUDFUManager().program_console_presets(bytes(bad))
    assert "dev" not in fake


def test_pre_flight_rejects_a_firmware_image(fake):
    with pytest.raises(LIFUCrypto.LIFUCryptoError, match="Not a preset package"):
        LIFUDFUManager().program_console_presets(signed_image())
    assert "dev" not in fake
