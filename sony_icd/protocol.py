"""Sony ICD-ST USB protocol, ICD-ST25 and ICD-ST10 (reverse-engineered from USB
captures of Digital Voice Editor 2.31 and disassembly of icdcomm2.dll / IcdUsb2.dll).

Transport (vendor requests, recipient interface, wValue 0xABAB, wIndex 0):
  status  control IN  bRequest 0x01, 4 bytes  -> b0 b1 LEN(2, big-endian)
            00 00 ....   idle, ready for a command
            0f 81 LLLL   a reply of LLLL bytes is waiting
            0f 01 ....   busy
  command control OUT bRequest 0x80, 24 or 32-byte frame
  reply   control IN  bRequest 0x81, LLLL bytes
  data    bulk IN     endpoint 0x81

Replies echo the command's opcode: the acknowledgement of a bulk command echoes
it unchanged; the final reply replaces its 0xFF byte with 0x00
(091001ff -> 09100100, 11ff0002 -> 11000002).

Safety: the only thing ever sent to the recorder is a command frame, and the
USB layer itself (usb.Device, enforcing sony_icd/policy.py) refuses any control OUT that
is not one of the exact frames Digital Voice Editor sends to list and download
recordings - prefix, opcode, frame length and arguments are all checked there,
so even direct calls to Recorder.dev cannot send anything else. _send() checks
the same rules earlier to give a clearer error. The recorder's command set also
contains erase/delete/firmware operations; nothing here can send them.

Timing: the recorder abandons a transaction if the host is slow to take the
next step (tens of ms), after which it answers 0f 01 until its USB cable is
replugged. Each transaction runs back-to-back on one thread; buffers are
allocated before it starts and nothing is printed or written to disk inside it.
"""
import struct
import time

from .usb import Device, LIBUSB_ERROR_TIMEOUT

from .policy import (BLOCK_RAW, CMD_DEVICE_INFO, CMD_FOLDER_INFO, CMD_GET_VOICE_BASE,  # noqa: F401
                     CMD_INFO_03, CMD_READ_BLOCK, CMD_TARGET_STATUS, EP_BULK_IN, FRAME_PREFIX,
                     MAX_BLOCKS, REQ_COMMAND, REQ_REPLY, REQ_STATUS, REQTYPE_IN, REQTYPE_OUT,
                     WVALUE, frame_ok, get_voice_opcode, is_get_voice)
from .policy import args_ok as _args_ok

VID, PID = 0x054C, 0x0103
STATUS_TIMEOUT_S = 6.0        # same limit as icdcomm2.dll GetBuffInfo
CONTROL_TIMEOUT_MS = 3000     # IcdUsb2 control timeout
# Bulk inactivity limit. Each read may wait only for what is left of this
# budget since the last byte-carrying read returned, so empty reads (with or
# without a timeout) never extend it. A read that returns data at its timeout
# counts as progress; libusb cannot say when inside that read the last byte
# came, so a stall is reported at most 2 x BULK_STALL_MS after the last byte.
# Shorter reads would tighten that, but each timeout cancels a USB transfer,
# and a healthy recorder never hits one.
BULK_STALL_MS = 5000
FOLDER_TABLE_SIZE = 137 * 528


# [bytes received, bytes expected] of the bulk transfer running or last run.
# The transfer loop only stores into it (the recorder drops a transaction if
# the next read is late), and another thread may read it for a progress bar;
# all recorder I/O runs on one thread, so there is one transfer at a time.
TRANSFER = [0, 0]

class RecorderError(Exception):
    pass


class RecorderStuck(RecorderError):
    """The recorder stopped mid-transaction; only a cable replug recovers it."""


def _completion_opcode(op):
    """Replace the command's 0xFF byte with 0x00 (see module docstring)."""
    b = bytearray(struct.pack(">I", op))
    return struct.unpack(">I", bytes(0 if x == 0xFF else x for x in b))[0]


