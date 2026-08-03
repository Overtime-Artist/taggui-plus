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

from utils.scan_cache import _get_cache_dir
from utils.settings import DEFAULT_SETTINGS, get_settings


def get_cache_directory() -> Path:
    """Return the ``<cache>/taggui`` folder that holds thumbnails and all
    scan/completion/duplicate caches."""
    return _get_cache_dir()


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

    Honours ``HF_HOME`` (and the legacy ``HUGGINGFACE_HUB_CACHE``) if set,
    otherwise falls back to the platform default ``~/.cache/huggingface``.
    """
    hf_hub_cache = os.environ.get('HUGGINGFACE_HUB_CACHE', '').strip()
    if hf_hub_cache:
        # This variable points at the ``hub`` subfolder; return its parent so we
        # remove the whole huggingface cache, matching the README instructions.
        parent = Path(hf_hub_cache).parent
        if parent.name:
            return parent
        return Path(hf_hub_cache)
    hf_home = os.environ.get('HF_HOME', '').strip()
    if hf_home:
        return Path(hf_home)
    return Path.home() / '.cache' / 'huggingface'


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
    was already gone), False if something could not be removed."""
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
    removing the whole ``<cache>/taggui`` folder."""
    return remove_directory(get_cache_directory())


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
