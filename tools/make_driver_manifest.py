"""Write the driver manifest: app/driver/models.json (build time only).

    python tools/make_driver_manifest.py [--out PATH]

The recorder driver (Windows' WinUSB, bound by app/driver/install-winusb.ps1)
covers every registered model that is supported and has needs_winusb, with
all of its USB ids. Placeholders (no USB ids) and models that do not use
WinUSB (e.g. a future mass-storage recorder) never get an INF entry. This
script turns the registry (openevp.recorders) into that list and the INF text
itself, so the PowerShell scripts never hard-code a device:

    {"schema": 1, "driver_version": "1.0.1.0", "driver_date": "09/25/2026",
     "models": [{"model_id", "name", "hardware_ids": ["USB\\VID_054C&PID_0103"]}],
     "inf": "<the INF, CRLF line ends, no final line end>"}

models.json is a build output (git-ignored): build_windows.ps1 writes it before
freezing the app, which bundles it next to the scripts (_internal/driver).

DriverVer rule. Windows keeps a driver package per version, and the setup
reinstalls an already-present package of the same version instead of building
a new one, so the INF must never change without a new version. The INF body
(everything but its DriverVer line) is pinned by hash in DRIVER_RELEASES: when
it changes (a model added or removed, a USB id, a device name, the template),
this script and the test suite fail until a new entry is appended with a
higher version, the date of the change and the new hash (the error prints it).
The last entry is the one that ships; an earlier body is never shipped again
under its old version.

Package names: the INF/catalog keep their first names (st25_winusb.inf/.cat)
for every model, so install and uninstall recognise every package OpenEVP has
ever installed (v0.5.0 on) by one name.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openevp.pnp import hardware_id  # noqa: E402

OUT = ROOT / "app" / "driver" / "models.json"
SCHEMA = 1
INF_NAME = "st25_winusb.inf"        # also in install-winusb.ps1 / uninstall-winusb.ps1
CAT_NAME = "st25_winusb.cat"
PROVIDER = "OpenEVP"

# (driver version, DriverVer date MM/dd/yyyy, SHA-256 of the INF body). Append only.
DRIVER_RELEASES = [
    # v0.7.0: provider renamed to OpenEVP; the Sony ICD-ST25 only.
    ("1.0.1.0", "09/25/2026", "119fbd31bc9f99aeefa8203a4c645624007f245dee6219e188eceeb91b37a60b"),
]

# Printable ASCII without " or % (the INF is ASCII; " and % are INF syntax). Same rule in
# app/driver/manifest.ps1, for INF device names and the manifest's model ids and names.
_TEXT = re.compile(r'[ !#$&-~]{1,120}')


class ManifestError(Exception):
    pass


def winusb_models(models):
    """The models the driver covers, in registry order."""
    chosen = [m for m in models if m.supported and m.needs_winusb]
    for m in chosen:
        for field in ("model_id", "name"):
            if not isinstance(getattr(m, field), str) or not _TEXT.fullmatch(getattr(m, field)):
                raise ManifestError(f"{m.model_id!r}: {field} {getattr(m, field)!r} must be printable "
                                    "ASCII without quotes or %")
        if not m.usb_ids:
            raise ManifestError(f"{m.model_id} needs WinUSB but has no USB ids")
    return chosen


def device_name(model):
    name = model.winusb_name or f"{model.name} - WinUSB"
    if not isinstance(name, str) or not _TEXT.fullmatch(name):
        raise ManifestError(f"{model.model_id}: driver device name {name!r} must be printable ASCII "
                            "without quotes or %")
    return name


def _string_key(index):
    return "DeviceName" if index == 0 else f"DeviceName{index + 1}"


def inf_text(models, date, version):
    """The INF for these (WinUSB) models; CRLF line ends, no final line end.
    date/version None leaves the DriverVer line out (the pinned body)."""
    if not models:
        raise ManifestError("no recorder model needs WinUSB: nothing to put in the driver")
    entries, strings = [], []
    for i, m in enumerate(models):
        key = _string_key(i)
        strings.append(f'{key} = "{device_name(m)}"')
        entries += [f"%{key}% = USB_Install, {hardware_id(v, p)}" for v, p in m.usb_ids]
    lines = [
        "[Version]",
        'Signature   = "$Windows NT$"',
        "Class       = USBDevice",
        "ClassGUID   = {88BAE032-5A81-49f0-BC3D-A4FF138216D6}",
        "Provider    = %Provider%",
        f"CatalogFile = {CAT_NAME}",
    ]
    if date is not None:
        lines.append(f"DriverVer   = {date},{version}")
    lines += ["", "[Manufacturer]", "%Provider% = Devices, NTamd64, NTx86, NTarm64", ""]
    for arch in ("NTamd64", "NTx86", "NTarm64"):
        lines += [f"[Devices.{arch}]", *entries, ""]
    lines += [
        "[USB_Install]",
        "Include = winusb.inf",
        "Needs   = WINUSB.NT",
        "",
        "[USB_Install.Services]",
        "Include = winusb.inf",
        "Needs   = WINUSB.NT.Services",
        "",
        "[USB_Install.HW]",
        "AddReg = Dev_AddReg",
        "",
        "[Dev_AddReg]",
        'HKR,,DeviceInterfaceGUIDs,0x10000,"{7C3C3F6B-2E54-4F0B-9C1B-5D2A8B3E0A25}"',
        "",
        "[Strings]",
        f'Provider   = "{PROVIDER}"',
        *strings,
    ]
    return "\r\n".join(lines)


def body_hash(models):
    return hashlib.sha256(inf_text(models, None, None).encode("ascii")).hexdigest()


def _date(text):
    return datetime.datetime.strptime(text, "%m/%d/%Y").date()


def _version(text):
    return tuple(int(n) for n in text.split("."))


def check_releases(releases):
    """Raise ManifestError unless versions rise, dates never go back and no body repeats."""
    for (v0, d0, h0), (v1, d1, h1) in zip(releases, releases[1:]):
        if _version(v1) <= _version(v0):
            raise ManifestError(f"driver version {v1} must be higher than {v0}")
        if _date(d1) < _date(d0):
            raise ManifestError(f"driver date {d1} is before {d0}")
    hashes = [h for _, _, h in releases]
    if len(set(hashes)) != len(hashes):
        raise ManifestError("two driver releases pin the same INF")
    for v, d, _ in releases:
        if len(_version(v)) != 4:
            raise ManifestError(f"driver version {v} needs four parts")
        _date(d)


def release_for(models, releases=None):
    """(version, date) the INF for these models ships as; ManifestError if the
    INF changed without a new DRIVER_RELEASES entry."""
    releases = DRIVER_RELEASES if releases is None else releases
    check_releases(releases)
    digest = body_hash(models)
    version, date, pinned = releases[-1]
    if digest != pinned:
        raise ManifestError(
            f"the driver INF changed (body SHA-256 {digest}); append to DRIVER_RELEASES in "
            f"tools/make_driver_manifest.py a version higher than {version}, today's date "
            f"(MM/dd/yyyy) and that hash")
    return version, date


def manifest(models=None, releases=None):
    """The manifest dict for these registry models (default: the shipped registry)."""
    if models is None:
        from openevp import recorders
        models = recorders.models()
    chosen = winusb_models(models)
    version, date = release_for(chosen, releases)
    return {
        "schema": SCHEMA,
        "about": "Generated by tools/make_driver_manifest.py at build time; do not edit.",
        "driver_version": version,
        "driver_date": date,
        "models": [{"model_id": m.model_id, "name": m.name,
                    "hardware_ids": [hardware_id(v, p) for v, p in m.usb_ids]} for m in chosen],
        "inf": inf_text(chosen, date, version),
    }


def write(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="ascii", newline="\n") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args(argv)
    try:
        data = manifest()
    except ManifestError as e:
        print(f"make_driver_manifest: {e}", file=sys.stderr)
        return 1
    write(args.out, data)
    names = ", ".join(f"{m['name']} ({' '.join(m['hardware_ids'])})" for m in data["models"])
    print(f"{args.out}: driver {data['driver_version']} for {names}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
