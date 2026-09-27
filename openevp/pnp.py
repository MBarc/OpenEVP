"""Recorders Windows can see but libusb cannot (no WinUSB driver yet).

libusb on Windows only lists devices that have a driver it can use, so a
recorder on a PC where the driver was never set up (or was removed) is
invisible to it. Windows' own device list (Plug and Play) still has it; the
app shows such a recorder as "needs setup" instead of not at all.

Matching is per model: a model asks for the instances of its own USB ids.
Which of them need the driver is decided per Windows device instance, from
the driver service Windows bound it to (read-only: CM_Get_DevNode_Registry_
PropertyW(CM_DRP_SERVICE)): a device needs setup unless its service is one
libusb can open (WinUSB, libusbK or libusb0, e.g. bound by Zadig: v0.7.2's
count treated those as usable, so they never get a placeholder) and Windows
reports no problem code for it. Only if Windows cannot say (an API error)
does it fall back to counting: the instances beyond the recorders of that
model libusb can use. Another model's recorders never count for or against it.
"""
import ctypes
import sys

SETUP_PREFIX = "setup:"
CM_GETIDLIST_FILTER_ENUMERATOR = 0x00000001
CM_GETIDLIST_FILTER_PRESENT = 0x00000100
CR_SUCCESS = 0
CR_NO_SUCH_VALUE = 0x25
CM_LOCATE_DEVNODE_NORMAL = 0
CM_DRP_SERVICE = 0x05
DN_HAS_PROBLEM = 0x400
CM_PROB_FAILED_INSTALL = 28
# Driver services libusb can open a device through, as Windows names them
# (HKLM\SYSTEM\CurrentControlSet\Services\<name>): WinUSB (the app's own
# setup), libusbK and libusb0 (libusb-win32), compared ignoring case.
LIBUSB_SERVICES = ("winusb", "libusbk", "libusb0")


class PnpError(OSError):
    """Windows could not say which driver a device instance has."""


def hardware_id(vid, pid):
    """Windows' hardware id for a USB device, e.g. USB\\VID_054C&PID_0103 (the
    driver INF lists these; a device's instance id is this + "\\" + serial/port)."""
    return f"USB\\VID_{vid:04X}&PID_{pid:04X}"


def _usb_instances():
    """Instance ids of every USB device plugged in now ([] off Windows or on error)."""
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
    return [i for i in buf[:size.value].split("\0") if i]


def instances_of(usb_ids, instances):
    """The instance ids among instances that belong to one of usb_ids
    ((vid, pid) pairs). Only the device itself matches, not the interfaces of
    a composite device (USB\\VID_..&PID_..&MI_00\\...)."""
    prefixes = tuple(hardware_id(v, p).upper() + "\\" for v, p in usb_ids)
    return [i for i in instances if prefixes and i.upper().startswith(prefixes)]


def present_instances(usb_ids):
    """Instance ids of every device of one model (its usb_ids) plugged in now,
    whatever its driver ([] off Windows)."""
    return instances_of(usb_ids, _usb_instances())


def _cfgmgr32():
    cfg = ctypes.windll.cfgmgr32
    ulong_p = ctypes.POINTER(ctypes.c_ulong)
    cfg.CM_Locate_DevNodeW.argtypes = [ulong_p, ctypes.c_wchar_p, ctypes.c_ulong]
    cfg.CM_Locate_DevNodeW.restype = ctypes.c_ulong
    cfg.CM_Get_DevNode_Status.argtypes = [ulong_p, ulong_p, ctypes.c_ulong, ctypes.c_ulong]
    cfg.CM_Get_DevNode_Status.restype = ctypes.c_ulong
    cfg.CM_Get_DevNode_Registry_PropertyW.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ulong_p,
                                                      ctypes.c_void_p, ulong_p, ctypes.c_ulong]
    cfg.CM_Get_DevNode_Registry_PropertyW.restype = ctypes.c_ulong
    return cfg


def driver_of(instance_id):
    """(service, problem) of one present device instance: the driver service
    Windows bound it to ("" for none, e.g. "WinUSB") and its problem code (0
    for none). Only reads Windows' device tree; raises PnpError if Windows
    cannot say (off Windows, the instance is gone, any API error)."""
    if sys.platform != "win32":
        raise PnpError("no Plug and Play device tree off Windows")
    try:
        cfg = _cfgmgr32()
        devinst = ctypes.c_ulong(0)
        cr = cfg.CM_Locate_DevNodeW(ctypes.byref(devinst), instance_id, CM_LOCATE_DEVNODE_NORMAL)
        if cr != CR_SUCCESS:
            raise PnpError(f"CM_Locate_DevNodeW({instance_id}) failed: CR 0x{cr:x}")
        status, problem = ctypes.c_ulong(0), ctypes.c_ulong(0)
        cr = cfg.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem), devinst.value, 0)
        if cr != CR_SUCCESS:
            raise PnpError(f"CM_Get_DevNode_Status({instance_id}) failed: CR 0x{cr:x}")
        buf = ctypes.create_unicode_buffer(256)
        size = ctypes.c_ulong(ctypes.sizeof(buf))
        cr = cfg.CM_Get_DevNode_Registry_PropertyW(devinst.value, CM_DRP_SERVICE, None, buf,
                                                   ctypes.byref(size), 0)
    except (AttributeError, ctypes.ArgumentError) as e:
        raise PnpError(f"cfgmgr32 unusable: {e}") from e
    if cr == CR_NO_SUCH_VALUE:                   # no driver installed at all
        service = ""
    elif cr != CR_SUCCESS:
        raise PnpError(f"CM_Get_DevNode_Registry_PropertyW({instance_id}) failed: CR 0x{cr:x}")
    else:
        service = buf.value
    return service, (problem.value if status.value & DN_HAS_PROBLEM else 0)


def lacks_winusb(service, problem):
    """Whether a device instance with this driver service and problem code
    needs the app's driver setup: its service is not one libusb can open, or
    Windows reports a problem with it (any nonzero problem code)."""
    return (service or "").lower() not in LIBUSB_SERVICES or problem != 0


def needs_setup(instances, usable_count):
    """Placeholder ids for one model's recorders libusb cannot use (no libusb
    driver, or a problem):
    decided per Windows instance (driver_of); if Windows cannot say for any of
    them, the instances beyond the usable_count recorders of that model libusb
    can use, as before."""
    try:
        missing = [i for i in sorted(instances) if lacks_winusb(*driver_of(i))]
    except OSError:
        missing = sorted(instances)[:max(0, len(instances) - usable_count)]
    return [SETUP_PREFIX + i for i in missing]
