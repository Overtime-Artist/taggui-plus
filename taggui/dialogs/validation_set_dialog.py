"""Tools > Create Validation Set: set aside a tag-balanced share of a folder's
images (with their captions) in a validation subfolder."""

from __future__ import annotations

import random
from pathlib import Path

from PySide6.QtCore import (QAbstractListModel, QEvent, QModelIndex, QRect,
                            QSize, Qt, QTimer, QUrl, Slot)
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox,
                               QDialog, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QListView, QMessageBox,
                               QPlainTextEdit,
                               QProgressDialog, QPushButton, QSizePolicy,
                               QStyle, QStyledItemDelegate, QVBoxLayout)

from dialogs.find_duplicates_dialog import (DuplicateScanner,
                                            _get_scanner_keeper)
from models.image_list_model import ImageListModel
from utils.duplicate_detection import DuplicateCache
from utils.image_file_operations import (get_invalid_file_name_reason,
                                         get_path_key)
from utils.settings import DEFAULT_SETTINGS, get_settings
from utils.settings_widgets import (FocusedScrollComboBox,
                                    FocusedScrollSettingsComboBox,
                                    FocusedScrollSettingsSpinBox,
                                    SettingsBigCheckBox)
from utils.utils import pluralize
from utils.validation_set import (DEFAULT_FOLDER_NAME,
                                  DEFAULT_VARIANT_MATCHING, NeverPickStore,
                                  VARIANT_MATCHING_LEVELS, build_units,
                                  is_in_validation_folder, is_within,
                                  pick_units, return_destination,
                                  source_folders, tag_coverage,
                                  target_count)
from widgets.image_list import transfer_images

_CELL_SIZE = 150
_TEXT_HEIGHT = 18

SHOW_PICKS = 'Picks'
SHOW_ALL = 'All images'
SHOW_NEVER_PICK = 'Never pick'
SHOW_EXISTING = 'Already set aside'
SHOW_OPTIONS = [SHOW_PICKS, SHOW_ALL, SHOW_NEVER_PICK, SHOW_EXISTING]

_PICK_COLOR = QColor(60, 180, 75)
_EXISTING_COLOR = QColor(70, 130, 220)

# Item states for the thumbnail grid.
_STATE_NONE = 0
_STATE_PICKED = 1
_STATE_EXISTING = 2


class _ThumbnailModel(QAbstractListModel):
    """The items shown in the thumbnail grid (indices into the dialog's
    item list)."""

    def __init__(self, dialog: 'ValidationSetDialog'):
        super().__init__(dialog)
        self.dialog = dialog
        self.items: list[int] = []

    def set_items(self, items: list[int]):
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self.dialog.items[item].path.name
        if role == Qt.ItemDataRole.DecorationRole:
            return self.dialog.thumbnail(item)
        if role == Qt.ItemDataRole.UserRole:
            return item
        return None


