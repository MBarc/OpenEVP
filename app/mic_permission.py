"""Microphone permission for the app's own page (Live mode and analog import).

WebView2 asks the host before a page may use the microphone (its
PermissionRequested event); with no handler it shows its own prompt, and since
pywebview runs WebView2 in private mode that prompt would come back every
session. pywebview does not handle the event, so this adds handlers on the
CoreWebView2 of the app's window (pythonnet, on the window's GUI thread), with
the app's page pinned when they are added: the URL pywebview loaded the window
with (window.real_url, served by pywebview's own server on 127.0.0.1), never
whatever the window shows later.

- A microphone request is allowed, without a prompt and without saving it in
  the profile, only when it comes from the pinned origin (scheme, host and
  port) and the window still shows the pinned page.
- A microphone request from anywhere else is denied, and so is every
  microphone request from a frame inside the page (each frame gets a handler
  of its own that denies it and stops it reaching the window's handler).
- The window cannot be navigated away from the pinned origin: any top-level
  navigation elsewhere is cancelled (the app's page has no links of its own).
- Every other kind of request is left to WebView2, as before. If the page
  cannot be pinned (not an http page on 127.0.0.1 or localhost), every
  microphone request is denied.

Windows' own privacy switch ("Let desktop apps access your microphone") still
applies: when it is off, the page's getUserMedia fails and the page says how to
turn it on.
"""
from urllib.parse import urlsplit

LOCAL_HOSTS = ("127.0.0.1", "localhost")
_handlers = []              # the .NET event handlers added, kept alive for the life of the process


def _origin(url):
    """(scheme, host, port) of an http(s) URL, or None."""
    try:
        u = urlsplit(str(url))
        port = u.port
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    return u.scheme, u.hostname, port if port is not None else (443 if u.scheme == "https" else 80)


def _path(url):
    try:
        return urlsplit(str(url)).path or "/"
    except ValueError:
        return None


class Policy:
    """What to allow, for the app page pinned at start (its URL)."""

    def __init__(self, page_url):
        origin = _origin(page_url)
        ok = origin is not None and origin[0] == "http" and origin[1] in LOCAL_HOSTS
        self.origin = origin if ok else None
        self.path = _path(page_url) if ok else None

    def decide(self, kind, uri, top, from_frame=False):
        """"allow", "deny" or None (leave it to WebView2) for one permission request:
        kind (its PermissionKind name), uri (who asks), top (the URL the window shows)."""
        if kind != "Microphone":
            return None
        if from_frame or self.origin is None:
            return "deny"
        if _origin(uri) == self.origin and _origin(top) == self.origin and _path(top) == self.path:
            return "allow"
        return "deny"

    def may_navigate(self, uri):
        """May the window's top-level page go to uri? Only within the pinned origin."""
        return self.origin is not None and _origin(uri) == self.origin


def _attach(core, policy):
    """Add the handlers to a CoreWebView2 (on the window's GUI thread)."""
    import clr
    from webview.util import interop_dll_path
    clr.AddReference(interop_dll_path("Microsoft.Web.WebView2.Core.dll"))
    from Microsoft.Web.WebView2.Core import CoreWebView2PermissionState

    def answer(args, verdict):
        if verdict == "allow":
            args.SavesInProfile = False
            args.State = CoreWebView2PermissionState.Allow
        elif verdict == "deny":
            args.State = CoreWebView2PermissionState.Deny

    def on_request(sender, args):
        try:
            answer(args, policy.decide(str(args.PermissionKind), args.Uri, sender.Source))
        except Exception:                          # never allow by mistake
            if str(args.PermissionKind) == "Microphone":
                args.State = CoreWebView2PermissionState.Deny

    def on_frame_request(sender, args):
        try:
            if str(args.PermissionKind) == "Microphone":
                args.State = CoreWebView2PermissionState.Deny
                args.Handled = True                # never reaches the window's handler
        except Exception:
            pass

    def on_frame(sender, args):
        try:
            args.Frame.PermissionRequested += on_frame_request
        except Exception:
            pass

    def on_navigation(sender, args):
        try:
            if not policy.may_navigate(args.Uri):
                args.Cancel = True
        except Exception:
            args.Cancel = True

    core.PermissionRequested += on_request
    core.FrameCreated += on_frame
    core.NavigationStarting += on_navigation
    _handlers.extend((on_request, on_frame_request, on_frame, on_navigation))


def install(window, page_url=None):
    """Add the handlers to the window's WebView2 now (its CoreWebView2 must exist).
    page_url: the app's page to pin (default window.real_url, the URL pywebview
    loaded). Frames made before this are not covered: install_early() is what the
    app uses. Returns the Policy, or None when there was no CoreWebView2 yet."""
    from System import Func, Type
    policy = Policy(page_url if page_url is not None else getattr(window, "real_url", None))
    added = []

    def add():
        core = window.native.webview.CoreWebView2
        if core is not None:
            _attach(core, policy)
            added.append(True)
    window.native.Invoke(Func[Type](add))
    return policy if added else None


def install_early(window, on_problem=None):
    """Add the handlers as WebView2 starts, before the app's page (or any frame in
    it) exists: the window's before_show event (on its GUI thread, synchronously)
    subscribes to the WebView2 control's CoreWebView2InitializationCompleted, which
    attaches them. state["policy"] is the Policy once attached."""
    state = {"policy": None}

    def problem(e):
        if on_problem is not None:
            on_problem(e)

    def initialized(sender, args):
        try:
            if args.IsSuccess and state["policy"] is None:
                policy = Policy(getattr(window, "real_url", None))
                _attach(sender.CoreWebView2, policy)
                state["policy"] = policy
        except Exception as e:
            problem(e)

    def before_show():
        try:
            window.native.webview.CoreWebView2InitializationCompleted += initialized
            _handlers.append(initialized)
        except Exception as e:
            problem(e)
    window.events.before_show += before_show
    return state
