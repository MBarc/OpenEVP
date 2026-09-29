"""st25-download: copy every recording off a Sony ICD-ST25 as .dvf files.

    st25-download [OUTPUT_FOLDER] [--list] [--folder A-E] [--raw] [--wav]
    st25-download --check-wav DVF_FILE

--wav also writes a WAV file (8000 Hz, 16-bit mono) beside each .dvf, decoded
by the built-in LPEC decoder; a .wav missing beside an already-saved .dvf is
filled in. --check-wav decodes one .dvf file to verify that WAV conversion
works in this build, and saves nothing.

Nothing on the recorder is changed or deleted. Every recording is downloaded
(it takes seconds); existing files are never overwritten, and a recording is
skipped only if a file with identical audio is already there.
"""
import argparse
import os
import sys
import time
import traceback

from openevp.paths import default_output, documents_dir, open_folder  # noqa: F401

from . import __version__, audio, dvf
from .export import save_dvf, save_raw, save_wav  # noqa: F401
from .folder import TableError, parse
from .protocol import Recorder, RecorderError
from .session import build_dvf
from .usb import UsbError

LETTERS = "ABCDE"
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
    print(f"OpenEVP {__version__} (ICD-ST25 downloader)")
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
    if args.wav and not args.list and not wav_ok:
        print(f"Note: --wav requested but {audio.status()}; only .dvf files will be saved.")
    elif wav_ok and not args.list and audio.status():
        print(f"Note: {audio.status()}.")            # slow mode
    problems = pr.problems
    try:
        rec = Recorder()
    except UsbError as e:
        print(f"\nCould not open the recorder: {e}")
        print("Check that it is plugged in and that its driver is WinUSB (see the README).")
        return 2
    with rec:
        info = rec.device_info()
        model = info[36:52].split(b"\0")[0].decode("latin-1", "replace")
        print(f"Connected: {model or 'unknown model'}")
        if model != "ICD-ST25":
            print(f"Warning: only the ICD-ST25 has been verified; this is '{model}'.")
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
                print(f"  {m.number:3d}  {m.when():19s}  {m.seconds():7.1f} s  {m.owner}{note}")
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
                if wav_ok:
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


def _tables_hint():
    """How to create the LPEC table data, when it is what is missing (a
    developer hint kept out of the user-facing message)."""
    try:
        from openevp.decoders.sony_lpec import tables
        tables.load()
    except Exception as e:
        return getattr(e, "hint", None)
    return None


def check_wav(path):
    """Decode a .dvf file to prove WAV conversion actually works on this
    build (openevp.decoders.sony_lpec plus its bundled table data and DLL), without touching
    the recorder. Does not save anything. Used to verify a build: e.g.
    `openevp-st25.exe --check-wav some.dvf`."""
    print(f"OpenEVP {__version__} (ICD-ST25 downloader)")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        print(f"Could not read {path}: {e}")
        return 1
    if not audio.available():
        print(f"WAV conversion is not available: {audio.status()}")
        hint = _tables_hint()
        if hint:
            print(f"(Developers: {hint}.)")
        return 1
    if audio.status():
        print(f"Note: {audio.status()}.")            # slow mode
    try:
        wav = audio.dvf_to_wav(data)
    except Exception as e:
        print(f"Could not decode {path}: {e}")
        return 1
    seconds = max(0, len(wav) - 44) / 2 / 8000
    print(f"OK: decoded {path} to {len(wav)} bytes of WAV ({seconds:.1f} s of audio).")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="st25-download", description=__doc__.splitlines()[0],
                                 epilog="WAV conversion is built in: --wav decodes each recording "
                                 "with OpenEVP's own LPEC decoder.")
    ap.add_argument("output", nargs="?", help="output folder (default: Documents\\OpenEVP)")
    ap.add_argument("--list", action="store_true", help="only list the recordings")
    ap.add_argument("--folder", choices=list(LETTERS), help="only this folder")
    ap.add_argument("--raw", action="store_true", help="also save the raw wire data (for debugging)")
    ap.add_argument("--wav", action="store_true", help="also write a WAV file (8000 Hz, 16-bit mono) beside each .dvf; also fills in a "
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
