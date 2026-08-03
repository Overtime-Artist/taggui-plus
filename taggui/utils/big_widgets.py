from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QCheckBox, QPushButton

from utils.settings import DEFAULT_SETTINGS, get_settings


class BigPushButton(QPushButton):
    def __init__(self, text: str):
        super().__init__(text)
        update_big_push_button_size(self)


class TallPushButton(QPushButton):
    def __init__(self, text: str):
        super().__init__(text)
        update_tall_push_button_height(self)

    def keyPressEvent(self, event: QKeyEvent):
        # A focused QPushButton in a plain window (not a dialog) only reacts to
        # Space, because Enter/Return is reserved for a dialog's default
        # button. These are prominent action buttons that can receive keyboard
        # focus (e.g. via the Alt+C shortcut), so also activate them on
        # Enter/Return to match what users expect.
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and not event.isAutoRepeat() and self.isEnabled()):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)


class BigCheckBox(QCheckBox):
    def __init__(self, text: str | None = None):
        super().__init__(text)
        update_big_check_box_style(self)


def update_big_push_button_size(button: BigPushButton):
    new_size = button.sizeHint() * 1.5
    button.setFixedSize(new_size)


def update_tall_push_button_height(button: TallPushButton):
    new_height = int(button.sizeHint().height() * 1.5)
    button.setFixedHeight(new_height)


def update_big_check_box_style(check_box: BigCheckBox):
    settings = get_settings()
    font_size = settings.value(
        'font_size', defaultValue=DEFAULT_SETTINGS['font_size'], type=int)
    new_size = font_size * 1.5
    check_box.setStyleSheet(
        f'QCheckBox::indicator '
        f'{{ width: {new_size}px; height: {new_size}px; }}')
