import base64
import hashlib
import io
import json
import os
import shutil
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import backend, updater  # noqa: E402

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:                                 # a development dependency only
    Ed25519PrivateKey = None

INSTALLER = b"MZ pretend installer " * 1000


class Web:
    """A fake opener: URL -> bytes."""
    def __init__(self, pages):
        self.pages = pages

    def __call__(self, url, accept=None):
        return io.BytesIO(self.pages[url])


def release(tag="v0.7.0", names=("OpenEVP-Setup-0.7.0.exe", "OpenEVP-Setup-0.7.0.exe.sig"), **extra):
    return json.dumps({"tag_name": tag, "body": "New: WAV", "html_url": "https://github.com/x",
                       "assets": [{"name": n, "size": len(INSTALLER) if n.endswith(".exe") else 88,
                                   "browser_download_url": f"https://dl/{n}"} for n in names], **extra}).encode()


class VersionTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(updater.parse_version("v0.7.0"), (0, 7, 0))
        self.assertEqual(updater.parse_version("1.2"), (1, 2, 0))
        for bad in ("", "v", "0.7.0-beta", "1.2.3.4", "x.y"):
            self.assertIsNone(updater.parse_version(bad), bad)
        self.assertGreater(updater.parse_version("0.10.0"), updater.parse_version("0.9.9"))


class Ed25519Tests(unittest.TestCase):
    def test_rfc8032_test_1(self):
        from app import ed25519
        public = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        sig = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555f"
                            "b8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        self.assertTrue(ed25519.verify(public, b"", sig))
        self.assertFalse(ed25519.verify(public, b"x", sig))
        self.assertFalse(ed25519.verify(public, b"", sig[:32] + bytes(32)))
        self.assertFalse(ed25519.verify(public, b"", sig[:63]))
        self.assertFalse(ed25519.verify(bytes(31), b"", sig))


class CheckTests(unittest.TestCase):
    def test_newer_release_with_signed_installer_is_offered(self):
        info = updater.check("0.6.0", opener=Web({updater.LATEST: release()}))
        self.assertEqual((info["version"], info["installer"], info["signature"]),
                         ("0.7.0", "https://dl/OpenEVP-Setup-0.7.0.exe", "https://dl/OpenEVP-Setup-0.7.0.exe.sig"))

    def test_not_offered(self):
        for current, page in (("0.7.0", release()), ("0.8.0", release()),
                              ("0.6.0", release(names=("OpenEVP-Setup-0.7.0.exe",))),   # unsigned
                              ("0.6.0", release(draft=True)), ("0.6.0", release(prerelease=True)),
                              ("0.6.0", release(tag="nightly"))):
            with self.subTest(current=current, page=page[:80]):
                self.assertIsNone(updater.check(current, opener=Web({updater.LATEST: page})))

    def test_only_https(self):
        with self.assertRaises(updater.UpdateError):
            updater._open("http://example.com/x")


def listed(tag, body=None, names=(), **extra):
    """One release as GitHub's releases list gives it."""
    return {"tag_name": tag, "body": body if body is not None else f"Notes for {tag}",
            "published_at": "2026-09-01T10:00:00Z", "html_url": f"https://github.com/{tag}",
            "assets": [{"name": n, "size": 5, "browser_download_url": f"https://dl/old/{n}"} for n in names],
            **extra}


def page(url_page):
    return f"{updater.RELEASES}&page={url_page}"


