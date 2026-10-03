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

def clean_name(name, what="folder"):
    """(name, None) with surrounding spaces removed, or (None, plain reason) when
    it cannot be a folder name on Windows. what: the word the reasons use ("folder",
    or "file" for a recording's new name without its extension)."""
    if not isinstance(name, str):
        return None, f"Type a name for the {what}."
    name = name.strip()
    if not name:
        return None, f"Type a name for the {what}."
    if len(name) > MAX_NAME:
        return None, f"A {what} name can be at most {MAX_NAME} characters."
    if name in (".", "..") or name.startswith("."):
        return None, f"A {what} name cannot start with a dot."
    if any(c in _FORBIDDEN or ord(c) < 32 for c in name):
        return None, f'A {what} name cannot contain any of < > : " / \\ | ? * or control characters.'
    if name.endswith("."):
        return None, f"A {what} name cannot end with a dot."
    if name.split(".")[0].strip().upper() in _RESERVED:
        return None, f'"{name}" is a name Windows reserves for devices. Choose another name.'
    return name, None


def hide(path):
    """Give a file Windows' hidden attribute (best effort; nothing elsewhere)."""
    if sys.platform != "win32":
        return
    try:
        full = os.path.abspath(path)
        attrs = ctypes.windll.kernel32.GetFileAttributesW(full)
        if attrs != 0xFFFFFFFF:
            ctypes.windll.kernel32.SetFileAttributesW(full, attrs | 0x2)     # FILE_ATTRIBUTE_HIDDEN
    except Exception:
        pass


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


_HIDDEN_OR_SYSTEM = 0x2 | 0x4          # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM


def entry_is_hidden(entry):
    """Is an os.DirEntry marked hidden or system on Windows (AppData, $RECYCLE.BIN,
    System Volume Information...)? Always False elsewhere."""
    return bool(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & _HIDDEN_OR_SYSTEM)


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
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    except ValueError:                       # another drive
        return False
    try:
        real_root = os.path.normcase(os.path.realpath(root))
        real = os.path.normcase(os.path.realpath(path))
        # The library folder itself may be a link the user chose (a redirected
        # Documents, say); whether it changed since the listing is checked by
        # the caller (library_ops._root_identity).
        if rel != os.curdir and is_link(path):
            return False
    except (OSError, ValueError):
        return False
    if rel == os.curdir:
        return allow_root and real == real_root
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return False
    if not real.startswith(real_root.rstrip(os.sep) + os.sep):
        return False
    return real == os.path.normcase(os.path.join(real_root, rel))


# ---- pinning folders for the length of an operation (Windows) -------------------------

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000      # needed to open a folder
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000    # a link is held itself, never what it points to
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class Pins:
    """Open handles on folders, shared for reading and writing but not for
    deleting: while one is held, nobody (this process included) can rename,
    move or delete that folder -- or any folder above it, since Windows does
    not rename a folder with an open handle inside it -- so it cannot be
    swapped for a junction halfway through an operation. Files and folders
    inside stay free to change. A no-op off Windows."""

    def __init__(self):
        self._held = {}                          # (normcased path, followed) -> handle

    def add(self, path, follow=False):
        """Hold path (a folder); raises OSError when it cannot be opened. A link
        is held itself, or with follow, the folder it leads to."""
        key = (os.path.normcase(os.path.abspath(path)), follow)
        if sys.platform != "win32" or key in self._held:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                         ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        # GENERIC_READ, not just attributes: Windows checks share modes only for
        # handles with data access.
        flags = FILE_FLAG_BACKUP_SEMANTICS | (0 if follow else FILE_FLAG_OPEN_REPARSE_POINT)
        handle = kernel32.CreateFileW(os.path.abspath(path), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                      None, OPEN_EXISTING, flags, None)
        if handle is None or handle == _INVALID_HANDLE:
            err = ctypes.get_last_error()
            raise OSError(None, ctypes.FormatError(err).strip(), path, err)
        self._held[key] = handle

    def chain(self, root, path):
        """Hold root (the name, and the folder it leads to when it is a link)
        and every folder from it down to path (path included)."""
        self.add(root)
        self.add(root, follow=True)
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
        if rel == os.curdir:
            return
        where = os.path.abspath(root)
        for part in rel.split(os.sep):
            where = os.path.join(where, part)
            self.add(where)

    def release(self, path):
        """Let go of one folder (before it is renamed or recycled by this process)."""
        handle = self._held.pop((os.path.normcase(os.path.abspath(path)), False), None)
        if handle is not None:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))

    def close(self):
        for key in list(self._held):
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self._held.pop(key)))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


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
NO_ROOM = ("This folder can't go to the Recycle Bin (it is too big for it, or the Recycle Bin is set to "
           "delete files immediately). Delete it in File Explorer if you really mean to.")
IN_USE = "{} was not (completely) moved to the Recycle Bin: a file is in use, or deleting was cancelled."
FILE_IN_USE = "{} was not moved to the Recycle Bin: it is in use (open in another program?), or deleting was cancelled."
BITBUCKET = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume"

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


