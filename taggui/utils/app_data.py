"""
Helpers for the in-app "Remove app data" (uninstall cleanup) feature.

TagGUI stores data outside its own folder, so deleting the application/repository
folder alone does not remove everything. This module locates, measures and
deletes that leftover data:

* the QSettings store (preferences, tag library, hidden models, etc.)
* the on-disk cache folder (thumbnails plus every ``*_cache.json`` file); the
  whole ``<cache>/taggui`` folder is removed, which is future-proof against new
  cache files being added there
* optionally the downloaded captioning models (a custom models directory and/or
  the shared Hugging Face cache)

Caption ``.txt`` files live next to the user's images and are never touched.
"""

import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths

from utils.danbooru_session import DANBOORU_PROFILE_NAME
from utils.scan_cache import _get_cache_dir
from utils.settings import DEFAULT_SETTINGS, get_settings


def get_cache_directory() -> Path:
    """Return the ``<cache>/taggui`` folder that holds thumbnails and all
    scan/completion/duplicate caches."""
    return _get_cache_dir()


def get_browser_profile_directories() -> list[Path]:
    """Return the on-disk folders QtWebEngine uses for the Danbooru wiki profile.

    The Danbooru wiki clears a Cloudflare challenge in an embedded browser and
    stores the resulting cookies (plus a small cache) in a dedicated QtWebEngine
    profile. QtWebEngine places these under the platform's app-data and cache
    locations, so they live outside the normal ``taggui`` cache folder and must
    be removed separately during uninstall cleanup. Resolved via QStandardPaths
    so the correct per-platform paths (Windows/macOS/Linux) are used.
    """
    directories = []
    for location in (QStandardPaths.StandardLocation.AppDataLocation,
                     QStandardPaths.StandardLocation.CacheLocation):
        base = QStandardPaths.writableLocation(location)
        if base:
            directories.append(
                Path(base) / 'QtWebEngine' / DANBOORU_PROFILE_NAME)
    # De-duplicate while preserving order (some platforms may coincide).
    unique = []
    for directory in directories:
        if directory not in unique:
            unique.append(directory)
    return unique


def get_browser_profile_size_bytes() -> int:
    """Return the combined size of the Danbooru wiki browser profile folders."""
    return sum(get_directory_size_bytes(directory)
               for directory in get_browser_profile_directories())


def get_models_directory() -> Path | None:
    """Return the custom models directory set in Settings, or ``None`` if the
    user has not set one (in which case models live in the Hugging Face cache)."""
    settings = get_settings()
    path = settings.value(
        'models_directory_path',
        defaultValue=DEFAULT_SETTINGS['models_directory_path'], type=str)
    path = (path or '').strip()
    if not path:
        return None
    return Path(path)


def get_hugging_face_cache_directory() -> Path:
    """Return the shared Hugging Face cache directory.

    Follows the same lookup order as the ``huggingface_hub`` library:
    ``HF_HUB_CACHE``, then the legacy ``HUGGINGFACE_HUB_CACHE``, then
    ``HF_HOME``, then ``$XDG_CACHE_HOME/huggingface``, then
    ``~/.cache/huggingface``.
    """
    for variable in ('HF_HUB_CACHE', 'HUGGINGFACE_HUB_CACHE'):
        hub_cache = os.environ.get(variable, '').strip()
        if hub_cache:
            hub_path = Path(hub_cache).expanduser()
            # The default layout is ``<HF_HOME>/hub``; only then is the parent
            # the Hugging Face folder. A custom folder with any other name is
            # the cache itself; its parent could be e.g. the home folder.
            if hub_path.name.lower() == 'hub' and hub_path.parent.name:
                return hub_path.parent
            return hub_path
    hf_home = os.environ.get('HF_HOME', '').strip()
    if hf_home:
        return Path(hf_home).expanduser()
    xdg_cache_home = os.environ.get('XDG_CACHE_HOME', '').strip()
    if xdg_cache_home:
        return Path(xdg_cache_home).expanduser() / 'huggingface'
    return Path.home() / '.cache' / 'huggingface'


def _resolve(path: Path) -> Path:
    """Absolute, symlink-free path; lower-cased on Windows so comparisons are
    case-insensitive there."""
    try:
        resolved = os.path.abspath(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):
        resolved = os.path.abspath(Path(path).expanduser())
    return Path(os.path.normcase(resolved))


