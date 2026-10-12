"""Delete from recorder: the one allowed write (policy and protocol). Synthetic data only."""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fixtures import DATE, FakeRecorderDevice  # noqa: E402
from sony_icd import policy  # noqa: E402
from sony_icd.protocol import Recorder, RecorderStuck, folder_counts  # noqa: E402

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
        for mode in (0x00, 0x02, 0x03, 0x04):          # folder / whole-recorder erase and the rest
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


if __name__ == "__main__":
    unittest.main()
