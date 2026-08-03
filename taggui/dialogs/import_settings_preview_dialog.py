from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QLabel, QVBoxLayout, QCheckBox,
                               QPushButton, QHBoxLayout, QGroupBox)

from utils.settings_export_import import EXPORT_CATEGORIES, get_category_summary


class ImportSettingsPreviewDialog(QDialog):
    def __init__(self, import_data: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Import Settings - Preview')
        self.import_data = import_data
        self.category_checkboxes = {}
        
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)
        
        header_label = QLabel('Select what to import:')
        layout.addWidget(header_label)
        
        data = import_data.get('data', {})
        for category_key, category_info in EXPORT_CATEGORIES.items():
            if category_key not in data:
                continue
            
            group_box = QGroupBox(category_info['label'])
            group_layout = QVBoxLayout()
            
            summary = get_category_summary(import_data, category_key)
            summary_label = QLabel(summary)
            summary_label.setStyleSheet('color: #666; font-size: 11px;')
            
            checkbox = QCheckBox('Import')
            checkbox.setChecked(True)
            self.category_checkboxes[category_key] = checkbox
            
            group_layout.addWidget(checkbox)
            group_layout.addWidget(summary_label)
            group_box.setLayout(group_layout)
            layout.addWidget(group_box)
        
        layout.addStretch()
        
        button_layout = QHBoxLayout()
        import_button = QPushButton('Import')
        import_button.clicked.connect(self.accept)
        cancel_button = QPushButton('Cancel')
        cancel_button.clicked.connect(self.reject)
        button_layout.addStretch()
        button_layout.addWidget(import_button)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)
        
        self.setMinimumWidth(450)
    
    def get_selected_categories(self) -> list[str]:
        return [
            category_key for category_key, checkbox in self.category_checkboxes.items()
            if checkbox.isChecked()
        ]
