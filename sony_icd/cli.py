"""openevp-cli: copy every recording off a Sony ICD-ST25 or ICD-ST10 as .dvf files.

    openevp-cli [OUTPUT_FOLDER] [--list] [--folder A-E] [--raw] [--wav]
    openevp-cli --check-wav DVF_FILE

--wav also writes a WAV file beside each .dvf, decoded by the built-in LPEC
decoders (LPEC LP: 8000 Hz mono; LPEC SP: 16000 Hz mono; LPEC ST: 44.1 kHz
stereo); a .wav missing beside an already-saved .dvf is filled in. A
recording whose codec can't be decoded in this build (e.g. its table data is
missing) is saved as .dvf only, with a note. --check-wav decodes one .dvf file to verify that WAV
conversion works in this build, and saves nothing.

Nothing on the recorder is changed or deleted. Every recording is downloaded
(it takes seconds); existing files are never overwritten, and a recording is
skipped only if a file with identical audio is already there.
"""
import argparse
import os
import sys
import time
import traceback
import wave

from openevp import wavinfo
from openevp.export import save_raw, save_wav
from openevp.paths import default_output, documents_dir, open_folder  # noqa: F401

from . import __version__, audio, dvf
from .export import save_dvf
from .folder import TableError, parse
from .protocol import Recorder, RecorderError
from .session import build_dvf
from .usb import UsbError

LETTERS = "ABCDE"
MODELS = ("ICD-ST25", "ICD-ST10")      # the identify strings verified; any other is read as an ST25
REPLUG = ("The recorder may now be stuck. Unplug its USB cable, wait a few seconds, "
          "plug it back in, and run this program again. Recordings already saved are skipped.")


class Progress:
    """What has happened so far, so an error can report it (see main())."""

    def __init__(self):
        self.saved = self.skipped = self.wav_saved = 0
        self.problems = []
        self.current = None          # label of the recording being handled, e.g. "A-007"
        self.out_root = None

    def summary(self):
        lines = [f"{self.saved} saved, {self.skipped} already saved before."]
        if self.wav_saved:
            plural = "" if self.wav_saved == 1 else "s"
            lines.append(f"{self.wav_saved} WAV file{plural} written.")
        lines += [f"  NOTE {p}" for p in self.problems]
        if (self.saved or self.skipped) and self.out_root:
            lines.append(f"Files are in: {self.out_root}")
        return "\n".join(lines)


