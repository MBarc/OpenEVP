"""The recorder models OpenEVP knows about: a static registry.

MODELS lists one instance of every model module (supported and placeholder).
It is checked when this package is imported: model ids must be unique, no
USB (vid, pid) may be claimed twice (an ambiguous match is an error, never a
guess), a supported model needs a registered native Format, and a placeholder claims no
USB ids and no driver. register()/unregister() exist for tests (fake models).

See openevp.recorders.base for the interface a model implements.
"""
from openevp import formats

from .base import Model, RejectedConnection
from .panasonic_rrdr60 import PanasonicRRDR60
from .sony_st10 import SonyST10
from .sony_st25 import SonyST25

# The shipped models, one instance each (supported and placeholders).
MODELS = [SonyST25(), SonyST10(), PanasonicRRDR60()]


def check(models):
    """Return models as a list if they can be registered together, else
    raise ValueError saying why."""
    models = list(models)
    ids, claims = set(), {}
    for m in models:
        if not isinstance(m, Model):
            raise ValueError(f"{m!r} is not a recorder Model")
        if not isinstance(m.model_id, str) or not m.model_id or not isinstance(m.name, str) or not m.name:
            raise ValueError(f"{m!r} needs a model_id and a name")
        if m.model_id in ids:
            raise ValueError(f"two recorder models are called {m.model_id}")
        ids.add(m.model_id)
        if m.supported:
            if not isinstance(m.native, formats.Format):
                raise ValueError(f"{m.model_id} has no native format")
            if formats.by_ext(m.native.ext) is not m.native:
                raise ValueError(f"{m.model_id}: its native format {m.native.ext} is not registered")
        elif m.usb_ids or m.needs_winusb:
            raise ValueError(f"placeholder {m.model_id} cannot claim USB ids or a driver")
        for pair in m.usb_ids:
            if (len(pair) != 2 or not all(isinstance(n, int) and 0 <= n <= 0xFFFF for n in pair)):
                raise ValueError(f"{m.model_id}: bad USB id {pair!r}")
            key = tuple(pair)
            if key in claims:
                raise ValueError(f"USB id {key[0]:04x}:{key[1]:04x} is claimed by both "
                                 f"{claims[key]} and {m.model_id}")
            claims[key] = m.model_id
    return models


_models = check(MODELS)


def models():
    """Every registered model, placeholders included."""
    return list(_models)


def supported():
    """The models OpenEVP can actually read."""
    return [m for m in _models if m.supported]


def get(model_id):
    """The model with this id, or None."""
    return next((m for m in _models if m.model_id == model_id), None)


def find(vid, pid):
    """The supported model that answers to this USB id, or None."""
    return next((m for m in supported() if (vid, pid) in [tuple(p) for p in m.usb_ids]), None)


def discover_all():
    """(devices, problems): every attached recorder of every supported model,
    as DiscoveredDevice records in registry order, and [(model_id, exception)]
    for the models whose discovery failed. One broken model never hides the
    others. A connection id reported more than once (ids must be unique across
    models) is ambiguous: every device with it is dropped and each reporting
    model gets a base.RejectedConnection problem (a ValueError naming the id),
    which tells the app to close and forget that connection rather than keep
    it as it does for a model whose discovery failed."""
    found, problems = [], []
    for m in supported():
        try:
            found.extend(m.discover())
        except Exception as e:
            problems.append((m.model_id, e))
    counts = {}
    for d in found:
        counts[d.connection_id] = counts.get(d.connection_id, 0) + 1
    for d in found:
        if counts[d.connection_id] > 1:
            problems.append((d.model_id, RejectedConnection(
                f"more than one recorder reports the connection id {d.connection_id!r}", d.connection_id)))
    return [d for d in found if counts[d.connection_id] == 1], problems


def register(model):
    """Add a model (tests only; shipped models go in MODELS). Returns it."""
    global _models
    _models = check(_models + [model])
    return model


def unregister(model_id):
    global _models
    _models = [m for m in _models if m.model_id != model_id]
