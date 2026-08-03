from html import escape
import shutil
import subprocess
from enum import Enum
from functools import reduce
from operator import or_
from pathlib import Path

from PySide6.QtCore import (QEvent, QFile, QItemSelection, QItemSelectionModel,
                            QItemSelectionRange, QModelIndex, QPointF, QRectF,
                            QSize, QUrl, Qt, Signal, Slot)
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QFont, QKeyEvent,
                           QPainter, QPalette, QPen, QTextDocument)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDockWidget,
                               QFileDialog, QHBoxLayout, QLabel, QInputDialog,
                               QLineEdit, QListView, QMenu, QMessageBox,
                               QPushButton, QStyle,
                               QStyleOptionViewItem, QStyledItemDelegate,
                               QVBoxLayout, QWidget)
from pyparsing import (CaselessKeyword, CaselessLiteral, Group, OpAssoc,
                       ParseException, QuotedString, Suppress, Word,
                       infix_notation, nums, one_of, printables)

from models.proxy_image_list_model import ProxyImageListModel
from utils.elided_tooltip import ElidedToolTipListView
from utils.enums import ImageListSortBy, SortOrder
from utils.image import Image, build_caption_text
from utils.settings import DEFAULT_SETTINGS, get_settings
from utils.completion_store import get_completion_store
from utils.settings_widgets import SettingsComboBox
from utils.utils import get_confirmation_dialog_reply, pluralize


def replace_filter_wildcards(filter_: str | list) -> str | list:
    """
    Replace escaped wildcard characters to make them compatible with the
    `fnmatch` module.
    """
    if isinstance(filter_, str):
        filter_ = filter_.replace(r'\*', '[*]').replace(r'\?', '[?]')
        return filter_
    replaced_filter = []
    for element in filter_:
        replaced_element = replace_filter_wildcards(element)
        replaced_filter.append(replaced_element)
    return replaced_filter


class FilterLineEdit(QLineEdit):
    def __init__(self):
        super().__init__()
        self.setPlaceholderText('Filter Images')
        self.setTextMargins(8, 0, 8, 0)
        self.setClearButtonEnabled(True)
        optionally_quoted_string = (QuotedString(quote_char='"', esc_char='\\')
                                    | QuotedString(quote_char="'",
                                                   esc_char='\\')
                                    | Word(printables, exclude_chars='()'))
        string_filter_keys = ['tag', 'caption', 'name', 'path', 'nl',
                               'category', 'complete']
        string_filter_expressions = [Group(CaselessLiteral(key) + Suppress(':')
                                           + optionally_quoted_string)
                                     for key in string_filter_keys]
        comparison_operator = one_of('= == != < > <= >=')
        number_filter_keys = ['tags', 'chars', 'tokens']
        number_filter_expressions = [Group(CaselessLiteral(key) + Suppress(':')
                                           + comparison_operator + Word(nums))
                                     for key in number_filter_keys]
        string_filter_expressions = reduce(or_, string_filter_expressions)
        number_filter_expressions = reduce(or_, number_filter_expressions)
        filter_expressions = (string_filter_expressions
                              | number_filter_expressions
                              | optionally_quoted_string)
        self.filter_text_parser = infix_notation(
            filter_expressions,
            # Operator, number of operands, associativity.
            [(CaselessKeyword('NOT'), 1, OpAssoc.RIGHT),
             (CaselessKeyword('AND'), 2, OpAssoc.LEFT),
             (CaselessKeyword('OR'), 2, OpAssoc.LEFT)])

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
            if self.palette().color(self.backgroundRole()).lightness() < 128:
                # Dark red for dark mode.
                invalid_palette.setColor(QPalette.ColorRole.Base, QColor('#442222'))
            else:
                # Light red for light mode.
                invalid_palette.setColor(QPalette.ColorRole.Base, QColor('#ffdddd'))
            self.setPalette(invalid_palette)
            return None


class SelectionMode(str, Enum):
    DEFAULT = 'Default'
    TOGGLE = 'Toggle'


IMAGE_LIST_SORT_DISPLAY_LABELS = {
    ImageListSortBy.PATH: 'Path',
    ImageListSortBy.NAME: 'Name',
    ImageListSortBy.MODIFIED_TIME: 'Modified',
    ImageListSortBy.CREATED_TIME: 'Created',
    ImageListSortBy.FILE_SIZE: 'File size',
    ImageListSortBy.RESOLUTION: 'Resolution',
    ImageListSortBy.TAG_COUNT: 'Tag count',
    ImageListSortBy.TOKEN_COUNT: 'Token count',
    ImageListSortBy.NATURAL_LANGUAGE_PROMPT_LENGTH: 'NL length'
}


