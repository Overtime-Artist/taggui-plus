"""
Persistent store for the per-image "complete" workflow flag.

The set of images the user has marked as complete is saved to
``completion_cache.json`` in the same cache directory used by the scan cache
(for example ``%LOCALAPPDATA%\\taggui`` on Windows).

Design
------
"Complete" is the user's own decision and should persist across a wide range of
external file operations:

* **Edited in place** (same path/name): kept automatically, because the flag is
  keyed by path and is never invalidated by modification time.
* **Moved or renamed externally**: the path key changes, so the flag would be
  orphaned. To reconnect it we also remember, for each complete image, a
  content **image hash** (identifies "the same image") and a **caption hash**
  (confirms "the same finished work"). When a complete image's old path is gone
  and a newly-seen image matches both hashes, the flag is re-homed to the new
  path. Byte-identical duplicates all become complete, which is the correct and
  unambiguous outcome.

Performance
-----------
Hashing never happens on the normal (warm) scan path:

* The image hash is computed once when the user marks an image complete, and
  only re-computed on a scan when that image's modification time changed
  (i.e. it was actually edited) or has not been hashed yet (lazy migration of
  legacy entries).
* The caption hash is tiny (a caption is a few KB of text) and is refreshed
  immediately on an in-program caption save and, as a backstop, on a scan when
  the caption file's modification time changed.
* Orphan re-homing only hashes candidate files whose size matches a missing
  complete image, so it costs nothing unless a move/rename actually happened.

The flag is deliberately kept out of the caption ``.txt`` files so that it
never becomes part of the exported training data.
"""

import hashlib
import threading
from pathlib import Path

from utils.scan_cache import _get_cache_dir, _load_json, _save_json

_HASH_CHUNK_SIZE = 1 << 20  # 1 MiB


def _hash_file(path) -> str | None:
    """Return the SHA-256 hex digest of a file's bytes, or None on error."""
    try:
        hasher = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(_HASH_CHUNK_SIZE), b''):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError:
        return None


def _hash_caption(caption_path) -> str | None:
    """Return the SHA-256 hex digest of a caption file's bytes.

    Returns None when the caption file does not exist (or can't be read), which
    represents "no caption". Two images that both have no caption therefore
    compare equal.
    """
    path = Path(caption_path)
    if not path.exists():
        return None
    return _hash_file(path)


def _empty_entry() -> dict:
    return {
        'image_hash': None,
        'image_size': None,
        'image_mtime_ns': None,
        'caption_hash': None,
        'caption_mtime_ns': None,
    }


