# SPDX-License-Identifier: Apache-2.0
"""Transmitter preset slots (open-lifu-transmitter-bl >= 1.1.0-rc.1).

Pages 96-125 hold 15 slots of 4 KB, each one signed preset: a PRS1 header
and one preset blob. The bootloader advertises them with capability bit 1
(USB: the 0xFFFFFF18 upload; I2C: GETCAPS) and writes a slot only after the
preset's header, DNLOADed to 0xFFFFFF20, verifies. These tests drive the SDK
against fakes of that bootloader over USB (the master) and over I2C through
the master (a slave).
"""

from __future__ import annotations

import struct

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from openlifu_sdk.io import LIFUCrypto, LIFUDFU  # noqa: E402
from openlifu_sdk.io import LIFUPresetPackage as pp  # noqa: E402
from openlifu_sdk.io.LIFUDFU import (  # noqa: E402
    I2C_DFU_CMD_GETCAPS, TRANSMITTER_PROFILE, USB_DFU_PRESET_STAGE_VIRT_ADDR,
    DFUHeaderRefusedError, LIFUDFUManager, STM32I2CDFUviaMaster,
)
from test_dfu_header_staging import (  # noqa: E402
    CAPS_OK, SLOT, FakeConsoleBootloader, signed_image,
)

BASE = pp.TX_PRESET_BASE
END = BASE + pp.TX_PRESET_SLOTS * pp.TX_PRESET_SLOT_SIZE
CAPS_PRESET = b"OWC1" + struct.pack("<I", 3)
RESET = TRANSMITTER_PROFILE.reset_virt_addr


def _preset(tmp_path, name="k", blob=b"OWPR" + bytes(range(256)) * 13):
    priv = ec.generate_private_key(ec.SECP256R1())
    keys = LIFUCrypto.SigningKeys(keys_dir=tmp_path / name, private_key=priv,
                                  public_key=priv.public_key())
    return pp.build_tx_preset(blob, diathermy_sha="ab" * 20, context="research", keys=keys), keys.public_key


# --------------------------------------------------------------------------
# The slot image
# --------------------------------------------------------------------------

def test_slots_are_fixed_4k_from_page_96():
    assert (BASE, pp.TX_PRESET_SLOT_SIZE, pp.TX_PRESET_SLOTS) == (0x08030000, 0x1000, 15)
    assert pp.tx_preset_slot_address(0) == 0x08030000
    assert pp.tx_preset_slot_address(14) == 0x0803E000
    assert END == 0x0803F000                      # the anti-rollback page
    assert (TRANSMITTER_PROFILE.preset_base, TRANSMITTER_PROFILE.preset_size,
            TRANSMITTER_PROFILE.preset_slot_size) == (BASE, END - BASE, 0x1000)
    for bad in (-1, 15):
        with pytest.raises(ValueError, match="slot"):
            pp.tx_preset_slot_address(bad)


def test_a_slot_image_is_a_prs1_header_then_the_blob(tmp_path):
    blob = b"OWPR" + bytes(3536 - 4)
    data, key = _preset(tmp_path, blob=blob)
    pkg = pp.parse_preset_package(data)
    assert data[:4] == b"PRS1" and pkg.body == blob and pkg.body_ok
    assert pp.verify_preset_package(data, key)


def test_a_blob_must_fit_its_slot(tmp_path):
    room = pp.TX_PRESET_SLOT_SIZE - pp.PRESET_BODY_OFFSET          # 3776
    assert _preset(tmp_path, blob=b"\x01" * room)[0]
    with pytest.raises(ValueError, match="page holds"):
        _preset(tmp_path, blob=b"\x01" * (room + 1))


# --------------------------------------------------------------------------
# USB (the master)
# --------------------------------------------------------------------------

