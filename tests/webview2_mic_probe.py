"""Run by test_mic_webview2.py in a process of its own: a hidden WebView2 window with
OpenEVP's microphone handlers (app.mic_permission) and Chromium's fake audio input
only (no real input is ever listed or opened). Prints one JSON line with what
happened:

- top:        getUserMedia from the app's own page (should work, with no prompt);
- frame:      getUserMedia from a same-origin iframe allowed "microphone" (denied);
- navigation: the page sends the window to another local server (cancelled);
- same_origin_page: the window goes to another page of the app's own server, which
  then asks for the microphone (denied: not the pinned page).
"""
import functools
import http.server
import json
import os
import sys
import tempfile
import threading
import time

os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--use-fake-device-for-media-stream"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import webview  # noqa: E402

from app import mic_permission  # noqa: E402

ASK = """<!DOCTYPE html><html><body><script>
window.R = null;
async function ask() {
  try { const s = await navigator.mediaDevices.getUserMedia({audio: true}); s.getTracks().forEach(t => t.stop()); return "allowed"; }
  catch (e) { return e.name; }
}
</script>%s</body></html>"""

PAGES = {
    "index.html": ASK % '<iframe id="f" src="frame.html" allow="microphone"></iframe>',
    "frame.html": ASK % "",
    "other.html": ASK % "",
}


def serve(folder):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=folder)
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def wait_js(window, js, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = window.evaluate_js(js)
        if v:
            return v
        time.sleep(0.2)
    return None


def run(window, out, foreign_url, state):
    try:
        window.events.loaded.wait(30)
        out["policy"] = state["policy"] is not None
        out["pinned"] = state["policy"] is not None and state["policy"].origin is not None
        window.evaluate_js("ask().then(r => { window.R = r; }); 'x'")
        out["top"] = wait_js(window, "window.R")
        window.evaluate_js("document.getElementById('f').contentWindow.ask().then(r => { window.F = r; }); 'x'")
        out["frame"] = wait_js(window, "window.F")
        before = window.get_current_url()
        window.evaluate_js(f"location.href = {json.dumps(foreign_url)}; 'x'")
        time.sleep(2)
        out["navigation"] = "cancelled" if window.get_current_url() == before else window.get_current_url()
        window.events.loaded.clear()
        window.evaluate_js("location.href = 'other.html'; 'x'")
        window.events.loaded.wait(10)
        time.sleep(0.5)
        window.evaluate_js("ask().then(r => { window.R = r; }); 'x'")
        out["same_origin_page"] = wait_js(window, "window.R")
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        window.destroy()


def main():
    with tempfile.TemporaryDirectory() as app_dir, tempfile.TemporaryDirectory() as other_dir:
        for name, html in PAGES.items():
            with open(os.path.join(app_dir, name), "w", encoding="utf-8") as f:
                f.write(html)
        with open(os.path.join(other_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(ASK % "")
        app_srv, other_srv = serve(app_dir), serve(other_dir)
        out = {}
        url = f"http://127.0.0.1:{app_srv.server_address[1]}/index.html"
        window = webview.create_window("probe", url, hidden=True)
        state = mic_permission.install_early(window, on_problem=lambda e: out.setdefault("problem", repr(e)))
        webview.start(run, (window, out, f"http://127.0.0.1:{other_srv.server_address[1]}/index.html", state),
                      gui="edgechromium")
        app_srv.shutdown()
        other_srv.shutdown()
        app_srv.server_close()
        other_srv.server_close()
        print(json.dumps(out))


if __name__ == "__main__":
    main()
