"""
"Find Duplicates" review dialog (design: DUPLICATE_DETECTION_PLAN, Phase 1).

Scans the loaded images (all, filtered or selected) for exact and near
duplicates, shows each duplicate group side by side, lets the user pick one
image to keep per group, and acts on the rest non-destructively (delete to
Recycle Bin, move to a folder, or tag them), optionally copying their tags to
the kept image first.
"""

import itertools
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (QAbstractListModel, QEvent, QModelIndex, QObject,
                            QRunnable, Qt, QThread, QThreadPool, QTimer,
                            Signal, Slot)
from PySide6.QtGui import (QFont, QIcon, QImageReader, QKeySequence, QPainter,
                           QPalette, QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox,
                               QComboBox, QDialog,
                               QFileDialog, QFrame, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QMessageBox,
                               QProgressDialog, QPushButton, QRadioButton,
                               QScrollArea, QSizePolicy, QSlider, QVBoxLayout,
                               QWidget)

from models.image_list_model import ImageListModel, Scope
from utils.duplicate_detection import (DuplicateCache, find_duplicate_groups,
                                       hamming_distance)
from utils.settings import DEFAULT_SETTINGS, get_settings
from utils.settings_widgets import (FocusedScrollSettingsComboBox,
                                    SettingsBigCheckBox)
from utils.utils import get_confirmation_dialog_reply, pluralize
from widgets.image_list import transfer_images, trash_images
from widgets.image_viewer import ImageViewer

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
KEEP_OLDEST_FILE = 'Oldest file'
KEEP_MOST_TAGS = 'Most tags'
KEEP_RULES = [KEEP_LARGEST_RESOLUTION, KEEP_LARGEST_FILE, KEEP_NEWEST_FILE,
              KEEP_OLDEST_FILE, KEEP_MOST_TAGS]


class _ClickableLabel(QLabel):
    """A QLabel that emits ``clicked`` when pressed with the left mouse button."""

    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


def _strictness_text(value: int) -> str:
    """Describe a strictness slider value."""
    if value == 0:
        return 'Exact / identical only'
    if value <= 2:
        return f'Very strict \u2014 near-identical (\u2264 {value})'
    if value <= 6:
        return f'Strict (\u2264 {value})'
    if value <= 12:
        return f'Medium (\u2264 {value})'
    return f'Loose (\u2264 {value})'


def _apply_indicator_palette(button):
    # Only the Base color is overridden, so the other colors keep following
    # the app theme.
    window_color = button.palette().color(QPalette.ColorRole.Window)
    if button.palette().color(QPalette.ColorRole.Base) != window_color:
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Base, window_color)
        button.setPalette(palette)


class _IndicatorPaletteFilter(QObject):
    """Updates the overridden indicator color when the app theme changes."""

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.PaletteChange:
            _apply_indicator_palette(watched)
        return False


_indicator_palette_filter = None


def _make_indicator_visible(button):
    """Fusion fills check box and radio button indicators with the Base
    color, which would make them invisible on the dark results background.
    Use the window color for them instead."""
    global _indicator_palette_filter
    if _indicator_palette_filter is None:
        _indicator_palette_filter = _IndicatorPaletteFilter()
    _apply_indicator_palette(button)
    button.installEventFilter(_indicator_palette_filter)


class _DuplicateCard(QFrame):
    """A frameless card; the kept one is outlined in the highlight color,
    like a selected image in the Images pane."""

    def __init__(self):
        super().__init__()
        self.kept = False

    def set_kept(self, kept: bool):
        if kept != self.kept:
            self.kept = kept
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.kept:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self.palette().color(QPalette.ColorRole.Highlight),
                            2))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 4, 4)


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


def _format_modified(mtime_ns) -> str:
    """Return the file's modified date (what "Newest file" and "Oldest file"
    compare), e.g. 'Modified 2024-05-01 14:32'."""
    if mtime_ns is None:
        return 'Modified ?'
    try:
        stamp = datetime.fromtimestamp(mtime_ns / 1e9)
    except (OverflowError, OSError, ValueError):
        return 'Modified ?'
    return f'Modified {stamp:%Y-%m-%d %H:%M}'


