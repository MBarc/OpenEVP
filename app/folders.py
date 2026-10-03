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
#
# The delete goes through the shell's IFileOperation, with a progress sink that
# holds a veto. Before the shell deletes an item it calls PreDeleteItem, whose
# flags carry TSF_DELETE_RECYCLE_IF_POSSIBLE when it is going to recycle it; a
# delete without that flag is a delete for good (no Recycle Bin, a Recycle Bin
# set to delete immediately, an item too big for it), and the sink refuses it,
# which cancels the whole operation before anything is deleted. Success is only
# what PostDeleteItem reports for the item: deleted with S_OK *and* a newly
# created item, the one in the Recycle Bin. It is never inferred from the path
# having gone.

FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
FOF_NORECURSEREPARSE = 0x8000
FOFX_RECYCLEONDELETE = 0x00080000           # Windows 8 and later recycle only with this
FOFX_EARLYFAILURE = 0x00100000              # the first failure stops the operation
RECYCLE_FLAGS = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT | FOF_NORECURSEREPARSE
                 | FOFX_RECYCLEONDELETE | FOFX_EARLYFAILURE)
TSF_DELETE_RECYCLE_IF_POSSIBLE = 0x80       # PreDeleteItem: this delete is a recycle
DRIVE_FIXED = 3
COINIT_APARTMENTTHREADED = 0x2
CLSCTX_ALL = 0x17
SIGDN_FILESYSPATH = 0x80058000
MAX_PATH = 260

S_OK = 0
E_NOINTERFACE = 0x80004002
RPC_E_CHANGED_MODE = 0x80010106
HR_CANCELLED = 0x800704C7                   # HRESULT_FROM_WIN32(ERROR_CANCELLED): the sink's veto
COPYENGINE_E_USER_CANCELLED = 0x80270000
COPYENGINE_E_CANCELLED = 0x80270001
COPYENGINE_E_ACCESS_DENIED_SRC = 0x80270021
COPYENGINE_E_SHARING_VIOLATION_SRC = 0x80270027
COPYENGINE_E_RECYCLE_UNKNOWN_ERROR = 0x80270035
COPYENGINE_E_RECYCLE_FORCE_NUKE = 0x80270036
COPYENGINE_E_RECYCLE_SIZE_TOO_BIG = 0x80270037
COPYENGINE_E_RECYCLE_PATH_TOO_LONG = 0x80270038
COPYENGINE_E_RECYCLE_BIN_NOT_FOUND = 0x8027003A

CLSID_FileOperation = "3ad05575-8857-4850-9277-11b85bdb8e09"
IID_IFileOperation = "947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8"
IID_IFileOperationProgressSink = "04b0f1a7-9490-44bc-96e1-4296a31252e2"
IID_IShellItem = "43826d1e-e718-42ee-bc55-a1e261c37bfe"
IID_IUnknown = "00000000-0000-0000-c000-000000000046"

NO_RECYCLE_BIN = "This drive has no Recycle Bin. Delete the folder in File Explorer if you really mean to."
NO_ROOM = ("This folder can't go to the Recycle Bin (it is too big for it, or the Recycle Bin is set to "
           "delete files immediately). Delete it in File Explorer if you really mean to.")
IN_USE = "{} was not (completely) moved to the Recycle Bin: a file is in use, or deleting was cancelled."
FILE_IN_USE = "{} was not moved to the Recycle Bin: it is in use (open in another program?), or deleting was cancelled."
TOO_LONG = "The path of {} is too long for the Recycle Bin. Delete it in File Explorer if you really mean to."
BITBUCKET = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume"

_BITS64 = ctypes.sizeof(ctypes.c_void_p) == 8


