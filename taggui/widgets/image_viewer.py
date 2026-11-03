import math
import time
from pathlib import Path

from PySide6.QtCore import (QEvent, QModelIndex, QObject, QPoint, QRect,
                             QRunnable, QSize, Qt, QThreadPool, QTimer, Signal,
                             Slot)
from PySide6.QtGui import (QIcon, QImage, QImageReader, QPainter,
                           QPalette, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QLabel, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

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
# Synchronized grid preview (shown when multiple images are selected).
# Each selected image is composited into one large pixmap that the normal
# zoom/pan machinery then scales, so zooming into a cell shows real detail
# rather than an upscaled thumbnail. The per-cell resolution adapts to how many
# cells are shown so the whole composite stays within a fixed pixel budget:
# small selections (the common "a few variants" case) get near-original detail,
# while very large selections trade per-cell sharpness for bounded memory.
GRID_COMPOSITE_BUDGET_PX = 16_000_000
GRID_CELL_MAX_PX = 2048
GRID_CELL_MIN_PX = 512
GRID_GAP_PX = 12
GRID_HIGHLIGHT_WIDTH_PX = 10
# Opacity applied to grid cells that do NOT contain the tag focused in the
# tag list. The focused ("has tag") cells stay fully opaque; hovering or the
# current cell temporarily restores full opacity so the image can be judged.
GRID_DIM_OPACITY = 0.28
# While decoding a page of grid cells, hand finished cells to the GUI at most
# this often so the grid sharpens progressively instead of all at once.
GRID_PARTIAL_EMIT_SECONDS = 0.4


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


class _GridCellsLoader(QObject, QRunnable):
    """Decodes several grid-cell images off the GUI thread.

    Used to upgrade the grid preview from instant thumbnails to sharp images
    without blocking the UI. Images are decoded to QImages here; the QPixmaps
    are built on the GUI thread in the receiving slot (see _ImageFileLoader).

    Each image is decoded at full resolution and then smooth-scaled down to the
    cell size it will be drawn at. This gives the same pixels as scaling the
    full image directly, while only keeping cell-sized images in memory.
    (Decoding with QImageReader.setScaledSize is faster but visibly lower
    quality, especially for JPEGs.)
    """
    # {path_str: QImage}, target cell size, render token, final batch?
    loaded = Signal(dict, int, int, bool)

    def __init__(self, path_strings: list[str], target_px: int, token: int):
        QObject.__init__(self)
        QRunnable.__init__(self)
        self.path_strings = path_strings
        self.target_px = target_px
        self.token = token
        self.setAutoDelete(False)

    @staticmethod
    def fit_to_cell(image: QImage, target_px: int) -> QImage:
        long_edge = max(image.width(), image.height())
        if long_edge <= target_px:
            return image
        scale = target_px / long_edge
        size = QSize(max(1, round(image.width() * scale)),
                     max(1, round(image.height() * scale)))
        return image.scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)

    @Slot()
    def run(self):
        results: dict[str, QImage] = {}
        last_emit = time.monotonic()
        for path_string in self.path_strings:
            reader = QImageReader(path_string)
            reader.setAutoTransform(True)
            image = reader.read()
            if not image.isNull():
                results[path_string] = self.fit_to_cell(image, self.target_px)
            if (results and time.monotonic() - last_emit
                    >= GRID_PARTIAL_EMIT_SECONDS):
                self.loaded.emit(results, self.target_px, self.token, False)
                results = {}
                last_emit = time.monotonic()
        self.loaded.emit(results, self.target_px, self.token, True)


class ImageLabel(QLabel):
    image_loaded = Signal()

    def __init__(self):
        super().__init__()
        self.image_path = None
        self.original_pixmap = QPixmap()
        self._pending_path: str | None = None
        self._current_loader: _ImageFileLoader | None = None
        # True while the label shows a text message (e.g. an unreadable image)
        # instead of a picture.
        self.message_shown = False
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
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
        self.message_shown = False
        self.setPixmap(scaled_pixmap)
        self.resize(scaled_pixmap.size())

    def clear_image(self):
        """Show nothing, cancelling any in-flight load."""
        self._pending_path = None
        self._current_loader = None
        self.image_path = None
        self.original_pixmap = QPixmap()
        self.message_shown = False
        self.clear()

    def show_message(self, text: str):
        """Show a text message in place of the image (keeps image_path)."""
        self.original_pixmap = QPixmap()
        self.message_shown = True
        self.setText(text)

    def set_static_pixmap(self, pixmap: QPixmap):
        """Display a ready-made pixmap (e.g. the composited grid) directly.

        Cancels any in-flight async single-image load so a late decode can't
        overwrite the pixmap we're showing.
        """
        self._pending_path = None
        self._current_loader = None
        self.image_path = None
        self.original_pixmap = pixmap


