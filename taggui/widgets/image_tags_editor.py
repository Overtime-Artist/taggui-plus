from typing import Optional
from PySide6.QtCore import (QEvent, QItemSelectionModel, QModelIndex, QPoint,
                            QPropertyAnimation, QStringListModel, Qt, QTimer,
                            Signal, Slot)
from PySide6.QtGui import QColor, QFocusEvent, QKeyEvent, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox,
                               QCompleter, QDockWidget,
                               QDialog, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListView, QMenu, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)
from transformers import PreTrainedTokenizerBase

from models.image_list_model import ImageListModel
from models.tag_library_model import TagLibraryModel
from models.proxy_image_list_model import ProxyImageListModel
from utils.elided_tooltip import ElidedToolTipListView
from utils.image import Image, build_caption_text
from utils.settings import DEFAULT_SETTINGS, get_settings, get_tag_separator
from utils.settings_widgets import SettingsComboBox
from utils.text_edit_item_delegate import TextEditItemDelegate
from utils.utils import get_confirmation_dialog_reply
from widgets.image_list import ImageList


class CompleterPopupList(QListView):
    def viewportEvent(self, event):
        if event.type() == QEvent.Type.ToolTip:
            return True
        return super().viewportEvent(event)


def get_new_tag_library_tags(tag_library_model: TagLibraryModel,
                       tags: list[str]) -> list[str]:
    new_tags = []
    seen_tags = set()
    for tag in tags:
        normalized_tag = tag.strip()
        if (not normalized_tag
                or normalized_tag in seen_tags
                or tag_library_model.has_tag(normalized_tag)):
            continue
        new_tags.append(normalized_tag)
        seen_tags.add(normalized_tag)
    return new_tags


def show_category_assignment_prompt(parent: QWidget,
                                    tag_library_model: TagLibraryModel,
                                    new_tags: list[str],
                                    default_category_id: str = ''):
    if not new_tags:
        return
    categories = tag_library_model.get_categories()
    prompt_parent = parent.window() if parent is not None else None
    normalized_tags = []
    seen_tags = set()
    for tag in new_tags:
        normalized_tag = tag.strip()
        if not normalized_tag or normalized_tag in seen_tags:
            continue
        normalized_tags.append(normalized_tag)
        seen_tags.add(normalized_tag)
    if not normalized_tags:
        return

    category_options = [('No category', '')]
    category_options.extend(
        [(category['name'], category['id']) for category in categories])

    def create_category_combo_box(combo_parent: QWidget) -> QComboBox:
        combo_box = QComboBox(combo_parent)
        for category_name, category_id in category_options:
            combo_box.addItem(category_name, category_id)
        default_index = combo_box.findData(default_category_id)
        combo_box.setCurrentIndex(default_index if default_index >= 0 else 0)
        return combo_box

    if len(normalized_tags) == 1:
        tag = normalized_tags[0]
        dialog = QDialog(prompt_parent)
        dialog.setWindowTitle('Assign Tag Category')
        layout = QVBoxLayout(dialog)
        label = QLabel(f'Assign a category to the new tag "{tag}"?')
        combo_box = create_category_combo_box(dialog)
        confirm_button = QPushButton('Confirm', dialog)
        confirm_button.setDefault(True)
        confirm_button.setAutoDefault(True)
        confirm_button.clicked.connect(dialog.accept)
        layout.addWidget(label)
        layout.addWidget(combo_box)
        layout.addWidget(confirm_button)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        category_id = combo_box.currentData()
        if not category_id:
            tag_library_model.clear_category([tag])
            return
        tag_library_model.assign_category([tag], category_id)
        return

    dialog = QDialog(prompt_parent)
    dialog.setWindowTitle('Assign Tag Category')
    layout = QVBoxLayout(dialog)
    label = QLabel('Assign categories to the new tags?')
    scroll_area = QScrollArea(dialog)
    scroll_area.setWidgetResizable(True)
    scroll_area.setMinimumHeight(180)
    scroll_area.setMaximumHeight(320)
    table_container = QWidget(scroll_area)
    grid_layout = QGridLayout(table_container)
    grid_layout.setContentsMargins(6, 6, 6, 6)
    grid_layout.setHorizontalSpacing(12)
    grid_layout.setVerticalSpacing(8)
    combo_box_by_tag = {}
    for row, tag in enumerate(normalized_tags):
        tag_label = QLabel(tag, table_container)
        combo_box = create_category_combo_box(table_container)
        grid_layout.addWidget(tag_label, row, 0)
        grid_layout.addWidget(combo_box, row, 1)
        combo_box_by_tag[tag] = combo_box
    scroll_area.setWidget(table_container)
    confirm_button = QPushButton('Confirm', dialog)
    confirm_button.setDefault(True)
    confirm_button.setAutoDefault(True)
    confirm_button.clicked.connect(dialog.accept)
    layout.addWidget(label)
    layout.addWidget(scroll_area)
    layout.addWidget(confirm_button)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return
    tags_to_clear = []
    tags_by_category_id = {}
    for tag, combo_box in combo_box_by_tag.items():
        category_id = combo_box.currentData()
        if not category_id:
            tags_to_clear.append(tag)
            continue
        tags_by_category_id.setdefault(category_id, []).append(tag)
    if tags_to_clear:
        tag_library_model.clear_category(tags_to_clear)
    for category_id, tags_for_category in tags_by_category_id.items():
        tag_library_model.assign_category(tags_for_category, category_id)


