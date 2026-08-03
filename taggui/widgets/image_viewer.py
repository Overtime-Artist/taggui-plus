from pathlib import Path

from PySide6.QtCore import (QEvent, QModelIndex, QObject, QPoint, QRunnable,
                             QSize, Qt, QThreadPool, QTimer, Signal, Slot)
from PySide6.QtGui import QIcon, QImage, QImageReader, QPixmap
from PySide6.QtWidgets import (QLabel, QScrollArea, QSizePolicy, QVBoxLayout,
                               QWidget)

from models.proxy_image_list_model import ProxyImageListModel
from utils.image import Image
from utils.settings import DEFAULT_SETTINGS, get_settings

ZOOM_STEP = 1.15
MIN_ZOOM_FACTOR = 0.25
# Above this zoom level, use FastTransformation immediately and queue a
# SmoothTransformation re-render after the zoom gesture settles.
FAST_ZOOM_THRESHOLD = 10.0
SMOOTH_RENDER_DELAY_MS = 80
# The image the user is currently viewing is the most latency-sensitive load,
# so it runs ahead of other global-pool work (e.g. the directory scanner).
IMAGE_LOAD_PRIORITY = 10
# For images at or above this pixel count the full-resolution decode takes long
# enough to be noticeable, so the already-decoded list thumbnail is shown
# upscaled as an instant placeholder until the sharp decode arrives. Smaller
# images decode fast enough that a placeholder would only cause a blur flash.
PLACEHOLDER_MIN_PIXELS = 8_000_000


class _ImageFileLoader(QObject, QRunnable):
    """Loads a full-resolution image file in a background thread.

    The file is decoded to a QImage (which is safe to build off the GUI
    thread); the QPixmap is created on the GUI thread in the receiving slot.
    Building a QPixmap directly on a worker thread is not thread-safe and Qt
    serializes it against the GUI thread, which freezes the UI for the entire
    decode of a large image.
    """
    loaded = Signal(QImage, str)

    def __init__(self, image_path: Path):
        QObject.__init__(self)
        QRunnable.__init__(self)
        self.image_path = image_path
        self.setAutoDelete(False)

    @Slot()
    def run(self):
        image_reader = QImageReader(str(self.image_path))
        image_reader.setAutoTransform(True)
        image = image_reader.read()
        self.loaded.emit(image, str(self.image_path))


class ImageLabel(QLabel):
    image_loaded = Signal()

    def __init__(self):
        super().__init__()
        self.image_path = None
        self.original_pixmap = QPixmap()
        self._pending_path: str | None = None
        self._current_loader: _ImageFileLoader | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setMinimumSize(QSize(1, 1))

    def load_image(self, image_path: Path,
                   placeholder_pixmap: QPixmap | None = None):
        self.image_path = image_path
        self._pending_path = str(image_path)
        # Show the placeholder (typically the upscaled list thumbnail)
        # immediately so there's instant visual feedback while the full
        # decode runs. If none is provided, clear the pixmap so
        # _update_scaled_image is a no-op until the async load completes.
        if placeholder_pixmap is not None and not placeholder_pixmap.isNull():
            self.original_pixmap = placeholder_pixmap
        else:
            self.original_pixmap = QPixmap()
        loader = _ImageFileLoader(image_path)
        loader.loaded.connect(self._on_file_loaded)
        self._current_loader = loader
        QThreadPool.globalInstance().start(loader, IMAGE_LOAD_PRIORITY)

    @Slot(QImage, str)
    def _on_file_loaded(self, image: QImage, path_str: str):
        if path_str != self._pending_path:
            return  # stale — user navigated away before this finished
        # Build the QPixmap here, on the GUI thread. This is cheap even for
        # large images and, unlike doing it on the worker thread, does not
        # block the UI.
        self.original_pixmap = QPixmap.fromImage(image)
        self._pending_path = None
        self._current_loader = None
        self.image_loaded.emit()

    def set_scaled_pixmap(self, scaled_pixmap: QPixmap):
        self.setPixmap(scaled_pixmap)
        self.resize(scaled_pixmap.size())


