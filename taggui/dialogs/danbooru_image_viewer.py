"""Full-size viewer for a Danbooru post's original image.

Built on the main ImageViewer (zoom with the mouse wheel or + / -, drag to
pan, double-click to fit). The window opens straight away showing Danbooru's
smaller sample image, then switches to the original once it has downloaded.
"""
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout)

from widgets.image_viewer import ImageViewer


class _ImageRecord:
    """The two attributes ImageViewer reads from an image record."""

    def __init__(self, path: Path, dimensions):
        self.path = path
        self.dimensions = dimensions


class _SingleImageModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.record = None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() or self.record is None else 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if (role == Qt.ItemDataRole.UserRole and index.isValid()
                and index.row() == 0):
            return self.record
        return None


class DanbooruImageViewerDialog(QDialog):
    download_requested = Signal(dict)

    def __init__(self, parent):
        super().__init__(parent)
        self.post = {}
        self.showing_original = False
        self._was_maximized = False
        self.model = _SingleImageModel(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 4, 8, 4)
        self.info_label = QLabel()
        self.info_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        bar.addWidget(self.info_label, 1)
        self.download_button = self._make_button('Download original\u2026')
        self.download_button.clicked.connect(
            lambda: self.download_requested.emit(dict(self.post)))
        self.full_screen_button = self._make_button('Full screen (F11)')
        self.full_screen_button.clicked.connect(self.toggle_full_screen)
        self.close_button = self._make_button('Close (Esc)')
        self.close_button.clicked.connect(self.reject)
        for button in (self.download_button, self.full_screen_button,
                       self.close_button):
            bar.addWidget(button)
        layout.addLayout(bar)
        self.viewer = ImageViewer(self.model)
        layout.addWidget(self.viewer, 1)

        for keys, slot in (
                (('+', '=', 'Ctrl++', 'Ctrl+='), self.viewer.zoom_in),
                (('-', 'Ctrl+-'), self.viewer.zoom_out),
                (('0', 'Ctrl+0'), self.viewer.reset_view),
                (('F11',), self.toggle_full_screen)):
            for key in keys:
                QShortcut(QKeySequence(key), self).activated.connect(slot)

        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry()
        self.resize(int(available.width() * 0.85),
                    int(available.height() * 0.85))

    @staticmethod
    def _make_button(text: str) -> QPushButton:
        button = QPushButton(text)
        # Keep focus on the image so the zoom keys and Esc always work.
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setAutoDefault(False)
        return button

    def _dimensions(self):
        width = int(self.post.get('image_width') or 0)
        height = int(self.post.get('image_height') or 0)
        return (width, height) if width > 0 and height > 0 else None

    def show_post(self, post: dict, sample_path: Path | None):
        """Show ``post``, using its sample image until the original arrives."""
        self.post = dict(post)
        self.showing_original = False
        self.setWindowTitle(f'Danbooru Post #{post.get("id")} \u2014 Original')
        if sample_path is not None:
            self.model.record = _ImageRecord(sample_path, self._dimensions())
            self.viewer.load_image(self.model.index(0))
        else:
            self.model.record = None
            self.viewer.load_image(QModelIndex())
        self.set_status('Downloading the original image\u2026')

    def show_original(self, post_id: str, original_path: Path):
        if str(self.post.get('id')) != str(post_id):
            return
        had_image = self.model.record is not None
        self.model.record = _ImageRecord(original_path, self._dimensions())
        self.showing_original = True
        index = self.model.index(0)
        if had_image:
            # Swap in the original without resetting the zoom or position.
            self.viewer.reload_image(index)
        else:
            self.viewer.load_image(index)
        self.set_status('')

    def show_original_failed(self, post_id: str, message: str):
        if str(self.post.get('id')) != str(post_id):
            return
        prefix = ('Showing the smaller sample image. '
                  if self.model.record is not None else '')
        self.set_status(f"{prefix}Couldn't download the original: {message}")

    def set_status(self, message: str):
        width = self.post.get('image_width') or '?'
        height = self.post.get('image_height') or '?'
        text = f'Post #{self.post.get("id")} \u2014 {width}\u00d7{height}'
        if message:
            text += f' \u2014 {message}'
        self.info_label.setText(text)

    def toggle_full_screen(self):
        if self.isFullScreen():
            if self._was_maximized:
                self.showMaximized()
            else:
                self.showNormal()
            self.full_screen_button.setText('Full screen (F11)')
            return
        self._was_maximized = self.isMaximized()
        # Without this, Windows places a full-screen dialog slightly off the
        # monitor and leaves the taskbar on top.
        self.setAttribute(Qt.WidgetAttribute.WA_Moved, True)
        self.showFullScreen()
        self.full_screen_button.setText('Exit full screen (F11)')
