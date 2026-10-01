"""Updates from the project's GitHub releases.

check() asks GitHub for the latest release and says whether it is newer than
this app; if it is, it also collects the notes of every release the user skips
over (see release_notes()), for display only. download() fetches its installer and refuses it unless the release
key signed it: the signature covers the version and the installer's SHA-256,
so neither a changed file nor an older (validly signed) installer passes.
launch() starts the installer (it asks Windows for admin rights itself) and the
app then closes so the installer can replace it.

Release side: tools/sign_release.py writes <installer>.sig next to the
installer; both are attached to the GitHub release.
"""
import base64
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

from app import ed25519

REPO = "MBarc/OpenEVP"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
MAX_PAGES = 3                      # 300 releases: far more than will ever be skipped over
MAX_RELEASES = 20                  # notes shown at most; older ones are counted ("and N earlier updates")
MAX_NOTES = 2000                   # characters of one release's notes
PUBLIC_KEY = bytes.fromhex("578f9dd01e04cddc38bab037959d5f49863dd9ce5a581d8709ccf3579e99cd42")
MAX_INSTALLER = 400 << 20          # far above any real installer
MAX_SIG = 1024
TIMEOUT = 15
TEMP_PREFIX = "OpenEVP-update-"


class UpdateError(Exception):
    pass


class UpdateCancelled(UpdateError):
    pass


_held = {}      # installer path -> open handle keeping it from being changed (see _hold)


def _hold(path):
    """Open `path` so that nobody can write, rename or delete it while this app runs
    (Windows share mode: read only), and return a binary file object reading it.
    The installer is then hashed from this handle, so the bytes checked are the
    bytes on disk, and they stay that way until the installer has been started."""
    if sys.platform != "win32":
        return open(path, "rb")
    import msvcrt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING = 0x80000000, 0x1, 3
    h = k32.CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING, 0, None)
    if h in (None, ctypes.c_void_p(-1).value):
        raise UpdateError(f"could not lock the downloaded installer (error {ctypes.get_last_error()})")
    return os.fdopen(msvcrt.open_osfhandle(h, os.O_RDONLY | os.O_BINARY), "rb")


def parse_version(text):
    """'v0.7.0' or '0.7' -> (0, 7, 0); None if it isn't a plain dotted version."""
    parts = str(text).strip().lstrip("vV").split(".")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts) + (0,) * (3 - len(parts))


def version_text(v):
    return ".".join(map(str, v))


def installer_name(version):
    return f"OpenEVP-Setup-{version}.exe"


def signed_message(version, sha256_hex):
    """What the release key signs for one installer."""
    return f"OpenEVP release\nversion {version}\nsha256 {sha256_hex}\n".encode("ascii")


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise UpdateError(f"refusing a redirect to a non-HTTPS address: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpsOnly)


def _open(url, accept=None):
    if not url.startswith("https://"):
        raise UpdateError(f"refusing a non-HTTPS address: {url}")
    headers = {"User-Agent": "OpenEVP-updater"}
    if accept:
        headers["Accept"] = accept
    return _opener.open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT)


def _entry(release, version):
    """One release's notes for the update dialog: {"version", "date", "notes"}."""
    return {"version": version_text(version), "date": str(release.get("published_at") or "")[:10],
            "notes": str(release.get("body") or "").strip()[:MAX_NOTES]}


def release_notes(mine, latest, latest_release, opener=_open):
    """The notes of every release newer than `mine` up to and including `latest`,
    newest first, at most MAX_RELEASES of them; returns (entries, how many more).

    Every published release (not a draft or prerelease) with a plain vX.Y.Z tag
    counts, with or without an installer: a release that was never offered as an
    update still changed the app, so its notes belong in "what's new". The notes
    are display only; the installer and its signature come from `latest_release`
    alone (see check()). If the list can't be fetched or read, only the latest
    release's notes are returned: the update is never held up by its notes."""
    found = {latest: _entry(latest_release, latest)}
    try:
        for page in range(1, MAX_PAGES + 1):
            with opener(f"{RELEASES}&page={page}", "application/vnd.github+json") as r:
                releases = json.loads(r.read(16 << 20))
            if not isinstance(releases, list):
                raise ValueError("not a list of releases")
            for release in releases:
                if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
                    continue
                v = parse_version(release.get("tag_name", ""))
                if v is not None and mine < v < latest and v not in found:
                    found[v] = _entry(release, v)
            if len(releases) < 100:
                break
    except Exception:
        found = {latest: found[latest]}
    newest_first = [found[v] for v in sorted(found, reverse=True)]
    return newest_first[:MAX_RELEASES], max(0, len(newest_first) - MAX_RELEASES)


