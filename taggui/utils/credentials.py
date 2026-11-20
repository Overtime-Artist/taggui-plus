"""Website logins (user name + API key) kept in the operating system's
password store.

Credentials are stored with ``keyring`` (Windows Credential Manager, macOS
Keychain or the Secret Service on Linux), never in QSettings, so they are not
included in settings exports or backups. Each site has one entry, keyed by a
short site id such as ``'danbooru'``, so other sites (e.g. Gelbooru) can be
added later without changes here.

If ``keyring`` is missing or only an insecure/non-functional backend is
available, the store is reported as unavailable and nothing is saved.
"""

import json
import threading

SERVICE_NAME = 'TagGUI Plus'

# Backends that either do nothing or keep secrets in a plain file.
_REJECTED_BACKEND_MODULE_PREFIXES = ('keyring.backends.fail',
                                     'keyring.backends.null',
                                     'keyrings.alt')

_lock = threading.Lock()
_cache: dict = {}


def _keyring():
    try:
        import keyring
    except Exception:
        return None
    return keyring


def _backend():
    keyring = _keyring()
    if keyring is None:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:
        return None
    # A ChainerBackend wraps the real backends; check what it actually uses.
    backends = getattr(backend, 'backends', None)
    candidates = list(backends) if backends else [backend]
    if not candidates:
        return None
    for candidate in candidates:
        module = type(candidate).__module__ or ''
        if module.startswith(_REJECTED_BACKEND_MODULE_PREFIXES):
            return None
    return backend


def is_store_available() -> bool:
    return _backend() is not None


def store_name() -> str:
    """A user-facing name for where credentials are saved."""
    backend = _backend()
    if backend is None:
        return ''
    backends = getattr(backend, 'backends', None)
    if backends:
        backend = list(backends)[0]
    module = type(backend).__module__ or ''
    if module.endswith('.Windows'):
        return 'Windows Credential Manager'
    if module.endswith('.macOS'):
        return 'macOS Keychain'
    if module.endswith(('.SecretService', '.libsecret')):
        return 'the system keyring (Secret Service)'
    if module.endswith('.kwallet'):
        return 'KWallet'
    return 'the system password store'


def get_site_credentials(site: str):
    """Return ``(login, api_key)`` for ``site``, or ``None`` if none saved."""
    with _lock:
        if site in _cache:
            return _cache[site]
    credentials = None
    backend = _backend()
    if backend is not None:
        try:
            stored = backend.get_password(SERVICE_NAME, site)
        except Exception:
            stored = None
        if stored:
            try:
                data = json.loads(stored)
                login = str(data.get('login') or '').strip()
                api_key = str(data.get('api_key') or '').strip()
                if login and api_key:
                    credentials = (login, api_key)
            except Exception:
                credentials = None
    with _lock:
        _cache[site] = credentials
    return credentials


def has_site_credentials(site: str) -> bool:
    return get_site_credentials(site) is not None


def set_site_credentials(site: str, login: str, api_key: str):
    """Save the credentials. Raises ``RuntimeError`` if the store is
    unavailable or the write fails."""
    login = (login or '').strip()
    api_key = (api_key or '').strip()
    if not login or not api_key:
        raise ValueError('Both a user name and an API key are required.')
    backend = _backend()
    if backend is None:
        raise RuntimeError('No secure password store is available.')
    value = json.dumps({'login': login, 'api_key': api_key})
    try:
        backend.set_password(SERVICE_NAME, site, value)
    except Exception as exception:
        raise RuntimeError(str(exception)) from exception
    with _lock:
        _cache[site] = (login, api_key)


def delete_site_credentials(site: str) -> bool:
    """Delete the saved credentials. Returns True if nothing is left stored
    (including when nothing was saved), False if deletion failed."""
    with _lock:
        _cache.pop(site, None)
    backend = _backend()
    if backend is None:
        return True
    try:
        if backend.get_password(SERVICE_NAME, site) is None:
            return True
        backend.delete_password(SERVICE_NAME, site)
    except Exception:
        try:
            return backend.get_password(SERVICE_NAME, site) is None
        except Exception:
            return False
    return True


def clear_cache():
    with _lock:
        _cache.clear()