def _protected_directories() -> list[Path]:
    """Folders that must never be deleted as a whole (or any folder that
    contains one of them)."""
    home = Path.home()
    protected = [home]
    for name in ('Desktop', 'Documents', 'Downloads', 'Pictures', 'Videos',
                 'Music', 'OneDrive', 'AppData', 'AppData/Local',
                 'AppData/Roaming', 'AppData/LocalLow', '.cache', '.config',
                 '.local', '.local/share', 'Library', 'Library/Caches',
                 'Movies'):
        protected.append(home / name)
    for variable in ('USERPROFILE', 'HOME', 'APPDATA', 'LOCALAPPDATA',
                     'PROGRAMDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)',
                     'PROGRAMW6432', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP',
                     'PUBLIC', 'ONEDRIVE', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME',
                     'XDG_DATA_HOME'):
        value = os.environ.get(variable, '').strip()
        if value:
            protected.append(Path(value))
    for location in (QStandardPaths.StandardLocation.HomeLocation,
                     QStandardPaths.StandardLocation.DesktopLocation,
                     QStandardPaths.StandardLocation.DocumentsLocation,
                     QStandardPaths.StandardLocation.DownloadLocation,
                     QStandardPaths.StandardLocation.PicturesLocation,
                     QStandardPaths.StandardLocation.MoviesLocation,
                     QStandardPaths.StandardLocation.MusicLocation,
                     QStandardPaths.StandardLocation.GenericDataLocation,
                     QStandardPaths.StandardLocation.GenericCacheLocation,
                     QStandardPaths.StandardLocation.GenericConfigLocation):
        for value in QStandardPaths.standardLocations(location):
            if value:
                protected.append(Path(value))
    if sys.platform != 'win32':
        for value in ('/bin', '/boot', '/dev', '/etc', '/home', '/lib',
                      '/lib64', '/media', '/mnt', '/opt', '/private', '/proc',
                      '/root', '/sbin', '/srv', '/sys', '/tmp', '/usr',
                      '/var', '/Applications', '/System', '/Users',
                      '/Volumes'):
            protected.append(Path(value))
    # The application's own folder.
    protected.append(Path(__file__).resolve().parent.parent.parent)
    return [_resolve(directory) for directory in protected]


def get_unsafe_removal_reason(path: Path | None) -> str | None:
    """Return why ``path`` must not be deleted as a whole, or ``None`` if it is
    safe. Guards against a mistyped models directory or environment variable
    (or an imported settings file) pointing at a drive, the home folder, a
    system folder or the user's image folder."""
    if path is None or not str(path).strip():
        return 'no folder is set'
    target = _resolve(path)
    if target == Path(target.anchor) or len(target.parts) <= 1:
        return 'it is the root of a drive'
    for protected in _protected_directories():
        if target == protected:
            return 'it is a system or personal folder'
        if protected.is_relative_to(target):
            return 'it contains a system or personal folder'
    image_directory = get_settings().value('directory_path', '', type=str)
    if image_directory and image_directory.strip():
        image_directory = _resolve(Path(image_directory.strip()))
        if (image_directory == target
                or image_directory.is_relative_to(target)):
            return 'it contains your image folder'
    return None


def get_directory_size_bytes(path: Path) -> int:
    """Return the total size in bytes of everything under ``path``.

    Returns 0 if the path does not exist or cannot be read. Symlinked files are
    counted by their link size (not followed) to avoid double counting or
    escaping the tree.
    """
    total = 0
    try:
        if not path.exists():
            return 0
        if path.is_file():
            return path.stat().st_size
        for root, _dirs, files in os.walk(path, followlinks=False):
            for name in files:
                file_path = Path(root) / name
                try:
                    total += file_path.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
    except OSError:
        return total
    return total


def format_size(num_bytes: int) -> str:
    """Return a human-readable size string such as ``'12.3 MB'``."""
    size = float(num_bytes)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            if unit == 'B':
                return f'{int(size)} {unit}'
            return f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} TB'


def remove_directory(path: Path) -> bool:
    """Delete ``path`` and everything under it. Returns True on success (or if it
    was already gone), False if something could not be removed or the folder
    is protected (see ``get_unsafe_removal_reason``)."""
    if get_unsafe_removal_reason(path) is not None:
        return False
    try:
        if not path.exists():
            return True
        shutil.rmtree(path, ignore_errors=False)
        return not path.exists()
    except OSError:
        # Best effort: remove whatever we can, then report failure.
        shutil.rmtree(path, ignore_errors=True)
        return not path.exists()


def remove_caches() -> bool:
    """Delete the thumbnail cache and all scan/completion/duplicate caches by
    removing the whole ``<cache>/taggui`` folder, plus the Danbooru wiki browser
    profile (Cloudflare clearance cookies and its small cache)."""
    ok = remove_directory(get_cache_directory())
    for directory in get_browser_profile_directories():
        if not remove_directory(directory):
            ok = False
    return ok


def remove_settings() -> bool:
    """Delete the saved settings (QSettings store).

    Clears every stored key and flushes. On Windows this empties the
    ``HKEY_CURRENT_USER\\Software\\taggui\\taggui`` registry key; on macOS/Linux
    it removes the plist/conf file's contents.
    """
    try:
        settings = get_settings()
        settings.clear()
        settings.sync()
        return True
    except Exception:
        return False
