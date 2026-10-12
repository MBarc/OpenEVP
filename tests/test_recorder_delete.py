"""Delete from recorder: the one allowed write (policy and protocol). Synthetic data only."""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fixtures import DATE, FakeRecorderDevice  # noqa: E402
from sony_icd import policy  # noqa: E402
from sony_icd.protocol import Recorder, RecorderError, RecorderStuck, folder_counts  # noqa: E402

UNCLEAR = ("A-002 may have been deleted; the recorder stopped answering. Unplug it, plug it back in, "
           "and check the folder before deleting again.")
BUSY = "Something else is using the recorder; try again when it finishes."
THREE = {1: [(0, 1, 0x1000, 3000, DATE, "X"), (1, 2, 0x2000, 4000, DATE, "X"), (2, 3, 0x3000, 5000, DATE, "X")]}


def recorder(folders=THREE):
    r = Recorder.__new__(Recorder)
    r.dev = FakeRecorderDevice({k: list(v) for k, v in folders.items()})
    return r


class PolicyTests(unittest.TestCase):
    def test_the_one_frame(self):
        f = policy.delete_frame(1, 2)
        self.assertEqual(len(f), 117)
        self.assertEqual(f[:18], policy.FRAME_PREFIX + bytes.fromhex("12ff0100") + b"\x00\x01")
        self.assertEqual(f[18:], bytes(1) + b"\x01" + bytes(97))
        self.assertTrue(policy.delete_frame_ok(f))
        self.assertTrue(policy.allow_out(policy.REQTYPE_OUT, policy.REQ_COMMAND, policy.WVALUE, 0, f))

    def test_everything_else_is_blocked(self):
        good = bytearray(policy.delete_frame(1, 2))
        bad = []
        for mode in (0x00, 0x02, 0x03, 0x04):          # every other mode
            f = bytearray(good); f[14] = mode; bad.append(f)
        f = bytearray(good); f[18:] = bytes(99); bad.append(f)                       # no flag
        f = bytearray(good); f[18] = 1; bad.append(f)                                # two flags
        f = bytearray(good); f[19] = 2; bad.append(f)                                # flag value not 1
        for folder in (0, 6):
            f = bytearray(good); f[16:18] = struct.pack(">H", folder); bad.append(f)
        bad += [good[:116], good + b"\x00", bytes(good[:12]) + bytes.fromhex("12ff0100")]
        f = bytearray(good); f[12] = 0x13; bad.append(f)                             # another command
        f = bytearray(good); f[0] = 0x01; bad.append(f)                              # prefix
        for f in bad:
            self.assertFalse(policy.delete_frame_ok(bytes(f)), bytes(f).hex())
            self.assertFalse(policy.allow_out(policy.REQTYPE_OUT, policy.REQ_COMMAND, policy.WVALUE, 0, bytes(f)))
        for folder, number in ((0, 1), (6, 1), (1, 0), (1, 100)):
            with self.assertRaises(ValueError):
                policy.delete_frame(folder, number)

    def test_a_14_byte_reply_may_be_read(self):
        self.assertTrue(policy.allow_in(policy.REQTYPE_IN, policy.REQ_REPLY, policy.WVALUE, 0, 14))
        self.assertFalse(policy.allow_in(policy.REQTYPE_IN, policy.REQ_REPLY, policy.WVALUE, 0, 15))


