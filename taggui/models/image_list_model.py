import random
import re
import sys
from collections import Counter, deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import exifread
import imagesize
from PySide6.QtCore import (QAbstractListModel, QModelIndex, QObject,
                            QRunnable, QSize, Qt, QThreadPool, Signal, Slot)
from PySide6.QtGui import QIcon, QImage, QImageReader, QPixmap
from PySide6.QtWidgets import QMessageBox

from utils.completion_store import get_completion_store
from utils.enums import ImageListSortBy, SortOrder
from utils.image import Image, build_caption_text, parse_caption_text
from utils.settings import DEFAULT_SETTINGS, get_settings
from utils.thumbnail_cache import evict_to_limit, get_cache_path
from utils.utils import get_confirmation_dialog_reply, pluralize

UNDO_STACK_SIZE = 32

# Relative priorities for thumbnail work submitted to the capped preload pool.
# On-demand thumbnails (items currently scrolling into view) should run before
# speculative bulk preloads.
ON_DEMAND_THUMBNAIL_PRIORITY = 1
# Background warming of never-cached thumbnails runs at a lower priority than
# on-demand loads so that items scrolling into view always jump ahead and the
# UI stays responsive.
BACKGROUND_THUMBNAIL_PRIORITY = 0


class BackgroundThumbnailWarmer(QRunnable):
    """
    Walks the current image list in a single low-priority background thread and
    writes a disk thumbnail for any image that has never been cached, so the
    Images pane no longer has to decode them the first time they scroll into
    view. It only touches the on-disk cache (it does not keep thumbnails in
    memory), so warming even a very large directory costs almost no RAM. The
    generation check lets it stop the instant the user switches directory or
    changes the thumbnail size.
    """

    def __init__(self, entries: list[tuple[Path, int | None]],
                 image_width: int, generation: int, is_current):
        super().__init__()
        self.entries = entries
        self.image_width = image_width
        self.generation = generation
        self.is_current = is_current

    def run(self):
        for image_path, file_modified_time_ns in self.entries:
            # Bail out as soon as this work is superseded (directory switched
            # or thumbnail size changed).
            if not self.is_current(self.generation):
                return
            cache_path = get_cache_path(
                image_path, file_modified_time_ns, self.image_width)
            # Skip images with no stable key or that are already cached; this is
            # what makes warming cheap on a directory that's mostly cached.
            if cache_path is None or cache_path.exists():
                continue
            image_reader = QImageReader(str(image_path))
            # Rotate the image based on the orientation tag.
            image_reader.setAutoTransform(True)
            image = image_reader.read()
            if image.isNull():
                continue
            thumbnail_image = image.scaledToWidth(
                self.image_width, Qt.TransformationMode.SmoothTransformation)
            if not thumbnail_image.isNull():
                thumbnail_image.save(str(cache_path))


class _CacheEvictionRunner(QRunnable):
    """Runs LRU cache eviction once at startup so over-limit entries are pruned."""

    def __init__(self, max_bytes: int):
        super().__init__()
        self.max_bytes = max_bytes

    def run(self):
        evict_to_limit(self.max_bytes)


def get_file_paths(directory_path: Path) -> set[Path]:
    """
    Recursively get all file paths in a directory, including those in
    subdirectories.
    """
    file_paths = set()
    for path in directory_path.iterdir():
        if path.is_file():
            file_paths.add(path)
        elif path.is_dir():
            file_paths.update(get_file_paths(path))
    return file_paths


def read_caption_file(text_file_path: Path,
                      tag_separator: str) -> tuple[list[str], str, int | None]:
    """
    Read and parse a caption `.txt` file.

    Returns (tags, natural_language_prompt, caption_file_modified_time_ns).
    The modified time is None when the file does not exist or cannot be read.
    """
    tags: list[str] = []
    natural_language_prompt = ''
    try:
        caption_file_modified_time_ns = text_file_path.stat().st_mtime_ns
    except OSError:
        return tags, natural_language_prompt, None
    # `errors='replace'` inserts a replacement marker such as '?' when there
    # is malformed data.
    try:
        caption = text_file_path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return tags, natural_language_prompt, None
    if caption:
        tags, natural_language_prompt = parse_caption_text(caption,
                                                           tag_separator)
    return tags, natural_language_prompt, caption_file_modified_time_ns


def get_image_dimensions(image_path: Path) -> tuple[int, int] | None:
    try:
        dimensions = imagesize.get(image_path)
        # Check the Exif orientation tag and rotate the dimensions if
        # necessary.
        with open(image_path, 'rb') as image_file:
            try:
                exif_tags = exifread.process_file(
                    image_file, details=False,
                    stop_tag='Image Orientation')
                if 'Image Orientation' in exif_tags:
                    orientations = exif_tags['Image Orientation'].values
                    if any(value in orientations for value in (5, 6, 7, 8)):
                        dimensions = (dimensions[1], dimensions[0])
            except Exception as exception:
                print(f'Failed to get Exif tags for {image_path}: '
                      f'{exception}', file=sys.stderr)
        return dimensions
    except (ValueError, OSError) as exception:
        print(f'Failed to get dimensions for {image_path}: '
              f'{exception}', file=sys.stderr)
        return None


@dataclass
class HistoryItem:
    action_name: str
    tags: list[list[str]]
    natural_language_prompts: list[str]
    should_ask_for_confirmation: bool


class Scope(str, Enum):
    ALL_IMAGES = 'All images'
    FILTERED_IMAGES = 'Filtered images'
    SELECTED_IMAGES = 'Selected images'


