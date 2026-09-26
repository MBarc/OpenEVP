"""Minimal libusb-1.0 binding via ctypes.

No third-party Python packages: on Linux it uses the system libusb-1.0, on
Windows the libusb-1.0.dll bundled with the program (the recorder must be bound
to the WinUSB driver, e.g. with Zadig).
"""
import contextlib
import ctypes
import ctypes.util
import os
import sys

from . import policy

LIBUSB_ERROR_TIMEOUT = -7
LIBUSB_ERROR_NOT_SUPPORTED = -12   # Windows: the device is not bound to WinUSB


class UsbError(Exception):
    pass


class DriverMissing(UsbError):
    """The recorder is attached but not bound to WinUSB (see the README)."""


class _DeviceDescriptor(ctypes.Structure):
    _fields_ = [("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
                ("bcdUSB", ctypes.c_uint16), ("bDeviceClass", ctypes.c_uint8),
                ("bDeviceSubClass", ctypes.c_uint8), ("bDeviceProtocol", ctypes.c_uint8),
                ("bMaxPacketSize0", ctypes.c_uint8), ("idVendor", ctypes.c_uint16),
                ("idProduct", ctypes.c_uint16), ("bcdDevice", ctypes.c_uint16),
                ("iManufacturer", ctypes.c_uint8), ("iProduct", ctypes.c_uint8),
                ("iSerialNumber", ctypes.c_uint8), ("bNumConfigurations", ctypes.c_uint8)]


def _load_libusb():
    candidates = []
    if sys.platform == "win32":
        # PyInstaller unpacks bundled files into sys._MEIPASS (a directory).
        here = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidates += [os.path.join(here, "libusb-1.0.dll"),
                       os.path.join(os.path.dirname(sys.executable), "libusb-1.0.dll"),
                       os.path.join(here, "..", "libusb-1.0.dll"),
                       "libusb-1.0.dll",
                       # Running from source (no PyInstaller bundle): the vendored copy.
                       os.path.join(repo_root, "vendor", "libusb-1.0.30", "libusb-1.0.dll")]
    else:
        found = ctypes.util.find_library("usb-1.0")
        candidates += [found] if found else []
        candidates += ["libusb-1.0.so.0", "libusb-1.0.dylib"]
    errors = []
    for c in candidates:
        try:
            return ctypes.CDLL(c)
        except OSError as e:
            errors.append(f"{c}: {e}")
    raise UsbError("libusb-1.0 could not be loaded (on Windows, libusb-1.0.dll must be bundled "
                   "with the program):\n  " + "\n  ".join(errors))


_lib = None


def _libusb():
    """Load libusb on first use, so a missing DLL is reported like any other error."""
    global _lib
    if _lib is not None:
        return _lib
    lib = _load_libusb()
    lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    lib.libusb_exit.argtypes = [ctypes.c_void_p]
    lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
    lib.libusb_open_device_with_vid_pid.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
    lib.libusb_close.argtypes = [ctypes.c_void_p]
    lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_set_auto_detach_kernel_driver.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_control_transfer.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint16,
                                            ctypes.c_uint16, ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint]
    lib.libusb_bulk_transfer.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_void_p, ctypes.c_int,
                                         ctypes.POINTER(ctypes.c_int), ctypes.c_uint]
    lib.libusb_error_name.restype = ctypes.c_char_p
    lib.libusb_error_name.argtypes = [ctypes.c_int]
    lib.libusb_get_device_list.restype = ctypes.c_ssize_t
    lib.libusb_get_device_list.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))]
    lib.libusb_free_device_list.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
    lib.libusb_get_device_descriptor.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.libusb_get_bus_number.restype = ctypes.c_uint8
    lib.libusb_get_bus_number.argtypes = [ctypes.c_void_p]
    lib.libusb_get_device_address.restype = ctypes.c_uint8
    lib.libusb_get_device_address.argtypes = [ctypes.c_void_p]
    lib.libusb_get_port_numbers.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_int]
    lib.libusb_open.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    _lib = lib
    return lib


