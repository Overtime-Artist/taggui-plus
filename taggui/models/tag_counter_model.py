from collections import Counter

from PySide6.QtCore import QAbstractListModel, Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QMessageBox

from models.tag_library_model import TagLibraryModel
from utils.image import Image
from utils.settings import get_tag_separator
from utils.utils import get_confirmation_dialog_reply, list_with_and, pluralize


class TagCounterModel(QAbstractListModel):
    tags_renaming_requested = Signal(list, list)

    def __init__(self, tag_library_model: TagLibraryModel):
        super().__init__()
        self.tag_library_model = tag_library_model
        self.tag_counter = Counter()
        self.most_common_tags = []
        self.all_tags_list = None
        # The first new tag of the last confirmed rename, so the All Tags list
        # can select it afterwards.
        self.renamed_tag = None
        self.tag_library_model.modelReset.connect(self.refresh_tag_colors)
        self.tag_library_model.categories_changed.connect(
            self.refresh_tag_colors)

    def rowCount(self, parent=None) -> int:
        return len(self.most_common_tags)

    def data(self, index, role=None) -> tuple[str, int] | str | QColor | None:
        if (index is None or not index.isValid()
                or not 0 <= index.row() < len(self.most_common_tags)):
            return None
        tag, count = self.most_common_tags[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return tag, count
        if role == Qt.ItemDataRole.DisplayRole:
            return f'{tag} ({count})'
        if role == Qt.ItemDataRole.EditRole:
            return tag
        if role == Qt.ItemDataRole.ToolTipRole:
            return f'{tag} ({count})'
        if role == Qt.ItemDataRole.ForegroundRole:
            category = self.tag_library_model.get_category_for_tag(tag)
            if category:
                color = QColor(category['color'])
                if color.isValid():
                    return color
        return None

    def flags(self, index) -> Qt.ItemFlag:
        """Make the tags editable."""
        return (Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable
                | Qt.ItemFlag.ItemIsEnabled)

    def setData(self, index, value: str,
                role=Qt.ItemDataRole.EditRole) -> bool:
        if not value or role != Qt.ItemDataRole.EditRole:
            return False
        edited_tag = self.data(index, Qt.ItemDataRole.EditRole)
        if edited_tag is None:
            return False
        # Rename the edited tag. The other selected tags are renamed with it
        # only when the edited tag is part of the selection.
        selected_tags = {}
        for selected_index in self.all_tags_list.selectedIndexes():
            selected_data = selected_index.data(Qt.ItemDataRole.UserRole)
            if selected_data:
                selected_tags[selected_data[0]] = selected_data[1]
        if edited_tag in selected_tags:
            old_tags = list(selected_tags)
        else:
            old_tags = [edited_tag]
        new_tags = self.get_new_tags(value, old_tags)
        if not new_tags or new_tags == [edited_tag]:
            return False
        old_tags_count = sum(self.tag_counter[tag] for tag in old_tags)
        question = (f'Rename {old_tags_count} '
                    f'{pluralize("instance", old_tags_count)} of ')
        if len(old_tags) < 10:
            quoted_tags = [f'"{tag}"' for tag in old_tags]
            question += (f'{pluralize("tag", len(old_tags))} '
                         f'{list_with_and(quoted_tags)} ')
        else:
            question += f'{len(old_tags)} tags '
        quoted_new_tags = [f'"{tag}"' for tag in new_tags]
        question += f'to {list_with_and(quoted_new_tags)}?'
        reply = get_confirmation_dialog_reply(
            title=f'Rename {pluralize("Tag", len(old_tags))}',
            question=question)
        if reply == QMessageBox.StandardButton.Yes:
            self.renamed_tag = new_tags[0]
            self.tags_renaming_requested.emit(old_tags, new_tags)
            return True
        return False

    def get_new_tags(self, text: str, old_tags: list[str]) -> list[str]:
        """Turn the typed name into the list of new tags: trimmed, split on
        the tag separator like the Add Tag box, and using the existing spelling
        when a tag differing only in letter case is already in use."""
        existing_spellings = {}
        for tag in self.tag_counter:
            if tag not in old_tags:
                existing_spellings.setdefault(tag.casefold(), tag)
        new_tags = []
        for part in text.split(get_tag_separator()):
            part = part.strip()
            if not part:
                continue
            part = existing_spellings.get(part.casefold(), part)
            if part not in new_tags:
                new_tags.append(part)
        return new_tags

    @Slot()
    def count_tags(self, images: list[Image]):
        self.beginResetModel()
        self.tag_counter.clear()
        for image in images:
            self.tag_counter.update(image.tags)
        self.most_common_tags = self.tag_counter.most_common()
        self.endResetModel()

    @Slot()
    def refresh_tag_colors(self):
        row_count = self.rowCount()
        if row_count == 0:
            return
        self.dataChanged.emit(
            self.index(0), self.index(row_count - 1),
            [Qt.ItemDataRole.ForegroundRole])