class ProtocolTests(unittest.TestCase):
    def test_counts(self):
        r = recorder()
        self.assertEqual(folder_counts(r.target_status()), {1: 3, 2: 0, 3: 0, 4: 0, 5: 0})

    def test_delete_message_returns_the_status_and_the_device_compacts(self):
        r = recorder()
        self.assertEqual(r.delete_message(1, 2), 0)
        self.assertEqual(r.dev.delete_calls, [(1, 2)])
        self.assertEqual([m[1] for m in r.dev.folders[1]], [1, 3])          # counters: the second went
        self.assertEqual([m[0] for m in r.dev.folders[1]], [0, 1])          # slots renumbered
        self.assertEqual(folder_counts(r.target_status())[1], 2)

    def test_a_refusal_is_the_status_byte(self):
        r = recorder()
        r.dev.delete_status = 1
        self.assertEqual(r.delete_message(1, 2), 1)
        self.assertEqual(len(r.dev.folders[1]), 3)

    def test_a_recorder_that_never_answers_is_stuck(self):
        r = recorder()
        r.dev.stuck_on_delete = True
        import sony_icd.protocol as p
        old = p.DELETE_TIMEOUT_S
        p.DELETE_TIMEOUT_S = 0.05
        try:
            with self.assertRaises(RecorderStuck):
                r.delete_message(1, 2)
        finally:
            p.DELETE_TIMEOUT_S = old


from sony_icd.session import RecorderSession  # noqa: E402


def session(folders=THREE):
    s = RecorderSession(recorder(folders))
    s.connect()
    return s


def ident(s, letter, number):
    m = s.messages(letter)[number - 1]
    return dict(slot=m.slot, counter=m.start_counter, length=m.length)


class SessionTests(unittest.TestCase):
    def test_deletes_the_one_picked_and_forgets_the_old_listing(self):
        s = session()
        picked = ident(s, "A", 2)
        s.download("A", 3)                                  # cached under its old number
        self.assertEqual(s.delete("A", 2, **picked), "A-002")
        self.assertEqual([m.start_counter for m in s.messages("A")], [1, 3])
        self.assertEqual(s.rec.dev.voice_calls[-1:], [(1, 3)])
        s.download("A", 2)                                  # read again, not the cached old A-002
        self.assertEqual(s.rec.dev.voice_calls[-1], (1, 2))

    def test_a_changed_recording_is_refused_with_nothing_sent(self):
        s = session()
        picked = ident(s, "A", 2)
        picked["length"] += 1
        with self.assertRaises(ValueError) as cm:
            s.delete("A", 2, **picked)
        self.assertEqual(str(cm.exception), "A-002 changed on the recorder; refresh and try again.")
        self.assertEqual(s.rec.dev.delete_calls, [])

    def test_a_number_past_the_end_is_refused(self):
        s = session()
        with self.assertRaisesRegex(ValueError, "changed on the recorder"):
            s.delete("A", 4, slot=3, counter=4, length=6000)
        self.assertEqual(s.rec.dev.delete_calls, [])

    def test_counts_that_disagree_with_the_table_send_nothing(self):
        import sony_icd.session as ss
        s = session()
        picked = ident(s, "A", 2)
        real = ss.folder_counts
        ss.folder_counts = lambda reply: {k: v + 1 for k, v in real(reply).items()}
        try:
            with self.assertRaisesRegex(ValueError, "changed on the recorder"):
                s.delete("A", 2, **picked)
        finally:
            ss.folder_counts = real
        self.assertEqual(s.rec.dev.delete_calls, [])

    def test_a_different_slot_or_counter_is_refused_with_nothing_sent(self):
        for field in ("slot", "counter"):
            s = session()
            picked = ident(s, "A", 2)
            picked[field] += 1
            with self.assertRaisesRegex(ValueError, "changed on the recorder"):
                s.delete("A", 2, **picked)
            self.assertEqual(s.rec.dev.delete_calls, [])

    def test_batteries_and_other_refusals(self):
        for status, words in ((1, "Replace the recorder's batteries, then try again."),
                              (3, "The recorder refused (status 3). Nothing else was deleted.")):
            s = session()
            s.rec.dev.delete_status = status
            with self.assertRaises(ValueError) as cm:
                s.delete("A", 2, **ident(s, "A", 2))
            self.assertEqual(str(cm.exception), words)

    def test_a_result_that_is_not_one_fewer_is_reported(self):
        s = session()
        picked = ident(s, "A", 2)
        dev = s.rec.dev
        real = dev.control_out

        def deletes_two(rt, rq, v, i, data, *rest):
            real(rt, rq, v, i, data, *rest)
            if policy.delete_frame_ok(data):
                del dev.folders[1][-1]                      # the recorder lost one more
        dev.control_out = deletes_two
        with self.assertRaises(ValueError) as cm:
            s.delete("A", 2, **picked)
        self.assertEqual(str(cm.exception), "After deleting A-002 the recorder lists 1 recording in folder A, "
                                            "not 2. Check the folder before deleting more.")

    def test_a_delete_that_goes_unanswered_says_it_may_have_happened(self):
        import sony_icd.protocol as p
        s = session()
        s.rec.dev.stuck_on_delete = True
        picked = ident(s, "A", 2)
        old = p.DELETE_TIMEOUT_S
        p.DELETE_TIMEOUT_S = 0.05
        try:
            with self.assertRaises(RecorderError) as cm:
                s.delete("A", 2, **picked)
        finally:
            p.DELETE_TIMEOUT_S = old
        self.assertIs(type(cm.exception), RecorderError)
        self.assertEqual(str(cm.exception), UNCLEAR)
        self.assertEqual(s.rec.dev.delete_calls, [(1, 2)])                   # sent once, never retried

    def test_a_recorder_lost_during_the_after_check_says_it_may_have_happened(self):
        import sony_icd.session as ss
        s = session()
        picked = ident(s, "A", 2)
        real = ss.folder_counts
        calls = []

        def counts(reply):
            calls.append(1)
            if len(calls) > 1:
                raise RecorderError("gone")
            return real(reply)
        ss.folder_counts = counts
        try:
            with self.assertRaises(RecorderError) as cm:
                s.delete("A", 2, **picked)
        finally:
            ss.folder_counts = real
        self.assertEqual(str(cm.exception), UNCLEAR)

    def test_the_recorder_dropping_a_different_one_is_reported(self):
        s = session()
        picked = ident(s, "A", 3)
        dev = s.rec.dev
        real = dev.control_out

        def drops_the_second(rt, rq, v, i, data, *rest):
            if policy.delete_frame_ok(data):
                data = policy.delete_frame(1, 2)           # the fake removes A-002, not A-003
            real(rt, rq, v, i, data, *rest)
        dev.control_out = drops_the_second
        with self.assertRaises(ValueError) as cm:
            s.delete("A", 3, **picked)
        self.assertEqual(str(cm.exception), "After deleting A-003 the recorder lists different recordings in "
                                            "folder A than expected. Check the folder before deleting more.")


