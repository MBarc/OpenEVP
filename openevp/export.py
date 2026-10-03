"""Saving recordings without ever replacing an existing file.

Shared by the command-line downloader and the desktop app. Format-specific
equality (e.g. .dvf audio with block counters removed) belongs to the format:
see sony_icd.export.save_dvf.
"""
import os
import re
import tempfile


def _parts(data):
    """data as a sequence of bytes-like pieces (a list of them is kept as is)."""
    return data if isinstance(data, (list, tuple)) else (data,)


def _size(data):
    return sum(memoryview(p).nbytes for p in _parts(data))


def publish(data, final_path):
    """Write data to final_path without ever replacing an existing file.

    ``data`` is bytes-like, or a list of bytes-like pieces written in order
    (openevp.wavinfo.marked_parts: a long WAV is not joined into a copy).
    Writes a uniquely named temp file in the same folder first, then publishes
    it with an operation that fails if the destination exists.
    """
    folder = os.path.dirname(final_path)
    fd, tmp = tempfile.mkstemp(prefix=".st25-", suffix=".part", dir=folder)
    try:
        with os.fdopen(fd, "wb") as f:
            for part in _parts(data):
                f.write(part)
            f.flush()
            os.fsync(f.fileno())
        if os.name == "nt":
            os.rename(tmp, final_path)          # refuses to overwrite on Windows
        else:
            os.link(tmp, final_path)            # refuses to overwrite on POSIX
            os.unlink(tmp)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _choose(outdir, name, same, same_file=None):
    """Return (path, already_saved) for data identified by same(existing_bytes)
    (or, when given, same_file(path), which reads the file itself).

    Every existing "<name>" / "<stem> (N)<ext>" file is checked, not just up to
    the first gap in the numbering (a user may have deleted "(2)" but kept
    "(3)"). If one holds the same data it is returned; otherwise the lowest
    free name is used. Never picks a path holding a file.
    """
    stem, ext = os.path.splitext(name)
    pattern = re.compile(re.escape(stem) + r"(?: \(([1-9][0-9]*)\))?" + re.escape(ext), re.IGNORECASE)
    try:
        entries = os.listdir(outdir)
    except FileNotFoundError:
        entries = []
    taken = set()
    for entry in entries:
        m = pattern.fullmatch(entry)
        if not m:
            continue
        if m.group(1) != "1":               # "name (1).dvf" does not occupy "name.dvf"
            taken.add(int(m.group(1) or 1))
        path = os.path.join(outdir, entry)
        try:
            if same_file is not None:
                found = same_file(path)
            else:
                with open(path, "rb") as f:
                    found = same(f.read())
        except OSError:
            continue
        if found:
            return path, True
    i = 1
    while i in taken:
        i += 1
    return os.path.join(outdir, name if i == 1 else f"{stem} ({i}){ext}"), False


def save_unique(data, outdir, name, same, same_file=None):
    """Publish data under name, or its lowest free "(N)" variant, unless a file
    matching same() (or same_file(path)) already exists. Returns (path, already_saved)."""
    for _attempt in range(5):               # another program may take the name first
        path, done = _choose(outdir, name, same, same_file)
        if done:
            return path, True
        try:
            publish(data, path)
            return path, False
        except FileExistsError:
            continue
    raise OSError(f"could not find a free file name for {name}")


def save_raw(raw, outdir, stem):
    """Save raw wire data as <stem>.raw (for --raw); returns the path used.

    Independent of the .dvf: it is written even when the .dvf was already saved
    or could not be built (an unsupported recording is exactly when the raw
    data is needed). Identical bytes already on disk are reused, a different
    file is never overwritten.
    """
    return save_unique(raw, outdir, stem + ".raw", lambda existing: existing == raw)[0]


def _file_equals(path, data):
    """Whether the file at path holds exactly data (bytes-like, or a list of
    pieces), compared piece by piece in blocks: neither is read whole."""
    with open(path, "rb") as f:
        if os.fstat(f.fileno()).st_size != _size(data):
            return False
        for part in _parts(data):
            view = memoryview(part).cast("B")
            for at in range(0, len(view), 1 << 20):
                block = view[at:at + (1 << 20)]
                if f.read(len(block)) != block:
                    return False
        return True


def save_wav(wav, outdir, name):
    """Save a WAV unless an identical file is already there. Returns (path, already_saved).

    ``wav`` is bytes-like or a list of pieces (openevp.wavinfo.marked_parts).
    Identical means byte-identical: the same audio with different EVP markers is a
    different file and gets a numbered name, so re-exporting after the marks
    changed never silently keeps the old marks. Existing files are compared in
    blocks (and only when the size matches), never read whole."""
    return save_unique(wav, outdir, name, None, lambda path: _file_equals(path, wav))
