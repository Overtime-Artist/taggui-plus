from collections import Counter

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QGridLayout,
                               QHBoxLayout, QKeySequenceEdit, QLabel,
                               QMessageBox, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)


def normalize_shortcut_sequences(value) -> list[str]:
    """Normalize a stored or default shortcut value into a list of
    key-sequence strings.

    Accepts a single string (possibly empty), a list/tuple of strings, or
    None. Empty entries are dropped and duplicates removed while the original
    order is preserved. This keeps the rest of the code able to treat a single
    binding and multiple bindings uniformly, and tolerates the way QSettings
    can hand a one-element list back as a plain string.
    """
    if value is None:
        return []
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        return []
    sequences = []
    for candidate in candidates:
        text = str(candidate).strip()
        if text and text not in sequences:
            sequences.append(text)
    return sequences


class ShortcutEditList(QWidget):
    """An editable list of key sequences for a single action.

    Shows one ``QKeySequenceEdit`` per binding plus an "Add binding" button, so
    an action can have more than one shortcut (for example both Alt+F and
    Ctrl+F focusing the same box). Each editor captures a single chord, so the
    entries are independent alternatives rather than one multi-step chord.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(2)
        self.rows_container = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_container)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(2)
        outer_layout.addWidget(self.rows_container)
        self.add_button = QPushButton('+ Add binding')
        self.add_button.setAutoDefault(False)
        self.add_button.clicked.connect(lambda: self._add_edit())
        outer_layout.addWidget(self.add_button, 0,
                               Qt.AlignmentFlag.AlignLeft)
        # Each entry is a (row_widget, key_sequence_edit) tuple.
        self.edits: list[tuple[QWidget, QKeySequenceEdit]] = []

    def _add_edit(self, key_sequence: QKeySequence | None = None
                  ) -> QKeySequenceEdit:
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)
        key_sequence_edit = QKeySequenceEdit()
        # Restrict each editor to a single chord so entries stay independent
        # alternatives instead of a multi-step chord like "Ctrl+F, Alt+F".
        if hasattr(key_sequence_edit, 'setMaximumSequenceLength'):
            key_sequence_edit.setMaximumSequenceLength(1)
        if key_sequence is not None:
            key_sequence_edit.setKeySequence(key_sequence)
        remove_button = QPushButton('\u2715')
        remove_button.setAutoDefault(False)
        remove_button.setFixedWidth(28)
        remove_button.clicked.connect(
            lambda: self._remove_edit(row_widget, key_sequence_edit))
        row_layout.addWidget(key_sequence_edit, 1)
        row_layout.addWidget(remove_button, 0)
        self.rows_layout.addWidget(row_widget)
        self.edits.append((row_widget, key_sequence_edit))
        return key_sequence_edit

    def _remove_edit(self, row_widget: QWidget,
                     key_sequence_edit: QKeySequenceEdit):
        self.edits = [(widget, edit) for widget, edit in self.edits
                      if edit is not key_sequence_edit]
        self.rows_layout.removeWidget(row_widget)
        row_widget.deleteLater()
        # Always keep at least one (possibly empty) editor visible.
        if not self.edits:
            self._add_edit()

    def set_key_sequences(self, sequences: list[str]):
        for row_widget, _ in self.edits:
            self.rows_layout.removeWidget(row_widget)
            row_widget.deleteLater()
        self.edits = []
        if not sequences:
            self._add_edit()
            return
        for sequence in sequences:
            self._add_edit(QKeySequence(sequence))

    def key_sequences(self) -> list[str]:
        sequences = []
        for _, key_sequence_edit in self.edits:
            text = key_sequence_edit.keySequence().toString(
                QKeySequence.SequenceFormat.NativeText).strip()
            if text and text not in sequences:
                sequences.append(text)
        return sequences


class KeyboardShortcutsDialog(QDialog):
    def __init__(self, parent,
                 shortcut_specs: list[tuple[str, str, str | list[str]]],
                 configured_shortcuts: dict[str, object]):
        super().__init__(parent)
        self.shortcut_specs = shortcut_specs
        self.edit_by_shortcut_id: dict[str, ShortcutEditList] = {}
        self.setWindowTitle('Keyboard Shortcuts')
        self.setMinimumWidth(700)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        grid_container = QWidget()
        grid_layout = QGridLayout(grid_container)
        grid_layout.setColumnStretch(1, 1)
        grid_layout.addWidget(QLabel('Action'), 0, 0,
                              Qt.AlignmentFlag.AlignLeft)
        grid_layout.addWidget(QLabel('Shortcut'), 0, 1,
                              Qt.AlignmentFlag.AlignLeft)
        grid_layout.addWidget(QLabel('Default'), 0, 2,
                              Qt.AlignmentFlag.AlignLeft)

        for row, (shortcut_id, action_name, default_shortcut) in enumerate(
                shortcut_specs, start=1):
            action_label = QLabel(action_name)
            shortcut_edit = ShortcutEditList()
            default_sequences = normalize_shortcut_sequences(default_shortcut)
            if shortcut_id in configured_shortcuts:
                initial_sequences = normalize_shortcut_sequences(
                    configured_shortcuts[shortcut_id])
            else:
                initial_sequences = default_sequences
            shortcut_edit.set_key_sequences(initial_sequences)
            default_label = QLabel(', '.join(default_sequences) or '(None)')
            reset_button = QPushButton('Reset')
            reset_button.setAutoDefault(False)
            clear_button = QPushButton('Clear')
            clear_button.setAutoDefault(False)
            reset_button.clicked.connect(
                lambda _, edit=shortcut_edit, sequences=default_sequences:
                edit.set_key_sequences(sequences))
            clear_button.clicked.connect(
                lambda _, edit=shortcut_edit: edit.set_key_sequences([]))
            grid_layout.addWidget(action_label, row, 0,
                                  Qt.AlignmentFlag.AlignTop)
            grid_layout.addWidget(shortcut_edit, row, 1)
            grid_layout.addWidget(default_label, row, 2,
                                  Qt.AlignmentFlag.AlignTop)
            grid_layout.addWidget(reset_button, row, 3,
                                  Qt.AlignmentFlag.AlignTop)
            grid_layout.addWidget(clear_button, row, 4,
                                  Qt.AlignmentFlag.AlignTop)
            self.edit_by_shortcut_id[shortcut_id] = shortcut_edit

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(grid_container)
        layout.addWidget(scroll_area)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel)
        reset_all_button = QPushButton('Reset All to Defaults')
        reset_all_button.setAutoDefault(False)
        button_box.addButton(reset_all_button, QDialogButtonBox.ButtonRole.ResetRole)
        button_box.accepted.connect(self.save_shortcuts)
        button_box.rejected.connect(self.reject)
        reset_all_button.clicked.connect(self.reset_all_shortcuts)
        layout.addWidget(button_box)

    def reset_all_shortcuts(self):
        for shortcut_id, _, default_shortcut in self.shortcut_specs:
            shortcut_edit = self.edit_by_shortcut_id[shortcut_id]
            shortcut_edit.set_key_sequences(
                normalize_shortcut_sequences(default_shortcut))

    def get_shortcut_values(self) -> dict[str, list[str]]:
        return {shortcut_id: shortcut_edit.key_sequences()
                for shortcut_id, shortcut_edit
                in self.edit_by_shortcut_id.items()}

    def save_shortcuts(self):
        shortcut_by_id = self.get_shortcut_values()
        all_sequences = [sequence for sequences in shortcut_by_id.values()
                         for sequence in sequences]
        duplicate_shortcuts = [sequence for sequence, count
                               in Counter(all_sequences).items() if count > 1]
        if duplicate_shortcuts:
            duplicates_text = ', '.join(sorted(duplicate_shortcuts))
            QMessageBox.critical(
                self, 'Duplicate Shortcuts',
                f'Duplicate shortcut(s): {duplicates_text}')
            return
        self.shortcut_by_id = shortcut_by_id
        self.accept()