def volume_guid(root):
    """The "{GUID}" of the volume mounted at a drive root ("C:\\"), or None."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetVolumeNameForVolumeMountPointW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    kernel32.GetVolumeNameForVolumeMountPointW.restype = ctypes.c_int
    buf = ctypes.create_unicode_buffer(64)
    if not kernel32.GetVolumeNameForVolumeMountPointW(root, buf, len(buf)):
        return None
    name = buf.value                        # \\?\Volume{GUID}\
    start, end = name.find("{"), name.find("}")
    return name[start:end + 1] if 0 <= start < end else None


def bin_settings(guid):
    """The Recycle Bin settings Windows keeps for one volume (per user):
    {"NukeOnDelete": int, "MaxCapacity": int (MB)}, with only the values found."""
    import winreg
    found = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, BITBUCKET + "\\" + guid) as key:
            for value in ("NukeOnDelete", "MaxCapacity"):
                try:
                    data, kind = winreg.QueryValueEx(key, value)
                except OSError:
                    continue
                if kind == winreg.REG_DWORD and isinstance(data, int):
                    found[value] = data
    except OSError:
        pass                                # never configured: Windows' defaults apply
    return found


def tree_size(path):
    """Bytes in path and everything in it (links not followed; unreadable parts
    skipped); a file's own size for a file."""
    try:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            return 0 if _link_stat(st) else st.st_size
    except OSError:
        return 0
    total, stack = 0, [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                if entry_is_link(e):
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                else:
                    total += e.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def bin_refuses(root, size, guid_of=None, settings_of=None):
    """Would the Recycle Bin of the drive at root delete something of `size` bytes
    for good: is it set to delete files immediately, or is size beyond its
    capacity? Missing settings mean Windows' defaults: not immediate, and an
    unknown capacity is never a reason to refuse."""
    guid = (guid_of or volume_guid)(root)
    settings = (settings_of or bin_settings)(guid) if guid else {}
    if settings.get("NukeOnDelete"):
        return True
    capacity = settings.get("MaxCapacity")
    return capacity is not None and size > capacity * 1024 * 1024


def drive_type(root):
    """GetDriveTypeW of a drive root ("C:\\"): DRIVE_FIXED (3) for a local disk."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetDriveTypeW.restype = ctypes.c_uint
    return kernel32.GetDriveTypeW(root)


def where_it_is(path):
    """The place path really names: its folder with every symlink and junction
    resolved (the long-path prefix of a drive path dropped), then its own name, not
    followed. None when it cannot be resolved."""
    try:
        path = os.path.abspath(path)
        parent = os.path.realpath(os.path.dirname(path))
    except (OSError, ValueError):
        return None
    if parent.startswith("\\\\?\\") and parent[5:6] == ":":
        parent = parent[4:]
    return os.path.join(parent, os.path.basename(path))


def recycle(path, owner=None, before=None):
    """Move one folder (or file) to the Recycle Bin, or raise RecycleError in
    plain words. Never deletes permanently on purpose: a drive without a Recycle
    Bin, a Recycle Bin set to delete immediately and a folder too big for it are
    refused before anything happens. owner: the app window's handle, or None.
    before: called right before the shell is asked (to let go of the folder)."""
    if sys.platform != "win32":
        raise RecycleError("The Recycle Bin is only available on Windows.")
    path = os.path.abspath(path)
    name = os.path.basename(path)
    is_file = os.path.isfile(path)

    def said(message):                               # the same words, for a file
        return message.replace("the folder", "the file").replace("This folder", "This file") if is_file else message
    if path.startswith("\\\\"):
        raise RecycleError(said(NO_RECYCLE_BIN))     # network and \\?\ paths
    # Where it really is: a folder above it may be a symlink or junction to another
    # drive (a NAS, a USB stick), where the shell would delete for good. Every check
    # below, and the delete itself, is on that place; another volume is refused.
    # (The item itself is not followed: a link is recycled as the link.)
    real = where_it_is(path)
    if (real is None or real.startswith("\\\\")      # a share, or a volume with no drive letter
            or os.path.normcase(os.path.splitdrive(real)[0]) != os.path.normcase(os.path.splitdrive(path)[0])):
        raise RecycleError(said(NO_RECYCLE_BIN))
    if len(path) >= MAX_PATH or len(real) >= MAX_PATH:
        raise RecycleError(f"The path of {name} is too long for the Recycle Bin. "
                           "Delete it in File Explorer if you really mean to.")
    path = real
    root = os.path.splitdrive(path)[0] + "\\"
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32")
    shell32.SHQueryRecycleBinW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(SHQUERYRBINFO)]
    shell32.SHQueryRecycleBinW.restype = ctypes.c_long
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.restype = None

    if drive_type(root) != DRIVE_FIXED:
        raise RecycleError(said(NO_RECYCLE_BIN))
    # FOF_WANTNUKEWARNING only asks before deleting for good (and a Yes would
    # delete): refuse up front whatever the Recycle Bin would not take. The
    # warning stays as a backstop for a case this does not foresee.
    if bin_refuses(root, tree_size(path)):
        raise RecycleError(said(NO_ROOM))
    source = ctypes.create_unicode_buffer(path + "\0")   # double-NUL terminated; kept referenced
    op = SHFILEOPSTRUCTW(hwnd=owner or None, wFunc=FO_DELETE,
                         pFrom=ctypes.cast(source, ctypes.c_wchar_p), pTo=None, fFlags=RECYCLE_FLAGS)
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)   # the shell calls below need COM
    try:
        info = SHQUERYRBINFO(cbSize=ctypes.sizeof(SHQUERYRBINFO))
        if shell32.SHQueryRecycleBinW(root, ctypes.byref(info)) != 0:
            raise RecycleError(said(NO_RECYCLE_BIN))
        if before is not None:
            before()
        code = shell32.SHFileOperationW(ctypes.byref(op))
    finally:
        if hr >= 0:                                      # S_OK or S_FALSE; not RPC_E_CHANGED_MODE
            ole32.CoUninitialize()
    del source
    if code == 0 and not op.fAnyOperationsAborted and not os.path.lexists(path):
        return
    if code == 0 or (is_file and code in (5, 32, 33)):   # access denied, sharing or lock violation
        raise RecycleError((FILE_IN_USE if is_file else IN_USE).format(name))
    raise RecycleError(f"{name} was not moved to the Recycle Bin (code 0x{code & 0xFFFFFFFF:x}).")