class ImageScrollArea(QScrollArea):
    clicked = Signal()
    reset_requested = Signal()
    # Wheel delta and the cursor position in viewport coordinates, so the zoom
    # can keep the point under the cursor in place.
    zoom_requested = Signal(int, QPoint)
    viewport_resized = Signal()
    # Emitted on a plain left click that did not turn into a drag-to-pan,
    # carrying the release position in viewport coordinates. Used in grid mode
    # to select the clicked cell as the current image without changing the
    # selection. A click that moves past the drag threshold pans instead and
    # does not emit this.
    cell_click_requested = Signal(QPoint)
    # Emitted on a right click, carrying the click position in viewport
    # coordinates. Used in grid mode to show a context menu for the cell under
    # the cursor. Ignored by the ImageViewer when not in grid mode.
    cell_context_menu_requested = Signal(QPoint)
    # Emitted as the cursor moves over the grid (no button held), carrying the
    # position in viewport coordinates. Used in grid mode to light up the
    # hovered cell so a dimmed (without-tag) image can be judged before adding.
    cell_hovered = Signal(QPoint)
    # Emitted when the cursor leaves the grid area, so any hover highlight can
    # be cleared.
    hover_left = Signal()

    def __init__(self, image_label: ImageLabel):
        super().__init__()
        self.image_label = image_label
        self.is_dragging = False
        self.last_drag_position = QPoint()
        # Distinguish a click (select cell) from a drag (pan): the press
        # position and whether the cursor has moved beyond the drag threshold.
        self.press_position = QPoint()
        self.drag_moved = False
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWidget(image_label)
        # Don't take keyboard focus when the preview is clicked or dragged.
        # Otherwise clicking/dragging the image would steal focus away from
        # whatever the user was working in (e.g. the Image Tags pane). Focus is
        # instead directed explicitly by MainWindow's `clicked` handler.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Track mouse motion without a button held so the grid can react to
        # hover (lighting up a dimmed cell under the cursor).
        self.viewport().setMouseTracking(True)
        self.image_label.setMouseTracking(True)
        self.viewport().installEventFilter(self)
        self.image_label.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj not in (self.viewport(), self.image_label):
            return super().eventFilter(obj, event)
        if obj == self.viewport() and event.type() == QEvent.Type.Resize:
            self.viewport_resized.emit()
            return False
        if event.type() == QEvent.Type.Wheel:
            viewport_point = self.viewport().mapFromGlobal(
                event.globalPosition().toPoint())
            self.zoom_requested.emit(event.angleDelta().y(), viewport_point)
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
            if mouse_event.button() == Qt.MouseButton.RightButton:
                viewport_point = self.viewport().mapFromGlobal(
                    mouse_event.globalPosition().toPoint())
                self.cell_context_menu_requested.emit(viewport_point)
                event.accept()
                return True
            if mouse_event.button() == Qt.MouseButton.LeftButton:
                self.clicked.emit()
                self.is_dragging = True
                self.drag_moved = False
                self.press_position = mouse_event.globalPosition().toPoint()
                self.last_drag_position = mouse_event.globalPosition().toPoint()
                self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                event.accept()
                return True
        if (event.type() == QEvent.Type.MouseMove and self.is_dragging
                and not (event.buttons() & Qt.MouseButton.LeftButton)):
            # The button release was never delivered (e.g. it happened over
            # another window or a dialog grabbed the mouse), so stop panning
            # instead of following the cursor with no button held.
            self.is_dragging = False
            self.viewport().unsetCursor()
        if event.type() == QEvent.Type.MouseMove and self.is_dragging:
            mouse_event = event
            current_drag_position = mouse_event.globalPosition().toPoint()
            if not self.drag_moved:
                moved = current_drag_position - self.press_position
                threshold = QApplication.startDragDistance()
                if abs(moved.x()) > threshold or abs(moved.y()) > threshold:
                    self.drag_moved = True
            delta = current_drag_position - self.last_drag_position
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y())
            self.last_drag_position = current_drag_position
            event.accept()
            return True
        if event.type() == QEvent.Type.MouseMove and not self.is_dragging:
            viewport_point = self.viewport().mapFromGlobal(
                event.globalPosition().toPoint())
            self.cell_hovered.emit(viewport_point)
            # Fall through so normal handling (cursor, etc.) still happens.
        if event.type() == QEvent.Type.Leave:
            self.hover_left.emit()
        if event.type() == QEvent.Type.MouseButtonRelease:
            mouse_event = event
            if (self.is_dragging
                    and mouse_event.button() == Qt.MouseButton.LeftButton):
                self.is_dragging = False
                self.viewport().unsetCursor()
                # A press-release with no real movement is a click, not a pan:
                # request selecting the cell under the cursor (grid mode only;
                # the ImageViewer ignores it otherwise). The release position is
                # mapped into viewport coordinates for hit-testing.
                if not self.drag_moved:
                    viewport_point = self.viewport().mapFromGlobal(
                        mouse_event.globalPosition().toPoint())
                    self.cell_click_requested.emit(viewport_point)
                event.accept()
                return True
        return super().eventFilter(obj, event)


