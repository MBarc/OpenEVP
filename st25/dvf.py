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

Audio block header: bytes 0..1 offset of the first frame that starts in the
block, 2..3 the header's own length 0x000A (every block of every mode seen; it
says nothing about the mode), 4..5 valid bytes in the block (0x0400 when
full), 6..9 time counter.
DVE fills the unused tail of the last block with 0xFF (the recorder sends 0x00).

Two recording modes, told apart by the low byte of the folder table's
message-list entry (st25.folder) and stored in the header's codec fields:

  mode  recorder   codec (byte 61)  audio
  0x00  ICD-ST25   0x2C LPEC LP     8 kHz mono, 750 bytes/s (6000 bit/s)
  0x6C  ICD-ST10   0x24 LPEC ST     44.1 kHz stereo, 283-byte frames (a 16-bit
                                    counter, a flags byte, 280 codec bytes) of
                                    2048 samples per channel

LP files have been verified against DVE. DVE also rounds two timestamps
(header byte 58, byte 9 of some blocks) differently; that does not change the
audio: DVE converts these files to WAV byte-identically to its own.

The LPEC ST header is OpenEVP's own until a file DVE saved from an ICD-ST10
can be compared: the LP template with codec 0x24, 2 channels (bytes 62..63),
48234 bit/s (64..67) and 6029 bytes/s (68..71, the codec bytes: 280 per 2048
samples at 44.1 kHz). validate() accepts it for good, so files saved with it
keep counting as "already saved". Its frames are checked: each block's frame
offset, whole frames, and counters that go up by one or restart at 0 (the
recorder starts a segment with a counter-0 frame).
"""
import re
import struct

BLOCK = 1024
WIRE_BLOCK = 1056
PAGE, PAGE_DATA = 528, 512
BLOCK_HEADER = 10
HEADER_LENGTH_FIELD = struct.pack(">H", BLOCK_HEADER)   # bytes 2..3 of every audio block

MODE_LP, MODE_ST = 0x00, 0x6C       # the folder table's mode byte (st25.folder.Message.mode)
MODES = {MODE_LP: "LP", MODE_ST: "LPEC ST"}
CODEC_AT = 61                        # header byte: the codec
CODEC_LP, CODEC_ST = 0x2C, 0x24
_CODECS = {MODE_LP: CODEC_LP, MODE_ST: CODEC_ST}

LP_BYTES_PER_SECOND = 750
ST_FRAME, ST_SAMPLES, ST_RATE = 283, 2048, 44100
ST_NOT_PLAYABLE = "LPEC ST (ICD-ST10) audio can't be played yet"   # no LPEC ST decoder yet

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


def _st_template():
    """The LP template with the LPEC ST codec fields (see the module docstring)."""
    h = bytearray(_TEMPLATE)
    h[CODEC_AT] = CODEC_ST
    h[62:64] = struct.pack(">H", 2)
    h[64:72] = struct.pack(">II", 48234, 6029)
    return bytes(h)


_TEMPLATES = {MODE_LP: _TEMPLATE, MODE_ST: _st_template()}


class FormatError(ValueError):
    pass


def codec(dvf_bytes):
    """The codec byte of a .dvf header (CODEC_LP, CODEC_ST...), or None if the
    data is too short or is not a Sony voice file. Reads only the header, so
    the first 512 bytes of a file are enough."""
    if len(dvf_bytes) <= CODEC_AT or bytes(dvf_bytes[:8]) != b"MS_VOICE":
        return None
    return dvf_bytes[CODEC_AT]


def mode_of(dvf_bytes):
    """The recording mode (MODE_LP, MODE_ST) a .dvf header names, or None."""
    c = codec(dvf_bytes)
    return next((m for m, k in _CODECS.items() if k == c), None)


def seconds(payload_bytes, mode):
    """The length of ``payload_bytes`` of audio (the blocks' valid bytes minus
    their headers) in ``mode``, or None for an unknown mode. For LPEC ST it
    counts every frame, the counter-0 segment frames too (a slight
    overstatement)."""
    if mode == MODE_LP:
        return payload_bytes / float(LP_BYTES_PER_SECOND)
    if mode == MODE_ST:
        return payload_bytes / ST_FRAME * ST_SAMPLES / ST_RATE
    return None


def _st_problem(blocks):
    """Why the audio blocks [(block bytes, valid)] are not an LPEC ST frame
    stream, or None: each block's frame offset must point at the next frame
    start, the stream must end on a whole frame, and each frame counter must
    follow the one before or restart at 0."""
    at = 0
    stream = bytearray()
    for k, (b, v) in enumerate(blocks):
        want = BLOCK_HEADER + (-at) % ST_FRAME
        if struct.unpack(">H", b[0:2])[0] != want:
            return f"block {k} is not LPEC ST data (frame offset {struct.unpack('>H', b[0:2])[0]}, expected {want})"
        stream += b[BLOCK_HEADER:v]
        at += v - BLOCK_HEADER
    if not stream or len(stream) % ST_FRAME:
        return f"the LPEC ST data ({len(stream)} bytes) is not a whole number of {ST_FRAME}-byte frames"
    prev = None
    for i in range(0, len(stream), ST_FRAME):
        c = struct.unpack(">H", stream[i:i + 2])[0]
        if prev is not None and c != 0 and c != (prev + 1) & 0xFFFF:
            return f"LPEC ST frame {i // ST_FRAME} has counter {c} after {prev}"
        prev = c
    return None


def strip_spare(raw):
    """Wire blocks (2 x [512 data + 16 spare]) -> 1024-byte blocks."""
    if not raw or len(raw) % WIRE_BLOCK:
        raise FormatError(f"raw length {len(raw)} is not a positive multiple of {WIRE_BLOCK}")
    return bytearray(b"".join(raw[i:i + PAGE_DATA] for i in range(0, len(raw), PAGE)))


def build(raw, entry_date, owner_name, expected_length=None, mode=MODE_LP):
    """Return the complete .dvf file for one message's raw wire data.

    expected_length (valid bytes from the folder table) is checked against the
    block headers, so truncated or mismatched data is rejected. mode is the
    folder table's mode byte; LPEC ST data must also be whole, consecutive
    frames (see the module docstring), so a table that names the wrong mode
    stops the download instead of producing a mislabelled file.
    """
    if mode not in _TEMPLATES:
        raise FormatError(f"unknown recording mode 0x{mode:02x}")
    audio = strip_spare(raw)
    blocks = len(audio) // BLOCK
    valid = []
    for k in range(blocks):
        b = audio[k * BLOCK:(k + 1) * BLOCK]
        if b[2:4] != HEADER_LENGTH_FIELD:
            raise FormatError(f"block {k} has an unknown block header (header length {b[2:4].hex()})")
        v = struct.unpack(">H", b[4:6])[0]
        if not BLOCK_HEADER <= v <= BLOCK:
            raise FormatError(f"block {k}: implausible valid length {v}")
        if k < blocks - 1 and v != BLOCK:
            raise FormatError(f"block {k} is only partly filled ({v} bytes) but is not the last block")
        valid.append(v)
    if expected_length is not None and sum(valid) != expected_length:
        raise FormatError(f"data holds {sum(valid)} bytes but the folder table says {expected_length}")
    if mode == MODE_ST:
        problem = _st_problem([(audio[k * BLOCK:(k + 1) * BLOCK], valid[k]) for k in range(blocks)])
        if problem:
            raise FormatError(problem)
    last = valid[-1]
    tail = (blocks - 1) * BLOCK
    audio[tail + last:tail + BLOCK] = b"\xff" * (BLOCK - last)

    h = bytearray(_TEMPLATES[mode])
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
    """Return None if dvf_bytes is a complete, self-consistent ICD-ST .dvf file
    (LP as written by this tool or by Digital Voice Editor, or LPEC ST as
    written by this tool), else the reason. codec() says which it is."""
    n = len(dvf_bytes)
    if n < 1024 + BLOCK or (n - 1024) % BLOCK:
        return "wrong size"
    mode = mode_of(dvf_bytes)
    template = _TEMPLATES.get(mode)
    if template is None or any(dvf_bytes[i] != template[i] for i in range(512) if i not in _PER_MESSAGE):
        return "header does not match the ICD-ST LP or LPEC ST layout"
    if dvf_bytes[512:1024] != b"\xff" * 512:
        return "damaged header padding"
    blocks = (n - 1024) // BLOCK
    if struct.unpack(">I", dvf_bytes[156:160])[0] != blocks * BLOCK:
        return "audio length field does not match the file size"
    valid = []
    for k in range(blocks):
        b = dvf_bytes[1024 + k * BLOCK:1024 + (k + 1) * BLOCK]
        v = struct.unpack(">H", b[4:6])[0]
        if b[2:4] != HEADER_LENGTH_FIELD or not BLOCK_HEADER <= v <= BLOCK or (k < blocks - 1 and v != BLOCK):
            return f"damaged audio block {k}"
        valid.append(v)
    if int.from_bytes(dvf_bytes[153:156], "big") != BLOCK - valid[-1]:
        return "padding field does not match the last block"
    if struct.unpack(">I", dvf_bytes[464:468])[0] != sum(valid) - BLOCK_HEADER * blocks:
        return "payload field does not match the audio blocks"
    if mode == MODE_ST:
        return _st_problem([(dvf_bytes[1024 + k * BLOCK:1024 + (k + 1) * BLOCK], valid[k]) for k in range(blocks)])
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


def audio_matches(existing_bytes, fingerprint):
    """Whether a .dvf already on disk holds the recording whose
    audio_fingerprint() is ``fingerprint``: THE .dvf "already saved" test,
    used by st25.export and openevp.formats. Frozen semantics: a plain
    equality of fingerprints (so a damaged file, fingerprint None, matches
    another damaged file)."""
    return audio_fingerprint(existing_bytes) == fingerprint


def same_audio(existing_bytes, new_bytes):
    """audio_matches() for two .dvf files."""
    return audio_matches(existing_bytes, audio_fingerprint(new_bytes))


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