class ImageScrollArea(QScrollArea):
    clicked = Signal()
    reset_requested = Signal()
    zoom_requested = Signal(int)
    viewport_resized = Signal()

    def __init__(self, image_label: ImageLabel):
        super().__init__()
        self.image_label = image_label
        self.is_dragging = False
        self.last_drag_position = QPoint()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWidget(image_label)
        # Don't take keyboard focus when the preview is clicked or dragged.
        # Otherwise clicking/dragging the image would steal focus away from
        # whatever the user was working in (e.g. the Image Tags pane). Focus is
        # instead directed explicitly by MainWindow's `clicked` handler.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().installEventFilter(self)
        self.image_label.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj not in (self.viewport(), self.image_label):
            return super().eventFilter(obj, event)
        if obj == self.viewport() and event.type() == QEvent.Type.Resize:
            self.viewport_resized.emit()
            return False
        if event.type() == QEvent.Type.Wheel:
            self.zoom_requested.emit(event.angleDelta().y())
            event.accept()
            return True
        if event.type() == QEvent.Type.MouseButtonDblClick:
            mouse_event = event
            if mouse_event.button() == Qt.MouseButton.LeftButton:
                self.clicked.emit()
                self.reset_requested.emit()
                event.accept()
                return True
        if event.type() == QEvent.Type.MouseButtonPress:
            mouse_event = event
            if mouse_event.button() == Qt.MouseButton.LeftButton:
                self.clicked.emit()
                self.is_dragging = True
                self.last_drag_position = mouse_event.globalPosition().toPoint()
                self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                event.accept()
                return True
        if event.type() == QEvent.Type.MouseMove and self.is_dragging:
            mouse_event = event
            current_drag_position = mouse_event.globalPosition().toPoint()
            delta = current_drag_position - self.last_drag_position
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y())
            self.last_drag_position = current_drag_position
            event.accept()
            return True
        if event.type() == QEvent.Type.MouseButtonRelease:
            mouse_event = event
            if (self.is_dragging
                    and mouse_event.button() == Qt.MouseButton.LeftButton):
                self.is_dragging = False
                self.viewport().unsetCursor()
                event.accept()
                return True
        return super().eventFilter(obj, event)


