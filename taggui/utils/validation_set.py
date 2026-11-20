"""Choosing a validation set for fine-tuning (Tools > Create Validation Set).

A validation set is a small share of a dataset that is held out of training so
the trainer can measure how well the model generalizes. The picks here are
*tag-balanced*: images are chosen so that each tag is represented roughly in
proportion to how often it appears in the whole folder, which makes sure that
concepts common enough to expect in the validation set are not missed by
chance. Near-duplicate variants are kept together ("units"), so near-copies
never end up on both sides of the split.

Images can be permanently marked "never pick". The marks are stored in a small
hidden file in each image's folder, so they travel with the dataset.
"""

from __future__ import annotations

import heapq
import json
import math
import os
import random
from collections import Counter
from pathlib import Path

from utils.image_file_operations import get_path_key

DEFAULT_FOLDER_NAME = 'Validation_Set'
NEVER_PICK_FILE_NAME = '.taggui_validation.json'

# Variant matching levels -> maximum dHash distance (see duplicate_detection).
# 0 still groups byte-identical copies.
VARIANT_MATCHING_LEVELS = {
    'Off (exact copies only)': 0,
    'Strict': 4,
    'Medium': 10,
    'Loose': 16,
}
DEFAULT_VARIANT_MATCHING = 'Medium'

_FILE_ATTRIBUTE_HIDDEN = 0x2


# --- Folders ------------------------------------------------------------------

def is_in_validation_folder(path: Path, root: Path, folder_name: str) -> bool:
    """Whether `path` lies inside a folder called `folder_name` (any case)
    somewhere below `root`."""
    try:
        parts = path.relative_to(root).parts[:-1]
    except ValueError:
        parts = path.parts[:-1]
    wanted = folder_name.casefold()
    return any(part.casefold() == wanted for part in parts)


def return_destination(path: Path, root: Path, folder_name: str) -> Path:
    """The folder an image set aside in a validation folder below `root`
    goes back to: the one holding its outermost folder called
    `folder_name` (any case)."""
    try:
        parts = path.relative_to(root).parts[:-1]
    except ValueError:
        return path.parent
    wanted = folder_name.casefold()
    for index, part in enumerate(parts):
        if part.casefold() == wanted:
            return root.joinpath(*parts[:index])
    return path.parent


def is_within(path: Path, folder: Path, include_subfolders: bool) -> bool:
    """Whether the image `path` is directly in `folder` (or, with
    `include_subfolders`, anywhere below it)."""
    if include_subfolders:
        folder_key = get_path_key(folder).rstrip('\\/')
        parent_key = get_path_key(path.parent)
        return (parent_key == folder_key
                or parent_key.startswith(folder_key + os.sep))
    return get_path_key(path.parent) == get_path_key(folder)


def source_folders(image_paths, root: Path, folder_name: str) -> list[Path]:
    """Every folder (the root and those below it) that holds images, plus
    their parents up to the root, leaving out validation folders. Sorted by
    path."""
    root_key = get_path_key(root)
    folders = {root_key: root}
    for path in image_paths:
        if is_in_validation_folder(path, root, folder_name):
            continue
        folder = path.parent
        while True:
            key = get_path_key(folder)
            if key in folders or len(key) < len(root_key):
                break
            folders[key] = folder
            if folder == folder.parent:
                break
            folder = folder.parent
    return sorted(folders.values(), key=lambda f: get_path_key(f))


# --- Never-pick marks ---------------------------------------------------------

def _set_hidden(path: Path, hidden: bool):
    if os.name != 'nt':
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        attributes = kernel32.GetFileAttributesW(str(path))
        if attributes == -1 or attributes == 0xFFFFFFFF:
            return
        if hidden:
            attributes |= _FILE_ATTRIBUTE_HIDDEN
        else:
            attributes &= ~_FILE_ATTRIBUTE_HIDDEN
        kernel32.SetFileAttributesW(str(path), attributes)
    except (AttributeError, OSError):
        pass


