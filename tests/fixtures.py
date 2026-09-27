"""Shared test data: synthetic folder tables and wire data shaped like a real
ICD-ST25, and an emulated recorder (no recordings are stored in this repository)."""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from st25 import policy  # noqa: E402
from st25.folder import FIRST_ENTRY_PAGE, PAGE, TABLE_SIZE  # noqa: E402
from st25.protocol import CMD_FOLDER_INFO, CMD_GET_VOICE, _completion_opcode  # noqa: E402

DATE = bytes.fromhex("07ed051713360403")   # 2029-05-23 19:54:04, Wednesday


def make_table(messages):
    """messages: list of (slot, start_counter, start_addr, length, date, owner)."""
    t = bytearray(b"\xff" * TABLE_SIZE)

    def put(page, offset, data):
        base = page * PAGE + offset
        t[base:base + len(data)] = data

    for i, (slot, counter, start, length, date, owner) in enumerate(messages):
        put(0, i * 4, struct.pack(">HH", slot, 0x6000))
        put(2 + slot // 128, (slot % 128) * 4, struct.pack(">I", counter))
        put(5 + slot // 64, (slot % 64) * 8, struct.pack(">II", start, (start + length - 1) | 0x80000000))
        entry = bytearray(b"\xff" * 512)
        entry[274:276] = b"\x03\x00"
        name = owner.encode("latin-1")
        entry[276:276 + len(name) + 1] = name + b"\0"
        entry[452:460] = date
        put(FIRST_ENTRY_PAGE + slot, 0, bytes(entry))
    put(4, 0, bytes(range(20)))          # page 4 is noise, parser must ignore it
    return bytes(t)


def make_raw(length, counter, marker=b"\x00\x0a", short_block=None):
    """Wire data for one message: blocks of 2 x (512 data + 16 spare)."""
    blocks = -(-length // 1024)
    out = bytearray()
    remaining = length
    for k in range(blocks):
        valid = min(1024, remaining)
        if short_block == k:
            valid = 600
        remaining -= min(1024, remaining)
        block = bytearray(1024)
        block[0:2] = b"\x00\x0a"
        block[2:4] = marker
        block[4:6] = struct.pack(">H", valid)
        block[6:10] = struct.pack(">I", counter + k)
        for j in range(10, valid):
            block[j] = (j * 7 + k) & 0xFF
        spare = b"\xff\x03\x0c\x3c\xff\xff\xff\xff\x02\xc1\x44\x1c\x2c\xff\xff\xff"
        out += block[:512] + spare + block[512:] + spare
    return bytes(out)


class FakeRecorderDevice:
    """Emulates the recorder's side of the protocol, enforcing the real policy.

    folders maps 1..5 (A..E) to make_table() message tuples; other folders are
    empty. GET_VOICE serves make_raw() for message N of the folder whose table
    was read last, as the real recorder does.
    """
    CONTROL_BUF = 4096

    def __init__(self, folders=None):
        self.folders = folders or {}
        self.queue = []          # pending replies (bytes)
        self.bulk = b""
        self.current = None
        self.voice_calls = []

    def _msg(self, first, opcode, body):
        return bytes([first]) + policy.FRAME_PREFIX[1:] + struct.pack(">I", opcode) + body

    def control_out(self, rt, req, val, idx, data, timeout_ms):
        assert policy.allow_out(rt, req, val, idx, data), data.hex()
        words = struct.unpack(f">{(len(data) - 12) // 4}I", data[12:])
        op = words[0]
        done = _completion_opcode(op)
        if op & 0xFFFF00FF == CMD_FOLDER_INFO:
            self.current = (op >> 8) & 0xFF
            size = struct.pack(">I", TABLE_SIZE)
            self.queue = [self._msg(0x0F, op, size + bytes(8)), self._msg(0x09, done, size + bytes(8))]
            self.bulk = make_table(self.folders.get(self.current, []))
        elif op == CMD_GET_VOICE:
            number = words[1] >> 16
            _slot, counter, _start, length, _date, _owner = self.folders[self.current][number - 1]
            self.voice_calls.append((self.current, number))
            raw = make_raw(length, counter)
            size = struct.pack(">I", len(raw))
            self.queue = [self._msg(0x0F, op, bytes(12) + size), self._msg(0x09, done, bytes(12) + size)]
            self.bulk = raw
        else:
            self.queue = [self._msg(0x09, done, bytes(48))]

    def control_in(self, rt, req, val, idx, length, timeout_ms):
        assert policy.allow_in(rt, req, val, idx, length)
        if req == policy.REQ_STATUS:
            if self.queue and not self.bulk_pending():
                return bytes.fromhex("0f81") + struct.pack(">H", len(self.queue[0]))
            return bytes(4)
        return self.queue.pop(0)

    def bulk_pending(self):
        return len(self.queue) == 1 and bool(self.bulk)

    def bulk_in_into(self, ep, dest, offset, max_len, timeout_ms):
        assert policy.allow_bulk_in(ep)
        n = min(max_len, 65536, len(self.bulk))
        dest[offset:offset + n] = self.bulk[:n]
        self.bulk = self.bulk[n:]
        return n, 0

    def close(self):
        pass


def st25_manager(ids, open_session, **kwargs):
    """An app.devices.DeviceManager over emulated ICD-ST25s: ids() gives the
    connection ids attached now ("<port>@<address>", or "setup:<instance>" for
    one whose driver is not set up), open_session(id) an st25 RecorderSession
    for one; the app uses it through the ST25 model's adapter, as it uses a
    real one (discovered and opened like openevp.recorders.sony_st25 does)."""
    from app.devices import DeviceManager
    from openevp.recorders import base
    from openevp.recorders.sony_st25 import SETUP_MESSAGE, SETUP_PREFIX, ST25Session, SonyST25

    def discover():
        found = []
        for i in ids():
            if i.startswith(SETUP_PREFIX):
                found.append(base.DiscoveredDevice(i, SonyST25.model_id, "", state=base.NEEDS_DRIVER,
                                                   message=SETUP_MESSAGE))
            else:
                found.append(base.DiscoveredDevice(i, SonyST25.model_id, "port " + i.split("@")[0], locator=i,
                                                   where="on USB port " + i.split("@")[0]))
        return found, []
    return DeviceManager(discover, lambda model, device: ST25Session(open_session(device.locator)), **kwargs)
