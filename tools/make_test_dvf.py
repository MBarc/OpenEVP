#!/usr/bin/env python3
"""Pack an LPEC frame stream into an ICD-ST25 LP .dvf, laid out as the recorder does.

Used to build the decoder's committed test vectors from synthetic audio (no real
recording is ever committed). The frames themselves come from an LPEC encoder; this module only
does the container.

Block layout (1024 bytes: a 10-byte header, then 1014 payload bytes):
  0..1  offset of the first frame that starts in this block, counted from the
        start of the block (10 when a frame starts right after the header);
        bytes before it continue the previous block's last frame
  2..3  0x000A (LP)
  4..5  valid bytes in the block, header included (1024 when full)
  6..9  time counter
Frames are 36, 48 or 60 bytes long and run on across block boundaries.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from st25 import dvf  # noqa: E402

PAYLOAD = dvf.BLOCK - dvf.BLOCK_HEADER                    # 1014
SPARE = bytes.fromhex("ff030c3cffffffff02c1441c2cffffff")  # as seen on the wire
DATE = bytes.fromhex("07ed051713360403")                   # same test date as tests/fixtures.py


def frame_offsets(sizes):
    """Per block, the header value of bytes 0..1 for frames of these sizes."""
    total = sum(sizes)
    blocks = -(-total // PAYLOAD)
    starts, pos = [], 0
    for n in sizes:
        starts.append(pos)
        pos += n
    offsets, i = [], 0
    for k in range(blocks):
        lo = k * PAYLOAD
        while i < len(starts) and starts[i] < lo:
            i += 1
        if i == len(starts) or starts[i] >= lo + PAYLOAD:
            raise ValueError(f"no frame starts in block {k}")
        offsets.append(dvf.BLOCK_HEADER + starts[i] - lo)
    return offsets


def pack(frames, counter=100, date=DATE, owner="Test"):
    """A valid .dvf holding the frames (a list of bytes objects) back to back."""
    stream = b"".join(frames)
    offsets = frame_offsets([len(f) for f in frames])
    raw = bytearray()
    length = 0
    for k, off in enumerate(offsets):
        chunk = stream[k * PAYLOAD:(k + 1) * PAYLOAD]
        block = bytearray(dvf.BLOCK)
        block[0:2] = struct.pack(">H", off)
        block[2:4] = dvf.LP_MARKER
        block[4:6] = struct.pack(">H", dvf.BLOCK_HEADER + len(chunk))
        block[6:10] = struct.pack(">I", counter + k)
        block[10:10 + len(chunk)] = chunk
        raw += block[:512] + SPARE + block[512:] + SPARE
        length += dvf.BLOCK_HEADER + len(chunk)
    return dvf.build(bytes(raw), date, owner, expected_length=length)


def payload(dvf_bytes):
    """The frame stream stored in a .dvf (the inverse of pack's concatenation)."""
    return dvf.payload(dvf_bytes)
