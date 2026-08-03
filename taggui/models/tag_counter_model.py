from collections import Counter

from PySide6.QtCore import QAbstractListModel, Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QMessageBox

from models.tag_library_model import TagLibraryModel
from utils.image import Image
from utils.utils import get_confirmation_dialog_reply, list_with_and, pluralize


class TagCounterModel(QAbstractListModel):
    tags_renaming_requested = Signal(list, str)

    def __init__(self, tag_library_model: TagLibraryModel):
        super().__init__()
        self.tag_library_model = tag_library_model
        self.tag_counter = Counter()
        self.most_common_tags = []
        self.all_tags_list = None
        self.tag_library_model.modelReset.connect(self.refresh_tag_colors)
        self.tag_library_model.categories_changed.connect(
            self.refresh_tag_colors)

    def rowCount(self, parent=None) -> int:
        return len(self.most_common_tags)

    def data(self, index, role=None) -> tuple[str, int] | str | QColor | None:
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
        new_tag = value
        if not new_tag or role != Qt.ItemDataRole.EditRole:
            return False
        old_tag = self.data(index, Qt.ItemDataRole.EditRole)
        if new_tag == old_tag:
            return False
        selected_indices = self.all_tags_list.selectedIndexes()
        old_tags = []
        old_tags_count = 0
        for selected_index in selected_indices:
            old_tag, old_tag_count = selected_index.data(
                Qt.ItemDataRole.UserRole)
            old_tags.append(old_tag)
            old_tags_count += old_tag_count
        question = (f'Rename {old_tags_count} '
                    f'{pluralize("instance", old_tags_count)} of ')
        if len(old_tags) < 10:
            quoted_tags = [f'"{tag}"' for tag in old_tags]
            question += (f'{pluralize("tag", len(old_tags))} '
                         f'{list_with_and(quoted_tags)} ')
        else:
            question += f'{len(old_tags)} tags '
        question += f'to "{new_tag}"?'
        reply = get_confirmation_dialog_reply(
            title=f'Rename {pluralize("Tag", len(old_tags))}',
            question=question)
        if reply == QMessageBox.StandardButton.Yes:
            self.tags_renaming_requested.emit(old_tags, new_tag)
            return True
        return False

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
