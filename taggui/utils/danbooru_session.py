"""Cloudflare-challenge-aware HTTP transport for Danbooru.

Danbooru (``danbooru.donmai.us``, and its beta ``betabooru.donmai.us``) sits
behind a Cloudflare "managed challenge" that blocks anonymous, non-JavaScript
HTTP clients with an HTTP 403 "Just a moment..." interstitial (identified by
the ``Cf-Mitigated: challenge`` response header). A plain ``urllib`` request --
and even a request carrying a perfect browser TLS fingerprint -- cannot pass
it, because clearing the challenge requires executing the page's JavaScript.

Approach (a.k.a. "Option B"): the embedded Chromium engine (QtWebEngine, already
shipped with PySide6) clears the challenge *once* in a small, visible, modal
window (so the user can complete an interactive checkbox if Cloudflare shows
one). After that, every Danbooru API request is performed **inside that same
browser page** via JavaScript ``fetch()`` -- so the request automatically
carries the browser's cookies *and* its exact TLS fingerprint, which is what
Cloudflare bound the clearance to. The response bytes are handed back to Python
over a ``QWebChannel`` bridge.

This is more robust than harvesting the ``cf_clearance`` cookie and replaying it
with a separate HTTP client: Cloudflare binds the clearance to the browser's TLS
fingerprint, so an external client (even one impersonating Chrome) is
re-challenged. Fetching through the page sidesteps that entirely.

The public entry point, :func:`danbooru_urlopen`, is a drop-in replacement for
``urllib.request.urlopen`` as the wiki dialog uses it: it supports the
``with ... as response`` form and exposes ``status``, ``read()`` and a
``headers`` object with ``get_content_type()``.

Design notes
------------
* All QtWebEngine work runs on the Qt GUI (main) thread. The wiki dialog
  performs its fetches on background ``QThread`` workers; a worker hands its
  request to the main thread via a queued Qt signal and then waits on a plain
  ``threading.Event``. The main thread stays on its normal event loop (the only
  nested loop is the brief, modal challenge solve), so closing the wiki while a
  fetch is in flight is safe and does not crash.
* :func:`initialize_on_main_thread` must be called once from the main thread
  (the wiki dialog does this in its constructor) before any fetch.
* :func:`cancel_pending_fetches` releases any worker blocked on an in-flight
  fetch; the wiki dialog calls it during teardown so worker threads unwind
  promptly.
* Requests to hosts *not* behind the challenge (e.g. the ``*.donmai.us`` image
  CDN) use a plain ``urllib`` request, since they are served directly.
* If QtWebEngine is unavailable, the transport falls back to a plain ``urllib``
  request so the app still functions (it will simply surface the 403 the way
  the old code did).
"""

import os
import sys
import threading
import uuid
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import urlopen as _urllib_urlopen

from PySide6.QtCore import (QFile, QIODevice, QObject, Qt, QThread, QTimer,
                            QUrl, Signal, Slot)
from PySide6.QtWidgets import QApplication

# Both the production and beta sites share one Cloudflare zone, so the same
# clearance applies to any of them. The whole ``*.donmai.us`` zone (including
# the ``cdn.donmai.us`` image host) sits behind the same challenge, so every
# ``donmai.us`` host is routed through the browser.
_CHALLENGE_SUFFIX = 'donmai.us'

# How long to wait for the browser to run the challenge JavaScript. Generous
# so that a one-off interactive checkbox (if Cloudflare shows one) can be
# completed by the user before we give up.
_SOLVE_TIMEOUT_MS = 90000
# Once the ``cf_clearance`` cookie appears, wait briefly for the final
# navigation/cookies to settle before treating the page as ready.
_SOLVE_SETTLE_MS = 1500

# Name of the shared QtWebEngine profile. Also used by the "Remove app data"
# cleanup (utils/app_data.py) to locate and delete the on-disk profile folder.
DANBOORU_PROFILE_NAME = 'taggui-danbooru'