def add_new_tag_library_tags_with_prompt(parent: QWidget,
                                   tag_library_model: TagLibraryModel,
                                   tags: list[str]) -> list[str]:
    new_tags = get_new_tag_library_tags(tag_library_model, tags)
    if not new_tags:
        return []
    tag_library_model.add_tags(new_tags)
    show_category_assignment_prompt(parent, tag_library_model, new_tags)
    return new_tags


def get_completion_tag(completion: str) -> str:
    """Return the underlying tag for a completer entry, stripping the
    ``alias → canonical`` decoration used for aliases."""
    if ' → ' in completion:
        return completion.split(' → ', 1)[1]
    return completion


class TagCompleter(QCompleter):
    """Autocomplete completer for the tag input box.

    Suggests every tag/alias that *contains* the typed text (case-insensitive),
    but orders the suggestions so the most relevant appear first:
      1. entries that start with the typed text (shortest first), then
      2. entries that merely contain the typed text.
    The first popup entry is therefore the best match, which keeps the
    Ctrl+Enter "add first suggestion" shortcut intuitive.
    """

    def __init__(self, model: QStringListModel, parent=None):
        super().__init__(model, parent)
        self._all_candidates: list[str] = []
        # Guards against re-entrant reordering while `setStringList` below emits
        # a model-reset that can ask the completer to re-filter.
        self._reordering = False
        self.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setFilterMode(Qt.MatchFlag.MatchContains)

    def set_candidates(self, candidates: list[str]):
        """Store the full list of tags/aliases and seed the source model."""
        self._all_candidates = list(candidates)
        self.model().setStringList(self._all_candidates)

    def splitPath(self, path: str) -> list[str]:
        # Reorder the candidate list on every keystroke so the completer's
        # "contains" filter returns the most relevant suggestions first.
        if not self._reordering:
            text = path.strip().lower()
            if text:
                def sort_key(candidate: str):
                    lowered = candidate.lower()
                    starts_with = lowered.startswith(text)
                    # starts-with entries (group 0) before contains-only
                    # entries (group 1); within each group shorter first, then
                    # alphabetical for a stable order.
                    return (0 if starts_with else 1, len(candidate), lowered)
                ordered = sorted(self._all_candidates, key=sort_key)
                if ordered != self.model().stringList():
                    self._reordering = True
                    try:
                        self.model().setStringList(ordered)
                    finally:
                        self._reordering = False
        return super().splitPath(path)


