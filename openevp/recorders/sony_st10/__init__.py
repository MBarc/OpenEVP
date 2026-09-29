"""Sony ICD-ST10: downloads only, playback coming.

The ICD-ST10 answers to the ICD-ST25's USB id (054C:0103) and speaks the same
protocol, with the same folder tables and read-only command policy, so it is
found and opened by the ST25 model (openevp.recorders.sony_st25): its session
reports model_id "sony-icd-st10" from the recorder's identify string
("ICD-ST10"), and the app shows and treats the recorder as this model from
then on. That is why this model claims no USB ids and no driver of its own
(the registry allows one claim per USB id; the ST25's driver covers both).

Its recordings are LPEC ST (44.1 kHz stereo; see st25/dvf.py): they are
listed and downloaded as .dvf files, which a later LPEC ST decoder can play
without downloading them again. Until then wav_problem() says they can't be
played, and the .dvf format refuses to decode them (formats.ST10_NOT_YET).
"""
from openevp import formats
from openevp.recorders import base
from openevp.recorders.sony_st25 import open_session


class SonyST10(base.Model):
    model_id = "sony-icd-st10"
    name = "Sony ICD-ST10"
    native = formats.DVF

    def wav_problem(self):
        return formats.ST10_NOT_YET

    def open(self, device):
        return open_session(self, device)
