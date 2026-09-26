"""Build Sony .dvf files the way Digital Voice Editor 2.31 saves ICD-ST recordings.

Layout (reverse-engineered by comparing DVE's saved files with raw downloads):
  bytes 0..511     header (fixed template + per-message fields below)
  bytes 512..1023  0xFF
  bytes 1024..     audio: one 1024-byte block per 1056-byte block on the wire
                   (each wire block = 2 NAND pages of 512 data + 16 spare bytes;
                   the spare bytes are dropped)

Per-message header fields (big-endian):
  52..59    recording date/time, copied verbatim from the message entry
            (yyyy yyyy mm dd hh mi ss weekday; all 0xFF when the recorder had no date).
            Deliberately verbatim even when implausible (clock never set, garbage):
            DVE does the same, and rewriting it would destroy the only record of
            what the recorder stored. Only the file name treats such a date as
            undated (folder.plausible_date).
  152       0x04 tag, then 153..155 = padding bytes in the last block
  156..159  audio length in the file (blocks * 1024), u32
  434..463  owner name from the message entry, NUL padded
  464..467  audio payload bytes (sum of valid bytes minus the 10-byte block headers), u32

Audio block header: bytes 0..1 frame offset, 2..3 0x000A (every LP block seen),
4..5 valid bytes in the block (0x0400 when full), 6..9 time counter.
DVE fills the unused tail of the last block with 0xFF (the recorder sends 0x00).

Only LP-mode recordings have been verified. DVE also rounds two timestamps
(header byte 58, byte 9 of some blocks) differently; that does not change the
audio: DVE converts these files to WAV byte-identically to its own.
"""
import re
import struct

BLOCK = 1024
WIRE_BLOCK = 1056
PAGE, PAGE_DATA = 528, 512
BLOCK_HEADER = 10
LP_MARKER = b"\x00\x0a"

# DVE 2.31 header for ST-series LPEC (LP) recordings with the per-message fields zeroed.
_TEMPLATE = bytes.fromhex(
    "4d535f564f4943450000005001010000534f4e5920434f52504f524154494f4e"
    "5354566f6963652e646c6c0000000000010100000000000000000000002c0001"
    "00001770000002ee040000010000000001000000000000500200000000000050"
    "0500000000000100060000000000001007000000000000200900000000000010"
    "0a000000000000100b0000000000001003000000000002000400000000000000"
    "9000000000000000000000000000000000000000000000000000000000000000"
) + bytes(416 - 192) + bytes.fromhex(
    "0100534f4e5920434f52502e202020200300"
) + bytes(480 - 434) + b"\xff\xff\xff\xff" + bytes(512 - 484)
assert len(_TEMPLATE) == 512


class FormatError(ValueError):
    pass


def strip_spare(raw):
    """Wire blocks (2 x [512 data + 16 spare]) -> 1024-byte blocks."""
    if not raw or len(raw) % WIRE_BLOCK:
        raise FormatError(f"raw length {len(raw)} is not a positive multiple of {WIRE_BLOCK}")
    return bytearray(b"".join(raw[i:i + PAGE_DATA] for i in range(0, len(raw), PAGE)))


def build(raw, entry_date, owner_name, expected_length=None):
    """Return the complete .dvf file for one message's raw wire data.

    expected_length (valid bytes from the folder table) is checked against the
    block headers, so truncated or mismatched data is rejected.
    """
    audio = strip_spare(raw)
    blocks = len(audio) // BLOCK
    valid = []
    for k in range(blocks):
        b = audio[k * BLOCK:(k + 1) * BLOCK]
        if b[2:4] != LP_MARKER:
            raise FormatError(f"block {k} is not an LP-mode block (marker {b[2:4].hex()}); "
                              "only LP recordings are supported so far")
        v = struct.unpack(">H", b[4:6])[0]
        if not BLOCK_HEADER <= v <= BLOCK:
            raise FormatError(f"block {k}: implausible valid length {v}")
        if k < blocks - 1 and v != BLOCK:
            raise FormatError(f"block {k} is only partly filled ({v} bytes) but is not the last block")
        valid.append(v)
    if expected_length is not None and sum(valid) != expected_length:
        raise FormatError(f"data holds {sum(valid)} bytes but the folder table says {expected_length}")
    last = valid[-1]
    tail = (blocks - 1) * BLOCK
    audio[tail + last:tail + BLOCK] = b"\xff" * (BLOCK - last)

    h = bytearray(_TEMPLATE)
    if len(entry_date) != 8:
        raise FormatError("entry date must be 8 bytes")
    h[52:60] = entry_date
    h[153:156] = (BLOCK - last).to_bytes(3, "big")
    h[156:160] = struct.pack(">I", blocks * BLOCK)
    name = owner_name.encode("latin-1", "replace")[:30]
    h[434:464] = name + bytes(30 - len(name))
    h[464:468] = struct.pack(">I", sum(valid) - BLOCK_HEADER * blocks)
    return bytes(h) + b"\xff" * 512 + bytes(audio)


