#!/usr/bin/env python3
"""Dev: compare generated .dvf files with Digital Voice Editor's own files.

    python3 tools/compare_dvf.py <generated_dir> <dve_dir>
Reports identical files, and for the rest which bytes differ (header vs audio).
"""
import glob
import os
import sys

gen, ref = sys.argv[1], sys.argv[2]
same = 0
for g in sorted(glob.glob(os.path.join(gen, "*.dvf"))):
    r = os.path.join(ref, os.path.basename(g))
    if not os.path.exists(r):
        print(f"{os.path.basename(g)}: no DVE file with this name")
        continue
    a, b = open(g, "rb").read(), open(r, "rb").read()
    if a == b:
        same += 1
        continue
    diff = [i for i in range(min(len(a), len(b))) if a[i] != b[i]]
    hdr = [i for i in diff if i < 1024]
    aud = [i for i in diff if i >= 1024]
    kinds = sorted({(i - 1024) % 1024 for i in aud})
    print(f"{os.path.basename(g)}: len {len(a)} vs {len(b)}, header diffs {hdr[:8]}, "
          f"audio diffs {len(aud)} at block offsets {kinds[:8]}")
print(f"{same} identical")
