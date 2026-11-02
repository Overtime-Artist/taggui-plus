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

Near duplicates are grouped strictly: every image in a group is within the
threshold of the group's main (largest) image, so a chain of small differences
(A looks like B, B looks like C) can never pull very different images together.

Both hashes are cached on disk keyed by ``(file_path, mtime_ns)`` (mirroring the
existing :class:`~utils.scan_cache.ScanCache`), so re-scanning a folder only
recomputes files whose modification time changed.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
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

    def prune(self, scanned_paths: list[Path]) -> None:
        """Forget files that no longer exist inside the folder that was just
        scanned, so entries for deleted, moved or renamed images don't pile
        up forever. Entries elsewhere are left alone."""
        if not scanned_paths:
            return
        try:
            root = os.path.commonpath([str(path.parent)
                                       for path in scanned_paths])
        except ValueError:
            # Images on different drives have no common folder.
            return
        prefix = os.path.join(root, '')
        scanned = {str(path) for path in scanned_paths}
        for key in list(self._entries):
            if (key.startswith(prefix) and key not in scanned
                    and not os.path.exists(key)):
                del self._entries[key]
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


# Bits set in each byte value, for numpy versions without `bitwise_count`.
_BYTE_BIT_COUNTS = np.array([bin(value).count('1') for value in range(256)],
                            dtype=np.uint16)


def _hashes_to_array(hashes: list[int]) -> np.ndarray:
    """Pack 256-bit hashes into an (n, 4) array of 64-bit words."""
    words = _DHASH_BITS // 64
    mask = (1 << 64) - 1
    return np.array([[(value >> (64 * word)) & mask for word in range(words)]
                     for value in hashes], dtype=np.uint64).reshape(-1, words)


def _distances(hashes: np.ndarray, center: np.ndarray) -> np.ndarray:
    """Hamming distance from `center` to every row of `hashes`."""
    differences = hashes ^ center
    if hasattr(np, 'bitwise_count'):
        return np.bitwise_count(differences).sum(axis=1, dtype=np.int32)
    return _BYTE_BIT_COUNTS[differences.view(np.uint8)].sum(axis=1)


def find_duplicate_groups(images, threshold: int,
                          cache: DuplicateCache | None = None,
                          progress_callback=None,
                          should_cancel=None) -> list[DuplicateGroup]:
    """Scan ``images`` and return groups of duplicates.

    ``images`` is any iterable of objects exposing ``path`` and ``dimensions``
    (the app's :class:`~utils.image.Image` works directly).

    ``threshold`` is the maximum dHash Hamming distance between an image and
    its group's main image for it to count as a near duplicate. ``0``
    disables perceptual matching entirely so that only byte-identical files
    (same SHA-256) are grouped ("exact / identical only"). Larger values also
    catch edited variants (with more false positives).

    ``progress_callback(stage, done, total)`` is called as the scan proceeds,
    with ``stage`` 'hashing' and then 'comparing'. ``should_cancel()`` (if
    given) is polled so a long scan can be aborted; on cancellation an empty
    list is returned.
    """
    if cache is None:
        cache = DuplicateCache()
        cache.load()

    records: list[ImageRecord] = []
    image_list = list(images)
    total = len(image_list)
    for done, image in enumerate(image_list):
        if should_cancel is not None and should_cancel():
            # Keep the hashes computed so far for the next scan.
            cache.save_if_dirty()
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
            progress_callback('hashing', done + 1, total)

    cache.prune([record.path for record in records])
    cache.save_if_dirty()

    # Byte-identical files (same SHA-256) always belong together; each set of
    # them is handled as one unit below.
    units: list[list[ImageRecord]] = []
    by_sha: dict[str, list[ImageRecord]] = {}
    for record in records:
        if record.sha256 is None:
            units.append([record])
        elif record.sha256 in by_sha:
            by_sha[record.sha256].append(record)
        else:
            by_sha[record.sha256] = [record]
            units.append(by_sha[record.sha256])
    for unit in units:
        unit.sort(key=_member_sort_key, reverse=True)

    if threshold <= 0:
        # "Exact / identical only": never merge images that merely look alike.
        clusters = [unit for unit in units if len(unit) > 1]
    else:
        clusters = _group_near_duplicates(units, threshold, progress_callback,
                                          should_cancel)
        if clusters is None:
            return []

    groups: list[DuplicateGroup] = []
    for members in clusters:
        distinct_hashes = {m.sha256 for m in members}
        kind = ('exact' if len(distinct_hashes) == 1
                and None not in distinct_hashes else 'near')
        # Show the largest / newest first inside each group. The sort is
        # stable, so the main image stays first if another one ties with it.
        members.sort(key=_member_sort_key, reverse=True)
        groups.append(DuplicateGroup(members, kind))

    # Exact groups first, then by size (biggest groups on top).
    groups.sort(key=lambda g: (g.kind != 'exact', -len(g.members)))
    return groups


def _group_near_duplicates(units: list[list[ImageRecord]], threshold: int,
                           progress_callback, should_cancel
                           ) -> list[list[ImageRecord]] | None:
    """Group the units (sets of byte-identical images) around main images.

    Going from the largest image down, each image not yet in a group becomes
    the main image of a new group, which takes every remaining image within
    `threshold` of it. Returns None if cancelled."""
    hashed = [unit for unit in units if unit[0].dhash is not None]
    clusters = [unit for unit in units
                if unit[0].dhash is None and len(unit) > 1]
    hashed.sort(key=lambda unit: _member_sort_key(unit[0]), reverse=True)
    hashes = _hashes_to_array([unit[0].dhash for unit in hashed])
    assigned = np.zeros(len(hashed), dtype=bool)
    count = len(hashed)
    for center in range(count):
        if center % 256 == 0:
            if should_cancel is not None and should_cancel():
                return None
            if progress_callback is not None:
                progress_callback('comparing', center, count)
        if assigned[center]:
            continue
        assigned[center] = True
        rest = slice(center + 1, count)
        close = ((_distances(hashes[rest], hashes[center]) <= threshold)
                 & ~assigned[rest])
        members = list(hashed[center])
        for offset in np.flatnonzero(close):
            members.extend(hashed[center + 1 + offset])
        assigned[rest] |= close
        if len(members) > 1:
            clusters.append(members)
    if progress_callback is not None:
        progress_callback('comparing', count, count)
    return clusters


def _member_sort_key(record: ImageRecord):
    width, height = (record.dimensions or (0, 0))
    pixels = (width or 0) * (height or 0)
    return (pixels, record.size or 0, record.mtime_ns or 0)
