"""
"Find Duplicates" review dialog (design: DUPLICATE_DETECTION_PLAN, Phase 1).

Scans the loaded images (all, filtered or selected) for exact and near
duplicates, shows each duplicate group side by side, lets the user pick one
image to keep per group, and acts on the rest non-destructively (delete to
Recycle Bin, move to a folder, or tag them), optionally copying their tags to
the kept image first.
"""

import itertools
from pathlib import Path

from PySide6.QtCore import (QObject, QRunnable, Qt, QThread, QThreadPool,
                            QTimer, Signal, Slot)
from PySide6.QtGui import QImageReader, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox,
                               QComboBox, QDialog,
                               QFileDialog, QFrame, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QMessageBox,
                               QProgressDialog, QPushButton, QRadioButton,
                               QScrollArea, QSlider, QVBoxLayout, QWidget)

from models.image_list_model import ImageListModel, Scope
from utils.duplicate_detection import (DuplicateCache, find_duplicate_groups,
                                       hamming_distance)
from utils.settings import DEFAULT_SETTINGS, get_settings
from utils.settings_widgets import (FocusedScrollSettingsComboBox,
                                    SettingsBigCheckBox)
from utils.utils import get_confirmation_dialog_reply, pluralize
from widgets.image_list import transfer_images, trash_images

# Thumbnail edge length (pixels) for the cards in a duplicate group.
_THUMBNAIL_SIZE = 140
# Rendering caps so a pathological result set (e.g. thousands of near-identical
# training images merged into giant groups) can never freeze the UI or exhaust
# memory. Groups are shown this many at a time ("Show next ... groups"), and
# only this many members per group until the group is expanded.
_MAX_GROUPS_SHOWN = 100
_MAX_MEMBERS_PER_GROUP = 12
# The strictness slider runs from 0 (only visually identical images) up to this
# maximum dHash Hamming distance. The hash is 256-bit, so each step is fine and
# strict: exact resizes / re-compressions differ by only ~0-2 bits, small edits
# by ~8-12, and clearly different images by 50+. A max of 20 keeps the whole
# range comfortably inside "genuinely similar" territory.
_MAX_THRESHOLD = 20
# The tag applied by the "Tag as duplicate" action.
_DUPLICATE_TAG = 'duplicate'

# Rules for picking the image to keep in each group.
KEEP_LARGEST_RESOLUTION = 'Largest resolution'
KEEP_LARGEST_FILE = 'Largest file'
KEEP_NEWEST_FILE = 'Newest file'
KEEP_MOST_TAGS = 'Most tags'
KEEP_RULES = [KEEP_LARGEST_RESOLUTION, KEEP_LARGEST_FILE, KEEP_NEWEST_FILE,
              KEEP_MOST_TAGS]


class _ClickableLabel(QLabel):
    """A QLabel that emits ``clicked`` when pressed with the left mouse button."""

    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


def _format_size(num_bytes) -> str:
    """Return a short human-readable file size such as '1.2 MB'."""
    if num_bytes is None:
        return '? '
    size = float(num_bytes)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            if unit == 'B':
                return f'{int(size)} {unit}'
            return f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} GB'


class DuplicateScanner(QObject, QRunnable):
    """Runs the (I/O heavy) duplicate scan off the UI thread."""

    # (stage, done, total); stage is 'hashing' or 'comparing'.
    progress = Signal(str, int, int)
    finished = Signal(list)

    def __init__(self, images, threshold: int, cache: DuplicateCache):
        QObject.__init__(self)
        QRunnable.__init__(self)
        self.images = images
        self.threshold = threshold
        self.cache = cache
        self._cancelled = False
        self.setAutoDelete(False)

    def cancel(self):
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @Slot()
    def run(self):
        groups = find_duplicate_groups(
            self.images, self.threshold, cache=self.cache,
            progress_callback=self.progress.emit,
            should_cancel=lambda: self._cancelled)
        # Drop the image list so a finished scanner holds no large data.
        self.images = None
        self.finished.emit(groups)


class _ScannerKeeper(QObject):
    """Keeps running scanners alive independently of the dialog, so closing
    the dialog mid-scan neither crashes the worker thread nor keeps the closed
    dialog in memory. A scanner is released once it has finished."""

    def __init__(self):
        super().__init__()
        self.running: set[DuplicateScanner] = set()

    def start(self, scanner: DuplicateScanner):
        self.running.add(scanner)
        scanner.finished.connect(self._release)
        QThreadPool.globalInstance().start(scanner)

    @Slot()
    def _release(self):
        scanner = self.sender()
        # Let the worker thread return from `run` before the last reference
        # to the scanner goes away.
        QTimer.singleShot(1000, lambda: self.running.discard(scanner))


_scanner_keeper: _ScannerKeeper | None = None


def _get_scanner_keeper() -> _ScannerKeeper:
    global _scanner_keeper
    if _scanner_keeper is None:
        _scanner_keeper = _ScannerKeeper()
    return _scanner_keeper


# --- Background thumbnail loading ------------------------------------------

class _ThumbnailEmitter(QObject):
    # (batch token, image path, QImage or None)
    loaded = Signal(int, str, object)


class _ThumbnailBatch:
    """Thumbnails requested for one set of results. Cancelling it makes
    workers that have not finished yet skip their work."""

    _tokens = itertools.count(1)

    def __init__(self):
        self.token = next(self._tokens)
        self.cancelled = False


