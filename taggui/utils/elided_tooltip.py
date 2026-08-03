from PySide6.QtCore import QEvent, QModelIndex, QRect, Qt
from PySide6.QtGui import QFontMetrics, QResizeEvent
from PySide6.QtWidgets import (QComboBox, QListView, QListWidget, QStyle,
                               QStyleOptionComboBox, QStyleOptionViewItem,
                               QToolTip)


def is_text_elided(text: str, metrics: QFontMetrics, rect: QRect,
                   wrap_text: bool = False) -> bool:
    if not text:
        return False
    if rect.width() <= 0 or rect.height() <= 0:
        return True
    if wrap_text or '\n' in text:
        text_flags = (Qt.TextFlag.TextWordWrap
                      | Qt.TextFlag.TextExpandTabs)
        bounding_rect = metrics.boundingRect(rect, text_flags, text)
        return (bounding_rect.height() > rect.height()
                or bounding_rect.width() > rect.width())
    return metrics.horizontalAdvance(text) > rect.width()


class ElidedToolTipListView(QListView):
    def viewportEvent(self, event):
        if event.type() != QEvent.Type.ToolTip:
            return super().viewportEvent(event)
        index = self.indexAt(event.pos())
        if not index.isValid():
            QToolTip.hideText()
            event.ignore()
            return True
        tooltip_text = self.get_elided_tooltip_text(index)
        if not tooltip_text:
            QToolTip.hideText()
            event.ignore()
            return True
        QToolTip.showText(event.globalPos(), tooltip_text,
                          self.viewport(), self.visualRect(index))
        return True

    def get_elided_tooltip_text(self, index: QModelIndex) -> str:
        if not self.is_index_text_elided(index):
            return ''
        tooltip_text = index.data(Qt.ItemDataRole.ToolTipRole)
        if tooltip_text:
            return str(tooltip_text)
        display_text = index.data(Qt.ItemDataRole.DisplayRole)
        return str(display_text) if display_text else ''

    def is_index_text_elided(self, index: QModelIndex) -> bool:
        option = QStyleOptionViewItem()
        self.initViewItemOption(option)
        option.rect = self.visualRect(index)
        delegate = self.itemDelegateForIndex(index)
        delegate.initStyleOption(option, index)
        text_rect = self.style().subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, option, self)
        wrap_text = bool(option.features
                         & QStyleOptionViewItem.ViewItemFeature.WrapText)
        return is_text_elided(option.text, option.fontMetrics, text_rect,
                              wrap_text=wrap_text)


class ElidedToolTipListWidget(QListWidget):
    def viewportEvent(self, event):
        if event.type() != QEvent.Type.ToolTip:
            return super().viewportEvent(event)
        index = self.indexAt(event.pos())
        if not index.isValid():
            QToolTip.hideText()
            event.ignore()
            return True
        tooltip_text = self.get_elided_tooltip_text(index)
        if not tooltip_text:
            QToolTip.hideText()
            event.ignore()
            return True
        QToolTip.showText(event.globalPos(), tooltip_text,
                          self.viewport(), self.visualRect(index))
        return True

    def get_elided_tooltip_text(self, index: QModelIndex) -> str:
        if not self.is_index_text_elided(index):
            return ''
        tooltip_text = index.data(Qt.ItemDataRole.ToolTipRole)
        if tooltip_text:
            return str(tooltip_text)
        display_text = index.data(Qt.ItemDataRole.DisplayRole)
        return str(display_text) if display_text else ''

    def is_index_text_elided(self, index: QModelIndex) -> bool:
        option = QStyleOptionViewItem()
        self.initViewItemOption(option)
        option.rect = self.visualRect(index)
        delegate = self.itemDelegateForIndex(index)
        delegate.initStyleOption(option, index)
        text_rect = self.style().subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, option, self)
        wrap_text = bool(option.features
                         & QStyleOptionViewItem.ViewItemFeature.WrapText)
        return is_text_elided(option.text, option.fontMetrics, text_rect,
                              wrap_text=wrap_text)


class ElidedToolTipComboBox(QComboBox):
    def __init__(self):
        super().__init__()
        self.currentTextChanged.connect(self.update_elided_tooltip)

    def resizeEvent(self, event: QResizeEvent):
        super().resizeEvent(event)
        self.update_elided_tooltip()

    def update_elided_tooltip(self):
        text = self.currentText()
        if not text:
            self.setToolTip('')
            return
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        text_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option,
            QStyle.SubControl.SC_ComboBoxEditField, self)
        if is_text_elided(text, self.fontMetrics(), text_rect):
            self.setToolTip(text)
            return
        self.setToolTip('')
