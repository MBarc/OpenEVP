"""Library folders: name rules, staying inside the library, and the Recycle Bin.

Everything the EVP library does to folders on disk goes through these helpers:
folder names are checked against Windows' rules before anything is created or
renamed, every path is checked to lie inside the library folder (no symlink or
junction on the way) right before it is used, and a deleted folder only ever
goes to the Recycle Bin -- if that is not possible, nothing is deleted.
"""
import ctypes
import os
import stat
import sys

MAX_NAME = 120              # characters in one folder name
MAX_PATH_CHARS = 240        # a resulting full path must stay below this (Windows MAX_PATH is 260)

_FORBIDDEN = set('<>:"/\\|?*')
_RESERVED = ({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
             | {f"COM{c}" for c in "0123456789\u00b9\u00b2\u00b3"}
             | {f"LPT{c}" for c in "0123456789\u00b9\u00b2\u00b3"})


class RecycleError(Exception):
    """The folder was not (completely) moved to the Recycle Bin; the message is plain words."""


# ---- names ------------------------------------------------------------------------

def clean_name(name):
    """(name, None) with surrounding spaces removed, or (None, plain reason) when
    it cannot be a folder name on Windows."""
    if not isinstance(name, str):
        return None, "Type a name for the folder."
    name = name.strip()
    if not name:
        return None, "Type a name for the folder."
    if len(name) > MAX_NAME:
        return None, f"A folder name can be at most {MAX_NAME} characters."
    if name in (".", "..") or name.startswith("."):
        return None, "A folder name cannot start with a dot."
    if any(c in _FORBIDDEN or ord(c) < 32 for c in name):
        return None, 'A folder name cannot contain any of < > : " / \\ | ? * or control characters.'
    if name.endswith("."):
        return None, "A folder name cannot end with a dot."
    if name.split(".")[0].strip().upper() in _RESERVED:
        return None, f'"{name}" is a name Windows reserves for devices. Choose another name.'
    return name, None


def too_long(path):
    """Is a full path too long to be used safely?"""
    return len(os.path.abspath(path)) >= MAX_PATH_CHARS


def name_taken(parent, name, ignore=None):
    """Is there already an entry named `name` in `parent` (ignoring case, as
    Windows does)? `ignore` is one existing name that does not count (the folder
    being renamed)."""
    want = name.casefold()
    skip = ignore.casefold() if ignore else None
    with os.scandir(parent) as it:
        for e in it:
            n = e.name.casefold()
            if n == want and n != skip:
                return True
    return False


# ---- containment ------------------------------------------------------------------

def _key(path):
    return os.path.normcase(os.path.abspath(path))


def under(path, prefix):
    """Is path the same as prefix, or inside it (by name, no disk access)?"""
    p, pre = _key(path), _key(prefix)
    return p == pre or p.startswith(pre.rstrip(os.sep) + os.sep)


def rebase(path, old, new):
    """path (under old, see under()) moved to the same place under new."""
    path, old, new = os.path.abspath(path), os.path.abspath(old), os.path.abspath(new)
    return new + path[len(old):]


_REPARSE_POINT = 0x400                  # FILE_ATTRIBUTE_REPARSE_POINT
_NAME_SURROGATE = 0x20000000            # reparse tags that stand for another file or folder


def _link_stat(st):
    """Does an lstat result describe a symlink, junction (mount point) or other
    name-surrogate reparse point? Cloud placeholders (OneDrive files on demand)
    are reparse points too, but not name surrogates: they are ordinary files."""
    if stat.S_ISLNK(st.st_mode):
        return True
    if getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
        return bool(getattr(st, "st_reparse_tag", 0) & _NAME_SURROGATE)
    return False


def is_link(path):
    """Is path itself a symlink or junction (never followed)?"""
    return _link_stat(os.lstat(path))


def entry_is_link(entry):
    """is_link() for an os.DirEntry, from what the folder listing already read
    (on any Python version: DirEntry.is_junction only exists from 3.12)."""
    return entry.is_symlink() or _link_stat(entry.stat(follow_symlinks=False))


def inside(root, path, allow_root=False):
    """Is path strictly inside the library folder root (or root itself with
    allow_root), with no symlink or junction between them? The path is resolved
    on disk (realpath) and must end up exactly where its name says, so a folder
    swapped for a junction since the listing is refused. path must exist."""
    try:
        real_root = os.path.normcase(os.path.realpath(root))
        real = os.path.normcase(os.path.realpath(path))
        if is_link(path):
            return False
    except (OSError, ValueError):
        return False
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    except ValueError:                       # another drive
        return False
    if rel == os.curdir:
        return allow_root and real == real_root
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return False
    if not real.startswith(real_root.rstrip(os.sep) + os.sep):
        return False
    return real == os.path.normcase(os.path.join(real_root, rel))


# ---- the Recycle Bin (Windows) ------------------------------------------------------

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000
FOF_NORECURSEREPARSE = 0x8000
RECYCLE_FLAGS = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
                 | FOF_WANTNUKEWARNING | FOF_NORECURSEREPARSE)