class FakeTxBootloader(FakeConsoleBootloader):
    """The transmitter bootloader's preset slots over USB DFU: a slot erases
    and writes only after a PRS1 header was staged and accepted, and only the
    slot the header is bound to."""

    def __init__(self, accept_preset=True, caps=CAPS_PRESET, **kwargs):
        super().__init__(caps=caps, profile=TRANSMITTER_PROFILE, **kwargs)
        self.verify_before_erase = False          # slot writes check in-band
        self.flash = bytearray(b"\xFF" * (0x0803F800 - SLOT))
        self.accept_preset = accept_preset
        self.staged = False
        self.bound = None

    def _slot_of(self, addr):
        return (addr - BASE) // pp.TX_PRESET_SLOT_SIZE

    def _ctrl_out(self, req, value, data=b""):
        if req == self.DFU_DNLOAD and value == 0 and data and data[0] == self.CMD_ERASE:
            addr = int.from_bytes(data[1:5], "little")
            if BASE <= addr < END:
                self.ops.append(("erase", addr))
                slot = self._slot_of(addr)
                if not self.staged or self.bound not in (None, slot):
                    self._fail(("erase", addr))
                else:
                    self.bound = slot
                    off = BASE + slot * pp.TX_PRESET_SLOT_SIZE - SLOT
                    self.flash[off:off + pp.TX_PRESET_SLOT_SIZE] = b"\xFF" * pp.TX_PRESET_SLOT_SIZE
                    self.state = self.STATE_DFU_DNLOAD_IDLE
                return len(data)
        if req == self.DFU_DNLOAD and value >= 2:
            addr = self.pointer + (value - 2) * self.transfer_size
            if addr == RESET:
                self.ops.append(("reset", addr))
                return len(data)
            if addr == USB_DFU_PRESET_STAGE_VIRT_ADDR:
                self.ops.append(("stage_preset", bytes(data)))
                self.staged = self.accept_preset and bytes(data[:4]) == b"PRS1"
                self.bound = None
                if not self.staged:
                    self._fail(("stage_preset", len(data)))
                else:
                    self.state = self.STATE_DFU_DNLOAD_IDLE
                return len(data)
            if BASE <= addr < END:
                self.ops.append(("write", addr))
                if not self.staged or self.bound != self._slot_of(addr):
                    self._fail(("write", addr))
                else:
                    self.flash[addr - SLOT:addr - SLOT + len(data)] = data
                    self.state = self.STATE_DFU_DNLOAD_IDLE
                return len(data)
        return super()._ctrl_out(req, value, data)


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def make(**_k):
        holder["dev"] = holder.get("dev") or FakeTxBootloader(**holder.get("kwargs", {}))
        return holder["dev"]
    monkeypatch.setattr(LIFUDFU, "STM32USBDFU", make)
    return holder


def test_a_slot_write_never_reaches_the_presets():
    dev = FakeTxBootloader()
    assert dev.slot_erase_bytes() == TRANSMITTER_PROFILE.slot_size == BASE - SLOT
    assert dev.ops == []                          # and needs no capability read


def test_preset_write_stages_then_erases_and_writes_only_its_slot(fake, tmp_path):
    data, key = _preset(tmp_path)
    LIFUDFUManager().program_transmitter_preset(3, data, public_key=key)
    dev = fake["dev"]
    kinds = dev.kinds()
    slot3 = pp.tx_preset_slot_address(3)
    assert kinds.index("stage_preset") < kinds.index("erase")
    assert dev.ops[kinds.index("stage_preset")][1] == data[:320]
    assert {a for k, a in dev.ops if k in ("erase", "write")} <= set(range(slot3, slot3 + 0x1000))
    assert bytes(dev.flash[slot3 - SLOT:slot3 - SLOT + len(data)]) == data
    assert kinds[-1] == "reset"                   # back to the application
    assert dev.refused == []


def test_a_refused_preset_leaves_the_slots(fake, tmp_path):
    fake["kwargs"] = {"accept_preset": False}
    data, _key = _preset(tmp_path)
    with pytest.raises(DFUHeaderRefusedError):
        LIFUDFUManager().program_transmitter_preset(0, data)
    assert "erase" not in fake["dev"].kinds() and "write" not in fake["dev"].kinds()


@pytest.mark.parametrize("caps", [CAPS_OK, "stall"])
def test_a_bootloader_without_preset_slots_is_refused_before_staging(fake, tmp_path, caps):
    fake["kwargs"] = {"caps": caps}
    data, _key = _preset(tmp_path)
    with pytest.raises(RuntimeError, match="no preset slots"):
        LIFUDFUManager().program_transmitter_preset(0, data)
    assert "stage_preset" not in fake["dev"].kinds()