class _ThumbnailDelegate(QStyledItemDelegate):
    def __init__(self, dialog: 'ValidationSetDialog'):
        super().__init__(dialog)
        self.dialog = dialog

    def sizeHint(self, option, index):
        return QSize(_CELL_SIZE + 8, _CELL_SIZE + _TEXT_HEIGHT + 8)

    def paint(self, painter: QPainter, option, index):
        item = index.data(Qt.ItemDataRole.UserRole)
        state = self.dialog.item_state(item)
        never_pick = self.dialog.is_never_pick(item)
        unit_size = self.dialog.unit_size(item)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cell = option.rect.adjusted(4, 4, -4, -4)
        palette = option.palette
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, palette.highlight())
        image_rect = QRect(cell.left(), cell.top(), cell.width(), _CELL_SIZE)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if icon is not None and not icon.isNull():
            pixmap = icon.pixmap(QSize(_CELL_SIZE, _CELL_SIZE))
            size = pixmap.size().scaled(image_rect.size(),
                                        Qt.AspectRatioMode.KeepAspectRatio)
            target = QRect(0, 0, size.width(), size.height())
            target.moveCenter(image_rect.center())
            if never_pick:
                painter.setOpacity(0.35)
            painter.drawPixmap(target, pixmap)
            painter.setOpacity(1.0)
        else:
            painter.fillRect(image_rect, palette.base())
        if state != _STATE_NONE:
            color = _PICK_COLOR if state == _STATE_PICKED else _EXISTING_COLOR
            pen = QPen(color, 4)
            if state == _STATE_EXISTING:
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(image_rect.adjusted(2, 2, -2, -2))
        badge_font = QFont(option.font)
        badge_font.setBold(True)
        painter.setFont(badge_font)
        if never_pick:
            self._draw_badge(painter, image_rect, '\U0001F6AB', top_right=True)
        if unit_size > 1:
            self._draw_badge(painter, image_rect,
                             f'{unit_size} variants', top_right=False)
        painter.setFont(option.font)
        text_rect = QRect(cell.left(), image_rect.bottom() + 2, cell.width(),
                          _TEXT_HEIGHT)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setPen(palette.highlightedText().color())
        else:
            painter.setPen(palette.text().color())
        name = option.fontMetrics.elidedText(
            index.data(Qt.ItemDataRole.DisplayRole),
            Qt.TextElideMode.ElideMiddle, text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, name)
        painter.restore()

    @staticmethod
    def _draw_badge(painter, image_rect: QRect, text: str, top_right: bool):
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 10
        height = metrics.height() + 4
        if top_right:
            rect = QRect(image_rect.right() - width - 4, image_rect.top() + 4,
                         width, height)
        else:
            rect = QRect(image_rect.left() + 4,
                         image_rect.bottom() - height - 4, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 170))
        painter.drawRoundedRect(rect, 4, 4)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