def find_never_pick_files(root: Path) -> list[Path]:
    """Every never-pick marks file in `root` and its subfolders."""
    wanted = NEVER_PICK_FILE_NAME.casefold()
    found = []
    for folder, _subfolders, file_names in os.walk(root):
        found.extend(Path(folder) / name for name in file_names
                     if name.casefold() == wanted)
    return found


def remove_never_pick_files(paths) -> list[Path]:
    """Delete marks files. Returns the ones that couldn't be deleted."""
    failed = []
    for path in paths:
        try:
            _set_hidden(path, False)
            path.unlink(missing_ok=True)
        except OSError:
            failed.append(path)
    return failed


class NeverPickStore:
    """The "never pick for validation" marks, read from and written to a
    hidden file in each image's folder (file names, relative to it)."""

    def __init__(self):
        # Folder key -> {file name key: file name}.
        self._folders: dict[str, dict[str, str]] = {}
        # Folder key -> marks file that exists but couldn't be read. Its
        # marks are ignored, and it is never overwritten.
        self.unreadable: dict[str, Path] = {}

    def forget(self):
        """Drop the cached marks, so files edited outside the app are read
        again."""
        self._folders.clear()
        self.unreadable.clear()

    def _names(self, folder: Path) -> dict[str, str]:
        key = get_path_key(folder)
        if key not in self._folders:
            names = {}
            file_path = folder / NEVER_PICK_FILE_NAME
            try:
                data = json.loads(file_path.read_text(encoding='utf-8'))
                if not isinstance(data, dict):
                    raise ValueError('not a JSON object')
                for name in data.get('never_pick', []):
                    if isinstance(name, str):
                        names[os.path.normcase(name)] = name
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError):
                names = {}
                self.unreadable[key] = file_path
            self._folders[key] = names
        return self._folders[key]

    def is_marked(self, image_path: Path) -> bool:
        return (os.path.normcase(image_path.name)
                in self._names(image_path.parent))

    def set_marked(self, image_paths, marked: bool):
        """Mark or unmark images and save every affected folder's file.
        Raises OSError if a file can't be read or written."""
        image_paths = list(image_paths)
        for path in image_paths:
            self._names(path.parent)
            file_path = self.unreadable.get(get_path_key(path.parent))
            if file_path is not None:
                raise OSError(f'{file_path} could not be read, so it was left '
                              f'unchanged. Fix or delete it, then try again.')
        changed_folders = {}
        for path in image_paths:
            names = self._names(path.parent)
            name_key = os.path.normcase(path.name)
            if marked and name_key not in names:
                names[name_key] = path.name
            elif not marked and name_key in names:
                del names[name_key]
            else:
                continue
            changed_folders[get_path_key(path.parent)] = path.parent
        for folder in changed_folders.values():
            self._save(folder)

    def _save(self, folder: Path):
        names = self._names(folder)
        # Forget images that no longer exist (moved, renamed or deleted).
        for name_key, name in list(names.items()):
            if not (folder / name).exists():
                del names[name_key]
        file_path = folder / NEVER_PICK_FILE_NAME
        if not names:
            if file_path.exists():
                _set_hidden(file_path, False)
                file_path.unlink()
            return
        text = json.dumps({'never_pick': sorted(names.values(),
                                                key=str.casefold)},
                          indent=2, ensure_ascii=False)
        # Windows refuses to overwrite a hidden file, so unhide it first.
        if file_path.exists():
            _set_hidden(file_path, False)
        file_path.write_text(text + '\n', encoding='utf-8')
        _set_hidden(file_path, True)


# --- Picking ------------------------------------------------------------------

def target_count(total: int, percentage: float) -> int:
    """How many images make up `percentage`% of `total` (rounded up, like
    common validation-split scripts)."""
    if total <= 0 or percentage <= 0:
        return 0
    return min(total, math.ceil(total * percentage / 100 - 1e-9))