def test_pre_flight_never_opens_the_device(fake, tmp_path):
    data, _key = _preset(tmp_path, "a")
    _other, other_key = _preset(tmp_path, "b")
    with pytest.raises(ValueError, match="not signed by the given key"):
        LIFUDFUManager().program_transmitter_preset(0, data, public_key=other_key)
    bad = bytearray(data)
    bad[-1] ^= 0x01
    with pytest.raises(ValueError, match="does not match its signed hash"):
        LIFUDFUManager().program_transmitter_preset(0, bytes(bad))
    with pytest.raises(ValueError, match="slot"):
        LIFUDFUManager().program_transmitter_preset(15, data)
    with pytest.raises(LIFUCrypto.LIFUCryptoError, match="Not a preset package"):
        LIFUDFUManager().program_transmitter_preset(0, signed_image())
    assert "dev" not in fake


# --------------------------------------------------------------------------
# I2C through the master (a slave)
# --------------------------------------------------------------------------

class FakeI2C:
    """STM32I2CDFUviaMaster's DFU commands, recorded, against a slave
    bootloader with (or without) preset slots."""

    def __init__(self, uart=None, i2c_addr=0x20, caps=3):
        self.addr, self.caps, self.calls = i2c_addr, caps, []

    def read_capabilities(self):
        self.calls.append(("caps",))
        return self.caps

    def stage_preset(self, package):
        self.calls.append(("stage", bytes(package[:320])))

    def erase_page(self, address):
        self.calls.append(("erase", address))

    def write_memory(self, address, data, progress_callback=None):
        self.calls.append(("write", address, bytes(data)))

    def reset(self):
        self.calls.append(("reset",))


@pytest.fixture
def i2c(monkeypatch):
    holder = {}

    def make(**k):
        holder["dev"] = FakeI2C(**k, **holder.get("kwargs", {}))
        return holder["dev"]
    monkeypatch.setattr(LIFUDFU, "STM32I2CDFUviaMaster", make)
    return holder


def test_slave_preset_write_order(i2c, tmp_path):
    data, key = _preset(tmp_path)
    LIFUDFUManager(uart=object()).program_transmitter_slave_preset_i2c(
        2, data, i2c_addr=0x20, public_key=key)
    slot2 = pp.tx_preset_slot_address(2)
    assert i2c["dev"].calls == [("caps",), ("stage", data[:320]), ("erase", slot2),
                                ("write", slot2, data), ("reset",)]


def test_slave_without_preset_slots_is_refused_before_staging(i2c, tmp_path):
    i2c["kwargs"] = {"caps": 1}
    data, _key = _preset(tmp_path)
    with pytest.raises(RuntimeError, match="no preset slots"):
        LIFUDFUManager(uart=object()).program_transmitter_slave_preset_i2c(0, data, i2c_addr=0x20)
    assert i2c["dev"].calls == [("caps",)]


def test_slave_needs_the_master_uart(tmp_path):
    data, _key = _preset(tmp_path)
    with pytest.raises(RuntimeError, match="master UART"):
        LIFUDFUManager().program_transmitter_slave_preset_i2c(0, data, i2c_addr=0x20)


@pytest.mark.parametrize("reply, caps", [
    (bytes([0, 0]) + b"OWC1" + struct.pack("<I", 3), 3),
    (bytes([2, 0]) + bytes(8), 0),                 # bootloader before 1.1.0-rc.1
    (bytes([0, 0]) + b"XXXX" + struct.pack("<I", 3), 0),
])
def test_i2c_getcaps(monkeypatch, reply, caps):
    dfu = STM32I2CDFUviaMaster(uart=None, i2c_addr=0x20)
    sent = []
    monkeypatch.setattr(dfu, "_exchange", lambda p, n: sent.append((p, n)) or reply)
    assert dfu.read_capabilities() == caps
    assert sent == [(bytes([I2C_DFU_CMD_GETCAPS]), 10)]


def test_i2c_stage_sends_the_header_to_the_staging_address(monkeypatch, tmp_path):
    data, _key = _preset(tmp_path)
    dfu = STM32I2CDFUviaMaster(uart=None, i2c_addr=0x20)
    blocks = []
    monkeypatch.setattr(dfu, "write_block", lambda a, d: blocks.append((a, d)))
    dfu.stage_preset(data)
    assert blocks == [(USB_DFU_PRESET_STAGE_VIRT_ADDR, data[:320])]
    with pytest.raises(ValueError, match="PRS1"):
        dfu.stage_preset(signed_image())


# --------------------------------------------------------------------------
# Through the application (standard images; FDA images only read)
# --------------------------------------------------------------------------

from unittest.mock import MagicMock  # noqa: E402

