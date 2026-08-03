from typing import Optional
import operator
from fnmatch import fnmatchcase

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt
from transformers import PreTrainedTokenizerBase

from models.image_list_model import ImageListModel
from utils.image import Image, build_caption_text


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
        caption = build_caption_text(image.tags, image.natural_language_prompt,
                                     self.tag_separator)
        if isinstance(filter_, str):
            return (fnmatchcase(caption, f'*{filter_}*')
                    or fnmatchcase(str(image.path), f'*{filter_}*'))
        if len(filter_) == 1:
            return self.does_image_match_filter(image, filter_[0])
        if len(filter_) == 2:
            if filter_[0] == 'NOT':
                return not self.does_image_match_filter(image, filter_[1])
            if filter_[0] == 'tag':
                return any(fnmatchcase(tag, filter_[1]) for tag in image.tags)
            if filter_[0] == 'caption':
                return fnmatchcase(caption, f'*{filter_[1]}*')
            if filter_[0] == 'name':
                return fnmatchcase(image.path.name, f'*{filter_[1]}*')
            if filter_[0] == 'path':
                return fnmatchcase(str(image.path), f'*{filter_[1]}*')
            if filter_[0] == 'nl':
                if filter_[1].casefold() == 'true':
                    return bool(image.natural_language_prompt)
                if filter_[1].casefold() == 'false':
                    return not image.natural_language_prompt
                return fnmatchcase(image.natural_language_prompt,
                                   f'*{filter_[1]}*')
            if filter_[0] == 'complete':
                if filter_[1].casefold() == 'true':
                    return bool(image.is_complete)
                if filter_[1].casefold() == 'false':
                    return not image.is_complete
                return False
            if filter_[0] == 'category':
               if self.tag_library_model is None:
                   return True
               category_name = filter_[1].casefold()
               categories = self.tag_library_model.categories
               # Find the category ID matching the category name
               matching_category_id = None
               for cat in categories:
                   if cat['name'].casefold() == category_name:
                       matching_category_id = cat['id']
                       break
               if matching_category_id is None:
                   return False
               # Check if any image tags belong to this category
               category_by_tag = self.tag_library_model.category_by_tag
               return any(category_by_tag.get(tag) == matching_category_id
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