def run(args, progress=None):
    pr = progress if progress is not None else Progress()
    print(f"OpenEVP {__version__} (Sony ICD-ST downloader)")
    if args.output:
        out_root = os.path.abspath(args.output)
    else:
        out_root, warning = default_output()
        if warning:
            print(f"Note: {warning}")
    pr.out_root = out_root
    if not args.list:
        print(f"Saving to: {out_root}")
    # Checked once (not per recording): availability doesn't change mid-run, and
    # tables.load() caches the parsed data in-process, so there's nothing to save
    # by re-checking inside the loop below.
    wav_ok = args.wav and audio.available()
    codec_ok = {dvf.CODEC_LP: wav_ok}       # other codecs (ICD-ST10: LPEC ST, SP) when first seen
    if args.wav and not args.list and not wav_ok:
        print(f"Note: --wav requested but {audio.status()}; only .dvf files will be saved.")
    elif wav_ok and not args.list and audio.status():
        print(f"Note: {audio.status()}.")            # slow mode
    problems = pr.problems
    try:
        rec = Recorder()
    except UsbError as e:
        print(f"\nCould not open the recorder: {e}")
        print("Check that it is plugged in and that its driver is WinUSB (see docs/technical.md).")
        return 2
    with rec:
        info = rec.device_info()
        model = info[36:52].split(b"\0")[0].decode("latin-1", "replace")
        print(f"Connected: {model or 'unknown model'}")
        if model not in MODELS:
            print(f"Warning: only the ICD-ST25 and ICD-ST10 have been verified; this is '{model}'.")
        # Digital Voice Editor reads these on connect; keep the same sequence.
        rec.read_block(0x1E0, 0)
        rec.read_block(0x1E0, 0x1E0)
        rec.info_03()
        rec.target_status()

        for fi, letter in enumerate(LETTERS, 1):
            if args.folder and letter != args.folder:
                continue
            table = rec.folder_table(fi)
            msgs = parse(table)
            print(f"\nFolder {letter}: {len(msgs)} recording(s)")
            for m in msgs:
                note = f"  ** {m.problem}" if m.problem else ""
                mode = f"  {dvf.MODES[m.mode]}" if m.mode in dvf.MODES else ""
                seconds = m.seconds()
                length = "      ?" if seconds is None else f"{seconds:7.1f}"
                print(f"  {m.number:3d}  {m.when():19s}  {length} s  {m.owner}{mode}{note}")
            if args.list or not msgs:
                continue
            outdir = os.path.join(out_root, letter)
            os.makedirs(outdir, exist_ok=True)
            for m in msgs:
                label = f"{letter}-{m.number:03d}"
                pr.current = label
                if m.problem:
                    problems.append(f"{label}: skipped ({m.problem})")
                    continue
                t0 = time.monotonic()
                raw = rec.voice_data(fi, m.number, m.blocks)
                # --- transaction complete: disk work and printing are safe from here ---
                name = dvf.filename(letter, m.number, m.owner, m.date, m.dated)
                raw_note = ""
                if args.raw:
                    try:
                        save_raw(raw, outdir, name[:-4])
                        raw_note = "; raw data saved"
                    except OSError as e:
                        problems.append(f"{label}: raw data not saved ({e})")
                try:
                    data = build_dvf(raw, m, label)
                except dvf.FormatError as e:
                    problems.append(f"{label}: not saved ({e})" + raw_note)
                    continue
                path, done = save_dvf(data, outdir, name)
                wav_note = ""
                wav_written = False
                codec = dvf.codec(data)
                if args.wav and codec not in codec_ok:
                    codec_ok[codec] = audio.available(codec)
                if args.wav and not codec_ok[codec] and codec != dvf.CODEC_LP:   # LP's reason was said up front
                    if not done:
                        wav_note = f"; no WAV: {audio.status(codec)}"
                elif args.wav and codec_ok[codec]:
                    try:
                        wav_path, wav_done = save_wav(audio.dvf_to_wav(data), outdir, name[:-4] + ".wav")
                        if not wav_done:
                            wav_note = f"; wav saved {os.path.basename(wav_path)}"
                            wav_written = True
                            pr.wav_saved += 1
                    except OSError as e:
                        problems.append(f"{label}: WAV not saved ({e})")
                    except Exception as e:                # the decoder rejected this recording
                        problems.append(f"{label}: WAV not converted ({e})")
                if done:
                    pr.skipped += 1
                    if wav_written:            # backfilling a .wav for a .dvf saved in an earlier run
                        print(f"  {os.path.basename(path)} already saved before"
                              f"; wav saved {os.path.basename(wav_path)}")
                    continue
                pr.saved += 1
                print(f"  saved {os.path.basename(path)}  ({m.seconds():.0f} s audio, "
                      f"{time.monotonic() - t0:.1f} s){wav_note}")
            pr.current = None
    print(f"\nDone: {pr.summary()}")
    if args.open and (pr.saved or pr.skipped) and not open_folder(out_root):
        print("(Could not open the folder automatically.)")
    return 1 if problems else 0


def _tables_hint(codec=None):
    """How to create the table data of the decoder for ``codec`` (LPEC LP by
    default, LPEC ST for CODEC_ST), when it is what is missing (a developer
    hint kept out of the user-facing message)."""
    try:
        if codec == dvf.CODEC_ST:
            from openevp.decoders.sony_lpec_st import tables
        else:
            from openevp.decoders.sony_lpec import tables
        tables.load()
    except Exception as e:
        return getattr(e, "hint", None)
    return None


