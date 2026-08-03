from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QColorDialog, QDialog, QDialogButtonBox,
                               QGridLayout, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout)


class CategoryDialog(QDialog):
    def __init__(self, parent, title: str, name: str = '', color: str = '#ffffff'):
        super().__init__(parent)
        self.setWindowTitle(title)

        self.name_line_edit = QLineEdit()
        self.name_line_edit.setText(name)
        self.name_line_edit.setClearButtonEnabled(True)

        self.color_line_edit = QLineEdit()
        self.color_line_edit.setText(color)
        self.color_line_edit.setClearButtonEnabled(True)

        self.pick_color_button = QPushButton('Pick Color...')
        self.pick_color_button.setAutoDefault(False)
        self.pick_color_button.clicked.connect(self.pick_color)

        form_layout = QGridLayout()
        form_layout.addWidget(QLabel('Category name'), 0, 0,
                              Qt.AlignmentFlag.AlignRight)
        form_layout.addWidget(self.name_line_edit, 0, 1)
        form_layout.addWidget(QLabel('Category color'), 1, 0,
                              Qt.AlignmentFlag.AlignRight)
        form_layout.addWidget(self.color_line_edit, 1, 1)
        form_layout.addWidget(self.pick_color_button, 1, 2)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form_layout)
        layout.addWidget(self.button_box)

    @Slot()
    def pick_color(self):
        current_color = QColor(self.color_line_edit.text())
        if not current_color.isValid():
            current_color = QColor('#ffffff')
        selected_color = QColorDialog.getColor(current_color, self,
                                               'Select category color')
        if not selected_color.isValid():
            return
        self.color_line_edit.setText(selected_color.name())

    def get_values(self) -> tuple[str, str]:
        return self.name_line_edit.text().strip(), self.color_line_edit.text().strip()
