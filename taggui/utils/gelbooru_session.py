"""Network access for the Gelbooru wiki.

Gelbooru's data API (``index.php?page=dapi``) only answers requests that carry
an account's ``api_key`` and ``user_id``. When the user has saved them in
Settings > Gelbooru Account they are added to API requests here, and only to
``https://gelbooru.com/index.php?page=dapi`` URLs: image hosts and web pages
never see them. Without a key (or if Gelbooru rejects it) the wiki reads the
public web pages instead.
"""
import hashlib
import threading
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import (HTTPRedirectHandler, Request, build_opener,
                            urlopen as _urllib_urlopen)

from utils import credentials

GELBOORU_BASE_URL = 'https://gelbooru.com'
GELBOORU_CREDENTIALS_SITE = 'gelbooru'
GELBOORU_ACCOUNT_OPTIONS_URL = (
    f'{GELBOORU_BASE_URL}/index.php?page=account&s=options')
USER_AGENT = 'TagGUI/1.0 (Wiki Viewer)'
_API_HOST = 'gelbooru.com'


class GelbooruApiKeyRejected(Exception):
    """Gelbooru refused the saved API key; the caller should fall back to the
    web pages."""


class _NoRedirect(HTTPRedirectHandler):
    # Requests carrying the key must never follow a redirect elsewhere.
    def redirect_request(self, *args, **kwargs):
        return None


_no_redirect_opener = build_opener(_NoRedirect)

_auth_lock = threading.Lock()
# SHA-256 digests (never the key itself) of keys Gelbooru rejected this
# session. A rejected key is not sent again until it is changed.
_rejected_key_digests = set()
_api_key_problem = ''


def _key_digest(user_id: str, api_key: str) -> str:
    return hashlib.sha256(f'{user_id}:{api_key}'.encode('utf-8')).hexdigest()


def parse_api_key_text(text: str) -> tuple[str, str]:
    """Split what the user pasted into ``(user_id, api_key)``.

    Gelbooru's account options page shows the key as
    ``&api_key=...&user_id=...``; pasting that whole string fills in both.
    Anything else is treated as the bare key (``user_id`` is then '')."""
    text = str(text or '').strip()
    if 'api_key=' in text:
        query = parse_qs(text.lstrip('?&'))
        api_key = (query.get('api_key') or [''])[0].strip()
        user_id = (query.get('user_id') or [''])[0].strip()
        return user_id, api_key
    return '', text


def _is_image_host(host: str) -> bool:
    return host.endswith('.gelbooru.com') and host != 'www.gelbooru.com'


def is_api_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()
        port = parsed.port
    except Exception:
        return False
    if (parsed.scheme != 'https' or host != _API_HOST
            or port not in (None, 443) or parsed.username is not None
            or parsed.path != '/index.php'):
        return False
    return parse_qs(parsed.query).get('page') == ['dapi']


def saved_api_credentials():
    """``(user_id, api_key)`` when a usable key is saved, else None."""
    saved = credentials.get_site_credentials(GELBOORU_CREDENTIALS_SITE)
    if not saved:
        return None
    user_id, api_key = (str(saved[0] or '').strip(),
                        str(saved[1] or '').strip())
    if not user_id or not api_key:
        return None
    with _auth_lock:
        if _key_digest(user_id, api_key) in _rejected_key_digests:
            return None
    return user_id, api_key


def has_usable_api_key() -> bool:
    return saved_api_credentials() is not None


def _mark_key_rejected(user_id: str, api_key: str):
    global _api_key_problem
    with _auth_lock:
        _rejected_key_digests.add(_key_digest(user_id, api_key))
        _api_key_problem = (
            'Gelbooru rejected your API key, so TagGUI Plus is reading the '
            'Gelbooru web pages instead. Check the user ID and API key in '
            'Settings > Gelbooru Account.')


def api_key_problem() -> str:
    """A user-facing explanation if the saved key was rejected this session,
    else an empty string."""
    with _auth_lock:
        return _api_key_problem


def reset_api_key_state():
    """Forget earlier rejections, e.g. after the user changes the key."""
    global _api_key_problem
    credentials.clear_cache()
    with _auth_lock:
        _rejected_key_digests.clear()
        _api_key_problem = ''


def gelbooru_urlopen(url: str, timeout=10, use_api_key: bool = True):
    """``urllib.request.urlopen`` for Gelbooru.

    Image hosts (img*.gelbooru.com) answer with an HTML page unless the
    request comes "from" gelbooru.com, so those get a Referer header. API
    requests get the saved key; if Gelbooru rejects it,
    ``GelbooruApiKeyRejected`` is raised and the key isn't used again this
    session. Error messages never include the key."""
    headers = {'User-Agent': USER_AGENT}
    host = (urlparse(url).hostname or '').lower()
    if _is_image_host(host):
        headers['Referer'] = f'{GELBOORU_BASE_URL}/'
    key = saved_api_credentials() if use_api_key and is_api_url(url) else None
    if key is None:
        return _urllib_urlopen(Request(url, headers=headers), timeout=timeout)
    user_id, api_key = key
    keyed_url = f'{url}&{urlencode({"api_key": api_key, "user_id": user_id})}'
    try:
        return _no_redirect_opener.open(Request(keyed_url, headers=headers),
                                        timeout=timeout)
    except HTTPError as error:
        code = error.code
        error.close()
        if code in (401, 403):
            _mark_key_rejected(user_id, api_key)
            raise GelbooruApiKeyRejected() from None
        raise OSError(f'Gelbooru returned status {code}.') from None
    except Exception as exception:
        # urllib errors can quote the URL; describe the failure without it.
        reason = getattr(exception, 'reason', None)
        raise OSError(f"Couldn't reach Gelbooru ({reason or type(exception).__name__}).") from None


def verify_api_key(timeout=15):
    """Check the saved API key with Gelbooru. Call from a worker thread.

    Returns ``(ok, message)``. ``ok`` is True when Gelbooru accepted the key,
    False when it rejected it, and None when it couldn't be checked."""
    reset_api_key_state()
    if not has_usable_api_key():
        return None, 'No API key is saved.'
    url = (f'{GELBOORU_BASE_URL}/index.php?'
           + urlencode({'page': 'dapi', 's': 'post', 'q': 'index',
                        'json': 1, 'limit': 1}))
    try:
        with gelbooru_urlopen(url, timeout=timeout) as response:
            status = getattr(response, 'status', 200)
            body = response.read()
    except GelbooruApiKeyRejected:
        return False, ('Gelbooru rejected the API key. Check the user ID and '
                       'API key.')
    except Exception as exception:
        return None, (f'{exception} The key is saved and will be used when '
                      f'Gelbooru is reachable.')
    if status == 200 and body.lstrip().startswith(b'{'):
        return True, 'Gelbooru accepted the API key.'
    return None, (f'Gelbooru gave an unexpected answer (status {status}). The '
                  f'key is saved and will be used when Gelbooru is reachable.')