_thumbnail_emitter: _ThumbnailEmitter | None = None
_thumbnail_pool: QThreadPool | None = None


def _get_thumbnail_emitter() -> _ThumbnailEmitter:
    global _thumbnail_emitter
    if _thumbnail_emitter is None:
        _thumbnail_emitter = _ThumbnailEmitter()
    return _thumbnail_emitter


def _get_thumbnail_pool() -> QThreadPool:
    global _thumbnail_pool
    if _thumbnail_pool is None:
        _thumbnail_pool = QThreadPool()
        # Every thumbnail is decoded at full resolution, so limit how many
        # large images are held in memory at once.
        _thumbnail_pool.setMaxThreadCount(
            max(1, min(4, QThread.idealThreadCount())))
    return _thumbnail_pool


def _load_thumbnail_image(path, device_pixel_ratio: float = 1.0):
    """Decode the full image and scale it down with smooth filtering.

    Decoding straight to a small size (QImageReader.setScaledSize) is faster,
    but the result is visibly less accurate, which matters when comparing
    near-identical images side by side.
    """
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        return None
    edge = round(_THUMBNAIL_SIZE * device_pixel_ratio)
    if image.width() > edge or image.height() > edge:
        image = image.scaled(edge, edge, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    image.setDevicePixelRatio(device_pixel_ratio)
    return image


class _ThumbnailLoader(QRunnable):
    def __init__(self, batch: _ThumbnailBatch, path: Path,
                 device_pixel_ratio: float):
        super().__init__()
        self.batch = batch
        self.path = path
        self.device_pixel_ratio = device_pixel_ratio

    def run(self):
        if self.batch.cancelled:
            return
        image = _load_thumbnail_image(self.path, self.device_pixel_ratio)
        if self.batch.cancelled:
            return
        _get_thumbnail_emitter().loaded.emit(self.batch.token,
                                             str(self.path), image)


# --- Keeper rules -----------------------------------------------------------

def _resolution_key(member) -> tuple:
    width, height = (member.dimensions or (0, 0))
    return ((width or 0) * (height or 0), member.size or 0)


def choose_keeper(members: list, rule: str, tag_counts: dict):
    """Return the member to keep under `rule`. `tag_counts` maps an image
    path to its number of tags. Ties fall back to the largest resolution and
    then to the earliest member."""
    def key(member):
        resolution = _resolution_key(member)
        if rule == KEEP_LARGEST_FILE:
            return (member.size or 0,) + resolution
        if rule == KEEP_NEWEST_FILE:
            return (member.mtime_ns or 0,) + resolution
        if rule == KEEP_MOST_TAGS:
            return (tag_counts.get(member.path, 0),) + resolution
        return resolution

    best = members[0]
    best_key = key(best)
    for member in members[1:]:
        member_key = key(member)
        if member_key > best_key:
            best, best_key = member, member_key
    return best


def describe_relation(member, keeper) -> tuple[str, str]:
    """Return a short label (and tooltip) describing how `member` compares
    with the kept image."""
    if member is keeper:
        return 'Kept', 'This is the image that will be kept.'
    if member.sha256 is not None and member.sha256 == keeper.sha256:
        return ('Identical file',
                'Byte-for-byte the same file as the kept image.')
    if member.dhash is None or keeper.dhash is None:
        return '', ''
    distance = hamming_distance(member.dhash, keeper.dhash)
    tooltip = ('How many of the 256 bits of the visual fingerprint differ '
               'from the kept image. 0 means it looks identical; higher '
               'numbers mean bigger visual differences.')
    if distance == 0:
        return 'Looks identical', tooltip
    return f'Differs by {distance}', tooltip


class FindDuplicatesDialog(QDialog):
    def __init__(self, parent, image_list_model: ImageListModel,
                 reload_callback):
        super().__init__(parent)
        # The dialog is opened afresh every time, so free it when it closes.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.image_list_model = image_list_model
        self.reload_callback = reload_callback
        self.settings = get_settings()
        self._cache = DuplicateCache()
        self._cache.load()
        self._scanner: DuplicateScanner | None = None
        self._progress_dialog: QProgressDialog | None = None
        self._scanned_count = 0
        self._groups: list = []
        self._group_selections: list[_GroupCards] = []
        # Image path -> number of tags, for the groups currently shown.
        self._tag_counts: dict[Path, int] = {}
        # Thumbnail state for the current results.
        self._thumbnail_batch = _ThumbnailBatch()
        self._thumbnails: dict[str, QPixmap | None] = {}
        self._pending_thumbnail_labels: dict[str, list[QLabel]] = {}
        _get_thumbnail_emitter().loaded.connect(self._on_thumbnail_loaded)

        self.setWindowTitle('Find Duplicates')
        self.resize(940, 700)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        description = QLabel(
            'Finds identical files and near duplicates (resizes, re-saves or '
            'minor edits). In each group, choose the image to keep, then act '
            'on the others. Deleted images go to the Recycle Bin.')
        description.setWordWrap(True)
        layout.addWidget(description)

        # --- Scan controls --------------------------------------------------
        scan_row = QHBoxLayout()
        scan_row.addWidget(QLabel('Scope:'))
        self.scope_combo_box = FocusedScrollSettingsComboBox(
            key='duplicate_scan_scope', default=Scope.ALL_IMAGES.value)
        self.scope_combo_box.addItems(list(Scope))
        if self.scope_combo_box.currentText() not in list(Scope):
            self.scope_combo_box.setCurrentText(Scope.ALL_IMAGES.value)
        self.scope_combo_box.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.scope_combo_box.currentTextChanged.connect(
            self._update_scope_count_label)
        scan_row.addWidget(self.scope_combo_box)
        self.scope_count_label = QLabel()
        scan_row.addWidget(self.scope_count_label)
        scan_row.addSpacing(16)
        scan_row.addWidget(QLabel('Strictness:'))
        self.strictness_slider = QSlider(Qt.Orientation.Horizontal)
        self.strictness_slider.setMinimum(0)
        self.strictness_slider.setMaximum(_MAX_THRESHOLD)
        self.strictness_slider.setValue(self._initial_threshold())
        self.strictness_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.strictness_slider.setTickInterval(2)
        self.strictness_slider.valueChanged.connect(self._update_strictness_label)
        scan_row.addWidget(self.strictness_slider, 1)
        self.strictness_label = QLabel()
        scan_row.addWidget(self.strictness_label)
        self.scan_button = QPushButton('Scan')
        self.scan_button.clicked.connect(self.start_scan)
        scan_row.addWidget(self.scan_button)
        layout.addLayout(scan_row)
        self._update_scope_count_label()
        self._update_strictness_label(self.strictness_slider.value())

        # --- Results area ---------------------------------------------------
        self.results_scroll_area = QScrollArea()
        self.results_scroll_area.setWidgetResizable(True)
        self.results_container = QWidget()
        self.results_layout = QVBoxLayout(self.results_container)
        self.results_layout.setContentsMargins(0, 0, 0, 0)
        self.results_layout.setSpacing(12)
        self.results_layout.addStretch()
        self.results_scroll_area.setWidget(self.results_container)
        layout.addWidget(self.results_scroll_area, 1)

        status_row = QHBoxLayout()
        self.status_label = QLabel('Click "Scan" to look for duplicates.')
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.status_label, 1)
        self.show_more_button = QPushButton()
        self.show_more_button.clicked.connect(self.show_more_groups)
        self.show_more_button.hide()
        status_row.addWidget(self.show_more_button)
        layout.addLayout(status_row)

        # --- Keeper + actions -----------------------------------------------
        actions_box = QGroupBox('Act on the results')
        actions_layout = QGridLayout(actions_box)
        actions_layout.setColumnStretch(4, 1)

        actions_layout.addWidget(QLabel('Keep in each group:'), 0, 0)
        self.keep_rule_combo_box = FocusedScrollSettingsComboBox(
            key='duplicate_keep_rule', default=KEEP_LARGEST_RESOLUTION)
        self.keep_rule_combo_box.addItems(KEEP_RULES)
        if self.keep_rule_combo_box.currentText() not in KEEP_RULES:
            self.keep_rule_combo_box.setCurrentText(KEEP_LARGEST_RESOLUTION)
        self.keep_rule_combo_box.setToolTip(
            'Which image is picked as the keeper when results appear, or when '
            'you click "Apply to all groups". You can still pick a different '
            'image in any group.')
        actions_layout.addWidget(self.keep_rule_combo_box, 0, 1)
        self.apply_rule_button = QPushButton('Apply to all groups')
        self.apply_rule_button.setToolTip(
            'Pick the keeper in every group (except ignored ones) using the '
            'rule on the left. This replaces any keepers you picked by hand.')
        self.apply_rule_button.clicked.connect(self.apply_keep_rule)
        actions_layout.addWidget(self.apply_rule_button, 0, 2)

        self.merge_tags_check_box = SettingsBigCheckBox(
            key='duplicate_merge_tags', default=False,
            text='Copy the tags of the other images to the kept image')
        self.merge_tags_check_box.setToolTip(
            'Before deleting, moving or tagging the other images, add any of '
            'their tags that the kept image does not have yet to the end of '
            'its tag list.')
        actions_layout.addWidget(self.merge_tags_check_box, 1, 0, 1, 5)

        actions_layout.addWidget(QLabel('Other images:'), 2, 0)
        self.delete_button = QPushButton('Delete to Recycle Bin')
        self.delete_button.clicked.connect(self.delete_others)
        actions_layout.addWidget(self.delete_button, 2, 1)
        self.move_button = QPushButton('Move to folder...')
        self.move_button.clicked.connect(self.move_others)
        actions_layout.addWidget(self.move_button, 2, 2)
        self.tag_button = QPushButton(f'Tag as "{_DUPLICATE_TAG}"')
        self.tag_button.clicked.connect(self.tag_others)
        actions_layout.addWidget(self.tag_button, 2, 3)
        layout.addWidget(actions_box)

        self._set_actions_enabled(False)

    # ------------------------------------------------------------------
    # Scope / strictness helpers
    # ------------------------------------------------------------------

    @property
    def scope(self) -> str:
        return self.scope_combo_box.currentText()

    def _images_in_scope(self) -> list:
        model = self.image_list_model
        return [image for index, image in enumerate(model.images)
                if model.is_image_in_scope(self.scope, index, image)]

    @Slot()
    def _update_scope_count_label(self):
        count = len(self._images_in_scope())
        self.scope_count_label.setText(
            f'({count} {pluralize("image", count)})')

    def _initial_threshold(self) -> int:
        value = self.settings.value(
            'duplicate_detection_strictness',
            defaultValue=DEFAULT_SETTINGS['duplicate_detection_strictness'],
            type=int)
        return max(0, min(_MAX_THRESHOLD, value))

    @Slot(int)
    def _update_strictness_label(self, value: int):
        if value == 0:
            text = 'Exact / identical only'
        elif value <= 2:
            text = f'Very strict \u2014 near-identical (\u2264 {value})'
        elif value <= 6:
            text = f'Strict (\u2264 {value})'
        elif value <= 12:
            text = f'Medium (\u2264 {value})'
        else:
            text = f'Loose (\u2264 {value})'
        self.strictness_label.setText(text)

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    @Slot()
    def start_scan(self):
        images = self._images_in_scope()
        self._update_scope_count_label()
        if len(images) < 2:
            if not self.image_list_model.images:
                text = 'No images are loaded. Load a directory first.'
            else:
                text = (f'The scope "{self.scope}" contains '
                        f'{len(images)} {pluralize("image", len(images))}. '
                        f'At least 2 are needed to look for duplicates.')
            self.status_label.setText(text)
            return
        threshold = self.strictness_slider.value()
        self.settings.setValue('duplicate_detection_strictness', threshold)
        self._clear_results()
        self._set_actions_enabled(False)
        self.scan_button.setEnabled(False)
        self._scanned_count = len(images)

        progress = QProgressDialog('Scanning images for duplicates...',
                                   'Cancel', 0, len(images), self)
        progress.setWindowTitle('Find Duplicates')
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        # Don't let Qt auto-hide the dialog the instant the bar reaches its
        # maximum: it is switched to the comparing stage and then to a
        # "Preparing groups" busy bar, and hidden with reset() at the end.
        progress.setAutoReset(False)
        progress.setValue(0)
        self._progress_dialog = progress

        scanner = DuplicateScanner(images, threshold, self._cache)
        self._scanner = scanner
        # Bound methods (not lambdas) so the connections end when the dialog
        # is deleted.
        scanner.progress.connect(self._on_scan_progress)
        scanner.finished.connect(self._on_scan_finished)
        progress.canceled.connect(scanner.cancel)
        _get_scanner_keeper().start(scanner)

    @Slot(str, int, int)
    def _on_scan_progress(self, stage: str, done: int, total: int):
        progress = self._progress_dialog
        if progress is None:
            return
        if stage == 'comparing':
            if progress.maximum() != total or not progress.labelText(
                    ).startswith('Comparing'):
                progress.setLabelText('Comparing images\u2026')
                progress.setRange(0, max(1, total))
        progress.setValue(done)

    @Slot(list)
    def _on_scan_finished(self, groups: list):
        scanner = self._scanner
        progress = self._progress_dialog
        self._scanner = None
        self._progress_dialog = None
        # Read the real cancellation state from the scanner. We must NOT rely
        # on progress.wasCanceled() here, because closing/resetting a
        # QProgressDialog itself counts as a cancel.
        if scanner is None or scanner.cancelled:
            if progress is not None:
                progress.reset()
            self.scan_button.setEnabled(True)
            self.status_label.setText('Scan cancelled.')
            return
        # Building the result cards can take a moment on large sets, so show
        # an indeterminate busy bar (which can't be cancelled) meanwhile.
        if progress is not None:
            progress.setLabelText('Preparing duplicate groups\u2026')
            progress.setCancelButton(None)
            progress.setRange(0, 0)
            progress.show()
            progress.raise_()
        QApplication.processEvents()
        self._groups = groups
        self._show_groups()
        if progress is not None:
            progress.reset()
        self.scan_button.setEnabled(True)

    def done(self, result: int):
        # Stop a running scan and any queued thumbnail loads when the dialog
        # closes (Close button, Escape or the window's X).
        if self._scanner is not None:
            self._scanner.cancel()
            self._scanner = None
        self._cancel_thumbnails()
        super().done(result)

    def _cancel_thumbnails(self):
        self._thumbnail_batch.cancelled = True
        _get_thumbnail_pool().clear()
        self._thumbnail_batch = _ThumbnailBatch()
        self._thumbnails.clear()
        self._pending_thumbnail_labels.clear()

    def _clear_results(self):
        self._cancel_thumbnails()
        self._group_selections.clear()
        self._groups = []
        self._tag_counts = {}
        self.show_more_button.hide()
        while self.results_layout.count():
            item = self.results_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.results_layout.addStretch()

    def _image_lookup(self) -> dict:
        """Map each loaded image's path to its (model row, Image)."""
        return {image.path: (row, image)
                for row, image in enumerate(self.image_list_model.images)}

    def _show_groups(self):
        groups = self._groups
        if not groups:
            self._clear_results()
            self.status_label.setText(
                f'No duplicates found among {self._scanned_count} '
                f'{pluralize("image", self._scanned_count)} at this '
                f'strictness. Try moving the slider toward "Loose".')
            self._set_actions_enabled(False)
            return
        self._tag_counts = {path: len(image.tags) for path, (_row, image)
                            in self._image_lookup().items()}
        self._add_group_cards(groups[:_MAX_GROUPS_SHOWN])
        self._set_actions_enabled(True)
        self._update_status()

    def _add_group_cards(self, groups: list):
        rule = self.keep_rule_combo_box.currentText()
        # Yield to the event loop now and then so the window stays responsive
        # while many cards are built.
        cards_built = 0
        for group in groups:
            keeper = choose_keeper(group.members, rule, self._tag_counts)
            group_cards = _GroupCards(self, group, keeper)
            self._group_selections.append(group_cards)
            self.results_layout.insertWidget(
                self.results_layout.count() - 1, group_cards.box)
            cards_built += group_cards.shown_count
            if cards_built >= 40:
                cards_built = 0
                QApplication.processEvents()

    @Slot()
    def show_more_groups(self):
        start = len(self._group_selections)
        self._add_group_cards(self._groups[start:start + _MAX_GROUPS_SHOWN])
        self._update_status()

    def _update_status(self):
        groups = self._groups
        shown = len(self._group_selections)
        duplicate_total = sum(len(group.members) for group in groups)
        message = (
            f'Found {len(groups)} duplicate '
            f'{pluralize("group", len(groups))} ({duplicate_total} images) '
            f'among {self._scanned_count} scanned. '
            f'Click a thumbnail to enlarge it.')
        notes = []
        remaining = len(groups) - shown
        if remaining > 0:
            notes.append(f'{shown} groups are shown so far')
            next_count = min(_MAX_GROUPS_SHOWN, remaining)
            self.show_more_button.setText(
                f'Show next {next_count} {pluralize("group", next_count)}')
            self.show_more_button.show()
        else:
            self.show_more_button.hide()
        if max(len(group.members) for group in groups) \
                > _MAX_MEMBERS_PER_GROUP:
            notes.append(
                'some groups are very large, which usually means the '
                'strictness is too loose \u2014 try dragging the slider left')
        if notes:
            message += ' Note: ' + '; '.join(notes) + '.'
        self.status_label.setText(message)

    def _card_info_text(self, member) -> str:
        if member.dimensions:
            width, height = member.dimensions
            dimensions_text = f'{width}\u00d7{height}'
        else:
            dimensions_text = 'unknown size'
        tag_count = self._tag_counts.get(member.path)
        tags_text = ('? tags' if tag_count is None
                     else f'{tag_count} {pluralize("tag", tag_count)}')
        return (f'{_elide(member.path.name)}\n'
                f'{dimensions_text}  \u2022  {_format_size(member.size)}\n'
                f'{tags_text}')

    def _build_card(self, member, group_cards) -> tuple:
        """Return (card, keep radio, relation label, info label)."""
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(8, 8, 8, 8)
        card_layout.setSpacing(4)

        thumbnail_label = _ClickableLabel()
        thumbnail_label.setFixedSize(_THUMBNAIL_SIZE, _THUMBNAIL_SIZE)
        thumbnail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumbnail_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._request_thumbnail(member.path, thumbnail_label)
        thumbnail_label.clicked.connect(
            lambda m=member, gc=group_cards: self._show_preview(gc, m))
        card_layout.addWidget(thumbnail_label)

        info_label = QLabel(self._card_info_text(member))
        info_label.setToolTip(str(member.path))
        card_layout.addWidget(info_label)

        # A space keeps the line's height before the real text is set.
        relation_label = QLabel(' ')
        card_layout.addWidget(relation_label)

        radio = QRadioButton('Keep')
        card_layout.addWidget(radio)
        return card, radio, relation_label, info_label

    # ------------------------------------------------------------------
    # Thumbnails
    # ------------------------------------------------------------------

    def _request_thumbnail(self, path: Path, label: QLabel):
        key = str(path)
        if key in self._thumbnails:
            _set_thumbnail(label, self._thumbnails[key])
            return
        label.setText('Loading\u2026')
        pending = self._pending_thumbnail_labels.setdefault(key, [])
        pending.append(label)
        if len(pending) == 1:
            _get_thumbnail_pool().start(_ThumbnailLoader(
                self._thumbnail_batch, path, self.devicePixelRatioF()))

    @Slot(int, str, object)
    def _on_thumbnail_loaded(self, token: int, path_text: str, image):
        if token != self._thumbnail_batch.token:
            return
        pixmap = QPixmap.fromImage(image) if image is not None else None
        self._thumbnails[path_text] = pixmap
        for label in self._pending_thumbnail_labels.pop(path_text, []):
            try:
                _set_thumbnail(label, pixmap)
            except RuntimeError:
                # The card was rebuilt (group expanded) before it loaded.
                pass

    def _show_preview(self, group_cards, member):
        """Open a large preview that can cycle through the group's images
        and let the user pick which one to keep."""
        if group_cards is not None and group_cards.radio_members:
            radio_members = list(group_cards.radio_members)
        else:
            # Fallback: a standalone image with no group/keep context.
            radio_members = [(None, member)]

        index = 0
        for position, (_radio, current) in enumerate(radio_members):
            if current is member:
                index = position
                break

        dialog = _PreviewDialog(self, radio_members, index)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.exec()

    # ------------------------------------------------------------------
    # Selection helpers
    # ------------------------------------------------------------------

    @Slot()
    def apply_keep_rule(self):
        rule = self.keep_rule_combo_box.currentText()
        for group_cards in self._group_selections:
            if group_cards.ignored:
                continue
            group_cards.set_keep(choose_keeper(group_cards.group.members,
                                               rule, self._tag_counts))

    def _keeper_targets(self) -> list[tuple]:
        """Return (kept member, [other members]) for every group that is not
        ignored. Includes members hidden behind a group's "+N more" tile."""
        pairs = []
        for group_cards in self._group_selections:
            if group_cards.ignored:
                continue
            keeper = group_cards.keep_member
            others = [member for member in group_cards.group.members
                      if member is not keeper]
            if others:
                pairs.append((keeper, others))
        return pairs

    def _set_actions_enabled(self, enabled: bool):
        for widget in (self.delete_button, self.move_button, self.tag_button,
                       self.apply_rule_button):
            widget.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _merge_tags_by_row(self, pairs, lookup, acted_on=None) -> dict:
        """Return {model row: tags} adding the other images' tags (that the
        keeper lacks) to each keeper. Only others in `acted_on` (a set of
        paths) count, if given."""
        tags_by_row = {}
        for keeper, others in pairs:
            keeper_entry = lookup.get(keeper.path)
            if keeper_entry is None:
                continue
            tags = []
            for member in others:
                if acted_on is not None and member.path not in acted_on:
                    continue
                entry = lookup.get(member.path)
                if entry is not None:
                    tags.extend(entry[1].tags)
            if tags:
                tags_by_row.setdefault(keeper_entry[0], []).extend(tags)
        return tags_by_row

    def _add_tags_by_row(self, tags_by_row: dict, action_name: str):
        if not tags_by_row:
            return
        model = self.image_list_model
        model.add_tags_per_image(
            [(model.index(row, 0), tags)
             for row, tags in sorted(tags_by_row.items())], action_name)

    @Slot()
    def delete_others(self):
        pairs = self._keeper_targets()
        targets = [member for _keeper, others in pairs for member in others]
        if not targets:
            self._warn_no_targets()
            return
        count = len(targets)
        merge = self.merge_tags_check_box.isChecked()
        question = (f'Move {count} duplicate {pluralize("image", count)} and '
                    f'{"its" if count == 1 else "their"} '
                    f'{pluralize("caption", count)} to the Recycle Bin?')
        if merge:
            question += ('\n\nTheir tags will be copied to the kept '
                         'images first.')
        reply = get_confirmation_dialog_reply('Delete Duplicates', question)
        if reply != QMessageBox.StandardButton.Yes:
            return
        lookup = self._image_lookup()
        deleted = trash_images(self, [member.path for member in targets],
                               list(lookup))
        if merge and deleted:
            self._add_tags_by_row(
                self._merge_tags_by_row(pairs, lookup, set(deleted)),
                'Merge Duplicate Tags')
        if not deleted:
            self.status_label.setText('Nothing was deleted.')
            return
        self._finish_destructive_action(
            f'Moved {len(deleted)} {pluralize("image", len(deleted))} to the '
            f'Recycle Bin.')

    @Slot()
    def move_others(self):
        pairs = self._keeper_targets()
        targets = [member for _keeper, others in pairs for member in others]
        if not targets:
            self._warn_no_targets()
            return
        destination = QFileDialog.getExistingDirectory(
            self, 'Select a folder to move the duplicates into',
            self.settings.value('directory_path', type=str) or '')
        if not destination:
            return
        lookup = self._image_lookup()
        transferred = transfer_images(
            self, [member.path for member in targets], Path(destination),
            is_move=True, all_image_paths=list(lookup),
            description='duplicate')
        moved = {source for source, _target in transferred}
        if self.merge_tags_check_box.isChecked() and moved:
            self._add_tags_by_row(
                self._merge_tags_by_row(pairs, lookup, moved),
                'Merge Duplicate Tags')
        if not moved:
            self.status_label.setText('Nothing was moved.')
            return
        self._finish_destructive_action(
            f'Moved {len(moved)} {pluralize("image", len(moved))} to '
            f'{destination}.')

    @Slot()
    def tag_others(self):
        pairs = self._keeper_targets()
        if not pairs:
            self._warn_no_targets()
            return
        lookup = self._image_lookup()
        tags_by_row = (self._merge_tags_by_row(pairs, lookup)
                       if self.merge_tags_check_box.isChecked() else {})
        tagged_count = 0
        for _keeper, others in pairs:
            for member in others:
                entry = lookup.get(member.path)
                if entry is not None:
                    tags_by_row.setdefault(entry[0], []).append(
                        _DUPLICATE_TAG)
                    tagged_count += 1
        if not tags_by_row:
            self.status_label.setText(
                'None of these images are loaded any more. Scan again.')
            return
        self._add_tags_by_row(tags_by_row, 'Tag Duplicates')
        self._tag_counts = {path: len(image.tags)
                            for path, (_row, image) in lookup.items()}
        for group_cards in self._group_selections:
            group_cards.refresh_info()
        self.status_label.setText(
            f'Tagged {tagged_count} duplicate '
            f'{pluralize("image", tagged_count)} as "{_DUPLICATE_TAG}". '
            f'Nothing was deleted.')

    def _finish_destructive_action(self, summary: str):
        # Files on disk changed. Refresh the main image list (this runs an
        # asynchronous re-scan of the directory), then clear our results and
        # ask the user to scan again. We deliberately don't auto-rescan here
        # because the model's image list is repopulated in the background and
        # may still be stale at this exact moment.
        self._clear_results()
        self._set_actions_enabled(False)
        if callable(self.reload_callback):
            self.reload_callback()
        self.status_label.setText(
            f'{summary} The directory is reloading \u2014 click "Scan" again '
            f'to check for any remaining duplicates.')

    def _warn_no_targets(self):
        QMessageBox.information(
            self, 'Find Duplicates',
            'There is nothing to act on: every group is ticked "Ignore". '
            'Un-ignore a group first.')


