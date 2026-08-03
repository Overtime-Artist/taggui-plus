from enum import Enum


# `StrEnum` is a Python 3.11 feature that can be used here.
class AllTagsSortBy(str, Enum):
    FREQUENCY = 'Frequency'
    NAME = 'Name'
    LENGTH = 'Length'
    CATEGORY = 'Category'


class ThemeMode(str, Enum):
    LIGHT = 'Light'
    DARK = 'Dark'


class SortOrder(str, Enum):
    ASCENDING = 'Ascending'
    DESCENDING = 'Descending'


class ImageListSortBy(str, Enum):
    PATH = 'Path'
    NAME = 'Name'
    MODIFIED_TIME = 'Modified time'
    CREATED_TIME = 'Created time'
    FILE_SIZE = 'File size'
    RESOLUTION = 'Image resolution'
    TAG_COUNT = 'Tag count'
    TOKEN_COUNT = 'Token count'
    NATURAL_LANGUAGE_PROMPT_LENGTH = 'Natural language prompt length'


class AllTagsFilterLogic(str, Enum):
    AND = 'AND'
    OR = 'OR'


class CaptionPosition(str, Enum):
    BEFORE_FIRST_TAG = 'Insert before first tag'
    AFTER_LAST_TAG = 'Insert after last tag'
    OVERWRITE_FIRST_TAG = 'Overwrite first tag'
    OVERWRITE_ALL_TAGS = 'Overwrite all tags'
    DO_NOT_ADD = 'Do not add to tags'


class CaptionDestination(str, Enum):
    TAGS = 'Tags'
    NATURAL_LANGUAGE = 'Natural language'


class NaturalLanguagePosition(str, Enum):
    BEFORE_EXISTING_TEXT = 'Add before current text'
    AFTER_EXISTING_TEXT = 'Add after current text'
    OVERWRITE_EXISTING_TEXT = 'Overwrite current text'
    DO_NOT_ADD = 'Do not add to text'


class CaptionDevice(str, Enum):
    GPU = 'GPU if available'
    CPU = 'CPU'