class CompleterModel(QStringListModel):
    """A string list model for the autocomplete popup that colors each
    suggestion with its tag category color (matching the tags list)."""

    def __init__(self, tag_library_model: TagLibraryModel, parent=None):
        super().__init__(parent)
        self.tag_library_model = tag_library_model

    def data(self, index: QModelIndex,
             role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.ForegroundRole and index.isValid():
            completion = super().data(index, Qt.ItemDataRole.DisplayRole)
            if completion:
                tag = get_completion_tag(completion)
                category = self.tag_library_model.get_category_for_tag(tag)
                if category:
                    color = QColor(category['color'])
                    if color.isValid():
                        return color
        return super().data(index, role)


class TagInputBox(QLineEdit):
    # Duration in milliseconds of the border flash / shake feedback.
    FLASH_DURATION_MS = 350

    tags_addition_requested = Signal(list, list)
    # Emitted with the tags that were genuinely new to the Tag Library when
    # they were added here (i.e. added to an image via the Image Tags pane).
    # This lets the main window queue the category-assignment prompt for those
    # tags, since this widget inserts them into the library before the tag
    # counter is updated.
    new_library_tags_added = Signal(list)

    def __init__(self, image_tag_list_model: QStringListModel,
                 tag_library_model: TagLibraryModel, image_list: ImageList,
                 tag_separator: str):
        super().__init__()
        self.image_tag_list_model = image_tag_list_model
        self.tag_library_model = tag_library_model
        self.image_list = image_list
        self.tag_separator = tag_separator

        self.setPlaceholderText('Add Tag')
        self.setTextMargins(8, 0, 8, 0)
        self.completer_model = None
        self.completer = None
        # State for the border-flash / shake feedback animations.
        self._shake_animation: Optional[QPropertyAnimation] = None
        self._flash_timer: Optional[QTimer] = None
        # Set by ImageTagsEditor once the tags list exists. Used to return
        # focus to the tags list after a tag is added.
        self.image_tags_list = None
        # True when this box was auto-focused by typing in the tags list (see
        # ImageTagsList.keyPressEvent). When set, focus returns to the tags list
        # after the tag is added. Cleared once focus leaves the box for any
        # other reason, so a later manual edit doesn't wrongly jump focus.
        self._return_focus_to_tags_list_after_add = False
        # Connect the library-change signals once. The refresh handler is a
        # no-op while autocomplete is disabled (guarded by `completer_model`),
        # so it is safe to keep connected even when the completer is torn down.
        self.tag_library_model.modelReset.connect(
            self._refresh_completer_candidates)
        self.tag_library_model.aliases_changed.connect(
            self._refresh_completer_candidates)
        # Color the typed text with its tag category color, and refresh it
        # whenever the text or the category assignments change.
        self.textChanged.connect(self._update_input_text_color)
        self.tag_library_model.modelReset.connect(
            self._update_input_text_color)
        self.tag_library_model.categories_changed.connect(
            self._update_input_text_color)
        settings = get_settings()
        autocomplete_tags = settings.value(
            'autocomplete_tags',
            defaultValue=DEFAULT_SETTINGS['autocomplete_tags'], type=bool)
        self.set_autocomplete_enabled(autocomplete_tags)

    def set_autocomplete_enabled(self, enabled: bool):
        """Create or tear down the autocomplete completer at runtime so the
        "Show tag autocomplete suggestions" setting applies without a restart.
        """
        if enabled:
            if self.completer is not None:
                return
            self.completer_model = CompleterModel(self.tag_library_model, self)
            self.completer = TagCompleter(self.completer_model, self)
            self.completer.setPopup(CompleterPopupList())
            self.setCompleter(self.completer)
            self._refresh_completer_candidates()
            self.completer.activated[str].connect(
                self._on_completion_activated)
        else:
            if self.completer is None:
                return
            # `setCompleter(None)` deletes the completer the line edit owns, so
            # we must not delete it again ourselves.
            self.setCompleter(None)
            self.completer = None
            self.completer_model = None

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() not in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            super().keyPressEvent(event)
            return
        # If Ctrl+Enter is pressed and the completer is visible, add the first
        # tag in the completer popup.
        if (event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and self.completer is not None
                and self.completer.popup().isVisible()):
            first_tag = self.completer.popup().model().data(
                self.completer.model().index(0, 0), Qt.ItemDataRole.EditRole)
            self.add_tag(self._completion_to_tag(first_tag))
        # Otherwise, add the tag in the input box.
        else:
            self.add_tag(self._completion_to_tag(self.text()))
        self.clear()
        if self.completer is not None:
            self.completer.popup().hide()
        self._return_focus_to_tags_list_if_needed()

    def _on_completion_activated(self, text: str):
        """Handle the user picking an autocomplete suggestion (e.g. by clicking
        it or pressing Enter while the popup is highlighted)."""
        self.add_tag(self._completion_to_tag(text))
        self._return_focus_to_tags_list_if_needed()

    def _return_focus_to_tags_list_if_needed(self):
        """Return keyboard focus to the tags list when this box was auto-focused
        by typing there. The flag is read and cleared before moving focus."""
        should_return = self._return_focus_to_tags_list_after_add
        self._return_focus_to_tags_list_after_add = False
        if should_return and self.image_tags_list is not None:
            self.image_tags_list.setFocus()

    def focusOutEvent(self, event: QFocusEvent):
        super().focusOutEvent(event)
        # Drop the auto-focus flag whenever focus genuinely leaves the box, so a
        # later manual edit won't jump focus back to the tags list. The
        # autocomplete popup does not count: it steals focus with
        # PopupFocusReason, and we still want the flag when a suggestion there
        # is what adds the tag.
        if event.reason() != Qt.FocusReason.PopupFocusReason:
            self._return_focus_to_tags_list_after_add = False

    def add_tag(self, tag: str):
        tag = tag.strip()
        if not tag:
            return
        tags = [t.strip() for t in tag.split(self.tag_separator) if t.strip()]
        selected_image_indices = self.image_list.get_selected_image_indices()
        selected_image_count = len(selected_image_indices)
        if len(tags) == 1 and selected_image_count == 1:
            resolved_tag = tags[0]
            if resolved_tag in self.image_tag_list_model.stringList():
                # The tag is already on the image: reject it and give the user
                # visual feedback instead of silently doing nothing.
                self._flash_duplicate()
                self.clear()
                return
            self.add_new_tags_to_library([resolved_tag])
            # Add an empty tag and set it to the new tag.
            self.image_tag_list_model.insertRow(
                self.image_tag_list_model.rowCount())
            new_tag_index = self.image_tag_list_model.index(
                self.image_tag_list_model.rowCount() - 1)
            self.image_tag_list_model.setData(new_tag_index, resolved_tag)
            settings = get_settings()
            apply_mode = settings.value(
                'auto_apply_implications',
                defaultValue=DEFAULT_SETTINGS['auto_apply_implications'])
            if isinstance(apply_mode, bool):
                apply_mode = 'Single image only' if apply_mode else 'Off'
            if apply_mode in ('Single image only', 'All selected images'):
                implied = self.tag_library_model.get_implied_tags([resolved_tag])
                existing = set(self.image_tag_list_model.stringList())
                new_implied = [t for t in implied if t not in existing]
                if new_implied:
                    current = self.image_tag_list_model.stringList()
                    self.image_tag_list_model.setStringList(current + new_implied)
                    self.add_new_tags_to_library(new_implied)
            self.flash_added_feedback()
            return
        if selected_image_count > 1:
            if len(tags) > 1:
                question = (f'Add tags to {selected_image_count} selected '
                            f'images?')
            else:
                question = (f'Add tag "{tags[0]}" to {selected_image_count} '
                            f'selected images?')
            reply = get_confirmation_dialog_reply(title='Add Tag',
                                                  question=question)
            if reply != QMessageBox.StandardButton.Yes:
                return
        settings = get_settings()
        apply_mode = settings.value(
            'auto_apply_implications',
            defaultValue=DEFAULT_SETTINGS['auto_apply_implications'])
        if isinstance(apply_mode, bool):
            apply_mode = 'Single image only' if apply_mode else 'Off'
        tags_to_add = list(tags)
        if apply_mode == 'All selected images':
            implied = self.tag_library_model.get_implied_tags(tags)
            tags_to_add = tags + [t for t in implied if t not in tags]
        self.add_new_tags_to_library(tags_to_add)
        self.tags_addition_requested.emit(tags_to_add, selected_image_indices)
        self.flash_added_feedback()

    def _new_tag_auto_select_disabled(self) -> bool:
        """Whether the "Do not auto-select newly added tags" setting is on."""
        return get_settings().value(
            'disable_new_tag_auto_select',
            defaultValue=DEFAULT_SETTINGS['disable_new_tag_auto_select'],
            type=bool)

    def add_new_tags_to_library(self, tags: list[str]):
        new_library_tags = [tag for tag in tags
                            if not self.tag_library_model.has_tag(tag)]
        self.tag_library_model.add_tags(tags)
        if new_library_tags:
            self.new_library_tags_added.emit(new_library_tags)

    @Slot()
    def _refresh_completer_candidates(self):
        if self.completer_model is None or self.completer is None:
            return
        completions = list(self.tag_library_model.tags)
        completions.extend(
            f'{alias} → {canonical}'
            for alias, canonical in sorted(self.tag_library_model.get_aliases().items()))
        self.completer.set_candidates(completions)

    def _update_input_text_color(self):
        """Color the input text with the category color of the typed tag.

        The color is applied via the palette's Text role (not a stylesheet)
        so the "Add Tag" placeholder keeps its default color.
        """
        tag = self._completion_to_tag(self.text().strip())
        color = None
        if tag:
            category = self.tag_library_model.get_category_for_tag(tag)
            if category:
                candidate = QColor(category['color'])
                if candidate.isValid():
                    color = candidate
        palette = self.palette()
        if color is None:
            # Restore the current theme's default text color.
            color = QApplication.palette(self).color(QPalette.ColorRole.Text)
        palette.setColor(QPalette.ColorRole.Text, color)
        self.setPalette(palette)

    def _flash_duplicate(self):
        """Signal a rejected duplicate tag by briefly turning the input box
        border red and shaking it, then restoring the normal border.
        """
        # A red border for the duration of the shake gives clear "rejected"
        # feedback. Restoring an empty stylesheet brings back the native focus
        # (blue) border afterwards.
        self._flash_border('#e03c3c')
        self._start_shake()

    def _flash_success(self):
        """Signal a successfully added tag by briefly turning the input box
        border green (no shake), then restoring the normal border. Used when
        newly added tags are not auto-selected, so the user still gets
        feedback that the tag was added.
        """
        self._flash_border('#3cc23c')

    def flash_added_feedback(self):
        """Public hook for external tag-adding flows (e.g. the All Tags pane
        and the wiki dialogs). Flashes the Add Tag box green when the user has
        disabled auto-selecting newly added tags, so every add path gives the
        same feedback.
        """
        if self._new_tag_auto_select_disabled():
            self._flash_success()

    def _flash_border(self, color: str):
        """Turn the input box border `color` for `FLASH_DURATION_MS`, then
        restore the normal border.
        """
        self.setStyleSheet(f'QLineEdit {{ border: 1px solid {color}; }}')
        if self._flash_timer is not None:
            self._flash_timer.stop()
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._restore_border)
        self._flash_timer.start(self.FLASH_DURATION_MS)

    def _restore_border(self):
        """Remove the temporary colored border and force the widget's style to
        repaint, so the native (Fusion) border returns immediately.
        """
        self.setStyleSheet('')
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def _start_shake(self):
        """Nudge the input box left and right a few times to draw attention."""
        if self._shake_animation is not None:
            self._shake_animation.stop()
        original_pos = self.pos()
        animation = QPropertyAnimation(self, b'pos', self)
        animation.setDuration(self.FLASH_DURATION_MS)
        amplitude = 6
        offsets = [0, amplitude, -amplitude, amplitude, -amplitude,
                   amplitude // 2, -amplitude // 2, 0]
        last_index = len(offsets) - 1
        for index, offset in enumerate(offsets):
            animation.setKeyValueAt(index / last_index,
                                    original_pos + QPoint(offset, 0))
        # Ensure the box ends up exactly where it started even if the layout
        # shifted it mid-animation.
        animation.finished.connect(lambda: self.move(original_pos))
        self._shake_animation = animation
        animation.start()

    def _completion_to_tag(self, completion: str) -> str:
        return get_completion_tag(completion)


class NaturalLanguageTextEdit(QPlainTextEdit):
    editing_finished = Signal()

    def __init__(self):
        super().__init__()
        self._is_dirty = False
        self.setPlaceholderText('Natural language prompt')
        self.textChanged.connect(self.mark_dirty)

    @Slot()
    def mark_dirty(self):
        self._is_dirty = True

    def focusOutEvent(self, event):
        if self._is_dirty:
            self.editing_finished.emit()
            self._is_dirty = False
        super().focusOutEvent(event)

    def set_clean_plain_text(self, text: str):
        self.blockSignals(True)
        self.setPlainText(text)
        self.blockSignals(False)
        self._is_dirty = False

    def mark_clean(self):
        self._is_dirty = False


class ImageTagsList(ElidedToolTipListView):
    danbooru_wiki_requested = Signal(str)
    gelbooru_wiki_requested = Signal(str)

    def __init__(self, image_tag_list_model: QStringListModel,
                 tag_library_model: TagLibraryModel):
        super().__init__()
        self.image_tag_list_model = image_tag_list_model
        self.tag_library_model = tag_library_model
        # Set by ImageTagsEditor once the Add Tag box exists. Used to redirect
        # typing in the tag list to the Add Tag box (see keyPressEvent).
        self.tag_input_box = None
        self.setModel(self.image_tag_list_model)
        self.setItemDelegate(TextEditItemDelegate(self))
        self.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setWordWrap(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        # Show the selected tag with the active (blue) highlight color even when
        # keyboard focus is elsewhere (e.g. still in the Add Tag box). Without
        # this, Qt paints the selection with the muted "inactive" color, so a
        # newly added tag would not appear highlighted.
        self._sync_inactive_selection_colors()

    def _sync_inactive_selection_colors(self):
        palette = self.palette()
        for role in (QPalette.ColorRole.Highlight,
                     QPalette.ColorRole.HighlightedText):
            active_color = palette.color(QPalette.ColorGroup.Active, role)
            palette.setColor(QPalette.ColorGroup.Inactive, role, active_color)
        self.setPalette(palette)

    def changeEvent(self, event):
        super().changeEvent(event)
        # Reapply after a theme/palette change, but guard against the recursion
        # that our own setPalette() call would otherwise trigger.
        if (event.type() == QEvent.Type.PaletteChange
                and not getattr(self, '_syncing_selection_colors', False)):
            self._syncing_selection_colors = True
            try:
                self._sync_inactive_selection_colors()
            finally:
                self._syncing_selection_colors = False

    @Slot(QPoint)
    def show_context_menu(self, position: QPoint):
        index = self.indexAt(position)
        if not index.isValid():
            return
        tag = index.data(Qt.ItemDataRole.DisplayRole)
        if not tag:
            return

        categories = self.tag_library_model.get_categories()
        current_category = self.tag_library_model.get_category_for_tag(
            str(tag))

        context_menu = QMenu(self)
        selected_tag_count = len(self.selectedIndexes())
        copy_action = context_menu.addAction(
            'Copy Tags' if selected_tag_count > 1 else 'Copy Tag')
        copy_action.setEnabled(selected_tag_count > 0)
        context_menu.addSeparator()
        view_wiki_action = context_menu.addAction('View Danbooru Wiki')
        view_gelbooru_wiki_action = context_menu.addAction('View Gelbooru Wiki')
        context_menu.addSeparator()
        assign_menu = context_menu.addMenu('Assign Category')
        category_actions = {}
        for category in categories:
            action = assign_menu.addAction(category['name'])
            category_actions[action] = category['id']
        assign_menu.setEnabled(bool(categories))

        clear_action = context_menu.addAction('Clear Category')
        clear_action.setEnabled(current_category is not None)

        selected_action = context_menu.exec(
            self.viewport().mapToGlobal(position))
        if selected_action == copy_action:
            self.copy_selected_tags_to_clipboard()
        elif selected_action == view_wiki_action:
            self.danbooru_wiki_requested.emit(str(tag))
        elif selected_action == view_gelbooru_wiki_action:
            self.gelbooru_wiki_requested.emit(str(tag))
        elif selected_action == clear_action:
            self.tag_library_model.clear_category([str(tag)])
        elif selected_action in category_actions:
            tag_str = str(tag)
            if not self.tag_library_model.has_tag(tag_str):
                self.tag_library_model.add_tags([tag_str])
            self.tag_library_model.assign_category(
                [tag_str], category_actions[selected_action])

    def keyPressEvent(self, event: QKeyEvent):
        """
        Redirect typing to the Add Tag box, otherwise delete selected tags when
        the delete key or backspace key is pressed.
        """
        if self._should_redirect_typing_to_tag_input(event):
            tag_input_box = self.tag_input_box
            # Remember to return focus here after the tag is added, since the
            # box is being auto-focused only because the user typed in this list.
            tag_input_box._return_focus_to_tags_list_after_add = True
            tag_input_box.raise_()
            tag_input_box.setFocus()
            tag_input_box.insert(event.text())
            return
        if event.key() not in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            super().keyPressEvent(event)
            return
        rows_to_remove = [index.row() for index in self.selectedIndexes()]
        if not rows_to_remove:
            return
        remaining_tags = [tag for i, tag
                          in enumerate(self.image_tag_list_model.stringList())
                          if i not in rows_to_remove]
        self.image_tag_list_model.setStringList(remaining_tags)
        min_removed_row = min(rows_to_remove)
        remaining_row_count = self.image_tag_list_model.rowCount()
        if min_removed_row < remaining_row_count:
            self.select_tag(min_removed_row)
        elif remaining_row_count:
            # Select the last tag.
            self.select_tag(remaining_row_count - 1)

    def _should_redirect_typing_to_tag_input(self, event: QKeyEvent) -> bool:
        """Whether a keystroke in the tag list should start a new tag.

        Typing a printable character while the tag list has keyboard focus
        jumps to the Add Tag box in the Image Tags pane, so the user can start
        a new tag without clicking the box first. This is unrelated to the
        "Auto-focus Add Tag box when typing in Images pane" setting, which only
        governs typing in the thumbnails (Images) pane.

        Editing a tag in place (by double-clicking it) routes keys to the
        item's own editor instead of this list, so this is never reached while
        editing a tag.
        """
        tag_input_box = self.tag_input_box
        # The Add Tag box is hidden in natural language mode; leave the default
        # behavior alone when there's nowhere to redirect to.
        if tag_input_box is None or tag_input_box.isHidden():
            return False
        # Don't hijack keyboard shortcuts such as Ctrl+C or Alt+... .
        blocking_modifiers = (Qt.KeyboardModifier.ControlModifier
                              | Qt.KeyboardModifier.AltModifier
                              | Qt.KeyboardModifier.MetaModifier)
        if ((event.modifiers() & blocking_modifiers)
                != Qt.KeyboardModifier.NoModifier):
            return False
        text = event.text()
        # Only redirect single printable characters (letters, numbers, and
        # basic special characters). Keys like Enter, Tab, the arrows, Delete
        # and Backspace have an empty or control-character `text()` and are
        # excluded, so tag deletion and navigation keep working.
        return len(text) == 1 and text.isprintable()

    def select_tag(self, row: int):
        # If the current index is not set, using the arrow keys to navigate
        # through the tags after selecting the tag will not work.
        self.setCurrentIndex(self.image_tag_list_model.index(row))
        self.selectionModel().select(
            self.image_tag_list_model.index(row),
            QItemSelectionModel.SelectionFlag.ClearAndSelect)

    def selected_tag_for_wiki(self) -> str:
        """Return the single selected tag to look up in the wiki, or '' when
        zero or multiple tags are selected. Used by the wiki keyboard shortcuts
        so they auto-search the selected tag, matching the Tag Library."""
        selected_indexes = self.selectedIndexes()
        if len(selected_indexes) != 1:
            return ''
        tag = selected_indexes[0].data(Qt.ItemDataRole.DisplayRole)
        return str(tag) if tag else ''

    def copy_selected_tags_to_clipboard(self) -> bool:
        """Copy the selected tags to the clipboard as separator-joined text.

        Returns True if at least one tag was copied, so the caller knows the
        copy was handled here instead of falling back to copying every tag of
        the image.
        """
        selected_rows = sorted(index.row()
                               for index in self.selectedIndexes())
        if not selected_rows:
            return False
        tags = self.image_tag_list_model.stringList()
        selected_tags = [tags[row] for row in selected_rows
                         if 0 <= row < len(tags)]
        if not selected_tags:
            return False
        QApplication.clipboard().setText(
            get_tag_separator().join(selected_tags))
        return True


class ImageTagsEditor(QDockWidget):
    danbooru_wiki_requested = Signal(str)
    gelbooru_wiki_requested = Signal(str)

    def __init__(self, image_list_model: ImageListModel,
                 proxy_image_list_model: ProxyImageListModel,
                 tag_library_model: TagLibraryModel,
                 image_tag_list_model: QStringListModel, image_list: ImageList,
                 tokenizer: Optional[PreTrainedTokenizerBase], tag_separator: str):
        super().__init__()
        self.image_list_model = image_list_model
        self.proxy_image_list_model = proxy_image_list_model
        self.image_tag_list_model = image_tag_list_model
        self.tokenizer = tokenizer
        self.tag_separator = tag_separator
        self.image_index = None
        self.is_loading_image_tags = False

        # Each `QDockWidget` needs a unique object name for saving its state.
        self.setObjectName('image_tags_editor')
        self.setWindowTitle('Image Tags')
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea
                             | Qt.DockWidgetArea.RightDockWidgetArea)
        self.tag_input_box = TagInputBox(self.image_tag_list_model,
                                         tag_library_model, image_list,
                                         tag_separator)
        self.natural_language_mode_check_box = QPushButton(
            'Natural language mode')
        self.natural_language_mode_check_box.setCheckable(True)
        self.natural_language_mode_check_box.setAutoDefault(False)
        self.natural_language_mode_check_box.setDefault(False)
        self._update_nl_button_style()
        self.image_tags_list = ImageTagsList(self.image_tag_list_model,
                                             tag_library_model)
        # Let the tag list redirect typing to the Add Tag box.
        self.image_tags_list.tag_input_box = self.tag_input_box
        # Let the Add Tag box return focus to the tag list after an auto-focused
        # add (see TagInputBox / ImageTagsList).
        self.tag_input_box.image_tags_list = self.image_tags_list
        self.image_tags_list.danbooru_wiki_requested.connect(
            self.danbooru_wiki_requested.emit)
        self.image_tags_list.gelbooru_wiki_requested.connect(
            self.gelbooru_wiki_requested.emit)
        self.natural_language_text_edit = NaturalLanguageTextEdit()
        self.token_count_label = QLabel()
        self.token_count_default_palette = QPalette(self.token_count_label.palette())
        # Shown on the opposite side of the token count when the current image
        # is flagged as complete. Uses the same green as the completion
        # checkmark in the Images pane (see ImageList's completion icon).
        self.complete_label = QLabel('Complete')
        self.complete_label.setStyleSheet('color: #3cc23c;')
        self.complete_label.setVisible(False)
        token_count_layout = QHBoxLayout()
        token_count_layout.setContentsMargins(0, 0, 0, 0)
        token_count_layout.addWidget(self.token_count_label)
        token_count_layout.addStretch()
        token_count_layout.addWidget(self.complete_label)
        # A container widget is required to use a layout with a `QDockWidget`.
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(self.natural_language_mode_check_box)
        layout.addWidget(self.tag_input_box)
        layout.addWidget(self.image_tags_list)
        layout.addWidget(self.natural_language_text_edit)
        layout.addLayout(token_count_layout)
        self.setWidget(container)

        # When a tag is added, select it and scroll to the bottom of the list,
        # unless the user disabled auto-selecting newly added tags.
        self.image_tag_list_model.rowsInserted.connect(self._on_tags_inserted)
        # `rowsInserted` does not have to be connected because `dataChanged`
        # is emitted when a tag is added.
        self.image_tag_list_model.modelReset.connect(self.count_tokens)
        self.image_tag_list_model.dataChanged.connect(self.count_tokens)
        self.natural_language_mode_check_box.toggled.connect(
            lambda: self.set_natural_language_mode())
        self.natural_language_text_edit.textChanged.connect(self.count_tokens)
        self.natural_language_text_edit.editing_finished.connect(
            self.save_natural_language_prompt)
        self.set_natural_language_mode()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange:
            self._update_nl_button_style()

    def _update_nl_button_style(self):
        is_dark = self.palette().windowText().color().lightness() > 128
        color = '#2a82da' if is_dark else '#308cc6'
        self.natural_language_mode_check_box.setStyleSheet(
            f'QPushButton:checked {{'
            f' background-color: {color};'
            f' color: white;'
            f' border: 1px solid {color};'
            f'}}'
        )

    def set_tokenizer(self, tokenizer: PreTrainedTokenizerBase):
        self.tokenizer = tokenizer
        self.count_tokens()

    def set_tag_separator(self, tag_separator: str):
        """Apply a new tag separator without a restart."""
        self.tag_separator = tag_separator
        self.tag_input_box.tag_separator = tag_separator
        self.count_tokens()

    def set_autocomplete_enabled(self, enabled: bool):
        """Toggle tag autocomplete without a restart."""
        self.tag_input_box.set_autocomplete_enabled(enabled)

    @Slot()
    def count_tokens(self):
        if self.tokenizer is None:
            self.token_count_label.setText('Loading...')
            return
        caption = build_caption_text(
            self.image_tag_list_model.stringList(),
            self.natural_language_text_edit.toPlainText(),
            self.tag_separator)
        # Subtract 2 for the `<|startoftext|>` and `<|endoftext|>` tokens.
        caption_token_count = len(self.tokenizer(caption).input_ids) - 2
        settings = get_settings()
        token_limit = settings.value(
            'image_tags_token_limit',
            defaultValue=DEFAULT_SETTINGS['image_tags_token_limit'], type=int)
        if caption_token_count > token_limit:
            warning_palette = QPalette(self.token_count_default_palette)
            warning_palette.setColor(QPalette.ColorRole.WindowText, QColor('red'))
            self.token_count_label.setPalette(warning_palette)
        else:
            self.token_count_label.setPalette(self.token_count_default_palette)
        self.token_count_label.setText(f'{caption_token_count} / '
                                       f'{token_limit} Tokens')

    @Slot()
    def refresh_token_count_palette(self):
        self.token_count_default_palette = QPalette(self.token_count_label.palette())
        self.count_tokens()

    @Slot()
    def select_first_tag(self):
        if self.image_tag_list_model.rowCount() == 0:
            return
        self.image_tags_list.select_tag(0)

    def select_last_tag(self):
        tag_count = self.image_tag_list_model.rowCount()
        if tag_count == 0:
            return
        self.image_tags_list.select_tag(tag_count - 1)

    def select_last_tag_or_flash(self):
        """Called by external add-tag flows (All Tags pane, wiki dialogs) after
        a tag is added. Selects the new (last) tag by default, or, when the
        user disabled auto-selecting new tags, flashes the Add Tag box green
        instead so the feedback matches the Add Tag box behavior.
        """
        if self.tag_input_box._new_tag_auto_select_disabled():
            self.tag_input_box.flash_added_feedback()
        else:
            self.select_last_tag()

    def clear_add_tag_box_if_matches(self, tag: str):
        """Clear the Add Tag box if its current text matches ``tag`` (ignoring
        surrounding whitespace and case). Used by external add-tag flows (e.g.
        the wiki dialogs) so that adding a tag the user had already typed into
        the box also empties the box, matching the normal Enter-to-add behavior.
        """
        current_text = self.tag_input_box.text().strip()
        if current_text and current_text.casefold() == tag.strip().casefold():
            self.tag_input_box.clear()

    def _on_tags_inserted(self, parent: QModelIndex, first: int, last: int):
        """Select the newly added tag and scroll it into view, unless the user
        turned on "Do not auto-select newly added tags".
        """
        disable_auto_select = get_settings().value(
            'disable_new_tag_auto_select',
            defaultValue=DEFAULT_SETTINGS['disable_new_tag_auto_select'],
            type=bool)
        if disable_auto_select:
            return
        self.image_tags_list.select_tag(last)
        self.image_tags_list.scrollToBottom()

    def load_image_tags(self, proxy_image_index: QModelIndex,
                        save_current_prompt: bool = True):
        if save_current_prompt:
            self.save_natural_language_prompt()
        next_image_index = self.proxy_image_list_model.mapToSource(
            proxy_image_index)
        self.image_index = next_image_index
        image: Image = self.proxy_image_list_model.data(
            proxy_image_index, Qt.ItemDataRole.UserRole)
        self.complete_label.setVisible(bool(image.is_complete))
        # If the string list already contains the image's tags, do not reload
        # them. This is the case when the tags are edited directly through the
        # image tags editor. Removing this check breaks the functionality of
        # reordering multiple tags at the same time because it gets interrupted
        # after one tag is moved.
        current_string_list = self.image_tag_list_model.stringList()
        current_natural_language_prompt = (
            self.natural_language_text_edit.toPlainText())
        if (current_string_list == image.tags
                and current_natural_language_prompt
                == image.natural_language_prompt):
            return
        self.is_loading_image_tags = True
        self.image_tag_list_model.setStringList(image.tags)
        self.natural_language_text_edit.set_clean_plain_text(
            image.natural_language_prompt)
        self.set_natural_language_mode()
        self.is_loading_image_tags = False
        self.count_tokens()
        if self.image_tags_list.hasFocus():
            self.select_first_tag()

    @Slot()
    def set_natural_language_mode(self):
        is_natural_language_mode = (
            self.natural_language_mode_check_box.isChecked())
        self.tag_input_box.setVisible(not is_natural_language_mode)
        self.image_tags_list.setVisible(not is_natural_language_mode)
        self.natural_language_text_edit.setVisible(is_natural_language_mode)
        self.count_tokens()

    @Slot()
    def save_natural_language_prompt(self):
        if self.image_index is None:
            return
        image: Image = self.image_list_model.data(self.image_index,
                                                  Qt.ItemDataRole.UserRole)
        natural_language_prompt = self.natural_language_text_edit.toPlainText()
        if image.natural_language_prompt == natural_language_prompt:
            self.natural_language_text_edit.mark_clean()
            return
        self.image_list_model.add_to_undo_stack(
            action_name='Edit Natural Language Prompt',
            should_ask_for_confirmation=False)
        self.image_list_model.update_image_natural_language_prompt(
            self.image_index, natural_language_prompt)
        self.natural_language_text_edit.mark_clean()

    @Slot()
    def reload_image_tags_if_changed(self, first_changed_index: QModelIndex,
                                     last_changed_index: QModelIndex):
        """
        Reload the tags for the current image if its index is in the range of
        changed indices.
        """
        if self.image_index is None:
            return
        if (first_changed_index.row() <= self.image_index.row()
                <= last_changed_index.row()):
            # Preserve the user's current selection across in-place tag edits
            # such as undo/redo, instead of resetting to the first tag. When
            # auto-select is on and the edit re-added a tag (e.g. redoing an add
            # or undoing a delete), select that re-added tag instead, mirroring
            # the behavior of adding a new tag.
            previous_row = self.image_tags_list.currentIndex().row()
            had_focus = self.image_tags_list.hasFocus()
            old_tags = self.image_tag_list_model.stringList()
            proxy_image_index = self.proxy_image_list_model.mapFromSource(
                self.image_index)
            self.load_image_tags(proxy_image_index, save_current_prompt=False)
            new_tags = self.image_tag_list_model.stringList()
            new_row_count = len(new_tags)
            if new_row_count == 0:
                return
            row_to_select = None
            if not self.tag_input_box._new_tag_auto_select_disabled():
                old_tag_set = set(old_tags)
                added_rows = [row for row, tag in enumerate(new_tags)
                              if tag not in old_tag_set]
                if added_rows:
                    # Select the last re-added tag.
                    row_to_select = added_rows[-1]
            if row_to_select is None and previous_row >= 0:
                row_to_select = min(previous_row, new_row_count - 1)
            if row_to_select is not None:
                self.image_tags_list.select_tag(row_to_select)
                if had_focus:
                    self.image_tags_list.setFocus()
