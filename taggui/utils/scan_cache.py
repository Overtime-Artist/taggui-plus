"""
Persistent disk cache for image dimension and caption data, keyed by
(file_path, mtime_ns).  On a warm cache the scanner avoids re-reading
every file and only does a stat() per image to validate freshness.

Known limitation: freshness is validated purely by modification time. If an
external tool edits a file's contents without changing its mtime (e.g. some
sync/restore tools that preserve timestamps), the cached dimensions/caption
for that file will be served as-is and won't refresh until the mtime changes.
Detecting this would require hashing every file's contents on each scan, which
would defeat the performance purpose of the cache.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path


def _get_cache_dir() -> Path:
    if sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', '') or
                    Path.home() / 'AppData' / 'Local')
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Caches'
    else:
        base = Path(os.environ.get('XDG_CACHE_HOME', '') or
                    Path.home() / '.cache')
    d = base / 'taggui'
    d.mkdir(parents=True, exist_ok=True)
    return d


# Serializes cache writes from the main thread and background save threads, so
# two saves of the same file can never interleave.
_save_lock = threading.Lock()
# Files that were found damaged when loaded. Their damaged contents must not be
# copied over the good backup on the next save.
_damaged_paths: set[str] = set()


def _get_backup_path(path: Path) -> Path:
    return path.with_name(path.name + '.bak')


def _read_json_dict(path: Path) -> dict:
    """Read a JSON object from ``path``. Raises on a missing, unreadable or
    damaged file."""
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f'{path} does not contain a JSON object')
    return data


def _load_json(path: Path, use_backup: bool = False) -> dict:
    """Load a cache file, returning ``{}`` if it is missing or damaged.

    With ``use_backup``, a damaged file falls back to the backup written by
    ``_save_json(..., keep_backup=True)``. A missing file does not fall back,
    so data the user deleted is not brought back.
    """
    try:
        return _read_json_dict(path)
    except FileNotFoundError:
        return {}
    except Exception:
        pass
    _damaged_paths.add(str(path))
    if use_backup:
        try:
            return _read_json_dict(_get_backup_path(path))
        except Exception:
            pass
    return {}


def _replace_file_contents(path: Path, text: str):
    """Write ``text`` to ``path`` so that a crash or interrupted write can
    never leave a partial file: the text goes to a temporary file that is then
    swapped into place in one step. Raises ``OSError`` on failure."""
    temp_path = path.with_name(
        f'{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        temp_path.write_text(text, encoding='utf-8')
        # On Windows the swap fails while another thread has the target open
        # for reading, so retry briefly.
        for attempt in range(10):
            try:
                os.replace(temp_path, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.05)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _save_json(path: Path, data: dict, keep_backup: bool = False):
    """Save a cache file safely (see ``_replace_file_contents``).

    With ``keep_backup``, the previous version is first copied to
    ``<name>.bak`` so it can be used if the main file is ever damaged.
    """
    try:
        text = json.dumps(data)
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    with _save_lock:
        if (keep_backup and str(path) not in _damaged_paths
                and path.exists()):
            try:
                _replace_file_contents(_get_backup_path(path),
                                       path.read_text(encoding='utf-8'))
            except Exception:
                pass
        try:
            _replace_file_contents(path, text)
            _damaged_paths.discard(str(path))
        except Exception:
            pass


class ScanCache:
    """
    Holds dimension and caption caches loaded once per scan.
    Call load() before the scan, then save_async() after to persist updates.
    """

    def __init__(self):
        cache_dir = _get_cache_dir()
        self._dim_path = cache_dir / 'dimension_cache.json'
        self._cap_path = cache_dir / 'caption_cache.json'
        self.dimensions: dict = {}
        self.captions: dict = {}

    def load(self):
        self.dimensions = _load_json(self._dim_path)
        self.captions = _load_json(self._cap_path)

    # ------------------------------------------------------------------
    # Dimension helpers
    # ------------------------------------------------------------------

    def get_dimensions(self, image_path: Path,
                       mtime_ns: int | None) -> tuple | None:
        """
        Returns cached (width, height) or None on miss/stale.
        A return value of None can also mean the image had unreadable
        dimensions — distinguish from miss via the second element of
        (value, hit) using get_dimensions_hit() if needed.
        """
        if mtime_ns is None:
            return None
        cached = self.dimensions.get(str(image_path))
        if cached is not None and cached.get('mtime_ns') == mtime_ns:
            dims = cached.get('dims')
            return tuple(dims) if dims else None
        return None

    def is_dimensions_cached(self, image_path: Path,
                              mtime_ns: int | None) -> bool:
        if mtime_ns is None:
            return False
        cached = self.dimensions.get(str(image_path))
        if cached is None or cached.get('mtime_ns') != mtime_ns:
            return False
        # Older versions cached (-1, -1) for images whose size could not be
        # read quickly (e.g. BMP). Treat those as not cached so they are read
        # again.
        dims = cached.get('dims')
        return not dims or min(dims) > 0

    def cache_dimensions(self, updates: dict, image_path: Path,
                         mtime_ns: int | None, dimensions) -> None:
        if mtime_ns is None:
            return
        updates[str(image_path)] = {
            'mtime_ns': mtime_ns,
            'dims': list(dimensions) if dimensions else None,
        }

    # ------------------------------------------------------------------
    # Caption helpers
    # ------------------------------------------------------------------

    def get_caption(self, txt_path_str: str,
                    mtime_ns: int) -> tuple | None:
        """Returns (tags_list, nl_prompt_str) or None on miss/stale."""
        cached = self.captions.get(txt_path_str)
        if cached is not None and cached.get('mtime_ns') == mtime_ns:
            return cached.get('tags', []), cached.get('nl', '')
        return None

    def cache_caption(self, updates: dict, txt_path_str: str,
                      mtime_ns: int, tags: list, nl: str) -> None:
        updates[txt_path_str] = {
            'mtime_ns': mtime_ns,
            'tags': tags,
            'nl': nl,
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_async(self, dim_updates: dict, cap_updates: dict,
                   directory_path: Path | None = None,
                   live_dim_keys: set | None = None,
                   live_cap_keys: set | None = None) -> None:
        """Merge updates into the loaded cache and write to disk in background.

        When ``directory_path`` and the ``live_*`` key sets are supplied, any
        cached entry that lives under that directory tree but was not seen in
        the current scan (i.e. the file was deleted or renamed) is pruned, so
        the cache stays bounded to files that actually exist. Entries for other
        directories are left untouched.
        """
        has_updates = bool(dim_updates or cap_updates)
        if not has_updates and directory_path is None:
            return
        merged_dims = {**self.dimensions, **dim_updates}
        merged_caps = {**self.captions, **cap_updates}
        t = threading.Thread(
            target=self._save,
            args=(merged_dims, merged_caps, has_updates, directory_path,
                  live_dim_keys, live_cap_keys),
            daemon=True,
        )
        t.start()

    @staticmethod
    def _prune_directory_entries(cache: dict, directory_path: Path,
                                 live_keys: set) -> int:
        """Drop entries under ``directory_path`` whose key is not in
        ``live_keys`` (file no longer present). Returns the number removed."""
        stale_keys = []
        for key in cache:
            if key in live_keys:
                continue
            try:
                is_under_tree = Path(key).is_relative_to(directory_path)
            except (ValueError, OSError):
                is_under_tree = False
            if is_under_tree:
                stale_keys.append(key)
        for key in stale_keys:
            del cache[key]
        return len(stale_keys)

    def _save(self, dimensions: dict, captions: dict, has_updates: bool,
              directory_path: Path | None = None,
              live_dim_keys: set | None = None,
              live_cap_keys: set | None = None) -> None:
        removed = 0
        if directory_path is not None:
            removed += self._prune_directory_entries(
                dimensions, directory_path, live_dim_keys or set())
            removed += self._prune_directory_entries(
                captions, directory_path, live_cap_keys or set())
        # Avoid needless disk writes on warm scans where nothing changed.
        if not has_updates and removed == 0:
            return
        _save_json(self._dim_path, dimensions)
        _save_json(self._cap_path, captions)
