#!/usr/bin/env python3
"""Dev: search for the formula behind DVF header bytes 153..155."""
import glob
import struct
import zlib

F = {}
for m in (1, 2, 3, 4, 5, 16, 17, 18):
    F[m] = open(sorted(glob.glob(f"dvf/*A_{m:03d}*"))[0], "rb").read()
tgt = {m: int.from_bytes(d[153:156], "big") for m, d in F.items()}
print("target", tgt)


def f465(d):
    return int.from_bytes(d[465:468], "big")


def body(d):
    return d[1024:]


def blocks(d):
    return [body(d)[k:k + 1024] for k in range(0, len(body(d)), 1024)]


tests = {
    "sum bytes body %1024": lambda d: sum(body(d)) % 1024,
    "sum bytes body %65536": lambda d: sum(body(d)) % 65536,
    "sum16 BE body %1024": lambda d: sum(struct.unpack(f">{len(body(d)) // 2}H", body(d))) % 1024,
    "sum bytes whole %1024": lambda d: sum(d) % 1024,
    "f465 %1024": lambda d: f465(d) % 1024,
    "ms = f465*4/3 %1024": lambda d: (f465(d) * 4 // 3) % 1024,
    "ms = f465*4/3 %65536": lambda d: (f465(d) * 4 // 3) % 65536,
    "sum first2 %1024": lambda d: sum(struct.unpack(">H", b[:2])[0] for b in blocks(d)) % 1024,
    "last block valid": lambda d: struct.unpack(">H", blocks(d)[-1][4:6])[0],
    "last block valid*?": lambda d: (struct.unpack(">H", blocks(d)[-1][4:6])[0] - 10) % 1024,
    "(f465) mod 1014": lambda d: f465(d) % 1014,
    "crc32 body & 0xffff": lambda d: zlib.crc32(body(d)) & 0xFFFF,
}
for name, fn in tests.items():
    vals = {m: fn(d) for m, d in F.items()}
    ok = vals == tgt
    print(f"{name:26s} {'MATCH' if ok else '     '} {[vals[m] for m in (1, 2, 3, 4)]}")
print("full values:")
for m, d in F.items():
    print(m, tgt[m], hex(tgt[m]), "f465", f465(d), "blocks", len(blocks(d)),
          "last valid", struct.unpack(">H", blocks(d)[-1][4:6])[0], "hdr152-160", d[150:162].hex())