class _VerticalWheelForwarder(QObject):
    """Sends plain vertical mouse-wheel scrolling over a group's row of cards
    (including its horizontal scroll bar) to the results list, so scrolling
    down the results never gets caught by a row that slides under the cursor.
    Shift+wheel and sideways (touchpad) scrolling scroll the row."""

    def __init__(self, target_scroll_area: QScrollArea, row_scroll_area:
                 QScrollArea, parent=None):
        super().__init__(parent)
        self.target_scroll_area = target_scroll_area
        self.row_scroll_area = row_scroll_area

    def eventFilter(self, watched, event):
        if event.type() != QEvent.Type.Wheel:
            return False
        delta = event.angleDelta()
        if delta.x() != 0 or delta.y() == 0:
            return False
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            bar = self.row_scroll_area.horizontalScrollBar()
            steps = (-delta.y() / 120 * QApplication.wheelScrollLines()
                     * bar.singleStep())
            bar.setValue(bar.value() + round(steps))
            return True
        QApplication.sendEvent(self.target_scroll_area.verticalScrollBar(),
                               event)
        return True


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
        if rule == KEEP_OLDEST_FILE:
            # Files whose date is unknown count as the newest.
            mtime_ns = member.mtime_ns
            return ((-mtime_ns if mtime_ns is not None else -float('inf')),
                    ) + resolution
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


