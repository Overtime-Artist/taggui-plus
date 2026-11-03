"""File operations on images and their caption files (move, copy, rename).

Rules shared by every operation:
* An existing file is never overwritten.
* The caption `.txt` file travels with its image. A caption that another
  image still uses (`a.png` and `a.jpg` share `a.txt`) is copied rather than
  moved, so the other image keeps its tags.
* An image is never placed next to an existing caption file it would
  silently adopt (e.g. moving `a.png` into a folder that has `a.txt`).
"""
import os
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from models.image_list_model import get_caption_key

WINDOWS_INVALID_CHARACTERS = '<>:"/\\|?*'
WINDOWS_RESERVED_NAMES = {'CON', 'PRN', 'AUX', 'NUL',
                          *(f'COM{i}' for i in range(1, 10)),
                          *(f'LPT{i}' for i in range(1, 10))}


def get_invalid_file_name_reason(stem: str) -> str | None:
    """Why `stem` can't be used as a file name on this operating system, or
    None if it can. Windows forbids more characters than Linux/macOS, where
    only `/` is not allowed."""
    if stem in ('.', '..'):
        return f'"{stem}" cannot be used as a file name.'
    if os.name == 'nt':
        invalid = sorted({c for c in stem
                          if c in WINDOWS_INVALID_CHARACTERS or ord(c) < 32})
        if invalid:
            shown = ' '.join(c if c.isprintable() else repr(c)
                             for c in invalid)
            return f'File names cannot contain these characters: {shown}'
        if stem.endswith(('.', ' ')):
            return 'File names cannot end with a dot or a space.'
        if stem.split('.')[0].strip().upper() in WINDOWS_RESERVED_NAMES:
            return f'"{stem}" is a name reserved by Windows.'
    elif '/' in stem or '\0' in stem:
        return 'File names cannot contain "/".'
    return None


def get_path_key(path: Path) -> str:
    """Comparison key for a path: ignores upper/lower case on Windows."""
    return os.path.normcase(str(path))


def find_caption_file(image_path: Path) -> Path | None:
    """The existing caption file of an image, whatever the case of its `.txt`
    extension, or None if there is none."""
    caption_path = image_path.with_suffix('.txt')
    if caption_path.exists():
        return caption_path
    if os.name == 'nt':
        # `exists()` already ignores case on Windows.
        return None
    wanted = caption_path.name.lower()
    try:
        with os.scandir(image_path.parent) as entries:
            for entry in entries:
                if entry.name.lower() == wanted and entry.is_file():
                    return Path(entry.path)
    except OSError:
        pass
    return None


def count_caption_users(image_paths) -> Counter:
    """How many of the given images use each caption file (by caption key)."""
    return Counter(get_caption_key(path.with_suffix('.txt'))
                   for path in image_paths)


@dataclass
class TransferItem:
    image_path: Path
    caption_path: Path | None
    target_image_path: Path
    target_caption_path: Path
    is_clash: bool = False


@dataclass
class TransferPlan:
    items: list[TransferItem]
    # Images skipped because they are already in the target folder (move).
    already_in_target: list[Path]

    @property
    def clashes(self) -> list[TransferItem]:
        return [item for item in self.items if item.is_clash]


def plan_transfer(image_paths: list[Path], target_directory: Path,
                  is_move: bool) -> TransferPlan:
    """Work out where each image and caption goes, and which of them clash
    with an existing file in the target folder or with another selected image
    of the same name."""
    target_directory_key = get_path_key(target_directory.resolve())
    items = []
    already_in_target = []
    # Target key -> key of the source caption that will be written there (or
    # the image path key for image targets).
    reserved: dict[str, str] = {}
    for image_path in image_paths:
        if (is_move and get_path_key(image_path.parent.resolve())
                == target_directory_key):
            already_in_target.append(image_path)
            continue
        caption_path = find_caption_file(image_path)
        item = TransferItem(
            image_path=image_path, caption_path=caption_path,
            target_image_path=target_directory / image_path.name,
            target_caption_path=(target_directory
                                 / image_path.with_suffix('.txt').name))
        item.is_clash = not _are_targets_free(item, reserved)
        if not item.is_clash:
            _reserve_targets(item, reserved)
        items.append(item)
    return TransferPlan(items=items, already_in_target=already_in_target)


