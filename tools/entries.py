#!/usr/bin/env python3
"""Dev: dump the message-entry pages of a folder table (528-byte NAND pages)."""
import struct
import sys

t = open(sys.argv[1] if len(sys.argv) > 1 else "probe-out/folder1_table.bin", "rb").read()
PAGE = 528
pages = [t[i:i + PAGE] for i in range(0, len(t), PAGE)]
for n, p in enumerate(pages):
    if b"Casey" not in p and p[:512].count(0xFF) > 500:
        continue
    date = p[452:460]
    name = p[272:304].split(b"\0")[0]
    print(f"page {n:3d} @{n * PAGE:6d} first16={p[:16].hex()} date={date.hex()} "
          f"name={name!r} 448-452={p[448:452].hex()} 480-496={p[480:496].hex()} spare={p[512:528].hex()}")
print("page0:", pages[0][:96].hex())
print("page1:", pages[1][:64].hex())
