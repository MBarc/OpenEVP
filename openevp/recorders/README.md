# Adding a recorder

Every voice recorder OpenEVP reads is one module in this folder, listed in
`MODELS` in [`__init__.py`](__init__.py). The app never talks to a recorder
any other way. The registry is static: there is no plugin loading.

The full contract is the docstring of [`base.py`](base.py). This page is the
checklist. The Sony ICD-ST25 ([`sony_st25/`](sony_st25/__init__.py), over the
`st25` package) is the worked example, and `tests/fakes/fake_models.py` has two
small fake models that are not the ST25.

## 1. The model

Subclass `base.Model` and set:

| attribute | meaning |
|---|---|
| `model_id` | stable id, e.g. `"sony-icd-st25"`; never changes once shipped |
| `name` | display name, e.g. `"Sony ICD-ST25"` |
| `usb_ids` | `((vid, pid), ...)` it answers to. Two models may not claim the same id: the registry refuses to load |
| `needs_winusb` | `True` if the app's driver setup must bind it to WinUSB (see *Driver* below) |
| `winusb_name` | the name Device Manager shows for it (default `"<name> - WinUSB"`); plain ASCII, no quotes or `%` |
| `native` | its `openevp.formats.Format`: the file type it downloads (see *Formats*) |
| `supported` | `True` (only placeholders set `False`) |

`discover()` returns a `DiscoveredDevice` for every recorder of this model
attached now, including one that can't be used yet (`state=NEEDS_DRIVER`, with a
`message`). `open(device)` returns a `Session` for one of them.

## 2. The session

One connected recorder. The app uses it only on its single device thread.

- `owner`: the owner name the recorder reports, or `None`.
- `folders()`: `[{"id", "label", "safe_name"}]`.
- `recordings(folder_id)`: `[{"number", "recorded_label", "recorded_sort", "seconds", "owner", "problem"}]`.
- `download(folder_id, number)`: a `Download` with the native file's bytes, or its `error`.
- `close()`: releases the device. The app calls it exactly once.

Ids and labels are opaque. The app hands folder ids and recording numbers back
only to the session that listed them, checks them against that listing, and
never builds a path from them. Labels are shown as plain text. Only a folder's
`safe_name` and a download's `filename` become parts of a path: both must pass
`base.safe_name_ok()`, `safe_name` must be unique ignoring case, and
`filename` must end with the native extension. An unknown id raises
`ValueError`.

A session only reads. It never decodes or writes files: the app downloads the
native bytes first, then converts and saves them after the call has returned.

## 3. Errors and device states

Raise these from `open()` and the session calls. `base.state_for()` maps them:

| exception | device state | session |
|---|---|---|
| `NotReady` | `needs_replug` | closed: the user replugs the recorder |
| `RecorderError` | `needs_replug` | closed: the connection failed |
| `DeviceGone` | `needs_replug` | closed: it is no longer attached |
| `DriverMissing` | `needs_driver` | kept (none could be opened) |
| `Cancelled` | unchanged | kept |
| anything else | unchanged | kept (e.g. `ValueError` for a bad id) |

Translate your transport's own exceptions into these, keeping their message
(`sony_st25/__init__.py` shows how). A problem with one recording is not an
exception: `download()` returns a `Download` with `error` set, and a batch
export carries on with the next recording.

## 4. Safety: a read-only allowlist in the transport

Nothing on a recorder may ever be changed or deleted. Each model enforces its
own deny-by-default allowlist of the exact commands it needs to list and
download recordings, and it enforces it inside its transport. The caller does
not supply the policy, and there is no unchecked transfer method. The ST25's
is `st25/policy.py`, checked on every transfer by `st25/usb.py`'s `Device`.
The framework never sends raw commands.

Expected in review:

- the allowlist, with where each allowed command comes from (the
  manufacturer's own software doing the same read);
- tests that the transport refuses anything outside the list, and that
  `tests/fixtures.py`-style emulation of the device asserts the policy on
  every request it receives.

## 5. Formats

`native` is an `openevp.formats.Format`. Register it in `openevp/formats.py`
(the registry refuses a supported model whose format isn't registered). A
format supplies:

- `ext`: lowercase, e.g. `".dvf"`;
- `same(existing, new)`: whether an existing file already holds this
  recording (the ST25 compares the audio without its block counters). Saving
  never overwrites: a different file gets a numbered name;
- `seconds(path)`: its length, or `None`;
- `decoder` (optional): `available()`, `reason()`, `warning()`,
  `to_wav(data, should_stop)`. It raises `DecodeError` when the file is
  bad, `DecoderUnavailable` when the decoder can't run on this build, and
  `Cancelled` when stopped. Without a decoder, recordings are still listed and
  exported in their native format. They just can't be played, marked or
  converted.

EVP marks are keyed by a fingerprint of the decoded audio, so the same
recording is recognised whether it was loaded from the recorder, a native
file or a WAV.

## 6. Driver

If the recorder needs WinUSB (`needs_winusb = True`), the driver INF is
generated from the registry at build time by `tools/make_driver_manifest.py`
(written to `app/driver/models.json`). Placeholders and mass-storage recorders
never get an INF entry.

**DriverVer rule:** the INF must never change without a new driver version,
or Windows reinstalls the old package in its place. Adding a model, a USB id or
a device name changes the INF, so `tools/make_driver_manifest.py` and the tests
fail until you append an entry to `DRIVER_RELEASES` with a higher version,
the date and the new hash (the error prints the hash). Never edit an existing
entry.

## 7. Tests

- Run the shared contract against your model. Mix
  `tests/recorder_contract.py`'s `RecorderContract` into a `TestCase` whose
  `setUp` sets `self.model` and makes one ready recorder discoverable, as
  `tests/test_sony_st25_model.py` (`ST25Contract`) and `tests/test_recorders.py`
  (`FakeAlphaContract`, `FakeBetaContract`) do.
- Emulate the device (no hardware in the tests). Commit only synthetic data,
  never real recordings.
- A decoder needs golden tests: synthetic vectors with reference output,
  in the style of `tests/test_lpec_vectors.py`. Gate them with
  `tests/release_gate.py` (`require(...)`, not `unittest.skipUnless`), so the
  release build fails instead of skipping when the decoder can't run.
- `python -m unittest discover -s tests` must pass with no ResourceWarnings.

## Placeholders

A planned model with no code yet (`sony_st10/`, `panasonic_rrdr60/`) sets only
`model_id`, `name` and `supported = False`. It claims no USB ids and no driver,
is never discovered or opened, and is not shown in the app. The README lists it
as planned. To implement one, fill in its module following the steps above and
remove `supported = False`.
