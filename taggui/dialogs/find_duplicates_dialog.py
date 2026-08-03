"""
"Find Duplicates" review dialog (design: DUPLICATE_DETECTION_PLAN, Phase 1).

Scans the currently loaded images for exact and near duplicates, shows each
duplicate group side by side, lets the user pick one image to keep per group,
and acts on the rest non-destructively (delete to Recycle Bin, move to a folder,
or tag them).
"""

from pathlib import Path

from PySide6.QtCore import (QFile, QObject, QRunnable, Qt, QThreadPool, QTimer,
                            Signal, Slot)
from PySide6.QtGui import QImageReader, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QDialog,
                               QFileDialog, QFrame, QGroupBox, QHBoxLayout,
                               QLabel, QMessageBox, QProgressDialog,
                               QPushButton, QRadioButton, QScrollArea, QSlider,
                               QVBoxLayout, QWidget)

from models.image_list_model import ImageListModel
from utils.completion_store import get_completion_store
from utils.duplicate_detection import DuplicateCache, find_duplicate_groups
from utils.settings import DEFAULT_SETTINGS, get_settings
from utils.utils import get_confirmation_dialog_reply, pluralize

# Thumbnail edge length (pixels) for the cards in a duplicate group.
_THUMBNAIL_SIZE = 140
# Rendering caps so a pathological result set (e.g. thousands of near-identical
# training images merged into giant groups) can never freeze the UI or exhaust
# memory. Only this many groups / members-per-group are drawn at once.
_MAX_GROUPS_SHOWN = 100
_MAX_MEMBERS_PER_GROUP = 12
# The strictness slider runs from 0 (only visually identical images) up to this
# maximum dHash Hamming distance. The hash is 256-bit, so each step is fine and
# strict: exact resizes / re-compressions differ by only ~0-2 bits, small edits
# by ~8-12, and clearly different images by 50+. A max of 20 keeps the whole
# range comfortably inside "genuinely similar" territory.
_MAX_THRESHOLD = 20
# The tag applied by the "Tag others" action.
_DUPLICATE_TAG = 'duplicate'


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

    progress = Signal(int, int)
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
            progress_callback=lambda done, total: self.progress.emit(done,
                                                                     total),
            should_cancel=lambda: self._cancelled)
        self.finished.emit(groups)