class ImageListItemDelegate(QStyledItemDelegate):
    def __init__(self, parent=None, tag_separator: str = ', ',
                 get_category_for_tag=None):
        super().__init__(parent)
        self.tag_separator = tag_separator
        self.get_category_for_tag = get_category_for_tag
        self.badge_font_size = DEFAULT_SETTINGS['image_list_resolution_badge_font_size']
        self.badge_background_alpha = self.transparency_to_alpha(
            DEFAULT_SETTINGS['image_list_resolution_badge_transparency'])
        self.show_badge = DEFAULT_SETTINGS['image_list_show_resolution_badge']
        self.show_completion_icon = DEFAULT_SETTINGS[
            'image_list_show_completion_icon']
        # Reused across paint calls to avoid repeated allocation/deallocation.
        self._rich_doc = QTextDocument()

    def transparency_to_alpha(self, transparency: int) -> int:
        clamped_transparency = max(0, min(transparency, 100))
        return round(255 * (100 - clamped_transparency) / 100)

    def set_badge_style(self, font_size: int, transparency: int,
                        show_badge: bool = True):
        self.badge_font_size = max(font_size, 1)
        self.badge_background_alpha = self.transparency_to_alpha(transparency)
        self.show_badge = show_badge

    def set_show_completion_icon(self, show_completion_icon: bool):
        self.show_completion_icon = show_completion_icon

    def get_tag_color(self, tag: str) -> str | None:
        if self.get_category_for_tag is None:
            return None
        category = self.get_category_for_tag(tag)
        if not category:
            return None
        color = QColor(category.get('color', ''))
        if not color.isValid():
            return None
        return color.name()

    def build_item_html(self, image: Image, option: QStyleOptionViewItem) -> str:
        default_text_color = (option.palette.color(QPalette.ColorRole.HighlightedText)
                              if option.state & QStyle.StateFlag.State_Selected
                              else option.palette.color(QPalette.ColorRole.Text))
        html_parts = [escape(image.path.name)]
        if image.natural_language_prompt:
            html_parts.append(' [NL]')

        if image.tags:
            tags_html_parts = []
            for index, tag in enumerate(image.tags):
                if index > 0:
                    tags_html_parts.append(escape(self.tag_separator))
                color = self.get_tag_color(tag)
                if color:
                    tags_html_parts.append(
                        f'<span style="color: {color};">{escape(tag)}</span>')
                else:
                    tags_html_parts.append(escape(tag))
            html_parts.append('<br>')
            html_parts.append(''.join(tags_html_parts))

        if image.natural_language_prompt:
            html_parts.append('<br>')
            html_parts.append(
                escape(image.natural_language_prompt).replace('\n', '<br>'))

        return (f'<div style="margin: 0; color: {default_text_color.name()};">'
                f'{"".join(html_parts)}</div>')

    def sizeHint(self, option: QStyleOptionViewItem,
                 index: QModelIndex) -> QSize:
        # Fast sizeHint that avoids the default O(n) text-layout measurement.
        # We compute icon height from the stored image dimensions (O(1) lookup)
        # and use a fixed text area height. paint() clips text to available space.
        icon_w = option.decorationSize.width()
        icon_max_h = option.decorationSize.height()
        if icon_w <= 0 or icon_max_h <= 0:
            return super().sizeHint(option, index)
        image = index.data(Qt.ItemDataRole.UserRole)
        if image and image.dimensions and image.dimensions[0] > 0:
            aspect = image.dimensions[1] / image.dimensions[0]
            icon_h = min(round(icon_w * aspect), icon_max_h)
        else:
            icon_h = min(icon_w, icon_max_h)
        fm_h = option.fontMetrics.height()
        return QSize(icon_w, max(icon_h, fm_h * 3 + 4))

    def _has_rich_content(self, image: 'Image') -> bool:
        """Returns True if this item needs HTML/rich-text rendering."""
        if image.natural_language_prompt:
            return True
        if not image.tags or self.get_category_for_tag is None:
            return False
        for tag in image.tags:
            cat = self.get_category_for_tag(tag)
            if cat and QColor(cat.get('color', '')).isValid():
                return True
        return False

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex):
        image = index.data(Qt.ItemDataRole.UserRole)
        if image is None:
            super().paint(painter, option, index)
            return

        option_copy = QStyleOptionViewItem(option)
        self.initStyleOption(option_copy, index)
        style = (option_copy.widget.style()
                 if option_copy.widget is not None
                 else QApplication.style())
        option_copy.text = ''
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option_copy,
                          painter, option_copy.widget)

        text_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, option_copy,
            option_copy.widget)
        if not text_rect.isEmpty():
            if self._has_rich_content(image):
                # Rich path: HTML rendering for colored tags / NL prompt.
                # Reuse the document object to avoid repeated allocation.
                doc = self._rich_doc
                doc.setDocumentMargin(0)
                doc.setDefaultFont(option_copy.font)
                doc.setHtml(self.build_item_html(image, option_copy))
                doc.setTextWidth(text_rect.width())
                painter.save()
                painter.translate(text_rect.topLeft())
                painter.setClipRect(
                    QRectF(0, 0, text_rect.width(), text_rect.height()))
                doc.drawContents(
                    painter,
                    QRectF(0, 0, text_rect.width(), text_rect.height()))
                painter.restore()
            else:
                # Fast path: plain QPainter text — no HTML parsing,
                # no QTextDocument allocation. ~50x faster per item.
                default_color = (
                    option.palette.color(QPalette.ColorRole.HighlightedText)
                    if option.state & QStyle.StateFlag.State_Selected
                    else option.palette.color(QPalette.ColorRole.Text))
                text = image.path.name
                if image.tags:
                    text += '\n' + self.tag_separator.join(image.tags)
                painter.save()
                painter.translate(text_rect.topLeft())
                painter.setClipRect(
                    QRectF(0, 0, text_rect.width(), text_rect.height()))
                painter.setPen(default_color)
                painter.drawText(
                    QRectF(0, 0, text_rect.width(), text_rect.height()),
                    Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                    text)
                painter.restore()

        if self.show_completion_icon and image.is_complete:
            option_copy = QStyleOptionViewItem(option)
            self.initStyleOption(option_copy, index)
            icon_rect = style.subElementRect(
                QStyle.SubElement.SE_ItemViewItemDecoration, option_copy,
                option_copy.widget)
            if not icon_rect.isEmpty():
                diameter = max(16, round(option.fontMetrics.height() * 1.15))
                badge_rect = QRectF(0, 0, diameter, diameter)
                badge_rect.moveTopRight(QRectF(icon_rect).topRight())
                badge_rect.translate(-4, 4)
                # Hollow green ring with a green checkmark; the interior is
                # left transparent so the thumbnail shows through.
                stroke_width = max(1.5, diameter * 0.11)
                green = QColor('#3cc23c')
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                pen = QPen(green, stroke_width)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                ring_rect = badge_rect.adjusted(
                    stroke_width / 2, stroke_width / 2,
                    -stroke_width / 2, -stroke_width / 2)
                painter.drawEllipse(ring_rect)
                # Checkmark drawn as a two-segment polyline inside the ring.
                left = badge_rect.left() + diameter * 0.28
                right = badge_rect.right() - diameter * 0.24
                mid_x = badge_rect.left() + diameter * 0.44
                top_y = badge_rect.top() + diameter * 0.38
                mid_y = badge_rect.top() + diameter * 0.52
                bottom_y = badge_rect.top() + diameter * 0.66
                painter.drawPolyline([
                    QPointF(left, mid_y),
                    QPointF(mid_x, bottom_y),
                    QPointF(right, top_y)])
                painter.restore()

        if not self.show_badge or not image.dimensions:
            return
        option_copy = QStyleOptionViewItem(option)
        self.initStyleOption(option_copy, index)
        icon_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemDecoration, option_copy,
            option_copy.widget)
        if icon_rect.isEmpty():
            return
        resolution_text = f'{image.dimensions[0]}x{image.dimensions[1]}'
        overlay_font = QFont(option.font)
        overlay_font.setPointSize(self.badge_font_size)
        painter.save()
        painter.setFont(overlay_font)
        metrics = painter.fontMetrics()
        text_rect = metrics.boundingRect(resolution_text)
        horizontal_padding = max(3, self.badge_font_size // 3)
        vertical_padding = max(1, self.badge_font_size // 5)
        overlay_rect = text_rect.adjusted(-horizontal_padding, -vertical_padding,
                                          horizontal_padding, vertical_padding)
        overlay_rect.moveBottomLeft(icon_rect.bottomLeft())
        overlay_rect.translate(4, -4)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, self.badge_background_alpha))
        painter.drawRoundedRect(overlay_rect, 4, 4)
        painter.setPen(QColor('#ffffff'))
        painter.drawText(overlay_rect,
                         Qt.AlignmentFlag.AlignCenter
                         | Qt.TextFlag.TextSingleLine,
                         resolution_text)
        painter.restore()