class ReleaseNotesTests(unittest.TestCase):
    """check() also returns the notes of every release the update skips over."""
    LATEST_TAG = "v0.10.0"

    def latest(self):
        return release(tag=self.LATEST_TAG, names=("OpenEVP-Setup-0.10.0.exe", "OpenEVP-Setup-0.10.0.exe.sig"),
                       body="Latest notes", published_at="2026-10-01T09:00:00Z")

    def check(self, current, pages, latest=None):
        web = Web({updater.LATEST: latest or self.latest(),
                   **{page(i + 1): json.dumps(p).encode() for i, p in enumerate(pages)}})
        return updater.check(current, opener=web)

    def test_every_skipped_release_newest_first(self):
        listing = [listed("v0.10.0", "Latest notes"),
                   listed("v0.9.9", names=("OpenEVP-Setup-0.9.9.exe", "OpenEVP-Setup-0.9.9.exe.sig")),
                   listed("v0.9.10-beta"),                         # not a plain version
                   listed("v0.9.11", prerelease=True),
                   listed("v0.9.12", draft=True),
                   listed("v0.9.8"),                               # no installer: its notes still count
                   listed("v0.11.0"),                              # newer than the latest (shouldn't happen): not shown
                   listed("v0.9.7"), listed("v0.9.6")]             # not newer than this app
        info = self.check("0.9.7", [[listing[i] for i in (5, 0, 2, 7, 1, 3, 4, 6, 8)]])   # GitHub's order isn't relied on
        self.assertEqual([r["version"] for r in info["releases"]], ["0.10.0", "0.9.9", "0.9.8"])
        self.assertEqual(info["releases"][0], {"version": "0.10.0", "date": "2026-10-01", "notes": "Latest notes"})
        self.assertEqual(info["releases"][1], {"version": "0.9.9", "date": "2026-09-01", "notes": "Notes for v0.9.9"})
        self.assertEqual((info["earlier"], info["notes"]), (0, "Latest notes"))

    def test_installer_and_signature_are_the_latest_releases(self):
        listing = [listed("v0.9.9", names=("OpenEVP-Setup-0.9.9.exe", "OpenEVP-Setup-0.9.9.exe.sig")),
                   listed("v0.9.8", names=("OpenEVP-Setup-0.9.8.exe", "OpenEVP-Setup-0.9.8.exe.sig"))]
        info = self.check("0.9.7", [listing])
        self.assertEqual((info["version"], info["installer"], info["signature"], info["size"], info["page"]),
                         ("0.10.0", "https://dl/OpenEVP-Setup-0.10.0.exe", "https://dl/OpenEVP-Setup-0.10.0.exe.sig",
                          len(INSTALLER), "https://github.com/x"))
        self.assertEqual([r["version"] for r in info["releases"]], ["0.10.0", "0.9.9", "0.9.8"])

    def test_numeric_versions_across_0_9_to_0_10(self):
        listing = [listed(f"v0.9.{n}") for n in (8, 9, 10, 11)] + [listed("v0.10.0")]
        info = self.check("0.9.9", [listing])
        self.assertEqual([r["version"] for r in info["releases"]], ["0.10.0", "0.9.11", "0.9.10"])

    def test_capped_with_a_count_of_earlier_ones(self):
        listing = [listed(f"v0.9.{n}") for n in range(1, 31)] + [listed("v0.10.0")]
        info = self.check("0.9.0", [listing])
        self.assertEqual(len(info["releases"]), updater.MAX_RELEASES)
        self.assertEqual([r["version"] for r in info["releases"][:3]], ["0.10.0", "0.9.30", "0.9.29"])
        self.assertEqual(info["releases"][-1]["version"], "0.9.12")
        self.assertEqual(info["earlier"], 31 - updater.MAX_RELEASES)

    def test_pages_are_followed_up_to_a_limit(self):
        full = [listed(f"v0.{m}.{n}") for m in (1, 2, 3, 4) for n in range(25)]     # 100: a full page
        later = [listed(f"v0.5.{n}") for n in range(100)]
        last = [listed("v0.0.9")]                                                    # a short page: the end
        info = self.check("0.0.1", [full, later, last])
        self.assertEqual(info["earlier"], 1 + 200 + 1 - updater.MAX_RELEASES)      # 0.10.0, two full pages, the last
        with mock.patch.object(updater, "MAX_PAGES", 1):
            self.assertEqual(self.check("0.0.1", [full, later, last])["earlier"], 100 + 1 - updater.MAX_RELEASES)
        info = self.check("0.0.1", [[listed("v0.0.9")]])                           # page 2 is never asked for
        self.assertEqual([r["version"] for r in info["releases"]], ["0.10.0", "0.0.9"])

    def test_a_failed_list_shows_the_latest_notes_only(self):
        class Failing(Web):
            def __call__(self, url, accept=None):
                if url.startswith(updater.RELEASES):
                    raise OSError("connection reset")
                return super().__call__(url, accept)
        for name, web in (("network", Failing({updater.LATEST: self.latest()})),
                          ("not JSON", Web({updater.LATEST: self.latest(), page(1): b"<html>"})),
                          ("not a list", Web({updater.LATEST: self.latest(), page(1): b'{"message": "x"}'})),
                          ("second page fails", Web({updater.LATEST: self.latest(),
                                                     page(1): json.dumps([listed("v0.9.9")] * 100).encode()}))):
            with self.subTest(name):
                info = updater.check("0.9.7", opener=web)
                self.assertEqual(info["releases"], [{"version": "0.10.0", "date": "2026-10-01", "notes": "Latest notes"}])
                self.assertEqual((info["earlier"], info["version"], info["installer"]),
                                 (0, "0.10.0", "https://dl/OpenEVP-Setup-0.10.0.exe"))

    def test_odd_entries_are_skipped(self):
        info = self.check("0.9.7", [[None, "x", {"tag_name": None}, listed("v0.9.8", body=None) | {"body": None}]])
        self.assertEqual(info["releases"][1], {"version": "0.9.8", "date": "2026-09-01", "notes": ""})

    def test_not_asked_for_when_up_to_date(self):
        asked = []

        class Watch(Web):
            def __call__(self, url, accept=None):
                asked.append(url)
                return super().__call__(url, accept)
        self.assertIsNone(updater.check("0.10.0", opener=Watch({updater.LATEST: self.latest()})))
        self.assertEqual(asked, [updater.LATEST])