def error_name(code):
    return _libusb().libusb_error_name(code).decode()


def port_path(bus, ports):
    """'bus-port.port...': the USB socket the recorder is plugged into."""
    return f"{bus}-" + ".".join(str(p) for p in ports)


@contextlib.contextmanager
def _device_list(lib, ctx):
    lst = ctypes.POINTER(ctypes.c_void_p)()
    n = lib.libusb_get_device_list(ctx, ctypes.byref(lst))
    if n < 0:
        raise UsbError(f"cannot list USB devices: {error_name(n)}")
    try:
        yield [lst[i] for i in range(n)]
    finally:
        lib.libusb_free_device_list(lst, 1)


def _find(lib, devices, vid, pid):
    """[(libusb_device, connection id)] for each vid:pid device (valid while the list is).

    The id is "<port path>@<device address>". The OS assigns a new address on
    every plug-in, so a replug in the same socket is a new connection.
    """
    found = []
    for dev in devices:
        d = _DeviceDescriptor()
        if lib.libusb_get_device_descriptor(dev, ctypes.byref(d)) != 0:
            continue
        if (d.idVendor, d.idProduct) != (vid, pid):
            continue
        ports = (ctypes.c_uint8 * 7)()
        n = lib.libusb_get_port_numbers(dev, ports, 7)
        if n < 0:
            continue
        path = port_path(lib.libusb_get_bus_number(dev), ports[:n])
        found.append((dev, f"{path}@{lib.libusb_get_device_address(dev)}"))
    return found


def list_devices(vid, pid):
    """Connection ids of every attached vid:pid device, whatever driver it has.

    Only the OS's device list and standard descriptors are read; no vendor
    command reaches the recorder. The app calls this on its USB thread, so it
    never runs during a transaction.
    """
    lib = _libusb()
    ctx = ctypes.c_void_p()
    rc = lib.libusb_init(ctypes.byref(ctx))
    if rc != 0:
        raise UsbError(f"libusb_init failed: {error_name(rc)}")
    try:
        with _device_list(lib, ctx) as devices:
            return [dev_id for _, dev_id in _find(lib, devices, vid, pid)]
    finally:
        lib.libusb_exit(ctx)


