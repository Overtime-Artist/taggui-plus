from typing import Callable

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QWidget


class ClickAwayFocusClearer(QObject):
    """
    Application event filter, installed only while a focused-scroll widget has
    focus. Clicking anywhere outside that widget (including empty space and
    labels, which never take focus themselves) clears its focus, so the scroll
    wheel stops changing its value.
    """

    def __init__(self):
        super().__init__()
        self.widget: QWidget | None = None

    def eventFilter(self, object_: QObject, event: QEvent) -> bool:
        if (event.type() != QEvent.Type.MouseButtonPress
                or self.widget is None or not isinstance(object_, QWidget)):
            return False
        widget = self.widget
        try:
            clicked_outside = (
                object_.window().windowType() != Qt.WindowType.Popup
                and object_ is not widget
                and not widget.isAncestorOf(object_))
        except RuntimeError:
            # The focused widget was deleted without a focus-out.
            self.stop()
            return False
        if clicked_outside:
            self.stop()
            widget.clearFocus()
        return False

    def start(self, widget: QWidget):
        app = QApplication.instance()
        if app is None:
            return
        if self.widget is None:
            app.installEventFilter(self)
        self.widget = widget

    def stop(self, widget: QWidget | None = None):
        if self.widget is None or (widget is not None
                                   and widget is not self.widget):
            return
        self.widget = None
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)


_click_away_focus_clearer: ClickAwayFocusClearer | None = None


def _get_click_away_focus_clearer() -> ClickAwayFocusClearer:
    global _click_away_focus_clearer
    if _click_away_focus_clearer is None:
        _click_away_focus_clearer = ClickAwayFocusClearer()
    return _click_away_focus_clearer


class UnfocusedScrollIgnorer(QObject):
    """
    Event filter that ignores scroll events on unfocused widgets.
    """

    def __init__(self, parent):
        # A parent is required to avoid garbage collection.
        super().__init__(parent)

    def eventFilter(self, object_: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Wheel and not object_.hasFocus():
            # This is required to let the scroll area scroll.
            event.ignore()
            return True
        return False


class FocusedScrollMixin:
    setFocusPolicy: Callable[[Qt.FocusPolicy], None]
    installEventFilter: Callable[[QObject], None]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.installEventFilter(UnfocusedScrollIgnorer(self))

    def focusInEvent(self, event):
        super().focusInEvent(event)
        _get_click_away_focus_clearer().start(self)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        # A focus-out caused by opening the dropdown's own popup keeps focus
        # logically on the widget, so keep watching for click-away.
        if event.reason() != Qt.FocusReason.PopupFocusReason:
            _get_click_away_focus_clearer().stop(self)