def describe_relation(member, keeper) -> str:
    """Return a short label describing how `member` compares with the kept
    image ("Differs by N" counts differing bits of the 256-bit visual
    fingerprint; see the README)."""
    if member is keeper:
        return 'Kept'
    if member.sha256 is not None and member.sha256 == keeper.sha256:
        return 'Identical file'
    if member.dhash is None or keeper.dhash is None:
        return ''
    distance = hamming_distance(member.dhash, keeper.dhash)
    if distance == 0:
        return 'Looks identical'
    return f'Differs by {distance}'


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
        # Roomy enough for several groups of cards, but never bigger than the
        # screen.
        screen = QApplication.primaryScreen()
        width, height = 1120, 900
        if screen is not None:
            available = screen.availableGeometry()
            width = min(width, int(available.width() * 0.9))
            height = min(height, int(available.height() * 0.9))
        self.resize(width, height)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        description = QLabel(
            'Finds identical files and near duplicates (resizes, re-saves or '
            'minor edits). In each group, choose the image to keep, then act '
            'on the others. Click a thumbnail to view it larger. Deleted '
            'images go to the Recycle Bin.')
        description.setWordWrap(True)
        layout.addWidget(description)

        # --- Scan controls --------------------------------------------------
        # Strictness gets its own row so the slider keeps a usable width next
        # to the longest strictness description.
        scan_grid = QGridLayout()
        scan_grid.setHorizontalSpacing(8)
        scan_grid.addWidget(QLabel('Scope:'), 0, 0)
        scope_row = QHBoxLayout()
        self.scope_combo_box = FocusedScrollSettingsComboBox(
            key='duplicate_scan_scope', default=Scope.ALL_IMAGES.value)
        self.scope_combo_box.addItems(list(Scope))
        if self.scope_combo_box.currentText() not in list(Scope):
            self.scope_combo_box.setCurrentText(Scope.ALL_IMAGES.value)
        self.scope_combo_box.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.scope_combo_box.setSizePolicy(QSizePolicy.Policy.Fixed,
                                           QSizePolicy.Policy.Fixed)
        self.scope_combo_box.currentTextChanged.connect(
            self._update_scope_count_label)
        scope_row.addWidget(self.scope_combo_box)
        self.scope_count_label = QLabel()
        scope_row.addSpacing(6)
        scope_row.addWidget(self.scope_count_label)
        scope_row.addStretch()
        scan_grid.addLayout(scope_row, 0, 1, 1, 3)
        scan_grid.addWidget(QLabel('Strictness:'), 1, 0)
        self.strictness_slider = QSlider(Qt.Orientation.Horizontal)
        self.strictness_slider.setMinimum(0)
        self.strictness_slider.setMaximum(_MAX_THRESHOLD)
        self.strictness_slider.setValue(self._initial_threshold())
        self.strictness_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.strictness_slider.setTickInterval(2)
        self.strictness_slider.setMinimumWidth(200)
        self.strictness_slider.valueChanged.connect(self._update_strictness_label)
        scan_grid.addWidget(self.strictness_slider, 1, 1)
        self.strictness_label = QLabel()
        # Reserve room for the longest description so the slider doesn't
        # change width while it is dragged.
        metrics = self.strictness_label.fontMetrics()
        self.strictness_label.setMinimumWidth(max(
            metrics.horizontalAdvance(_strictness_text(value))
            for value in range(_MAX_THRESHOLD + 1)) + 4)
        scan_grid.addWidget(self.strictness_label, 1, 2)
        self.scan_button = QPushButton('Scan')
        self.scan_button.clicked.connect(self.start_scan)
        scan_grid.addWidget(self.scan_button, 1, 3)
        scan_grid.setColumnStretch(1, 1)
        layout.addLayout(scan_grid)
        self._update_scope_count_label()
        self._update_strictness_label(self.strictness_slider.value())

        # --- Results area ---------------------------------------------------
        # A darker background (the color of input fields) sets the results
        # apart from the controls, and each group sits on it as a panel.
        self.results_scroll_area = QScrollArea()
        self.results_scroll_area.setWidgetResizable(True)
        self.results_container = QWidget()
        self.results_container.setObjectName('duplicateResults')
        self.results_container.setBackgroundRole(QPalette.ColorRole.Base)
        self.results_container.setAutoFillBackground(True)
        self.results_layout = QVBoxLayout(self.results_container)
        self.results_layout.setContentsMargins(10, 10, 10, 10)
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
        actions_layout.addWidget(self.keep_rule_combo_box, 0, 1)
        self.apply_rule_button = QPushButton('Apply to all groups')
        self.apply_rule_button.clicked.connect(self.apply_keep_rule)
        actions_layout.addWidget(self.apply_rule_button, 0, 2)

        self.merge_tags_check_box = SettingsBigCheckBox(
            key='duplicate_merge_tags', default=False,
            text='Copy the tags of the other images to the kept image')
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
        self.strictness_label.setText(_strictness_text(value))

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
            f'among {self._scanned_count} scanned.')
        remaining = len(groups) - shown
        if remaining > 0:
            message += f' Showing {shown}.'
            next_count = min(_MAX_GROUPS_SHOWN, remaining)
            self.show_more_button.setText(
                f'Show next {next_count} {pluralize("group", next_count)}')
            self.show_more_button.show()
        else:
            self.show_more_button.hide()
        if max(len(group.members) for group in groups) \
                > _MAX_MEMBERS_PER_GROUP:
            message += (' Some groups are very large; try a stricter '
                        'setting.')
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
                f'{_format_modified(member.mtime_ns)}\n'
                f'{tags_text}')

    def _build_card(self, member, group_cards) -> tuple:
        """Return (card, keep radio, relation label, info label)."""
        card = _DuplicateCard()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(6, 6, 6, 6)
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
        _make_indicator_visible(radio)
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

        dialog = _PreviewDialog(self, radio_members, index,
                                self._thumbnails)
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