class Device:
    """One opened USB device with interface 0 claimed.

    Every transfer is checked against the fixed recorder policy in
    st25.policy (not supplied by callers): control OUT only for whitelisted
    command frames, control IN only for the status/reply reads, bulk only from
    IN endpoint 0x81. Direction is enforced in USB terms too (bit 7 of the
    request type / endpoint address), so an "IN" method can never write.
    There is no unfiltered transfer method.

    Outside that policy is only standard USB lifecycle traffic issued by
    libusb/the OS: opening the device, claiming interface 0 and releasing it
    in close() (libusb documents that release may send a standard
    SET_INTERFACE to alternate setting 0). None of it is a vendor request, so
    none of it reaches the recorder's command interpreter.

    Transfer buffers are allocated once here, never inside a transaction: the
    recorder has a tight deadline between protocol steps.
    """

    CONTROL_BUF = 4096
    BULK_CHUNK = 64 * 1024

    def __init__(self, vid, pid, device_id=None):
        lib = _libusb()
        self._lib = lib
        self._ctx = ctypes.c_void_p()
        self._h = None
        rc = lib.libusb_init(ctypes.byref(self._ctx))
        if rc != 0:
            raise UsbError(f"libusb_init failed: {error_name(rc)}")
        if device_id is None:
            self._h = lib.libusb_open_device_with_vid_pid(self._ctx, vid, pid)
            if not self._h:
                self._fail(UsbError(f"device {vid:04x}:{pid:04x} not found, busy, or not bound to WinUSB"))
        else:
            self._h = self._open_id(vid, pid, device_id)
        lib.libusb_set_auto_detach_kernel_driver(self._h, 1)   # no-op on Windows
        rc = lib.libusb_claim_interface(self._h, 0)
        if rc != 0:
            self.close()
            raise UsbError(f"cannot claim interface 0: {error_name(rc)} (is another program using the recorder?)")
        self._ctrl = ctypes.create_string_buffer(self.CONTROL_BUF)
        self._bulk = ctypes.create_string_buffer(self.BULK_CHUNK)
        self._got = ctypes.c_int(0)

    def _fail(self, error):
        self._lib.libusb_exit(self._ctx)
        self._ctx = None
        raise error

    def _open_id(self, vid, pid, device_id):
        lib = self._lib
        handle = ctypes.c_void_p()
        rc = None
        port = device_id.split("@")[0]
        with _device_list(lib, self._ctx) as devices:
            for dev, found_id in _find(lib, devices, vid, pid):
                if found_id == device_id:
                    rc = lib.libusb_open(dev, ctypes.byref(handle))   # takes its own reference
                    break
        if rc is None:
            self._fail(UsbError(f"the recorder on USB port {port} is no longer connected"))
        if rc == LIBUSB_ERROR_NOT_SUPPORTED:
            self._fail(DriverMissing(f"the recorder on USB port {port} does not have the WinUSB driver"))
        if rc != 0:
            self._fail(UsbError(f"cannot open the recorder on USB port {port}: {error_name(rc)}"))
        return handle.value

    def control_in(self, request_type, request, value, index, length, timeout_ms):
        if not (request_type & 0x80 and policy.allow_in(request_type, request, value, index, length)):
            raise UsbError(f"blocked control IN request_type=0x{request_type:02x} request=0x{request:02x}")
        if length > self.CONTROL_BUF:
            raise UsbError(f"control IN length {length} exceeds {self.CONTROL_BUF}")
        rc = self._lib.libusb_control_transfer(self._h, request_type, request, value, index,
                                               self._ctrl, length, timeout_ms)
        if rc < 0:
            raise UsbError(f"control IN request 0x{request:02x} failed: {error_name(rc)}")
        return ctypes.string_at(self._ctrl, rc)

    def control_out(self, request_type, request, value, index, data, timeout_ms):
        data = bytes(data)
        if request_type & 0x80 or not policy.allow_out(request_type, request, value, index, data):
            raise UsbError(f"blocked control OUT request_type=0x{request_type:02x} request=0x{request:02x} "
                           f"data={data[:32].hex()}")
        if len(data) > self.CONTROL_BUF:
            raise UsbError("control OUT payload too large")
        ctypes.memmove(self._ctrl, data, len(data))
        rc = self._lib.libusb_control_transfer(self._h, request_type, request, value, index,
                                               self._ctrl, len(data), timeout_ms)
        if rc < 0:
            raise UsbError(f"control OUT request 0x{request:02x} failed: {error_name(rc)}")
        if rc != len(data):
            raise UsbError(f"control OUT short write {rc}/{len(data)}")

    def bulk_in_into(self, endpoint, dest, offset, max_len, timeout_ms):
        """Read up to max_len bytes (<= BULK_CHUNK) into dest[offset:].

        Returns (bytes_read, libusb_rc). The caller decides whether a timeout
        with progress is acceptable; any other error is raised here.
        """
        if not (endpoint & 0x80 and policy.allow_bulk_in(endpoint)):
            raise UsbError(f"blocked bulk read on endpoint 0x{endpoint:02x}")
        n = min(max_len, self.BULK_CHUNK)
        self._got.value = 0
        rc = self._lib.libusb_bulk_transfer(self._h, endpoint, self._bulk, n, ctypes.byref(self._got), timeout_ms)
        got = self._got.value
        if rc not in (0, LIBUSB_ERROR_TIMEOUT):
            raise UsbError(f"bulk IN failed: {error_name(rc)} after {got} bytes")
        if got > n:
            raise UsbError("bulk IN returned more data than requested")
        dest[offset:offset + got] = ctypes.string_at(self._bulk, got)
        return got, rc

    def close(self):
        if self._h:
            self._lib.libusb_release_interface(self._h, 0)
            self._lib.libusb_close(self._h)
            self._h = None
        if self._ctx:
            self._lib.libusb_exit(self._ctx)
            self._ctx = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
