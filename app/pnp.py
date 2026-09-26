"""Recorders Windows can see but libusb cannot (no WinUSB driver yet).

libusb on Windows only lists devices that have a driver it can use, so a
recorder on a PC where the driver was never set up (or was removed) is
invisible to it. Windows' own device list (Plug and Play) still has it; the
app shows such a recorder as "needs setup" instead of not at all.
"""
import ctypes
import sys

HARDWARE_ID = "USB\\VID_054C&PID_0103\\"
SETUP_PREFIX = "setup:"
CM_GETIDLIST_FILTER_ENUMERATOR = 0x00000001
CM_GETIDLIST_FILTER_PRESENT = 0x00000100
CR_SUCCESS = 0


def present_instances():
    """Instance ids of every ST25 currently plugged in, whatever its driver ([] off Windows)."""
    if sys.platform != "win32":
        return []
    cfg = ctypes.windll.cfgmgr32
    flags = CM_GETIDLIST_FILTER_ENUMERATOR | CM_GETIDLIST_FILTER_PRESENT
    size = ctypes.c_ulong(0)
    if cfg.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), "USB", flags) != CR_SUCCESS:
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    if cfg.CM_Get_Device_ID_ListW("USB", buf, size.value, flags) != CR_SUCCESS:
        return []
    ids = buf[:size.value].split("\0")
    return [i for i in ids if i.upper().startswith(HARDWARE_ID)]


def needs_setup(instances, usable_count):
    """Placeholder ids for recorders present in Windows beyond those libusb can use."""
    missing = sorted(instances)[:max(0, len(instances) - usable_count)]
    return [SETUP_PREFIX + i for i in missing]
