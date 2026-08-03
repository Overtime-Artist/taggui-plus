"""
Confirmation dialog for the in-app "Remove app data" (uninstall cleanup) action.

The dialog lists exactly what will be deleted with per-item checkboxes, performs
the deletion when confirmed, and then asks the caller to quit immediately so
nothing is regenerated during shutdown (see the ``data_removed`` flag).

Caches and settings are selected by default. The downloaded models options are
OFF by default and clearly warned, because the Hugging Face cache is shared with
other tools.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFrame,
                               QLabel, QMessageBox, QPushButton, QVBoxLayout)

from utils import app_data


class RemoveAppDataDialog(QDialog):
    """Ask the user which leftover app data to delete, then delete it.

    After a successful deletion ``self.data_removed`` is set to True and the
    dialog is accepted; the caller is expected to quit the application
    immediately.
    """

    def __init__(self, parent=None, dialog_font: QFont | None = None):
        super().__init__(parent)
        if dialog_font is not None:
            self.setFont(dialog_font)
        self.setWindowTitle('Remove app data')
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.WindowTitleHint |
            Qt.WindowType.WindowCloseButtonHint |
            Qt.WindowType.WindowSystemMenuHint)

        # Set to True only after data has actually been deleted.
        self.data_removed = False

        # Gather the targets and their current sizes up front.
        cache_dir = app_data.get_cache_directory()
        cache_size = app_data.get_directory_size_bytes(cache_dir)
        models_dir = app_data.get_models_directory()
        models_size = (app_data.get_directory_size_bytes(models_dir)
                       if models_dir is not None else 0)
        hf_dir = app_data.get_hugging_face_cache_directory()
        hf_size = app_data.get_directory_size_bytes(hf_dir)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title_label = QLabel('Remove TagGUI Plus data from this computer')
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title_label.setFont(title_font)
        layout.addWidget(title_label)

        intro = QLabel(
            'Deleting the TagGUI folder does not remove the data TagGUI stores '
            'elsewhere on your computer. Choose what to remove below. TagGUI '
            'will delete the selected items and then close immediately so '
            'nothing is written back.\n\n'
            'Your caption .txt files sit next to your images and are never '
            'touched.')
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # --- Caches (thumbnails + all scan/completion/duplicate caches) ---
        self.caches_check_box = QCheckBox(
            f'Caches (thumbnails and scan data) \u2013 '
            f'{app_data.format_size(cache_size)}')
        self.caches_check_box.setChecked(True)
        layout.addWidget(self.caches_check_box)
        layout.addWidget(self._hint(str(cache_dir)))

        # --- Settings ---
        self.settings_check_box = QCheckBox(
            'App settings (all your preferences, tag library, categories, '
            'hidden models, etc.)')
        self.settings_check_box.setChecked(True)
        layout.addWidget(self.settings_check_box)

        layout.addWidget(self._separator())

        models_header = QLabel('Downloaded models (optional)')
        models_header_font = QFont()
        models_header_font.setBold(True)
        models_header.setFont(models_header_font)
        layout.addWidget(models_header)

        models_warning = QLabel(
            'These can be several gigabytes but can also be re-downloaded '
            'later. Leave them unchecked if you might reinstall TagGUI or use '
            'these models elsewhere.')
        models_warning.setWordWrap(True)
        layout.addWidget(models_warning)

        # --- Custom models directory (only if the user set one) ---
        self.models_dir_check_box = None
        if models_dir is not None:
            self.models_dir_check_box = QCheckBox(
                f'Models directory set in Settings \u2013 '
                f'{app_data.format_size(models_size)}')
            self.models_dir_check_box.setChecked(False)
            layout.addWidget(self.models_dir_check_box)
            layout.addWidget(self._hint(str(models_dir)))

        # --- Shared Hugging Face cache ---
        self.hf_check_box = QCheckBox(
            f'Shared Hugging Face model cache \u2013 '
            f'{app_data.format_size(hf_size)}')
        self.hf_check_box.setChecked(False)
        self.hf_check_box.setEnabled(hf_size > 0 or hf_dir.exists())
        layout.addWidget(self.hf_check_box)
        hf_warning = QLabel(
            f'Warning: this folder ({hf_dir}) is shared with other programs '
            f'that use Hugging Face models. Only remove it if no other tool on '
            f'this computer relies on it.')
        hf_warning.setWordWrap(True)
        hf_warning.setStyleSheet('color: #cc7a00;')
        layout.addWidget(hf_warning)

        layout.addWidget(self._separator())

        # --- Buttons ---
        button_box = QDialogButtonBox()
        self.remove_button = QPushButton('Remove selected and quit')
        self.remove_button.setAutoDefault(False)
        button_box.addButton(
            self.remove_button, QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_button = button_box.addButton(
            QDialogButtonBox.StandardButton.Cancel)
        cancel_button.setAutoDefault(True)
        cancel_button.setDefault(True)
        self.remove_button.clicked.connect(self._on_remove_clicked)
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(button_box)

        self.setMinimumWidth(520)

        # Keep the models directory path so we can delete it on confirm.
        self._models_dir = models_dir
        self._hf_dir = hf_dir

    def _hint(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        hint_font = QFont()
        hint_font.setPointSize(8)
        label.setFont(hint_font)
        label.setStyleSheet('color: #999999;')
        label.setContentsMargins(22, 0, 0, 0)
        return label

    def _separator(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    def _on_remove_clicked(self):
        remove_caches = self.caches_check_box.isChecked()
        remove_settings = self.settings_check_box.isChecked()
        remove_models_dir = (self.models_dir_check_box is not None
                             and self.models_dir_check_box.isChecked())
        remove_hf = self.hf_check_box.isChecked()

        if not any((remove_caches, remove_settings, remove_models_dir,
                    remove_hf)):
            QMessageBox.information(
                self, 'Nothing selected',
                'Select at least one item to remove, or press Cancel.')
            return

        # Final confirmation summarising the selection.
        selected_lines = []
        if remove_caches:
            selected_lines.append('\u2022 Caches (thumbnails and scan data)')
        if remove_settings:
            selected_lines.append('\u2022 App settings')
        if remove_models_dir:
            selected_lines.append(
                f'\u2022 Models directory ({self._models_dir})')
        if remove_hf:
            selected_lines.append(
                f'\u2022 Shared Hugging Face cache ({self._hf_dir})')
        summary = '\n'.join(selected_lines)
        reply = QMessageBox.warning(
            self, 'Remove app data',
            'This will permanently delete:\n\n'
            f'{summary}\n\n'
            'TagGUI will then close immediately. This cannot be undone. '
            'Continue?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        failures = []
        if remove_caches and not app_data.remove_caches():
            failures.append('caches')
        if remove_models_dir and self._models_dir is not None:
            if not app_data.remove_directory(self._models_dir):
                failures.append('models directory')
        if remove_hf and not app_data.remove_directory(self._hf_dir):
            failures.append('Hugging Face cache')
        # Delete settings last: once cleared, nothing else needs to read them.
        if remove_settings and not app_data.remove_settings():
            failures.append('settings')

        self.data_removed = True

        if failures:
            QMessageBox.warning(
                self, 'Some items could not be fully removed',
                'The following could not be fully deleted (they may be in use '
                'or protected):\n\n\u2022 '
                + '\n\u2022 '.join(failures)
                + '\n\nYou can delete the remaining files manually. See the '
                '"Data locations and uninstalling" section of the README for '
                'exact paths.\n\nTagGUI will now close.')
        else:
            QMessageBox.information(
                self, 'App data removed',
                'The selected app data has been removed. TagGUI will now '
                'close.\n\n'
                'To finish uninstalling, delete the TagGUI application/'
                'repository folder yourself after it closes (it cannot delete '
                'its own folder while running).')

        self.accept()