class ImageViewer(QWidget):
    clicked = Signal()
    # Emitted when a grid cell is clicked (not dragged), carrying the proxy
    # QModelIndex of the clicked image so MainWindow can make it current.
    grid_cell_clicked = Signal(QModelIndex)
    # Emitted when a grid cell is right-clicked, carrying the proxy QModelIndex
    # of the image under the cursor and the global screen position where the
    # context menu should appear.
    grid_cell_context_menu_requested = Signal(QModelIndex, QPoint)
    # Emitted once, after the first image has been fully decoded and scaled.
    # Used by MainWindow to defer show() until the UI is ready to paint.
    first_image_rendered = Signal()
    # Emitted on a right click over the single-image preview (not the grid),
    # carrying the global screen position where a context menu should appear.
    image_context_menu_requested = Signal(QPoint)

    def __init__(self, proxy_image_list_model: ProxyImageListModel):
        super().__init__()
        self.proxy_image_list_model = proxy_image_list_model
        self.image_label = ImageLabel()
        self.scroll_area = ImageScrollArea(self.image_label)
        self.zoom_factor = 1.0
        self._first_render_done = False
        # Set by a cursor-anchored zoom and consumed by the next
        # _update_scaled_image: (label x ratio, label y ratio, viewport point).
        self._pending_zoom_anchor: tuple[float, float, QPoint] | None = None
        # Grid-preview state. When _grid_mode is True the label shows a single
        # composited pixmap of all selected images instead of one image.
        self._grid_mode = False
        # path -> (cell size it was decoded for, pixmap). Only holds the cells
        # of the page currently shown.
        self._grid_cell_cache: dict[str, tuple[int, QPixmap]] = {}
        self._grid_cell_layout: list[tuple[QRect, int]] = []
        self._grid_current_cell_rect: QRect | None = None
        # Paths of images to mark in the grid (those containing the tag focused
        # in the Differences list) and the inputs of the last grid render, so
        # the grid can be rebuilt when only the marks change.
        self._grid_marked_paths: set[str] = set()
        self._grid_proxy_indices: list[QModelIndex] | None = None
        self._grid_current_position = 0
        self._grid_cell_cap = 0
        # Absolute position (index into _grid_proxy_indices) of the cell the
        # cursor is currently hovering, or None. A hovered without-tag cell is
        # drawn at full opacity so it can be judged before deciding to add.
        self._grid_hover_position: int | None = None
        # Keyboard "peek": while True the current (highlighted) cell is drawn at
        # full opacity even when it lacks the focused tag, giving keyboard-only
        # users the equivalent of hovering it with the mouse.
        self._grid_peek = False
        # Progressive loading: a monotonically increasing token identifies the
        # latest grid render so that a stale background decode can't overwrite a
        # newer view, plus a reference to the running loader to keep it alive.
        self._grid_render_token = 0
        self._grid_loaders: set = set()
        # path -> the loader currently decoding it, so re-renders while a page
        # is loading don't start duplicate decodes.
        self._grid_in_flight: dict[str, _GridCellsLoader] = {}
        # Subtle, fixed overlay (bottom-left of the viewport) showing the
        # current page and total image count while a grid is displayed. Parented
        # to the viewport so it stays put regardless of zoom or scroll. Its
        # appearance (visibility, text size, background transparency) is
        # controlled by settings and applied via refresh_grid_overlay_style().
        self._grid_overlay_label = QLabel(self.scroll_area.viewport())
        self._grid_overlay_show = True
        self._grid_overlay_font_size = DEFAULT_SETTINGS[
            'variant_grid_overlay_font_size']
        self._grid_overlay_background_alpha = self._transparency_to_alpha(
            DEFAULT_SETTINGS['variant_grid_overlay_transparency'])
        self._grid_overlay_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._grid_overlay_label.hide()
        # Image-count overlay (bottom-right), shown only in focus mode, where
        # the Images pane and its "Image N / M" label are hidden.
        self._focus_overlay_label = QLabel(self.scroll_area.viewport())
        self._focus_overlay_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._focus_overlay_label.hide()
        self._focus_overlay_enabled = False
        self.refresh_grid_overlay_style()
        self.scroll_area.clicked.connect(lambda: self.clicked.emit())
        self.scroll_area.reset_requested.connect(self.reset_view)
        self.scroll_area.zoom_requested.connect(self._on_wheel_zoom)
        self.scroll_area.viewport_resized.connect(self._on_viewport_resized)
        self.scroll_area.cell_click_requested.connect(
            self._on_cell_click_requested)
        self.scroll_area.cell_context_menu_requested.connect(
            self._on_cell_context_menu_requested)
        self.scroll_area.cell_hovered.connect(self._on_cell_hovered)
        self.scroll_area.hover_left.connect(self._on_hover_left)
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

    @staticmethod
    def _transparency_to_alpha(transparency: int) -> int:
        """Map a transparency percentage (0-100) to an alpha value (0-255).

        Mirrors the resolution badge: 0% transparency -> fully opaque (255),
        100% transparency -> fully transparent (0).
        """
        clamped = max(0, min(int(transparency), 100))
        return round(255 * (100 - clamped) / 100)

    def refresh_grid_overlay_style(self):
        """Re-read the grid overlay settings and apply them to the label."""
        settings = get_settings()
        self._grid_overlay_show = settings.value(
            'variant_grid_overlay_show',
            defaultValue=DEFAULT_SETTINGS['variant_grid_overlay_show'],
            type=bool)
        self._grid_overlay_font_size = settings.value(
            'variant_grid_overlay_font_size',
            defaultValue=DEFAULT_SETTINGS['variant_grid_overlay_font_size'],
            type=int)
        transparency = settings.value(
            'variant_grid_overlay_transparency',
            defaultValue=DEFAULT_SETTINGS['variant_grid_overlay_transparency'],
            type=int)
        self._grid_overlay_background_alpha = self._transparency_to_alpha(
            transparency)
        font_size = max(1, self._grid_overlay_font_size)
        style = (f'background-color: rgba(0, 0, 0, '
                 f'{self._grid_overlay_background_alpha}); '
                 f'color: rgba(255, 255, 255, 220); '
                 f'padding: 3px 8px; border-radius: 6px; '
                 f'font-size: {font_size}pt;')
        self._grid_overlay_label.setStyleSheet(style)
        self._focus_overlay_label.setStyleSheet(style)
        # Show/hide immediately to reflect a toggled "show overlay" setting.
        self._update_grid_overlay()
        self._update_focus_overlay()

    def _on_viewport_resized(self):
        self._resize_debounce_timer.start()
        self._position_grid_overlay()
        self._position_focus_overlay()

    def set_focus_overlay_text(self, text: str):
        self._focus_overlay_label.setText(text)
        self._update_focus_overlay()

    def set_focus_overlay_enabled(self, enabled: bool):
        self._focus_overlay_enabled = enabled
        self._update_focus_overlay()

    def _update_focus_overlay(self):
        label = self._focus_overlay_label
        if not self._focus_overlay_enabled or not label.text():
            label.hide()
            return
        label.adjustSize()
        label.show()
        label.raise_()
        self._position_focus_overlay()

    def _position_focus_overlay(self):
        """Keep the focus mode overlay pinned to the bottom-right."""
        label = self._focus_overlay_label
        if not label.isVisible():
            return
        margin = 10
        viewport = self.scroll_area.viewport()
        label.move(viewport.width() - label.width() - margin,
                   viewport.height() - label.height() - margin)

    def _update_grid_overlay(self):
        """Refresh and show the subtle page / image-count overlay."""
        if (not self._grid_overlay_show or not self._grid_mode
                or self._grid_proxy_indices is None):
            self._grid_overlay_label.hide()
            return
        total = len(self._grid_proxy_indices)
        cell_cap = self._grid_cell_cap if self._grid_cell_cap > 0 else total
        page_count = max(1, math.ceil(total / cell_cap))
        current_page = self._grid_current_position // cell_cap + 1
        noun = 'image' if total == 1 else 'images'
        if page_count > 1:
            text = f'Page {current_page}/{page_count} \u00b7 {total} {noun}'
        else:
            text = f'{total} {noun}'
        self._grid_overlay_label.setText(text)
        self._grid_overlay_label.adjustSize()
        self._grid_overlay_label.show()
        self._grid_overlay_label.raise_()
        self._position_grid_overlay()

    def _position_grid_overlay(self):
        """Keep the overlay pinned to the bottom-left of the viewport."""
        if not self._grid_overlay_label.isVisible():
            return
        margin = 10
        viewport = self.scroll_area.viewport()
        label = self._grid_overlay_label
        label.move(margin,
                   viewport.height() - label.height() - margin)

    def _update_scaled_image(self, force_smooth: bool = False):
        anchor = self._pending_zoom_anchor
        self._pending_zoom_anchor = None
        if self.image_label.original_pixmap.isNull():
            if self.image_label.message_shown:
                self.image_label.resize(self.scroll_area.viewport().size())
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
        if anchor is not None:
            self._restore_view_anchor(*anchor)
        else:
            self.restore_view_center(old_size, self.image_label.size())
        if use_fast:
            self._smooth_render_timer.start()

    def _restore_view_anchor(self, ratio_x: float, ratio_y: float,
                             viewport_point: QPoint):
        """Scroll so the image point at (ratio_x, ratio_y) of the label stays
        under ``viewport_point`` after a zoom."""
        viewport_size = self.scroll_area.viewport().size()
        new_size = self.image_label.size()
        horizontal_bar = self.scroll_area.horizontalScrollBar()
        vertical_bar = self.scroll_area.verticalScrollBar()
        if new_size.width() > viewport_size.width():
            horizontal_bar.setValue(
                round(ratio_x * new_size.width() - viewport_point.x()))
        else:
            horizontal_bar.setValue(0)
        if new_size.height() > viewport_size.height():
            vertical_bar.setValue(
                round(ratio_y * new_size.height() - viewport_point.y()))
        else:
            vertical_bar.setValue(0)

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

    @Slot(int, QPoint)
    def zoom_image(self, wheel_delta: int, anchor: QPoint | None = None):
        """Zoom by one step per 120 units of wheel delta (one mouse notch).

        Smaller deltas (touchpads, high-resolution wheels) zoom
        proportionally less. With ``anchor`` (a viewport point, e.g. the
        cursor) the image point under it stays in place; otherwise the view
        center is kept.
        """
        if self.image_label.original_pixmap.isNull() or wheel_delta == 0:
            return
        zoom_step = ZOOM_STEP ** (wheel_delta / 120)
        new_zoom_factor = max(
            MIN_ZOOM_FACTOR,
            min(self._max_zoom_factor(), self.zoom_factor * zoom_step))
        if new_zoom_factor == self.zoom_factor:
            return
        self.zoom_factor = new_zoom_factor
        self._pending_zoom_anchor = None
        if anchor is not None:
            label = self.image_label
            if label.width() > 0 and label.height() > 0:
                label_point = label.mapFrom(self.scroll_area.viewport(),
                                            anchor)
                ratio_x = min(1.0, max(0.0, label_point.x() / label.width()))
                ratio_y = min(1.0, max(0.0, label_point.y() / label.height()))
                self._pending_zoom_anchor = (ratio_x, ratio_y, QPoint(anchor))
        self._update_scaled_image()

    @Slot(int, QPoint)
    def _on_wheel_zoom(self, wheel_delta: int, viewport_point: QPoint):
        self.zoom_image(wheel_delta, viewport_point)

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
        # One mouse-wheel notch, centred on the view.
        self.zoom_image(120)

    @Slot()
    def zoom_out(self):
        self.zoom_image(-120)

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
        if self.image_label.original_pixmap.isNull():
            # The file could not be decoded (corrupt, unsupported, or deleted).
            # Say so rather than leaving the previous image on screen.
            name = (self.image_label.image_path.name
                    if self.image_label.image_path is not None else '')
            self.image_label.show_message(
                f"Can't display this image\n{name}")
            self.scroll_area.horizontalScrollBar().setValue(0)
            self.scroll_area.verticalScrollBar().setValue(0)
            self.image_label.resize(self.scroll_area.viewport().size())
        else:
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
        self._load_image(proxy_image_index, preserve_view=False)

    def reload_image(self, proxy_image_index: QModelIndex):
        """Re-read the image (e.g. after it changed on disk), keeping the
        current zoom and scroll position."""
        self._load_image(proxy_image_index, preserve_view=True)

    def _load_image(self, proxy_image_index: QModelIndex,
                    preserve_view: bool):
        if self._grid_mode:
            # A grid is being shown for a multi-image selection; ignore
            # single-image loads until the grid is torn down via exit_grid().
            return
        self._resize_debounce_timer.stop()
        self._smooth_render_timer.stop()
        self._pending_zoom_anchor = None
        if not proxy_image_index.isValid():
            # No current image (e.g. the filter matches nothing): show nothing
            # rather than leaving the previous image on screen.
            self.zoom_factor = 1.0
            self.image_label.clear_image()
            self.scroll_area.horizontalScrollBar().setValue(0)
            self.scroll_area.verticalScrollBar().setValue(0)
            self.image_label.resize(self.scroll_area.viewport().size())
            return
        image: Image = self.proxy_image_list_model.data(
            proxy_image_index, Qt.ItemDataRole.UserRole)
        if preserve_view:
            # Keep showing the current picture until the new decode arrives;
            # _update_scaled_image then keeps the zoom and view center.
            self.image_label.load_image(image.path)
            return
        # Reset zoom/scroll state immediately (image load happens async).
        self.zoom_factor = 1.0
        self.scroll_area.horizontalScrollBar().setValue(0)
        self.scroll_area.verticalScrollBar().setValue(0)
        # Start async decode; _on_image_loaded fires when done. Show the list
        # thumbnail upscaled as an instant placeholder for large images.
        placeholder = self._get_thumbnail_placeholder(proxy_image_index, image)
        self.image_label.load_image(image.path, placeholder)
        if placeholder is not None:
            self._update_scaled_image()

    # ------------------------------------------------------------------
    # Synchronized grid preview
    # ------------------------------------------------------------------
    def show_grid(self, proxy_indices: list[QModelIndex],
                  current_position: int, cell_cap: int):
        """Show all selected images as one zoomable grid.

        `proxy_indices` are the selected rows in display order, `current_position`
        is the index within that list of the current (highlighted) image, and
        `cell_cap` limits how many cells are composited at once (a window
        centered on the current image is used when the selection is larger).
        Called on first entering grid mode; resets zoom to fit the whole grid.
        """
        self._grid_mode = True
        self._render_grid(proxy_indices, current_position, cell_cap,
                          preserve_view=False)
        if not self._first_render_done:
            self._first_render_done = True
            self.first_image_rendered.emit()

    def update_grid_current(self, proxy_indices: list[QModelIndex],
                            current_position: int, cell_cap: int):
        """Re-render the grid after the current image changed (arrow keys).

        Keeps the user's current zoom level and, when zoomed in, scrolls so the
        newly-current cell is centered in the viewport.
        """
        if not self._grid_mode:
            self.show_grid(proxy_indices, current_position, cell_cap)
            return
        self._render_grid(proxy_indices, current_position, cell_cap,
                          preserve_view=True, recenter_current=True)

    def exit_grid(self):
        """Leave grid mode. The caller should then load the single image."""
        if not self._grid_mode:
            return
        self._grid_mode = False
        self._grid_cell_cache.clear()
        self._grid_in_flight.clear()
        self._grid_cell_layout = []
        self._grid_current_cell_rect = None
        self._grid_marked_paths = set()
        self._grid_proxy_indices = None
        self._grid_hover_position = None
        self._grid_peek = False
        # Invalidate any in-flight background decode so its result is ignored on
        # arrival. The loader keeps its own reference until it finishes (it
        # removes itself in _on_grid_cells_loaded), so we must not drop it here.
        self._grid_render_token += 1
        self._grid_overlay_label.hide()

    def set_marked_paths(self, paths: list[str]):
        """Mark the grid cells whose image path is in ``paths``.

        Used to show which selected images contain the tag focused in the
        Differences list. Re-renders the grid in place (keeping zoom/scroll).
        """
        new_marks = set(paths)
        if new_marks == self._grid_marked_paths:
            return
        self._grid_marked_paths = new_marks
        if self._grid_mode and self._grid_proxy_indices is not None:
            self._render_grid(self._grid_proxy_indices,
                              self._grid_current_position,
                              self._grid_cell_cap, preserve_view=True)

    def is_grid_mode(self) -> bool:
        return self._grid_mode

    def _grid_dimming_active(self) -> bool:
        """True when a difference tag is focused so some cells are dimmed.

        Dimming applies only when the marked ("has tag") images are a
        non-empty strict subset of the currently displayed window; a common
        tag (all marked) or no focus (none marked) leaves every cell full.
        """
        if not self._grid_marked_paths or self._grid_proxy_indices is None:
            return False
        window_indices, _ = self._grid_window(
            self._grid_proxy_indices, self._grid_current_position,
            self._grid_cell_cap)
        if not window_indices:
            return False
        marked = 0
        for proxy_index in window_indices:
            image: Image = self.proxy_image_list_model.data(
                proxy_index, Qt.ItemDataRole.UserRole)
            if image is not None and str(image.path) in self._grid_marked_paths:
                marked += 1
        return 0 < marked < len(window_indices)

    def _rerender_grid_in_place(self):
        self._render_grid(self._grid_proxy_indices,
                          self._grid_current_position,
                          self._grid_cell_cap, preserve_view=True)

    @Slot(QPoint)
    def _on_cell_hovered(self, viewport_point: QPoint):
        if not self._grid_mode or self._grid_proxy_indices is None:
            return
        # Hover only matters while some cells are dimmed; otherwise there's
        # nothing to light up and a re-render would be wasted work.
        if not self._grid_dimming_active():
            if self._grid_hover_position is not None:
                self._grid_hover_position = None
            return
        position = self._cell_position_at(viewport_point)
        if position == self._grid_hover_position:
            return
        self._grid_hover_position = position
        self._rerender_grid_in_place()

    @Slot()
    def _on_hover_left(self):
        if self._grid_hover_position is None:
            return
        self._grid_hover_position = None
        if (self._grid_mode and self._grid_proxy_indices is not None
                and self._grid_dimming_active()):
            self._rerender_grid_in_place()

    def set_grid_peek(self, enabled: bool):
        """Reveal the current (highlighted) cell at full opacity while held.

        The keyboard equivalent of hovering the current cell: lets a
        keyboard-only user see a dimmed (without-tag) current image clearly
        before deciding whether to add the focused tag to it.
        """
        enabled = bool(enabled)
        if enabled == self._grid_peek:
            return
        self._grid_peek = enabled
        if (self._grid_mode and self._grid_proxy_indices is not None
                and self._grid_dimming_active()):
            self._rerender_grid_in_place()

    def refresh_grid_paths(self, changed_paths: set[str]):
        """Re-decode grid cells whose image file changed on disk.

        Called after the refresh-on-focus scan reports content changes. Any
        changed path that is part of the current grid has its stale cached
        pixmap dropped and the grid is re-rendered, which re-decodes the file
        so the grid shows the up-to-date image instead of the old thumbnail.
        """
        if (not self._grid_mode or self._grid_proxy_indices is None
                or not changed_paths):
            return
        grid_paths = set()
        for index in self._grid_proxy_indices:
            image: Image = self.proxy_image_list_model.data(
                index, Qt.ItemDataRole.UserRole)
            if image is not None:
                grid_paths.add(str(image.path))
        relevant = changed_paths & grid_paths
        if not relevant:
            return
        for path in relevant:
            self._grid_cell_cache.pop(path, None)
            # Drop any decode of the old file contents still in progress.
            self._grid_in_flight.pop(path, None)
        self._render_grid(self._grid_proxy_indices,
                          self._grid_current_position,
                          self._grid_cell_cap, preserve_view=True)

    def _render_grid(self, proxy_indices: list[QModelIndex],
                     current_position: int, cell_cap: int,
                     preserve_view: bool, recenter_current: bool = False):
        self._grid_proxy_indices = list(proxy_indices)
        self._grid_current_position = current_position
        self._grid_cell_cap = cell_cap
        self._grid_render_token += 1
        token = self._grid_render_token
        window_indices, highlight_position = self._grid_window(
            proxy_indices, current_position, cell_cap)
        # Only keep decoded cells for the page being shown, so paging through
        # a large selection doesn't accumulate every page in memory.
        window_paths = self._paths_of(window_indices)
        for path in list(self._grid_cell_cache):
            if path not in window_paths:
                del self._grid_cell_cache[path]
        # Build and show the composite immediately, using sharp cached images
        # where available and the already-decoded list thumbnails elsewhere as
        # instant placeholders.
        pixmap = self._build_grid_pixmap(window_indices, highlight_position)
        self._resize_debounce_timer.stop()
        self._smooth_render_timer.stop()
        if not preserve_view:
            self.zoom_factor = 1.0
            self.scroll_area.horizontalScrollBar().setValue(0)
            self.scroll_area.verticalScrollBar().setValue(0)
        self.image_label.set_static_pixmap(pixmap)
        self._update_scaled_image()
        # Only scroll to the current cell when the current image actually moved
        # (arrow-key navigation). In-place re-renders (mark changes, content
        # refreshes) keep the user's exact scroll position instead of snapping
        # back to the highlighted cell.
        if preserve_view and recenter_current and self.zoom_factor > 1.0:
            self._center_on_current_cell()
        # Decode any cells not yet cached at full quality in the background, then
        # silently swap them in when ready (mirrors the single-image placeholder
        # -> sharp-image transition).
        self._schedule_grid_high_quality(window_indices, token)
        self._update_grid_overlay()

    def _paths_of(self, proxy_indices: list[QModelIndex]) -> set[str]:
        paths = set()
        for proxy_index in proxy_indices:
            image: Image = self.proxy_image_list_model.data(
                proxy_index, Qt.ItemDataRole.UserRole)
            if image is not None:
                paths.add(str(image.path))
        return paths

    def _schedule_grid_high_quality(self, window_indices: list[QModelIndex],
                                    token: int):
        target_px = self._grid_cell_size(self._grid_geometry_count())
        missing: list[str] = []
        seen: set[str] = set()
        for proxy_index in window_indices:
            image: Image = self.proxy_image_list_model.data(
                proxy_index, Qt.ItemDataRole.UserRole)
            path_string = str(image.path)
            if path_string in seen:
                continue
            seen.add(path_string)
            cached = self._grid_cell_cache.get(path_string)
            # Re-decode when the cell size changed (e.g. the selection grew),
            # so the cell is neither blurry nor larger than needed.
            if (cached is not None and not cached[1].isNull()
                    and cached[0] == target_px):
                continue
            in_flight = self._grid_in_flight.get(path_string)
            if in_flight is not None and in_flight.target_px == target_px:
                continue  # already being decoded at this size
            missing.append(path_string)
        if not missing:
            return
        loader = _GridCellsLoader(missing, target_px, token)
        loader.loaded.connect(self._on_grid_cells_loaded)
        self._grid_loaders.add(loader)  # keep alive while it runs
        for path_string in missing:
            self._grid_in_flight[path_string] = loader
        QThreadPool.globalInstance().start(loader, IMAGE_LOAD_PRIORITY)

    @Slot(dict, int, int, bool)
    def _on_grid_cells_loaded(self, images: dict, target_px: int, token: int,
                              final: bool):
        loader = self.sender()
        # Only accept results this loader is still responsible for. A path is
        # handed to a newer loader when its file changed on disk or the cell
        # size changed, and everything is dropped when the grid is closed.
        accepted = {}
        for path_string, image in images.items():
            if self._grid_in_flight.get(path_string) is loader:
                del self._grid_in_flight[path_string]
                accepted[path_string] = image
        if final:
            self._grid_loaders.discard(loader)
            for path_string in [path for path, owner
                                in self._grid_in_flight.items()
                                if owner is loader]:
                del self._grid_in_flight[path_string]
        if not self._grid_mode or self._grid_proxy_indices is None:
            return
        window_indices, _ = self._grid_window(
            self._grid_proxy_indices, self._grid_current_position,
            self._grid_cell_cap)
        window_paths = self._paths_of(window_indices)
        # Build the QPixmaps on the GUI thread and cache the ones still shown.
        changed = False
        for path_string, image in accepted.items():
            if not image.isNull() and path_string in window_paths:
                self._grid_cell_cache[path_string] = (
                    target_px, QPixmap.fromImage(image))
                changed = True
        if not changed:
            return
        # Re-render in place with the now-cached sharp images, keeping the view.
        self._render_grid(self._grid_proxy_indices,
                          self._grid_current_position,
                          self._grid_cell_cap, preserve_view=True)

    @staticmethod
    def _grid_window(proxy_indices: list[QModelIndex], current_position: int,
                     cell_cap: int) -> tuple[list[QModelIndex], int]:
        count = len(proxy_indices)
        if count <= cell_cap:
            return proxy_indices, current_position
        # Fixed pages: the selection is split into consecutive pages of
        # ``cell_cap`` images. The current image determines which page is shown,
        # and the highlight moves through every cell of that page before the
        # view flips to the next (or previous) page. This avoids the window
        # scrolling before all images in the current page have been cycled.
        page_start = (current_position // cell_cap) * cell_cap
        page_end = min(page_start + cell_cap, count)
        return proxy_indices[page_start:page_end], current_position - page_start

    def _grid_geometry_count(self) -> int:
        """Cell count that drives grid geometry (cell size and columns).

        When the selection spans multiple pages, geometry is based on the page
        capacity (``cell_cap``) so every page - including a partial last page -
        uses the same cell size and column layout; a short last page just has
        empty trailing slots (fewer rows). When everything fits on one page,
        geometry is based on the actual number of images so a small selection
        still gets large, space-filling cells.
        """
        total = len(self._grid_proxy_indices) if self._grid_proxy_indices else 0
        cell_cap = self._grid_cell_cap
        if cell_cap > 0 and total > cell_cap:
            return cell_cap
        return max(1, total)

    @staticmethod
    def _grid_cell_size(cell_count: int) -> int:
        """Per-cell composite size (long edge) for a given number of cells.

        Chosen so the whole grid stays near GRID_COMPOSITE_BUDGET_PX pixels:
        fewer cells -> larger, sharper cells; more cells -> smaller cells.
        """
        target = math.sqrt(GRID_COMPOSITE_BUDGET_PX / max(1, cell_count))
        return int(max(GRID_CELL_MIN_PX, min(GRID_CELL_MAX_PX, target)))

    def _get_cell_thumbnail(self, proxy_index: QModelIndex) -> QPixmap:
        """The already-decoded list thumbnail for a cell, used as a placeholder."""
        icon = self.proxy_image_list_model.data(
            proxy_index, Qt.ItemDataRole.DecorationRole)
        if not isinstance(icon, QIcon):
            return QPixmap()
        available_sizes = icon.availableSizes()
        if not available_sizes:
            return QPixmap()
        return icon.pixmap(available_sizes[0])

    @staticmethod
    def _cell_footprint(image: Image, source_pixmap: QPixmap,
                        cell: int) -> QSize:
        """Draw size for a cell image, based on the image's native size.

        The footprint is the same whether a thumbnail placeholder or the sharp
        image is being drawn, so the background upgrade sharpens the cell in
        place without any change in size or position. Images are never upscaled
        beyond their native resolution (that would bake blur into the composite
        and hurt zoom quality); the long edge is capped at the cell size.
        """
        dimensions = getattr(image, 'dimensions', None)
        if dimensions:
            width, height = dimensions
        elif not source_pixmap.isNull():
            width, height = source_pixmap.width(), source_pixmap.height()
        else:
            return QSize(cell, cell)
        long_edge = max(width, height)
        if long_edge <= 0:
            return QSize(cell, cell)
        scale = min(cell / long_edge, 1.0)
        return QSize(max(1, round(width * scale)), max(1, round(height * scale)))

    def _build_grid_pixmap(self, window_indices: list[QModelIndex],
                           highlight_position: int) -> QPixmap:
        window_start = self._grid_current_position - highlight_position
        cell_infos: list[tuple[QModelIndex, Image, int, bool]] = []
        marked_count = 0
        for position, proxy_index in enumerate(window_indices):
            image: Image = self.proxy_image_list_model.data(
                proxy_index, Qt.ItemDataRole.UserRole)
            is_marked = str(image.path) in self._grid_marked_paths
            if is_marked:
                marked_count += 1
            cell_infos.append(
                (proxy_index, image, window_start + position, is_marked))
        # Dim the cells that don't contain the focused tag, but only when the
        # marked images are a non-empty strict subset (a focused difference
        # tag). A common tag (all marked) or no focus leaves every cell full.
        dim_active = 0 < marked_count < len(cell_infos)
        count = max(1, len(cell_infos))
        geometry_count = self._grid_geometry_count()
        columns = max(1, math.ceil(math.sqrt(geometry_count)))
        cell = self._grid_cell_size(geometry_count)
        gap = GRID_GAP_PX
        total_width = columns * cell + (columns + 1) * gap
        rows = math.ceil(count / columns)
        total_height = rows * cell + (rows + 1) * gap
        canvas = QPixmap(total_width, total_height)
        canvas.fill(self.palette().color(QPalette.ColorRole.Window))
        painter = QPainter(canvas)
        self._grid_current_cell_rect = None
        self._grid_cell_layout = []

        for position, (proxy_index, image, absolute_position, is_marked) \
                in enumerate(cell_infos):
            row = position // columns
            column = position % columns
            x = gap + column * (cell + gap)
            y = gap + row * (cell + gap)
            cell_rect = QRect(x, y, cell, cell)
            self._grid_cell_layout.append((cell_rect, absolute_position))
            # Prefer the sharp cached image (even one decoded for a different
            # cell size while its replacement loads); fall back to the list
            # thumbnail as an instant placeholder until the decode arrives.
            cached = self._grid_cell_cache.get(str(image.path))
            source_pixmap = cached[1] if cached is not None else QPixmap()
            if source_pixmap.isNull():
                source_pixmap = self._get_cell_thumbnail(proxy_index)
            # A without-tag cell is dimmed, except the cell under the cursor
            # (hover) and, while peeking, the current cell. The current cell is
            # otherwise dimmed like any other so its brightness honestly
            # reflects whether it has the focused tag.
            is_current = absolute_position == self._grid_current_position
            should_dim = (dim_active and not is_marked
                          and absolute_position != self._grid_hover_position
                          and not (is_current and self._grid_peek))
            if not source_pixmap.isNull():
                footprint = self._cell_footprint(image, source_pixmap, cell)
                fitted = source_pixmap.scaled(
                    footprint, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                offset_x = x + (cell - fitted.width()) // 2
                offset_y = y + (cell - fitted.height()) // 2
                if should_dim:
                    painter.setOpacity(GRID_DIM_OPACITY)
                painter.drawPixmap(offset_x, offset_y, fitted)
                if should_dim:
                    painter.setOpacity(1.0)
            if absolute_position == self._grid_current_position:
                pen = QPen(self.palette().color(QPalette.ColorRole.Highlight))
                pen.setWidth(GRID_HIGHLIGHT_WIDTH_PX)
                painter.setPen(pen)
                inset = GRID_HIGHLIGHT_WIDTH_PX // 2
                painter.drawRect(x - inset, y - inset,
                                 cell + 2 * inset, cell + 2 * inset)
                self._grid_current_cell_rect = cell_rect
        painter.end()
        return canvas

    def _center_on_current_cell(self):
        rect = self._grid_current_cell_rect
        if rect is None:
            return
        original_size = self.image_label.original_pixmap.size()
        scaled_size = self.image_label.size()
        if original_size.width() <= 0 or original_size.height() <= 0:
            return
        scale_x = scaled_size.width() / original_size.width()
        scale_y = scaled_size.height() / original_size.height()
        center_x = (rect.x() + rect.width() / 2) * scale_x
        center_y = (rect.y() + rect.height() / 2) * scale_y
        viewport_size = self.scroll_area.viewport().size()
        self.scroll_area.horizontalScrollBar().setValue(
            round(center_x - viewport_size.width() / 2))
        self.scroll_area.verticalScrollBar().setValue(
            round(center_y - viewport_size.height() / 2))

    # ------------------------------------------------------------------
    # Grid cell click-to-select
    # ------------------------------------------------------------------
    def _cell_at_composite_point(self, composite_x: float,
                                 composite_y: float) -> int | None:
        """Absolute selection position of the cell containing a composite point.

        `composite_x`/`composite_y` are in the coordinate space of the
        composited grid pixmap (``original_pixmap``). Returns the position
        within ``self._grid_proxy_indices``, or None if the point is in a gap,
        outside the grid, or over an empty cell.
        """
        for rect, absolute_position in self._grid_cell_layout:
            if (rect.x() <= composite_x < rect.x() + rect.width()
                    and rect.y() <= composite_y < rect.y() + rect.height()):
                return absolute_position
        return None

    def _cell_position_at(self, viewport_point: QPoint) -> int | None:
        """Absolute selection position of the cell under a viewport point.

        Maps the viewport point into composite (``original_pixmap``)
        coordinates, accounting for the current zoom scale and scroll offset,
        then hit-tests it against the grid layout.
        """
        if not self._grid_mode or self._grid_proxy_indices is None:
            return None
        original_size = self.image_label.original_pixmap.size()
        scaled_size = self.image_label.size()
        if original_size.width() <= 0 or original_size.height() <= 0:
            return None
        if scaled_size.width() <= 0 or scaled_size.height() <= 0:
            return None
        # image_label is the scroll area's widget; mapFrom converts a viewport
        # point into label coordinates, absorbing both centering and scroll.
        label_point = self.image_label.mapFrom(
            self.scroll_area.viewport(), viewport_point)
        scale_x = scaled_size.width() / original_size.width()
        scale_y = scaled_size.height() / original_size.height()
        if scale_x <= 0 or scale_y <= 0:
            return None
        composite_x = label_point.x() / scale_x
        composite_y = label_point.y() / scale_y
        return self._cell_at_composite_point(composite_x, composite_y)

    @Slot(QPoint)
    def _on_cell_click_requested(self, viewport_point: QPoint):
        if not self._grid_mode or self._grid_proxy_indices is None:
            return
        position = self._cell_position_at(viewport_point)
        if position is None:
            return
        if position < 0 or position >= len(self._grid_proxy_indices):
            return
        proxy_index = self._grid_proxy_indices[position]
        if proxy_index.isValid():
            self.grid_cell_clicked.emit(proxy_index)

    @Slot(QPoint)
    def _on_cell_context_menu_requested(self, viewport_point: QPoint):
        if not self._grid_mode:
            if (self.image_label.image_path is not None
                    and not self.image_label.original_pixmap.isNull()):
                self.image_context_menu_requested.emit(
                    self.scroll_area.viewport().mapToGlobal(viewport_point))
            return
        if self._grid_proxy_indices is None:
            return
        position = self._cell_position_at(viewport_point)
        if position is None:
            return
        if position < 0 or position >= len(self._grid_proxy_indices):
            return
        proxy_index = self._grid_proxy_indices[position]
        if proxy_index.isValid():
            global_point = self.scroll_area.viewport().mapToGlobal(
                viewport_point)
            self.grid_cell_context_menu_requested.emit(proxy_index,
                                                       global_point)