def _set_thumbnail(label: QLabel, pixmap):
    if pixmap is None:
        label.setText('(no preview)')
    else:
        label.setPixmap(pixmap)


def _elide(text: str, limit: int = 22) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 1] + '\u2026'


class _PreviewDialog(QDialog):
    """Large image preview that can page through every image in a duplicate
    group (Previous / Next), mark which one to keep, and scales each image to
    fit the window without ever enlarging it beyond its original resolution.

    ``radio_members`` is a list of ``(QRadioButton | None, member)`` tuples
    shared with the main dialog, so pressing "Keep this image" here checks the
    matching radio back in the results list.
    """

    def __init__(self, parent, radio_members, index):
        super().__init__(parent)
        self.radio_members = radio_members
        self.index = index
        self._pixmap = None

        layout = QVBoxLayout(self)

        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.image_label)
        layout.addWidget(self.scroll, 1)

        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        button_row = QHBoxLayout()
        self.prev_button = QPushButton('\u2190 Previous')
        self.next_button = QPushButton('Next \u2192')
        self.keep_button = QPushButton('Keep this image')
        self.close_button = QPushButton('Close')
        self.prev_button.clicked.connect(lambda: self._step(-1))
        self.next_button.clicked.connect(lambda: self._step(1))
        self.keep_button.clicked.connect(self._keep_current)
        self.close_button.clicked.connect(self.accept)
        button_row.addWidget(self.prev_button)
        button_row.addWidget(self.next_button)
        button_row.addStretch()
        button_row.addWidget(self.keep_button)
        button_row.addWidget(self.close_button)
        layout.addLayout(button_row)

        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry()
        self.resize(int(available.width() * 0.85),
                    int(available.height() * 0.85))

        # Left/Right arrow keys page through the group. QShortcut works even
        # when a button has keyboard focus (a plain keyPressEvent would not).
        previous_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Left), self)
        previous_shortcut.activated.connect(lambda: self._step(-1))
        next_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Right), self)
        next_shortcut.activated.connect(lambda: self._step(1))

        self._load_current()

    def _load_current(self):
        _radio, member = self.radio_members[self.index]
        self._pixmap = QPixmap(str(member.path))

        total = len(self.radio_members)
        self.setWindowTitle(
            f'{member.path.name}  \u2014  {self.index + 1} of {total}')
        multiple = total > 1
        self.prev_button.setEnabled(multiple)
        self.next_button.setEnabled(multiple)

        if self._pixmap.isNull():
            original_text = ''
        else:
            original_text = (f'{self._pixmap.width()}\u00d7'
                             f'{self._pixmap.height()} (original)')
        try:
            size_text = _format_size(member.path.stat().st_size)
        except OSError:
            size_text = ''
        parts = [part for part in (original_text, size_text) if part]
        self.info_label.setText(
            str(member.path)
            + (('\n' + '  \u2022  '.join(parts)) if parts else ''))

        self._update_keep_button()
        self._render()
        self._focus_primary_button()

    def _focus_primary_button(self):
        """Give keyboard focus (and Enter-key default) to the most useful
        button for the current image: "Keep this image" when it can still be
        kept, otherwise "Next" so Enter keeps paging through the group, and
        "Close" as a last resort. Navigation is also always available via the
        Left/Right arrow keys, so the default button is reserved for the
        primary decision rather than paging."""
        if self.keep_button.isEnabled():
            target = self.keep_button
        elif self.next_button.isEnabled():
            target = self.next_button
        else:
            target = self.close_button
        # Every button keeps ``autoDefault`` on so that tabbing focus to another
        # button makes *it* the Enter-key target; only the primary target is the
        # initial default and gets focus.
        for button in (self.prev_button, self.next_button,
                       self.keep_button, self.close_button):
            button.setAutoDefault(True)
            button.setDefault(button is target)
        target.setFocus()

    def _update_keep_button(self):
        radio, _member = self.radio_members[self.index]
        already_kept = radio is not None and radio.isChecked()
        if radio is None:
            self.keep_button.setText('Keep this image')
            self.keep_button.setEnabled(False)
        elif already_kept:
            self.keep_button.setText('\u2713 Keeping this image')
            self.keep_button.setEnabled(False)
        else:
            self.keep_button.setText('Keep this image')
            self.keep_button.setEnabled(True)

    def _render(self):
        """Scale to fit the viewport, but never upscale past original size."""
        if self._pixmap is None or self._pixmap.isNull():
            self.image_label.setText('(cannot load this image)')
            return
        viewport = self.scroll.viewport().size()
        available_width = max(1, viewport.width())
        available_height = max(1, viewport.height())
        pixmap = self._pixmap
        if (pixmap.width() > available_width
                or pixmap.height() > available_height):
            pixmap = pixmap.scaled(
                available_width, available_height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
        self.image_label.setPixmap(pixmap)

    def _step(self, delta):
        self.index = (self.index + delta) % len(self.radio_members)
        self._load_current()

    def _keep_current(self):
        radio, _member = self.radio_members[self.index]
        if radio is not None:
            radio.setChecked(True)
        self._update_keep_button()
        # Keeping disables the Keep button, so move focus/default off it to the
        # next most useful action.
        self._focus_primary_button()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()

    def showEvent(self, event):
        super().showEvent(event)
        # The viewport has no real size until the window is shown and laid
        # out, so defer one more fit-to-window pass to the next event-loop
        # turn. Without this the first image opens unscaled.
        QTimer.singleShot(0, self._render)


class _GroupCards:
    """Renders and manages one duplicate group's row of image cards.

    Only the first ``_MAX_MEMBERS_PER_GROUP`` images are shown initially; a
    clickable "+N more" tile expands the row to show every image in the group.
    ``keep_member`` is the image to keep, even when it is hidden.
    """

    def __init__(self, dialog, group, keep_member):
        self.dialog = dialog
        self.group = group
        self.keep_member = keep_member
        self.expanded = False
        # list of (QRadioButton, member) for the currently shown cards.
        self.radio_members: list = []
        # list of (relation QLabel, info QLabel, member) for the shown cards.
        self.card_labels: list = []
        self.shown_count = 0

        kind_label = 'Exact duplicates' if group.kind == 'exact' \
            else 'Near duplicates'
        self.box = QGroupBox(f'{kind_label} \u2014 {len(group.members)} images')
        box_layout = QVBoxLayout(self.box)

        # "Ignore this group" lets the user protect a whole group: when checked,
        # none of its images are deleted, moved, or tagged by the action
        # buttons, and picking a keeper is disabled because it's irrelevant.
        self.ignore_checkbox = QCheckBox(
            'Ignore this group (leave all its images untouched)')
        self.ignore_checkbox.toggled.connect(self._on_ignore_toggled)
        box_layout.addWidget(self.ignore_checkbox)

        self.cards_scroll = QScrollArea()
        self.cards_scroll.setWidgetResizable(True)
        self.cards_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.cards_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        box_layout.addWidget(self.cards_scroll)
        self._populate()

    @property
    def ignored(self) -> bool:
        return self.ignore_checkbox.isChecked()

    def _on_ignore_toggled(self, ignored: bool):
        # Dim and disable the thumbnails when the group is ignored so it's
        # visually obvious it won't be acted on and no keeper can be picked.
        self.cards_scroll.setEnabled(not ignored)

    def _populate(self):
        button_group = QButtonGroup(self.box)
        button_group.setExclusive(True)
        self.button_group = button_group
        self.radio_members = []
        self.card_labels = []

        cards_widget = QWidget()
        cards_layout = QHBoxLayout(cards_widget)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(10)

        members = self.group.members
        shown = members if self.expanded else members[:_MAX_MEMBERS_PER_GROUP]
        for position, member in enumerate(shown):
            card, radio, relation_label, info_label = \
                self.dialog._build_card(member, self)
            radio.setChecked(member is self.keep_member)
            radio.toggled.connect(
                lambda checked, m=member: self._on_keep_toggled(checked, m))
            button_group.addButton(radio)
            self.radio_members.append((radio, member))
            self.card_labels.append((relation_label, info_label, member))
            cards_layout.addWidget(card)
            if self.expanded and position % 20 == 19:
                QApplication.processEvents()

        hidden = len(members) - len(shown)
        if hidden > 0:
            more_tile = _ClickableLabel(f'+{hidden} more\n\nClick to show')
            more_tile.setFixedSize(_THUMBNAIL_SIZE, _THUMBNAIL_SIZE)
            more_tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
            more_tile.setFrameShape(QFrame.Shape.StyledPanel)
            more_tile.setCursor(Qt.CursorShape.PointingHandCursor)
            more_tile.clicked.connect(self.expand)
            cards_layout.addWidget(more_tile)
        cards_layout.addStretch()

        # Fit the row's height to the cards, leaving room for the horizontal
        # scroll bar.
        self.cards_scroll.setFixedHeight(
            cards_widget.sizeHint().height()
            + self.cards_scroll.horizontalScrollBar().sizeHint().height() + 4)
        # setWidget takes ownership and deletes the previously set widget.
        self.cards_scroll.setWidget(cards_widget)
        self.shown_count = len(shown)
        self.update_relations()

    def _on_keep_toggled(self, checked: bool, member):
        if checked:
            self.keep_member = member
            self.update_relations()

    def update_relations(self):
        """Refresh the "Kept" / "Differs by N" label on every shown card."""
        for relation_label, _info_label, member in self.card_labels:
            text, tooltip = describe_relation(member, self.keep_member)
            relation_label.setText(f'<b>{text}</b>' if member
                                   is self.keep_member else text)
            relation_label.setToolTip(tooltip)

    def refresh_info(self):
        """Refresh each shown card's details (e.g. its tag count)."""
        for _relation_label, info_label, member in self.card_labels:
            info_label.setText(self.dialog._card_info_text(member))

    def set_keep(self, member):
        """Make `member` the keeper, expanding the group if it is hidden."""
        if all(shown is not member for _radio, shown in self.radio_members):
            self.keep_member = member
            self.expand()
            return
        for radio, shown in self.radio_members:
            if shown is member:
                radio.setChecked(True)
                return

    def expand(self):
        if self.expanded:
            return
        self.expanded = True
        self._populate()
