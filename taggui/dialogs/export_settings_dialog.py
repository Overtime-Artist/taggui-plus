from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QLabel, QVBoxLayout, QCheckBox,
                               QPushButton, QHBoxLayout)


class ExportSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Export Settings')
        self.include_tag_library = False
        
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)
        
        label = QLabel('Select what to export:')
        layout.addWidget(label)
        
        self.include_tag_library_checkbox = QCheckBox(
            'Include Tag Library')
        self.include_tag_library_checkbox.setChecked(False)
        layout.addWidget(self.include_tag_library_checkbox)
        
        self.include_auto_captioner_checkbox = QCheckBox(
            'Include Auto-Captioner Settings')
        self.include_auto_captioner_checkbox.setChecked(True)
        layout.addWidget(self.include_auto_captioner_checkbox)
        
        self.include_completed_images_checkbox = QCheckBox(
            'Include Completed Image Marks')
        self.include_completed_images_checkbox.setChecked(False)
        self.include_completed_images_checkbox.setToolTip(
            'Export which images you have marked complete. These marks are '
            'specific to your own image dataset.')
        layout.addWidget(self.include_completed_images_checkbox)
        
        layout.addStretch()
        
        button_layout = QHBoxLayout()
        export_button = QPushButton('Export')
        export_button.clicked.connect(self.accept)
        cancel_button = QPushButton('Cancel')
        cancel_button.clicked.connect(self.reject)
        button_layout.addStretch()
        button_layout.addWidget(export_button)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)
        
        self.setMinimumWidth(400)
    
    def get_include_tag_library(self) -> bool:
        return self.include_tag_library_checkbox.isChecked()

    def get_include_auto_captioner(self) -> bool:
        return self.include_auto_captioner_checkbox.isChecked()

    def get_include_completed_images(self) -> bool:
        return self.include_completed_images_checkbox.isChecked()
