from PySide6.QtCore import QMimeData, QModelIndex, QStringListModel, Qt, Slot
from PySide6.QtGui import QColor

from models.tag_library_model import TagLibraryModel


class ImageTagListModel(QStringListModel):
    def __init__(self, tag_library_model: TagLibraryModel):
        super().__init__()
        self.tag_library_model = tag_library_model
        self.tag_library_model.modelReset.connect(self.refresh_tag_colors)
        self.tag_library_model.categories_changed.connect(
            self.refresh_tag_colors)

    def data(self, index: QModelIndex, role=None) -> str | QColor | None:
        if role == Qt.ItemDataRole.ToolTipRole and index.isValid():
            return super().data(index, Qt.ItemDataRole.EditRole)
        if role == Qt.ItemDataRole.ForegroundRole and index.isValid():
            tag = super().data(index, Qt.ItemDataRole.EditRole)
            category = self.tag_library_model.get_category_for_tag(tag)
            if category:
                color = QColor(category['color'])
                if color.isValid():
                    return color
        return super().data(index, role)

    @Slot()
    def refresh_tag_colors(self):
        row_count = self.rowCount()
        if row_count == 0:
            return
        self.dataChanged.emit(
            self.index(0, 0), self.index(row_count - 1, 0),
            [Qt.ItemDataRole.ForegroundRole])

    def dropMimeData(self, data: QMimeData, action: Qt.DropAction, row: int,
                     column: int, parent: QModelIndex) -> bool:
        # Overriding this method like this somehow disables dropping a tag onto
        # another tag, preventing tags from being overwritten.
        return False
