"""The Windows side of sharing library files (app.sharing): a real file drag, as
File Explorer starts one, and files on the clipboard. Both run on the window's GUI
thread (the WinForms form pywebview hosts WebView2 in), where OLE drag and drop
and the clipboard need to be.

How the drag starts: the page cancels the browser's own drag of a library row
(dragstart, preventDefault) and calls Api.drag_out(); this then calls
Control.DoDragDrop on the form while the mouse button is still down, with the files
as CF_HDROP (DataFormats.FileDrop) and Copy as the only effect, so a drop target
can never move the original. DoDragDrop runs Windows' drag loop and returns when
the drag ends. Dropped back on OpenEVP's own page, it arrives as an ordinary file
drag (the page moves the recordings when that is onto a library folder)."""


DROPEFFECT_COPY = 1


def _winforms():
    import clr
    clr.AddReference("System.Windows.Forms")
    import System.Windows.Forms as WinForms
    return WinForms


def _on_gui_thread(form, fn):
    """Run fn() on the form's GUI thread and return what it returned (raises what it raised)."""
    from System import Func, Type
    out = {}

    def run():
        try:
            out["value"] = fn()
        except Exception as e:                       # raised in the caller's thread, not lost in .NET
            out["error"] = e
    form.Invoke(Func[Type](run))
    if "error" in out:
        raise out["error"]
    return out.get("value")


def _file_list(paths):
    from System import Array, String
    return Array[String]([str(p) for p in paths])


def drag_data(paths):
    """What a drag carries: the files as CF_HDROP (DataFormats.FileDrop)."""
    WinForms = _winforms()
    return WinForms.DataObject(WinForms.DataFormats.FileDrop, _file_list(paths))


def clipboard_data(paths):
    """What Copy file puts on the clipboard: the files as CF_HDROP, and Preferred
    DropEffect Copy, so a paste in File Explorer copies them."""
    WinForms = _winforms()
    from System import Array, Byte
    from System.IO import MemoryStream
    data = WinForms.DataObject()
    data.SetData(WinForms.DataFormats.FileDrop, _file_list(paths))
    data.SetData("Preferred DropEffect", MemoryStream(Array[Byte](bytes([DROPEFFECT_COPY, 0, 0, 0]))))
    return data


def drag_files(form, paths):
    """A file drag of paths from the form: "copy" when it was dropped on something
    that took the files, "none" when it was not, None when the left mouse button
    was already up (the user let go while the files were being prepared)."""
    WinForms = _winforms()

    def drag():
        if not WinForms.Control.MouseButtons.HasFlag(WinForms.MouseButtons.Left):
            return None
        effect = form.DoDragDrop(drag_data(paths), WinForms.DragDropEffects.Copy)
        return "copy" if effect.HasFlag(WinForms.DragDropEffects.Copy) else "none"
    return _on_gui_thread(form, drag)



def copy_files(form, paths):
    """Put paths on the clipboard as files (CF_HDROP, with Preferred DropEffect
    Copy), as Copy in File Explorer does; they stay there after OpenEVP closes."""
    WinForms = _winforms()

    def copy():
        # copy=True: kept after OpenEVP closes; 10 tries 100 ms apart if the clipboard is busy
        WinForms.Clipboard.SetDataObject(clipboard_data(paths), True, 10, 100)
    _on_gui_thread(form, copy)