class Recorder:
    def __init__(self, device_id=None):
        self.dev = Device(VID, PID, device_id)

    def close(self):
        self.dev.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- transport -------------------------------------------------------
    def status(self):
        s = self.dev.control_in(REQTYPE_IN, REQ_STATUS, WVALUE, 0, 4, CONTROL_TIMEOUT_MS)
        if len(s) != 4:
            raise RecorderError(f"short status {s.hex()}")
        return s

    def _wait(self, want_reply):
        deadline = time.monotonic() + STATUS_TIMEOUT_S
        while True:
            s = self.status()
            if want_reply and s[0] == 0x0F and s[1] == 0x81:
                n = struct.unpack(">H", s[2:4])[0]
                if not 16 <= n <= self.dev.CONTROL_BUF:
                    raise RecorderError(f"implausible reply length {n}")
                return n
            if not want_reply and s[0] == 0x00 and s[1] == 0x00:
                return 0
            if time.monotonic() > deadline:
                if s[:2] == b"\x0f\x01":
                    raise RecorderStuck(f"recorder stays busy (status {s.hex()})")
                raise RecorderError(f"recorder not {'replying' if want_reply else 'ready'} (status {s.hex()})")

    def _send(self, words):
        if not _args_ok(words):
            raise RecorderError("refusing to send a command that Digital Voice Editor does not send: "
                                + " ".join(f"{w:08x}" for w in words))
        frame = FRAME_PREFIX + b"".join(struct.pack(">I", w) for w in words)
        if len(frame) != (32 if is_get_voice(words[0]) else 24):
            raise RecorderError("unexpected frame length")
        self._wait(want_reply=False)
        self.dev.control_out(REQTYPE_OUT, REQ_COMMAND, WVALUE, 0, frame, CONTROL_TIMEOUT_MS)

    def _reply(self, first, opcode, min_len):
        n = self._wait(want_reply=True)
        r = self.dev.control_in(REQTYPE_IN, REQ_REPLY, WVALUE, 0, n, CONTROL_TIMEOUT_MS)
        if len(r) != n or n < min_len:
            raise RecorderError(f"reply length {len(r)} (announced {n}, need {min_len})")
        if r[:12] != bytes([first]) + FRAME_PREFIX[1:12]:
            raise RecorderError(f"unexpected reply header {r[:12].hex()}")
        got = struct.unpack(">I", r[12:16])[0]
        if got != opcode:
            raise RecorderError(f"reply is for 0x{got:08x}, expected 0x{opcode:08x}")
        return r

    def query(self, words, min_len):
        """Command with a single reply (no bulk phase)."""
        self._send(words)
        return self._reply(0x09, _completion_opcode(words[0]), min_len)

    def query_bulk(self, words, size_index, expected_size):
        """Command answered by ack reply -> bulk data -> completion reply.

        The whole transaction runs without printing or disk I/O; the data
        buffer is allocated before the command is sent - but only after the
        command (and so the size) has passed the allow-list, which bounds it.
        """
        if not _args_ok(words):
            raise RecorderError("refusing to send a command that Digital Voice Editor does not send: "
                                + " ".join(f"{w:08x}" for w in words))
        allowed = words[4] if is_get_voice(words[0]) else FOLDER_TABLE_SIZE
        if expected_size != allowed or not 0 < expected_size <= MAX_BLOCKS * BLOCK_RAW:
            raise RecorderError(f"refusing implausible transfer size {expected_size}")
        data = bytearray(expected_size)
        TRANSFER[:] = [0, expected_size]
        self._send(words)
        ack = self._reply(0x0F, words[0], size_index + 4)
        size = struct.unpack(">I", ack[size_index:size_index + 4])[0]
        if size != expected_size:
            raise RecorderError(f"recorder announced {size} bytes, expected {expected_size}")
        got = 0
        last_progress = time.monotonic()
        while got < size:
            left_ms = BULK_STALL_MS - int((time.monotonic() - last_progress) * 1000)
            if left_ms <= 0:
                raise RecorderStuck(f"data stopped after {got} of {size} bytes")
            n, rc = self.dev.bulk_in_into(EP_BULK_IN, data, got, size - got, left_ms)
            got += n
            TRANSFER[0] = got
            if n:
                last_progress = time.monotonic()
            elif rc == LIBUSB_ERROR_TIMEOUT:
                raise RecorderStuck(f"data stopped after {got} of {size} bytes")
        done = self._reply(0x09, _completion_opcode(words[0]), size_index + 4)
        if done[16:size_index + 4] != ack[16:size_index + 4]:
            raise RecorderError("final reply does not match the acknowledgement")
        return ack, bytes(data), done

    # ---- operations DVE performs ------------------------------------------
    def device_info(self):
        return self.query([CMD_DEVICE_INFO, 0, 0], 52)

    def read_block(self, length, offset):
        return self.query([CMD_READ_BLOCK, length, offset], 24)

    def info_03(self):
        return self.query([CMD_INFO_03, 0, 0], 24)

    def target_status(self):
        return self.query([CMD_TARGET_STATUS, 0, 0], 24)

    def folder_table(self, folder):
        """Raw message table for folder 1..5 (A..E)."""
        if not 1 <= folder <= 5:
            raise ValueError("folder must be 1..5")
        _, table, _ = self.query_bulk([CMD_FOLDER_INFO | (folder << 8), 0, 0], 16, FOLDER_TABLE_SIZE)
        return table

    def voice_data(self, folder, msg, blocks):
        """Raw wire data of message `msg` (1-based) in folder 1..5 (A..E).

        The folder travels in the opcode's low 16 bits, as Digital Voice
        Editor sends it; reading a folder's table does not select the folder.
        """
        size = blocks * BLOCK_RAW
        _, raw, _ = self.query_bulk([get_voice_opcode(folder), msg << 16, 1, blocks, size], 28, size)
        return raw