@unittest.skipIf(Ed25519PrivateKey is None, "needs the cryptography package")
class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.public = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.info = {"version": "0.7.0", "installer": "https://dl/i.exe", "signature": "https://dl/i.sig",
                     "size": len(INSTALLER)}

    def sig(self, version="0.7.0", data=INSTALLER, key=None):
        msg = updater.signed_message(version, hashlib.sha256(data).hexdigest())
        return base64.b64encode((key or self.key).sign(msg)) + b"\n"

    def fetch(self, sig, data=INSTALLER, progress=None, **info):
        web = Web({"https://dl/i.exe": data, "https://dl/i.sig": sig})
        return updater.download({**self.info, **info}, progress=progress, opener=web, public_key=self.public)

    def test_good_installer_is_kept(self):
        seen = []
        path = self.fetch(self.sig(), progress=seen.append)
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        self.assertTrue(path.endswith("OpenEVP-Setup-0.7.0.exe"))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), INSTALLER)
        self.assertEqual(seen[-1], 1.0)

    def test_rejected_and_nothing_left_behind(self):
        cases = {
            "changed file": dict(sig=self.sig(), data=INSTALLER[:-1] + b"!"),
            "an older installer's signature": dict(sig=self.sig(version="0.6.0")),
            "garbage signature": dict(sig=b"not base64!!"),
            "another key": dict(sig=self.sig(key=Ed25519PrivateKey.generate())),
            "incomplete download": dict(sig=self.sig(), size=len(INSTALLER) + 5),
        }
        real = updater.tempfile.mkdtemp
        for name, case in cases.items():
            made = []
            with self.subTest(name), mock.patch.object(updater.tempfile, "mkdtemp",
                                                       side_effect=lambda **k: made.append(real(**k)) or made[-1]):
                with self.assertRaises(updater.UpdateError):
                    self.fetch(**case)
                self.assertFalse(os.path.exists(made[0]))

    def release_held(self):
        for path in list(updater._held):
            updater.discard(path)

    def test_cancel_stops_the_download_midway(self):
        calls = []

        def cancelled():
            calls.append(1)
            return len(calls) > 1                       # after the first chunk
        big = INSTALLER * 20
        made = []
        real = updater.tempfile.mkdtemp
        with mock.patch.object(updater.tempfile, "mkdtemp",
                               side_effect=lambda **k: made.append(real(**k)) or made[-1]):
            with self.assertRaises(updater.UpdateCancelled):
                updater.download({**self.info, "size": len(big)}, public_key=self.public, cancelled=cancelled,
                                 opener=Web({"https://dl/i.exe": big, "https://dl/i.sig": self.sig(data=big)}))
        self.assertEqual(len(calls), 2)
        self.assertFalse(os.path.exists(made[0]))

    @unittest.skipUnless(sys.platform == "win32", "Windows share modes")
    def test_verified_installer_is_locked_but_still_runs(self):
        path = self.fetch(self.sig())
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        self.addCleanup(self.release_held)
        with self.assertRaises(OSError):
            open(path, "ab").close()                    # no one can change it...
        with self.assertRaises(OSError):
            os.remove(path)                             # ...or swap it
        # ...and Windows can still start a locked program (a real exe, locked the same way).
        exe = os.path.join(os.path.dirname(path), "where.exe")
        shutil.copyfile(os.path.join(os.environ["SystemRoot"], "System32", "where.exe"), exe)
        held = updater._hold(exe)
        import subprocess
        self.assertEqual(subprocess.run([exe, "/?"], capture_output=True).returncode, 0)
        held.close()
        updater.discard(path)                           # not run after all: unlocked and deleted
        self.assertFalse(os.path.exists(os.path.dirname(path)))
        self.assertNotIn(path, updater._held)

    def test_size_cap(self):
        with mock.patch.object(updater, "MAX_INSTALLER", 100), self.assertRaises(updater.UpdateError):
            self.fetch(self.sig())