DRIVE_FIXED = 3
COINIT_APARTMENTTHREADED = 0x2
MAX_PATH = 260

NO_RECYCLE_BIN = "This drive has no Recycle Bin. Delete the folder in File Explorer if you really mean to."
IN_USE = "Some files are in use and were not moved to the Recycle Bin."

_BITS64 = ctypes.sizeof(ctypes.c_void_p) == 8


class SHFILEOPSTRUCTW(ctypes.Structure):
    if not _BITS64:
        _pack_ = 1                          # shellapi.h packs its structs to 1 on 32-bit only
    _fields_ = [("hwnd", ctypes.c_void_p),
                ("wFunc", ctypes.c_uint),
                ("pFrom", ctypes.c_wchar_p),
                ("pTo", ctypes.c_wchar_p),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", ctypes.c_int),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_wchar_p)]


class SHQUERYRBINFO(ctypes.Structure):
    if not _BITS64:
        _pack_ = 1
    _fields_ = [("cbSize", ctypes.c_uint32),
                ("i64Size", ctypes.c_int64),
                ("i64NumItems", ctypes.c_int64)]


def recycle(path, owner=None):
    """Move one folder (or file) to the Recycle Bin, or raise RecycleError in
    plain words. Never deletes permanently: a drive without a Recycle Bin is
    refused before anything happens. owner: the app window's handle, or None."""
    if sys.platform != "win32":
        raise RecycleError("The Recycle Bin is only available on Windows.")
    path = os.path.abspath(path)
    name = os.path.basename(path)
    if path.startswith("\\\\"):
        raise RecycleError(NO_RECYCLE_BIN)           # network and \\?\ paths
    if len(path) >= MAX_PATH:
        raise RecycleError(f"The path of {name} is too long for the Recycle Bin. "
                           "Delete it in File Explorer if you really mean to.")
    root = os.path.splitdrive(path)[0] + "\\"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32")
    kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetDriveTypeW.restype = ctypes.c_uint
    shell32.SHQueryRecycleBinW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(SHQUERYRBINFO)]
    shell32.SHQueryRecycleBinW.restype = ctypes.c_long
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.restype = None

    if kernel32.GetDriveTypeW(root) != DRIVE_FIXED:
        raise RecycleError(NO_RECYCLE_BIN)
    source = ctypes.create_unicode_buffer(path + "\0")   # double-NUL terminated; kept referenced
    op = SHFILEOPSTRUCTW(hwnd=owner or None, wFunc=FO_DELETE,
                         pFrom=ctypes.cast(source, ctypes.c_wchar_p), pTo=None, fFlags=RECYCLE_FLAGS)
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)   # the shell calls below need COM
    try:
        info = SHQUERYRBINFO(cbSize=ctypes.sizeof(SHQUERYRBINFO))
        if shell32.SHQueryRecycleBinW(root, ctypes.byref(info)) != 0:
            raise RecycleError(NO_RECYCLE_BIN)
        code = shell32.SHFileOperationW(ctypes.byref(op))
    finally:
        if hr >= 0:                                      # S_OK or S_FALSE; not RPC_E_CHANGED_MODE
            ole32.CoUninitialize()
    del source
    if code == 0 and not op.fAnyOperationsAborted and not os.path.lexists(path):
        return
    if code == 0:
        raise RecycleError(IN_USE)
    raise RecycleError(f"{name} was not moved to the Recycle Bin (code 0x{code & 0xFFFFFFFF:x}).")
