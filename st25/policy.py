"""The fixed USB traffic policy for the ICD-ST25, enforced inside usb.Device.

It lives in its own module (not supplied by callers) so that no code path -
including direct use of usb.Device - can send the recorder anything except the
exact command frames Digital Voice Editor sends to list and download
recordings. The recorder's command set also contains erase/delete/firmware
upgrade; none of those frames pass these checks.

Scope: this covers every transfer the program itself makes. Standard USB
lifecycle requests made by libusb/the OS (claim/release of interface 0, e.g. a
SET_INTERFACE on release) are not vendor commands and are outside it.
"""
import struct

REQTYPE_IN = 0xC1     # vendor | interface | device-to-host
REQTYPE_OUT = 0x41    # vendor | interface | host-to-device
WVALUE = 0xABAB
REQ_STATUS, REQ_COMMAND, REQ_REPLY = 0x01, 0x80, 0x81
EP_BULK_IN = 0x81

FRAME_PREFIX = bytes.fromhex("00e00008" "0046abab" "00000000")

CMD_DEVICE_INFO = 0x090001FF
CMD_READ_BLOCK = 0x090005FF       # args: length 0x1E0, offset 0 or 0x1E0
CMD_INFO_03 = 0x090003FF
CMD_TARGET_STATUS = 0x092000FF
CMD_FOLDER_INFO = 0x091000FF      # 0x0910NNff for folder NN = 1..5 (A..E)
# GET_VOICE: 0x11FF0000 | folder, folder 1..5 (A..E); args: msg<<16, 1, blocks, blocks*1056.
# icdcomm2.dll's GetVoiceDataST writes its first argument (the folder) into the
# opcode's low 16 bits. Reading a folder's table does not select the folder: on
# an ICD-ST10, 0x11FF0001 sent after folder B's table read returned A-001's data.
CMD_GET_VOICE_BASE = 0x11FF0000
GET_VOICE_FOLDERS = range(1, 6)


def get_voice_opcode(folder):
    """The GET_VOICE opcode for folder 1..5 (A..E)."""
    if folder not in GET_VOICE_FOLDERS:
        raise ValueError("folder must be 1..5")
    return CMD_GET_VOICE_BASE | folder


def is_get_voice(op):
    return op & 0xFFFF0000 == CMD_GET_VOICE_BASE and op & 0xFFFF in GET_VOICE_FOLDERS


BLOCK_RAW = 1056
MAX_BLOCKS = 32 * 1024 * 1024 // 1024   # the ST25 has 32 MB of flash
MAX_REPLY = 4096


def args_ok(words):
    """Exact argument shapes Digital Voice Editor uses for each command."""
    op, args = words[0], list(words[1:])
    if op in (CMD_DEVICE_INFO, CMD_INFO_03, CMD_TARGET_STATUS):
        return args == [0, 0]
    if op == CMD_READ_BLOCK:
        return args in ([0x1E0, 0], [0x1E0, 0x1E0])
    if op & 0xFFFF00FF == CMD_FOLDER_INFO and 1 <= (op >> 8) & 0xFF <= 5:
        return args == [0, 0]
    if is_get_voice(op):
        if len(args) != 4:
            return False
        msg, one, blocks, size = args
        return (msg & 0xFFFF == 0 and 1 <= msg >> 16 <= 0xFFFF and one == 1
                and 1 <= blocks <= MAX_BLOCKS and size == blocks * BLOCK_RAW)
    return False


def frame_ok(frame):
    """True only for a complete command frame Digital Voice Editor sends."""
    frame = bytes(frame)
    if len(frame) not in (24, 32) or frame[:12] != FRAME_PREFIX:
        return False
    words = struct.unpack(f">{(len(frame) - 12) // 4}I", frame[12:])
    return args_ok(words) and len(frame) == (32 if is_get_voice(words[0]) else 24)


def allow_out(request_type, request, value, index, data):
    """The only OUT transfer ever allowed: a whitelisted command frame."""
    return (request_type == REQTYPE_OUT and request == REQ_COMMAND and value == WVALUE
            and index == 0 and frame_ok(data))


def allow_in(request_type, request, value, index, length):
    """Only the status poll and the reply read (both device-to-host)."""
    if request_type != REQTYPE_IN or value != WVALUE or index != 0:
        return False
    return (request == REQ_STATUS and length == 4) or (request == REQ_REPLY and 16 <= length <= MAX_REPLY)


def allow_bulk_in(endpoint):
    return endpoint == EP_BULK_IN
