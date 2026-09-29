"""Sony ICD-ST10: download, play, WAV export and EVP marks.

The ICD-ST10 answers to the ICD-ST25's USB id (054C:0103) and speaks the same
protocol, with the same folder tables and read-only command policy, so it is
found and opened by the ST25 model (openevp.recorders.sony_st25): its session
reports model_id "sony-icd-st10" from the recorder's identify string
("ICD-ST10"), and the app shows and treats the recorder as this model from
then on. That is why this model claims no USB ids and no driver of its own
(the registry allows one claim per USB id; the ST25's driver covers both).

Its recordings are in one of three modes, per recording (see st25/dvf.py):
LPEC ST (44.1 kHz stereo, openevp.decoders.sony_lpec_st), LPEC LP (the
ICD-ST25's codec, openevp.decoders.sony_lpec) and LPEC SP (16 kHz mono, no
decoder yet). All are downloaded as .dvf files; the .dvf format picks the
decoder by each file's codec byte. So wav_problem() is the base model's (the
.dvf format's), and each recording row's play_problem says why that one
cannot be played (e.g. "LPEC SP (16 kHz) audio can't be played yet", or a
build without the LPEC ST decoder's table data).
"""
from openevp import formats
from openevp.recorders import base
from openevp.recorders.sony_st25 import open_session


class SonyST10(base.Model):
    model_id = "sony-icd-st10"
    name = "Sony ICD-ST10"
    native = formats.DVF

    def open(self, device):
        return open_session(self, device)
