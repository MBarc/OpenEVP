"""The audio fingerprint of a WAV, and EVP marks stored inside WAV files.

The fingerprint covers the decoded samples and their format, not the file: a
.dvf decoded by OpenEVP, the WAV OpenEVP exports from it, and the WAV Digital
Voice Editor converts from it all have the same samples, so the same
fingerprint (and therefore the same EVP marks). This is exact matching only:
any resampling, gain change or bit-depth change is a different recording, on
purpose -- there is no fuzzy matching here. WAVE_FORMAT_EXTENSIBLE PCM (which
Python's own ``wave`` module accepts) fingerprints the same as plain PCM: the
prefix is built from channels/width/rate, not the format tag, and both are
still the same PCM samples in the same layout.

Marks are written as standard RIFF markers (a 'cue ' chunk plus a LIST/adtl
chunk with 'labl' texts and 'ltxt' region lengths), which audio editors such
as Audition and Ocenaudio show.
"""
import hashlib
import struct
import wave

CHUNK_FRAMES = 1 << 18                  # ~1 MiB of 16-bit mono per read

# with_markers() refuses to build a WAV bigger than this (RIFF's own 32-bit
# size field cannot express more than 4 GiB - 1 anyway).
_MAX_OUTPUT_SIZE = (1 << 32) - 1


def fingerprint_prefix(nchannels, sampwidth, framerate):
    """The versioned header hashed ahead of the raw PCM samples. The version
    (``pcm1``) lets a future change to what is fingerprinted (or how) produce
    values that can never collide with today's."""
    return b"pcm1:%d:%d:%d\n" % (nchannels, sampwidth, framerate)


def fingerprint_stream(nchannels, sampwidth, framerate, chunks):
    """Hex SHA-256 over fingerprint_prefix(...) followed by chunks (raw PCM
    bytes, in the order they appear in the data chunk)."""
    h = hashlib.sha256(fingerprint_prefix(nchannels, sampwidth, framerate))
    for c in chunks:
        h.update(c)
    return h.hexdigest()


def wav_fingerprint(f):
    """The audio fingerprint of a PCM WAV. ``f`` is a path or a binary file
    object. Reads the data chunk in CHUNK_FRAMES-frame pieces through the
    ``wave`` module -- never the whole file at once.

    Raises ValueError for anything that is not a readable PCM WAV, including
    one whose data chunk is shorter than its header claims (a truncated
    file): fingerprinting a partial recording as if it were the recording
    would be worse than refusing it.
    """
    try:
        w = wave.open(f)
    except (wave.Error, EOFError) as e:
        raise ValueError(f"not a PCM WAV file ({e})") from None
    with w:
        nchannels = w.getnchannels()
        sampwidth = w.getsampwidth()
        framerate = w.getframerate()
        expected = w.getnframes() * nchannels * sampwidth
        total = 0

        def chunks():
            nonlocal total
            while True:
                data = w.readframes(CHUNK_FRAMES)
                if not data:
                    return
                total += len(data)
                yield data

        fp = fingerprint_stream(nchannels, sampwidth, framerate, chunks())
        if total != expected:
            raise ValueError("the WAV file is truncated")
        return fp


def _as_file(f):
    """(file object, should we close it) for f, a path or an open binary file."""
    if hasattr(f, "read"):
        return f, False
    return open(f, "rb"), True


def read_markers(f):
    """EVP marks embedded in a WAV as standard RIFF cue/LIST markers, sorted
    by start: ``[{"start": seconds, "end": seconds, "note": str}, ...]``. A
    cue point with no matching 'ltxt' region is a point mark (``end ==
    start``).

    ``f`` is a path or an open binary file object. This only seeks through
    chunk headers -- the data chunk (which can be huge) is skipped over with
    a seek and never read. Anything unexpected -- not a WAV, a garbled or
    lying chunk size, a corrupt cue/LIST chunk -- is treated as "no markers
    found"; this never raises.
    """
    fobj, opened = _as_file(f)
    try:
        return _read_markers(fobj)
    except (OSError, struct.error):
        return []
    finally:
        if opened:
            fobj.close()