class ImageListView(ElidedToolTipListView):
    tags_paste_requested = Signal(list, list)
    directory_reload_requested = Signal()

    def __init__(self, parent, proxy_image_list_model: ProxyImageListModel,
                 tag_separator: str, image_width: int,
                 get_category_for_tag=None):
        super().__init__(parent)
        self.proxy_image_list_model = proxy_image_list_model
        self.tag_separator = tag_separator
        # Set by the main window once the Image Tags pane exists. Used by the
        # "type to add a tag" feature to move focus to the Add Tag input.
        self.image_tags_editor = None
        self.setModel(proxy_image_list_model)
        self.setWordWrap(True)
        self.item_delegate = ImageListItemDelegate(
            self, tag_separator=tag_separator,
            get_category_for_tag=get_category_for_tag)
        self.setItemDelegate(self.item_delegate)
        self.refresh_resolution_badge_style()
        # If the actual height of the image is greater than 3 times the width,
        # the image will be scaled down to fit.
        self.setIconSize(QSize(image_width, image_width * 3))

        self.invert_selection_action = self.addAction('Invert Selection')
        self.invert_selection_action.setShortcut('Ctrl+I')
        self.invert_selection_action.triggered.connect(self.invert_selection)
        self.copy_tags_action = self.addAction('Copy Tags')
        self.copy_tags_action.setShortcut('Ctrl+C')
        self.copy_tags_action.triggered.connect(
            self.copy_selected_image_tags)
        self.copy_caption_action = self.addAction('Copy Caption')
        self.copy_caption_action.setShortcut('Ctrl+Shift+C')
        self.copy_caption_action.triggered.connect(
            self.copy_selected_image_captions)
        self.copy_prompt_action = self.addAction(
            'Copy Natural Language Prompt')
        self.copy_prompt_action.setShortcut('Ctrl+N')
        self.copy_prompt_action.triggered.connect(
            self.copy_selected_image_prompts)
        self.paste_tags_action = self.addAction('Paste Tags')
        self.paste_tags_action.setShortcut('Ctrl+V')
        self.paste_tags_action.triggered.connect(
            self.paste_tags)
        self.copy_file_names_action = self.addAction('Copy File Name')
        self.copy_file_names_action.setShortcut('Ctrl+Shift+P')
        self.copy_file_names_action.triggered.connect(
            self.copy_selected_image_file_names)
        self.copy_paths_action = self.addAction('Copy Path')
        self.copy_paths_action.setShortcut('Ctrl+P')
        self.copy_paths_action.triggered.connect(
            self.copy_selected_image_paths)
        self.move_images_action = self.addAction('Move Images to...')
        self.move_images_action.setShortcut('Ctrl+M')
        self.move_images_action.triggered.connect(
            self.move_selected_images)
        self.copy_images_action = self.addAction('Copy Images to...')
        self.copy_images_action.setShortcut('Ctrl+Shift+M')
        self.copy_images_action.triggered.connect(
            self.copy_selected_images)
        self.delete_images_action = self.addAction('Delete Images')
        # Setting the shortcut to `Del` creates a conflict with tag deletion.
        self.delete_images_action.setShortcut('Ctrl+Del')
        self.delete_images_action.triggered.connect(
            self.delete_selected_images)
        self.open_image_action = self.addAction('Open Image in Default App')
        self.open_image_action.setShortcut('Ctrl+O')
        self.open_image_action.triggered.connect(self.open_image)
        self.open_image_editor_action = self.addAction(
            'Open Image in Configured Editor')
        self.open_image_editor_action.setShortcut('Ctrl+E')
        self.open_image_editor_action.triggered.connect(
            self.open_image_in_editor)
        self.rename_image_action = self.addAction('Rename Image File...')
        self.rename_image_action.setShortcut('F2')
        self.rename_image_action.triggered.connect(self.rename_image_file)
        self.open_caption_file_action = self.addAction(
            'Open Caption File in Default App')
        self.open_caption_file_action.setShortcut('Ctrl+Shift+O')
        self.open_caption_file_action.triggered.connect(
            self.open_caption_file)
        self.mark_complete_action = self.addAction('Mark as Complete')
        self.mark_complete_action.setShortcut('Ctrl+K')
        self.mark_complete_action.triggered.connect(
            lambda: self.set_selected_images_complete(True))
        self.mark_incomplete_action = self.addAction('Mark as Incomplete')
        self.mark_incomplete_action.setShortcut('Ctrl+Shift+K')
        self.mark_incomplete_action.triggered.connect(
            lambda: self.set_selected_images_complete(False))
        self.select_all_images_action = self.addAction('Select All Images')
        self.select_all_images_action.setShortcut('Ctrl+A')
        self.select_all_images_action.triggered.connect(self.selectAll)

        self.context_menu = QMenu(self)
        self.context_menu.addAction(self.select_all_images_action)
        self.context_menu.addAction(self.invert_selection_action)
        self.context_menu.addSeparator()
        self.context_menu.addAction(self.copy_tags_action)
        self.context_menu.addAction(self.copy_caption_action)
        self.context_menu.addAction(self.copy_prompt_action)
        self.context_menu.addAction(self.paste_tags_action)
        self.context_menu.addAction(self.copy_file_names_action)
        self.context_menu.addAction(self.copy_paths_action)
        self.context_menu.addSeparator()
        self.context_menu.addAction(self.move_images_action)
        self.context_menu.addAction(self.copy_images_action)
        self.context_menu.addAction(self.delete_images_action)
        self.context_menu.addAction(self.open_image_action)
        self.context_menu.addAction(self.open_image_editor_action)
        self.context_menu.addAction(self.rename_image_action)
        self.context_menu.addAction(self.open_caption_file_action)
        self.context_menu.addSeparator()
        self.context_menu.addAction(self.mark_complete_action)
        self.context_menu.addAction(self.mark_incomplete_action)
        self.selectionModel().selectionChanged.connect(
            self.update_context_menu_actions)

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.ToolTip:
            return True
        return super().viewportEvent(event)

    def refresh_resolution_badge_style(self):
        settings = get_settings()
        badge_font_size = settings.value(
            'image_list_resolution_badge_font_size',
            defaultValue=DEFAULT_SETTINGS['image_list_resolution_badge_font_size'],
            type=int)
        badge_transparency = settings.value(
            'image_list_resolution_badge_transparency',
            defaultValue=DEFAULT_SETTINGS['image_list_resolution_badge_transparency'],
            type=int)
        show_badge = settings.value(
            'image_list_show_resolution_badge',
            defaultValue=DEFAULT_SETTINGS['image_list_show_resolution_badge'],
            type=bool)
        self.item_delegate.set_badge_style(badge_font_size, badge_transparency,
                                           show_badge)
        show_completion_icon = settings.value(
            'image_list_show_completion_icon',
            defaultValue=DEFAULT_SETTINGS['image_list_show_completion_icon'],
            type=bool)
        self.item_delegate.set_show_completion_icon(show_completion_icon)
        self.viewport().update()

    def refresh_image_width(self, image_width: int):
        normalized_width = max(image_width, 16)
        self.setIconSize(QSize(normalized_width, normalized_width * 3))
        self.doItemsLayout()
        self.viewport().update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Left:
            if self.switch_between_selected_images(-1):
                return
        elif event.key() == Qt.Key.Key_Right:
            if self.switch_between_selected_images(1):
                return
        elif self._should_redirect_typing_to_tag_input(event):
            editor = self.image_tags_editor
            editor.raise_()
            editor.tag_input_box.setFocus()
            editor.tag_input_box.insert(event.text())
            return
        super().keyPressEvent(event)

    def _should_redirect_typing_to_tag_input(self, event: QKeyEvent) -> bool:
        """Whether a keystroke in the thumbnails should start a new tag.

        When the "type to add a tag" setting is enabled, typing a printable
        character while the thumbnails have focus should jump to the Add Tag
        input in the Image Tags pane instead of running the built-in
        type-to-search. The Filter Images box is a separate widget, so its
        typing is never affected by this.
        """
        editor = self.image_tags_editor
        # The Add Tag input is hidden in natural language mode; leave the
        # default behavior alone when there's nowhere to redirect to.
        if editor is None or editor.tag_input_box.isHidden():
            return False
        if not get_settings().value(
                'image_list_auto_focus_add_tag_box',
                defaultValue=DEFAULT_SETTINGS['image_list_auto_focus_add_tag_box'],
                type=bool):
            return False
        # Don't hijack keyboard shortcuts such as Ctrl+C or Alt+... .
        blocking_modifiers = (Qt.KeyboardModifier.ControlModifier
                              | Qt.KeyboardModifier.AltModifier
                              | Qt.KeyboardModifier.MetaModifier)
        if ((event.modifiers() & blocking_modifiers)
                != Qt.KeyboardModifier.NoModifier):
            return False
        text = event.text()
        # Only redirect single printable characters (letters, numbers, and
        # basic special characters). Keys like Enter, Tab, and the arrows have
        # an empty or control-character `text()` and are excluded.
        return len(text) == 1 and text.isprintable()

    def contextMenuEvent(self, event):
        self.context_menu.exec_(event.globalPos())

    def switch_between_selected_images(self, direction: int) -> bool:
        selected_proxy_indices = sorted(self.selectedIndexes(),
                                        key=lambda index: index.row())
        if len(selected_proxy_indices) < 2:
            return False
        selected_rows = [index.row() for index in selected_proxy_indices]
        current_row = self.selectionModel().currentIndex().row()
        if current_row not in selected_rows:
            return False
        current_selected_row = selected_rows.index(current_row)
        next_selected_row = current_selected_row + direction
        if next_selected_row < 0 or next_selected_row >= len(selected_rows):
            return False
        next_proxy_index = selected_proxy_indices[next_selected_row]
        self.selectionModel().setCurrentIndex(
            next_proxy_index, QItemSelectionModel.SelectionFlag.NoUpdate)
        self.scrollTo(next_proxy_index,
                      QAbstractItemView.ScrollHint.EnsureVisible)
        return True

    @Slot()
    def invert_selection(self):
        selected_proxy_rows = {index.row() for index in self.selectedIndexes()}
        all_proxy_rows = set(range(self.proxy_image_list_model.rowCount()))
        unselected_proxy_rows = all_proxy_rows - selected_proxy_rows
        first_unselected_proxy_row = min(unselected_proxy_rows, default=0)
        item_selection = QItemSelection()
        for row in unselected_proxy_rows:
            item_selection.append(
                QItemSelectionRange(self.proxy_image_list_model.index(row, 0)))
        self.setCurrentIndex(self.model().index(first_unselected_proxy_row, 0))
        self.selectionModel().select(
            item_selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    def get_selected_images(self) -> list[Image]:
        selected_image_proxy_indices = self.selectedIndexes()
        selected_images = [index.data(Qt.ItemDataRole.UserRole)
                           for index in selected_image_proxy_indices]
        return selected_images

    @Slot()
    def copy_selected_image_tags(self):
        # "Copy Tags" (Ctrl+C) is a window-wide shortcut, so it fires no matter
        # which pane is focused. If the focused widget is a tag list (the Image
        # Tags or All Tags pane) that knows how to copy just its selected tags,
        # let it handle the copy instead of copying every tag of the image.
        focus_widget = QApplication.focusWidget()
        while focus_widget is not None:
            copy_selected_tags = getattr(
                focus_widget, 'copy_selected_tags_to_clipboard', None)
            if callable(copy_selected_tags) and copy_selected_tags():
                return
            focus_widget = focus_widget.parentWidget()
        selected_images = self.get_selected_images()
        selected_image_captions = [self.tag_separator.join(image.tags)
                                   for image in selected_images]
        QApplication.clipboard().setText('\n'.join(selected_image_captions))

    @Slot()
    def copy_selected_image_captions(self):
        # Copy the full caption file contents (all tags plus the natural
        # language prompt) for each selected image.
        selected_images = self.get_selected_images()
        selected_image_captions = [
            build_caption_text(image.tags, image.natural_language_prompt,
                               self.tag_separator)
            for image in selected_images]
        QApplication.clipboard().setText('\n'.join(selected_image_captions))

    @Slot()
    def copy_selected_image_prompts(self):
        # Copy just the natural language prompt of each selected image.
        selected_images = self.get_selected_images()
        selected_image_prompts = [image.natural_language_prompt
                                  for image in selected_images]
        QApplication.clipboard().setText('\n'.join(selected_image_prompts))

    def get_selected_image_indices(self) -> list[QModelIndex]:
        selected_image_proxy_indices = self.selectedIndexes()
        selected_image_indices = [
            self.proxy_image_list_model.mapToSource(proxy_index)
            for proxy_index in selected_image_proxy_indices]
        return selected_image_indices

    @Slot(bool)
    def set_selected_images_complete(self, is_complete: bool):
        selected_image_indices = self.get_selected_image_indices()
        if not selected_image_indices:
            return
        self.proxy_image_list_model.sourceModel().set_images_complete(
            selected_image_indices, is_complete)

    @Slot()
    def paste_tags(self):
        selected_image_count = len(self.selectedIndexes())
        if selected_image_count > 1:
            reply = get_confirmation_dialog_reply(
                title='Paste Tags',
                question=f'Paste tags to {selected_image_count} selected '
                         f'images?')
            if reply != QMessageBox.StandardButton.Yes:
                return
        tags = QApplication.clipboard().text().split(self.tag_separator)
        selected_image_indices = self.get_selected_image_indices()
        self.tags_paste_requested.emit(tags, selected_image_indices)

    @Slot()
    def copy_selected_image_file_names(self):
        selected_images = self.get_selected_images()
        selected_image_file_names = [image.path.name
                                     for image in selected_images]
        QApplication.clipboard().setText('\n'.join(selected_image_file_names))

    @Slot()
    def copy_selected_image_paths(self):
        selected_images = self.get_selected_images()
        selected_image_paths = [str(image.path) for image in selected_images]
        QApplication.clipboard().setText('\n'.join(selected_image_paths))

    @Slot()
    def move_selected_images(self):
        selected_images = self.get_selected_images()
        selected_image_count = len(selected_images)
        caption = (f'Select directory to move {selected_image_count} selected '
                   f'{pluralize("Image", selected_image_count)} and '
                   f'{pluralize("caption", selected_image_count)} to')
        settings = get_settings()
        move_directory_path = QFileDialog.getExistingDirectory(
            parent=self, caption=caption,
            dir=settings.value('directory_path', type=str))
        if not move_directory_path:
            return
        move_directory_path = Path(move_directory_path)
        completion_store = get_completion_store()
        completion_changed = False
        for image in selected_images:
            try:
                target_image_path = move_directory_path / image.path.name
                image.path.replace(target_image_path)
                if completion_store.move_completion(image.path,
                                                    target_image_path):
                    completion_changed = True
                caption_file_path = image.path.with_suffix('.txt')
                if caption_file_path.exists():
                    caption_file_path.replace(
                        move_directory_path / caption_file_path.name)
            except OSError:
                QMessageBox.critical(self, 'Error',
                                     f'Failed to move {image.path} to '
                                     f'{move_directory_path}.')
        if completion_changed:
            completion_store.save()
        self.directory_reload_requested.emit()

    @Slot()
    def copy_selected_images(self):
        selected_images = self.get_selected_images()
        selected_image_count = len(selected_images)
        caption = (f'Select directory to copy {selected_image_count} selected '
                   f'{pluralize("Image", selected_image_count)} and '
                   f'{pluralize("caption", selected_image_count)} to')
        settings = get_settings()
        copy_directory_path = QFileDialog.getExistingDirectory(
            parent=self, caption=caption,
            dir=settings.value('directory_path', type=str))
        if not copy_directory_path:
            return
        copy_directory_path = Path(copy_directory_path)
        completion_store = get_completion_store()
        completion_changed = False
        for image in selected_images:
            try:
                shutil.copy(image.path, copy_directory_path)
                target_image_path = copy_directory_path / image.path.name
                if completion_store.copy_completion(image.path,
                                                    target_image_path):
                    completion_changed = True
                caption_file_path = image.path.with_suffix('.txt')
                if caption_file_path.exists():
                    shutil.copy(caption_file_path, copy_directory_path)
            except OSError:
                QMessageBox.critical(self, 'Error',
                                     f'Failed to copy {image.path} to '
                                     f'{copy_directory_path}.')
        if completion_changed:
            completion_store.save()

    @Slot()
    def delete_selected_images(self):
        selected_images = self.get_selected_images()
        selected_image_count = len(selected_images)
        title = f'Delete {pluralize("Image", selected_image_count)}'
        question = (f'Delete {selected_image_count} selected '
                    f'{pluralize("image", selected_image_count)} and '
                    f'{"its" if selected_image_count == 1 else "their"} '
                    f'{pluralize("caption", selected_image_count)}?')
        reply = get_confirmation_dialog_reply(title, question)
        if reply != QMessageBox.StandardButton.Yes:
            return
        for image in selected_images:
            image_file = QFile(image.path)
            if not image_file.moveToTrash():
                QMessageBox.critical(self, 'Error',
                                     f'Failed to delete {image.path}.')
            caption_file_path = image.path.with_suffix('.txt')
            caption_file = QFile(caption_file_path)
            if caption_file.exists():
                if not caption_file.moveToTrash():
                    QMessageBox.critical(self, 'Error',
                                         f'Failed to delete '
                                         f'{caption_file_path}.')
        self.directory_reload_requested.emit()

    @Slot()
    def open_image(self):
        selected_images = self.get_selected_images()
        image_path = selected_images[0].path
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(image_path)))

    @Slot()
    def open_image_in_editor(self):
        selected_images = self.get_selected_images()
        image_path = selected_images[0].path
        settings = get_settings()
        editor_executable_path = settings.value(
            'image_editor_executable_path',
            defaultValue=DEFAULT_SETTINGS['image_editor_executable_path'],
            type=str).strip()
        if not editor_executable_path:
            QMessageBox.information(
                self, 'Image Editor Not Set',
                'Set "Image editor executable path" in Settings to use this '
                'action.')
            return
        try:
            subprocess.Popen([editor_executable_path, str(image_path)])
        except OSError:
            QMessageBox.critical(
                self, 'Error',
                f'Failed to open image editor:\n{editor_executable_path}')

    @Slot()
    def rename_image_file(self):
        selected_images = self.get_selected_images()
        if len(selected_images) != 1:
            return
        image_path = selected_images[0].path
        new_stem, is_ok = QInputDialog.getText(
            self, 'Rename Image File',
            'Enter new file name (without extension):',
            text=image_path.stem)
        if not is_ok:
            return
        new_stem = new_stem.strip()
        if not new_stem:
            QMessageBox.critical(self, 'Error',
                                 'The file name cannot be empty.')
            return
        new_image_path = image_path.with_name(f'{new_stem}{image_path.suffix}')
        if new_image_path == image_path:
            return
        if new_image_path.exists():
            QMessageBox.critical(
                self, 'Error',
                f'File already exists: {new_image_path.name}.')
            return
        old_caption_file_path = image_path.with_suffix('.txt')
        new_caption_file_path = new_image_path.with_suffix('.txt')
        if (old_caption_file_path.exists()
                and old_caption_file_path != new_caption_file_path
                and new_caption_file_path.exists()):
            QMessageBox.critical(
                self, 'Error',
                f'Caption file already exists: {new_caption_file_path.name}.')
            return
        image_renamed = False
        try:
            image_path.replace(new_image_path)
            image_renamed = True
            if (old_caption_file_path.exists()
                    and old_caption_file_path != new_caption_file_path):
                old_caption_file_path.replace(new_caption_file_path)
        except OSError:
            if image_renamed:
                try:
                    new_image_path.replace(image_path)
                except OSError:
                    pass
            QMessageBox.critical(
                self, 'Error',
                f'Failed to rename {image_path.name}.')
            return
        self.directory_reload_requested.emit()

    @Slot()
    def open_caption_file(self):
        selected_images = self.get_selected_images()
        caption_file_path = selected_images[0].path.with_suffix('.txt')
        if not caption_file_path.exists():
            QMessageBox.critical(
                self, 'Error',
                f'Caption file does not exist for {selected_images[0].path}.')
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(caption_file_path)))

    @Slot()
    def update_context_menu_actions(self):
        selected_image_count = len(self.selectedIndexes())
        copy_file_names_action_name = (
            f'Copy File {pluralize("Name", selected_image_count)}')
        copy_paths_action_name = (f'Copy '
                                  f'{pluralize("Path", selected_image_count)}')
        move_images_action_name = (
            f'Move {pluralize("Image", selected_image_count)} to...')
        copy_images_action_name = (
            f'Copy {pluralize("Image", selected_image_count)} to...')
        delete_images_action_name = (
            f'Delete {pluralize("Image", selected_image_count)}')
        self.copy_file_names_action.setText(copy_file_names_action_name)
        self.copy_paths_action.setText(copy_paths_action_name)
        self.move_images_action.setText(move_images_action_name)
        self.copy_images_action.setText(copy_images_action_name)
        self.delete_images_action.setText(delete_images_action_name)
        has_caption_file = False
        if selected_image_count == 1:
            caption_file_path = self.get_selected_images()[0].path.with_suffix(
                '.txt')
            has_caption_file = caption_file_path.exists()
        self.open_image_action.setVisible(selected_image_count == 1)
        self.open_image_editor_action.setVisible(selected_image_count == 1)
        self.rename_image_action.setVisible(selected_image_count == 1)
        self.open_caption_file_action.setVisible(has_caption_file)
        mark_complete_action_name = (
            f'Mark {pluralize("Image", selected_image_count)} as Complete')
        mark_incomplete_action_name = (
            f'Mark {pluralize("Image", selected_image_count)} as Incomplete')
        self.mark_complete_action.setText(mark_complete_action_name)
        self.mark_incomplete_action.setText(mark_incomplete_action_name)
        self.mark_complete_action.setVisible(selected_image_count > 0)
        self.mark_incomplete_action.setVisible(selected_image_count > 0)

    def get_shortcut_actions(self) -> dict[str, tuple[str, QAction]]:
        return {
            'invert_selection': ('Invert Selection', self.invert_selection_action),
            'copy_tags': ('Copy Tags', self.copy_tags_action),
            'copy_caption': ('Copy Caption', self.copy_caption_action),
            'copy_prompt': ('Copy Natural Language Prompt',
                            self.copy_prompt_action),
            'paste_tags': ('Paste Tags', self.paste_tags_action),
            'copy_file_name': ('Copy File Name', self.copy_file_names_action),
            'copy_path': ('Copy Path', self.copy_paths_action),
            'move_images': ('Move Images to...', self.move_images_action),
            'copy_images': ('Copy Images to...', self.copy_images_action),
            'delete_images': ('Delete Images', self.delete_images_action),
            'open_image': ('Open Image in Default App', self.open_image_action),
            'open_image_editor': ('Open Image in Configured Editor',
                                  self.open_image_editor_action),
            'rename_image_file': ('Rename Image File...', self.rename_image_action),
            'open_caption_file': ('Open Caption File in Default App',
                                  self.open_caption_file_action),
            'mark_complete': ('Mark as Complete', self.mark_complete_action),
            'mark_incomplete': ('Mark as Incomplete',
                                self.mark_incomplete_action),
            'select_all_images': ('Select All Images',
                                  self.select_all_images_action)
        }


