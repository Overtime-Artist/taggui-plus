from enum import Enum

from PySide6.QtCore import (QItemSelection, QItemSelectionModel, QPoint, Qt,
                            QTimer, Signal, Slot)
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDockWidget,
                               QHBoxLayout, QLabel, QMenu, QMessageBox,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from models.proxy_tag_counter_model import ProxyTagCounterModel
from models.tag_counter_model import TagCounterModel
from utils.big_widgets import TallPushButton
from utils.elided_tooltip import ElidedToolTipListView
from utils.enums import AllTagsFilterLogic, AllTagsSortBy, SortOrder
from utils.settings import get_settings, get_tag_separator
from utils.settings_widgets import SettingsComboBox
from utils.tag_filter import TagFilterLineEdit
from utils.text_edit_item_delegate import TextEditItemDelegate
from utils.utils import get_confirmation_dialog_reply, list_with_and, pluralize


class FilterLineEdit(TagFilterLineEdit):
    def __init__(self):
        super().__init__('Search Tags')


class ClickAction(str, Enum):
    FILTER_IMAGES = 'Filter images'
    ADD_TO_SELECTED = 'Add to selected'


class AllTagsList(ElidedToolTipListView):
    image_list_filter_requested = Signal(list)
    tag_addition_requested = Signal(str)
    tags_deletion_requested = Signal(list)
    danbooru_wiki_requested = Signal(str)
    gelbooru_wiki_requested = Signal(str)

    def __init__(self, proxy_tag_counter_model: ProxyTagCounterModel,
                 all_tags_editor: 'AllTagsEditor'):
        super().__init__()
        self.setModel(proxy_tag_counter_model)
        self.all_tags_editor = all_tags_editor
        self.suppress_filter_on_selection_change = False
        self.setItemDelegate(TextEditItemDelegate(self))
        self.setWordWrap(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        # `selectionChanged` must be used and not `currentChanged` because
        # `currentChanged` is not emitted when the same tag is deselected and
        # selected again.
        self.selectionModel().selectionChanged.connect(
            self.handle_selection_change)
        self.pending_click_row = None
        self.pending_click_timer = QTimer()
        self.pending_click_timer.setSingleShot(True)
        self.pending_click_timer.timeout.connect(self.emit_tag_addition)

    def emit_tag_addition(self):
        """Emit tag_addition_requested for a pending click."""
        if self.pending_click_row is not None:
            index = self.model().index(self.pending_click_row, 0)
            tag = index.data(Qt.ItemDataRole.EditRole)
            self.tag_addition_requested.emit(tag)
        self.pending_click_row = None

    def mousePressEvent(self, event: QMouseEvent):
        click_action = (self.all_tags_editor.click_action_combo_box
                        .currentText())
        if event.button() == Qt.MouseButton.RightButton:
            self.suppress_filter_on_selection_change = True
        if (event.button() == Qt.MouseButton.LeftButton
                and click_action == ClickAction.ADD_TO_SELECTED):
            # Don't emit tag_addition_requested if an editor is active or if
            # a modal dialog is open (indicating the user is in the middle of
            # renaming or other operations).
            from PySide6.QtWidgets import QApplication
            modal_widgets = QApplication.instance().topLevelWidgets()
            has_modal_dialog = any(
                w.isModal() and w.isVisible() for w in modal_widgets
            )
            if self.state() != QAbstractItemView.State.EditingState and not has_modal_dialog:
                index = self.indexAt(event.pos())
                row = index.row() if index.isValid() else -1
                # Check if this is a second click on the same row (part of a
                # double-click). If so, cancel the pending signal emission.
                if row == self.pending_click_row and row >= 0:
                    self.pending_click_timer.stop()
                    self.pending_click_row = None
                elif row >= 0:
                    # This might be the first click of a double-click, so defer
                    # the signal emission to allow double-click detection.
                    self.pending_click_row = row
                    self.pending_click_timer.start(500)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        """Cancel pending tag addition when double-click is detected."""
        self.pending_click_timer.stop()
        self.pending_click_row = None
        super().mouseDoubleClickEvent(event)

    @Slot(QPoint)
    def show_context_menu(self, position: QPoint):
        selected_index = self.indexAt(position)
        if not selected_index.isValid():
            return
        if selected_index not in self.selectedIndexes():
            self.suppress_filter_on_selection_change = True
            self.selectionModel().select(
                selected_index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect)
            self.setCurrentIndex(selected_index)

        tag = selected_index.data(Qt.ItemDataRole.EditRole)
        if not tag:
            return

        tag_library_model = (self.all_tags_editor.tag_counter_model
                           .tag_library_model)
        categories = tag_library_model.get_categories()

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

        # Gather every selected tag so category actions apply to all of them,
        # not just the tag that was right-clicked.
        selected_tags = []
        for index in self.selectedIndexes():
            index_tag = index.data(Qt.ItemDataRole.EditRole)
            if index_tag:
                selected_tags.append(str(index_tag))
        selected_tags = list(dict.fromkeys(selected_tags))
        if not selected_tags:
            selected_tags = [str(tag)]

        any_has_category = any(
            tag_library_model.get_category_for_tag(selected_tag) is not None
            for selected_tag in selected_tags)
        clear_action = context_menu.addAction('Clear Category')
        clear_action.setEnabled(any_has_category)

        selected_action = context_menu.exec(self.viewport().mapToGlobal(position))
        if selected_action == copy_action:
            self.copy_selected_tags_to_clipboard()
        elif selected_action == view_wiki_action:
            self.danbooru_wiki_requested.emit(str(tag))
        elif selected_action == view_gelbooru_wiki_action:
            self.gelbooru_wiki_requested.emit(str(tag))
        elif selected_action == clear_action:
            tag_library_model.clear_category(selected_tags)
        elif selected_action in category_actions:
            tags_to_add = [selected_tag for selected_tag in selected_tags
                           if not tag_library_model.has_tag(selected_tag)]
            if tags_to_add:
                tag_library_model.add_tags(tags_to_add)
            tag_library_model.assign_category(
                selected_tags, category_actions[selected_action])

    def keyPressEvent(self, event: QKeyEvent):
        """
        Delete all instances of the selected tag when the delete key or
        backspace key is pressed.
        """
        if event.key() not in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            super().keyPressEvent(event)
            return
        selected_indices = self.selectedIndexes()
        if not selected_indices:
            return
        tags = []
        tags_count = 0
        for selected_index in selected_indices:
            tag, tag_count = selected_index.data(Qt.ItemDataRole.UserRole)
            tags.append(tag)
            tags_count += tag_count
        question = (f'Delete {tags_count} {pluralize("instance", tags_count)} '
                    f'of ')
        if len(tags) < 10:
            quoted_tags = [f'"{tag}"' for tag in tags]
            question += (f'{pluralize("tag", len(tags))} '
                         f'{list_with_and(quoted_tags)}?')
        else:
            question += f'{len(tags)} tags?'
        reply = get_confirmation_dialog_reply(
            title=f'Delete {pluralize("Tag", len(tags))}', question=question)
        if reply == QMessageBox.StandardButton.Yes:
            self.tags_deletion_requested.emit(tags)

    def handle_selection_change(self, selected: QItemSelection, _):
        if self.suppress_filter_on_selection_change:
            self.suppress_filter_on_selection_change = False
            return
        click_action = (self.all_tags_editor.click_action_combo_box
                        .currentText())
        if click_action != ClickAction.FILTER_IMAGES:
            return
        selected_indices = self.selectedIndexes()
        if not selected_indices:
            return
        selected_tags = [index.data(Qt.ItemDataRole.EditRole)
                         for index in selected_indices]
        self.image_list_filter_requested.emit(selected_tags)

    def selected_tag_for_wiki(self) -> str:
        """Return the single selected tag to look up in the wiki, or '' when
        zero or multiple tags are selected. Used by the wiki keyboard shortcuts
        so they auto-search the selected tag, matching the Tag Library."""
        selected_indexes = self.selectedIndexes()
        if len(selected_indexes) != 1:
            return ''
        tag = selected_indexes[0].data(Qt.ItemDataRole.EditRole)
        return str(tag) if tag else ''

    def copy_selected_tags_to_clipboard(self) -> bool:
        """Copy the selected tags to the clipboard as separator-joined text.

        Returns True if at least one tag was copied, so the caller knows the
        copy was handled here instead of falling back to copying every tag of
        the selected image(s).
        """
        ordered_indices = sorted(self.selectedIndexes(),
                                 key=lambda index: index.row())
        selected_tags = [str(index.data(Qt.ItemDataRole.EditRole))
                         for index in ordered_indices
                         if index.data(Qt.ItemDataRole.EditRole)]
        if not selected_tags:
            return False
        QApplication.clipboard().setText(
            get_tag_separator().join(selected_tags))
        return True


class AllTagsEditor(QDockWidget):
    danbooru_wiki_requested = Signal(str)
    gelbooru_wiki_requested = Signal(str)

    def __init__(self, tag_counter_model: TagCounterModel):
        super().__init__()
        self.tag_counter_model = tag_counter_model

        # Each `QDockWidget` needs a unique object name for saving its state.
        self.setObjectName('all_tags_editor')
        self.setWindowTitle('All Tags')
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea
                             | Qt.DockWidgetArea.RightDockWidgetArea)
        self.proxy_tag_counter_model = ProxyTagCounterModel(
            self.tag_counter_model)
        self.proxy_tag_counter_model.setFilterRole(Qt.ItemDataRole.EditRole)
        self.filter_line_edit = FilterLineEdit()

        # Combined click action + filter logic row (idea 4)
        combined_layout = QHBoxLayout()
        combined_layout.addWidget(QLabel('Click action'))
        self.click_action_combo_box = SettingsComboBox(
            key='all_tags_click_action')
        self.click_action_combo_box.addItems(list(ClickAction))
        self.click_action_combo_box.setSizeAdjustPolicy(
            SettingsComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.click_action_combo_box.setMinimumContentsLength(3)
        combined_layout.addWidget(self.click_action_combo_box, stretch=1)
        combined_layout.addSpacing(8)
        combined_layout.addWidget(QLabel('Logic'))
        self.filter_logic_combo_box = SettingsComboBox(
            key='all_tags_filter_logic', default='AND')
        self.filter_logic_combo_box.addItems(list(AllTagsFilterLogic))
        self.filter_logic_combo_box.setMinimumContentsLength(2)
        self.filter_logic_combo_box.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        combined_layout.addWidget(self.filter_logic_combo_box)

        # Sort row with toggle button (idea 3)
        sort_layout = QHBoxLayout()
        sort_label = QLabel('Sort by')
        self.sort_by_combo_box = SettingsComboBox(key='all_tags_sort_by',
                                                  default='Frequency')
        self.sort_by_combo_box.addItems(list(AllTagsSortBy))
        self.sort_by_combo_box.setSizeAdjustPolicy(
            SettingsComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.sort_by_combo_box.setMinimumContentsLength(6)
        self.sort_by_combo_box.currentTextChanged.connect(self.sort_tags)
        self.sort_order_button = QPushButton()
        self.sort_order_button.setAutoDefault(False)
        self.sort_order_button.setFixedWidth(
            self.sort_order_button.sizeHint().width() + 6)
        saved_sort_order = get_settings().value(
            'all_tags_sort_order', SortOrder.DESCENDING.value, type=str)
        self.is_sort_descending = (saved_sort_order == SortOrder.DESCENDING)
        self.update_sort_order_button()
        sort_layout.addWidget(sort_label)
        sort_layout.addWidget(self.sort_by_combo_box, stretch=1)
        sort_layout.addWidget(self.sort_order_button)
        self.clear_filter_button = TallPushButton('Clear Image List Filter')
        self.clear_filter_button.setFixedHeight(
            int(self.clear_filter_button.sizeHint().height() * 1.5))
        self.all_tags_list = AllTagsList(self.proxy_tag_counter_model,
                                         all_tags_editor=self)
        self.tag_count_label = QLabel()
        # A container widget is required to use a layout with a `QDockWidget`.
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(self.filter_line_edit)
        layout.addLayout(combined_layout)
        layout.addLayout(sort_layout)
        layout.addWidget(self.clear_filter_button)
        layout.addWidget(self.all_tags_list)
        layout.addWidget(self.tag_count_label)
        self.setWidget(container)

        self.proxy_tag_counter_model.modelReset.connect(
            self.update_tag_count_label)
        self.proxy_tag_counter_model.rowsInserted.connect(
            self.update_tag_count_label)
        self.proxy_tag_counter_model.rowsRemoved.connect(
            self.update_tag_count_label)
        self.tag_counter_model.tag_library_model.categories_changed.connect(
            self.sort_tags)
        self.filter_line_edit.textChanged.connect(self.set_filter)
        self.filter_line_edit.textChanged.connect(self.update_tag_count_label)
        self.all_tags_list.danbooru_wiki_requested.connect(
            self.danbooru_wiki_requested.emit)
        self.all_tags_list.gelbooru_wiki_requested.connect(
            self.gelbooru_wiki_requested.emit)
        self.sort_order_button.clicked.connect(self.toggle_sort_order)
        self.click_action_combo_box.currentTextChanged.connect(
            self.set_selection_mode)
        self.set_selection_mode(self.click_action_combo_box.currentText())
        self.sort_tags()

    @Slot()
    def sort_tags(self):
        self.proxy_tag_counter_model.sort_by = (self.sort_by_combo_box
                                                .currentText())
        sort_order = (Qt.SortOrder.DescendingOrder
                      if self.is_sort_descending
                      else Qt.SortOrder.AscendingOrder)
        # `invalidate()` must be called to force the proxy model to re-sort.
        self.proxy_tag_counter_model.invalidate()
        self.proxy_tag_counter_model.sort(0, sort_order)

    @Slot()
    def toggle_sort_order(self):
        self.is_sort_descending = not self.is_sort_descending
        sort_order = (SortOrder.DESCENDING
                      if self.is_sort_descending
                      else SortOrder.ASCENDING).value
        get_settings().setValue('all_tags_sort_order', sort_order)
        self.update_sort_order_button()
        self.sort_tags()

    def update_sort_order_button(self):
        if self.is_sort_descending:
            self.sort_order_button.setText('↓')
        else:
            self.sort_order_button.setText('↑')

    @Slot(str)
    def set_filter(self, filter_):
        self.proxy_tag_counter_model.set_filter(
            self.filter_line_edit.parse_filter_text())

    @Slot()
    def update_tag_count_label(self):
        total_tag_count = self.tag_counter_model.rowCount()
        filtered_tag_count = self.proxy_tag_counter_model.rowCount()
        self.tag_count_label.setText(f'{filtered_tag_count} / '
                                     f'{total_tag_count} Tags')

    @Slot(str)
    def set_selection_mode(self, click_action: str):
        if click_action == ClickAction.FILTER_IMAGES:
            self.all_tags_list.setSelectionMode(
                QAbstractItemView.SelectionMode.ExtendedSelection)
        elif click_action == ClickAction.ADD_TO_SELECTED:
            self.all_tags_list.setSelectionMode(
                QAbstractItemView.SelectionMode.SingleSelection)
            self.all_tags_list.selectionModel().select(
                self.all_tags_list.selectionModel().currentIndex(),
                QItemSelectionModel.SelectionFlag.ClearAndSelect)