class DirectoryScanner(QObject, QRunnable):
    """Scans a directory for images and captions off the main thread."""
    scan_complete = Signal(list)

    def __init__(self, directory_path: Path, image_suffixes: list,
                 tag_separator: str):
        QObject.__init__(self)
        QRunnable.__init__(self)
        self.directory_path = directory_path
        self.image_suffixes = image_suffixes
        self.tag_separator = tag_separator
        self.setAutoDelete(False)

    @Slot()
    def run(self):
        from utils.scan_cache import ScanCache
        scan_cache = ScanCache()
        scan_cache.load()
        completion_store = get_completion_store()
        completion_store.load()

        file_paths = get_file_paths(self.directory_path)
        image_paths = sorted(
            p for p in file_paths if p.suffix.lower() in self.image_suffixes
        )
        text_file_path_strings = {str(p) for p in file_paths
                                  if p.suffix == '.txt'}

        dim_updates: dict = {}
        cap_updates: dict = {}
        images: list = []
        # Per-image info handed to the completion store so it can refresh
        # hashes and re-home "complete" flags after external moves/renames.
        scanned_for_completion: list = []

        for image_path in image_paths:
            # Single stat for image mtime/size (cache key + Image attribute +
            # completion-store size prefilter).
            try:
                image_stat = image_path.stat()
                mtime_ns = image_stat.st_mtime_ns
                file_size = image_stat.st_size
            except OSError:
                mtime_ns = None
                file_size = None

            # Dimensions — use cache if available, otherwise read from file.
            if scan_cache.is_dimensions_cached(image_path, mtime_ns):
                dimensions = scan_cache.get_dimensions(image_path, mtime_ns)
            else:
                dimensions = get_image_dimensions(image_path)
                scan_cache.cache_dimensions(dim_updates, image_path,
                                            mtime_ns, dimensions)

            # Caption — use cache if available, otherwise read from disk.
            txt_path = image_path.with_suffix('.txt')
            txt_path_str = str(txt_path)
            tags: list = []
            nl: str = ''
            cap_mtime_ns = None

            if txt_path_str in text_file_path_strings:
                try:
                    cap_mtime_ns = txt_path.stat().st_mtime_ns
                except OSError:
                    cap_mtime_ns = None

                cached_cap = (
                    scan_cache.get_caption(txt_path_str, cap_mtime_ns)
                    if cap_mtime_ns is not None else None
                )
                if cached_cap is not None:
                    tags, nl = cached_cap
                else:
                    try:
                        caption = txt_path.read_text(encoding='utf-8',
                                                     errors='replace')
                        if caption:
                            tags, nl = parse_caption_text(
                                caption, self.tag_separator)
                    except OSError:
                        pass
                    if cap_mtime_ns is not None:
                        scan_cache.cache_caption(cap_updates, txt_path_str,
                                                 cap_mtime_ns, tags, nl)

            images.append(Image(image_path, dimensions, tags, nl,
                                file_modified_time_ns=mtime_ns,
                                caption_file_modified_time_ns=cap_mtime_ns,
                                is_complete=completion_store.is_complete(
                                    image_path)))
            scanned_for_completion.append({
                'path': image_path,
                'size': file_size,
                'mtime_ns': mtime_ns,
                'cap_path': txt_path_str,
                'cap_mtime_ns': cap_mtime_ns,
            })

        # Reconcile the completion store against this scan: refresh hashes for
        # externally edited images and re-home "complete" flags for images that
        # were moved or renamed outside the program. Then sync the flag onto the
        # freshly built Image objects (re-homing can newly complete an image).
        if completion_store.reconcile(scanned_for_completion):
            completion_store.save()
        for image in images:
            image.is_complete = completion_store.is_complete(image.path)

        live_dim_keys = {str(image_path) for image_path in image_paths}
        scan_cache.save_async(dim_updates, cap_updates,
                              directory_path=self.directory_path,
                              live_dim_keys=live_dim_keys,
                              live_cap_keys=text_file_path_strings)
        images.sort(key=lambda img: img.path)
        self.scan_complete.emit(images)


@dataclass
class RefreshResult:
    """The outcome of a background directory refresh (see RefreshScanner)."""
    directory_path: Path
    added_images: list
    removed_paths: set
    # path -> {'file': (snapshot_mtime, new_mtime, dimensions),
    #          'caption': (snapshot_mtime, new_mtime, tags, prompt)}
    updates: dict
    # path -> new is_complete value, for images already in the model whose
    # "complete" flag changed during reconciliation (e.g. a completed image
    # that was moved into a subfolder and re-homed onto its new path).
    completion_changes: dict = field(default_factory=dict)


class RefreshScanner(QObject, QRunnable):
    """
    Re-scans the current directory off the main thread to detect files that
    changed while the window was in the background, then reports a diff.

    All disk I/O (directory walk, stats, reading changed caption files) happens
    here, in a worker thread, so re-focusing the window never freezes the UI.
    The resulting diff is applied on the main thread by
    ImageListModel.apply_refresh_result, which does only fast in-memory work.
    """
    refresh_complete = Signal(object)

    def __init__(self, directory_path: Path, image_suffixes: list,
                 tag_separator: str, snapshot: dict):
        QObject.__init__(self)
        QRunnable.__init__(self)
        self.directory_path = directory_path
        self.image_suffixes = image_suffixes
        self.tag_separator = tag_separator
        # snapshot: path -> (file_modified_time_ns, caption_file_modified_time_ns)
        # for every image currently loaded, captured on the main thread.
        self.snapshot = snapshot
        self.setAutoDelete(False)

    @Slot()
    def run(self):
        try:
            result = self._scan()
        except OSError:
            # The directory may have been removed or become unreadable while
            # scanning. Report an empty (no-op) diff so the UI simply skips
            # this refresh; the next re-focus will try again.
            result = RefreshResult(self.directory_path, [], set(), {})
        self.refresh_complete.emit(result)

    def _scan(self) -> 'RefreshResult':
        completion_store = get_completion_store()
        file_paths = get_file_paths(self.directory_path)
        image_paths = {path for path in file_paths
                       if path.suffix.lower() in self.image_suffixes}
        text_file_path_strings = {str(path) for path in file_paths
                                  if path.suffix == '.txt'}
        existing_paths = set(self.snapshot.keys())
        removed_paths = existing_paths - image_paths
        added_paths = sorted(image_paths - existing_paths)

        # Per-image info handed to the completion store so it can refresh hashes
        # and re-home "complete" flags after external moves/renames, exactly as
        # the full directory scan does. Gathered for every image on disk (both
        # newly added and already loaded) while we stat them below.
        scanned_for_completion: list = []

        # Build full Image objects for newly added files (read-only work).
        added_images = []
        for image_path in added_paths:
            dimensions = get_image_dimensions(image_path)
            try:
                image_stat = image_path.stat()
                file_modified_time_ns = image_stat.st_mtime_ns
                file_size = image_stat.st_size
            except OSError:
                file_modified_time_ns = None
                file_size = None
            tags: list = []
            natural_language_prompt = ''
            caption_file_modified_time_ns = None
            text_file_path = image_path.with_suffix('.txt')
            text_file_path_str = str(text_file_path)
            if text_file_path_str in text_file_path_strings:
                (tags, natural_language_prompt,
                 caption_file_modified_time_ns) = read_caption_file(
                     text_file_path, self.tag_separator)
            # is_complete is filled in after reconcile() below, since re-homing
            # can newly complete a moved-in image.
            added_images.append(Image(
                image_path, dimensions, tags, natural_language_prompt,
                file_modified_time_ns=file_modified_time_ns,
                caption_file_modified_time_ns=caption_file_modified_time_ns,
                is_complete=False))
            scanned_for_completion.append({
                'path': image_path,
                'size': file_size,
                'mtime_ns': file_modified_time_ns,
                'cap_path': text_file_path_str,
                'cap_mtime_ns': caption_file_modified_time_ns,
            })
        added_images.sort(key=lambda img: img.path)

        # Detect changes to images that already existed at snapshot time.
        updates: dict = {}
        for image_path, (snapshot_file_mtime,
                         snapshot_caption_mtime) in self.snapshot.items():
            if image_path in removed_paths:
                continue
            entry: dict = {}

            # Image file: single stat for mtime (change detection) and size
            # (completion-store re-home prefilter).
            try:
                image_stat = image_path.stat()
                new_file_mtime = image_stat.st_mtime_ns
                file_size = image_stat.st_size
            except OSError:
                new_file_mtime = snapshot_file_mtime
                file_size = None
            if new_file_mtime != snapshot_file_mtime:
                entry['file'] = (snapshot_file_mtime, new_file_mtime,
                                 get_image_dimensions(image_path))

            # Caption file: stat first; only read + parse when it changed.
            text_file_path = image_path.with_suffix('.txt')
            text_file_path_str = str(text_file_path)
            try:
                new_caption_mtime = text_file_path.stat().st_mtime_ns
            except OSError:
                new_caption_mtime = None
            if new_caption_mtime != snapshot_caption_mtime:
                if new_caption_mtime is None:
                    entry['caption'] = (snapshot_caption_mtime, None, [], '')
                else:
                    tags, natural_language_prompt, _ = read_caption_file(
                        text_file_path, self.tag_separator)
                    entry['caption'] = (snapshot_caption_mtime,
                                        new_caption_mtime, tags,
                                        natural_language_prompt)

            if entry:
                updates[image_path] = entry
            scanned_for_completion.append({
                'path': image_path,
                'size': file_size,
                'mtime_ns': new_file_mtime,
                'cap_path': text_file_path_str,
                'cap_mtime_ns': new_caption_mtime,
            })

        # Reconcile the completion store against everything currently on disk so
        # that "complete" flags follow images moved or renamed while the window
        # was in the background — mirroring the full directory scan.
        if completion_store.reconcile(scanned_for_completion):
            completion_store.save()

        # Apply the (possibly re-homed) flags to the newly added images.
        for image in added_images:
            image.is_complete = completion_store.is_complete(image.path)

        # Report "complete" flags for already-loaded images. Reconciliation can
        # only newly complete an existing image (it never clears a flag whose
        # image is still present), so reporting the complete ones is enough;
        # apply_refresh_result diffs these against the model.
        completion_changes: dict = {}
        for image_path in self.snapshot:
            if image_path in removed_paths:
                continue
            if completion_store.is_complete(image_path):
                completion_changes[image_path] = True

        return RefreshResult(self.directory_path, added_images, removed_paths,
                             updates, completion_changes)