class ValidationSetDialog(QDialog):
    def __init__(self, parent, image_list_model: ImageListModel,
                 directory_path: Path, current_image_path: Path | None,
                 reload_callback, refresh_callback=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.image_list_model = image_list_model
        self.directory_path = Path(directory_path)
        self.reload_callback = reload_callback
        # Rescans the folder for changes made outside the app.
        self.refresh_callback = refresh_callback
        self.settings = get_settings()
        self.never_pick = NeverPickStore()
        self.rng = random.Random()
        self._cache = DuplicateCache()
        self._cache.load()
        self._scanner: DuplicateScanner | None = None
        self._outside_change_note = ''
        self._structure_timer = QTimer(self)
        self._structure_timer.setSingleShot(True)
        self._structure_timer.timeout.connect(self._on_images_added_or_removed)
        self._tags_timer = QTimer(self)
        self._tags_timer.setSingleShot(True)
        self._tags_timer.setInterval(150)
        self._tags_timer.timeout.connect(self._update_status)
        self._progress_dialog: QProgressDialog | None = None
        # The images in scope: the candidates, then those already set aside.
        self.items: list = []
        self._candidate_count = 0
        self._model_rows: dict[int, int] = {}
        # Near-duplicate units over the candidates, and what they were
        # scanned for.
        self._units: list[list[int]] | None = None
        self._unit_of_item: dict[int, int] = {}
        self._scan_key = None
        self._picked_units: list[int] = []
        self._picked_items: set[int] = set()
        # Picks dropped by the user, kept out until the scope changes.
        self._dropped_items: set[int] = set()
        self._missed_tags_text = ''

        self.setWindowTitle('Create Validation Set')
        screen = QApplication.primaryScreen()
        width, height = 1060, 860
        if screen is not None:
            available = screen.availableGeometry()
            width = min(width, int(available.width() * 0.9))
            height = min(height, int(available.height() * 0.9))
        self.resize(width, height)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        description = QLabel(
            'Hold out a tag-balanced share of a folder\'s images (with their '
            'captions) for validation. Double-click a thumbnail to open it.')
        description.setWordWrap(True)
        layout.addWidget(description)

        settings_box = QGroupBox('Settings')
        settings_layout = QVBoxLayout(settings_box)
        settings_layout.setSpacing(8)
        folder_row = QHBoxLayout()
        folder_row.setSpacing(10)
        folder_row.addWidget(QLabel('Folder:'))
        self.folder_combo_box = FocusedScrollComboBox()
        # Fusion would grow the list to the screen height before scrolling.
        self.folder_combo_box.setStyleSheet('combobox-popup: 0;')
        self.folder_combo_box.setMaxVisibleItems(15)
        self.folder_combo_box.setSizePolicy(QSizePolicy.Policy.Expanding,
                                            QSizePolicy.Policy.Fixed)
        folder_row.addWidget(self.folder_combo_box, 1)
        folder_row.addSpacing(16)
        self.include_subfolders_check_box = SettingsBigCheckBox(
            key='validation_set_include_subfolders',
            default=DEFAULT_SETTINGS['validation_set_include_subfolders'],
            text='Include subfolders')
        folder_row.addWidget(self.include_subfolders_check_box)
        settings_layout.addLayout(folder_row)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        settings_layout.addLayout(grid)

        grid.addWidget(QLabel('Validation share:'), 1, 0)
        self.percentage_spin_box = FocusedScrollSettingsSpinBox(
            key='validation_set_percentage',
            default=DEFAULT_SETTINGS['validation_set_percentage'],
            minimum=1, maximum=50)
        self.percentage_spin_box.setSuffix('%')
        self.percentage_spin_box.setMinimumWidth(80)
        grid.addWidget(self.percentage_spin_box, 1, 1,
                       Qt.AlignmentFlag.AlignLeft)
        grid.addWidget(QLabel('Subfolder name:'), 2, 0)
        self.folder_name_edit = QLineEdit(self.settings.value(
            'validation_set_folder_name',
            DEFAULT_SETTINGS['validation_set_folder_name'], type=str)
            or DEFAULT_FOLDER_NAME)
        self.folder_name_edit.setFixedWidth(220)
        grid.addWidget(self.folder_name_edit, 2, 1)
        grid.addWidget(QLabel('Keep variants together:'), 1, 3)
        self.variant_combo_box = FocusedScrollSettingsComboBox(
            key='validation_set_variant_matching',
            default=DEFAULT_VARIANT_MATCHING)
        self.variant_combo_box.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.variant_combo_box.addItems(list(VARIANT_MATCHING_LEVELS))
        if self.variant_combo_box.currentText() not in VARIANT_MATCHING_LEVELS:
            self.variant_combo_box.setCurrentText(DEFAULT_VARIANT_MATCHING)
        grid.addWidget(self.variant_combo_box, 1, 4,
                       Qt.AlignmentFlag.AlignLeft)
        grid.setColumnStretch(2, 1)
        grid.setColumnMinimumWidth(2, 24)
        layout.addWidget(settings_box)

        pick_row = QHBoxLayout()
        pick_row.setSpacing(12)
        self.pick_button = QPushButton('Pick Images')
        self.pick_button.setMinimumWidth(110)
        self.pick_button.clicked.connect(self.pick)
        pick_row.addWidget(self.pick_button)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        pick_row.addWidget(self.status_label, 1)
        layout.addLayout(pick_row)

        coverage_row = QHBoxLayout()
        coverage_row.setSpacing(12)
        self.coverage_label = QLabel()
        coverage_row.addWidget(self.coverage_label)
        self.missed_button = QPushButton()
        self.missed_button.setCheckable(True)
        self.missed_button.toggled.connect(self._update_missed_visibility)
        coverage_row.addWidget(self.missed_button)
        coverage_row.addStretch()
        layout.addLayout(coverage_row)
        self.missed_text = QPlainTextEdit()
        self.missed_text.setReadOnly(True)
        self.missed_text.setMaximumHeight(
            self.missed_text.fontMetrics().lineSpacing() * 4 + 12)
        self.missed_text.hide()
        layout.addWidget(self.missed_text)

        show_row = QHBoxLayout()
        show_row.addWidget(QLabel('Show:'))
        self.show_combo_box = FocusedScrollComboBox()
        self.show_combo_box.addItems(SHOW_OPTIONS)
        self.show_combo_box.setCurrentText(SHOW_ALL)
        self.show_combo_box.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.show_combo_box.currentTextChanged.connect(self._refresh_view)
        show_row.addWidget(self.show_combo_box)
        self.shown_count_label = QLabel()
        show_row.addWidget(self.shown_count_label)
        show_row.addStretch()
        legend = QLabel(
            f'<span style="color:{_PICK_COLOR.name()}">\u25A0</span> picked'
            f'&nbsp;&nbsp;&nbsp;<span style="color:{_EXISTING_COLOR.name()}">'
            f'\u25A0</span> already set aside&nbsp;&nbsp;&nbsp;'
            f'\U0001F6AB never pick')
        show_row.addWidget(legend)
        layout.addLayout(show_row)

        self.thumbnail_model = _ThumbnailModel(self)
        self.list_view = QListView()
        self.list_view.setViewMode(QListView.ViewMode.IconMode)
        self.list_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.list_view.setMovement(QListView.Movement.Static)
        self.list_view.setUniformItemSizes(True)
        self.list_view.setSpacing(2)
        self.list_view.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_view.setItemDelegate(_ThumbnailDelegate(self))
        self.list_view.setModel(self.thumbnail_model)
        self.list_view.doubleClicked.connect(self._open_image)
        self.list_view.selectionModel().selectionChanged.connect(
            self._update_buttons)
        layout.addWidget(self.list_view, 1)

        review_row = QHBoxLayout()
        self.never_pick_button = QPushButton('Never Pick')
        self.never_pick_button.clicked.connect(
            lambda: self.set_never_pick(True))
        review_row.addWidget(self.never_pick_button)
        self.allow_button = QPushButton('Allow')
        self.allow_button.clicked.connect(lambda: self.set_never_pick(False))
        review_row.addWidget(self.allow_button)
        self.drop_button = QPushButton('Drop Pick')
        self.drop_button.clicked.connect(self.drop_picks)
        review_row.addWidget(self.drop_button)
        self.return_button = QPushButton('Return to Folder')
        self.return_button.clicked.connect(self.return_existing)
        review_row.addWidget(self.return_button)
        review_row.addStretch()
        self.move_button = QPushButton()
        self.move_button.clicked.connect(self.move_picks)
        review_row.addWidget(self.move_button)
        close_button = QPushButton('Close')
        close_button.clicked.connect(self.reject)
        review_row.addWidget(close_button)
        layout.addLayout(review_row)

        self._populate_folders(current_image_path)
        self.folder_combo_box.currentIndexChanged.connect(self._scope_changed)
        self.include_subfolders_check_box.stateChanged.connect(
            self._on_include_subfolders_changed)
        self.percentage_spin_box.valueChanged.connect(
            self._on_percentage_changed)
        self.folder_name_edit.textChanged.connect(
            self._on_folder_name_changed)
        self.variant_combo_box.currentTextChanged.connect(
            self._on_variant_matching_changed)
        image_list_model.dataChanged.connect(self._on_model_data_changed)
        image_list_model.rowsInserted.connect(self._structure_timer.start)
        image_list_model.rowsRemoved.connect(self._structure_timer.start)
        image_list_model.modelReset.connect(self._structure_timer.start)
        self._scope_changed()

    # ------------------------------------------------------------------
    # Scope
    # ------------------------------------------------------------------

    @property
    def folder_name(self) -> str:
        return self.folder_name_edit.text().strip()

    def _folder_name_error(self) -> str | None:
        name = self.folder_name
        if not name:
            return 'Enter a name for the validation subfolder.'
        if name in ('.', '..'):
            return f'"{name}" can\'t be used as a folder name.'
        return get_invalid_file_name_reason(name)

    def _source_folder(self) -> Path | None:
        return self.folder_combo_box.currentData()

    def _populate_folders(self, current_image_path: Path | None):
        name = self.folder_name if not self._folder_name_error() else (
            DEFAULT_FOLDER_NAME)
        paths = [image.path for image in self.image_list_model.images]
        folders = source_folders(paths, self.directory_path, name)
        selected = self._source_folder()
        if selected is None and current_image_path is not None:
            selected = Path(current_image_path).parent
            if is_in_validation_folder(Path(current_image_path),
                                       self.directory_path, name):
                selected = None
        include_subfolders = self.include_subfolders_check_box.isChecked()
        self.folder_combo_box.blockSignals(True)
        self.folder_combo_box.clear()
        selected_index = 0
        for folder in folders:
            count = sum(
                1 for path in paths
                if is_within(path, folder, include_subfolders)
                and not is_in_validation_folder(path, folder, name))
            try:
                relative = folder.relative_to(self.directory_path)
                label = (self.directory_path.name if relative == Path('.')
                         else f'{self.directory_path.name}\\{relative}')
            except ValueError:
                label = str(folder)
            self.folder_combo_box.addItem(
                f'{label}  ({count} {pluralize("image", count)})', folder)
            if (selected is not None
                    and get_path_key(folder) == get_path_key(selected)):
                selected_index = self.folder_combo_box.count() - 1
        self.folder_combo_box.setCurrentIndex(selected_index)
        self.folder_combo_box.blockSignals(False)

    def _validation_folder(self) -> Path | None:
        folder = self._source_folder()
        if folder is None or self._folder_name_error():
            return None
        return folder / self.folder_name

    @Slot()
    def _on_include_subfolders_changed(self):
        self._populate_folders(None)
        self._scope_changed()

    @Slot()
    def _on_folder_name_changed(self):
        if not self._folder_name_error():
            self.settings.setValue('validation_set_folder_name',
                                   self.folder_name)
            self._populate_folders(None)
        self._scope_changed()

    @Slot()
    def _on_variant_matching_changed(self):
        self._units = None
        self._clear_picks()
        self._update_all()

    @Slot()
    def _on_percentage_changed(self):
        if self._units is not None and self._picked_units:
            self._pick_from_scratch()
        else:
            self._update_all()

    @Slot()
    def _scope_changed(self):
        """Gather the images of the chosen folder: the candidates and the
        ones already in its validation subfolder."""
        self._outside_change_note = ''
        self._units = None
        self._dropped_items.clear()
        self._clear_picks()
        self.items = []
        self._candidate_count = 0
        folder = self._source_folder()
        validation_folder = self._validation_folder()
        if folder is not None and validation_folder is not None:
            include_subfolders = self.include_subfolders_check_box.isChecked()
            name = self.folder_name
            candidates = []
            existing = []
            for image in self.image_list_model.images:
                path = image.path
                if not is_within(path, folder, include_subfolders=True):
                    continue
                if is_in_validation_folder(path, folder, name):
                    if include_subfolders or is_within(
                            path, validation_folder,
                            include_subfolders=True):
                        existing.append(image)
                elif is_within(path, folder, include_subfolders):
                    candidates.append(image)
            self.items = candidates + existing
            self._candidate_count = len(candidates)
        self._model_rows = {id(image): row for row, image
                            in enumerate(self.image_list_model.images)}
        self.show_combo_box.setCurrentText(SHOW_ALL)
        self._update_all()

    def _clear_picks(self):
        self._picked_units = []
        self._picked_items = set()

    # ------------------------------------------------------------------
    # Item info (used by the thumbnail grid)
    # ------------------------------------------------------------------

    def thumbnail(self, item: int):
        row = self._model_rows.get(id(self.items[item]))
        if row is None:
            return None
        return self.image_list_model.data(self.image_list_model.index(row),
                                          Qt.ItemDataRole.DecorationRole)

    def item_state(self, item: int) -> int:
        if item >= self._candidate_count:
            return _STATE_EXISTING
        if item in self._picked_items:
            return _STATE_PICKED
        return _STATE_NONE

    def is_never_pick(self, item: int) -> bool:
        return (item < self._candidate_count
                and self.never_pick.is_marked(self.items[item].path))

    def unit_size(self, item: int) -> int:
        unit = self._unit_of_item.get(item)
        if self._units is None or unit is None:
            return 1
        return len(self._units[unit])

    @Slot(QModelIndex, QModelIndex)
    def _on_model_data_changed(self, *_args):
        # Thumbnails load in the background in the main image list; captions
        # can also be changed outside the app.
        self.list_view.viewport().update()
        self._tags_timer.start()

    def changeEvent(self, event):
        super().changeEvent(event)
        if (event.type() == QEvent.Type.ActivationChange
                and self.isActiveWindow()):
            # The main window only rescans when it is activated, which
            # doesn't happen while this dialog is open.
            self.never_pick.forget()
            if callable(self.refresh_callback):
                self.refresh_callback()
            self._update_status()
            self.list_view.viewport().update()

    @Slot()
    def _on_images_added_or_removed(self):
        """Images were added, removed, renamed or moved outside the app (or
        by the main window): start over with the current folder contents."""
        had_picks = bool(self._picked_items)
        if self._scanner is not None:
            self._scanner.cancel()
        self._populate_folders(None)
        self._scope_changed()
        self._outside_change_note = (
            'The folder changed outside the app, so the picks were cleared.'
            if had_picks else '')
        self._update_status()

    @Slot(QModelIndex)
    def _open_image(self, index: QModelIndex):
        item = index.data(Qt.ItemDataRole.UserRole)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.items[item].path)))

    # ------------------------------------------------------------------
    # Picking
    # ------------------------------------------------------------------

    def _target(self) -> int:
        return target_count(len(self.items), self.percentage_spin_box.value())

    def _existing_items(self) -> list[int]:
        return list(range(self._candidate_count, len(self.items)))

    def _unit_eligible(self, unit: list[int]) -> bool:
        return not any(item in self._dropped_items or self.is_never_pick(item)
                       for item in unit)

    @Slot()
    def pick(self):
        self._outside_change_note = ''
        if self._candidate_count == 0:
            return
        threshold = VARIANT_MATCHING_LEVELS.get(
            self.variant_combo_box.currentText(), 0)
        candidates = self.items[:self._candidate_count]
        scan_key = (tuple(get_path_key(image.path) for image in candidates),
                    threshold)
        if self._units is not None and scan_key == self._scan_key:
            self._pick_from_scratch()
            return
        if self._candidate_count < 2:
            self._set_groups(scan_key, [])
            return
        self.pick_button.setEnabled(False)
        progress = QProgressDialog('Looking for variants of the same image...',
                                   'Cancel', 0, len(candidates), self)
        progress.setWindowTitle('Create Validation Set')
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(400)
        progress.setAutoReset(False)
        progress.setValue(0)
        self._progress_dialog = progress
        scanner = DuplicateScanner(candidates, threshold, self._cache)
        scanner.scan_key = scan_key
        self._scanner = scanner
        scanner.progress.connect(self._on_scan_progress)
        scanner.finished.connect(self._on_scan_finished)
        progress.canceled.connect(scanner.cancel)
        _get_scanner_keeper().start(scanner)

    @Slot(str, int, int)
    def _on_scan_progress(self, stage: str, done: int, total: int):
        progress = self._progress_dialog
        if progress is None:
            return
        if stage == 'comparing' and progress.maximum() != total:
            progress.setLabelText('Comparing images\u2026')
            progress.setRange(0, max(1, total))
        progress.setValue(done)

    @Slot(list)
    def _on_scan_finished(self, groups: list):
        scanner = self._scanner
        progress = self._progress_dialog
        self._scanner = None
        self._progress_dialog = None
        if progress is not None:
            progress.reset()
        self.pick_button.setEnabled(True)
        if scanner is None or scanner.cancelled:
            return
        self._set_groups(scanner.scan_key, groups)

    def _set_groups(self, scan_key, groups: list):
        index_by_key = {get_path_key(image.path): item for item, image
                        in enumerate(self.items[:self._candidate_count])}
        item_groups = []
        for group in groups:
            members = [index_by_key.get(get_path_key(member.path))
                       for member in group.members]
            members = [member for member in members if member is not None]
            if len(members) > 1:
                item_groups.append(members)
        self._units = build_units(self._candidate_count, item_groups)
        self._unit_of_item = {item: unit_index for unit_index, unit
                              in enumerate(self._units) for item in unit}
        self._scan_key = scan_key
        self._pick_from_scratch()

    def _pick_from_scratch(self):
        self._clear_picks()
        self._fill_picks()
        self.show_combo_box.setCurrentText(SHOW_PICKS)
        self._update_all()

    def _fill_picks(self):
        """Top up the picks to the target, keeping the current ones."""
        if self._units is None:
            return
        item_tags = [list(image.tags) for image in self.items]
        fixed = self._existing_items() + sorted(self._picked_items)
        picked_units = set(self._picked_units)
        eligible = [unit_index not in picked_units
                    and self._unit_eligible(unit)
                    for unit_index, unit in enumerate(self._units)]
        new_units = pick_units(item_tags, self._units, eligible, fixed,
                               self._target(), self.rng)
        for unit_index in new_units:
            self._picked_units.append(unit_index)
            self._picked_items.update(self._units[unit_index])

    def _remove_picked_units(self, items):
        units = {self._unit_of_item[item] for item in items
                 if item in self._picked_items}
        if not units:
            return False
        self._picked_units = [unit for unit in self._picked_units
                              if unit not in units]
        self._picked_items = {item for unit in self._picked_units
                              for item in self._units[unit]}
        return True

    def _selected_items(self) -> list[int]:
        return [index.data(Qt.ItemDataRole.UserRole)
                for index in self.list_view.selectionModel().selectedIndexes()]

    def set_never_pick(self, marked: bool):
        items = [item for item in self._selected_items()
                 if item < self._candidate_count]
        if not items:
            return
        try:
            self.never_pick.set_marked(
                [self.items[item].path for item in items], marked)
        except OSError as exception:
            QMessageBox.critical(
                self, 'Create Validation Set',
                f'The "never pick" marks could not be saved:\n\n{exception}')
            return
        if marked and self._units is not None:
            affected = [member for item in items
                        for member in self._units[self._unit_of_item[item]]]
            if self._remove_picked_units(affected):
                self._fill_picks()
        self._update_all()

    @Slot()
    def drop_picks(self):
        items = [item for item in self._selected_items()
                 if item in self._picked_items]
        if not items:
            return
        self._dropped_items.update(items)
        self._remove_picked_units(items)
        self._fill_picks()
        self._update_all()

    # ------------------------------------------------------------------
    # Moving
    # ------------------------------------------------------------------

    @Slot()
    def move_picks(self):
        validation_folder = self._validation_folder()
        if validation_folder is None or not self._picked_items:
            return
        paths = [self.items[item].path for item in sorted(self._picked_items)]
        count = len(paths)
        reply = QMessageBox.question(
            self, 'Create Validation Set',
            f'Move {count} {pluralize("image", count)} and '
            f'{"its caption" if count == 1 else "their captions"} into '
            f'{validation_folder}?')
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            validation_folder.mkdir(parents=True, exist_ok=True)
        except OSError as exception:
            QMessageBox.critical(
                self, 'Create Validation Set',
                f'The folder {validation_folder} could not be created:\n\n'
                f'{exception}')
            return
        all_paths = [image.path for image in self.image_list_model.images]
        transferred = transfer_images(self, paths, validation_folder,
                                      is_move=True, all_image_paths=all_paths,
                                      description='picked')
        if not transferred:
            return
        self.accept()
        if callable(self.reload_callback):
            self.reload_callback()

    @Slot()
    def return_existing(self):
        """Move the selected images already set aside (with their captions)
        back to the folder their validation subfolder is in."""
        folder = self._source_folder()
        items = [item for item in self._selected_items()
                 if item >= self._candidate_count]
        if folder is None or not items or self._folder_name_error():
            return
        destinations: dict[str, tuple[Path, list[Path]]] = {}
        for item in sorted(items):
            path = self.items[item].path
            destination = return_destination(path, folder, self.folder_name)
            destinations.setdefault(get_path_key(destination),
                                    (destination, []))[1].append(path)
        count = len(items)
        if len(destinations) == 1:
            target = f'{next(iter(destinations.values()))[0]}'
        else:
            target = 'the folders their validation subfolders are in'
        reply = QMessageBox.question(
            self, 'Create Validation Set',
            f'Move {count} {pluralize("image", count)} and '
            f'{"its caption" if count == 1 else "their captions"} back '
            f'into {target}?')
        if reply != QMessageBox.StandardButton.Yes:
            return
        all_paths = [image.path for image in self.image_list_model.images]
        transferred = False
        for destination, paths in destinations.values():
            if transfer_images(self, paths, destination, is_move=True,
                               all_image_paths=all_paths,
                               description='selected'):
                transferred = True
        if not transferred:
            return
        self.accept()
        if callable(self.reload_callback):
            self.reload_callback()

    def done(self, result: int):
        if self._scanner is not None:
            self._scanner.cancel()
            self._scanner = None
        try:
            self.image_list_model.dataChanged.disconnect(
                self._on_model_data_changed)
            for signal in (self.image_list_model.rowsInserted,
                           self.image_list_model.rowsRemoved,
                           self.image_list_model.modelReset):
                signal.disconnect(self._structure_timer.start)
        except (RuntimeError, TypeError):
            pass
        self._structure_timer.stop()
        self._tags_timer.stop()
        super().done(result)

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def _update_all(self):
        self._update_status()
        self._refresh_view()

    def _update_status(self):
        name_error = self._folder_name_error()
        self.coverage_label.clear()
        self._missed_tags_text = ''
        self._update_missed_visibility()
        if name_error:
            self.status_label.setText(name_error)
            self._update_buttons()
            return
        name = self.folder_name
        total = len(self.items)
        candidates = self._candidate_count
        for image in self.items[:candidates]:
            self.never_pick.is_marked(image.path)
        existing = total - candidates
        target = self._target()
        percentage = self.percentage_spin_box.value()
        if candidates == 0 and existing == 0:
            self.status_label.setText('This folder has no images.')
            self._update_buttons()
            return
        parts = [f'{total} {pluralize("image", total)}',
                 f'{percentage}% = {target}']
        if existing:
            parts.append(f'{existing} already in "{name}"')
        needed = max(0, target - existing)
        if needed == 0:
            parts.append('no more needed')
        elif self._units is None:
            parts.append(f'{needed} to pick')
        else:
            picked = len(self._picked_items)
            parts.append(f'picked {picked} of {needed}')
            self.pick_button.setText('Re-roll')
        text = '  \u00b7  '.join(parts)
        if (needed and self._units is not None
                and len(self._picked_items) < needed):
            text += ('\nThe rest can\'t be picked without going over, or '
                     'without images marked "never pick" or dropped.')
        if self._units is None:
            self.pick_button.setText('Pick Images')
        if self._outside_change_note:
            text += '\n' + self._outside_change_note
        for file_path in self.never_pick.unreadable.values():
            text += (f'\nCouldn\'t read {file_path}, so its "never pick" '
                     f'marks are ignored. Fix or delete it.')
        self.status_label.setText(text)
        self._update_coverage()
        self._update_buttons()

    def _update_coverage(self):
        if self._units is None and not self._existing_items():
            return
        item_tags = [list(image.tags) for image in self.items]
        in_set = self._existing_items() + sorted(self._picked_items)
        minimum, expected, missed = tag_coverage(item_tags, in_set,
                                                 self._target())
        if not expected:
            self.coverage_label.setText(
                'No tag is on enough images to expect it in a set this size.')
            return
        covered = len(expected) - len(missed)
        self.coverage_label.setText(
            f'Tags covered: {covered} of {len(expected)}')
        if missed:
            self._missed_tags_text = (
                f'Tags on at least {minimum} {pluralize("image", minimum)} '
                f'are expected in a set this size. Missed:\n'
                + ', '.join(missed))
            self.missed_button.setText(
                f'Missed Tags ({len(missed)})')
        self._update_missed_visibility()

    @Slot()
    def _update_missed_visibility(self):
        has_missed = bool(self._missed_tags_text)
        self.missed_button.setVisible(has_missed)
        self.missed_text.setPlainText(self._missed_tags_text)
        self.missed_text.setVisible(has_missed
                                    and self.missed_button.isChecked())

    @Slot()
    def _refresh_view(self):
        show = self.show_combo_box.currentText()
        if show == SHOW_PICKS:
            items = sorted(self._picked_items)
        elif show == SHOW_NEVER_PICK:
            items = [item for item in range(self._candidate_count)
                     if self.is_never_pick(item)]
        elif show == SHOW_EXISTING:
            items = self._existing_items()
        else:
            items = list(range(len(self.items)))
        self.thumbnail_model.set_items(items)
        self.shown_count_label.setText(
            f'({len(items)} {pluralize("image", len(items))})')
        self._update_buttons()

    @Slot()
    def _update_buttons(self):
        selected = self._selected_items()
        candidates = [item for item in selected
                      if item < self._candidate_count]
        self.never_pick_button.setEnabled(
            any(not self.is_never_pick(item) for item in candidates))
        self.allow_button.setEnabled(
            any(self.is_never_pick(item) for item in candidates))
        self.drop_button.setEnabled(
            any(item in self._picked_items for item in selected))
        self.return_button.setEnabled(
            not self._folder_name_error()
            and any(item >= self._candidate_count for item in selected))
        self.pick_button.setEnabled(
            self._scanner is None and self._candidate_count > 0
            and not self._folder_name_error())
        count = len(self._picked_items)
        name = self.folder_name or DEFAULT_FOLDER_NAME
        self.move_button.setText(
            f'Move {count} {pluralize("Image", count)} to "{name}"')
        self.move_button.setEnabled(count > 0
                                    and not self._folder_name_error())