def build_units(item_count: int, groups) -> list[list[int]]:
    """Partition items 0..item_count-1 into units: each group of near
    duplicates (lists of item indices, possibly overlapping) becomes one unit,
    every other item its own."""
    parent = list(range(item_count))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for group in groups:
        group = list(group)
        for other in group[1:]:
            root_a, root_b = find(group[0]), find(other)
            if root_a != root_b:
                parent[root_b] = root_a
    units: dict[int, list[int]] = {}
    for index in range(item_count):
        units.setdefault(find(index), []).append(index)
    return list(units.values())


def pick_units(item_tags: list, units: list[list[int]], eligible: list[bool],
               existing: list[int], target_total: int,
               rng: random.Random) -> list[int]:
    """Choose units (indices into `units`) for the validation set.

    `item_tags` holds the tags of every image in scope: the candidates and
    the images already in the validation folder (`existing`, item indices).
    Units are added greedily, each time taking the one that best fills the
    gap between each tag's share of the validation set and its share of the
    whole folder. Rare tags weigh as much as common ones, so every tag that
    is common enough to expect in the set tends to get represented. A random
    factor (from `rng`) varies the picks between re-rolls.
    """
    item_count = len(item_tags)
    remaining = target_total - len(existing)
    if remaining <= 0 or item_count == 0:
        return []
    share = target_total / item_count
    tag_counts = Counter(tag for tags in item_tags for tag in set(tags))
    desired = {tag: count * share for tag, count in tag_counts.items()}
    in_set = Counter(tag for index in existing
                     for tag in set(item_tags[index]))
    unit_tag_counts = [Counter(tag for index in unit
                               for tag in set(item_tags[index]))
                       for unit in units]
    weights = {index: 0.75 + 0.5 * rng.random()
               for index in range(len(units)) if eligible[index]}

    def score(unit_index: int) -> float:
        gain = 0.0
        for tag, count in unit_tag_counts[unit_index].items():
            want = desired[tag]
            have = in_set[tag]
            scale = max(want, 1.0)
            missing_before = max(0.0, want - have)
            missing_after = max(0.0, want - have - count)
            gain += (missing_before - missing_after) / scale
            overshoot = max(0.0, have + count - scale) - max(0.0, have - scale)
            gain -= 0.5 * overshoot / scale
        size = len(units[unit_index])
        # A small random tie-breaker so untagged images are picked at random.
        return (gain / size + 0.01) * weights[unit_index]

    # Lazy greedy: a unit's score only goes down as the set fills up, so a
    # stale score is an upper bound and only the top needs re-scoring.
    heap = [(-score(index), index) for index in weights]
    heapq.heapify(heap)
    picked = []
    while heap and remaining > 0:
        _, index = heapq.heappop(heap)
        if len(units[index]) > remaining:
            continue
        current = score(index)
        if heap and current < -heap[0][0] - 1e-12:
            heapq.heappush(heap, (-current, index))
            continue
        picked.append(index)
        remaining -= len(units[index])
        in_set.update(unit_tag_counts[index])
    return picked


def tag_coverage(item_tags: list, validation_items, target_total: int):
    """Which tags are common enough to expect in the validation set, and which
    of those it misses.

    Returns (minimum image count, expected tags, missed tags), where a tag is
    expected when it is on at least `minimum` images (so its proportional
    share is at least one image). Missed tags are sorted most common first.
    """
    item_count = len(item_tags)
    if item_count == 0 or target_total <= 0:
        return 0, [], []
    minimum = math.ceil(item_count / target_total - 1e-9)
    tag_counts = Counter(tag for tags in item_tags for tag in set(tags))
    expected = [tag for tag, count in tag_counts.items() if count >= minimum]
    in_set = {tag for index in validation_items
              for tag in item_tags[index]}
    missed = sorted((tag for tag in expected if tag not in in_set),
                    key=lambda tag: (-tag_counts[tag], tag))
    return minimum, expected, missed