class ImageList(QDockWidget):
    def __init__(self, proxy_image_list_model: ProxyImageListModel,
                 tag_separator: str, image_width: int,
                 get_category_for_tag=None):
        super().__init__()
        self.proxy_image_list_model = proxy_image_list_model
        # Each `QDockWidget` needs a unique object name for saving its state.
        self.setObjectName('image_list')
        self.setWindowTitle('Images')
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea
                             | Qt.DockWidgetArea.RightDockWidgetArea)

        self.filter_line_edit = FilterLineEdit()
        selection_mode_layout = QHBoxLayout()
        selection_mode_label = QLabel('Selection mode')
        self.selection_mode_combo_box = SettingsComboBox(
            key='image_list_selection_mode')
        self.selection_mode_combo_box.addItems(list(SelectionMode))
        self.selection_mode_combo_box.setSizeAdjustPolicy(
            SettingsComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.selection_mode_combo_box.setMinimumContentsLength(4)
        selection_mode_layout.addWidget(selection_mode_label)
        selection_mode_layout.addWidget(self.selection_mode_combo_box,
                                        stretch=1)
        sort_layout = QHBoxLayout()
        sort_label = QLabel('Sort by')
        self.sort_by_combo_box = SettingsComboBox(key='image_list_sort_by')
        for sort_by in ImageListSortBy:
            self.sort_by_combo_box.addItem(
                IMAGE_LIST_SORT_DISPLAY_LABELS.get(sort_by, sort_by.value),
                sort_by.value)
        saved_sort_by = get_settings().value(
            'image_list_sort_by',
            defaultValue=DEFAULT_SETTINGS['image_list_sort_by'], type=str)
        saved_sort_by_index = self.sort_by_combo_box.findData(saved_sort_by)
        if saved_sort_by_index == -1:
            saved_sort_by_index = self.sort_by_combo_box.findData(
                ImageListSortBy.PATH.value)
        self.sort_by_combo_box.setCurrentIndex(max(saved_sort_by_index, 0))
        self.sort_by_combo_box.setSizeAdjustPolicy(
            SettingsComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.sort_by_combo_box.setMinimumContentsLength(6)
        self.sort_order_button = QPushButton()
        self.sort_order_button.setFixedWidth(
            self.sort_order_button.sizeHint().width() + 6)
        saved_sort_order = get_settings().value(
            'image_list_sort_order',
            defaultValue=DEFAULT_SETTINGS['image_list_sort_order'], type=str)
        self.is_sort_descending = (saved_sort_order == SortOrder.DESCENDING)
        self.update_sort_order_button()
        sort_layout.addWidget(sort_label)
        sort_layout.addWidget(self.sort_by_combo_box, stretch=1)
        sort_layout.addWidget(self.sort_order_button)
        self.list_view = ImageListView(self, proxy_image_list_model,
                                       tag_separator, image_width,
                                       get_category_for_tag=get_category_for_tag)
        self.image_index_label = QLabel()
        # A container widget is required to use a layout with a `QDockWidget`.
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(self.filter_line_edit)
        layout.addLayout(selection_mode_layout)
        layout.addLayout(sort_layout)
        layout.addWidget(self.list_view)
        layout.addWidget(self.image_index_label)
        self.setWidget(container)

        self.selection_mode_combo_box.currentTextChanged.connect(
            self.set_selection_mode)
        self.sort_by_combo_box.currentIndexChanged.connect(
            lambda _: self.handle_sort_by_changed())
        self.sort_order_button.clicked.connect(self.toggle_sort_order)
        self.set_selection_mode(self.selection_mode_combo_box.currentText())
        self.apply_current_sort()

    def refresh_font_size(self, font_size: int):
        font = QFont(self.list_view.font())
        font.setPointSize(max(font_size, 1))
        self.list_view.setFont(font)
        self.list_view.doItemsLayout()
        self.list_view.viewport().update()
        self.list_view.updateGeometry()

    def set_tag_separator(self, tag_separator: str):
        """Apply a new tag separator to the list view and its delegate without
        a restart, then repaint so the tag rendering updates immediately."""
        self.list_view.tag_separator = tag_separator
        self.list_view.item_delegate.tag_separator = tag_separator
        self.list_view.viewport().update()

    def apply_current_sort(self):
        current_image_path = None
        current_proxy_index = self.list_view.currentIndex()
        if current_proxy_index.isValid():
            current_image: Image = current_proxy_index.data(Qt.ItemDataRole.UserRole)
            if current_image is not None:
                current_image_path = current_image.path
        sort_by = self.sort_by_combo_box.currentData()
        sort_order = (SortOrder.DESCENDING
                      if self.is_sort_descending
                      else SortOrder.ASCENDING).value
        source_model = self.proxy_image_list_model.sourceModel()
        source_model.sort_images(sort_by, sort_order)
        if current_image_path is None:
            return
        for proxy_row in range(self.proxy_image_list_model.rowCount()):
            proxy_index = self.proxy_image_list_model.index(proxy_row, 0)
            image: Image = proxy_index.data(Qt.ItemDataRole.UserRole)
            if image is None or image.path != current_image_path:
                continue
            self.list_view.setCurrentIndex(proxy_index)
            return

    def handle_sort_by_changed(self):
        sort_by = self.sort_by_combo_box.currentData()
        get_settings().setValue('image_list_sort_by', sort_by)
        self.apply_current_sort()

    def toggle_sort_order(self):
        self.is_sort_descending = not self.is_sort_descending
        sort_order = (SortOrder.DESCENDING
                      if self.is_sort_descending
                      else SortOrder.ASCENDING).value
        get_settings().setValue('image_list_sort_order', sort_order)
        self.update_sort_order_button()
        self.apply_current_sort()

    def update_sort_order_button(self):
        if self.is_sort_descending:
            self.sort_order_button.setText('↓')
            return
        self.sort_order_button.setText('↑')

    def set_selection_mode(self, selection_mode: str):
        if selection_mode == SelectionMode.DEFAULT:
            self.list_view.setSelectionMode(
                QAbstractItemView.SelectionMode.ExtendedSelection)
        elif selection_mode == SelectionMode.TOGGLE:
            self.list_view.setSelectionMode(
                QAbstractItemView.SelectionMode.MultiSelection)

    @Slot()
    def update_image_index_label(self, proxy_image_index: QModelIndex):
        image_count = self.proxy_image_list_model.rowCount()
        unfiltered_image_count = (self.proxy_image_list_model.sourceModel()
                                  .rowCount())
        label_text = f'Image {proxy_image_index.row() + 1} / {image_count}'
        if image_count != unfiltered_image_count:
            label_text += f' ({unfiltered_image_count} total)'
        self.image_index_label.setText(label_text)

    @Slot()
    def go_to_previous_image(self):
        if self.list_view.selectionModel().currentIndex().row() == 0:
            return
        self.list_view.clearSelection()
        previous_image_index = self.proxy_image_list_model.index(
            self.list_view.selectionModel().currentIndex().row() - 1, 0)
        self.list_view.setCurrentIndex(previous_image_index)

    @Slot()
    def go_to_next_image(self):
        if (self.list_view.selectionModel().currentIndex().row()
                == self.proxy_image_list_model.rowCount() - 1):
            return
        self.list_view.clearSelection()
        next_image_index = self.proxy_image_list_model.index(
            self.list_view.selectionModel().currentIndex().row() + 1, 0)
        self.list_view.setCurrentIndex(next_image_index)

    @Slot()
    def jump_to_first_untagged_image(self):
        """
        Select the first image that has no tags, or the last image if all
        images are tagged.
        """
        proxy_image_index = None
        for proxy_image_index in range(self.proxy_image_list_model.rowCount()):
            image: Image = self.proxy_image_list_model.data(
                self.proxy_image_list_model.index(proxy_image_index, 0),
                Qt.ItemDataRole.UserRole)
            if not image.tags:
                break
        if proxy_image_index is None:
            return
        self.list_view.clearSelection()
        self.list_view.setCurrentIndex(
            self.proxy_image_list_model.index(proxy_image_index, 0))

    @Slot()
    def jump_to_first_incomplete_image(self):
        """
        Select the first image that is not marked as complete, or the last
        image if all images are complete.
        """
        proxy_image_index = None
        for proxy_image_index in range(self.proxy_image_list_model.rowCount()):
            image: Image = self.proxy_image_list_model.data(
                self.proxy_image_list_model.index(proxy_image_index, 0),
                Qt.ItemDataRole.UserRole)
            if not image.is_complete:
                break
        if proxy_image_index is None:
            return
        self.list_view.clearSelection()
        self.list_view.setCurrentIndex(
            self.proxy_image_list_model.index(proxy_image_index, 0))

    def get_selected_image_indices(self) -> list[QModelIndex]:
        return self.list_view.get_selected_image_indices()