from openevp.recorders import base, get as model  # noqa: E402
from openevp.recorders.sony_st25 import SonyIcdSession  # noqa: E402


class ModelTests(unittest.TestCase):
    def test_only_the_st10_can_delete(self):
        self.assertTrue(model("sony-icd-st10").can_delete)
        self.assertFalse(model("sony-icd-st25").can_delete)
        self.assertFalse(base.Model.can_delete)

    def test_rows_carry_a_key_and_delete_takes_it(self):
        s = SonyIcdSession(session())
        rows = s.recordings("A")
        self.assertEqual(rows[1]["key"], "1:2:4000")
        self.assertEqual(s.delete("A", 2, rows[1]["key"]), "A-002")
        self.assertEqual(len(s.recordings("A")), 2)

    def test_a_malformed_key_is_refused(self):
        s = SonyIcdSession(session())
        for key in ("", "1:2", "a:b:c", None, 5):
            with self.assertRaises(ValueError):
                s.delete("A", 2, key)


import threading  # noqa: E402
import test_backend  # noqa: E402  (the module: importing BackendTests would collect its tests here)


class ApiTests(unittest.TestCase):
    def setUp(self):
        # The recorder is opened by the first device refresh (inside BackendTests.setUp), so it must
        # identify as an ICD-ST10 from the start: swap in an ST10 fake for that setUp.
        real = test_backend.FakeRecorderDevice
        test_backend.FakeRecorderDevice = lambda folders, **kw: real(folders, identify="ICD-ST10", **kw)
        try:
            test_backend.BackendTests.setUp(self)
        finally:
            test_backend.FakeRecorderDevice = real
        self.assertEqual(self.api.recordings(test_backend.ID)["model_id"], "sony-icd-st10")

    def items(self, *numbers):
        rows = {r["number"]: r for r in self.api.recordings(test_backend.ID)["folders"][0]["recordings"]}
        return [{"folder": "A", "number": n, "key": rows[n]["key"]} for n in numbers]

    def run_job(self, items):
        done = threading.Event()
        old = self.api._emit
        self.busy_at_end = []

        def emit(e, p):
            if e.startswith("recorder-delete-") and not e.endswith("progress"):
                self.busy_at_end.append(self.api._busy.locked())
            old(e, p)
            if e.startswith("recorder-delete-") and not e.endswith("progress"):
                done.set()
        self.api._emit = emit
        r = self.api.delete_from_recorder(test_backend.ID, items, 7)
        self.assertTrue(r["ok"], r)
        self.assertTrue(done.wait(5))
        return [(e, p) for e, p in self.events if e.startswith("recorder-delete-")]

    def test_the_st10_can_and_the_st25_cannot(self):
        self.assertTrue(self.api.recordings(test_backend.ID)["can_delete"])

    def test_deletes_highest_first_and_forgets_the_caches(self):
        n = len(self.dev.folders[1])
        events = self.run_job(self.items(1, 2))
        self.assertEqual(self.dev.delete_calls, [(1, 2), (1, 1)])
        self.assertEqual(events[-1], ("recorder-delete-done", {"job": 7, "deleted": ["A-002", "A-001"]}))
        self.assertEqual(len(self.dev.folders[1]), n - 2)
        self.assertIn(test_backend.ID, self.server.forgotten)
        self.assertEqual(self.busy_at_end, [False])          # the page hears it is over only once the lock is free

    def test_an_unclear_delete_reports_it_and_the_recorder_needs_a_replug(self):
        import sony_icd.protocol as p
        self.dev.stuck_on_delete = True
        old = p.DELETE_TIMEOUT_S
        p.DELETE_TIMEOUT_S = 0.05
        try:
            events = self.run_job(self.items(2))
        finally:
            p.DELETE_TIMEOUT_S = old
        name, ev = events[-1]
        self.assertEqual(name, "recorder-delete-failed")
        self.assertEqual(ev["error"], UNCLEAR)
        self.assertEqual(self.dev.delete_calls, [(1, 2)])
        rows = {d["id"]: d for d in self.api.devices()["devices"]}
        self.assertEqual(rows[test_backend.ID]["state"], "needs_replug")

    def test_stopped_by_the_app_closing_mid_job(self):
        real = self.api._manager.with_session

        def then_stop(device_id, fn):
            out = real(device_id, fn)
            self.api._stop.set()                             # the app closes after the first delete
            return out
        items = self.items(1, 2)
        self.api._manager.with_session = then_stop
        events = self.run_job(items)
        self.assertEqual(self.dev.delete_calls, [(1, 2)])
        name, ev = events[-1]
        self.assertEqual((name, ev["error"], ev["deleted"]), ("recorder-delete-failed",
                                                              "Stopped because the app is closing.", ["A-002"]))

    def test_the_caches_of_that_device_are_gone_after_a_job(self):
        self.items(1)
        self.api._natives[(test_backend.ID, "A", 1)] = object()
        self.assertIn(test_backend.ID, self.api._listings)
        self.run_job(self.items(1))
        self.assertNotIn(test_backend.ID, self.api._listings)
        self.assertFalse([k for k in self.api._natives if k[0] == test_backend.ID])

    def test_a_job_over_two_folders_goes_highest_first_in_each(self):
        self.api._listings.pop(test_backend.ID)
        self.api._manager._devices[test_backend.ID].session._session._tables.clear()      # B was read empty at setUp
        self.dev.folders[2] = [(0, 200, 0x5000, 3000, DATE, "Casey"), (1, 300, 0x6000, 3000, DATE, "Casey")]
        rows = {}
        for f in self.api.recordings(test_backend.ID)["folders"]:
            for r in f["recordings"]:
                rows[(f["id"], r["number"])] = r["key"]
        items = [{"folder": f, "number": n, "key": rows[(f, n)]} for f, n in (("A", 1), ("B", 1), ("A", 2), ("B", 2))]
        events = self.run_job(items)
        self.assertEqual(self.dev.delete_calls, [(1, 2), (1, 1), (2, 2), (2, 1)])
        self.assertEqual(events[-1][0], "recorder-delete-done")

    def test_a_backup_running_refuses_deleting(self):
        self.api.backing_up = lambda: True
        self.assertEqual(self.api.delete_info_recorder(test_backend.ID, self.items(1))["error"], BUSY)
        r = self.api.delete_from_recorder(test_backend.ID, self.items(1), 7)
        self.assertEqual((r["ok"], r["error"]), (False, BUSY))
        self.assertEqual(self.dev.delete_calls, [])
        self.assertFalse(self.api._busy.locked())

    def test_a_stale_key_is_refused_before_anything(self):
        items = self.items(1)
        items[0]["key"] = "9:9:9"
        r = self.api.delete_from_recorder(test_backend.ID, items, 7)
        self.assertFalse(r["ok"])
        self.assertEqual(self.dev.delete_calls, [])

    def test_stops_at_the_first_refusal(self):
        self.dev.delete_status = 1
        events = self.run_job(self.items(1, 2))
        self.assertEqual(self.dev.delete_calls, [(1, 2)])
        name, p = events[-1]
        self.assertEqual((name, p["deleted"]), ("recorder-delete-failed", []))
        self.assertEqual(p["error"], "Replace the recorder's batteries, then try again.")

    def test_info_says_which_are_saved(self):
        info = self.api.delete_info_recorder(test_backend.ID, self.items(1))
        self.assertTrue(info["ok"], info)
        self.assertEqual((info["items"][0]["label"], info["items"][0]["saved"]), ("A-001", False))


    def test_audio_is_refused_during_a_delete(self):
        with self.api._recs_lock:
            self.api._deleting.add(test_backend.ID)
        r = self.api.audio(test_backend.ID, "A", 1)
        self.assertEqual((r["ok"], r["error"]), (False, "Wait until the recorder has finished deleting."))

    def test_a_play_that_overlaps_a_delete_leaves_no_cache(self):
        real = self.server.prepare

        def prepare(key, make=None, write=None):
            self.api._delete_gen[test_backend.ID] = self.api._delete_gen.get(test_backend.ID, 0) + 1
            return real(key, make=make, write=write)
        self.server.prepare = prepare
        r = self.api.audio(test_backend.ID, "A", 1)
        self.assertEqual((r["ok"], r["error"]), (False, "The recorder's list changed while this was opening; "
                                                        "pick the recording again."))
        self.assertNotIn((test_backend.ID, "A", 1), self.api._natives)
        self.assertIn(test_backend.ID, self.server.forgotten)

    def test_info_is_refused_while_busy_and_dedupes(self):
        items = self.items(1)
        self.assertEqual(len(self.api.delete_info_recorder(test_backend.ID, items + items)["items"]), 1)
        self.api._busy.acquire()
        try:
            r = self.api.delete_info_recorder(test_backend.ID, items)
            self.assertEqual(r["error"], "Something else is using the recorder; try again when it finishes.")
        finally:
            self.api._busy.release()


class ApiSt25Tests(unittest.TestCase):
    """The default test_backend recorder is an ICD-ST25: the Api refuses, nothing is sent."""
    def setUp(self):
        test_backend.BackendTests.setUp(self)

    def test_the_st25_is_refused_with_nothing_sent(self):
        self.assertFalse(self.api.recordings(test_backend.ID)["can_delete"])
        rows = self.api.recordings(test_backend.ID)["folders"][0]["recordings"]
        r = self.api.delete_from_recorder(test_backend.ID, [{"folder": "A", "number": 1, "key": rows[0]["key"]}], 7)
        self.assertFalse(r["ok"])
        self.assertEqual(self.dev.delete_calls, [])


if __name__ == "__main__":
    unittest.main()
