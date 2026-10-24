import re

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QGridLayout, QLabel,
                               QListWidget, QPushButton, QSizePolicy,
                               QVBoxLayout)

from models.image_list_model import ImageListModel, Scope
from utils.settings import get_settings
from utils.settings_widgets import (SettingsBigCheckBox, SettingsComboBox,
                                    SettingsLineEdit)
from utils.utils import pluralize


class MatchesListWidget(QListWidget):
    """List view for the find-text matches preview. Reports a compact
    preferred height so the dialog opens at a modest size instead of
    QListWidget's tall default, while still allowing the user to enlarge the
    dialog to see more rows."""

    PREFERRED_HEIGHT = 140
    MINIMUM_HEIGHT = 90

    def minimumSizeHint(self):
        size = super().minimumSizeHint()
        size.setHeight(self.MINIMUM_HEIGHT)
        return size

    def sizeHint(self):
        size = super().sizeHint()
        size.setHeight(self.PREFERRED_HEIGHT)
        return size


class FindAndReplaceDialog(QDialog):
    # The most frequent matching tags are always shown; the rest are summarised
    # in a trailing line. The cap only bounds how many rows are built in the
    # widget (the match count and scan are always exact), so it stays large
    # enough to show every tag for any realistic search while still guarding
    # against pathologically broad find text.
    MAX_LISTED_MATCHES = 1000

    def __init__(self, parent, image_list_model: ImageListModel):
        super().__init__(parent)
        self.image_list_model = image_list_model
        self.settings = get_settings()
        self.setWindowTitle('Find and Replace')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(20)
        description_label = QLabel(
            'This is for finding and replacing arbitrary text in captions. '
            'You can also use the All Tags pane to rename or delete all '
            'instances of a tag.')
        description_label.setWordWrap(True)
        layout.addWidget(description_label)
        grid_layout = QGridLayout()
        grid_layout.addWidget(QLabel('Find text'), 0, 0,
                              Qt.AlignmentFlag.AlignRight)
        grid_layout.addWidget(QLabel('Replace with'), 1, 0,
                              Qt.AlignmentFlag.AlignRight)
        grid_layout.addWidget(QLabel('Scope'), 2, 0,
                              Qt.AlignmentFlag.AlignRight)
        grid_layout.addWidget(QLabel('Whole tags only'), 3, 0,
                              Qt.AlignmentFlag.AlignRight)
        grid_layout.addWidget(QLabel('Use regex for find text'), 4, 0,
                              Qt.AlignmentFlag.AlignRight)
        self.find_text_line_edit = SettingsLineEdit(key='find_text')
        self.find_text_line_edit.setClearButtonEnabled(True)
        self.find_text_line_edit.textChanged.connect(self.display_match_count)
        grid_layout.addWidget(self.find_text_line_edit, 0, 1)
        self.replace_text_line_edit = SettingsLineEdit(key='replace_text')
        self.replace_text_line_edit.setClearButtonEnabled(True)
        grid_layout.addWidget(self.replace_text_line_edit, 1, 1)
        self.scope_combo_box = SettingsComboBox(key='replace_scope')
        self.scope_combo_box.addItems(list(Scope))
        self.scope_combo_box.currentTextChanged.connect(
            self.display_match_count)
        grid_layout.addWidget(self.scope_combo_box, 2, 1)
        self.whole_tags_only_check_box = SettingsBigCheckBox(
            key='replace_whole_tags_only', default=False)
        self.whole_tags_only_check_box.stateChanged.connect(
            self.display_match_count)
        grid_layout.addWidget(self.whole_tags_only_check_box, 3, 1)
        self.use_regex_check_box = SettingsBigCheckBox(key='replace_use_regex',
                                                       default=False)
        self.use_regex_check_box.stateChanged.connect(self.display_match_count)
        grid_layout.addWidget(self.use_regex_check_box, 4, 1)
        layout.addLayout(grid_layout)
        # Always-visible preview of the tags that contain the find text. The
        # label and list live in their own tight sub-layout so the gap between
        # them stays small (the dialog's main 20px spacing would otherwise leave
        # a large gap after the label). The list is the stretchy element, so
        # when the user resizes the dialog the list grows to use the extra room.
        results_layout = QVBoxLayout()
        results_layout.setSpacing(4)
        self.matches_label = QLabel()
        self.matches_label.setSizePolicy(QSizePolicy.Policy.Preferred,
                                         QSizePolicy.Policy.Fixed)
        results_layout.addWidget(self.matches_label)
        self.matches_list = MatchesListWidget()
        self.matches_list.setUniformItemSizes(True)
        self.matches_list.setWordWrap(False)
        self.matches_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.matches_list.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        self.matches_list.setSizePolicy(QSizePolicy.Policy.Expanding,
                                        QSizePolicy.Policy.Expanding)
        results_layout.addWidget(self.matches_list)
        layout.addLayout(results_layout, stretch=1)
        self.replace_button = QPushButton('Replace')
        self.replace_button.clicked.connect(self.replace)
        self.replace_button.clicked.connect(self.display_match_count)
        layout.addWidget(self.replace_button)
        self.display_match_count()

    def disable_replace_button(self):
        self.replace_button.setText('Replace')
        self.replace_button.setEnabled(False)
        self.clear_matches_list()

    def clear_matches_list(self):
        self.matches_list.clear()
        self.matches_label.setText('Found in 0 tags:')

    @Slot()
    def display_match_count(self):
        text = self.find_text_line_edit.text()
        if not text:
            self.disable_replace_button()
            return
        self.replace_button.setEnabled(True)
        scope = self.scope_combo_box.currentText()
        whole_tags_only = self.whole_tags_only_check_box.isChecked()
        use_regex = self.use_regex_check_box.isChecked()
        try:
            match_count, matched_tags = self.image_list_model.get_text_matches(
                text, scope, whole_tags_only, use_regex)
        except re.error:
            self.disable_replace_button()
            return
        self.replace_button.setText(f'Replace {match_count} '
                                    f'{pluralize("instance", match_count)}')
        self.populate_matches_list(matched_tags)

    def populate_matches_list(self, matched_tags: list[tuple[str, int]]):
        self.matches_list.clear()
        tag_count = len(matched_tags)
        self.matches_label.setText(
            f'Found in {tag_count} {pluralize("tag", tag_count)}:')
        shown = matched_tags[:self.MAX_LISTED_MATCHES]
        self.matches_list.addItems(
            [f'{tag}  ({count})' for tag, count in shown])
        remaining = tag_count - len(shown)
        if remaining > 0:
            self.matches_list.addItem(
                f'\u2026 and {remaining} more '
                f'{pluralize("tag", remaining)}')

    @Slot()
    def replace(self):
        scope = self.scope_combo_box.currentText()
        use_regex = self.use_regex_check_box.isChecked()
        if self.whole_tags_only_check_box.isChecked():
            replace_text = self.replace_text_line_edit.text()
            if replace_text:
                self.image_list_model.rename_tags(
                    [self.find_text_line_edit.text()], replace_text, scope,
                    use_regex)
            else:
                self.image_list_model.delete_tags(
                    [self.find_text_line_edit.text()], scope, use_regex)
        else:
            self.image_list_model.find_and_replace(
                self.find_text_line_edit.text(),
                self.replace_text_line_edit.text(), scope, use_regex)
