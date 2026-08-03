"""
Duplicate and near-duplicate image detection (design: DUPLICATE_DETECTION_PLAN).

Phase 1 provides two tiers of detection:

* **Exact duplicates** — files whose raw bytes are identical, found via a
  SHA-256 content hash. This is 100% reliable and needs no image decoding.
* **Near duplicates** — visually similar images (resizes, re-compressions, minor
  edits) found via a 256-bit *difference hash* (dHash). Two dHashes are compared
  by counting how many bits differ (Hamming distance); a smaller distance means
  a more similar image. A strictness threshold controls how close two images
  must be to count as duplicates. A finer (256-bit) hash gives more useful,
  stricter gradations than a coarse 64-bit one.

Both hashes are cached on disk keyed by ``(file_path, mtime_ns)`` (mirroring the
existing :class:`~utils.scan_cache.ScanCache`), so re-scanning a folder only
recomputes files whose modification time changed.
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image as PILImage

from utils.completion_store import _hash_file
from utils.scan_cache import _get_cache_dir, _load_json, _save_json

# Edge length of the grayscale sample used for the difference hash. A
# (size+1) x size grayscale sample produces size*size comparisons between
# horizontally adjacent pixels. 16 -> 16x16 = 256 bits, which is far finer
# (and therefore stricter per step) than a coarse 8x8/64-bit hash.
_DHASH_SIZE = 16
# Total bits in a dHash. Stored alongside cached hashes so that raising or
# lowering the resolution automatically invalidates old cached values.
_DHASH_BITS = _DHASH_SIZE * _DHASH_SIZE


def compute_dhash(image_path) -> int | None:
    """Return a 256-bit perceptual difference hash for the image, or ``None``.

    The image is shrunk to a tiny ``(size + 1) x size`` grayscale thumbnail and
    each pixel is compared with its right-hand neighbour; the resulting bits are
    packed into a single integer. Resizes and re-compressions of the same image
    produce identical or very close hashes.
    """
    try:
        with PILImage.open(image_path) as image:
            # 'L' is 8-bit grayscale. Resizing to a fixed tiny size discards
            # resolution and most compression differences.
            small = image.convert('L').resize(
                (_DHASH_SIZE + 1, _DHASH_SIZE), PILImage.LANCZOS)
            pixels = list(small.getdata())
    except (OSError, ValueError, SyntaxError):
        # Unreadable, truncated, or unsupported image: skip it silently so one
        # bad file never aborts a whole scan.
        return None
    row_width = _DHASH_SIZE + 1
    bits = 0
    bit_index = 0
    for row in range(_DHASH_SIZE):
        row_start = row * row_width
        for column in range(_DHASH_SIZE):
            left = pixels[row_start + column]
            right = pixels[row_start + column + 1]
            if left > right:
                bits |= (1 << bit_index)
            bit_index += 1
    return bits


def hamming_distance(hash_a: int, hash_b: int) -> int:
    """Return the number of differing bits between two dHashes."""
    return (hash_a ^ hash_b).bit_count()


class DuplicateCache:
    """On-disk cache of each image's SHA-256 and dHash, keyed by
    ``(path, mtime_ns)``. Only files whose mtime changed are recomputed."""

    def __init__(self):
        self._path = _get_cache_dir() / 'duplicate_cache.json'
        # Maps ``str(image_path)`` -> {'mtime_ns', 'sha256', 'dhash'}.
        self._entries: dict[str, dict] = {}
        self._dirty = False

    def load(self) -> None:
        data = _load_json(self._path)
        entries: dict[str, dict] = {}
        for key, value in data.items():
            if isinstance(value, dict):
                entries[str(key)] = value
        self._entries = entries
        self._dirty = False

    def get(self, image_path: Path, mtime_ns: int | None) -> dict | None:
        """Return the cached ``{'sha256', 'dhash'}`` for a fresh entry, else
        ``None`` (a miss or a stale entry that must be recomputed)."""
        if mtime_ns is None:
            return None
        cached = self._entries.get(str(image_path))
        if cached is not None and cached.get('mtime_ns') == mtime_ns:
            return cached
        return None

    def put(self, image_path: Path, mtime_ns: int | None,
            sha256: str | None, dhash: int | None) -> None:
        if mtime_ns is None:
            return
        self._entries[str(image_path)] = {
            'mtime_ns': mtime_ns,
            'sha256': sha256,
            'dhash': dhash,
            # Records which hash resolution produced ``dhash`` so that changing
            # _DHASH_BITS invalidates values computed by an older version.
            'dhash_bits': _DHASH_BITS,
        }
        self._dirty = True

    def save_if_dirty(self) -> None:
        if not self._dirty:
            return
        _save_json(self._path, self._entries)
        self._dirty = False


class DuplicateGroup:
    """A set of images considered duplicates of one another.

    ``kind`` is ``'exact'`` when every member is byte-for-byte identical (all
    share one SHA-256), otherwise ``'near'``.
    """

    def __init__(self, members: list['ImageRecord'], kind: str):
        self.members = members
        self.kind = kind


class ImageRecord:
    """Lightweight per-image data used during a duplicate scan."""

    def __init__(self, path: Path, dimensions, size: int | None,
                 mtime_ns: int | None, sha256: str | None, dhash: int | None):
        self.path = path
        self.dimensions = dimensions
        self.size = size
        self.mtime_ns = mtime_ns
        self.sha256 = sha256
        self.dhash = dhash


def _stat_size_mtime(path: Path) -> tuple[int | None, int | None]:
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return None, None


def _find(parent: list[int], node: int) -> int:
    root = node
    while parent[root] != root:
        root = parent[root]
    # Path compression keeps the union-find fast on large datasets.
    while parent[node] != root:
        parent[node], node = root, parent[node]
    return root


def find_duplicate_groups(images, threshold: int,
                          cache: DuplicateCache | None = None,
                          progress_callback=None,
                          should_cancel=None) -> list[DuplicateGroup]:
    """Scan ``images`` and return groups of duplicates.

    ``images`` is any iterable of objects exposing ``path`` and ``dimensions``
    (the app's :class:`~utils.image.Image` works directly).

    ``threshold`` is the maximum dHash Hamming distance for two images to be
    considered near-duplicates. ``0`` disables perceptual matching entirely so
    that only byte-identical files (same SHA-256) are grouped ("exact /
    identical only"). Larger values also catch edited variants (with more false
    positives).

    ``progress_callback(done, total)`` is called as hashing proceeds and
    ``should_cancel()`` (if given) is polled so a long scan can be aborted; on
    cancellation an empty list is returned.
    """
    if cache is None:
        cache = DuplicateCache()
        cache.load()

    records: list[ImageRecord] = []
    image_list = list(images)
    total = len(image_list)
    for done, image in enumerate(image_list):
        if should_cancel is not None and should_cancel():
            return []
        path = Path(image.path)
        size, mtime_ns = _stat_size_mtime(path)
        cached = cache.get(path, mtime_ns)
        if cached is not None:
            sha256 = cached.get('sha256')
            # Reuse the cached dHash only if it was computed at the current
            # resolution; otherwise recompute just the dHash (the SHA-256 is
            # unaffected by hash-size changes, so no need to re-read the file
            # for it).
            if cached.get('dhash_bits') == _DHASH_BITS:
                dhash = cached.get('dhash')
            else:
                dhash = compute_dhash(path)
                cache.put(path, mtime_ns, sha256, dhash)
        else:
            sha256 = _hash_file(path)
            dhash = compute_dhash(path)
            cache.put(path, mtime_ns, sha256, dhash)
        records.append(ImageRecord(
            path=path, dimensions=getattr(image, 'dimensions', None),
            size=size, mtime_ns=mtime_ns, sha256=sha256, dhash=dhash))
        if progress_callback is not None:
            progress_callback(done + 1, total)

    cache.save_if_dirty()

    # Union-find over duplicate relationships. Two images are joined when they
    # are byte-identical (same SHA-256) or their dHash distance is within the
    # threshold. Exact copies naturally have distance 0 and are joined too.
    parent = list(range(len(records)))

    def union(a: int, b: int) -> None:
        root_a, root_b = _find(parent, a), _find(parent, b)
        if root_a != root_b:
            parent[root_b] = root_a

    # First pass: join exact duplicates by SHA-256 (cheap, no distance maths).
    by_sha: dict[str, int] = {}
    for index, record in enumerate(records):
        if record.sha256 is None:
            continue
        if record.sha256 in by_sha:
            union(by_sha[record.sha256], index)
        else:
            by_sha[record.sha256] = index

    # Second pass: join near duplicates by dHash distance. Skipped entirely
    # when the threshold is 0 ("exact / identical only"), so that mode groups
    # strictly by byte-identical SHA-256 and never merges images that merely
    # look alike. O(n^2) over images that have a dHash; fine for the typical
    # few-hundred-to-few-thousand set.
    if threshold > 0:
        hashed = [i for i, r in enumerate(records) if r.dhash is not None]
        for a_pos in range(len(hashed)):
            if should_cancel is not None and should_cancel():
                return []
            i = hashed[a_pos]
            for b_pos in range(a_pos + 1, len(hashed)):
                j = hashed[b_pos]
                if _find(parent, i) == _find(parent, j):
                    continue  # Already grouped (e.g. exact match).
                if hamming_distance(records[i].dhash,
                                    records[j].dhash) <= threshold:
                    union(i, j)

    # Collect groups, keeping only those with more than one member.
    clusters: dict[int, list[ImageRecord]] = {}
    for index, record in enumerate(records):
        root = _find(parent, index)
        clusters.setdefault(root, []).append(record)

    groups: list[DuplicateGroup] = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        distinct_hashes = {m.sha256 for m in members if m.sha256 is not None}
        kind = 'exact' if len(distinct_hashes) == 1 else 'near'
        # Show the largest / newest first inside each group.
        members.sort(key=_member_sort_key, reverse=True)
        groups.append(DuplicateGroup(members, kind))

    # Exact groups first, then by size (biggest groups on top).
    groups.sort(key=lambda g: (g.kind != 'exact', -len(g.members)))
    return groups


def _member_sort_key(record: ImageRecord):
    width, height = (record.dimensions or (0, 0))
    pixels = (width or 0) * (height or 0)
    return (pixels, record.size or 0, record.mtime_ns or 0)