class SHQUERYRBINFO(ctypes.Structure):
    if not _BITS64:
        _pack_ = 1                          # shellapi.h packs its structs to 1 on 32-bit only
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
    capacity? This is only the early, plain refusal for what the registry shows.
    Missing settings mean Windows' defaults (not immediate, and a capacity Windows
    works out itself and does not publish), so an unknown capacity is no reason
    to refuse here: what the shell would still delete for good is vetoed while it
    runs (see recycle)."""
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


class RecycleOnly:
    """The progress sink's decisions for one recycle, without any COM (so a fake
    shell can test them): every delete that is not a recycle of exactly `target`
    is refused, and nothing else (rename, move, copy, new item) is allowed."""

    def __init__(self, target):
        self._target = self._key(target)
        self.vetoed = None                  # "nuke" or "other" once something was refused
        self.recycled = False               # PostDeleteItem: target deleted with S_OK into the Recycle Bin
        self.failure = None                 # the HRESULT PostDeleteItem gave otherwise
        self.stray = False                  # PostDeleteItem for some other item

    @staticmethod
    def _key(path):
        return os.path.normcase(os.path.abspath(path)) if path else None

    def pre_delete(self, flags, item):
        """PreDeleteItem: S_OK lets the shell go on; anything else cancels it."""
        if self.vetoed:
            return HR_CANCELLED
        if self._key(item) != self._target:
            self.vetoed = "other"           # a part of it on its own: only a delete for good does that
            return HR_CANCELLED
        if not flags & TSF_DELETE_RECYCLE_IF_POSSIBLE:
            self.vetoed = "nuke"
            return HR_CANCELLED
        return S_OK

    def pre_other(self):
        """PreRenameItem, PreMoveItem, PreCopyItem, PreNewItem: never."""
        self.vetoed = self.vetoed or "other"
        return HR_CANCELLED

    def post_delete(self, flags, item, hr, created):
        """PostDeleteItem: created says whether the shell made a new item (the
        one in the Recycle Bin); a successful delete without one was for good.
        Seen on Windows 11: a recycle comes with flags 0x282 (TSF_DELETE_RECYCLE_IF_POSSIBLE
        set) and hr 0x270008; a drive with no Recycle Bin (subst) with flags 0x202."""
        if self._key(item) != self._target:
            self.stray = True
            return
        if not hr & 0x80000000 and created:    # a success code (a recycle gives COPYENGINE_S_DONT_PROCESS_CHILDREN)
            self.recycled = True
        else:
            self.failure = (hr & 0xFFFFFFFF) or HR_CANCELLED

    def succeeded(self, hr, aborted):
        return (self.recycled and not self.vetoed and not self.stray and self.failure is None
                and hr & 0xFFFFFFFF == S_OK and not aborted)


def _signed(hr):
    return hr - (1 << 32) if hr & 0x80000000 else hr


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(text):
    import uuid
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def _method(ptr, index, *argtypes):
    """Method `index` of the COM object at ptr, as a callable without `this`."""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])
    return lambda *args: fn(ptr, *args)


def _release(ptr):
    if ptr:
        vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(ptr)


def item_path(item):
    """The file-system path of an IShellItem (a pointer), or None."""
    if not item:
        return None
    out = ctypes.c_void_p()
    hr = _method(item, 5, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p))(SIGDN_FILESYSPATH, ctypes.byref(out))
    if hr < 0 or not out.value:
        return None
    try:
        return ctypes.wstring_at(out.value)
    finally:
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree(out.value)


_LINGERING = []                             # sinks the shell kept a reference to: never freed


class ProgressSink:
    """An IFileOperationProgressSink made with ctypes that hands every call to a
    RecycleOnly. An exception in a Pre callback refuses."""

    def __init__(self, decide):
        P, H, U, V, D, W = (ctypes.WINFUNCTYPE, ctypes.c_long, ctypes.c_ulong, ctypes.c_void_p,
                            ctypes.c_uint32, ctypes.c_wchar_p)
        self.refs = 0
        known = {bytes(_guid(IID_IUnknown)), bytes(_guid(IID_IFileOperationProgressSink))}

        def qi(this, riid, ppv):
            try:
                if riid and bytes(riid.contents) in known:
                    ppv[0] = self.pointer
                    self.refs += 1
                    return S_OK
                ppv[0] = None
            except Exception:
                pass
            return _signed(E_NOINTERFACE)

        def add_ref(this):
            self.refs += 1
            return self.refs

        def release(this):
            self.refs = max(0, self.refs - 1)
            return self.refs

        def ok(*args):
            return S_OK

        def refuse(*args):
            try:
                return _signed(decide.pre_other())
            except Exception:
                return _signed(HR_CANCELLED)

        def pre_delete(this, flags, item):
            try:
                return _signed(decide.pre_delete(flags, item_path(item)))
            except Exception:
                return _signed(HR_CANCELLED)

        def post_delete(this, flags, item, hr, created):
            try:
                decide.post_delete(flags, item_path(item), hr, bool(created))
            except Exception:
                decide.stray = True
            return S_OK

        self._funcs = [
            P(H, V, ctypes.POINTER(_GUID), ctypes.POINTER(V))(qi),
            P(U, V)(add_ref),
            P(U, V)(release),
            P(H, V)(ok),                                # StartOperations
            P(H, V, H)(ok),                             # FinishOperations
            P(H, V, D, V, W)(refuse),                   # PreRenameItem
            P(H, V, D, V, W, H, V)(ok),                 # PostRenameItem
            P(H, V, D, V, V, W)(refuse),                # PreMoveItem
            P(H, V, D, V, V, W, H, V)(ok),              # PostMoveItem
            P(H, V, D, V, V, W)(refuse),                # PreCopyItem
            P(H, V, D, V, V, W, H, V)(ok),              # PostCopyItem
            P(H, V, D, V)(pre_delete),                  # PreDeleteItem
            P(H, V, D, V, H, V)(post_delete),           # PostDeleteItem
            P(H, V, D, V, W)(refuse),                   # PreNewItem
            P(H, V, D, V, W, W, D, H, V)(ok),           # PostNewItem
            P(H, V, ctypes.c_uint, ctypes.c_uint)(ok),  # UpdateProgress
            P(H, V)(ok), P(H, V)(ok), P(H, V)(ok),      # ResetTimer, PauseTimer, ResumeTimer
        ]
        self._vtbl = (ctypes.c_void_p * len(self._funcs))(*[ctypes.cast(f, ctypes.c_void_p) for f in self._funcs])
        self._obj = (ctypes.c_void_p * 1)(ctypes.addressof(self._vtbl))
        self.pointer = ctypes.addressof(self._obj)


def shell_recycle(path, owner, before, decide):
    """Ask IFileOperation to recycle path, with a ProgressSink over decide (a
    RecycleOnly). Returns (HRESULT, any operations aborted): the HRESULT of the
    first step that failed, else PerformOperations'. Nothing is asked of the
    shell unless the sink is in place."""
    ole32 = ctypes.WinDLL("ole32")
    shell32 = ctypes.WinDLL("shell32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_uint32,
                                       ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoCreateInstance.restype = ctypes.c_long
    shell32.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(_GUID),
                                                    ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
    init = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if init & 0xFFFFFFFF == RPC_E_CHANGED_MODE:
        # This thread is in the multithreaded apartment, and IFileOperation only
        # works in a single-threaded one: run it on a thread of its own.
        import threading
        out = {}

        def run():
            try:
                out["result"] = shell_recycle(path, owner, before, decide)
            except BaseException as e:          # handed back to the caller below
                out["error"] = e
        worker = threading.Thread(target=run, name="openevp-recycle", daemon=True)
        worker.start()
        worker.join()
        if "error" in out:
            raise out["error"]
        return out["result"]
    op, item, cookie = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_uint32()
    sink, advised = None, False
    try:
        if init < 0:
            return init & 0xFFFFFFFF, False
        hr = ole32.CoCreateInstance(ctypes.byref(_guid(CLSID_FileOperation)), None, CLSCTX_ALL,
                                    ctypes.byref(_guid(IID_IFileOperation)), ctypes.byref(op))
        if hr < 0:
            return hr & 0xFFFFFFFF, False
        hr = shell32.SHCreateItemFromParsingName(path, None, ctypes.byref(_guid(IID_IShellItem)),
                                                 ctypes.byref(item))
        if hr < 0:
            return hr & 0xFFFFFFFF, False
        sink = ProgressSink(decide)
        hr = _method(op, 3, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))(sink.pointer, ctypes.byref(cookie))
        if hr < 0:                                                    # Advise
            return hr & 0xFFFFFFFF, False
        advised = True
        hr = _method(op, 5, ctypes.c_uint32)(RECYCLE_FLAGS)           # SetOperationFlags
        if hr < 0:
            return hr & 0xFFFFFFFF, False
        if owner:
            _method(op, 9, ctypes.c_void_p)(owner)                    # SetOwnerWindow
        hr = _method(op, 18, ctypes.c_void_p, ctypes.c_void_p)(item, None)   # DeleteItem
        if hr < 0:
            return hr & 0xFFFFFFFF, False
        if before is not None:
            before()
        hr = _method(op, 21)()                                        # PerformOperations
        aborted = ctypes.c_int(0)
        _method(op, 22, ctypes.POINTER(ctypes.c_int))(ctypes.byref(aborted))   # GetAnyOperationsAborted
        return hr & 0xFFFFFFFF, bool(aborted.value)
    finally:
        if advised:
            _method(op, 4, ctypes.c_uint32)(cookie.value)             # Unadvise
        _release(item.value)
        _release(op.value)
        if sink is not None and sink.refs > 0:
            _LINGERING.append(sink)                      # the shell still holds it: keep its code alive
        if init >= 0:                                    # S_OK or S_FALSE
            ole32.CoUninitialize()


def _bin_exists(root):
    """Does SHQueryRecycleBinW know a Recycle Bin for the drive at root?"""
    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    shell32.SHQueryRecycleBinW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(SHQUERYRBINFO)]
    shell32.SHQueryRecycleBinW.restype = ctypes.c_long
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.restype = None
    init = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    try:
        info = SHQUERYRBINFO(cbSize=ctypes.sizeof(SHQUERYRBINFO))
        return shell32.SHQueryRecycleBinW(root, ctypes.byref(info)) == 0
    finally:
        if init >= 0:
            ole32.CoUninitialize()


def recycle(path, owner=None, before=None, shell=None):
    """Move one folder (or file) to the Recycle Bin, or raise RecycleError in
    plain words. Never deletes permanently: a drive without a Recycle Bin, a
    Recycle Bin set to delete immediately and a folder too big for it are refused
    up front where the settings show it, and whatever the shell would still
    delete for good is vetoed while it runs (the item stays where it is).
    owner: the app window's handle, or None. before: called right before the
    shell is asked (to let go of the folder). shell: shell_recycle, or a fake
    with its signature in tests."""
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
        raise RecycleError(TOO_LONG.format(name))
    path = real
    root = os.path.splitdrive(path)[0] + "\\"
    if drive_type(root) != DRIVE_FIXED:
        raise RecycleError(said(NO_RECYCLE_BIN))
    if bin_refuses(root, tree_size(path)):
        raise RecycleError(said(NO_ROOM))
    if not _bin_exists(root):
        raise RecycleError(said(NO_RECYCLE_BIN))
    decide = RecycleOnly(path)
    hr, aborted = (shell or shell_recycle)(path, owner or None, before, decide)
    if decide.succeeded(hr, aborted):
        return
    if decide.vetoed == "nuke":
        raise RecycleError(said(NO_ROOM))
    code = decide.failure if decide.failure is not None else hr & 0xFFFFFFFF
    win32 = code & 0xFFFF if code & 0xFFFF0000 == 0x80070000 else None
    if code in (COPYENGINE_E_RECYCLE_FORCE_NUKE, COPYENGINE_E_RECYCLE_SIZE_TOO_BIG,
                COPYENGINE_E_RECYCLE_UNKNOWN_ERROR):
        raise RecycleError(said(NO_ROOM))
    if code == COPYENGINE_E_RECYCLE_BIN_NOT_FOUND:
        raise RecycleError(said(NO_RECYCLE_BIN))
    if code == COPYENGINE_E_RECYCLE_PATH_TOO_LONG:
        raise RecycleError(TOO_LONG.format(name))
    if (code in (S_OK, HR_CANCELLED, COPYENGINE_E_USER_CANCELLED, COPYENGINE_E_CANCELLED,
                 COPYENGINE_E_ACCESS_DENIED_SRC, COPYENGINE_E_SHARING_VIOLATION_SRC)
            or win32 in (5, 32, 33)):                # access denied, sharing or lock violation
        raise RecycleError((FILE_IN_USE if is_file else IN_USE).format(name))
    raise RecycleError(f"{name} was not moved to the Recycle Bin (code 0x{code:x}).")
