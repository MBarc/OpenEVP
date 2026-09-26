"""Sign a release installer for the in-app updater (app/updater.py).

    python tools/sign_release.py dist/OpenEVP-Setup-0.7.0.exe   # writes ...exe.sig
    python tools/sign_release.py --new-key                       # once: make the key

The private key lives only in hush (HUSH_PATH below); this machine reads it as
its enrolled device (X-Hush-Device) and never writes it to disk. The public key
is compiled into the app (app/updater.py PUBLIC_KEY). Needs `cryptography`
(a development dependency; the app itself only verifies, in pure Python).
"""
import argparse
import base64
import hashlib
import json
import os
import re
import socket
import sys
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import ed25519, updater  # noqa: E402

HUSH = "http://hush.local:4874/api/v1"
HUSH_PATH = "Projects/OpenEVP/Release signing key"


def _hush(method, body=None):
    req = urllib.request.Request(f"{HUSH}/secrets/{urllib.parse.quote(HUSH_PATH)}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Hush-Device": socket.gethostname().lower(),
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def _raw_public(key):
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def new_key():
    try:
        _hush("GET")
        sys.exit(f"hush already holds {HUSH_PATH!r}; refusing to replace the release key.")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    _hush("PUT", {"value": seed.hex()})
    print("Stored the private key in hush at", HUSH_PATH)
    print("Public key (put this in app/updater.py PUBLIC_KEY):", _raw_public(key).hex())


def load_key():
    reply = _hush("GET")
    seed = bytes.fromhex(reply["value"])
    return Ed25519PrivateKey.from_private_bytes(seed)


def sign(installer):
    m = re.fullmatch(r"OpenEVP-Setup-(\d+\.\d+\.\d+)\.exe", os.path.basename(installer))
    if not m:
        sys.exit("expected an installer named OpenEVP-Setup-<x.y.z>.exe")
    digest = hashlib.sha256()
    with open(installer, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    message = updater.signed_message(m.group(1), digest.hexdigest())
    key = load_key()
    signature = key.sign(message)
    public = _raw_public(key)
    if public != updater.PUBLIC_KEY:
        sys.exit("the key in hush does not match app/updater.py PUBLIC_KEY; not signing.")
    assert ed25519.verify(public, message, signature)
    with open(installer + ".sig", "w", encoding="ascii") as f:
        f.write(base64.b64encode(signature).decode() + "\n")
    print("Wrote", installer + ".sig")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("installer", nargs="?")
    p.add_argument("--new-key", action="store_true")
    a = p.parse_args()
    if a.new_key:
        new_key()
    elif a.installer:
        sign(a.installer)
    else:
        p.error("give an installer to sign, or --new-key")
