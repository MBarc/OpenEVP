"""Golden bytes for the ICD-ST25 (LPEC LP) path, pinned at v0.8.2.

Adding another recording mode (the ICD-ST10's LPEC ST) must not change a
single byte OpenEVP writes for an ST25 recording, nor what it accepts or
fingerprints. The hashes below were computed with the v0.8.2 code (c989da4)
from synthetic data only (tests/fixtures.py, tests/vectors).
"""
import dataclasses
import glob
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fixtures import DATE, FakeRecorderDevice, make_raw, make_table  # noqa: E402
from st25 import dvf  # noqa: E402
from st25.folder import parse  # noqa: E402
from st25.protocol import Recorder  # noqa: E402
from st25.session import RecorderSession  # noqa: E402

VECTORS = os.path.join(os.path.dirname(__file__), "vectors")

# (length, first counter, date, owner) -> (sha256 of the .dvf, audio_fingerprint)
BUILDS = {
    (2958, 0x3AB79EC2, DATE, "Casey Q Hunters"): (
        "5fd24474bc143300a15c74807e97b94c38a131d82f28fe24dc4c8f5bf97ad4f9",
        "f43231cfd9f4c2a4b20d65d974388c722925fcded91d62cf4e55202d16a154b1"),
    (1024, 1, b"\xff" * 8, ""): (
        "bc7d2ec6c06ffdf0783309e3506b5c294b98fe3381ed698863a197118de6d9a3",
        "67b3b1cd28b044710e83809307bd8093ff46cec9b4e73fd4831d18d87954f08d"),
    (5137, 7, DATE, "X" * 40): (
        "934fb54f90e8e336cb0f2adaa767a5c0f1c473bc82152fe94139a1275bd603c8",
        "af8806c9dea3276846c8d72c11b91ffbeab164facf3fb7be61bcc8c26898eafc"),
    (50000, 0xFFFFFF00, b"\xff" * 8, "Unknown"): (
        "11e996dfb07439838423fa528ad77111c0dfc12704e605709e31544faa16161c",
        "4a27eeacf6feab61b5b97ab2e0946882228a1d061c3358e8a3e21d0753a40569"),
}

TABLE = [(0, 0x3AB79EC2, 0x1D0000, 2958, DATE, "Casey Q Hunters"),
         (70, 5, 0x200000, 5000, b"\xff" * 8, "Z"),
         (3, 9, 0x1000, 3000, DATE, "")]
TABLE_SHA = "0410ffdcf29b0bd48d7064d822554a89d5b744f3e6eb683179b0d71a9d582d90"

# tests/vectors/<name>.dvf -> (validate(), sha256 of payload(), audio_fingerprint)
VECTOR_SHAS = "4beaa0ef993657d4359fbaadea793cc2545e0cd3632441c0cc3e3c611260cc70"


def sha(b):
    return hashlib.sha256(b).hexdigest()


def parsed(table):
    msgs = parse(table)
    return [(m.number, m.slot, m.start_counter, m.length, m.blocks, m.date, m.owner, m.problem,
             round(m.seconds(), 6), m.dated, m.when()) for m in msgs]


class St25GoldenTests(unittest.TestCase):
    def test_builds_are_byte_identical(self):
        for (length, counter, date, owner), (want, fp) in BUILDS.items():
            f = dvf.build(make_raw(length, counter), date, owner, expected_length=length)
            self.assertEqual((sha(f), dvf.audio_fingerprint(f)), (want, fp), (length, owner))
            self.assertIsNone(dvf.validate(f))

    def test_session_download_is_byte_identical(self):
        dev = FakeRecorderDevice({1: [(0, 0x3AB79EC2, 0x1D0000, 2958, DATE, "Casey Q Hunters")]})
        r = Recorder.__new__(Recorder)
        r.dev = dev
        s = RecorderSession(r)
        s.connect()
        d = s.download("A", 1)
        self.assertEqual((d.name, d.error), ("001_A_001_Casey Q Hunters_2029_05_23.dvf", ""))
        self.assertEqual(sha(d.dvf), BUILDS[(2958, 0x3AB79EC2, DATE, "Casey Q Hunters")][0])

    def test_folder_table_parses_as_before(self):
        self.assertEqual(sha(repr(parsed(make_table(TABLE))).encode()), TABLE_SHA)

    def test_lp_file_claiming_lpec_st_is_rejected(self):
        # Byte 61 alone says LPEC ST: the rest of the header is LP's, so it is neither layout.
        f = bytearray(dvf.build(make_raw(2958, 100), DATE, "X", expected_length=2958))
        f[61] = 0x24
        self.assertIsNotNone(dvf.validate(bytes(f)))
        self.assertIsNone(dvf.audio_fingerprint(bytes(f)))

    def test_block_without_the_header_length_field_is_rejected(self):
        f = bytearray(dvf.build(make_raw(2958, 100), DATE, "X", expected_length=2958))
        f[1024 + 1024 + 2:1024 + 1024 + 4] = b"\x00\x0b"      # the second block's bytes 2..3
        self.assertIsNotNone(dvf.validate(bytes(f)))
        self.assertIsNone(dvf.audio_fingerprint(bytes(f)))

    def test_decoder_vectors_validate_and_fingerprint_as_before(self):
        got = {}
        for p in sorted(glob.glob(os.path.join(VECTORS, "*.dvf"))):
            with open(p, "rb") as f:
                b = f.read()
            got[os.path.basename(p)] = (dvf.validate(b), sha(dvf.payload(b)), dvf.audio_fingerprint(b))
        self.assertEqual(sha(repr(sorted(got.items())).encode()), VECTOR_SHAS)


if __name__ == "__main__":
    unittest.main()
