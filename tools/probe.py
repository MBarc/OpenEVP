#!/usr/bin/env python3
"""Development probe: replay Digital Voice Editor's connect sequence and dump
every reply and folder table in full, so the formats can be decoded.

    python3 tools/probe.py <outdir>

Read-only like the downloader: it only sends the commands sony_icd/policy.py allows.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sony_icd.protocol import CMD_FOLDER_INFO, FOLDER_TABLE_SIZE, Recorder  # noqa: E402


def probe(r, out):
    os.makedirs(out, exist_ok=True)

    def save(name, data):
        with open(os.path.join(out, name), "wb") as f:
            f.write(data)
        print(f"{name:28s} {len(data):6d} B  {data[:48].hex()}")

    print("status:", r.status().hex())
    save("device_info.bin", r.device_info())
    save("block_0.bin", r.read_block(0x1E0, 0))
    save("block_1e0.bin", r.read_block(0x1E0, 0x1E0))
    save("info_03.bin", r.info_03())
    save("target_status.bin", r.target_status())
    for folder in range(1, 6):
        # query_bulk, not folder_table(): the probe keeps the ack and completion replies too.
        ack, table, done = r.query_bulk([CMD_FOLDER_INFO | (folder << 8), 0, 0], 16, FOLDER_TABLE_SIZE)
        save(f"folder{folder}_ack.bin", ack)
        save(f"folder{folder}_table.bin", table)
        save(f"folder{folder}_done.bin", done)
    print("status:", r.status().hex())


def main(argv):
    out = argv[1] if len(argv) > 1 else "probe-out"
    with Recorder() as r:
        probe(r, out)


if __name__ == "__main__":
    main(sys.argv)
