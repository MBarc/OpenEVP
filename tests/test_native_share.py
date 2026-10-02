"""app.native_share: what a drag out and Copy file hand to Windows (built with
WinForms through pythonnet, as the app does). No drag is started and the
clipboard is not touched: both act on the real desktop (the drag drops wherever
the cursor is), so they are checked by hand (docs/app-test-checklist.md)."""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import clr  # noqa: F401  (pythonnet, as pywebview loads it on Windows)
    HAVE_CLR = sys.platform == "win32"
except Exception:
    HAVE_CLR = False


@unittest.skipUnless(HAVE_CLR, "Windows with pythonnet")
class NativeShareTests(unittest.TestCase):
    def setUp(self):
        from app import native_share
        self.ns = native_share
        self.WF = native_share._winforms()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = []
        for name in ("Old Mill 1.mp3", "knock é.wav"):
            p = os.path.join(tmp.name, name)
            with open(p, "wb") as f:
                f.write(b"x")
            self.paths.append(p)

    def files(self, data):
        return [str(p) for p in data.GetData(self.WF.DataFormats.FileDrop)]

    def test_a_drag_carries_the_files_only(self):
        data = self.ns.drag_data(self.paths)
        self.assertEqual(self.files(data), self.paths)
        self.assertTrue(data.GetDataPresent(self.WF.DataFormats.FileDrop))

    def test_the_clipboard_gets_the_files_and_copy(self):
        data = self.ns.clipboard_data(self.paths)
        self.assertEqual(self.files(data), self.paths)
        stream = data.GetData("Preferred DropEffect")
        stream.Position = 0
        self.assertEqual([stream.ReadByte() for _ in range(4)], [1, 0, 0, 0])   # DROPEFFECT_COPY

    def test_no_drag_once_the_button_is_up(self):
        """drag_files() checks the button on the GUI thread before DoDragDrop: let go
        (while an MP3 was being made) means no drag. The button state is faked; the
        form stand-in runs the call in place and must never be asked to drag."""
        form = mock.Mock()
        form.Invoke.side_effect = lambda f: f()
        up = mock.Mock()
        up.HasFlag.return_value = False
        wf = types.SimpleNamespace(Control=types.SimpleNamespace(MouseButtons=up), MouseButtons=self.WF.MouseButtons,
                                   DragDropEffects=self.WF.DragDropEffects)
        with mock.patch.object(self.ns, "_winforms", return_value=wf):
            self.assertIsNone(self.ns.drag_files(form, self.paths))
        up.HasFlag.assert_called_once_with(self.WF.MouseButtons.Left)
        form.DoDragDrop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