class _GroupImagesModel(QAbstractListModel):
    """The images of one duplicate group, in the shape the main ImageViewer
    reads: the image record (path and dimensions) for UserRole and the
    already-loaded card thumbnail, used as an instant placeholder, for
    DecorationRole."""

    def __init__(self, members, thumbnails: dict, parent=None):
        super().__init__(parent)
        self.members = members
        self.thumbnails = thumbnails

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.members)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.members):
            return None
        member = self.members[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return member
        if role == Qt.ItemDataRole.DecorationRole:
            pixmap = self.thumbnails.get(str(member.path))
            if pixmap is not None and not pixmap.isNull():
                return QIcon(pixmap)
        return None


_OVERLAY_STYLE = (
    'QFrame#previewOverlay { background-color: rgba(0, 0, 0, 160);'
    ' border-radius: 6px; }'
    'QFrame#previewOverlay QLabel { color: rgba(255, 255, 255, 230);'
    ' background: transparent; }'
    'QFrame#previewOverlay QPushButton { color: white;'
    ' background-color: rgba(255, 255, 255, 28);'
    ' border: 1px solid rgba(255, 255, 255, 70); border-radius: 4px;'
    ' padding: 4px 10px; }'
    'QFrame#previewOverlay QPushButton:hover {'
    ' background-color: rgba(255, 255, 255, 60); }'
    'QFrame#previewOverlay QPushButton:pressed {'
    ' background-color: rgba(255, 255, 255, 90); }'
    'QFrame#previewOverlay QPushButton:disabled {'
    ' color: rgba(255, 255, 255, 110);'
    ' border-color: rgba(255, 255, 255, 35); }')
# Hide the overlaid controls after this long without mouse movement.
_OVERLAY_HIDE_DELAY_MS = 2500
_OVERLAY_MARGIN = 10


def _main_window_key_sequences(widget, shortcut_id: str,
                               attribute: str) -> list[QKeySequence] | None:
    """Return the keys the main window currently uses for one of its
    QShortcut-based actions (including any extra bindings), or None when no
    ancestor of ``widget`` has that shortcut."""
    window = widget
    while window is not None and not hasattr(window, attribute):
        window = window.parentWidget()
    if window is None:
        return None
    shortcuts = [getattr(window, attribute)]
    shortcuts += getattr(window, '_extra_qshortcuts', {}).get(shortcut_id, [])
    return [shortcut.key() for shortcut in shortcuts]


class _PreviewDialog(QDialog):
    """Large viewer for a duplicate group, built on the main image viewer:
    zoom (mouse wheel, + / -), pan (drag) and double-click to reset, a
    single-image view that cycles through the group and a grid view of the
    whole group, with controls overlaid on the image and a full-screen
    toggle.

    ``radio_members`` is a list of ``(QRadioButton | None, member)`` tuples
    shared with the main dialog, so "Keep this image" here checks the
    matching radio back in the results list.
    """

    def __init__(self, parent, radio_members, index, thumbnails=None):
        super().__init__(parent)
        self.radio_members = radio_members
        self.index = index
        self._grid = False
        self._was_maximized = False
        self._zoomed_at_double_click = False
        members = [member for _radio, member in radio_members]
        self.model = _GroupImagesModel(members, thumbnails or {}, self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.viewer = ImageViewer(self.model)
        layout.addWidget(self.viewer)
        viewport = self.viewer.scroll_area.viewport()

        # Top left: what is shown.
        self.info_overlay = self._make_overlay(viewport)
        info_layout = QVBoxLayout(self.info_overlay)
        info_layout.setContentsMargins(10, 6, 10, 6)
        info_layout.setSpacing(2)
        self.name_label = QLabel()
        name_font = QFont(self.font())
        name_font.setBold(True)
        self.name_label.setFont(name_font)
        self.details_label = QLabel()
        info_layout.addWidget(self.name_label)
        info_layout.addWidget(self.details_label)

        # Top right: view controls.
        self.view_overlay = self._make_overlay(viewport)
        view_layout = QHBoxLayout(self.view_overlay)
        view_layout.setContentsMargins(6, 6, 6, 6)
        self.grid_button = self._make_button('Grid view (G)')
        self.grid_button.clicked.connect(self.toggle_grid)
        self.full_screen_button = self._make_button('Full screen (F11)')
        self.full_screen_button.clicked.connect(self.toggle_full_screen)
        self.close_button = self._make_button('Close (Esc)')
        self.close_button.clicked.connect(self.reject)
        for button in (self.grid_button, self.full_screen_button,
                       self.close_button):
            view_layout.addWidget(button)

        # Bottom centre: paging and the keep decision.
        self.nav_overlay = self._make_overlay(viewport)
        nav_layout = QHBoxLayout(self.nav_overlay)
        nav_layout.setContentsMargins(6, 6, 6, 6)
        self.prev_button = self._make_button('\u2190 Previous')
        self.prev_button.clicked.connect(lambda: self._step(-1))
        self.keep_button = self._make_button('Keep this image (K)')
        self.keep_button.clicked.connect(self._keep_current)
        self.next_button = self._make_button('Next \u2192')
        self.next_button.clicked.connect(lambda: self._step(1))
        for button in (self.prev_button, self.keep_button, self.next_button):
            nav_layout.addWidget(button)
        self.overlays = (self.info_overlay, self.view_overlay,
                         self.nav_overlay)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(_OVERLAY_HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self._hide_overlays_if_idle)
        # Watch mouse movement anywhere over the image to bring the controls
        # back, and viewport resizes to keep them in their corners.
        for widget in (viewport, self.viewer.image_label):
            widget.setMouseTracking(True)
            widget.installEventFilter(self)
        for overlay in self.overlays:
            overlay.installEventFilter(self)

        self.viewer.grid_cell_clicked.connect(self._on_grid_cell_clicked)
        # Double-clicking a grid cell opens it in the single-image view.
        self.viewer.scroll_area.reset_requested.connect(
            self._on_double_click)

        own_keys = []
        for key, slot in (
                (Qt.Key.Key_Left, lambda: self._step(-1)),
                (Qt.Key.Key_Right, lambda: self._step(1)),
                (Qt.Key.Key_K, self._keep_current),
                (Qt.Key.Key_G, self.toggle_grid),
                (Qt.Key.Key_F11, self.toggle_full_screen),
                (Qt.Key.Key_Return, self._on_enter),
                (Qt.Key.Key_Enter, self._on_enter),
                (Qt.Key.Key_Plus, self.viewer.zoom_in),
                (Qt.Key.Key_Equal, self.viewer.zoom_in),
                (Qt.Key.Key_Minus, self.viewer.zoom_out),
                (Qt.Key.Key_0, self.viewer.reset_view),
                (Qt.Key.Key_Escape, None)):
            sequence = QKeySequence(key)
            own_keys.append(sequence.toString())
            if slot is not None:
                QShortcut(sequence, self).activated.connect(slot)
        # The main window's zoom keys (Ctrl+0 / Ctrl++ / Ctrl+- unless
        # customized) work here too. A key is never bound twice, since Qt
        # ignores a key that two shortcuts share.
        for shortcut_id, attribute, slot, defaults in (
                ('reset_image_zoom', 'reset_image_zoom_shortcut',
                 self.viewer.reset_view, ['Ctrl+0']),
                ('zoom_in', 'zoom_in_shortcut', self.viewer.zoom_in,
                 ['Ctrl++', 'Ctrl+=']),
                ('zoom_out', 'zoom_out_shortcut', self.viewer.zoom_out,
                 ['Ctrl+-'])):
            sequences = _main_window_key_sequences(parent, shortcut_id,
                                                   attribute)
            if sequences is None:
                sequences = [QKeySequence(text) for text in defaults]
            for sequence in sequences:
                text = sequence.toString()
                if sequence.isEmpty() or text in own_keys:
                    continue
                own_keys.append(text)
                QShortcut(sequence, self).activated.connect(slot)

        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry()
        self.resize(int(available.width() * 0.85),
                    int(available.height() * 0.85))
        self._show_current()

    @staticmethod
    def _make_overlay(parent) -> QFrame:
        overlay = QFrame(parent)
        overlay.setObjectName('previewOverlay')
        overlay.setStyleSheet(_OVERLAY_STYLE)
        overlay.setCursor(Qt.CursorShape.ArrowCursor)
        # Clicks on the panel itself must not pan the image or pick a cell.
        overlay.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation)
        return overlay

    @staticmethod
    def _make_button(text: str) -> QPushButton:
        button = QPushButton(text)
        # Keep keyboard focus off the buttons so the arrow keys, Enter and
        # Esc always reach the viewer's shortcuts.
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setAutoDefault(False)
        return button

    # --- Showing images -----------------------------------------------------

    def _indices(self) -> list[QModelIndex]:
        return [self.model.index(row) for row in range(self.model.rowCount())]

    def _grid_cell_cap(self) -> int:
        return max(1, get_settings().value(
            'variant_grid_cell_cap',
            defaultValue=DEFAULT_SETTINGS['variant_grid_cell_cap'], type=int))

    def _kept_member(self):
        for radio, member in self.radio_members:
            if radio is not None and radio.isChecked():
                return member
        return None

    def _show_current(self, keep_zoom: bool = False):
        """Show the current image, in the single-image or the grid view.
        With ``keep_zoom`` a zoomed-in view keeps its zoom and position, so
        the same detail can be compared across the group."""
        if self._grid:
            self.viewer.update_grid_current(self._indices(), self.index,
                                            self._grid_cell_cap())
        else:
            self.viewer.exit_grid()
            index = self.model.index(self.index)
            if keep_zoom and not self._at_fit_zoom():
                self.viewer.reload_image(index)
            else:
                self.viewer.load_image(index)
        self._update_info()

    def _at_fit_zoom(self) -> bool:
        return abs(self.viewer.zoom_factor - 1.0) < 1e-9

    def _update_info(self):
        _radio, member = self.radio_members[self.index]
        total = len(self.radio_members)
        self.setWindowTitle(
            f'{member.path}  \u2014  {self.index + 1} of {total}')
        self.name_label.setText(
            f'{member.path.name}  \u2014  {self.index + 1} of {total}')
        parts = []
        if member.dimensions:
            width, height = member.dimensions
            parts.append(f'{width}\u00d7{height}')
        if member.size is not None:
            parts.append(_format_size(member.size))
        parts.append(_format_modified(member.mtime_ns))
        kept = self._kept_member()
        if kept is not None:
            relation = describe_relation(member, kept)
            if relation:
                parts.append(relation)
        self.details_label.setText('  \u2022  '.join(parts))

        multiple = total > 1
        self.prev_button.setEnabled(multiple)
        self.next_button.setEnabled(multiple)
        radio = self.radio_members[self.index][0]
        if radio is None:
            self.keep_button.setText('Keep this image (K)')
            self.keep_button.setEnabled(False)
        elif radio.isChecked():
            self.keep_button.setText('\u2713 Keeping this image')
            self.keep_button.setEnabled(False)
        else:
            self.keep_button.setText('Keep this image (K)')
            self.keep_button.setEnabled(True)
        self.grid_button.setText('Single view (G)' if self._grid
                                 else 'Grid view (G)')
        self.grid_button.setEnabled(multiple or self._grid)
        self.full_screen_button.setText(
            'Exit full screen (F11)' if self.isFullScreen()
            else 'Full screen (F11)')
        self._position_overlays()

    def _step(self, delta):
        if len(self.radio_members) < 2:
            return
        self.index = (self.index + delta) % len(self.radio_members)
        self._show_current(keep_zoom=True)
        self._show_overlays()

    def _keep_current(self):
        radio, _member = self.radio_members[self.index]
        if radio is None or radio.isChecked():
            return
        radio.setChecked(True)
        self._update_info()

    def toggle_grid(self):
        if not self._grid and len(self.radio_members) < 2:
            return
        self._grid = not self._grid
        if self._grid:
            self.viewer.show_grid(self._indices(), self.index,
                                  self._grid_cell_cap())
            self._update_info()
        else:
            self._show_current()
        self._show_overlays()

    def _open_in_single_view(self, row: int):
        self.index = row
        self._grid = False
        self._show_current()

    @Slot(QModelIndex)
    def _on_grid_cell_clicked(self, proxy_index):
        # Like the main grid: a click makes the cell current.
        if self._grid and proxy_index.row() != self.index:
            self.index = proxy_index.row()
            self._show_current()

    def _on_double_click(self):
        # In a zoomed grid, a double-click only resets the zoom (the viewer
        # does that itself); once the whole grid fits, it opens the cell.
        if self._grid and not self._zoomed_at_double_click:
            # The first click of the double-click already made the cell
            # under the cursor current.
            QTimer.singleShot(0, lambda: self._open_in_single_view(
                self.index))

    def _on_enter(self):
        if self._grid:
            self._open_in_single_view(self.index)
        else:
            self._keep_current()

    # --- Full screen ----------------------------------------------------------

    def toggle_full_screen(self):
        if self.isFullScreen():
            if self._was_maximized:
                self.showMaximized()
            else:
                self.showNormal()
        else:
            self._was_maximized = self.isMaximized()
            # Without this, QDialog re-centres itself when shown and lands a
            # few pixels off the monitor, so the Windows taskbar stays on top.
            self.setAttribute(Qt.WidgetAttribute.WA_Moved, True)
            self.showFullScreen()
        self._update_info()
        self._show_overlays()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.toggle_full_screen()
            event.accept()
            return
        super().keyPressEvent(event)

    # --- Overlaid controls ----------------------------------------------------

    def _position_overlays(self):
        viewport = self.viewer.scroll_area.viewport()
        width = viewport.width()
        height = viewport.height()
        for overlay in self.overlays:
            overlay.adjustSize()
        self.info_overlay.move(_OVERLAY_MARGIN, _OVERLAY_MARGIN)
        view_x = width - self.view_overlay.width() - _OVERLAY_MARGIN
        view_y = _OVERLAY_MARGIN
        # In a narrow window, put the buttons under the info box.
        if view_x < self.info_overlay.geometry().right() + _OVERLAY_MARGIN:
            view_x = max(_OVERLAY_MARGIN, view_x)
            view_y = self.info_overlay.geometry().bottom() + _OVERLAY_MARGIN
        self.view_overlay.move(view_x, view_y)
        self.nav_overlay.move(
            (width - self.nav_overlay.width()) // 2,
            height - self.nav_overlay.height() - _OVERLAY_MARGIN)
        for overlay in self.overlays:
            overlay.raise_()

    def _show_overlays(self):
        for overlay in self.overlays:
            overlay.show()
        self._position_overlays()
        self.viewer.scroll_area.viewport().unsetCursor()
        self._hide_timer.start()

    def _hide_overlays_if_idle(self):
        # Keep the controls while the cursor is on them.
        if any(overlay.underMouse() for overlay in self.overlays):
            self._hide_timer.start()
            return
        for overlay in self.overlays:
            overlay.hide()
        if self.isFullScreen() and not self.viewer.scroll_area.is_dragging:
            self.viewer.scroll_area.viewport().setCursor(
                Qt.CursorShape.BlankCursor)

    def eventFilter(self, watched, event):
        event_type = event.type()
        if event_type == QEvent.Type.MouseButtonDblClick:
            # Seen before the viewer resets the zoom for the same event.
            self._zoomed_at_double_click = not self._at_fit_zoom()
        elif event_type == QEvent.Type.MouseMove:
            if not all(overlay.isVisible() for overlay in self.overlays):
                self._show_overlays()
            else:
                self._hide_timer.start()
        elif (event_type == QEvent.Type.Resize
              and watched is self.viewer.scroll_area.viewport()):
            self._position_overlays()
        return False

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._show_overlays)


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
        # Only a title and a divider line, so the images sit directly on the
        # dark results background like in the Images pane.
        self.box.setFlat(True)
        title_font = self.box.font()
        title_font.setBold(True)
        self.box.setFont(title_font)
        box_layout = QVBoxLayout(self.box)
        box_layout.setContentsMargins(10, 8, 10, 10)

        # "Ignore this group" lets the user protect a whole group: when checked,
        # none of its images are deleted, moved, or tagged by the action
        # buttons, and picking a keeper is disabled because it's irrelevant.
        self.ignore_checkbox = QCheckBox(
            'Ignore this group (leave all its images untouched)')
        self.ignore_checkbox.toggled.connect(self._on_ignore_toggled)
        # Only the group's title is bold.
        normal_font = QFont(title_font)
        normal_font.setBold(False)
        self.ignore_checkbox.setFont(normal_font)
        _make_indicator_visible(self.ignore_checkbox)
        box_layout.addWidget(self.ignore_checkbox)

        self.cards_scroll = QScrollArea()
        self.cards_scroll.setFont(normal_font)
        self.cards_scroll.setWidgetResizable(True)
        self.cards_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.cards_scroll.viewport().setAutoFillBackground(False)
        self.cards_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._wheel_forwarder = _VerticalWheelForwarder(
            dialog.results_scroll_area, self.cards_scroll, self.box)
        self.cards_scroll.viewport().installEventFilter(self._wheel_forwarder)
        self.cards_scroll.horizontalScrollBar().installEventFilter(
            self._wheel_forwarder)
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
        self.cards = []

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
            self.cards.append((card, member))
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
            text = describe_relation(member, self.keep_member)
            relation_label.setText(f'<b>{text}</b>' if member
                                   is self.keep_member else text)
        for card, member in self.cards:
            card.set_kept(member is self.keep_member)

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