# Header bytes that differ per recording; everything else must equal the template.
_PER_MESSAGE = set(range(52, 60)) | set(range(153, 160)) | set(range(434, 468))


def validate(dvf_bytes):
    """Return None if dvf_bytes is a complete, self-consistent ST25 LP .dvf file
    (as written by this tool or by Digital Voice Editor), else the reason."""
    n = len(dvf_bytes)
    if n < 1024 + BLOCK or (n - 1024) % BLOCK:
        return "wrong size"
    if any(dvf_bytes[i] != _TEMPLATE[i] for i in range(512) if i not in _PER_MESSAGE):
        return "header does not match the ST25 LP layout"
    if dvf_bytes[512:1024] != b"\xff" * 512:
        return "damaged header padding"
    blocks = (n - 1024) // BLOCK
    if struct.unpack(">I", dvf_bytes[156:160])[0] != blocks * BLOCK:
        return "audio length field does not match the file size"
    valid = []
    for k in range(blocks):
        b = dvf_bytes[1024 + k * BLOCK:1024 + (k + 1) * BLOCK]
        v = struct.unpack(">H", b[4:6])[0]
        if b[2:4] != LP_MARKER or not BLOCK_HEADER <= v <= BLOCK or (k < blocks - 1 and v != BLOCK):
            return f"damaged audio block {k}"
        valid.append(v)
    if int.from_bytes(dvf_bytes[153:156], "big") != BLOCK - valid[-1]:
        return "padding field does not match the last block"
    if struct.unpack(">I", dvf_bytes[464:468])[0] != sum(valid) - BLOCK_HEADER * blocks:
        return "payload field does not match the audio blocks"
    return None


def payload(dvf_bytes):
    """The frame stream stored in a .dvf's audio blocks: each block's payload
    bytes (the 10-byte header stripped, only the ``valid`` bytes kept),
    concatenated in block order. The inverse of tools/make_test_dvf.pack's
    concatenation, and what a decoder feeds to decode_payload."""
    audio = dvf_bytes[1024:]
    out = bytearray()
    for k in range(len(audio) // BLOCK):
        b = audio[k * BLOCK:(k + 1) * BLOCK]
        out += b[BLOCK_HEADER:struct.unpack(">H", b[4:6])[0]]
    return bytes(out)


def audio_fingerprint(dvf_bytes):
    """SHA-256 of a .dvf file's audio, ignoring the per-block time counters.

    Digital Voice Editor rewrites those counters (by +1 or +2) in its own files,
    and undated recordings carry 0xFFFFFFFF, so counters cannot identify a
    recording; the audio itself can. Returns None unless the file passes
    validate(), so a damaged file never counts as "already saved".
    """
    if validate(dvf_bytes) is not None:
        return None
    audio = bytearray(dvf_bytes[1024:])
    for k in range(0, len(audio), BLOCK):
        audio[k + 6:k + 10] = b"\0\0\0\0"
    import hashlib
    return hashlib.sha256(audio).hexdigest()


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_component(text, fallback="Unknown"):
    """Make device-provided text safe as one Windows file-name component."""
    s = _UNSAFE.sub("_", text).strip().rstrip(". ")
    s = s[:40]
    if not s or s.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?i)(COM|LPT)\d", s):
        return fallback
    return s


def filename(folder_letter, number, owner, entry_date, dated):
    """DVE's naming: 001_A_007_<owner>_<yyyy_mm_dd>.dvf (no date part if undated)."""
    base = f"001_{folder_letter}_{number:03d}_{safe_component(owner)}"
    if dated:
        y = struct.unpack(">H", entry_date[0:2])[0]
        base += f"_{y:04d}_{entry_date[2]:02d}_{entry_date[3]:02d}"
    return base + ".dvf"