class ImageViewer(QWidget):
    clicked = Signal()
    # Emitted once, after the first image has been fully decoded and scaled.
    # Used by MainWindow to defer show() until the UI is ready to paint.
    first_image_rendered = Signal()

    def __init__(self, proxy_image_list_model: ProxyImageListModel):
        super().__init__()
        self.proxy_image_list_model = proxy_image_list_model
        self.image_label = ImageLabel()
        self.scroll_area = ImageScrollArea(self.image_label)
        self.zoom_factor = 1.0
        self._first_render_done = False
        self.scroll_area.clicked.connect(lambda: self.clicked.emit())
        self.scroll_area.reset_requested.connect(self.reset_view)
        self.scroll_area.zoom_requested.connect(self.zoom_image)
        self.scroll_area.viewport_resized.connect(self._on_viewport_resized)
        self.image_label.image_loaded.connect(self._on_image_loaded)

        self._resize_debounce_timer = QTimer(self)
        self._resize_debounce_timer.setSingleShot(True)
        self._resize_debounce_timer.setInterval(30)
        self._resize_debounce_timer.timeout.connect(self._update_scaled_image)

        self._smooth_render_timer = QTimer(self)
        self._smooth_render_timer.setSingleShot(True)
        self._smooth_render_timer.setInterval(SMOOTH_RENDER_DELAY_MS)
        self._smooth_render_timer.timeout.connect(
            lambda: self._update_scaled_image(force_smooth=True))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll_area)

    def _max_zoom_factor(self) -> float:
        return float(get_settings().value(
            'max_image_preview_zoom',
            defaultValue=DEFAULT_SETTINGS['max_image_preview_zoom'],
            type=int))

    def _on_viewport_resized(self):
        self._resize_debounce_timer.start()

    def _update_scaled_image(self, force_smooth: bool = False):
        if self.image_label.original_pixmap.isNull():
            return
        viewport_size = self.scroll_area.viewport().size()
        if viewport_size.width() <= 0 or viewport_size.height() <= 0:
            return
        original_size = self.image_label.original_pixmap.size()
        if original_size.width() <= 0 or original_size.height() <= 0:
            return
        fit_scale = min(viewport_size.width() / original_size.width(),
                        viewport_size.height() / original_size.height())
        effective_scale = fit_scale * self.zoom_factor
        target_size = QSize(max(1, round(original_size.width() * effective_scale)),
                            max(1, round(original_size.height() * effective_scale)))
        # Use FastTransformation at high zoom to stay responsive; a smooth
        # re-render is queued automatically to follow up.
        use_fast = self.zoom_factor > FAST_ZOOM_THRESHOLD and not force_smooth
        transformation = (Qt.TransformationMode.FastTransformation
                          if use_fast
                          else Qt.TransformationMode.SmoothTransformation)
        old_size = self.image_label.size()
        scaled_pixmap = self.image_label.original_pixmap.scaled(
            target_size, Qt.AspectRatioMode.KeepAspectRatio, transformation)
        self.image_label.set_scaled_pixmap(scaled_pixmap)
        self.restore_view_center(old_size, self.image_label.size())
        if use_fast:
            self._smooth_render_timer.start()

    def restore_view_center(self, old_size: QSize, new_size: QSize):
        viewport_size = self.scroll_area.viewport().size()
        if old_size.width() > viewport_size.width():
            horizontal_center_ratio = (
                self.scroll_area.horizontalScrollBar().value()
                + viewport_size.width() / 2) / old_size.width()
        else:
            horizontal_center_ratio = 0.5
        if old_size.height() > viewport_size.height():
            vertical_center_ratio = (
                self.scroll_area.verticalScrollBar().value()
                + viewport_size.height() / 2) / old_size.height()
        else:
            vertical_center_ratio = 0.5
        if new_size.width() > viewport_size.width():
            self.scroll_area.horizontalScrollBar().setValue(
                round(horizontal_center_ratio * new_size.width()
                      - viewport_size.width() / 2))
        else:
            self.scroll_area.horizontalScrollBar().setValue(0)
        if new_size.height() > viewport_size.height():
            self.scroll_area.verticalScrollBar().setValue(
                round(vertical_center_ratio * new_size.height()
                      - viewport_size.height() / 2))
        else:
            self.scroll_area.verticalScrollBar().setValue(0)

    @Slot(int)
    def zoom_image(self, wheel_delta: int):
        if self.image_label.original_pixmap.isNull() or wheel_delta == 0:
            return
        zoom_step = ZOOM_STEP if wheel_delta > 0 else 1 / ZOOM_STEP
        new_zoom_factor = max(
            MIN_ZOOM_FACTOR,
            min(self._max_zoom_factor(), self.zoom_factor * zoom_step))
        if new_zoom_factor == self.zoom_factor:
            return
        self.zoom_factor = new_zoom_factor
        self._update_scaled_image()

    @Slot()
    def reset_view(self):
        self.zoom_factor = 1.0
        self.scroll_area.horizontalScrollBar().setValue(0)
        self.scroll_area.verticalScrollBar().setValue(0)
        self._resize_debounce_timer.stop()
        self._smooth_render_timer.stop()
        self._update_scaled_image()

    @Slot()
    def zoom_in(self):
        # zoom_image only checks the sign of its argument, so any positive
        # value performs a single zoom-in step (matching a mouse wheel notch).
        self.zoom_image(1)

    @Slot()
    def zoom_out(self):
        self.zoom_image(-1)

    def _pan(self, horizontal_steps: int, vertical_steps: int):
        """Scroll the (zoomed) image by a fraction of the viewport.

        Has no visible effect when the image already fits, since the scroll
        bars then have no range to move through. This mirrors the click-and-
        drag panning, which also just moves the scroll bars.
        """
        viewport_size = self.scroll_area.viewport().size()
        horizontal_bar = self.scroll_area.horizontalScrollBar()
        vertical_bar = self.scroll_area.verticalScrollBar()
        horizontal_step = max(1, viewport_size.width() // 5)
        vertical_step = max(1, viewport_size.height() // 5)
        horizontal_bar.setValue(
            horizontal_bar.value() + horizontal_steps * horizontal_step)
        vertical_bar.setValue(
            vertical_bar.value() + vertical_steps * vertical_step)

    @Slot()
    def pan_left(self):
        self._pan(-1, 0)

    @Slot()
    def pan_right(self):
        self._pan(1, 0)

    @Slot()
    def pan_up(self):
        self._pan(0, -1)

    @Slot()
    def pan_down(self):
        self._pan(0, 1)

    @Slot()
    def _on_image_loaded(self):
        """Called when the async image load finishes; render the image."""
        self._update_scaled_image()
        if not self._first_render_done:
            self._first_render_done = True
            self.first_image_rendered.emit()

    def _get_thumbnail_placeholder(
            self, proxy_image_index: QModelIndex,
            image: Image) -> QPixmap | None:
        """Return the list thumbnail as a placeholder for large images.

        The thumbnail is already decoded, in memory, and correctly oriented,
        so showing it upscaled gives instant feedback while the full-
        resolution image decodes in the background. Returns None for smaller
        images (which decode fast enough not to need a placeholder) or when no
        thumbnail is available yet.
        """
        dimensions = image.dimensions
        if not dimensions:
            return None
        width, height = dimensions
        if width * height < PLACEHOLDER_MIN_PIXELS:
            return None
        icon = self.proxy_image_list_model.data(
            proxy_image_index, Qt.ItemDataRole.DecorationRole)
        if not isinstance(icon, QIcon):
            return None
        available_sizes = icon.availableSizes()
        if not available_sizes:
            return None
        placeholder = icon.pixmap(available_sizes[0])
        if placeholder.isNull():
            return None
        return placeholder

    @Slot()
    def load_image(self, proxy_image_index: QModelIndex):
        if not proxy_image_index.isValid():
            return
        image: Image = self.proxy_image_list_model.data(
            proxy_image_index, Qt.ItemDataRole.UserRole)
        # Reset zoom/scroll state immediately (image load happens async).
        self.zoom_factor = 1.0
        self.scroll_area.horizontalScrollBar().setValue(0)
        self.scroll_area.verticalScrollBar().setValue(0)
        self._resize_debounce_timer.stop()
        self._smooth_render_timer.stop()
        # Start async decode; _on_image_loaded fires when done. Show the list
        # thumbnail upscaled as an instant placeholder for large images.
        placeholder = self._get_thumbnail_placeholder(proxy_image_index, image)
        self.image_label.load_image(image.path, placeholder)
        if placeholder is not None:
            self._update_scaled_image()

