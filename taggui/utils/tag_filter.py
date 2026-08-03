import operator
from functools import reduce
from operator import or_
from fnmatch import fnmatchcase

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QLineEdit
from pyparsing import (CaselessKeyword, CaselessLiteral, Group, OpAssoc,
                       ParseException, QuotedString, Suppress, Word,
                       infix_notation, nums, one_of, printables)

# Comparison operators shared by the number filters (`count:` and `length:`).
COMPARISON_OPERATORS = {
    '=': operator.eq,
    '==': operator.eq,
    '!=': operator.ne,
    '<': operator.lt,
    '>': operator.gt,
    '<=': operator.le,
    '>=': operator.ge,
}

# Values that mean "tag has no assigned category" for the `category:` filter.
UNCATEGORIZED_VALUES = {'none', 'uncategorized', ''}


def replace_filter_wildcards(filter_: str | list) -> str | list:
    """
    Replace escaped wildcard characters (`\\*` and `\\?`) so they are treated
    as literal characters by the `fnmatch` module instead of wildcards.
    """
    if isinstance(filter_, str):
        return filter_.replace(r'\*', '[*]').replace(r'\?', '[?]')
    return [replace_filter_wildcards(element) for element in filter_]


def build_tag_filter_parser():
    """
    Build a pyparsing parser for the tag filter syntax. It mirrors the Images
    pane filter: `tag:` and `category:` string filters, `count:` and `length:`
    number filters, the `AND`/`OR`/`NOT` operators, grouping with parentheses,
    quoted strings, and `*`/`?` wildcards.
    """
    optionally_quoted_string = (QuotedString(quote_char='"', esc_char='\\')
                                | QuotedString(quote_char="'", esc_char='\\')
                                | Word(printables, exclude_chars='()'))
    string_filter_keys = ['tag', 'category']
    string_filter_expressions = [Group(CaselessLiteral(key) + Suppress(':')
                                       + optionally_quoted_string)
                                 for key in string_filter_keys]
    comparison_operator = one_of('= == != < > <= >=')
    number_filter_keys = ['count', 'length']
    number_filter_expressions = [Group(CaselessLiteral(key) + Suppress(':')
                                       + comparison_operator + Word(nums))
                                 for key in number_filter_keys]
    string_filter_expressions = reduce(or_, string_filter_expressions)
    number_filter_expressions = reduce(or_, number_filter_expressions)
    filter_expressions = (string_filter_expressions
                          | number_filter_expressions
                          | optionally_quoted_string)
    return infix_notation(
        filter_expressions,
        # Operator, number of operands, associativity.
        [(CaselessKeyword('NOT'), 1, OpAssoc.RIGHT),
         (CaselessKeyword('AND'), 2, OpAssoc.LEFT),
         (CaselessKeyword('OR'), 2, OpAssoc.LEFT)])


def does_tag_match_filter(filter_: list | str, tag: str, count: int | None,
                          tag_library_model) -> bool:
    """
    Return whether `tag` matches the parsed `filter_`. `count` is the tag
    frequency (used by `count:`), or `None` when frequency is not applicable
    (e.g. the Tag Library, where a `count:` filter simply never matches).
    """
    tag_casefolded = tag.casefold()
    if isinstance(filter_, str):
        # A bare term matches anywhere in the tag name.
        return fnmatchcase(tag_casefolded, f'*{filter_.casefold()}*')
    if len(filter_) == 1:
        return does_tag_match_filter(filter_[0], tag, count, tag_library_model)
    if len(filter_) == 2:
        if filter_[0] == 'NOT':
            return not does_tag_match_filter(filter_[1], tag, count,
                                             tag_library_model)
        key, value = filter_[0], filter_[1]
        if key == 'tag':
            return fnmatchcase(tag_casefolded, value.casefold())
        if key == 'category':
            category = (tag_library_model.get_category_for_tag(tag)
                        if tag_library_model is not None else None)
            if value.casefold() in UNCATEGORIZED_VALUES:
                return category is None
            if category is None:
                return False
            return fnmatchcase(category['name'].casefold(),
                               f'*{value.casefold()}*')
        return False
    # `len(filter_) >= 3`: either a boolean expression or a number comparison.
    if filter_[1] == 'AND':
        return (does_tag_match_filter(filter_[0], tag, count,
                                      tag_library_model)
                and does_tag_match_filter(filter_[2:], tag, count,
                                          tag_library_model))
    if filter_[1] == 'OR':
        return (does_tag_match_filter(filter_[0], tag, count,
                                      tag_library_model)
                or does_tag_match_filter(filter_[2:], tag, count,
                                         tag_library_model))
    comparison_operator = COMPARISON_OPERATORS[filter_[1]]
    if filter_[0] == 'count':
        if count is None:
            return False
        number_to_compare = count
    elif filter_[0] == 'length':
        number_to_compare = len(tag)
    else:
        return False
    return comparison_operator(number_to_compare, int(filter_[2]))


class TagFilterLineEdit(QLineEdit):
    """
    A line edit that parses the shared tag filter syntax. `parse_filter_text`
    returns the parsed filter (a nested list or string), or `None` when the
    field is empty or the text cannot be parsed.
    """

    def __init__(self, placeholder_text: str):
        super().__init__()
        self.setPlaceholderText(placeholder_text)
        self.setTextMargins(8, 0, 8, 0)
        self.setClearButtonEnabled(True)
        self.filter_text_parser = build_tag_filter_parser()

    def parse_filter_text(self) -> list | str | None:
        filter_text = self.text()
        if not filter_text:
            self.setPalette(QApplication.palette(self))
            return None
        try:
            filter_ = self.filter_text_parser.parse_string(
                filter_text, parse_all=True).as_list()[0]
            filter_ = replace_filter_wildcards(filter_)
            self.setPalette(QApplication.palette(self))
            return filter_
        except ParseException:
            # Change the background color when the filter text is invalid.
            invalid_palette = QPalette(self.palette())
            if self.palette().color(
                    self.backgroundRole()).lightness() < 128:
                # Dark red for dark mode.
                invalid_palette.setColor(QPalette.ColorRole.Base,
                                         QColor('#442222'))
            else:
                # Light red for light mode.
                invalid_palette.setColor(QPalette.ColorRole.Base,
                                         QColor('#ffdddd'))
            self.setPalette(invalid_palette)
            return None
