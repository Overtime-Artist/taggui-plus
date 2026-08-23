from PySide6.QtWidgets import (QButtonGroup, QRadioButton, QVBoxLayout,
                               QWidget)

from utils.settings_widgets import SettingsBigCheckBox
from utils.utils import ConfirmationDialog


class CaptionMultipleImagesDialog(ConfirmationDialog):
    def __init__(self, selected_image_count: int):
        title = 'Generate Captions'
        question = f'Caption {selected_image_count} selected images?'
        super().__init__(title=title, question=question)

        # Scope choice: caption the whole selection (default) or only the
        # current (highlighted) image. Captioning only the current image is a
        # convenience for the grid / group view where several variants are
        # selected but the user wants to (re)caption just one.
        self.caption_all_radio = QRadioButton(
            f'Caption all {selected_image_count} selected images')
        self.caption_current_only_radio = QRadioButton(
            'Caption only the current image')
        self.caption_all_radio.setChecked(True)
        self._scope_button_group = QButtonGroup(self)
        self._scope_button_group.addButton(self.caption_all_radio)
        self._scope_button_group.addButton(self.caption_current_only_radio)
        scope_container = QWidget()
        scope_layout = QVBoxLayout(scope_container)
        scope_layout.setContentsMargins(0, 0, 0, 0)
        scope_layout.setSpacing(6)
        scope_layout.addWidget(self.caption_all_radio)
        scope_layout.addWidget(self.caption_current_only_radio)

        self.show_alert_check_box = SettingsBigCheckBox(
            key='show_alert_when_captioning_finished', default=True,
            text='Show alert when finished')
        self.setCheckBox(self.show_alert_check_box)

        # `QMessageBox` places the question label at grid cell (0, 2) and the
        # check box at (1, 2). Insert the scope radios between them, pushing the
        # alert check box down one row, so the dialog reads top-to-bottom:
        # question, scope choice, alert option, buttons.
        layout = self.layout()
        layout.removeWidget(self.show_alert_check_box)
        layout.addWidget(scope_container, 1, 2)
        layout.addWidget(self.show_alert_check_box, 2, 2)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(20)

        # Captioning only the current image reduces to single-image captioning,
        # which never shows a finish alert, so disable the alert option then.
        self.caption_current_only_radio.toggled.connect(
            lambda checked: self.show_alert_check_box.setEnabled(not checked))

    def caption_current_image_only(self) -> bool:
        return self.caption_current_only_radio.isChecked()