_solver = None

# Hosts we have actually seen Cloudflare challenge this session. Until a host is
# in here, each request tries a lightweight plain HTTP request first, so if
# Danbooru is not (or no longer) challenging, the embedded browser is never even
# created and behaviour is identical to the original plain-HTTP app.
_known_challenged_hosts = set()


# Set the TAGGUI_DANBOORU_DEBUG environment variable to '1' to print the
# step-by-step '[danbooru]' Cloudflare/transport messages to the console. Off by
# default so normal use stays quiet.
_DEBUG_LOG = os.environ.get('TAGGUI_DANBOORU_DEBUG', '') == '1'


def _log(message: str):
    if _DEBUG_LOG:
        print(f'[danbooru] {message}', file=sys.__stderr__)


def _challenged_host(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ''
    except Exception:
        return False
    return host == _CHALLENGE_SUFFIX or host.endswith('.' + _CHALLENGE_SUFFIX)


def _origin_of(url: str) -> str:
    parsed = urlparse(url)
    origin = f'{parsed.scheme}://{parsed.hostname}'
    if parsed.port:
        origin += f':{parsed.port}'
    return origin


def _cookie_covers(cookie_domain: str, host: str) -> bool:
    """True if a cookie set for ``cookie_domain`` is sent to ``host``."""
    domain = (cookie_domain or '').lstrip('.')
    if not domain:
        return False
    return host == domain or host.endswith('.' + domain)


class _Headers:
    """Minimal adapter exposing the header accessors the wiki dialog uses."""

    def __init__(self, content_type: str):
        self._content_type = content_type or ''

    def get(self, name, default=None):
        if (name or '').lower() == 'content-type':
            return self._content_type or default
        return default

    def get_content_type(self) -> str:
        return self._content_type.split(';')[0].strip() or 'application/octet-stream'


class _BytesResponse:
    """A ``urllib``-response-like wrapper around bytes fetched via the page."""

    def __init__(self, status: int, content_type: str, body: bytes):
        self.status = status
        self.headers = _Headers(content_type)
        self._body = body or b''

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exception_info):
        return False


class _UrllibResponseAdapter:
    """Fallback wrapper so a plain ``urllib`` response exposes ``status``.

    ``http.client.HTTPResponse`` already provides ``read()`` and ``headers`` and
    works as a context manager; only ``status`` is guaranteed, so this simply
    passes everything through unchanged."""

    def __init__(self, response):
        self._response = response

    def __getattr__(self, name):
        return getattr(self._response, name)

    def __enter__(self):
        return self

    def __exit__(self, *exception_info):
        self._response.close()
        return False


def _looks_like_challenge(status: int, body: bytes) -> bool:
    """True when a page fetch came back as a Cloudflare challenge interstitial."""
    if status not in (403, 429, 503):
        return False
    try:
        head = (body or b'')[:1500].decode('utf-8', 'replace').lower()
    except Exception:
        return True
    return ('just a moment' in head or 'cf-chl' in head
            or 'challenge-platform' in head or 'cf-mitigated' in head)


# JavaScript executed inside the cleared Danbooru page to perform one request.
# ``__RID__`` and ``__URL__`` are substituted per call. The response body is
# returned (base64-encoded, so binary payloads survive) over the QWebChannel
# bridge installed as ``window.__tagguiBridge``.
_FETCH_JS = r'''(function(){
  var rid = "__RID__";
  var url = __URL__;
  function send(status, ct, b64){
    if (window.__tagguiBridge) { window.__tagguiBridge.deliver(rid, status, ct, b64); }
    else { setTimeout(function(){ send(status, ct, b64); }, 50); }
  }
  try {
    fetch(url, {credentials: 'include'}).then(function(r){
      var ct = r.headers.get('content-type') || '';
      return r.arrayBuffer().then(function(buf){
        var bytes = new Uint8Array(buf);
        var binary = '';
        var chunk = 0x8000;
        for (var i = 0; i < bytes.length; i += chunk) {
          binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
        }
        send(r.status, ct, btoa(binary));
      });
    }).catch(function(){ send(0, '', ''); });
  } catch (e) { send(0, '', ''); }
})();'''


