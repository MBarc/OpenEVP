"""Where OpenEVP saves by default, and showing a folder in Explorer.

Shared by the desktop app and the command-line downloader.
"""
import os
import sys


def documents_dir():
    """Return (folder, warning). The folder is the user's real Documents folder
    (following OneDrive/folder redirection on Windows); warning is None unless it
    could not be determined and a fallback is used - never a silently invented path."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD),
                            ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]
            # FOLDERID_Documents
            documents = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
            path = ctypes.c_wchar_p()
            hr = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(documents), 0, None, ctypes.byref(path))
            result = path.value if hr == 0 else None
            ctypes.windll.ole32.CoTaskMemFree(path)        # required even when the call fails
            if result and os.path.isdir(result):
                return result, None
            reason = f"Windows did not report a Documents folder (error {hr & 0xFFFFFFFF:#x})"
        except Exception as e:
            reason = f"the Documents folder could not be determined ({e})"
    else:
        reason = None
    home = os.path.expanduser("~")
    guess = os.path.join(home, "Documents")
    if os.path.isdir(guess):
        return guess, reason and f"{reason}; using {guess}"
    return home, f"{reason or 'no Documents folder found'}; using your home folder {home} instead"


def default_output():
    """Return (folder, warning): <Documents>\\OpenEVP. Only shown here; it is created
    by the first export, never just because a program started."""
    base, warning = documents_dir()
    return os.path.join(base, "OpenEVP"), warning


def open_folder(path):
    """Show the folder in Explorer (Windows only). Returns False if that failed."""
    if sys.platform != "win32":
        return False
    try:
        os.startfile(path)
        return True
    except OSError:
        return False