class FakeUpdater:
    def __init__(self, info=None, fail=None, launch_fails=False):
        self.info, self.fail, self.launched, self.discarded = info, fail, [], []
        self.launch_fails = launch_fails

    def check(self, current):
        return self.info

    def download(self, info, progress=None, cancelled=None):
        if self.fail:
            raise updater.UpdateError(self.fail)
        progress(1.0)
        return "C:/tmp/OpenEVP-Setup-9.0.0.exe"

    def launch(self, path):
        if self.launch_fails:
            raise OSError("could not start it")
        self.launched.append(path)

    def discard(self, path):
        self.discarded.append(path)


class BackendUpdateTests(unittest.TestCase):
    INFO = {"version": "9.0.0", "notes": "n", "page": "p", "installer": "i", "signature": "s", "size": 1}

    def api(self, up, can_install=True):
        self.events, self.quits, self.order = [], [], []
        return backend.Api(None, lambda *e: self.events.append(e), None, "D", None, updater=up,
                           quit_app=lambda: self.quits.append(1), can_install=can_install,
                           before_install=lambda: self.order.append("mutex dropped"))

    def test_release_notes_are_passed_on(self):
        releases = [{"version": "9.0.0", "date": "2026-10-01", "notes": "n"}, {"version": "8.9.0", "date": "", "notes": "m"}]
        r = self.api(FakeUpdater({**self.INFO, "releases": releases, "earlier": 4})).check_update()
        self.assertEqual((r["releases"], r["earlier"], r["notes"]), (releases, 4, "n"))
        r = self.api(FakeUpdater(self.INFO)).check_update()                       # an updater without them
        self.assertEqual((r["releases"], r["earlier"]), ([{"version": "9.0.0", "date": "", "notes": "n"}], 0))

    def test_no_update(self):
        r = self.api(FakeUpdater()).check_update()
        self.assertEqual((r["ok"], r["available"]), (True, False))
        self.assertFalse(self.api(None).check_update()["available"])

    def test_install_downloads_launches_and_quits(self):
        up = FakeUpdater(self.INFO)
        a = self.api(up)
        self.assertEqual(a.check_update()["version"], "9.0.0")
        self.assertEqual(a.install_update(), {"ok": True})
        self.assertEqual((up.launched, self.quits), (["C:/tmp/OpenEVP-Setup-9.0.0.exe"], [1]))
        self.assertEqual(self.order, ["mutex dropped"])
        self.assertEqual(self.events, [("update-progress", {"percent": 100})])

    def test_progress_is_sent_per_whole_percent(self):
        class Slow(FakeUpdater):
            def download(self, info, progress=None, cancelled=None):
                for i in range(1001):
                    progress(i / 1000)
                return "x.exe"
        a = self.api(Slow(self.INFO))
        a.check_update()
        a.install_update()
        self.assertEqual(len(self.events), 101)

    def test_closing_during_download_cancels(self):
        class Closing(FakeUpdater):
            def download(self, info, progress=None, cancelled=None):
                api.request_stop()                      # the user closed the window meanwhile
                return "x.exe"
        up = Closing(self.INFO)
        api = self.api(up)
        api.check_update()
        self.assertTrue(api.updating() is False)
        r = api.install_update()
        self.assertIn("cancelled", r["error"])
        self.assertEqual((up.launched, self.order, self.quits), ([], [], []))
        self.assertEqual(up.discarded, ["x.exe"])
        self.assertFalse(api.updating())

    def test_installer_that_cannot_start_is_discarded(self):
        up = FakeUpdater(self.INFO, launch_fails=True)
        a = self.api(up)
        a.check_update()
        self.assertFalse(a.install_update()["ok"])
        self.assertEqual((up.discarded, self.quits), (["C:/tmp/OpenEVP-Setup-9.0.0.exe"], []))

    def test_an_update_is_not_reported_as_an_export(self):
        class Watch(FakeUpdater):
            def download(self, info, progress=None, cancelled=None):
                seen.append((api.updating(), api.exporting()))
                return "x.exe"
        seen = []
        api = self.api(Watch(self.INFO))
        api.check_update()
        api.install_update()
        self.assertEqual(seen, [(True, False)])

    def test_install_refusals(self):
        self.assertFalse(self.api(FakeUpdater(self.INFO)).install_update()["ok"])   # not checked yet
        a = self.api(FakeUpdater(self.INFO), can_install=False)
        a.check_update()
        self.assertIn("from source", a.install_update()["error"])
        a = self.api(FakeUpdater(self.INFO, fail="the installer's signature is not valid"))
        a.check_update()
        self.assertIn("signature is not valid", a.install_update()["error"])
        self.assertFalse(a.exporting())                                               # lock released
        self.assertEqual(self.quits, [])
        a = self.api(FakeUpdater(self.INFO))
        a.check_update()
        a._busy.acquire()                                                             # an export runs
        self.assertIn("export", a.install_update()["error"])

    def test_check_failure_is_reported(self):
        class Offline(FakeUpdater):
            def check(self, current):
                raise OSError("no network")
        r = self.api(Offline()).check_update()
        self.assertFalse(r["ok"])
        self.assertIn("could not reach GitHub", r["error"])

    def test_plain_words_for_http_errors(self):
        import urllib.error
        for code, words in ((404, "no releases"), (403, "limiting requests")):
            e = urllib.error.HTTPError("u", code, "x", {}, None)
            self.assertIn(words, backend._update_problem(e))


class RedirectTests(unittest.TestCase):
    def test_redirect_to_http_is_refused(self):
        import urllib.request
        handler = updater._HttpsOnly()
        req = urllib.request.Request("https://github.com/x")
        with self.assertRaises(updater.UpdateError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://evil/x")
        self.assertIsNotNone(handler.redirect_request(req, None, 302, "Found", {}, "https://objects.example/x"))



@unittest.skipUnless(sys.platform == "win32", "Windows mutexes")
class HandOverTests(unittest.TestCase):
    def test_refuses_while_another_window_holds_the_mutex(self):
        from app import main
        if main._another_instance_running():
            self.skipTest("an OpenEVP window is open on this PC")
        ours, other = main._announce_running(), main._announce_running()   # "other" plays a second window
        self.addCleanup(main._stop_announcing, other)
        self.addCleanup(main._stop_announcing, ours)
        with self.assertRaises(updater.UpdateError):
            main._hand_over(ours)
        self.assertEqual(len(ours), 2)                   # re-announced: this window keeps running
        main._stop_announcing(other)
        main._hand_over(ours)                            # alone now: handed over
        self.assertEqual(ours, [])
        self.assertFalse(main._another_instance_running())


if __name__ == "__main__":
    unittest.main()