# Runs inside the loaded page to tell whether we are looking at the Cloudflare
# challenge interstitial or the real site. Returns ``'challenge'`` or
# ``'cleared'``. This is how we recognise that an earlier clearance (persisted
# in the shared cookie store) already lets the page through -- in that case no
# new ``cf_clearance`` cookie is added, so the cookie signal alone never fires.
_CHALLENGE_PROBE_JS = r'''(function(){
  try {
    var title = (document.title || '');
    if (/just a moment|attention required|verifying|checking your browser/i.test(title)) {
      return 'challenge';
    }
    if (document.querySelector('#challenge-running, #challenge-form, #cf-chl-widget, .cf-turnstile, [id^="cf-chl"]')) {
      return 'challenge';
    }
    var html = (document.documentElement ? document.documentElement.innerHTML : '');
    if (/challenge-platform|cf-chl|turnstile\//i.test(html) && !/<body[^>]*id=["']?[cp]-/i.test(html)) {
      // Challenge scripts are present and the real site body is not.
      if (document.body && document.body.children.length < 3) {
        return 'challenge';
      }
    }
    return 'cleared';
  } catch (e) { return 'cleared'; }
})();'''


class _Bridge(QObject):
    """QWebChannel endpoint the page calls to return a fetched response."""

    def __init__(self, pending: dict, lock: threading.Lock):
        super().__init__()
        self._pending = pending
        self._lock = lock

    @Slot(str, int, str, str)
    def deliver(self, request_id, status, content_type, body_b64):
        import base64
        with self._lock:
            item = self._pending.get(request_id)
        if item is None:
            return
        event, holder = item
        try:
            body = base64.b64decode(body_b64) if body_b64 else b''
        except Exception:
            body = b''
        holder['status'] = int(status)
        holder['content_type'] = content_type or ''
        holder['body'] = body
        event.set()


