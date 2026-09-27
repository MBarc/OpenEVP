"""Sony ICD-ST10: planned, not supported yet.

A placeholder so the registry and docs show where the model goes. Nothing is
known yet about its USB ids, protocol or file format (no hardware to study),
so it claims no USB ids, needs no driver and is never discovered or opened.
"""
from openevp.recorders import base


class SonyST10(base.Model):
    model_id = "sony-icd-st10"
    name = "Sony ICD-ST10"
    supported = False
