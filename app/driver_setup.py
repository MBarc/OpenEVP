"""Run the recorder driver setup (driver/install-winusb.ps1) with admin rights.

The installer runs the same script during installation; the app runs it only
when a recorder turns up without its driver (for example after the driver was
removed). Windows shows one admin prompt.
"""
import ctypes
import os
import sys
from ctypes import wintypes

SCRIPT = "install-winusb.ps1"
ERROR_CANCELLED = 1223
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
SW_HIDE = 0
INFINITE = 0xFFFFFFFF


class SetupCancelled(Exception):
    pass


class SetupRefused(Exception):
    """The script is not in a folder only administrators can change, so it is not elevated."""


def script_path():
    """The bundled script: <app folder>/_internal/driver when frozen, app/driver from source."""
    frozen = getattr(sys, "_MEIPASS", None)
    base = os.path.join(frozen, "driver") if frozen else os.path.join(os.path.dirname(os.path.abspath(__file__)), "driver")
    return os.path.join(base, SCRIPT)


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


def _run_elevated(exe, params):
    """Run exe elevated (UAC), wait, return its exit code. Raises SetupCancelled on "No"."""
    info = _ShellExecuteInfo(cbSize=ctypes.sizeof(_ShellExecuteInfo), fMask=SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC,
                             lpVerb="runas", lpFile=exe, lpParameters=params, nShow=SW_HIDE)
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.GetLastError()
        if err == ERROR_CANCELLED:
            raise SetupCancelled("the administrator prompt was declined")
        raise OSError(err, ctypes.FormatError(err))
    k32 = ctypes.windll.kernel32
    try:
        k32.WaitForSingleObject(info.hProcess, INFINITE)
        code = wintypes.DWORD()
        k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        k32.CloseHandle(info.hProcess)


def _powershell():
    """Windows PowerShell by full path from the real system folder (not a search, not %SystemRoot%)."""
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.kernel32.GetSystemDirectoryW(buf, 260)
    return os.path.join(buf.value, "WindowsPowerShell", "v1.0", "powershell.exe")


def log_path():
    """The script writes its log to <install folder>/driver-setup (admin-only, readable by all)."""
    return os.path.normpath(os.path.join(os.path.dirname(script_path()), "..", "..", "driver-setup", "setup.log"))


def _program_files():
    """Program Files as Windows reports it (FOLDERID_ProgramFiles), not an environment variable."""
    class GUID(ctypes.Structure):
        _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]
    fid = GUID(0x905E63B6, 0xC1BF, 0x494E, (ctypes.c_ubyte * 8)(0xB2, 0x9C, 0x65, 0xB7, 0x32, 0xD3, 0xD2, 0x1A))
    path = ctypes.c_wchar_p()
    hr = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(path))
    try:
        return path.value if hr == 0 else None
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)


def is_protected(path, program_files):
    """True if path is inside Program Files (where only administrators can write)."""
    if not program_files:
        return False
    real, root = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(program_files))
    return os.path.commonpath([real, root]) == root


def set_up_driver():
    """Install the WinUSB driver for the ST25. Returns (exit_code, log_text).

    Exit codes are the script's: 0 done, 3010 done (restart recommended), 1 failed.
    """
    script = script_path()
    if not is_protected(script, _program_files()):
        # Elevating a script anyone could have edited would hand them admin rights.
        raise SetupRefused("it only works in the installed app (run OpenEVP-Setup to install it)")
    code = _run_elevated(_powershell(), f'-NoProfile -ExecutionPolicy Bypass -File "{script}"')
    try:
        with open(log_path(), encoding="utf-8", errors="replace") as f:
            log = f.read()
    except OSError:
        log = ""
    return code, log