class _OriginSession:
    """Owns one QtWebEngine page bound to a single origin.

    Every ``donmai.us`` origin we fetch from (the site itself and the
    ``cdn.donmai.us`` image host) gets its own page, because a page can only
    ``fetch()`` its own origin without tripping cross-origin restrictions. All
    sessions share one profile/cookie store, so a domain-wide ``cf_clearance``
    earned for one origin is reused by the others without a second challenge.
    """

    def __init__(self, solver, origin: str):
        self._solver = solver
        self.origin = origin
        try:
            self.host = urlparse(origin).hostname or ''
        except Exception:
            self.host = ''
        self.page = None
        self.view = None
        self.web_view = None
        self.channel = None
        self.bridge = None
        self.ready = False
        self.solving = False
        self.queued = []

    def ensure_browser(self):
        if self.page is not None:
            return
        from PySide6.QtWebChannel import QWebChannel
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineScript

        profile = self._solver.ensure_profile()
        page = QWebEnginePage(profile, self._solver)

        bridge = _Bridge(self._solver._pending, self._solver._pending_lock)
        channel = QWebChannel(page)
        channel.registerObject('bridge', bridge)
        page.setWebChannel(channel)

        # Install the QWebChannel client plus a tiny bootstrap on every document
        # so ``window.__tagguiBridge`` is available after each navigation.
        bootstrap = _read_qwebchannel_js() + (
            '\nnew QWebChannel(qt.webChannelTransport, function(channel){'
            ' window.__tagguiBridge = channel.objects.bridge; });\n')
        script = QWebEngineScript()
        script.setName('taggui_danbooru_bridge')
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        script.setSourceCode(bootstrap)
        page.scripts().insert(script)

        from PySide6.QtWidgets import (QLabel, QVBoxLayout, QWidget)
        from PySide6.QtWebEngineWidgets import QWebEngineView

        web_view = QWebEngineView()
        web_view.setPage(page)

        class _SolveWindow(QWidget):
            """Hosts the Cloudflare challenge page with a short explanatory
            header. Reports when the user closes it, so a mid-solve close ends
            the wait immediately instead of hanging."""

            closed = Signal()

            def closeEvent(self, event):
                self.closed.emit()
                super().closeEvent(event)

        window = _SolveWindow()
        window.setWindowTitle('Verifying access to Danbooru…')
        # A clean dialog frame (title + close button only, no maximize).
        window.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint)

        header = QLabel(
            'This app needs to pass a one-time Cloudflare security check for '
            'Danbooru.\nComplete the check below (tick the checkbox if one '
            'appears). This window closes automatically when it is done.')
        header.setWordWrap(True)
        header.setContentsMargins(12, 10, 12, 10)
        header.setStyleSheet('font-size: 12px;')

        layout = QVBoxLayout(window)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(web_view, 1)

        self.page = page
        self.bridge = bridge
        self.channel = channel
        self.web_view = web_view
        self.view = window

    def handle(self, request):
        self.ensure_browser()
        if request['force']:
            self.ready = False
        if not self.ready:
            self.queued.append(request)
            if not self.solving:
                self.solving = True
                try:
                    self._solve()
                finally:
                    self.solving = False
                ready = self.ready
                queued, self.queued = self.queued, []
                for queued_request in queued:
                    if ready:
                        self._issue_fetch(queued_request)
                    else:
                        self._solver._deliver_fail(queued_request['rid'])
            return
        self._issue_fetch(request)

    def _issue_fetch(self, request):
        import json
        request_id = request['rid']
        javascript = (_FETCH_JS
                      .replace('__RID__', request_id)
                      .replace('__URL__', json.dumps(request['url'])))
        self.page.runJavaScript(javascript)
        # Safety net: if the page never calls back, release the waiter.
        timeout_ms = int(request['timeout'] * 1000) + 5000
        QTimer.singleShot(
            timeout_ms,
            lambda rid=request_id: self._solver._deliver_fail(rid))

    def _solve(self):
        """Clear the challenge for this origin in a modal, always-on-top window.

        A managed challenge runs rendering- and visibility-gated JavaScript
        (requestAnimationFrame plus the Page Visibility API), which browsers
        pause for a non-visible page, so the window must be genuinely visible.
        Making it application-modal both brings it reliably to the front (so the
        user can click a checkbox if one appears) and prevents the wiki dialog
        from being torn down mid-solve -- which would otherwise crash the app.
        """
        from PySide6.QtCore import QEventLoop

        harvested = {'ok': False}
        shown = {'value': False}
        loop = QEventLoop()

        settle_timer = QTimer(self._solver)
        settle_timer.setSingleShot(True)
        settle_timer.timeout.connect(loop.quit)
        overall_timer = QTimer(self._solver)
        overall_timer.setSingleShot(True)
        overall_timer.timeout.connect(loop.quit)

        cookie_store = self.page.profile().cookieStore()

        def mark_cleared():
            if not harvested['ok']:
                harvested['ok'] = True
                settle_timer.start(_SOLVE_SETTLE_MS)

        def show_window():
            # Only reveal the verify window when an actual challenge needs the
            # user (or needs a visible, animating page to auto-solve). An
            # already-cleared lookup never shows a window at all.
            if shown['value'] or harvested['ok']:
                return
            shown['value'] = True
            self.view.setWindowModality(Qt.WindowModality.ApplicationModal)
            self.view.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            self.view.resize(460, 580)
            # Center the window over the active application window (falling back
            # to the primary screen) so it appears where the user is looking.
            try:
                application = QApplication.instance()
                reference = (application.activeWindow()
                             if application is not None else None)
                if reference is not None and reference is not self.view:
                    area = reference.frameGeometry()
                else:
                    screen = (application.primaryScreen()
                              if application is not None else None)
                    area = (screen.availableGeometry()
                            if screen is not None else None)
                if area is not None:
                    geometry = self.view.frameGeometry()
                    geometry.moveCenter(area.center())
                    self.view.move(geometry.topLeft())
            except Exception:
                pass
            self.view.show()
            self.view.raise_()
            self.view.activateWindow()

        def on_cookie_added(cookie):
            try:
                name = bytes(cookie.name()).decode('ascii', 'replace')
                domain = cookie.domain() or ''
            except Exception:
                return
            # A freshly issued cf_clearance cookie that applies to this host
            # means the user just solved the challenge for this origin.
            if name == 'cf_clearance' and _cookie_covers(domain, self.host):
                mark_cleared()

        def on_probe_result(result):
            # ``result`` is 'cleared' when the loaded document is the real site
            # rather than the Cloudflare interstitial. This covers the common
            # case where a persisted clearance already lets the page straight
            # through, so no new cookie is ever added. If it is still the
            # challenge, make the window visible so it can be solved.
            if result == 'cleared':
                mark_cleared()
            else:
                show_window()

        def on_load_finished(ok):
            _log(f'[{self.host}] challenge page navigation finished (ok={ok})')
            # Give the page a moment to run any challenge JS, then inspect it.
            QTimer.singleShot(
                400,
                lambda: self.page.runJavaScript(
                    _CHALLENGE_PROBE_JS, 0, on_probe_result))

        def on_window_closed():
            # The user closed the verify window; stop waiting immediately.
            loop.quit()

        cookie_store.cookieAdded.connect(on_cookie_added)
        try:
            cookie_store.loadAllCookies()
        except Exception:
            pass
        self.page.loadFinished.connect(on_load_finished)
        self.view.closed.connect(on_window_closed)

        # Fallback: if the first probe is slow (page still settling), reveal the
        # window so a real challenge is never left hidden and unsolvable.
        reveal_timer = QTimer(self._solver)
        reveal_timer.setSingleShot(True)
        reveal_timer.timeout.connect(
            lambda: show_window() if not harvested['ok'] else None)
        reveal_timer.start(4000)

        overall_timer.start(_SOLVE_TIMEOUT_MS)
        self.page.load(QUrl(self.origin + '/'))
        loop.exec()
        reveal_timer.stop()
        self.view.hide()

        overall_timer.stop()
        settle_timer.stop()
        try:
            cookie_store.cookieAdded.disconnect(on_cookie_added)
        except Exception:
            pass
        try:
            self.page.loadFinished.disconnect(on_load_finished)
        except Exception:
            pass
        try:
            self.view.closed.disconnect(on_window_closed)
        except Exception:
            pass

        self.ready = harvested['ok']
        if self.ready:
            _log(f'[{self.host}] cleared Cloudflare challenge; fetching '
                 f'through the browser page')
        else:
            _log(f'[{self.host}] challenge not cleared (final page title: '
                 f'{self.page.title()!r}); it may require interaction or '
                 f'timed out')