def resolve_clashes_by_renaming(plan: TransferPlan):
    """Give every clashing item a free name such as `a (2).png`, so both the
    existing file and the new one are kept."""
    reserved: dict[str, str] = {}
    for item in plan.items:
        if not item.is_clash:
            _reserve_targets(item, reserved)
    for item in plan.items:
        if not item.is_clash:
            continue
        directory = item.target_image_path.parent
        stem = item.image_path.stem
        suffix = item.image_path.suffix
        number = 2
        while True:
            item.target_image_path = directory / f'{stem} ({number}){suffix}'
            item.target_caption_path = directory / f'{stem} ({number}).txt'
            if _are_targets_free(item, reserved):
                break
            number += 1
        item.is_clash = False
        _reserve_targets(item, reserved)


def _source_caption_key(item: TransferItem) -> str:
    if item.caption_path is None:
        return 'no caption:' + get_path_key(item.image_path)
    return get_caption_key(item.caption_path)


def _are_targets_free(item: TransferItem, reserved: dict[str, str]) -> bool:
    image_key = get_path_key(item.target_image_path)
    if item.target_image_path.exists() or image_key in reserved:
        return False
    caption_key = get_caption_key(item.target_caption_path)
    if caption_key in reserved:
        # Two selected images sharing one caption (`a.png` and `a.jpg` with
        # `a.txt`) may keep sharing it at the target.
        return reserved[caption_key] == _source_caption_key(item)
    return find_caption_file(item.target_caption_path) is None


def _reserve_targets(item: TransferItem, reserved: dict[str, str]):
    reserved[get_path_key(item.target_image_path)] = get_path_key(
        item.image_path)
    reserved[get_caption_key(item.target_caption_path)] = (
        _source_caption_key(item))


def _transfer_file(source: Path, target: Path, is_move: bool):
    # Checked right before writing: never overwrite an existing file.
    if target.exists():
        raise FileExistsError(f'{target.name} already exists')
    if is_move:
        # Unlike `Path.replace`, `shutil.move` also works across drives.
        shutil.move(str(source), str(target))
    else:
        shutil.copy2(source, target)


@dataclass
class TransferResult:
    # (source image path, target image path) for every image transferred.
    transferred: list[tuple[Path, Path]]
    errors: list[str]


def execute_transfer(plan: TransferPlan, is_move: bool,
                     caption_users: Counter) -> TransferResult:
    """Move or copy the non-clashing items of `plan`. `caption_users` counts
    how many images in the whole list use each caption (see
    `count_caption_users`). When moving, a caption is only moved away once no
    image left behind still uses it; until then it is copied."""
    verb = 'moved' if is_move else 'copied'
    items = [item for item in plan.items if not item.is_clash]
    remaining_users = Counter(caption_users)
    # Source caption key -> caption keys of the targets already written.
    written_captions: dict[str, set[str]] = {}
    transferred = []
    errors = []
    for item in items:
        try:
            _transfer_file(item.image_path, item.target_image_path, is_move)
        except OSError as exception:
            errors.append(f'{item.image_path.name} could not be {verb}: '
                          f'{_describe_error(exception)}')
            continue
        transferred.append((item.image_path, item.target_image_path))
        if item.caption_path is None:
            continue
        caption_key = _source_caption_key(item)
        if is_move:
            remaining_users[caption_key] -= 1
        is_last_user = is_move and remaining_users[caption_key] <= 0
        written_targets = written_captions.setdefault(caption_key, set())
        target_key = get_caption_key(item.target_caption_path)
        try:
            if target_key in written_targets:
                if is_last_user:
                    item.caption_path.unlink()
            else:
                _transfer_file(item.caption_path, item.target_caption_path,
                               is_move=is_last_user)
                written_targets.add(target_key)
        except OSError as exception:
            errors.append(f'{item.image_path.name} was {verb}, but its caption '
                          f'file {item.caption_path.name} could not be: '
                          f'{_describe_error(exception)}')
    return TransferResult(transferred=transferred, errors=errors)


def _describe_error(exception: OSError) -> str:
    if isinstance(exception, FileExistsError):
        return 'a file with that name already exists'
    return exception.strerror or str(exception)


def format_error_summary(errors: list[str], max_shown: int = 10) -> str:
    lines = errors[:max_shown]
    if len(errors) > max_shown:
        lines.append(f'...and {len(errors) - max_shown} more.')
    return '\n'.join(lines)
