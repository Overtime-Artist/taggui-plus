import hashlib
import os
import sys
import threading
from pathlib import Path


def get_thumbnail_cache_dir() -> Path:
    """Return (and create if needed) the platform-appropriate thumbnail cache directory."""
    if sys.platform == 'win32':
        local_app_data = os.environ.get('LOCALAPPDATA', '')
        base = Path(local_app_data) if local_app_data else Path.home() / 'AppData' / 'Local'
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Caches'
    else:
        xdg_cache = os.environ.get('XDG_CACHE_HOME', '')
        base = Path(xdg_cache) if xdg_cache else Path.home() / '.cache'
    cache_dir = base / 'taggui' / 'thumbnails'
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_cache_path(image_path: Path, file_modified_time_ns: int | None,
                   image_width: int) -> Path | None:
    """
    Return the cache file path for a given image at the given display width.
    Returns None if the mod time is unknown (can't form a stable key).
    The cache key encodes path + mod time + width, so stale entries are never
    loaded — they just accumulate until eviction.
    """
    if file_modified_time_ns is None:
        return None
    key = hashlib.sha256(
        f'{image_path}:{file_modified_time_ns}:{image_width}'.encode()
    ).hexdigest()
    return get_thumbnail_cache_dir() / f'{key}.png'


def save_thumbnail(thumbnail_image, cache_path: Path):
    """Save a thumbnail (a ``QImage``) to the cache without ever leaving a
    partly written file: it is written to a temporary file first and then
    swapped into place. Several threads may save the same thumbnail at once;
    whichever finishes last wins, and both results are identical."""
    temp_path = cache_path.with_name(
        f'{cache_path.stem}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        if thumbnail_image.save(str(temp_path), 'PNG'):
            os.replace(temp_path, cache_path)
    except OSError:
        pass
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def mark_thumbnail_used(cache_path: Path):
    """Record that a cached thumbnail was just used, so the cache size limit
    removes the least recently used thumbnails first. (Windows does not
    reliably record when a file was last read, so the modified time is used.)"""
    try:
        os.utime(cache_path)
    except OSError:
        pass


def _is_cache_file(f: Path) -> bool:
    # `.tmp` files are leftovers from a save that was interrupted.
    return f.is_file() and f.suffix in ('.png', '.tmp')


def get_cache_size_bytes() -> int:
    """Return total size of all cached thumbnail files in bytes."""
    cache_dir = get_thumbnail_cache_dir()
    try:
        return sum(
            f.stat().st_size for f in cache_dir.iterdir()
            if _is_cache_file(f)
        )
    except OSError:
        return 0


def clear_cache():
    """Delete all cached thumbnail files."""
    cache_dir = get_thumbnail_cache_dir()
    try:
        for f in cache_dir.iterdir():
            if _is_cache_file(f):
                try:
                    f.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def evict_to_limit(max_bytes: int):
    """
    Delete the least recently used cached files until the total cache size is
    at or below max_bytes. "Used" is the file's modified time, which is updated
    by `mark_thumbnail_used` whenever a cached thumbnail is shown. This is
    called in a background thread on startup so stale / over-limit entries are
    cleaned up across sessions.
    """
    cache_dir = get_thumbnail_cache_dir()
    try:
        files = []
        for f in cache_dir.iterdir():
            if _is_cache_file(f):
                try:
                    stat = f.stat()
                    files.append((f, stat.st_size, stat.st_mtime))
                except OSError:
                    pass
    except OSError:
        return

    total = sum(size for _, size, _ in files)
    if total <= max_bytes:
        return

    # Delete least recently used entries first
    files.sort(key=lambda x: x[2])
    for f, size, _ in files:
        try:
            f.unlink()
        except OSError:
            pass
        total -= size
        if total <= max_bytes:
            break
