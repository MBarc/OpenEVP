"""Microphone permission for the app's own page (Live mode and analog import).

WebView2 asks the host before a page may use the microphone (its
PermissionRequested event); with no handler it shows its own prompt, and since
pywebview runs WebView2 in private mode that prompt would come back every
session. pywebview does not handle the event, so this adds a handler on the
CoreWebView2 of the app's window (pythonnet, on the window's GUI thread):

- a microphone request from the app's own page (the same scheme, host and port
  as the page shown, served by pywebview on 127.0.0.1 or localhost) is allowed,
  without a prompt and without saving it in the profile;
- a microphone request from anywhere else is denied;
- every other kind of request is left to WebView2 as before.

Windows' own privacy switch ("Let desktop apps access your microphone") still
applies: when it is off, the page's getUserMedia fails and the page says how to
turn it on.
"""
from urllib.parse import urlsplit

LOCAL_HOSTS = ("127.0.0.1", "localhost")
_handlers = []              # the .NET event handlers added, kept alive for the life of the process


def own_page(uri, page):
    """Is a request from uri made by the page at page (the app's own local page)?"""
    try:
        a, b = urlsplit(str(uri)), urlsplit(str(page))
        return (a.scheme == b.scheme == "http" and a.hostname in LOCAL_HOSTS and a.hostname == b.hostname
                and a.port is not None and a.port == b.port)
    except ValueError:
        return False


def decide(kind, uri, page):
    """"allow", "deny" or None (leave it to WebView2) for one permission request."""
    if kind != "Microphone":
        return None
    return "allow" if own_page(uri, page) else "deny"


def install(window):
    """Add the handler to the window's WebView2 (once its CoreWebView2 exists: after
    the page has loaded). Returns True if it was added."""
    import clr
    from System import Func, Type
    from webview.util import interop_dll_path
    clr.AddReference(interop_dll_path("Microsoft.Web.WebView2.Core.dll"))
    from Microsoft.Web.WebView2.Core import CoreWebView2PermissionState

    def on_request(sender, args):
        try:
            verdict = decide(str(args.PermissionKind), args.Uri, sender.Source)
            if verdict == "allow":
                args.SavesInProfile = False
                args.State = CoreWebView2PermissionState.Allow
            elif verdict == "deny":
                args.State = CoreWebView2PermissionState.Deny
        except Exception:                          # never allow by mistake
            if str(args.PermissionKind) == "Microphone":
                args.State = CoreWebView2PermissionState.Deny

    added = []

    def add():
        core = window.native.webview.CoreWebView2
        if core is not None:
            core.PermissionRequested += on_request
            added.append(True)
    window.native.Invoke(Func[Type](add))
    if added:
        _handlers.append(on_request)
    return bool(added)


def install_when_loaded(window, on_problem=None):
    """Add the handler the first time the window's page has loaded."""
    state = {"done": False}

    def loaded():
        if state["done"]:
            return
        try:
            state["done"] = install(window)
        except Exception as e:
            if on_problem is not None:
                on_problem(e)
    window.events.loaded += loaded
    return state