class _ChallengeSolver(QObject):
    """Lives on the main thread; owns per-origin QtWebEngine pages.

    Worker threads submit requests via the ``_fetch_requested`` signal (queued
    to this thread) and block on an event until the matching page returns the
    response bytes over the QWebChannel bridge.
    """

    _fetch_requested = Signal(object)

    def __init__(self):
        super().__init__()
        self._profile = None
        self._sessions = {}
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._fetch_requested.connect(self._on_fetch_requested)

    def ensure_profile(self):
        from PySide6.QtWebEngineCore import QWebEngineProfile
        if self._profile is None:
            profile = QWebEngineProfile(DANBOORU_PROFILE_NAME, self)
            try:
                profile.setPersistentCookiesPolicy(
                    QWebEngineProfile.PersistentCookiesPolicy
                    .ForcePersistentCookies)
            except Exception:
                pass
            self._profile = profile
        return self._profile

    # --- fetch path (worker side) ------------------------------------------
    def fetch(self, url: str, timeout: float, force: bool) -> dict:
        """Called from a worker thread; blocks until the page returns bytes."""
        request_id = uuid.uuid4().hex
        event = threading.Event()
        holder: dict = {}
        with self._pending_lock:
            self._pending[request_id] = (event, holder)
        request = {'rid': request_id, 'url': url, 'timeout': timeout,
                   'force': force}
        self._fetch_requested.emit(request)
        # The main thread always resolves the request (on success, failure, or
        # one of its safety-net timers), so this wait is only a last-resort
        # backstop. It must comfortably exceed the interactive challenge-solve
        # window, or a user who takes a while to click the Cloudflare checkbox
        # would see the request abandoned while the verify window is still open.
        backstop = _SOLVE_TIMEOUT_MS / 1000 + timeout + 20
        if not event.wait(backstop):
            holder.setdefault('status', 0)
        with self._pending_lock:
            self._pending.pop(request_id, None)
        return holder

    # --- fetch path (main-thread side) -------------------------------------
    @Slot(object)
    def _on_fetch_requested(self, request):
        try:
            origin = _origin_of(request['url'])
            session = self._sessions.get(origin)
            if session is None:
                session = _OriginSession(self, origin)
                self._sessions[origin] = session
            session.handle(request)
        except Exception as exception:
            _log(f'could not fetch through the browser: {exception}')
            self._deliver_fail(request['rid'])

    def _deliver_fail(self, request_id):
        with self._pending_lock:
            item = self._pending.get(request_id)
        if item is None:
            return
        event, holder = item
        holder.setdefault('status', 0)
        holder.setdefault('content_type', '')
        holder.setdefault('body', b'')
        event.set()

    def cancel_all(self):
        with self._pending_lock:
            items = list(self._pending.values())
            self._pending.clear()
        for event, holder in items:
            holder.setdefault('status', 0)
            holder.setdefault('content_type', '')
            holder.setdefault('body', b'')
            event.set()

    def shutdown(self):
        """Destroy all browser pages before the shared profile.

        QtWebEngine requires every ``QWebEnginePage`` to be deleted before its
        ``QWebEngineProfile``; otherwise it logs "Release of profile requested
        but WebEnginePage still not deleted. Expect troubles !" at exit. We
        delete the views and pages explicitly, then the profile, in that order.
        """
        try:
            import shiboken6
        except Exception:
            shiboken6 = None

        def _delete(obj):
            if obj is None:
                return
            if shiboken6 is not None:
                try:
                    if shiboken6.isValid(obj):
                        shiboken6.delete(obj)
                    return
                except Exception:
                    pass
            try:
                obj.deleteLater()
            except Exception:
                pass

        self.cancel_all()
        for session in list(self._sessions.values()):
            try:
                if session.view is not None:
                    session.view.close()
            except Exception:
                pass
            _delete(session.view)
            session.view = None
            _delete(session.web_view)
            session.web_view = None
            _delete(session.page)
            session.page = None
            session.channel = None
            session.bridge = None
        self._sessions.clear()
        _delete(self._profile)
        self._profile = None