class ThumbnailLoader(QObject, QRunnable):
    thumbnail_loaded = Signal(object, QImage, int, object)

    def __init__(self, image_path: Path, image_width: int,
                 file_modified_time_ns: int | None):
        QObject.__init__(self)
        QRunnable.__init__(self)
        self.image_path = image_path
        self.image_width = image_width
        self.file_modified_time_ns = file_modified_time_ns
        self.setAutoDelete(False)

    @Slot()
    def run(self):
        cache_path = get_cache_path(
            self.image_path, self.file_modified_time_ns, self.image_width)
        if cache_path is not None and cache_path.exists():
            cached_image = QImage(str(cache_path))
            if not cached_image.isNull():
                self.thumbnail_loaded.emit(
                    self.image_path, cached_image,
                    self.image_width, self.file_modified_time_ns)
                return
        image_reader = QImageReader(str(self.image_path))
        # Rotate the image based on the orientation tag.
        image_reader.setAutoTransform(True)
        image = image_reader.read()
        if image.isNull():
            thumbnail_image = QImage()
        else:
            thumbnail_image = image.scaledToWidth(
                self.image_width, Qt.TransformationMode.SmoothTransformation)
        if not thumbnail_image.isNull() and cache_path is not None:
            thumbnail_image.save(str(cache_path))
        self.thumbnail_loaded.emit(self.image_path, thumbnail_image,
                                   self.image_width,
                                   self.file_modified_time_ns)