def check_wav(path):
    """Decode a .dvf file to prove WAV conversion actually works on this
    build (the decoder for its codec, openevp.decoders.sony_lpec for an
    ICD-ST25 file or openevp.decoders.sony_lpec_st for an ICD-ST10 one, plus
    its bundled table data and DLL), without touching the recorder. Does not
    save anything. Used to verify a build: e.g.
    `openevp-cli.exe --check-wav some.dvf`."""
    print(f"OpenEVP {__version__} (Sony ICD-ST downloader)")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        print(f"Could not read {path}: {e}")
        return 1
    codec = dvf.codec(data)
    if not audio.available(codec):
        print(f"WAV conversion is not available: {audio.status(codec)}")
        hint = _tables_hint(codec)
        if hint:
            print(f"(Developers: {hint}.)")
        return 1
    if audio.status(codec):
        print(f"Note: {audio.status(codec)}.")       # slow mode
    try:
        wav = audio.dvf_to_wav(data)
    except Exception as e:
        print(f"Could not decode {path}: {e}")
        return 1
    seconds = _wav_seconds(wav)
    print(f"OK: decoded {path} to {len(wav)} bytes of WAV ({seconds:.1f} s of audio).")
    return 0


def _wav_seconds(wav):
    """The length of a WAV from its header (the LP decoder's when unreadable: 8000 Hz 16-bit mono)."""
    try:
        with wavinfo.buffer_file(wav) as f, wave.open(f) as w:
            return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, ZeroDivisionError):
        return max(0, len(wav) - 44) / 2 / 8000


def main(argv=None):
    ap = argparse.ArgumentParser(prog="openevp-cli", description=__doc__.splitlines()[0],
                                 epilog="WAV conversion is built in: --wav decodes each recording "
                                 "with OpenEVP's own LPEC decoders (LPEC LP, and LPEC SP and ST "
                                 "from an ICD-ST10).")
    ap.add_argument("output", nargs="?", help="output folder (default: Documents\\OpenEVP)")
    ap.add_argument("--list", action="store_true", help="only list the recordings")
    ap.add_argument("--folder", choices=list(LETTERS), help="only this folder")
    ap.add_argument("--raw", action="store_true", help="also save the raw wire data (for debugging)")
    ap.add_argument("--wav", action="store_true", help="also write a WAV file beside each .dvf (16-bit; 8000 Hz mono "
                    "for LPEC LP, 16000 Hz mono for LPEC SP, 44100 Hz stereo for LPEC ST); also fills in a "
                    "missing .wav beside a .dvf saved earlier")
    ap.add_argument("--open", action="store_true",
                    help="open the output folder when done (default when the .exe is double-clicked)")
    ap.add_argument("--check-wav", metavar="DVF_FILE",
                    help="decode DVF_FILE to verify WAV conversion works on this build, without "
                    "touching the recorder, and exit")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    if args.check_wav:
        return check_wav(args.check_wav)
    if argv is None and len(sys.argv) == 1 and getattr(sys, "frozen", False):
        args.open = True                    # double-clicked: show the recordings afterwards
    pr = Progress()
    try:
        return run(args, pr)
    except (RecorderError, UsbError, TableError) as e:
        print(f"\nERROR{_at(pr)}: {e}\n{REPLUG}")
    except OSError as e:
        print(f"\nERROR saving files{_at(pr)}: {e}\n"
              "Check free disk space and that the output folder is writable.")
    except KeyboardInterrupt:
        print(f"\nStopped{_at(pr)}.\n{REPLUG}")
    except Exception:
        print(f"\nUnexpected error{_at(pr)} - please report it at https://github.com/MBarc/OpenEVP/issues with this text:\n")
        traceback.print_exc(file=sys.stdout)
        print(f"\n{REPLUG}")
    print(f"\nBefore stopping: {pr.summary()}")
    return 1


def _at(pr):
    return f" at recording {pr.current}" if pr.current else ""


if __name__ == "__main__":
    sys.exit(main())
