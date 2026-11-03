from typing import Optional
import operator
from fnmatch import fnmatchcase

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt
from transformers import PreTrainedTokenizerBase

from models.image_list_model import ImageListModel
from utils.image import Image, build_caption_text
from utils.tag_filter import UNCATEGORIZED_VALUES


class ProxyImageListModel(QSortFilterProxyModel):
    def __init__(self, image_list_model: ImageListModel,
                 tokenizer: Optional[PreTrainedTokenizerBase], tag_separator: str,
                 tag_library_model=None):
        super().__init__()
        self.setSourceModel(image_list_model)
        self.tokenizer = tokenizer
        self.tag_separator = tag_separator
        self.tag_library_model = tag_library_model
        self.filter: list | None = None

    def set_tokenizer(self, tokenizer: PreTrainedTokenizerBase):
        self.tokenizer = tokenizer
        if self.filter is not None:
            self.invalidateFilter()

    def does_image_match_filter(self, image: Image,
                                filter_: list | str) -> bool:
        """Text matching ignores capitals, like the All Tags and Tag Library
        filters."""
        caption = build_caption_text(image.tags, image.natural_language_prompt,
                                     self.tag_separator).casefold()
        if isinstance(filter_, str):
            pattern = f'*{filter_.casefold()}*'
            return (fnmatchcase(caption, pattern)
                    or fnmatchcase(str(image.path).casefold(), pattern))
        if len(filter_) == 1:
            return self.does_image_match_filter(image, filter_[0])
        if len(filter_) == 2:
            if filter_[0] == 'NOT':
                return not self.does_image_match_filter(image, filter_[1])
            value = filter_[1].casefold()
            if filter_[0] == 'tag':
                return any(fnmatchcase(tag.casefold(), value)
                           for tag in image.tags)
            if filter_[0] == 'caption':
                return fnmatchcase(caption, f'*{value}*')
            if filter_[0] == 'name':
                return fnmatchcase(image.path.name.casefold(), f'*{value}*')
            if filter_[0] == 'path':
                return fnmatchcase(str(image.path).casefold(), f'*{value}*')
            if filter_[0] == 'nl':
                if value == 'true':
                    return bool(image.natural_language_prompt)
                if value == 'false':
                    return not image.natural_language_prompt
                return fnmatchcase(image.natural_language_prompt.casefold(),
                                   f'*{value}*')
            if filter_[0] == 'complete':
                if value == 'true':
                    return bool(image.is_complete)
                if value == 'false':
                    return not image.is_complete
                return False
            if filter_[0] == 'category':
                if self.tag_library_model is None:
                    return True
                # Same rules as the All Tags filter: `none`/`uncategorized`
                # find tags without a category, other values match part of
                # the category name.
                category_by_tag = self.tag_library_model.category_by_tag
                categories = self.tag_library_model.categories
                if value in UNCATEGORIZED_VALUES:
                    category_ids = {category['id'] for category in categories}
                    return any(category_by_tag.get(tag) not in category_ids
                               for tag in image.tags)
                pattern = f'*{value}*'
                matching_category_ids = {
                    category['id'] for category in categories
                    if fnmatchcase(category['name'].casefold(), pattern)}
                return any(category_by_tag.get(tag) in matching_category_ids
                           for tag in image.tags)
        if filter_[1] == 'AND':
            return (self.does_image_match_filter(image, filter_[0])
                    and self.does_image_match_filter(image, filter_[2:]))
        if filter_[1] == 'OR':
            return (self.does_image_match_filter(image, filter_[0])
                    or self.does_image_match_filter(image, filter_[2:]))
        comparison_operators = {
            '=': operator.eq,
            '==': operator.eq,
            '!=': operator.ne,
            '<': operator.lt,
            '>': operator.gt,
            '<=': operator.le,
            '>=': operator.ge
        }
        comparison_operator = comparison_operators[filter_[1]]
        number_to_compare = None
        if filter_[0] == 'tags':
            number_to_compare = len(image.tags)
        elif filter_[0] == 'chars':
            number_to_compare = len(caption)
        elif filter_[0] == 'tokens':
            if self.tokenizer is None:
                return True
            # Subtract 2 for the `<|startoftext|>` and `<|endoftext|>` tokens.
            number_to_compare = len(self.tokenizer(caption).input_ids) - 2
        return comparison_operator(number_to_compare, int(filter_[2]))

    def filterAcceptsRow(self, source_row: int,
                         source_parent: QModelIndex) -> bool:
        # Show all images if there is no filter.
        if self.filter is None:
            return True
        image_index = self.sourceModel().index(source_row, 0)
        image: Image = self.sourceModel().data(image_index,
                                               Qt.ItemDataRole.UserRole)
        return self.does_image_match_filter(image, self.filter)

    def is_image_in_filtered_images(self, image: Image) -> bool:
        return (self.filter is None
                or self.does_image_match_filter(image, self.filter))