def check(current, opener=_open):
    """The latest release if it is newer than `current`, else None.
    Returns {"version", "notes", "page", "installer", "size", "signature",
    "releases", "earlier"}: "notes" are the latest release's, "releases" the notes
    of every release skipped over (release_notes()) and "earlier" how many more
    there are than are listed."""
    with opener(LATEST, "application/vnd.github+json") as r:
        release = json.loads(r.read(1 << 20))
    if release.get("draft") or release.get("prerelease"):
        return None
    latest, mine = parse_version(release.get("tag_name", "")), parse_version(current)
    if latest is None or mine is None or latest <= mine:
        return None
    version = version_text(latest)
    assets = {a.get("name"): a for a in release.get("assets", [])}
    exe, sig = assets.get(installer_name(version)), assets.get(installer_name(version) + ".sig")
    if not exe or not sig:
        return None                    # a release without a signed installer is not offered
    releases, earlier = release_notes(mine, latest, release, opener)
    return {"version": version, "notes": releases[0]["notes"],
            "page": release.get("html_url", ""), "installer": exe["browser_download_url"],
            "size": int(exe.get("size") or 0), "signature": sig["browser_download_url"],
            "releases": releases, "earlier": earlier}


def download(info, progress=None, opener=_open, public_key=None, cancelled=None):
    """Download and verify the installer; returns its path in a new temp folder.
    Raises UpdateError (UpdateCancelled if `cancelled()` turns true meanwhile) and
    leaves nothing behind if anything is wrong. The verified file stays locked
    against changes while this app runs."""
    key = public_key or PUBLIC_KEY
    folder = tempfile.mkdtemp(prefix=TEMP_PREFIX)
    path = os.path.join(folder, installer_name(info["version"]))
    try:
        with opener(info["signature"]) as r:
            sig_text = r.read(MAX_SIG + 1)
        if len(sig_text) > MAX_SIG:
            raise UpdateError("the signature file is too large")
        try:
            signature = base64.b64decode(sig_text.strip(), validate=True)
        except ValueError:
            raise UpdateError("the signature file is damaged") from None
        got = 0
        with opener(info["installer"]) as r, open(path, "wb") as f:
            while True:
                if cancelled and cancelled():
                    raise UpdateCancelled("the update was cancelled")
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_INSTALLER:
                    raise UpdateError("the download is far larger than an installer")
                f.write(chunk)
                if progress and info.get("size"):
                    progress(min(got / info["size"], 1.0))
        if info.get("size") and got != info["size"]:
            raise UpdateError("the download was incomplete")
        held = _hold(path)                             # from here on the file cannot change
        try:
            digest = hashlib.sha256()
            for chunk in iter(lambda: held.read(1 << 20), b""):
                digest.update(chunk)
            if not ed25519.verify(key, signed_message(info["version"], digest.hexdigest()), signature):
                raise UpdateError("the installer's signature is not valid, so it was not run")
        except BaseException:
            held.close()
            raise
        _held[path] = held
        return path
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def discard(path):
    """Unlock and delete a downloaded installer that will not be run after all."""
    held = _held.pop(path, None)
    if held:
        held.close()
    shutil.rmtree(os.path.dirname(path), ignore_errors=True)


def launch(path):
    """Start the installer: no wizard pages (a progress window only), then it
    reopens OpenEVP (/RELAUNCH, see installer/openevp.iss)."""
    subprocess.Popen([path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH"],
                     cwd=os.path.dirname(path), close_fds=True)


def clean_old_downloads():
    """Remove installers left from earlier updates (they are only needed while they run)."""
    base = tempfile.gettempdir()
    try:
        names = os.listdir(base)
    except OSError:
        return
    for name in names:
        if name.startswith(TEMP_PREFIX):
            shutil.rmtree(os.path.join(base, name), ignore_errors=True)