def _read_qwebchannel_js() -> str:
    qfile = QFile(':/qtwebchannel/qwebchannel.js')
    if not qfile.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError('bundled qwebchannel.js resource is unavailable')
    try:
        return bytes(qfile.readAll()).decode('utf-8')
    finally:
        qfile.close()


def initialize_on_main_thread():
    """Create the solver on the GUI thread. Safe to call multiple times."""
    global _solver
    if _solver is not None:
        return
    application = QApplication.instance()
    if application is None:
        return
    if QThread.currentThread() is not application.thread():
        _log('initialize_on_main_thread() must be called from the GUI thread')
        return
    _solver = _ChallengeSolver()
    # Tear the browser pages down before the profile at application exit.
    try:
        application.aboutToQuit.connect(_solver.shutdown)
    except Exception:
        pass


def cancel_pending_fetches():
    """Release any worker blocked on an in-flight page fetch (used at teardown)."""
    solver = _solver
    if solver is not None:
        solver.cancel_all()


def _try_plain_request(url, timeout):
    """Attempt a plain HTTP request.

    Returns a response object on success or for a genuine application-level HTTP
    error (e.g. 404 for an unknown wiki page). Returns ``None`` only when the
    request was blocked by a Cloudflare challenge (so the caller should fall
    back to the embedded browser) or a transient network error occurred."""
    try:
        return _UrllibResponseAdapter(_urllib_urlopen(url, timeout=timeout))
    except HTTPError as error:
        mitigated = ''
        try:
            mitigated = (error.headers.get('Cf-Mitigated') or '') if \
                error.headers else ''
        except Exception:
            mitigated = ''
        body = b''
        try:
            body = error.read() or b''
        except Exception:
            body = b''
        if (mitigated.lower() == 'challenge'
                or _looks_like_challenge(error.code, body)):
            # Cloudflare blocked us; signal the caller to use the browser.
            return None
        # A real HTTP response (not a challenge) -- surface it to the caller.
        content_type = ''
        try:
            content_type = error.headers.get_content_type() if \
                error.headers else ''
        except Exception:
            content_type = ''
        return _BytesResponse(error.code, content_type, body)
    except Exception:
        # Network-level failure; let the caller decide (browser fallback).
        return None


