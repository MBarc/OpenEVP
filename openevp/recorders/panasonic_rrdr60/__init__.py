"""Panasonic RR-DR60: planned, not supported yet.

A placeholder so the registry and docs show where the model goes. Nothing is
known yet about its USB ids, protocol or file format (no hardware, software
or sample recordings to study), so it claims no USB ids, needs no driver and
is never discovered or opened.
"""
from openevp.recorders import base


class PanasonicRRDR60(base.Model):
    model_id = "panasonic-rr-dr60"
    name = "Panasonic RR-DR60"
    supported = False