class FindDuplicatesDialog(QDialog):
    def __init__(self, parent, image_list_model: ImageListModel,
                 reload_callback):
        super().__init__(parent)
        self.image_list_model = image_list_model
        self.reload_callback = reload_callback
        self.settings = get_settings()
        self._cache = DuplicateCache()
        self._cache.load()
        self._scanner: DuplicateScanner | None = None
        # One (QButtonGroup, [(QRadioButton, member), ...]) per duplicate group.
        self._group_selections: list[tuple[QButtonGroup, list]] = []

        self.setWindowTitle('Find Duplicates')
        self.resize(900, 640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        description = QLabel(
            'Finds exact duplicates (identical files) and near duplicates '
            '(resizes, re-saves, or minor edits) among the loaded images. '
            'Choose one image to keep in each group, then act on the rest. '
            'Tick "Ignore this group" to leave a whole group untouched. '
            'Nothing is deleted permanently — deletions go to the Recycle Bin.')
        description.setWordWrap(True)
        layout.addWidget(description)

        # --- Strictness controls -------------------------------------------
        controls_row = QHBoxLayout()
        controls_row.addWidget(QLabel('Strictness:'))
        self.strictness_slider = QSlider(Qt.Orientation.Horizontal)
        self.strictness_slider.setMinimum(0)
        self.strictness_slider.setMaximum(_MAX_THRESHOLD)
        self.strictness_slider.setValue(self._initial_threshold())
        self.strictness_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.strictness_slider.setTickInterval(2)
        self.strictness_slider.valueChanged.connect(self._update_strictness_label)
        controls_row.addWidget(self.strictness_slider, 1)
        self.strictness_label = QLabel()
        controls_row.addWidget(self.strictness_label)
        self.scan_button = QPushButton('Scan')
        self.scan_button.clicked.connect(self.start_scan)
        controls_row.addWidget(self.scan_button)
        layout.addLayout(controls_row)
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

        self.status_label = QLabel('Click "Scan" to look for duplicates.')
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # --- Auto-select + action buttons ----------------------------------
        auto_row = QHBoxLayout()
        auto_row.addWidget(QLabel('Auto-select keeper:'))
        self.keep_largest_button = QPushButton('Keep largest resolution')
        self.keep_largest_button.clicked.connect(
            lambda: self._auto_select('resolution'))
        auto_row.addWidget(self.keep_largest_button)
        self.keep_newest_button = QPushButton('Keep newest file')
        self.keep_newest_button.clicked.connect(
            lambda: self._auto_select('newest'))
        auto_row.addWidget(self.keep_newest_button)
        auto_row.addStretch()
        layout.addLayout(auto_row)

        action_row = QHBoxLayout()
        action_row.addStretch()
        self.delete_button = QPushButton('Delete others to Recycle Bin')
        self.delete_button.clicked.connect(self.delete_others)
        action_row.addWidget(self.delete_button)
        self.move_button = QPushButton('Move others to folder...')
        self.move_button.clicked.connect(self.move_others)
        action_row.addWidget(self.move_button)
        self.tag_button = QPushButton(f'Tag others as "{_DUPLICATE_TAG}"')
        self.tag_button.clicked.connect(self.tag_others)
        action_row.addWidget(self.tag_button)
        layout.addLayout(action_row)

        self._set_actions_enabled(False)

    # ------------------------------------------------------------------
    # Strictness helpers
    # ------------------------------------------------------------------

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
        images = list(self.image_list_model.images)
        if not images:
            self.status_label.setText('No images are loaded. Load a directory '
                                      'first, then scan.')
            return
        threshold = self.strictness_slider.value()
        self.settings.setValue('duplicate_detection_strictness', threshold)
        self._clear_results()
        self._set_actions_enabled(False)
        self.scan_button.setEnabled(False)

        progress = QProgressDialog('Scanning images for duplicates...',
                                   'Cancel', 0, len(images), self)
        progress.setWindowTitle('Find Duplicates')
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        # Don't let Qt auto-hide the dialog the instant the bar reaches its
        # maximum: we want to keep it on screen and switch it to a "Preparing
        # groups" busy bar while the results render. We hide it ourselves with
        # reset() once everything is done.
        progress.setAutoReset(False)
        progress.setValue(0)

        scanner = DuplicateScanner(images, threshold, self._cache)
        self._scanner = scanner
        scanner.progress.connect(
            lambda done, total: progress.setValue(done))
        progress.canceled.connect(scanner.cancel)

        def on_finished(groups):
            # Read the real cancellation state from the scanner. We must NOT
            # rely on progress.wasCanceled() here, because closing/resetting a
            # QProgressDialog itself counts as a cancel and would give a false
            # positive on a successful scan.
            was_cancelled = scanner.cancelled
            self._scanner = None
            if was_cancelled:
                progress.reset()
                self.scan_button.setEnabled(True)
                self.status_label.setText('Scan cancelled.')
                return
            # Building and rendering the result cards can take a noticeable
            # moment on large sets. Rather than closing the progress dialog and
            # leaving the user staring at a frozen-looking window, switch the
            # same dialog to an indeterminate "busy" bar until rendering is
            # done. Removing the Cancel button reflects that this phase can't be
            # cancelled. _show_groups yields to the event loop while drawing, so
            # the busy indicator keeps animating.
            progress.setLabelText('Preparing duplicate groups\u2026')
            progress.setCancelButton(None)
            progress.setRange(0, 0)
            progress.show()
            progress.raise_()
            QApplication.processEvents()
            # Keep the Scan button disabled until rendering is finished to
            # avoid a re-entrant scan.
            self._show_groups(groups, len(images))
            progress.reset()
            self.scan_button.setEnabled(True)

        scanner.finished.connect(on_finished)
        QThreadPool.globalInstance().start(scanner)

    def _clear_results(self):
        self._group_selections.clear()
        while self.results_layout.count():
            item = self.results_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.results_layout.addStretch()

    def _show_groups(self, groups: list, scanned_count: int):
        self._clear_results()
        if not groups:
            self.status_label.setText(
                f'No duplicates found among {scanned_count} '
                f'{pluralize("image", scanned_count)} at this strictness. '
                f'Try moving the slider toward "Loose".')
            self._set_actions_enabled(False)
            return
        duplicate_total = sum(len(group.members) for group in groups)
        largest_group = max(len(group.members) for group in groups)
        shown_groups = groups[:_MAX_GROUPS_SHOWN]

        # Build the group boxes, yielding to the event loop periodically so the
        # window stays responsive even when there are many cards to draw.
        cards_built = 0
        for group in shown_groups:
            group_cards = _GroupCards(self, group)
            self._group_selections.append(group_cards)
            self.results_layout.insertWidget(
                self.results_layout.count() - 1, group_cards.box)
            cards_built += group_cards.shown_count
            if cards_built >= 40:
                cards_built = 0
                QApplication.processEvents()

        message = (
            f'Found {len(groups)} duplicate '
            f'{pluralize("group", len(groups))} ({duplicate_total} images) '
            f'among {scanned_count} scanned. '
            f'The first image in each group is kept by default. '
            f'Click a thumbnail to enlarge it.')
        notes = []
        if len(groups) > _MAX_GROUPS_SHOWN:
            notes.append(
                f'Only the first {_MAX_GROUPS_SHOWN} groups are shown')
        if largest_group > _MAX_MEMBERS_PER_GROUP:
            notes.append(
                'some groups are very large, which usually means the '
                'strictness is too loose \u2014 try dragging the slider left')
        if notes:
            message += ' Note: ' + '; '.join(notes) + '.'
        self.status_label.setText(message)
        self._set_actions_enabled(True)

    def _build_card(self, member, keep: bool,
                    group_cards=None) -> tuple[QWidget, QRadioButton]:
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(8, 8, 8, 8)
        card_layout.setSpacing(4)

        thumbnail_label = _ClickableLabel()
        thumbnail_label.setFixedSize(_THUMBNAIL_SIZE, _THUMBNAIL_SIZE)
        thumbnail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumbnail_label.setCursor(Qt.CursorShape.PointingHandCursor)
        pixmap = _load_thumbnail(member.path)
        if pixmap is not None:
            thumbnail_label.setPixmap(pixmap)
        else:
            thumbnail_label.setText('(no preview)')
        thumbnail_label.clicked.connect(
            lambda m=member, gc=group_cards: self._show_preview(gc, m))
        card_layout.addWidget(thumbnail_label)

        if member.dimensions:
            width, height = member.dimensions
            dimensions_text = f'{width}\u00d7{height}'
        else:
            dimensions_text = 'unknown size'
        info_label = QLabel(
            f'{_elide(member.path.name)}\n'
            f'{dimensions_text}  \u2022  {_format_size(member.size)}')
        info_label.setToolTip(str(member.path))
        card_layout.addWidget(info_label)

        radio = QRadioButton('Keep')
        radio.setChecked(keep)
        card_layout.addWidget(radio)
        return card, radio

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
        dialog.exec()

    # ------------------------------------------------------------------
    # Selection helpers
    # ------------------------------------------------------------------

    def _auto_select(self, strategy: str):
        for group_cards in self._group_selections:
            if group_cards.ignored:
                continue
            radio_members = group_cards.radio_members
            if not radio_members:
                continue
            if strategy == 'resolution':
                best = max(radio_members, key=lambda rm: _resolution_key(
                    rm[1]))
            else:  # 'newest'
                best = max(radio_members, key=lambda rm: (rm[1].mtime_ns or 0))
            best[0].setChecked(True)

    def _targets(self) -> list:
        """Return the members NOT marked "Keep" across all groups, excluding
        any group the user has marked "Ignore"."""
        targets = []
        for group_cards in self._group_selections:
            if group_cards.ignored:
                continue
            for radio, member in group_cards.radio_members:
                if not radio.isChecked():
                    targets.append(member)
        return targets

    def _set_actions_enabled(self, enabled: bool):
        self.delete_button.setEnabled(enabled)
        self.move_button.setEnabled(enabled)
        self.tag_button.setEnabled(enabled)
        self.keep_largest_button.setEnabled(enabled)
        self.keep_newest_button.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    @Slot()
    def delete_others(self):
        targets = self._targets()
        if not targets:
            self._warn_no_targets()
            return
        count = len(targets)
        reply = get_confirmation_dialog_reply(
            'Delete Duplicates',
            f'Move {count} duplicate {pluralize("image", count)} and '
            f'{"its" if count == 1 else "their"} '
            f'{pluralize("caption", count)} to the Recycle Bin?')
        if reply != QMessageBox.StandardButton.Yes:
            return
        failures = 0
        for member in targets:
            if not QFile(str(member.path)).moveToTrash():
                failures += 1
                continue
            caption_path = member.path.with_suffix('.txt')
            caption_file = QFile(str(caption_path))
            if caption_file.exists():
                caption_file.moveToTrash()
        if failures:
            QMessageBox.warning(
                self, 'Delete Duplicates',
                f'Failed to delete {failures} '
                f'{pluralize("file", failures)}. They may be open or '
                f'read-only.')
        self._finish_destructive_action()

    @Slot()
    def move_others(self):
        targets = self._targets()
        if not targets:
            self._warn_no_targets()
            return
        destination = QFileDialog.getExistingDirectory(
            self, 'Select a folder to move the duplicates into',
            self.settings.value('directory_path', type=str) or '')
        if not destination:
            return
        destination_path = Path(destination)
        completion_store = get_completion_store()
        completion_changed = False
        failures = 0
        for member in targets:
            try:
                target_image_path = destination_path / member.path.name
                member.path.replace(target_image_path)
                if completion_store.move_completion(member.path,
                                                    target_image_path):
                    completion_changed = True
                caption_path = member.path.with_suffix('.txt')
                if caption_path.exists():
                    caption_path.replace(destination_path / caption_path.name)
            except OSError:
                failures += 1
        if completion_changed:
            completion_store.save()
        if failures:
            QMessageBox.warning(
                self, 'Move Duplicates',
                f'Failed to move {failures} {pluralize("file", failures)}.')
        self._finish_destructive_action()

    @Slot()
    def tag_others(self):
        targets = self._targets()
        if not targets:
            self._warn_no_targets()
            return
        target_paths = {member.path for member in targets}
        indices = [self.image_list_model.index(row, 0)
                   for row, image in enumerate(self.image_list_model.images)
                   if image.path in target_paths]
        if not indices:
            return
        self.image_list_model.add_tags([_DUPLICATE_TAG], indices)
        count = len(indices)
        self.status_label.setText(
            f'Tagged {count} duplicate {pluralize("image", count)} as '
            f'"{_DUPLICATE_TAG}". Nothing was deleted.')

    def _finish_destructive_action(self):
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
            'Done. The directory is reloading \u2014 click "Scan" again to '
            'check for any remaining duplicates.')

    def _warn_no_targets(self):
        QMessageBox.information(
            self, 'Find Duplicates',
            'There is nothing to act on. Every image is either marked "Keep" '
            'or belongs to a group you have ticked "Ignore". Un-keep the '
            'duplicates you want to remove, or un-ignore a group, first.')


