from PySide6.QtCore import QEvent, QItemSelectionModel, Qt
from PySide6.QtWidgets import (QFrame, QMessageBox, QPlainTextEdit,
                                QStyledItemDelegate)


class TextEditItemDelegate(QStyledItemDelegate):
    def _commit_and_close_editor(self, editor):
        if getattr(editor, '_taggui_commit_closed', False):
            return
        editor._taggui_commit_closed = True
        text = editor.toPlainText().strip()
        model = self.parent().model()
        # The row-removal and duplicate-warning behaviour below only applies to
        # the Image Tags editor, whose model is a QStringListModel.  Other views
        # (e.g. the All Tags pane, backed by ProxyTagCounterModel) validate
        # edits in their own setData(), so for them just commit and close.
        string_list = (model.stringList()
                       if hasattr(model, 'stringList') else None)
        if not text:
            self.closeEditor.emit(editor)
            # Clearing a tag deletes it, but only for the editable string list.
            if string_list is not None:
                model.removeRow(editor.index.row())
            return
        # Warn if the edited value already exists at a different row.
        if string_list is not None:
            for row, existing in enumerate(string_list):
                if row != editor.index.row() and existing == text:
                    msg = QMessageBox(self.parent())
                    msg.setWindowTitle('Duplicate Tag')
                    msg.setIcon(QMessageBox.Icon.Warning)
                    msg.setText(f'"{text}" already exists in the tag list. '
                                f'The edit will be reverted.')
                    msg.exec()
                    self.closeEditor.emit(editor)
                    return
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    def paint(self, painter, option, index):
        # Add some left padding.
        option.rect.adjust(4, 0, 0, 0)
        super().paint(painter, option, index)

    def createEditor(self, parent, option, index):
        editor = QPlainTextEdit(parent)
        editor.setFrameStyle(QFrame.Shape.NoFrame)
        editor.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        editor.setViewportMargins(3, 0, 0, 0)
        editor.index = index
        editor._taggui_commit_closed = False
        return editor

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(size.height() + 8)
        return size

    def eventFilter(self, editor, event: QEvent):
        if (event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            self._commit_and_close_editor(editor)
            self.parent().setCurrentIndex(
                self.parent().model().index(editor.index.row(), 0))
            self.parent().selectionModel().select(
                self.parent().model().index(editor.index.row(), 0),
                QItemSelectionModel.SelectionFlag.ClearAndSelect)
            self.parent().setFocus()
            return True
        # This is required to prevent crashing when the user clicks on another
        # tag in the All Tags list.
        if event.type() == QEvent.FocusOut:
            self._commit_and_close_editor(editor)
            return True
        return False