class CompletionStore:
    """Loads, queries, and persists the set of complete images.

    Each complete image is stored under its full file path with the hash
    metadata needed to recognise it again after an external move or rename.
    """

    def __init__(self):
        self._path = _get_cache_dir() / 'completion_cache.json'
        # Maps ``str(image_path)`` -> entry dict (see ``_empty_entry``).
        self._entries: dict[str, dict] = {}
        self._loaded = False
        self._dirty = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Loading / persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load the complete-image entries from disk (once).

        Accepts both the current dict-per-entry format and the legacy
        ``{path: true}`` format (which loads with empty hash metadata that is
        filled in lazily on the next scan).
        """
        data = _load_json(self._path)
        entries: dict[str, dict] = {}
        for key, value in data.items():
            if isinstance(value, dict):
                entry = _empty_entry()
                for field in entry:
                    if value.get(field) is not None:
                        entry[field] = value[field]
                entries[str(key)] = entry
            elif value:
                # Legacy boolean entry: complete, but not yet hashed.
                entries[str(key)] = _empty_entry()
        with self._lock:
            self._entries = entries
            self._loaded = True
            self._dirty = False

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def save(self) -> None:
        """Write the current entries to disk."""
        with self._lock:
            data = {path: dict(entry) for path, entry in self._entries.items()}
            self._dirty = False
        _save_json(self._path, data)

    def save_if_dirty(self) -> None:
        """Persist only if in-memory state changed since the last save/load."""
        with self._lock:
            dirty = self._dirty
        if dirty:
            self.save()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def is_complete(self, image_path) -> bool:
        self.ensure_loaded()
        with self._lock:
            return str(image_path) in self._entries

    # ------------------------------------------------------------------
    # Mutations driven by user actions
    # ------------------------------------------------------------------

    def _build_entry(self, image_path: Path) -> dict:
        """Compute a full entry (image + caption hash) for a complete image."""
        entry = _empty_entry()
        entry['image_hash'] = _hash_file(image_path)
        try:
            stat = image_path.stat()
            entry['image_size'] = stat.st_size
            entry['image_mtime_ns'] = stat.st_mtime_ns
        except OSError:
            pass
        caption_path = image_path.with_suffix('.txt')
        entry['caption_hash'] = _hash_caption(caption_path)
        try:
            entry['caption_mtime_ns'] = caption_path.stat().st_mtime_ns
        except OSError:
            pass
        return entry

    def set_complete(self, image_path, is_complete: bool) -> bool:
        """
        Update the flag for a single image in memory. Returns True if the
        stored value actually changed. Call save() afterwards to persist.

        When marking complete, the image and caption hashes are computed once
        so the image can be recognised again after an external move/rename.
        """
        self.ensure_loaded()
        key = str(image_path)
        with self._lock:
            currently_complete = key in self._entries
            if is_complete == currently_complete:
                return False
        if is_complete:
            entry = self._build_entry(Path(image_path))
            with self._lock:
                self._entries[key] = entry
                self._dirty = True
        else:
            with self._lock:
                self._entries.pop(key, None)
                self._dirty = True
        return True

    def refresh_caption(self, image_path) -> bool:
        """Re-hash the caption of an already-complete image (in memory).

        Called after an in-program caption save so the stored caption hash
        stays current. This is a no-op for images that aren't complete. The
        change is flushed to disk at the next save()/save_if_dirty().
        """
        self.ensure_loaded()
        key = str(image_path)
        caption_path = Path(image_path).with_suffix('.txt')
        new_hash = _hash_caption(caption_path)
        try:
            new_mtime = caption_path.stat().st_mtime_ns
        except OSError:
            new_mtime = None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False
            if (entry['caption_hash'] == new_hash
                    and entry['caption_mtime_ns'] == new_mtime):
                return False
            entry['caption_hash'] = new_hash
            entry['caption_mtime_ns'] = new_mtime
            self._dirty = True
            return True

    def copy_completion(self, source_path, target_path) -> bool:
        """
        Mirror the "complete" flag (and its hashes) from ``source_path`` onto
        ``target_path`` for an in-program copy. Only sets the target when the
        source is complete; never clears an existing target flag. Returns True
        if the stored set changed. Call save() afterwards to persist.
        """
        self.ensure_loaded()
        source_key = str(source_path)
        target_key = str(target_path)
        with self._lock:
            if source_key not in self._entries:
                return False
            if target_key in self._entries:
                return False
            self._entries[target_key] = dict(self._entries[source_key])
            self._dirty = True
            return True

    def move_completion(self, source_path, target_path) -> bool:
        """
        Move the "complete" flag (and its hashes) from ``source_path`` to
        ``target_path`` for an in-program move, removing the stale source
        entry. Returns True if the stored set changed. Call save() afterwards
        to persist.
        """
        self.ensure_loaded()
        source_key = str(source_path)
        target_key = str(target_path)
        with self._lock:
            entry = self._entries.get(source_key)
            if entry is None:
                return False
            self._entries.pop(source_key, None)
            self._entries[target_key] = dict(entry)
            self._dirty = True
            return True

    # ------------------------------------------------------------------
    # Bulk export / import (for the settings export/import feature)
    # ------------------------------------------------------------------

    def export_entries(self) -> dict[str, dict]:
        """Return a deep copy of every complete-image entry.

        The result maps ``str(image_path)`` -> entry dict (image/caption hashes
        and sizes/mtimes). Reflects the current in-memory state, including any
        changes not yet flushed to disk.
        """
        self.ensure_loaded()
        with self._lock:
            return {path: dict(entry) for path, entry in self._entries.items()}

    def import_entries(self, entries: dict[str, dict]) -> bool:
        """Merge externally provided complete-image entries into the store.

        Union semantics: paths that are already complete are left untouched (an
        import never un-marks or overwrites an existing entry); only new paths
        are added. Each added entry keeps its image/caption hashes so a later
        scan can re-home it onto the matching image if its exported path does
        not exist on this machine.

        Returns True if anything was added. Persists to disk when it did.
        """
        if not isinstance(entries, dict) or not entries:
            return False
        self.ensure_loaded()
        added = False
        with self._lock:
            for key, value in entries.items():
                key = str(key)
                if key in self._entries or not isinstance(value, dict):
                    continue
                entry = _empty_entry()
                for field in entry:
                    if value.get(field) is not None:
                        entry[field] = value[field]
                self._entries[key] = entry
                added = True
            if added:
                self._dirty = True
        if added:
            self.save()
        return added

    # ------------------------------------------------------------------
    # Scan-time reconciliation
    # ------------------------------------------------------------------

    def _refresh_entry(self, entry: dict, info: dict) -> bool:
        """Refresh a present complete image's hashes when its files changed.

        ``info`` is one item from the ``scanned`` list passed to reconcile.
        Returns True if the entry was modified.
        """
        changed = False
        # Image hash: recompute only when missing (lazy migration) or when the
        # modification time changed (the image was edited in place).
        mtime_ns = info.get('mtime_ns')
        if entry['image_hash'] is None or entry['image_mtime_ns'] != mtime_ns:
            new_hash = _hash_file(info['path'])
            if new_hash is not None:
                if (entry['image_hash'] != new_hash
                        or entry['image_size'] != info.get('size')
                        or entry['image_mtime_ns'] != mtime_ns):
                    entry['image_hash'] = new_hash
                    entry['image_size'] = info.get('size')
                    entry['image_mtime_ns'] = mtime_ns
                    changed = True
        # Caption hash: recompute only when missing or when the caption file's
        # modification time changed.
        cap_mtime_ns = info.get('cap_mtime_ns')
        if (entry['caption_hash'] is None
                or entry['caption_mtime_ns'] != cap_mtime_ns):
            new_cap = _hash_caption(info['cap_path'])
            if (entry['caption_hash'] != new_cap
                    or entry['caption_mtime_ns'] != cap_mtime_ns):
                entry['caption_hash'] = new_cap
                entry['caption_mtime_ns'] = cap_mtime_ns
                changed = True
        return changed

    def reconcile(self, scanned: list[dict]) -> bool:
        """Reconcile the store against a freshly scanned directory.

        ``scanned`` is a list of dicts, one per image found in the scan, each
        with keys: ``path`` (Path), ``size`` (int|None), ``mtime_ns``
        (int|None), ``cap_path`` (str, the ``.txt`` path) and ``cap_mtime_ns``
        (int|None).

        Two things happen:

        1. For images that are already complete at their current path, refresh
           their stored hashes if the files changed (see ``_refresh_entry``).
        2. Re-home orphaned complete entries — ones whose stored path no longer
           exists on disk — onto any newly-seen image whose image hash *and*
           caption hash match. Size is used as a cheap prefilter so only
           plausible candidates are hashed.

        Returns True if anything changed (so the caller can persist).
        """
        self.ensure_loaded()
        changed = False
        scanned_keys = {str(info['path']) for info in scanned}
        with self._lock:
            # 1. Refresh hashes for images that are complete at their path.
            for info in scanned:
                entry = self._entries.get(str(info['path']))
                if entry is not None and self._refresh_entry(entry, info):
                    changed = True

            # 2. Collect orphans: complete entries whose path was not scanned
            #    and no longer exists on disk. Entries without an image hash
            #    can't be matched, and entries whose file still exists (e.g. in
            #    another, not-scanned directory) are left untouched.
            orphans = []
            for key, entry in self._entries.items():
                if key in scanned_keys or entry['image_hash'] is None:
                    continue
                try:
                    still_exists = Path(key).exists()
                except OSError:
                    still_exists = False
                if not still_exists:
                    orphans.append(entry)

            if not orphans:
                if changed:
                    self._dirty = True
                return changed

            orphan_sizes = {e['image_size'] for e in orphans
                            if e['image_size'] is not None}
            orphan_hashes = {e['image_hash'] for e in orphans}
            # Set of (image_hash, caption_hash) identities to match against.
            orphan_identities = {(e['image_hash'], e['caption_hash'])
                                 for e in orphans}

            matched_identities = set()
            new_entries: dict[str, dict] = {}
            for info in scanned:
                key = str(info['path'])
                if key in self._entries:
                    continue  # Already complete at this path.
                size = info.get('size')
                if (orphan_sizes and size is not None
                        and size not in orphan_sizes):
                    continue  # Size prefilter miss — skip hashing.
                image_hash = _hash_file(info['path'])
                if image_hash is None or image_hash not in orphan_hashes:
                    continue
                caption_hash = _hash_caption(info['cap_path'])
                identity = (image_hash, caption_hash)
                if identity not in orphan_identities:
                    # Same image but a different caption: treat as different
                    # (unfinished) work and leave it not-complete.
                    continue
                entry = _empty_entry()
                entry['image_hash'] = image_hash
                entry['image_size'] = size
                entry['image_mtime_ns'] = info.get('mtime_ns')
                entry['caption_hash'] = caption_hash
                entry['caption_mtime_ns'] = info.get('cap_mtime_ns')
                new_entries[key] = entry
                matched_identities.add(identity)

            if new_entries:
                self._entries.update(new_entries)
                changed = True
                # Remove exactly the orphan identities that found a new home;
                # leave unmatched orphans in place so they can re-home later
                # when their new directory is scanned.
                stale_keys = [
                    key for key, entry in self._entries.items()
                    if key not in scanned_keys
                    and key not in new_entries
                    and not Path(key).exists()
                    and (entry['image_hash'], entry['caption_hash'])
                    in matched_identities
                ]
                for key in stale_keys:
                    self._entries.pop(key, None)

            if changed:
                self._dirty = True
        return changed


_completion_store: CompletionStore | None = None


def get_completion_store() -> CompletionStore:
    """Return the shared, lazily created completion store."""
    global _completion_store
    if _completion_store is None:
        _completion_store = CompletionStore()
    return _completion_store
