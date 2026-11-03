import re

from PySide6.QtCore import QEvent, QObject, Qt, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton,
                               QVBoxLayout)

from models.image_list_model import ImageListModel, Scope
from models.tag_library_model import TagLibraryModel
from models.tag_counter_model import TagCounterModel
from utils.elided_tooltip import ElidedToolTipListWidget
from utils.settings import get_settings, DEFAULT_SETTINGS
from utils.settings_widgets import (FocusedScrollSettingsComboBox,
                                    SettingsBigCheckBox, SettingsLineEdit)
from utils.utils import pluralize
from widgets.auto_captioner import HorizontalLine


def _parse_move_to_front_tags(text: str) -> list[str]:
    """Split the comma-separated 'move to front' setting into a tag list,
    honouring escaped commas (\\,) the same way the dialog input does."""
    tags = re.split(r'(?<!\\),', text)
    return [tag.strip().replace(r'\,', ',') for tag in tags]


def get_batch_reorder_scope() -> str:
    """The saved Batch Reorder scope, shared by the dialog and the default
    reorder shortcut so both affect the same images."""
    scope = get_settings().value('batch_reorder_scope',
                                 defaultValue=Scope.ALL_IMAGES.value, type=str)
    return scope if scope in {s.value for s in Scope} else (
        Scope.ALL_IMAGES.value)


def apply_batch_reorder(option: str, image_list_model: ImageListModel,
                        tag_counter_model: TagCounterModel,
                        tag_library_model: TagLibraryModel):
    """Run a single batch reorder operation without opening the dialog.

    Reads the relevant saved settings ('do_not_reorder_first_tag' and, for
    'Move Tags to Front', 'move_to_front_tags') and the scope chosen in the
    dialog, so the result matches what the dialog would produce for the same
    option."""
    settings = get_settings()
    scope = get_batch_reorder_scope()
    do_not_reorder_first_tag = settings.value(
        'do_not_reorder_first_tag', defaultValue=False, type=bool)
    if option == 'Sort Tags Alphabetically':
        image_list_model.sort_tags_alphabetically(do_not_reorder_first_tag,
                                                  scope)
    elif option == 'Sort Tags by Frequency':
        image_list_model.sort_tags_by_frequency(
            tag_counter_model.tag_counter, do_not_reorder_first_tag, scope)
    elif option == 'Sort Tags by Tag Category':
        image_list_model.sort_tags_by_category(
            tag_library_model.get_category_for_tag,
            tag_library_model.get_category_order_map(),
            do_not_reorder_first_tag, scope)
    elif option == 'Reverse Order of Tags':
        image_list_model.reverse_tags_order(do_not_reorder_first_tag, scope)
    elif option == 'Shuffle Tags Randomly':
        image_list_model.shuffle_tags(do_not_reorder_first_tag, scope)
    elif option == 'Move Tags to Front':
        text = settings.value('move_to_front_tags', defaultValue='', type=str)
        tags = _parse_move_to_front_tags(text)
        image_list_model.move_tags_to_front(tags, scope)


class _ReturnKeyFilter(QObject):
    """Runs `callback` when Enter is pressed in the watched widget and stops
    the key from reaching the dialog, which would otherwise activate the
    default reorder button."""

    def __init__(self, callback, parent):
        super().__init__(parent)
        self.callback = callback

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            self.callback()
            return True
        return super().eventFilter(watched, event)


