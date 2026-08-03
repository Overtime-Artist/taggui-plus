from PySide6.QtCore import QModelIndex, QSortFilterProxyModel

from models.tag_counter_model import TagCounterModel
from utils.enums import AllTagsSortBy
from utils.tag_filter import does_tag_match_filter


class ProxyTagCounterModel(QSortFilterProxyModel):
    def __init__(self, tag_counter_model: TagCounterModel):
        super().__init__()
        self.setSourceModel(tag_counter_model)
        self.tag_counter_model = tag_counter_model
        self.sort_by = None
        self.filter: list | str | None = None

    # Setting a sort role results in lots of calls to `data()` and is very
    # slow, so implement a custom `lessThan()` method instead.
    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        left_tag, left_count = self.tag_counter_model.most_common_tags[
            left.row()]
        right_tag, right_count = self.tag_counter_model.most_common_tags[
            right.row()]
        if self.sort_by == AllTagsSortBy.FREQUENCY:
            return left_count < right_count
        elif self.sort_by == AllTagsSortBy.NAME:
            return left_tag < right_tag
        elif self.sort_by == AllTagsSortBy.LENGTH:
            return len(left_tag) < len(right_tag)
        elif self.sort_by == AllTagsSortBy.CATEGORY:
            tag_library_model = self.tag_counter_model.tag_library_model
            category_order_map = tag_library_model.get_category_order_map()
            uncategorized_sort_index = len(category_order_map)
            left_category = tag_library_model.get_category_for_tag(left_tag)
            right_category = tag_library_model.get_category_for_tag(right_tag)
            left_sort_index = category_order_map.get(
                left_category['id'], uncategorized_sort_index
            ) if left_category else uncategorized_sort_index
            right_sort_index = category_order_map.get(
                right_category['id'], uncategorized_sort_index
            ) if right_category else uncategorized_sort_index
            return (left_sort_index, left_tag.casefold()) < (
                right_sort_index, right_tag.casefold())

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex):
        if self.filter is None:
            return True
        tag, count = self.tag_counter_model.most_common_tags[source_row]
        return does_tag_match_filter(
            self.filter, tag, count,
            self.tag_counter_model.tag_library_model)

    def set_filter(self, filter_: list | str | None):
        self.filter = filter_
        self.invalidate()