def _load_thumbnail(path):
    """Decode an image directly at (roughly) thumbnail size.

    Using QImageReader.setScaledSize makes the decoder produce a small image
    instead of loading the full-resolution bitmap into memory first, which is
    dramatically faster and lighter when a group has many large images.
    """
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    original = reader.size()
    if original.isValid() and (original.width() > _THUMBNAIL_SIZE
                               or original.height() > _THUMBNAIL_SIZE):
        reader.setScaledSize(original.scaled(
            _THUMBNAIL_SIZE, _THUMBNAIL_SIZE,
            Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        return None
    return QPixmap.fromImage(image)


def _resolution_key(member) -> tuple:
    width, height = (member.dimensions or (0, 0))
    return ((width or 0) * (height or 0), member.size or 0)


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
    The currently kept image is preserved across an expansion.
    """

    def __init__(self, dialog, group):
        self.dialog = dialog
        self.group = group
        self.expanded = False
        # list of (QRadioButton, member) for the currently shown cards.
        self.radio_members: list = []
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
        self.cards_scroll.setFixedHeight(_THUMBNAIL_SIZE + 100)
        box_layout.addWidget(self.cards_scroll)
        self._populate()

    def _current_keep(self):
        for radio, member in self.radio_members:
            if radio.isChecked():
                return member
        return None

    @property
    def ignored(self) -> bool:
        return self.ignore_checkbox.isChecked()

    def _on_ignore_toggled(self, ignored: bool):
        # Dim and disable the thumbnails when the group is ignored so it's
        # visually obvious it won't be acted on and no keeper can be picked.
        self.cards_scroll.setEnabled(not ignored)

    def _populate(self, keep_member=None):
        button_group = QButtonGroup(self.box)
        button_group.setExclusive(True)
        self.button_group = button_group
        self.radio_members = []

        cards_widget = QWidget()
        cards_layout = QHBoxLayout(cards_widget)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(10)

        members = self.group.members
        shown = members if self.expanded else members[:_MAX_MEMBERS_PER_GROUP]
        for position, member in enumerate(shown):
            default_keep = (keep_member is None and position == 0)
            card, radio = self.dialog._build_card(
                member, keep=default_keep, group_cards=self)
            if keep_member is not None and member is keep_member:
                radio.setChecked(True)
            button_group.addButton(radio)
            self.radio_members.append((radio, member))
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

        # setWidget takes ownership and deletes the previously set widget.
        self.cards_scroll.setWidget(cards_widget)
        self.shown_count = len(shown)

    def expand(self):
        if self.expanded:
            return
        keep_member = self._current_keep()
        self.expanded = True
        self._populate(keep_member=keep_member)