class BatchReorderTagsDialog(QDialog):
    def __init__(self, parent, image_list_model: ImageListModel,
                 tag_counter_model: TagCounterModel,
                 tag_library_model: TagLibraryModel):
        super().__init__(parent)
        self.image_list_model = image_list_model
        self.tag_library_model = tag_library_model
        self.setWindowTitle('Batch Reorder Tags')
        layout = QVBoxLayout(self)
        scope_layout = QHBoxLayout()
        scope_layout.setContentsMargins(20, 20, 20, 0)
        scope_layout.addWidget(QLabel('Scope'))
        self.scope_combo_box = FocusedScrollSettingsComboBox(
            key='batch_reorder_scope', default=Scope.ALL_IMAGES.value)
        self.scope_combo_box.addItems(list(Scope))
        if self.scope_combo_box.currentText() not in list(Scope):
            self.scope_combo_box.setCurrentText(Scope.ALL_IMAGES.value)
        scope_layout.addWidget(self.scope_combo_box, stretch=1)
        self.scope_count_label = QLabel()
        scope_layout.addWidget(self.scope_count_label)
        self.scope_combo_box.currentTextChanged.connect(
            self.update_scope_count_label)
        self.update_scope_count_label()
        layout.addLayout(scope_layout)
        top_layout = QHBoxLayout()
        top_layout.setContentsMargins(20, 20, 20, 20)
        top_layout.setSpacing(20)
        do_not_reorder_first_tag_check_box = SettingsBigCheckBox(
            key='do_not_reorder_first_tag', default=False)
        do_not_reorder_first_tag_check_box.setText('Do not reorder first tag')
        top_layout.addWidget(do_not_reorder_first_tag_check_box)
        top_buttons_layout = QVBoxLayout()
        top_buttons_layout.setSpacing(20)
        sort_alphabetically_button = QPushButton('Sort Tags Alphabetically')
        sort_alphabetically_button.setAutoDefault(False)
        sort_alphabetically_button.clicked.connect(
            lambda: self.image_list_model.sort_tags_alphabetically(
                do_not_reorder_first_tag_check_box.isChecked(), self.scope))
        top_buttons_layout.addWidget(sort_alphabetically_button)
        sort_by_frequency_button = QPushButton('Sort Tags by Frequency')
        sort_by_frequency_button.setAutoDefault(False)
        sort_by_frequency_button.clicked.connect(
            lambda: self.image_list_model.sort_tags_by_frequency(
                tag_counter_model.tag_counter,
                do_not_reorder_first_tag_check_box.isChecked(), self.scope))
        top_buttons_layout.addWidget(sort_by_frequency_button)
        sort_by_category_button = QPushButton('Sort Tags by Tag Category')
        sort_by_category_button.setAutoDefault(False)
        sort_by_category_button.clicked.connect(
            lambda: self.sort_tags_by_category(
                do_not_reorder_first_tag_check_box.isChecked()))
        top_buttons_layout.addWidget(sort_by_category_button)
        reverse_button = QPushButton('Reverse Order of Tags')
        reverse_button.setAutoDefault(False)
        reverse_button.clicked.connect(
            lambda: self.image_list_model.reverse_tags_order(
                do_not_reorder_first_tag_check_box.isChecked(), self.scope))
        top_buttons_layout.addWidget(reverse_button)
        shuffle_button = QPushButton('Shuffle Tags Randomly')
        shuffle_button.setAutoDefault(False)
        shuffle_button.clicked.connect(
            lambda: self.image_list_model.shuffle_tags(
                do_not_reorder_first_tag_check_box.isChecked(), self.scope))
        top_buttons_layout.addWidget(shuffle_button)
        top_layout.addLayout(top_buttons_layout)
        horizontal_line = HorizontalLine()
        bottom_layout = QVBoxLayout()
        bottom_layout.setContentsMargins(20, 20, 20, 20)
        bottom_layout.setSpacing(20)
        self.move_tags_line_edit = SettingsLineEdit(key='move_to_front_tags')
        self.move_tags_line_edit.setPlaceholderText('Tags to move to front '
                                                    '(comma-separated)')
        self.move_tags_line_edit.setClearButtonEnabled(True)
        self.move_tags_line_edit.textChanged.connect(
            lambda: self.move_tags_button.setEnabled(
                bool(self.move_tags_line_edit.text())))
        self.move_tags_button = QPushButton('Move Tags to Front')
        self.move_tags_button.setAutoDefault(False)
        self.move_tags_button.setEnabled(False)
        self.move_tags_button.clicked.connect(self.move_tags_to_front)
        self.move_tags_line_edit.installEventFilter(_ReturnKeyFilter(
            self.move_tags_to_front_from_enter, self))
        self.categories_list = ElidedToolTipListWidget()
        self.categories_list.setDragDropMode(
            QAbstractItemView.DragDropMode.InternalMove)
        self.categories_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.categories_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.load_categories()
        bottom_layout.addWidget(self.move_tags_line_edit)
        bottom_layout.addWidget(self.move_tags_button)
        bottom_layout.addWidget(QLabel('Category order for Sort Tags by Tag Category'))
        bottom_layout.addWidget(self.categories_list)
        layout.addLayout(top_layout)
        layout.addWidget(horizontal_line)
        layout.addLayout(bottom_layout)

        self.reorder_buttons = {
            'Sort Tags Alphabetically': sort_alphabetically_button,
            'Sort Tags by Frequency': sort_by_frequency_button,
            'Sort Tags by Tag Category': sort_by_category_button,
            'Reverse Order of Tags': reverse_button,
            'Shuffle Tags Randomly': shuffle_button,
            'Move Tags to Front': self.move_tags_button,
        }
        self._preselect_default_reorder_button()

        self.move_tags_line_edit.textChanged.emit(
            self.move_tags_line_edit.text())

    def _preselect_default_reorder_button(self):
        """Make the button for the user's configured default reorder the
        dialog's default button, so pressing Enter runs it immediately."""
        settings = get_settings()
        default_option = settings.value(
            'default_batch_reorder',
            defaultValue=DEFAULT_SETTINGS['default_batch_reorder'], type=str)
        default_button = self.reorder_buttons.get(default_option)
        if default_button is None:
            return
        for button in self.reorder_buttons.values():
            is_default = button is default_button
            button.setDefault(is_default)
            button.setAutoDefault(is_default)
        default_button.setFocus()

    @property
    def scope(self) -> str:
        return self.scope_combo_box.currentText()

    @Slot()
    def update_scope_count_label(self):
        model = self.image_list_model
        count = sum(1 for index, image in enumerate(model.images)
                    if model.is_image_in_scope(self.scope, index, image))
        self.scope_count_label.setText(
            f'({count} {pluralize("image", count)})')

    @Slot()
    def move_tags_to_front(self):
        tags = _parse_move_to_front_tags(self.move_tags_line_edit.text())
        self.image_list_model.move_tags_to_front(tags, self.scope)

    def move_tags_to_front_from_enter(self):
        if self.move_tags_button.isEnabled():
            self.move_tags_to_front()

    def load_categories(self):
        self.categories_list.clear()
        for category in self.tag_library_model.get_categories():
            item = QListWidgetItem(category['name'])
            item.setData(Qt.ItemDataRole.UserRole, category['id'])
            color = QColor(category['color'])
            if color.isValid():
                item.setForeground(color)
            self.categories_list.addItem(item)

    @Slot()
    def sort_tags_by_category(self, do_not_reorder_first_tag: bool):
        ordered_category_ids = [
            self.categories_list.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(self.categories_list.count())
        ]
        self.tag_library_model.set_category_order(ordered_category_ids)
        self.image_list_model.sort_tags_by_category(
            self.tag_library_model.get_category_for_tag,
            self.tag_library_model.get_category_order_map(),
            do_not_reorder_first_tag, self.scope)