def _read_markers(fobj):
    header = fobj.read(12)
    if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        return []
    riff_end = 8 + struct.unpack_from("<I", header, 4)[0]

    rate = None
    points, labels, lengths = {}, {}, {}
    pos = 12
    while pos + 8 <= riff_end:
        fobj.seek(pos)
        chdr = fobj.read(8)
        if len(chdr) < 8:
            break
        cid, size = chdr[:4], struct.unpack_from("<I", chdr, 4)[0]
        payload, payload_end = pos + 8, pos + 8 + size
        if payload_end > riff_end:
            break                                  # chunk claims to run past the RIFF end: stop here
        if cid == b"fmt " and size >= 16:
            fobj.seek(payload)
            body = fobj.read(16)
            if len(body) == 16:
                rate = struct.unpack_from("<I", body, 4)[0]
        elif cid == b"cue " and size >= 4:
            fobj.seek(payload)
            body = fobj.read(size)
            if len(body) == size:
                n = struct.unpack_from("<I", body, 0)[0]
                for i in range(min(n, (len(body) - 4) // 24)):
                    cue_id, _pos, _fcc, _cs, _bs, sample = struct.unpack_from("<II4sIII", body, 4 + 24 * i)
                    points[cue_id] = sample
        elif cid == b"LIST" and size >= 4:
            fobj.seek(payload)
            listtype = fobj.read(4)
            if listtype == b"adtl":
                _read_adtl(fobj, payload + 4, min(payload_end, riff_end), labels, lengths)
        # 'data' and anything else: never read, just skip past it below
        pos = payload + size + (size & 1)

    if rate is None:
        return []
    marks = []
    for cue_id in sorted(points, key=lambda k: points[k]):
        start = points[cue_id]
        marks.append({"start": start / rate, "end": (start + lengths.get(cue_id, 0)) / rate,
                      "note": labels.get(cue_id, "")})
    return marks


def _read_adtl(fobj, start, end, labels, lengths):
    """Read 'labl'/'ltxt' sub-chunks of a LIST/adtl chunk spanning [start, end)
    into labels/lengths, bounded by end (the LIST's own declared size, further
    bounded by the RIFF size)."""
    pos = start
    while pos + 8 <= end:
        fobj.seek(pos)
        shdr = fobj.read(8)
        if len(shdr) < 8:
            return
        sid, ssize = shdr[:4], struct.unpack_from("<I", shdr, 4)[0]
        sbody, sbody_end = pos + 8, pos + 8 + ssize
        if sbody_end > end:
            return                                 # sub-chunk claims to run past the LIST/RIFF bound
        fobj.seek(sbody)
        body = fobj.read(ssize)
        if len(body) != ssize:
            return
        if sid == b"labl" and len(body) >= 4:
            labels[struct.unpack_from("<I", body, 0)[0]] = body[4:].split(b"\0", 1)[0].decode("utf-8", "replace")
        elif sid == b"ltxt" and len(body) >= 8:
            cue_id, length = struct.unpack_from("<II", body, 0)
            lengths[cue_id] = length
        pos = sbody + ssize + (ssize & 1)


def _sub(cid, body):
    """A complete sub-chunk (4-byte id + 4-byte size + body + pad byte)."""
    return cid + struct.pack("<I", len(body)) + body + (b"\0" if len(body) & 1 else b"")


def _strict_chunks(buf):
    """Top-level RIFF/WAVE chunks of buf, as (id, payload offset, size).

    Strict: raises ValueError the moment the framing is inconsistent in any
    way. This is only ever used on WAV bytes with_markers() receives fresh
    out of OpenEVP's own decoder -- never on an arbitrary WAV a user or
    another tool produced.
    """
    if len(buf) < 12 or bytes(buf[0:4]) != b"RIFF" or bytes(buf[8:12]) != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    riff_size = struct.unpack_from("<I", buf, 4)[0]
    if riff_size != len(buf) - 8:
        raise ValueError("RIFF chunk size does not match the data given")
    out, pos = [], 12
    while pos < len(buf):
        if pos + 8 > len(buf):
            raise ValueError("truncated chunk header")
        cid = bytes(buf[pos:pos + 4])
        size = struct.unpack_from("<I", buf, pos + 4)[0]
        payload = pos + 8
        if payload + size > len(buf):
            raise ValueError(f"{cid!r} chunk runs past the end of the file")
        out.append((cid, payload, size))
        pos = payload + size + (size & 1)
    return out


def with_markers(wav_bytes, marks):
    """A copy of a freshly-decoded WAV with the given EVP marks written as
    standard cue/LIST markers (any old ones removed first).

    ``marks`` items: ``{"start", "end", "cls", "note"}`` (seconds; "note" may
    be omitted/empty). A point mark has ``start == end``. The label text is
    ``"EVP <cls>: <note>"``, or ``"EVP <cls>"`` when note is empty.

    This is strict, and meant only for WAV bytes straight out of OpenEVP's
    own decoder: it raises ValueError on anything malformed (bad RIFF bounds,
    a missing fmt or data chunk) rather than guessing. Reading marks back
    from an arbitrary WAV -- including one this function did not just write
    -- is read_markers()'s job, not this one's.
    """
    buf = memoryview(wav_bytes)
    chunks = _strict_chunks(buf)

    rate, has_data = None, False
    kept = []
    for cid, off, size in chunks:
        if cid == b"fmt " and size >= 16:
            rate = struct.unpack_from("<I", buf, off + 4)[0]
        if cid == b"data":
            has_data = True
        if cid == b"cue " or (cid == b"LIST" and bytes(buf[off:off + 4]) == b"adtl"):
            continue                                # old markers: dropped, replaced below
        kept.append(buf[off - 8:off])               # the chunk's own 8-byte id+size header
        kept.append(buf[off:off + size])             # its payload, exactly the declared size
        if size & 1:
            kept.append(b"\0")                       # explicit pad byte -- never read from the source

    if rate is None:
        raise ValueError("no fmt chunk")
    if not has_data:
        raise ValueError("no data chunk")

    cues, adtl = [], []
    for i, m in enumerate(sorted(marks, key=lambda m: m["start"]), 1):
        start = round(m["start"] * rate)
        length = max(0, round(m["end"] * rate) - start)
        cues.append(struct.pack("<II4sIII", i, start, b"data", 0, 0, start))
        text = f"EVP {m['cls']}: {m['note']}" if m.get("note") else f"EVP {m['cls']}"
        adtl.append(_sub(b"labl", struct.pack("<I", i) + text.encode("utf-8") + b"\0"))
        if length:
            adtl.append(_sub(b"ltxt", struct.pack("<II4s4H", i, length, b"rgn ", 0, 0, 0, 0)))

    extra = []
    if cues:
        extra.append(_sub(b"cue ", struct.pack("<I", len(cues)) + b"".join(cues)))
        extra.append(_sub(b"LIST", b"adtl" + b"".join(adtl)))

    body_len = 4 + sum(len(k) for k in kept) + sum(len(e) for e in extra)      # "WAVE" + chunks
    total_len = 8 + body_len
    if total_len > _MAX_OUTPUT_SIZE:
        raise ValueError("the marked WAV would exceed the 4 GiB limit")
    return b"".join([b"RIFF", struct.pack("<I", body_len), b"WAVE", *kept, *extra])