class ImageListModel(QAbstractListModel):
    update_undo_and_redo_actions_requested = Signal()
    directory_loaded = Signal()

    def __init__(self, image_list_image_width: int, tag_separator: str):
        super().__init__()
        self.image_list_image_width = image_list_image_width
        self.tag_separator = tag_separator
        self.tokenizer = None
        self.images: list[Image] = []
        self.undo_stack = deque(maxlen=UNDO_STACK_SIZE)
        self.redo_stack = []
        # True only while restoring tags from the undo/redo history. Consumers
        # (e.g. the Common/Differences panel) use this to distinguish a genuine
        # new edit from a history restore, so an undone/redone tag returns to
        # its original position while a freshly added tag lands at the end.
        self.is_restoring_history = False
        self.proxy_image_list_model = None
        self.image_list_selection_model = None
        self.thumbnail_loaders = set()
        self._current_scanner = None
        self._pending_images: list = []
        # Dedicated pool for all thumbnail work (both on-demand and bulk
        # preloading); capped so thumbnails can't starve the global pool, which
        # is reserved for the interactive image load, the directory scanner, and
        # startup tasks.
        self._preload_pool = QThreadPool()
        self._preload_pool.setMaxThreadCount(4)
        # Bumped whenever the directory or thumbnail size changes so any
        # in-flight background warming knows to stop.
        self._preload_generation = 0
        self._start_cache_eviction()

    def rowCount(self, parent=None) -> int:
        return len(self.images)

    def set_tokenizer(self, tokenizer):
        self.tokenizer = tokenizer

    def _start_cache_eviction(self):
        """Evict over-limit cache entries in a background thread at startup."""
        settings = get_settings()
        max_mb = settings.value(
            'thumbnail_cache_max_size_mb',
            defaultValue=DEFAULT_SETTINGS['thumbnail_cache_max_size_mb'],
            type=int)
        runner = _CacheEvictionRunner(max(1, max_mb) * 1024 * 1024)
        QThreadPool.globalInstance().start(runner)

    def _start_background_warm(self):
        """
        Start (or restart) background caching of thumbnails that aren't on disk
        yet. Runs one worker on the capped preload pool at a lower priority than
        on-demand loads, so scrolling always takes precedence and there's no
        noticeable slowdown. Controlled by the 'background_thumbnail_caching'
        setting.
        """
        settings = get_settings()
        enabled = settings.value(
            'background_thumbnail_caching',
            defaultValue=DEFAULT_SETTINGS['background_thumbnail_caching'],
            type=bool)
        if not enabled or not self.images:
            return
        # Snapshot the paths/mod-times now so the worker never touches the live
        # image list (which the main thread may mutate during a refresh).
        entries = [(image.path, image.file_modified_time_ns)
                   for image in self.images]
        generation = self._preload_generation
        warmer = BackgroundThumbnailWarmer(
            entries, self.image_list_image_width, generation,
            lambda gen: self._preload_generation == gen)
        self._preload_pool.start(warmer, BACKGROUND_THUMBNAIL_PRIORITY)

    def set_image_width(self, image_width: int):
        normalized_width = max(image_width, 16)
        if normalized_width == self.image_list_image_width:
            return
        self._preload_pool.clear()
        self._preload_generation += 1
        self.image_list_image_width = normalized_width
        for image in self.images:
            image.thumbnail = None
            image.thumbnail_loading = False
        row_count = self.rowCount()
        if row_count == 0:
            return
        self.dataChanged.emit(
            self.index(0, 0), self.index(row_count - 1, 0),
            [Qt.ItemDataRole.DecorationRole, Qt.ItemDataRole.SizeHintRole])
        # Thumbnails are keyed by width, so the new size needs its own cache.
        self._start_background_warm()

    def load_thumbnail(self, image: Image):
        if image.thumbnail or image.thumbnail_loading:
            return
        image.thumbnail_loading = True
        thumbnail_loader = ThumbnailLoader(
            image.path, self.image_list_image_width, image.file_modified_time_ns)
        thumbnail_loader.thumbnail_loaded.connect(self.on_thumbnail_loaded)
        self.thumbnail_loaders.add(thumbnail_loader)
        # On-demand loads are for items scrolling into view: run them on the
        # capped thumbnail pool ahead of speculative preloads, and keep them off
        # the global pool so they don't delay the interactive image load.
        self._preload_pool.start(thumbnail_loader,
                                 ON_DEMAND_THUMBNAIL_PRIORITY)

    @Slot(object, QImage, int, object)
    def on_thumbnail_loaded(self, image_path: Path, thumbnail_image: QImage,
                            image_width: int,
                            file_modified_time_ns: int | None):
        thumbnail_loader = self.sender()
        if thumbnail_loader in self.thumbnail_loaders:
            self.thumbnail_loaders.remove(thumbnail_loader)
            thumbnail_loader.deleteLater()
        for row, image in enumerate(self.images):
            if image.path != image_path:
                continue
            if image_width != self.image_list_image_width:
                return
            if file_modified_time_ns != image.file_modified_time_ns:
                return
            image.thumbnail_loading = False
            # Skip if already loaded synchronously by data().
            if image.thumbnail:
                return
            if thumbnail_image.isNull():
                return
            pixmap = QPixmap.fromImage(thumbnail_image)
            image.thumbnail = QIcon(pixmap)
            image_index = self.index(row, 0)
            self.dataChanged.emit(
                image_index, image_index,
                [Qt.ItemDataRole.DecorationRole, Qt.ItemDataRole.SizeHintRole])
            return

    def data(self, index, role=None) -> Image | str | QIcon | QSize:
        image = self.images[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return image
        if role == Qt.ItemDataRole.DisplayRole:
            # The text shown next to the thumbnail in the image list.
            text = image.path.name
            if image.natural_language_prompt:
                text += ' [NL]'
            caption = build_caption_text(image.tags,
                                         image.natural_language_prompt,
                                         self.tag_separator)
            if caption:
                text += f'\n{caption}'
            return text
        if role == Qt.ItemDataRole.ToolTipRole:
            text = image.path.name
            if image.natural_language_prompt:
                text += ' [NL]'
            caption = build_caption_text(image.tags,
                                         image.natural_language_prompt,
                                         self.tag_separator)
            if caption:
                text += f'\n{caption}'
            return text
        if role == Qt.ItemDataRole.DecorationRole:
            # Return a cached thumbnail if already loaded in this session.
            if image.thumbnail:
                return image.thumbnail
            # Check the persistent disk cache — much faster than a full decode.
            cache_path = get_cache_path(
                image.path, image.file_modified_time_ns,
                self.image_list_image_width)
            if cache_path is not None and cache_path.exists():
                cached_image = QImage(str(cache_path))
                if not cached_image.isNull():
                    pixmap = QPixmap.fromImage(cached_image)
                    thumbnail = QIcon(pixmap)
                    image.thumbnail = thumbnail
                    image.thumbnail_loading = False
                    return thumbnail
            # Cache miss — start async loader, don't block main thread.
            if not image.thumbnail_loading:
                self.load_thumbnail(image)
            return None
        if role == Qt.ItemDataRole.SizeHintRole:
            if image.thumbnail:
                return image.thumbnail.availableSizes()[0]
            dimensions = image.dimensions
            if not dimensions:
                return QSize(self.image_list_image_width,
                             self.image_list_image_width)
            width, height = dimensions
            # Scale the dimensions to the image width.
            return QSize(self.image_list_image_width,
                         int(self.image_list_image_width * height / width))

    def _build_sort_key(self, sort_by: str, sort_order: str):
        """Return (key_func, reverse) for sorting a list of Image objects."""
        reverse = (sort_order == SortOrder.DESCENDING)
        file_stat_by_path = {}
        token_count_by_path = {}

        def get_token_count(image: Image):
            if self.tokenizer is None:
                return 0
            token_count = token_count_by_path.get(image.path)
            if token_count is not None:
                return token_count
            caption = build_caption_text(image.tags,
                                         image.natural_language_prompt,
                                         self.tag_separator)
            # Subtract 2 for the `<|startoftext|>` and `<|endoftext|>` tokens.
            token_count = len(self.tokenizer(caption).input_ids) - 2
            token_count_by_path[image.path] = token_count
            return token_count

        def get_file_stat(image: Image):
            file_stat = file_stat_by_path.get(image.path)
            if file_stat is not None:
                return file_stat
            try:
                file_stat = image.path.stat()
            except OSError:
                file_stat = None
            file_stat_by_path[image.path] = file_stat
            return file_stat

        def get_sort_key(image: Image):
            if sort_by == ImageListSortBy.PATH:
                primary_sort = str(image.path).casefold()
            elif sort_by == ImageListSortBy.NAME:
                primary_sort = image.path.name.casefold()
            elif sort_by == ImageListSortBy.MODIFIED_TIME:
                file_stat = get_file_stat(image)
                primary_sort = file_stat.st_mtime if file_stat else 0.0
            elif sort_by == ImageListSortBy.CREATED_TIME:
                file_stat = get_file_stat(image)
                primary_sort = file_stat.st_ctime if file_stat else 0.0
            elif sort_by == ImageListSortBy.FILE_SIZE:
                file_stat = get_file_stat(image)
                primary_sort = file_stat.st_size if file_stat else 0
            elif sort_by == ImageListSortBy.RESOLUTION:
                if image.dimensions:
                    width, height = image.dimensions
                    primary_sort = width * height
                else:
                    primary_sort = 0
            elif sort_by == ImageListSortBy.TAG_COUNT:
                primary_sort = len(image.tags)
            elif sort_by == ImageListSortBy.TOKEN_COUNT:
                primary_sort = get_token_count(image)
            elif sort_by == ImageListSortBy.NATURAL_LANGUAGE_PROMPT_LENGTH:
                primary_sort = len(image.natural_language_prompt)
            else:
                primary_sort = str(image.path).casefold()
            return primary_sort, str(image.path).casefold()

        return get_sort_key, reverse

    def sort_images(self, sort_by: str, sort_order: str):
        if len(self.images) <= 1:
            return
        get_sort_key, reverse = self._build_sort_key(sort_by, sort_order)
        sorted_images = sorted(self.images, key=get_sort_key, reverse=reverse)
        if sorted_images == self.images:
            return
        self.beginResetModel()
        self.images = sorted_images
        self.endResetModel()

    def apply_pending_images(self, sort_by: str, sort_order: str):
        """Sort pending scanned images and commit them in a single model reset."""
        images = self._pending_images
        self._pending_images = []
        if len(images) > 1:
            get_sort_key, reverse = self._build_sort_key(sort_by, sort_order)
            images.sort(key=get_sort_key, reverse=reverse)
        self.beginResetModel()
        self.images = images
        self.endResetModel()
        # Silently cache any thumbnails that have never been generated.
        self._start_background_warm()

    def get_image_suffixes(self) -> list[str]:
        settings = get_settings()
        image_suffixes_string = settings.value(
            'image_list_file_formats',
            defaultValue=DEFAULT_SETTINGS['image_list_file_formats'], type=str)
        image_suffixes = []
        for suffix in image_suffixes_string.split(','):
            suffix = suffix.strip().lower()
            if not suffix.startswith('.'):
                suffix = '.' + suffix
            image_suffixes.append(suffix)
        return image_suffixes

    def read_caption_for_image(self, image_path: Path
                               ) -> tuple[list[str], str, int | None]:
        text_file_path = image_path.with_suffix('.txt')
        return read_caption_file(text_file_path, self.tag_separator)

    def load_directory(self, directory_path: Path):
        # Cancel any pending preload workers from the previous directory.
        self._preload_pool.clear()
        self._preload_generation += 1
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.update_undo_and_redo_actions_requested.emit()
        # Scan in a background thread; the old list stays visible until done.
        image_suffixes = self.get_image_suffixes()
        scanner = DirectoryScanner(directory_path, image_suffixes,
                                   self.tag_separator)
        scanner.scan_complete.connect(
            lambda images, s=scanner: self._on_directory_scanned(images, s))
        self._current_scanner = scanner
        QThreadPool.globalInstance().start(scanner)

    @Slot(list)
    def _on_directory_scanned(self, images: list, scanner=None):
        # Ignore results from a superseded scan: if the user switched
        # directories before this scan finished, a newer scanner is now
        # current and this stale result must not overwrite its images.
        if scanner is not None and scanner is not self._current_scanner:
            return
        # Store images for main_window._on_directory_loaded to sort and commit
        # in a single model reset (avoids the double-reset from sort + load).
        self._pending_images = images
        self.directory_loaded.emit()

    def apply_refresh_result(self, result) -> tuple[bool, set[int]]:
        """
        Apply a diff produced by RefreshScanner on the main thread.

        Only fast in-memory work happens here (no disk I/O). Returns
        (structure_changed, changed_rows) so the caller can re-sort / re-count
        and reload the current image if needed, matching the old synchronous
        refresh behavior. Per-image updates are guarded by the snapshot
        modified-times so a value the user changed while the scan was running is
        never overwritten with stale data.
        """
        structure_changed = False

        # Removals: files that disappeared from the directory.
        if result.removed_paths:
            rows_to_remove = [row for row, image in enumerate(self.images)
                              if image.path in result.removed_paths]
            for row in sorted(rows_to_remove, reverse=True):
                self.beginRemoveRows(QModelIndex(), row, row)
                del self.images[row]
                self.endRemoveRows()
            if rows_to_remove:
                structure_changed = True

        # Additions: files that newly appeared. Skip any that already exist in
        # the model (e.g. if the directory was reloaded meanwhile).
        if result.added_images:
            existing_paths = {image.path for image in self.images}
            new_images = [image for image in result.added_images
                          if image.path not in existing_paths]
            if new_images:
                start_row = len(self.images)
                end_row = start_row + len(new_images) - 1
                self.beginInsertRows(QModelIndex(), start_row, end_row)
                self.images.extend(new_images)
                self.endInsertRows()
                structure_changed = True

        # Updates: images whose file and/or caption changed on disk.
        changed_rows = []
        if result.updates:
            path_to_row = {image.path: row
                           for row, image in enumerate(self.images)}
            for image_path, entry in result.updates.items():
                row = path_to_row.get(image_path)
                if row is None:
                    continue
                image = self.images[row]
                did_change = False
                if 'file' in entry:
                    snapshot_mtime, new_mtime, dimensions = entry['file']
                    # Only apply if the model still matches what the scan saw,
                    # so a change the user made meanwhile is not clobbered.
                    if image.file_modified_time_ns == snapshot_mtime:
                        image.file_modified_time_ns = new_mtime
                        image.dimensions = dimensions
                        image.thumbnail = None
                        image.thumbnail_loading = False
                        did_change = True
                if 'caption' in entry:
                    (snapshot_mtime, new_mtime, tags,
                     natural_language_prompt) = entry['caption']
                    if image.caption_file_modified_time_ns == snapshot_mtime:
                        image.caption_file_modified_time_ns = new_mtime
                        image.tags = tags
                        image.natural_language_prompt = natural_language_prompt
                        did_change = True
                if did_change:
                    changed_rows.append(row)
            for row in changed_rows:
                image_index = self.index(row, 0)
                self.dataChanged.emit(
                    image_index, image_index,
                    [Qt.ItemDataRole.DecorationRole,
                     Qt.ItemDataRole.SizeHintRole,
                     Qt.ItemDataRole.DisplayRole,
                     Qt.ItemDataRole.ToolTipRole,
                     Qt.ItemDataRole.UserRole])
        # Completion flags that changed during reconciliation (e.g. a completed
        # image moved into a subfolder was re-homed onto its new path). Update
        # the affected rows so their "complete" badge refreshes immediately,
        # without waiting for a full reload/restart. The authoritative value is
        # re-read from the store here (on the main thread) so a completion toggle
        # the user made while the background scan was running is never clobbered.
        if result.completion_changes:
            completion_store = get_completion_store()
            path_to_row = {image.path: row
                           for row, image in enumerate(self.images)}
            for image_path in result.completion_changes:
                row = path_to_row.get(image_path)
                if row is None:
                    continue
                image = self.images[row]
                is_complete = completion_store.is_complete(image_path)
                if image.is_complete == is_complete:
                    continue
                image.is_complete = is_complete
                if row not in changed_rows:
                    changed_rows.append(row)
                image_index = self.index(row, 0)
                self.dataChanged.emit(
                    image_index, image_index,
                    [Qt.ItemDataRole.DecorationRole,
                     Qt.ItemDataRole.ToolTipRole,
                     Qt.ItemDataRole.UserRole])

        # Newly added files (and updated ones whose thumbnails were reset) may
        # not be cached yet — warm them silently in the background. Bump the
        # generation first so any warmer still running from the initial load
        # stops and this one restarts from the up-to-date image list.
        if structure_changed or changed_rows:
            self._preload_generation += 1
            self._start_background_warm()
        return structure_changed, set(changed_rows)

    def add_to_undo_stack(self, action_name: str,
                          should_ask_for_confirmation: bool):
        """Add the current state of the image tags to the undo stack."""
        tags = [image.tags.copy() for image in self.images]
        natural_language_prompts = [
            image.natural_language_prompt for image in self.images
        ]
        self.undo_stack.append(HistoryItem(action_name, tags,
                                           natural_language_prompts,
                                           should_ask_for_confirmation))
        self.redo_stack.clear()
        self.update_undo_and_redo_actions_requested.emit()

    def write_image_tags_to_disk(self, image: Image):
        text_file_path = image.path.with_suffix('.txt')
        try:
            text_file_path.write_text(
                build_caption_text(image.tags, image.natural_language_prompt,
                                   self.tag_separator),
                encoding='utf-8', errors='replace')
            image.caption_file_modified_time_ns = text_file_path.stat().st_mtime_ns
            # Keep the completion store's caption hash current for complete
            # images so an external move/rename still confirms as the same
            # finished work. In-memory only; flushed on scan/toggle/close.
            if image.is_complete:
                get_completion_store().refresh_caption(image.path)
        except OSError:
            error_message_box = QMessageBox()
            error_message_box.setWindowTitle('Error')
            error_message_box.setIcon(QMessageBox.Icon.Critical)
            error_message_box.setText(f'Failed to save tags for {image.path}.')
            error_message_box.exec()

    def restore_history_tags(self, is_undo: bool):
        if is_undo:
            source_stack = self.undo_stack
            destination_stack = self.redo_stack
        else:
            # Redo.
            source_stack = self.redo_stack
            destination_stack = self.undo_stack
        if not source_stack:
            return
        history_item = source_stack[-1]
        if history_item.should_ask_for_confirmation:
            undo_or_redo_string = 'Undo' if is_undo else 'Redo'
            reply = get_confirmation_dialog_reply(
                title=undo_or_redo_string,
                question=f'{undo_or_redo_string} '
                         f'"{history_item.action_name}"?')
            if reply != QMessageBox.StandardButton.Yes:
                return
        source_stack.pop()
        tags = [image.tags for image in self.images]
        natural_language_prompts = [
            image.natural_language_prompt for image in self.images
        ]
        destination_stack.append(HistoryItem(
            history_item.action_name, tags, natural_language_prompts,
            history_item.should_ask_for_confirmation))
        changed_image_indices = []
        for image_index, (image, history_image_tags,
                          history_natural_language_prompt) in enumerate(
                zip(self.images, history_item.tags,
                    history_item.natural_language_prompts)):
            if (image.tags == history_image_tags
                    and image.natural_language_prompt
                    == history_natural_language_prompt):
                continue
            changed_image_indices.append(image_index)
            image.tags = history_image_tags
            image.natural_language_prompt = history_natural_language_prompt
            self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.is_restoring_history = True
            try:
                self.dataChanged.emit(self.index(changed_image_indices[0]),
                                      self.index(changed_image_indices[-1]))
            finally:
                self.is_restoring_history = False
        self.update_undo_and_redo_actions_requested.emit()

    @Slot()
    def undo(self):
        """Undo the last action."""
        self.restore_history_tags(is_undo=True)

    @Slot()
    def redo(self):
        """Redo the last undone action."""
        self.restore_history_tags(is_undo=False)

    def is_image_in_scope(self, scope: Scope | str, image_index: int,
                          image: Image) -> bool:
        if scope == Scope.ALL_IMAGES:
            return True
        if scope == Scope.FILTERED_IMAGES:
            return self.proxy_image_list_model.is_image_in_filtered_images(
                image)
        if scope == Scope.SELECTED_IMAGES:
            proxy_index = self.proxy_image_list_model.mapFromSource(
                self.index(image_index))
            return self.image_list_selection_model.isSelected(proxy_index)

    def get_text_match_count(self, text: str, scope: Scope | str,
                             whole_tags_only: bool, use_regex: bool) -> int:
        """Get the number of instances of a text in all captions."""
        match_count = 0
        for image_index, image in enumerate(self.images):
            if not self.is_image_in_scope(scope, image_index, image):
                continue
            if whole_tags_only:
                if use_regex:
                    match_count += len([
                        tag for tag in image.tags
                        if re.fullmatch(pattern=text, string=tag)
                    ])
                else:
                    match_count += image.tags.count(text)
            else:
                caption = self.tag_separator.join(image.tags)
                if use_regex:
                    match_count += len(re.findall(pattern=text,
                                                  string=caption))
                else:
                    match_count += caption.count(text)
        return match_count

    def find_and_replace(self, find_text: str, replace_text: str,
                         scope: Scope | str, use_regex: bool):
        """
        Find and replace arbitrary text in captions, within and across tag
        boundaries.
        """
        if not find_text:
            return
        self.add_to_undo_stack(action_name='Find and Replace',
                               should_ask_for_confirmation=True)
        changed_image_indices = []
        for image_index, image in enumerate(self.images):
            if not self.is_image_in_scope(scope, image_index, image):
                continue
            caption = self.tag_separator.join(image.tags)
            if use_regex:
                if not re.search(pattern=find_text, string=caption):
                    continue
                caption = re.sub(pattern=find_text, repl=replace_text,
                                 string=caption)
            else:
                if find_text not in caption:
                    continue
                caption = caption.replace(find_text, replace_text)
            changed_image_indices.append(image_index)
            image.tags = list(dict.fromkeys(
                t.strip() for t in caption.split(self.tag_separator)
                if t.strip()))
            self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))

    def sort_tags_alphabetically(self, do_not_reorder_first_tag: bool):
        """Sort the tags for each image in alphabetical order."""
        self.add_to_undo_stack(action_name='Sort Tags',
                               should_ask_for_confirmation=True)
        changed_image_indices = []
        for image_index, image in enumerate(self.images):
            if len(image.tags) < 2:
                continue
            old_caption = self.tag_separator.join(image.tags)
            if do_not_reorder_first_tag:
                first_tag = image.tags[0]
                image.tags = [first_tag] + sorted(image.tags[1:])
            else:
                image.tags.sort()
            new_caption = self.tag_separator.join(image.tags)
            if new_caption != old_caption:
                changed_image_indices.append(image_index)
                self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))

    def sort_tags_by_frequency(self, tag_counter: Counter,
                               do_not_reorder_first_tag: bool):
        """
        Sort the tags for each image by the total number of times a tag appears
        across all images.
        """
        self.add_to_undo_stack(action_name='Sort Tags',
                               should_ask_for_confirmation=True)
        changed_image_indices = []
        for image_index, image in enumerate(self.images):
            if len(image.tags) < 2:
                continue
            old_caption = self.tag_separator.join(image.tags)
            if do_not_reorder_first_tag:
                first_tag = image.tags[0]
                image.tags = [first_tag] + sorted(
                    image.tags[1:], key=lambda tag: tag_counter[tag],
                    reverse=True)
            else:
                image.tags.sort(key=lambda tag: tag_counter[tag], reverse=True)
            new_caption = self.tag_separator.join(image.tags)
            if new_caption != old_caption:
                changed_image_indices.append(image_index)
                self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))

    def reverse_tags_order(self, do_not_reorder_first_tag: bool):
        """Reverse the order of the tags for each image."""
        self.add_to_undo_stack(action_name='Reverse Order of Tags',
                               should_ask_for_confirmation=True)
        changed_image_indices = []
        for image_index, image in enumerate(self.images):
            if len(image.tags) < 2:
                continue
            changed_image_indices.append(image_index)
            if do_not_reorder_first_tag:
                image.tags = [image.tags[0]] + list(reversed(image.tags[1:]))
            else:
                image.tags = list(reversed(image.tags))
            self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))

    def shuffle_tags(self, do_not_reorder_first_tag: bool):
        """Shuffle the tags for each image randomly."""
        self.add_to_undo_stack(action_name='Shuffle Tags',
                               should_ask_for_confirmation=True)
        changed_image_indices = []
        for image_index, image in enumerate(self.images):
            if len(image.tags) < 2:
                continue
            changed_image_indices.append(image_index)
            if do_not_reorder_first_tag:
                first_tag, *remaining_tags = image.tags
                random.shuffle(remaining_tags)
                image.tags = [first_tag] + remaining_tags
            else:
                random.shuffle(image.tags)
            self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))

    def move_tags_to_front(self, tags_to_move: list[str]):
        """
        Move one or more tags to the front of the tags list for each image.
        """
        self.add_to_undo_stack(action_name='Move Tags to Front',
                               should_ask_for_confirmation=True)
        changed_image_indices = []
        for image_index, image in enumerate(self.images):
            if not any(tag in image.tags for tag in tags_to_move):
                continue
            old_caption = self.tag_separator.join(image.tags)
            moved_tags = []
            for tag in tags_to_move:
                tag_count = image.tags.count(tag)
                moved_tags.extend([tag] * tag_count)
            unmoved_tags = [tag for tag in image.tags if tag not in moved_tags]
            image.tags = moved_tags + unmoved_tags
            new_caption = self.tag_separator.join(image.tags)
            if new_caption != old_caption:
                changed_image_indices.append(image_index)
                self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))

    def sort_tags_by_category(self, get_category_for_tag,
                              category_order_map: dict[str, int],
                              do_not_reorder_first_tag: bool):
        """
        Move categorized tags to the front using the configured category order,
        while preserving the relative order of tags within each category and for
        uncategorized tags.
        """
        self.add_to_undo_stack(action_name='Sort Tags',
                               should_ask_for_confirmation=True)
        changed_image_indices = []
        uncategorized_sort_index = len(category_order_map)
        for image_index, image in enumerate(self.images):
            if len(image.tags) < 2:
                continue
            old_caption = self.tag_separator.join(image.tags)
            if do_not_reorder_first_tag:
                first_tag = image.tags[0]
                sortable_tags = image.tags[1:]
            else:
                first_tag = None
                sortable_tags = image.tags
            tag_groups = [[] for _ in range(uncategorized_sort_index + 1)]
            for tag in sortable_tags:
                category = get_category_for_tag(tag)
                if category is None:
                    sort_index = uncategorized_sort_index
                else:
                    sort_index = category_order_map.get(
                        category['id'], uncategorized_sort_index)
                tag_groups[sort_index].append(tag)
            sorted_tags = []
            for group in tag_groups:
                sorted_tags.extend(group)
            if first_tag is not None:
                image.tags = [first_tag] + sorted_tags
            else:
                image.tags = sorted_tags
            new_caption = self.tag_separator.join(image.tags)
            if new_caption != old_caption:
                changed_image_indices.append(image_index)
                self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))

    def set_images_complete(self, image_indices: list[QModelIndex],
                            is_complete: bool):
        """Mark the given images as complete or incomplete and persist the
        change to the completion store. Emits dataChanged so the completion
        icon repaints."""
        completion_store = get_completion_store()
        changed_rows = []
        for image_index in image_indices:
            image: Image = self.data(image_index, Qt.ItemDataRole.UserRole)
            if image.is_complete == is_complete:
                continue
            image.is_complete = is_complete
            completion_store.set_complete(image.path, is_complete)
            changed_rows.append(image_index.row())
        if not changed_rows:
            return
        completion_store.save()
        for row in changed_rows:
            index = self.index(row, 0)
            self.dataChanged.emit(index, index,
                                  [Qt.ItemDataRole.DecorationRole,
                                   Qt.ItemDataRole.UserRole])

    def update_image_tags(self, image_index: QModelIndex, tags: list[str]):
        image: Image = self.data(image_index, Qt.ItemDataRole.UserRole)
        if image.tags == tags:
            return
        image.tags = tags
        self.dataChanged.emit(image_index, image_index)
        self.write_image_tags_to_disk(image)

    def update_image_caption(self, image_index: QModelIndex, tags: list[str],
                             natural_language_prompt: str):
        image: Image = self.data(image_index, Qt.ItemDataRole.UserRole)
        tags = list(dict.fromkeys(tags))
        if (image.tags == tags
                and image.natural_language_prompt == natural_language_prompt):
            return
        image.tags = tags
        image.natural_language_prompt = natural_language_prompt
        self.dataChanged.emit(image_index, image_index)
        self.write_image_tags_to_disk(image)

    def update_image_natural_language_prompt(self, image_index: QModelIndex,
                                             natural_language_prompt: str):
        image: Image = self.data(image_index, Qt.ItemDataRole.UserRole)
        if image.natural_language_prompt == natural_language_prompt:
            return
        image.natural_language_prompt = natural_language_prompt
        self.dataChanged.emit(image_index, image_index)
        self.write_image_tags_to_disk(image)

    @Slot(list, list)
    def add_tags(self, tags: list[str], image_indices: list[QModelIndex]):
        """Add one or more tags to one or more images."""
        if not image_indices:
            return
        action_name = f'Add {pluralize("Tag", len(tags))}'
        should_ask_for_confirmation = len(image_indices) > 1
        self.add_to_undo_stack(action_name, should_ask_for_confirmation)
        for image_index in image_indices:
            image: Image = self.data(image_index, Qt.ItemDataRole.UserRole)
            existing = set(image.tags)
            new_tags = [t for t in tags if t not in existing]
            image.tags.extend(new_tags)
            self.write_image_tags_to_disk(image)
        min_image_index = min(image_indices, key=lambda index: index.row())
        max_image_index = max(image_indices, key=lambda index: index.row())
        self.dataChanged.emit(min_image_index, max_image_index)

    @Slot(list, list)
    def remove_tags_from_images(self, tags: list[str],
                                image_indices: list[QModelIndex]):
        """Remove one or more tags from an explicit set of images.

        Unlike ``delete_tags`` (which works by scope across the whole model),
        this targets exactly the given image indices, as needed by the grouped
        multi-image tags view ("remove from all selected" / "remove from the
        current image"). Only images that actually contain one of the tags are
        modified and written to disk.
        """
        if not image_indices or not tags:
            return
        tags_to_remove = set(tags)
        action_name = f'Remove {pluralize("Tag", len(tags))}'
        should_ask_for_confirmation = len(image_indices) > 1
        self.add_to_undo_stack(action_name, should_ask_for_confirmation)
        changed_indices = []
        for image_index in image_indices:
            image: Image = self.data(image_index, Qt.ItemDataRole.UserRole)
            if not any(tag in tags_to_remove for tag in image.tags):
                continue
            image.tags = [tag for tag in image.tags
                          if tag not in tags_to_remove]
            self.write_image_tags_to_disk(image)
            changed_indices.append(image_index)
        if not changed_indices:
            return
        min_image_index = min(changed_indices, key=lambda index: index.row())
        max_image_index = max(changed_indices, key=lambda index: index.row())
        self.dataChanged.emit(min_image_index, max_image_index)

    @Slot(str, str, list)
    def rename_tag_in_images(self, old_tag: str, new_tag: str,
                             image_indices: list[QModelIndex]):
        """Rename a tag on an explicit set of images (grouped view in-place edit).

        Mirrors ``remove_tags_from_images`` but for an in-place rename: only the
        given images that actually contain ``old_tag`` are modified, the new tag
        takes the old tag's position, and duplicates are collapsed. No-op when
        the new tag is empty or unchanged.
        """
        new_tag = new_tag.strip()
        if not image_indices or not old_tag or not new_tag or old_tag == new_tag:
            return
        targets = [index for index in image_indices
                   if old_tag in self.data(index,
                                           Qt.ItemDataRole.UserRole).tags]
        if not targets:
            return
        self.add_to_undo_stack(
            action_name='Rename Tag',
            should_ask_for_confirmation=len(targets) > 1)
        for image_index in targets:
            image: Image = self.data(image_index, Qt.ItemDataRole.UserRole)
            image.tags = list(dict.fromkeys(
                new_tag if tag == old_tag else tag for tag in image.tags))
            self.write_image_tags_to_disk(image)
        min_image_index = min(targets, key=lambda index: index.row())
        max_image_index = max(targets, key=lambda index: index.row())
        self.dataChanged.emit(min_image_index, max_image_index)

    @Slot(list, str)
    def rename_tags(self, old_tags: list[str], new_tag: str,
                    scope: Scope | str = Scope.ALL_IMAGES,
                    use_regex: bool = False):
        self.add_to_undo_stack(
            action_name=f'Rename {pluralize("Tag", len(old_tags))}',
            should_ask_for_confirmation=True)
        changed_image_indices = []
        for image_index, image in enumerate(self.images):
            if not self.is_image_in_scope(scope, image_index, image):
                continue
            if use_regex:
                pattern = old_tags[0]
                if not any(re.fullmatch(pattern=pattern, string=image_tag)
                           for image_tag in image.tags):
                    continue
                image.tags = list(dict.fromkeys(
                    new_tag if re.fullmatch(pattern=pattern, string=image_tag)
                    else image_tag for image_tag in image.tags))
            else:
                if not any(old_tag in image.tags for old_tag in old_tags):
                    continue
                image.tags = list(dict.fromkeys(
                    new_tag if image_tag in old_tags else image_tag
                    for image_tag in image.tags))
            changed_image_indices.append(image_index)
            self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))

    @Slot(list)
    def delete_tags(self, tags: list[str],
                    scope: Scope | str = Scope.ALL_IMAGES,
                    use_regex: bool = False):
        self.add_to_undo_stack(
            action_name=f'Delete {pluralize("Tag", len(tags))}',
            should_ask_for_confirmation=True)
        changed_image_indices = []
        for image_index, image in enumerate(self.images):
            if not self.is_image_in_scope(scope, image_index, image):
                continue
            if use_regex:
                pattern = tags[0]
                if not any(re.fullmatch(pattern=pattern, string=image_tag)
                           for image_tag in image.tags):
                    continue
                image.tags = [image_tag for image_tag in image.tags
                              if not re.fullmatch(pattern=pattern,
                                                  string=image_tag)]
            else:
                if not any(tag in image.tags for tag in tags):
                    continue
                image.tags = [image_tag for image_tag in image.tags
                              if image_tag not in tags]
            changed_image_indices.append(image_index)
            self.write_image_tags_to_disk(image)
        if changed_image_indices:
            self.dataChanged.emit(self.index(changed_image_indices[0]),
                                  self.index(changed_image_indices[-1]))
