"""Full-size viewer for a Danbooru or Gelbooru post's original image.

Built on the main ImageViewer (zoom with the mouse wheel or + / -, drag to
pan). The window opens straight away showing the site's smaller sample image,
then switches to the original once it has downloaded. The image opens fitted
to the window but never enlarged past 100% (actual size); double-click or 1
switches between fit and 100%, and 0 fits.
"""
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QModelIndex, QPoint, Qt, Signal
from PySide6.QtGui import QCursor, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout)

from widgets.image_viewer import MIN_ZOOM_FACTOR, ZOOM_STEP, ImageViewer


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


class _ActualSizeImageViewer(ImageViewer):
    """ImageViewer that knows the original's size, so it can show 100%.

    The viewer's zoom factor is relative to "fit to window". The sample and
    the original fit to the same on-screen size, so 100% of the original is
    the same zoom factor whichever of them is showing."""
    zoom_changed = Signal()

    def __init__(self, model):
        super().__init__(model)
        self.actual_width = 0
        # While True (until the user zooms), keep the image fitted but never
        # larger than 100%, even as the window is resized.
        self.capped_fit = True
        self.scroll_area.reset_requested.disconnect(self.reset_view)
        self.scroll_area.reset_requested.connect(
            self.toggle_fit_and_actual_size)

    def _fit_width(self) -> float:
        pixmap = self.image_label.original_pixmap
        viewport = self.scroll_area.viewport().size()
        if pixmap.isNull() or viewport.width() <= 0 or viewport.height() <= 0:
            return 0.0
        return pixmap.width() * min(viewport.width() / pixmap.width(),
                                    viewport.height() / pixmap.height())

    def actual_size_zoom(self) -> float:
        fit_width = self._fit_width()
        width = self.actual_width or self.image_label.original_pixmap.width()
        return width / fit_width if fit_width > 0 and width > 0 else 1.0

    def zoom_percent(self) -> int | None:
        width = self.actual_width or self.image_label.original_pixmap.width()
        if self.image_label.original_pixmap.isNull() or width <= 0:
            return None
        return round(self._fit_width() * self.zoom_factor / width * 100)

    def _max_zoom_factor(self) -> float:
        return max(super()._max_zoom_factor(), self.actual_size_zoom())

    def _update_scaled_image(self, force_smooth: bool = False):
        if self.capped_fit:
            self.zoom_factor = min(1.0, self.actual_size_zoom())
        super()._update_scaled_image(force_smooth)
        self.zoom_changed.emit()

    def zoom_image(self, wheel_delta: int, anchor: QPoint | None = None):
        if self.image_label.original_pixmap.isNull() or wheel_delta == 0:
            return
        self.capped_fit = False
        # Stop at exactly 100% when a step would pass over it.
        actual = self.actual_size_zoom()
        target = self.zoom_factor * ZOOM_STEP ** (wheel_delta / 120)
        if (self.zoom_factor - actual) * (target - actual) < 0:
            self._set_zoom(actual, anchor)
            return
        if wheel_delta < 0 and self.zoom_factor < MIN_ZOOM_FACTOR:
            # A tiny image shown at 100% is already below the usual minimum.
            return
        super().zoom_image(wheel_delta, anchor)

    def _set_zoom(self, zoom_factor: float, anchor: QPoint | None):
        self.capped_fit = False
        self.zoom_factor = zoom_factor
        self._pending_zoom_anchor = None
        label = self.image_label
        if anchor is not None and label.width() > 0 and label.height() > 0:
            label_point = label.mapFrom(self.scroll_area.viewport(), anchor)
            ratio_x = min(1.0, max(0.0, label_point.x() / label.width()))
            ratio_y = min(1.0, max(0.0, label_point.y() / label.height()))
            self._pending_zoom_anchor = (ratio_x, ratio_y, QPoint(anchor))
        self._update_scaled_image()

    def reset_view(self):
        """Fit to the window (enlarging a small image)."""
        self.capped_fit = False
        super().reset_view()

    def show_actual_size(self, anchor: QPoint | None = None):
        if not self.image_label.original_pixmap.isNull():
            self._set_zoom(self.actual_size_zoom(), anchor)

    def toggle_fit_and_actual_size(self):
        if self.image_label.original_pixmap.isNull():
            return
        actual = self.actual_size_zoom()
        if abs(self.zoom_factor - actual) < 1e-6 * actual:
            self.reset_view()
        else:
            self.show_actual_size(self.scroll_area.viewport().mapFromGlobal(
                QCursor.pos()))

    def begin_post(self, actual_width: int):
        self.actual_width = actual_width
        self.capped_fit = True


class BooruImageViewerDialog(QDialog):
    download_requested = Signal(dict)

    def __init__(self, parent, site_name: str = 'Danbooru'):
        super().__init__(parent)
        self.site_name = site_name
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
        self.zoom_label = QLabel()
        bar.addWidget(self.zoom_label)
        bar.addSpacing(8)
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
        self.viewer = _ActualSizeImageViewer(self.model)
        self.viewer.zoom_changed.connect(self._update_zoom_label)
        layout.addWidget(self.viewer, 1)

        for keys, slot in (
                (('+', '=', 'Ctrl++', 'Ctrl+='), self.viewer.zoom_in),
                (('-', 'Ctrl+-'), self.viewer.zoom_out),
                (('0', 'Ctrl+0'), self.viewer.reset_view),
                (('1', 'Ctrl+1'), self.viewer.show_actual_size),
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

    def _update_zoom_label(self):
        percent = self.viewer.zoom_percent()
        self.zoom_label.setText('' if percent is None else f'{percent}%')

    def show_post(self, post: dict, sample_path: Path | None):
        """Show ``post``, using its sample image until the original arrives."""
        self.post = dict(post)
        self.showing_original = False
        dimensions = self._dimensions()
        self.viewer.begin_post(dimensions[0] if dimensions else 0)
        self.setWindowTitle(
            f'{self.site_name} Post #{post.get("id")} \u2014 Original')
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