def danbooru_urlopen(url, timeout=10):
    """Drop-in ``urllib.request.urlopen`` replacement for Danbooru requests.

    Hosts not behind the challenge (e.g. a plain CDN) use ``urllib`` directly.
    For a challenge-prone ``donmai.us`` host, a lightweight plain request is
    tried first; only if Cloudflare actually challenges is the request repeated
    inside the cleared QtWebEngine page (same cookies and TLS fingerprint). This
    means that if Danbooru stops challenging, the embedded browser is never
    created and behaviour matches the original plain-HTTP app."""
    if not _challenged_host(url):
        return _UrllibResponseAdapter(_urllib_urlopen(url, timeout=timeout))

    try:
        host = urlparse(url).hostname or ''
    except Exception:
        host = ''

    # Fast path: until we have actually seen this host challenged, try plain
    # HTTP. If it is not challenged, we return without ever touching the browser.
    if host not in _known_challenged_hosts:
        plain = _try_plain_request(url, timeout)
        if plain is not None:
            return plain
        if host:
            _known_challenged_hosts.add(host)
        _log(f'{host or url} is behind a Cloudflare challenge; routing through '
             f'the embedded browser')

    solver = _solver
    if solver is None:
        _log('challenge solver is not initialized; using a plain urllib '
             'request (challenged endpoints will return 403)')
        return _UrllibResponseAdapter(_urllib_urlopen(url, timeout=timeout))

    holder = solver.fetch(url, timeout, force=False)
    status = holder.get('status', 0)
    body = holder.get('body', b'')

    if _looks_like_challenge(status, body):
        _log('page fetch was challenged; re-solving the Cloudflare challenge')
        holder = solver.fetch(url, timeout, force=True)
        status = holder.get('status', 0)
        body = holder.get('body', b'')
        if _looks_like_challenge(status, body):
            _log('still challenged after re-solving; the challenge could not '
                 'be cleared')
        elif status:
            _log('Danbooru request succeeded through the browser page')

    if status == 0 and not body:
        # The page machinery produced nothing usable; fall back so the caller
        # still gets a (likely 403) response rather than hanging.
        try:
            return _UrllibResponseAdapter(_urllib_urlopen(url, timeout=timeout))
        except Exception:
            return _BytesResponse(0, '', b'')

    return _BytesResponse(status, holder.get('content_type', ''), body)
