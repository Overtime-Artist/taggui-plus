from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt

from utils.tag_filter import does_tag_match_filter


class TagLibraryFilterProxyModel(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.filter: list | str | None = None
        # Model used to resolve a tag's category for `category:` filters. When
        # `None`, the source model is used (the source model is the tag library
        # model itself in the Tag Library pane). Subclasses whose source model
        # is not the tag library model (e.g. the Manage Library dialog's table
        # model) set this to the underlying tag library model.
        self.category_lookup_model = None

    def set_filter(self, filter_: list | str | None):
        self.filter = filter_
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int,
                         source_parent: QModelIndex) -> bool:
        if self.filter is None:
            return True
        source_index = self.sourceModel().index(source_row, 0, source_parent)
        tag = source_index.data(Qt.ItemDataRole.EditRole)
        if not tag:
            return False
        category_model = (self.category_lookup_model
                          if self.category_lookup_model is not None
                          else self.sourceModel())
        # `count` is `None` because tag frequency is not available in the Tag
        # Library, so a `count:` filter simply never matches here.
        return does_tag_match_filter(self.filter, str(tag), None,
                                     category_model)