from openlifu_sdk.io.exceptions import LIFUDeviceError  # noqa: E402
from openlifu_sdk.io.LIFUConfig import (  # noqa: E402
    OW_CONTROLLER, OW_ERROR, OW_PRESET_COMMIT, OW_PRESET_EMPTY, OW_PRESET_INFO,
    OW_PRESET_NO_ROOM, OW_PRESET_WRITE, OW_RESP,
)
from openlifu_sdk.io.LIFUTXDevice import LIFUTxDevice, TxDevice  # noqa: E402
from openlifu_sdk.io.uart import OWUart  # noqa: E402


def _reply(packet_type=OW_RESP, reserved=0, data=b""):
    r = MagicMock()
    r.packet_type, r.reserved = packet_type, reserved
    r.data, r.data_len = bytearray(data), len(data)
    return r


def _tx(make, replies):
    dev = make()
    uart = MagicMock(spec=OWUart)
    uart.desc = "TX"
    uart.demo_mode = False
    uart.asyncMode = False
    uart.is_connected = True
    uart.send_packet.side_effect = list(replies)
    dev._uart = uart
    return dev


def test_an_unsigned_slot_image_carries_the_blob_size_and_hash():
    blob = b"OWPR" + bytes(500)
    image = pp.tx_slot_image(blob)
    hdr = pp.parse_preset_header(image)
    assert image[:4] == b"PRS1" and image[0x140:] == blob
    assert hdr.body_size == len(blob)
    pkg = pp.parse_preset_package(image)
    assert pkg.body_ok
    with pytest.raises(ValueError, match="slot holds"):
        pp.tx_slot_image(b"\x01" * (pp.TX_PRESET_SLOT_SIZE - 0x140 + 1))


def test_write_preset_sends_the_slot_image_and_commits_it(tmp_path):
    import zlib
    data, _key = _preset(tmp_path)
    chunks = len(range(0, len(data), 512))
    dev = _tx(LIFUTxDevice, [_reply()] * (chunks + 1))
    assert dev.write_preset(4, data, module=1)
    sent = [c.kwargs for c in dev._uart.send_packet.call_args_list]
    writes = [s for s in sent if s["command"] == OW_PRESET_WRITE]
    assert all((s["reserved"], s["addr"]) == (4, 1) for s in sent)
    assert b"".join(bytes(w["data"])[2:] for w in writes) == data
    assert sent[-1]["command"] == OW_PRESET_COMMIT
    assert bytes(sent[-1]["data"]) == struct.pack("<II", len(data), zlib.crc32(data))


def test_write_preset_wraps_a_bare_blob_in_an_unsigned_header():
    blob = b"OWPR" + bytes(200)
    dev = _tx(LIFUTxDevice, [_reply()] * 3)       # two parts (320 + 204 bytes), commit
    dev.write_preset(0, blob)
    first = bytes(dev._uart.send_packet.call_args_list[0].kwargs["data"])
    assert first[2:6] == b"PRS1"
    commit = bytes(dev._uart.send_packet.call_args_list[-1].kwargs["data"])
    assert struct.unpack("<I", commit[:4])[0] == 0x140 + len(blob)


@pytest.mark.parametrize("make", [TxDevice, LIFUTxDevice])
def test_preset_info_reads_a_slot_header(make, tmp_path):
    data, key = _preset(tmp_path)
    dev = _tx(make, [_reply(data=data[:320])])
    info = dev.get_preset_info(2, module=1)
    assert (info.diathermy_sha, info.context) == ("ab" * 20, "research")
    assert info.signed_by(key)
    sent = dev._uart.send_packet.call_args.kwargs
    assert (sent["packet_type"], sent["command"], sent["reserved"], sent["addr"]) == (
        OW_CONTROLLER, OW_PRESET_INFO, 2, 1)


def test_preset_info_of_an_empty_slot_is_none():
    dev = _tx(TxDevice, [_reply(OW_ERROR, OW_PRESET_EMPTY)])
    assert dev.get_preset_info(0) is None


def test_preset_info_without_preset_slots_raises():
    dev = _tx(TxDevice, [_reply(OW_ERROR, OW_PRESET_NO_ROOM)])
    with pytest.raises(LIFUDeviceError):
        dev.get_preset_info(0)


def test_the_fda_component_reads_preset_info_but_cannot_write():
    from openlifu_sdk.io.LIFUConfig import TX_FDA_PACKETS
    assert OW_PRESET_INFO in TX_FDA_PACKETS[OW_CONTROLLER]
    assert OW_PRESET_WRITE not in TX_FDA_PACKETS[OW_CONTROLLER]
