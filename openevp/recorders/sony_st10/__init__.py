"""Sony ICD-ST10: download, play, WAV export and EVP marks.

The ICD-ST10 answers to the ICD-ST25's USB id (054C:0103) and speaks the same
protocol, with the same folder tables and read-only command policy, so it is
found and opened by the ST25 model (openevp.recorders.sony_st25): its session
reports model_id "sony-icd-st10" from the recorder's identify string
("ICD-ST10"), and the app shows and treats the recorder as this model from
then on. That is why this model claims no USB ids and no driver of its own
(the registry allows one claim per USB id; the ST25's driver covers both).

Its recordings are LPEC ST (44.1 kHz stereo; see st25/dvf.py), downloaded as
.dvf files and decoded by openevp.decoders.sony_lpec_st (the .dvf format
picks the decoder by the file's codec byte). wav_problem() is that decoder's
problem: None when it can run, else why not (e.g. a build without its table
data), so the app greys out playback and WAV export with the reason.
"""
from openevp import formats
from openevp.recorders import base
from openevp.recorders.sony_st25 import open_session


class SonyST10(base.Model):
    model_id = "sony-icd-st10"
    name = "Sony ICD-ST10"
    native = formats.DVF

    def wav_problem(self):
        return formats.codec_problem(formats.CODEC_ST)

    def open(self, device):
        return open_session(self, device)
