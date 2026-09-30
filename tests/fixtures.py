"""Shared test data: synthetic folder tables and wire data shaped like a real
ICD-ST25, and an emulated recorder (no recordings are stored in this repository)."""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from st25 import policy  # noqa: E402
from st25.folder import FIRST_ENTRY_PAGE, PAGE, TABLE_SIZE  # noqa: E402
from st25.protocol import CMD_FOLDER_INFO, _completion_opcode  # noqa: E402

DATE = bytes.fromhex("07ed051713360403")   # 2029-05-23 19:54:04, Wednesday


def make_table(messages):
    """messages: list of (slot, start_counter, start_addr, length, date, owner),
    optionally with a 7th item, the message-list mode byte (0x00 LP, as on the
    ICD-ST25; 0x6c for the ICD-ST10's LPEC ST, whose entries carry a 90 00 tag
    where the ST25 has 03 00 before the owner name)."""
    t = bytearray(b"\xff" * TABLE_SIZE)

    def put(page, offset, data):
        base = page * PAGE + offset
        t[base:base + len(data)] = data

    for i, (slot, counter, start, length, date, owner, *mode) in enumerate(messages):
        mode = mode[0] if mode else 0x00
        put(0, i * 4, struct.pack(">HH", slot, 0x6000 | mode))
        put(2 + slot // 128, (slot % 128) * 4, struct.pack(">I", counter))
        put(5 + slot // 64, (slot % 64) * 8, struct.pack(">II", start, (start + length - 1) | 0x80000000))
        entry = bytearray(b"\xff" * 512)
        entry[274:276] = b"\x03\x00" if mode == 0x00 else b"\x90\x00"
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


ST_FRAME = 283                        # ICD-ST10 LPEC ST: 3-byte header (counter, flags) + 280 bytes


def make_st_frames(counters):
    """An LPEC ST frame stream (synthetic: not real audio): one 283-byte frame
    per counter, each a 16-bit counter, a zero flags byte and 280 filler bytes."""
    out = bytearray()
    for i, c in enumerate(counters):
        out += struct.pack(">HB", c, 0) + bytes((i * 13 + j * 7) & 0xFF for j in range(ST_FRAME - 3))
    return bytes(out)


ST_VECTOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vectors", "lpec_st", "all-features-40")


def st_audio_frames():
    """40 LPEC ST frames that decode: tests/vectors/lpec_st/all-features-40.bin
    (generated to cover every feature of the codec, not recorded audio), its
    last frame's counter set to 1 so that, like the recorder's, the stream
    restarts at 0, 1 (a .dvf accepts no other). The counter is not codec data:
    Sony's PCM for these frames is st_audio_pcm() (37 frames of 2048 stereo
    samples: the first frame after each reset gives none)."""
    with open(ST_VECTOR + ".bin", "rb") as f:
        frames = bytearray(f.read())
    frames[39 * ST_FRAME:39 * ST_FRAME + 2] = struct.pack(">H", 1)
    return bytes(frames)


def st_audio_pcm():
    """Sony's PCM (interleaved int16, 44.1 kHz stereo) for st_audio_frames()."""
    with open(ST_VECTOR + ".pcm", "rb") as f:
        return f.read()


def st_audio_wav():
    """The WAV OpenEVP writes for st_audio_frames(): the canonical 44-byte header and Sony's PCM."""
    pcm = st_audio_pcm()
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16, 1, 2, 44100,
                       176400, 4, 16, b"data", len(pcm)) + pcm


def make_st_raw(frames, counter=0xFFFFFFFF):
    """Wire data for one ICD-ST10 message holding the frame stream ``frames``,
    1014 payload bytes per block as the recorder sends them. Block bytes 0..1
    are the offset of the first frame that starts in the block (10 + the rest
    of a frame carried over from the block before), 2..3 the 10-byte header
    length, 4..5 the valid bytes, 6..9 the time counter (0xFFFFFFFF on an
    ST10 whose clock is not set)."""
    per = 1024 - 10
    spare = b"\xff\x03\x0c\x3c\xff\xff\xff\xff\x02\xc1\x44\x1c\x2c\xff\xff\xff"
    out = bytearray()
    for at in range(0, len(frames), per):
        chunk = frames[at:at + per]
        block = bytearray(1024)
        block[0:2] = struct.pack(">H", 10 + (-at) % ST_FRAME)
        block[2:4] = b"\x00\x0a"
        block[4:6] = struct.pack(">H", 10 + len(chunk))
        block[6:10] = struct.pack(">I", counter)
        block[10:10 + len(chunk)] = chunk
        out += block[:512] + spare + block[512:] + spare
    return bytes(out)


class FakeRecorderDevice:
    """Emulates the recorder's side of the protocol, enforcing the real policy.

    folders maps 1..5 (A..E) to make_table() message tuples; other folders are
    empty. GET_VOICE serves make_raw() for message N of the folder named in
    its opcode's low word (0x11FF0000 | folder), as the real recorder does
    (which table was read last does not matter), or voice[(folder, N)] if
    given. identify is the model name in the device-info reply ("ICD-ST10");
    by default the reply carries none.
    """
    CONTROL_BUF = 4096

    def __init__(self, folders=None, voice=None, identify=""):
        self.folders = folders or {}
        self.voice = voice or {}
        self.identify = identify
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
        elif policy.is_get_voice(op):
            folder, number = op & 0xFFFF, words[1] >> 16
            _slot, counter, _start, length, _date, _owner, *_mode = self.folders[folder][number - 1]
            self.voice_calls.append((folder, number))
            raw = self.voice.get((folder, number)) or make_raw(length, counter)
            size = struct.pack(">I", len(raw))
            self.queue = [self._msg(0x0F, op, bytes(12) + size), self._msg(0x09, done, bytes(12) + size)]
            self.bulk = raw
        elif op == policy.CMD_DEVICE_INFO and self.identify:
            body = bytearray(48)
            body[20:20 + len(self.identify)] = self.identify.encode("latin-1")
            self.queue = [self._msg(0x09, done, bytes(body))]
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


def made_wav(make=None, write=None):
    """The WAV bytes a fake AudioServer.prepare(key, make=..., write=...) is
    handed: make() returns them; write(f) decodes into a file (the real
    server's streamed form), here an in-memory one."""
    if write is None:
        return make()
    import io
    f = io.BytesIO()
    write(f)
    return f.getvalue()
