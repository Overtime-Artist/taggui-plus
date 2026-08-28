from pathlib import Path
import ctypes
import os
import subprocess
import sys

from PySide6.QtCore import (QEvent, QEventLoop, QItemSelectionModel,
                            QKeyCombination, QModelIndex,
                            QObject, QRunnable, QThreadPool, QTimer, QUrl,
                            Qt, Signal, Slot)
from PySide6.QtGui import (QAction, QColor, QCloseEvent, QDesktopServices, QFont, QIcon,
                           QKeySequence, QPalette, QPixmap, QShortcut)
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QGridLayout,
                               QHBoxLayout, QMainWindow, QDialog, QDockWidget, QLabel,
                               QMessageBox, QPushButton, QScrollArea, QStackedWidget,
                               QTabBar, QToolTip, QVBoxLayout, QWidget)
from transformers import AutoTokenizer

from dialogs.batch_reorder_tags_dialog import (BatchReorderTagsDialog,
                                               apply_batch_reorder)
from dialogs.danbooru_wiki_dialog import DanbooruWikiDialog
from dialogs.directory_analytics_dialog import DirectoryAnalyticsDialog
from dialogs.export_settings_dialog import ExportSettingsDialog
from dialogs.find_and_replace_dialog import FindAndReplaceDialog
from dialogs.find_duplicates_dialog import FindDuplicatesDialog
from dialogs.gelbooru_wiki_dialog import GelbooruWikiDialog
from dialogs.import_settings_preview_dialog import ImportSettingsPreviewDialog
from dialogs.keyboard_shortcuts_dialog import (KeyboardShortcutsDialog,
                                               normalize_shortcut_sequences)
from dialogs.settings_dialog import SettingsDialog
from dialogs.tag_library_dialog import TagLibraryDialog
from models.image_list_model import ImageListModel, RefreshScanner
from models.image_tag_list_model import ImageTagListModel
from models.tag_library_model import TagLibraryModel
from models.proxy_image_list_model import ProxyImageListModel
from models.tag_counter_model import TagCounterModel
from utils.big_widgets import BigPushButton
from utils.enums import (CaptionDestination, ImageListSortBy, SortOrder,
                         ThemeMode)
from utils.image import Image
from utils.key_press_forwarder import KeyPressForwarder
from utils.completion_store import get_completion_store
from utils.settings import (DEFAULT_SETTINGS, get_settings, get_tag_separator,
                            get_hidden_model_ids, migrate_settings)
from utils.settings_export_import import (import_settings, load_settings_from_file,
                                          save_settings_to_file)
from utils.shortcut_remover import ShortcutRemover
from utils.utils import get_resource_path, pluralize
from widgets.all_tags_editor import AllTagsEditor
from widgets.auto_captioner import AutoCaptioner
from widgets.image_list import ImageList
from widgets.image_tags_editor import (ImageTagsEditor,
                                       show_category_assignment_prompt)
from widgets.image_viewer import ImageViewer

ICON_PATH = Path('images/icon.ico')
GITHUB_REPOSITORY_URL = 'https://github.com/Overtime-Artist/taggui-plus'
TOKENIZER_DIRECTORY_PATH = Path('clip-vit-base-patch32')


class TokenizerLoader(QObject, QRunnable):
    tokenizer_loaded = Signal(object)

    def __init__(self, tokenizer_path: Path):
        QObject.__init__(self)
        QRunnable.__init__(self)
        self.tokenizer_path = tokenizer_path
        self.setAutoDelete(False)

    def run(self):
        tokenizer = AutoTokenizer.from_pretrained(str(self.tokenizer_path))
        self.tokenizer_loaded.emit(tokenizer)


class MainWindow(QMainWindow):
    def __init__(self, app: QApplication):
        super().__init__()
        # Keep the window fully transparent until _show_window() reveals it.
        # The window is never show()n during construction (run_gui defers it),
        # so this opacity also acts as insurance: if anything shows the window
        # early, it stays invisible until the UI is fully painted.
        self.setWindowOpacity(0.0)
        self.app = app
        migrate_settings()
        self.settings = get_settings()
        if not self.settings.contains('image_list_font_size'):
            self.settings.setValue(
                'image_list_font_size',
                self.settings.value(
                    'font_size',
                    defaultValue=DEFAULT_SETTINGS['font_size'], type=int))
        # The path of the currently loaded directory. This is set later when a
        # directory is loaded.
        self.directory_path = None
        # Tracks an in-flight background refresh (RefreshScanner) so re-focusing
        # the window repeatedly does not start overlapping scans.
        self._refresh_scanner = None
        image_list_image_width = self.settings.value(
            'image_list_image_width',
            defaultValue=DEFAULT_SETTINGS['image_list_image_width'], type=int)
        tag_separator = get_tag_separator()
        tokenizer = None
        self.image_list_model = ImageListModel(image_list_image_width,
                                               tag_separator)
        self.tag_library_model = TagLibraryModel()
        self.proxy_image_list_model = ProxyImageListModel(
            self.image_list_model, tokenizer, tag_separator,
            self.tag_library_model)
        self.image_list_model.proxy_image_list_model = (
            self.proxy_image_list_model)
        self.pending_auto_caption_category_tags = []
        # True from the moment auto-captioning starts until the single
        # end-of-run category-assignment prompt has been shown. New tags are
        # collected into pending_auto_caption_category_tags while this is True.
        # It stays True slightly longer than auto_captioner.is_captioning so
        # that a tag-change dispatch queued for the final image (which can fire
        # after is_captioning flips to False) still joins the batch instead of
        # opening its own second prompt.
        self.is_collecting_auto_caption_category_tags = False
        self.is_syncing_tag_library_from_directory = False
        self.previous_all_tags = set()
        self.removed_all_tags_pending = set()
        self.category_assignment_tags_pending = set()
        # Tags that were just added to the Tag Library for the first time via
        # the Image Tags pane. They are recorded here (before the tag counter
        # updates) so track_all_tags_changes can queue the category prompt for
        # them once they appear in All Tags.
        self.newly_added_library_tags_pending = set()
        self.tag_change_prompt_dispatch_scheduled = False
        self.skip_next_all_tags_removal_prompt = True
        self.should_skip_save_state_on_close = False
        # Set to True by the "Remove app data" flow so that closing the app does
        # not rewrite any of the data that was just deleted.
        self.is_removing_app_data = False
        self._pending_directory_select_index = 0
        # Image filter text to reapply once the next directory finishes loading
        # (used to restore the active filter on startup). Empty means "no
        # filter"; every load_directory call sets this explicitly.
        self._pending_directory_filter = ''
        self.tag_counter_model = TagCounterModel(self.tag_library_model)
        self.image_tag_list_model = ImageTagListModel(self.tag_library_model)

        self.setWindowIcon(QIcon(QPixmap(get_resource_path(ICON_PATH))))
        # The font size must be set before creating the widgets to ensure that
        # everything has the correct font size.
        self.set_font_size()
        self.apply_theme()
        self.image_viewer = ImageViewer(self.proxy_image_list_model)
        self.create_central_widget()
        self.image_list = ImageList(self.proxy_image_list_model,
                                    tag_separator, image_list_image_width,
                                    get_category_for_tag=self.tag_library_model.get_category_for_tag)
        self.refresh_image_list_font_size()
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea,
                           self.image_list)
        self.image_tags_editor = ImageTagsEditor(
            self.image_list_model, self.proxy_image_list_model,
            self.tag_library_model,
            self.image_tag_list_model, self.image_list, tokenizer,
            tag_separator)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea,
                           self.image_tags_editor)
        # Give the thumbnails list access to the Image Tags pane so typing in
        # the Images pane can jump to the Add Tag input (see
        # ImageListView.keyPressEvent).
        self.image_list.list_view.image_tags_editor = self.image_tags_editor

        # Start loading the tokenizer in the background so startup doesn't
        # block the event loop.
        tokenizer_loader = TokenizerLoader(
            get_resource_path(TOKENIZER_DIRECTORY_PATH))
        tokenizer_loader.tokenizer_loaded.connect(self._on_tokenizer_loaded)
        QThreadPool.globalInstance().start(tokenizer_loader)
        self.all_tags_editor = AllTagsEditor(self.tag_counter_model)
        self.tag_counter_model.all_tags_list = (self.all_tags_editor
                                                .all_tags_list)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea,
                           self.all_tags_editor)
        self.auto_captioner = AutoCaptioner(self.image_list_model,
                                            self.image_list,
                                            self.tag_library_model)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea,
                           self.auto_captioner)
        self.tabifyDockWidget(self.all_tags_editor, self.auto_captioner)
        self.all_tags_editor.raise_()
        self.refresh_pane_title_fonts()
        # Set default widths for the dock widgets only on first run.
        # When saved geometry exists these values are overwritten by
        # restoreGeometry/restoreState anyway, so skip the resize on
        # subsequent runs.
        if not self.settings.contains('geometry'):
            self.resize(image_list_image_width * 8,
                        int(image_list_image_width * 4.5))
            self.resizeDocks([self.image_list, self.image_tags_editor,
                              self.all_tags_editor],
                             [int(image_list_image_width * 2.5)] * 3,
                             Qt.Orientation.Horizontal)
        self.image_tags_editor.tag_input_box.setDisabled(True)
        self.image_tags_editor.natural_language_mode_check_box.setDisabled(True)
        self.image_tags_editor.natural_language_text_edit.setDisabled(True)
        self.auto_captioner.set_start_cancel_button_enabled(False)
        self.reload_directory_action = QAction('Reload Directory', parent=self)
        self.reload_directory_action.setDisabled(True)
        self.undo_action = QAction('Undo', parent=self)
        self.redo_action = QAction('Redo', parent=self)
        self.toggle_image_list_action = QAction('Images', parent=self)
        self.toggle_image_tags_editor_action = QAction('Image Tags',
                                                       parent=self)
        self.toggle_all_tags_editor_action = QAction('All Tags', parent=self)
        self.toggle_auto_captioner_action = QAction('Auto-Captioner',
                                                    parent=self)
        self.open_danbooru_wiki_action = QAction('Danbooru Wiki...' + ' ' * 12, parent=self)
        self.open_gelbooru_wiki_action = QAction('Gelbooru Wiki...' + ' ' * 12, parent=self)
        self.create_menus()
        self.app.installEventFilter(self)

        self.image_list_selection_model = (self.image_list.list_view
                                           .selectionModel())
        self.image_list_model.image_list_selection_model = (
            self.image_list_selection_model)
        self.connect_image_list_signals()
        self.connect_image_tags_editor_signals()
        self.connect_all_tags_editor_signals()
        self.connect_auto_captioner_signals()
        for dock in self.get_dock_widgets():
            dock.topLevelChanged.connect(self._reset_dock_state)
        # Forward any unhandled image changing key presses to the image list.
        key_press_forwarder = KeyPressForwarder(
            parent=self, target=self.image_list.list_view,
            keys_to_forward=(Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp,
                             Qt.Key.Key_PageDown, Qt.Key.Key_Home,
                             Qt.Key.Key_End))
        self.installEventFilter(key_press_forwarder)
        # Remove the Ctrl+Z shortcut from text input boxes to prevent it from
        # conflicting with the undo action.
        ctrl_z = QKeyCombination(Qt.KeyboardModifier.ControlModifier,
                                 key=Qt.Key.Key_Z)
        ctrl_y = QKeyCombination(Qt.KeyboardModifier.ControlModifier,
                                 key=Qt.Key.Key_Y)
        shortcut_remover = ShortcutRemover(parent=self,
                                           shortcuts=(ctrl_z, ctrl_y))
        self.image_list.filter_line_edit.installEventFilter(shortcut_remover)
        self.image_tags_editor.tag_input_box.installEventFilter(
            shortcut_remover)
        self.all_tags_editor.filter_line_edit.installEventFilter(
            shortcut_remover)
        # Set keyboard shortcuts. These are stored on ``self`` so they can be
        # exposed (and reconfigured) through the Keyboard Shortcuts dialog via
        # ``get_shortcut_action_map``. The key sequences set here are just the
        # defaults; ``apply_configured_shortcuts`` (called below, after every
        # shortcut object exists) applies any user customizations.
        self.focus_filter_images_box_shortcut = QShortcut(
            QKeySequence('Alt+F'), self)
        self.focus_filter_images_box_shortcut.activated.connect(
            self.image_list.raise_)
        self.focus_filter_images_box_shortcut.activated.connect(
            self.image_list.filter_line_edit.setFocus)
        self.focus_add_tag_box_shortcut = QShortcut(
            QKeySequence('Alt+A'), self)
        self.focus_add_tag_box_shortcut.activated.connect(
            self.image_tags_editor.raise_)
        self.focus_add_tag_box_shortcut.activated.connect(
            self.image_tags_editor.tag_input_box.setFocus)
        self.focus_image_tags_list_shortcut = QShortcut(
            QKeySequence('Alt+I'), self)
        self.focus_image_tags_list_shortcut.activated.connect(
            self.image_tags_editor.focus_tags_list)
        self.focus_search_tags_box_shortcut = QShortcut(
            QKeySequence('Alt+S'), self)
        self.focus_search_tags_box_shortcut.activated.connect(
            self.all_tags_editor.raise_)
        self.focus_search_tags_box_shortcut.activated.connect(
            self.all_tags_editor.filter_line_edit.setFocus)
        self.focus_caption_button_shortcut = QShortcut(
            QKeySequence('Alt+C'), self)
        self.focus_caption_button_shortcut.activated.connect(
            self.auto_captioner.focus_start_cancel_button)
        self.go_to_previous_image_shortcut = QShortcut(
            QKeySequence('Ctrl+Up'), self)
        self.go_to_previous_image_shortcut.activated.connect(
            self.image_list.go_to_previous_image)
        self.go_to_next_image_shortcut = QShortcut(
            QKeySequence('Ctrl+Down'), self)
        self.go_to_next_image_shortcut.activated.connect(
            self.image_list.go_to_next_image)
        self.jump_to_first_untagged_image_shortcut = QShortcut(
            QKeySequence('Ctrl+J'), self)
        self.jump_to_first_untagged_image_shortcut.activated.connect(
            self.image_list.jump_to_first_untagged_image)
        self.jump_to_first_incomplete_image_shortcut = QShortcut(
            QKeySequence('Ctrl+Shift+J'), self)
        self.jump_to_first_incomplete_image_shortcut.activated.connect(
            self.image_list.jump_to_first_incomplete_image)
        self.reset_image_zoom_shortcut = QShortcut(
            QKeySequence('Ctrl+0'), self)
        self.reset_image_zoom_shortcut.activated.connect(
            self.image_viewer.reset_view)
        self.zoom_in_shortcut = QShortcut(QKeySequence('Ctrl++'), self)
        self.zoom_in_shortcut.activated.connect(self.image_viewer.zoom_in)
        self.zoom_out_shortcut = QShortcut(QKeySequence('Ctrl+-'), self)
        self.zoom_out_shortcut.activated.connect(self.image_viewer.zoom_out)
        self.pan_image_left_shortcut = QShortcut(
            QKeySequence('Alt+Left'), self)
        self.pan_image_left_shortcut.activated.connect(
            self.image_viewer.pan_left)
        self.pan_image_right_shortcut = QShortcut(
            QKeySequence('Alt+Right'), self)
        self.pan_image_right_shortcut.activated.connect(
            self.image_viewer.pan_right)
        self.pan_image_up_shortcut = QShortcut(QKeySequence('Alt+Up'), self)
        self.pan_image_up_shortcut.activated.connect(
            self.image_viewer.pan_up)
        self.pan_image_down_shortcut = QShortcut(
            QKeySequence('Alt+Down'), self)
        self.pan_image_down_shortcut.activated.connect(
            self.image_viewer.pan_down)
        self.apply_configured_shortcuts()
        self.restore()
        # Show the window once the first image has been rendered.  Fires via
        # image_viewer.first_image_rendered after the initial pixmap.scaled()
        # completes.  _show_window() is idempotent, so it is safe for both the
        # signal and the timer below to call it.
        self.image_viewer.first_image_rendered.connect(self._show_window)
        # Safety net: guarantees the window becomes visible even if no image is
        # ever rendered (e.g. an empty directory, a load error, or a path that
        # never emits first_image_rendered).  5s is a generous upper bound that
        # a normal first render finishes well within.
        QTimer.singleShot(5000, self._show_window)
        self.refresh_pane_title_fonts()
        self.image_tags_editor.tag_input_box.setFocus()

    def closeEvent(self, event: QCloseEvent):
        """Save the window geometry and state before closing."""
        # When removing app data for an uninstall, skip every save so nothing
        # that was just deleted (settings, completion store, etc.) is recreated.
        if self.is_removing_app_data:
            super().closeEvent(event)
            return
        self.image_tags_editor.save_natural_language_prompt()
        # Persist any pending completion-store changes (e.g. caption hashes
        # refreshed after in-program caption edits).
        get_completion_store().save_if_dirty()
        # Don't overwrite geometry/state if we're closing after importing settings
        if not self.should_skip_save_state_on_close:
            self.settings.setValue('geometry', self.saveGeometry())
            self.settings.setValue('window_state', self.saveState())
            # Persist the active image filter so the same filtered view (and
            # position within it) is restored on the next launch.
            self.settings.setValue(
                'image_list_filter',
                self.image_list.filter_line_edit.text())
            self.settings.sync()
        super().closeEvent(event)

    @Slot()
    def _show_window(self):
        """Show the main window if it is not already visible."""
        if not self.isVisible():
            # Floating docks were kept off-screen (WA_DontShowOnScreen set in
            # restore()) so they wouldn't flash during restoreState(). Clear
            # that now so they can render.
            for dock in self.get_dock_widgets():
                dock.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, False)
            # Show the window while fully transparent so its first paint
            # happens invisibly, then force that paint to complete before
            # revealing it.  Without this the OS briefly shows a blank white
            # window before Qt paints the UI for the first time.
            self.setWindowOpacity(0.0)
            self.show()
            # Explicitly show any floating docks that were suppressed.
            for dock in self.get_dock_widgets():
                if dock.isFloating():
                    dock.show()
            # Flush pending layout and paint events so the window is fully
            # rendered, then reveal it in one step.
            QApplication.processEvents(
                QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            self.setWindowOpacity(1.0)

    def showEvent(self, event):
        super().showEvent(event)
        theme = self.settings.value(
            'theme', defaultValue=DEFAULT_SETTINGS['theme'], type=str)
        self.set_windows_title_bar_theme(self, theme == ThemeMode.DARK)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self.refresh_changed_image_files()
        super().changeEvent(event)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Show and isinstance(obj, QDialog):
            self.apply_dialog_title_bar_theme(obj)
        if (event.type() == QEvent.Type.Show
                and isinstance(obj, QDockWidget)
                and obj.isFloating()):
            self.apply_floating_dock_title_bar_theme(obj)
        return super().eventFilter(obj, event)

    def set_font_size(self):
        font = QFont(self.app.font())
        font_size = self.settings.value(
            'font_size', defaultValue=DEFAULT_SETTINGS['font_size'], type=int)
        font.setPointSize(font_size)
        self.app.setFont(font)

    def get_dark_palette(self) -> QPalette:
        dark_palette = QPalette()
        soft_text = QColor(220, 220, 220)
        dark_palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
        dark_palette.setColor(QPalette.ColorRole.WindowText, soft_text)
        dark_palette.setColor(QPalette.ColorRole.Base, QColor(35, 35, 35))
        dark_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
        dark_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(35, 35, 35))
        dark_palette.setColor(QPalette.ColorRole.ToolTipText, soft_text)
        dark_palette.setColor(QPalette.ColorRole.Text, soft_text)
        dark_palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
        dark_palette.setColor(QPalette.ColorRole.ButtonText, soft_text)
        dark_palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
        dark_palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
        dark_palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
        dark_palette.setColor(QPalette.ColorRole.HighlightedText,
                              soft_text)
        dark_palette.setColor(QPalette.ColorRole.PlaceholderText,
                              QColor(170, 170, 170))
        # Dim disabled controls so they read as inactive (Fusion can't derive
        # this on its own without an explicit Disabled color group).
        disabled_text = QColor(120, 120, 120)
        for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                     QPalette.ColorRole.ButtonText,
                     QPalette.ColorRole.HighlightedText):
            dark_palette.setColor(QPalette.ColorGroup.Disabled, role,
                                  disabled_text)
        return dark_palette

    def set_windows_title_bar_theme(self, widget: QWidget, use_dark_mode: bool,
                                    force_frame_refresh: bool = True):
        if sys.platform != 'win32':
            return
        window_handle = widget.windowHandle()
        if window_handle is None or not window_handle.isTopLevel():
            return
        try:
            hwnd = int(window_handle.winId())
            dark_mode_value = ctypes.c_int(1 if use_dark_mode else 0)
            for attribute in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd),
                    ctypes.c_uint(attribute),
                    ctypes.byref(dark_mode_value),
                    ctypes.sizeof(dark_mode_value))
            if force_frame_refresh:
                ctypes.windll.user32.SetWindowPos(
                    ctypes.c_void_p(hwnd),
                    ctypes.c_void_p(0),
                    0, 0, 0, 0,
                    0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
                ctypes.windll.dwmapi.DwmFlush()
        except Exception:
            return

    def apply_windows_title_bar_theme(self, use_dark_mode: bool):
        self.set_windows_title_bar_theme(self, use_dark_mode)

    def get_dock_widgets(self) -> tuple[QDockWidget, ...]:
        return (self.image_list, self.image_tags_editor, self.all_tags_editor,
                self.auto_captioner)

    @Slot()
    def _reset_dock_state(self):
        # After heavy dock manipulation Qt's rubber-band/highlight overlay can
        # get stuck. Toggling animated docking off and back on forces Qt to
        # rebuild its internal drag state machine and clears the stale state.
        self.setAnimated(False)
        self.setAnimated(True)

    def apply_floating_dock_title_bar_theme(self, dock_widget: QDockWidget):
        is_dark_mode = self.settings.value(
            'theme', defaultValue=DEFAULT_SETTINGS['theme'], type=str) == ThemeMode.DARK
        self.set_windows_title_bar_theme(
            dock_widget, is_dark_mode, force_frame_refresh=False)
        QTimer.singleShot(
            0, lambda: self.set_windows_title_bar_theme(
                dock_widget, is_dark_mode, force_frame_refresh=False))
        QTimer.singleShot(
            120, lambda: self.set_windows_title_bar_theme(
                dock_widget, is_dark_mode, force_frame_refresh=False))

    def apply_all_floating_dock_title_bars(self):
        for dock_widget in self.get_dock_widgets():
            if dock_widget.isFloating():
                self.apply_floating_dock_title_bar_theme(dock_widget)

    def apply_dialog_title_bar_theme(self, dialog: QDialog):
        is_dark_mode = self.settings.value(
            'theme', defaultValue=DEFAULT_SETTINGS['theme'], type=str) == ThemeMode.DARK
        self.set_windows_title_bar_theme(dialog, is_dark_mode)
        # Re-apply after Qt repaints the dialog chrome.
        QTimer.singleShot(
            0,
            lambda: self.set_windows_title_bar_theme(dialog, is_dark_mode))
        QTimer.singleShot(
            120,
            lambda: self.set_windows_title_bar_theme(dialog, is_dark_mode))

    @Slot()
    def apply_theme(self):
        theme = self.settings.value(
            'theme', defaultValue=DEFAULT_SETTINGS['theme'], type=str)
        is_dark_mode = theme == ThemeMode.DARK
        if theme == ThemeMode.DARK:
            self.app.setPalette(self.get_dark_palette())
        else:
            # Not setting this results in some ugly colors.
            self.app.setPalette(self.app.style().standardPalette())
        self.apply_tooltip_style()
        self.apply_windows_title_bar_theme(is_dark_mode)
        # Re-apply after pending Qt repaints to prevent non-client frame reset.
        QTimer.singleShot(0, lambda: self.apply_windows_title_bar_theme(is_dark_mode))
        QTimer.singleShot(120, lambda: self.apply_windows_title_bar_theme(is_dark_mode))
        if hasattr(self, 'image_list'):
            self.apply_all_floating_dock_title_bars()
        if hasattr(self, 'image_tags_editor'):
            self.image_tags_editor.refresh_token_count_palette()
        if hasattr(self, 'image_list'):
            self.refresh_pane_title_fonts()

    def apply_tooltip_style(self):
        tooltip_font = QFont(self.app.font())
        tooltip_font.setPointSize(max(tooltip_font.pointSize() - 2, 9))
        QToolTip.setFont(tooltip_font)

        palette = self.app.palette()
        tooltip_palette = QPalette(palette)
        tooltip_palette.setColor(QPalette.ColorRole.ToolTipBase,
                                 palette.color(QPalette.ColorRole.Base))
        tooltip_palette.setColor(QPalette.ColorRole.ToolTipText,
                                 palette.color(QPalette.ColorRole.Text))
        QToolTip.setPalette(tooltip_palette)

    def create_central_widget(self):
        central_widget = QStackedWidget()
        # Put the button inside a widget so that it will not fill up the entire
        # space.
        load_directory_widget = QWidget()
        load_directory_button = BigPushButton('Load Directory...')
        load_directory_button.clicked.connect(self.select_and_load_directory)
        QVBoxLayout(load_directory_widget).addWidget(
            load_directory_button, alignment=Qt.AlignmentFlag.AlignCenter)
        central_widget.addWidget(load_directory_widget)
        central_widget.addWidget(self.image_viewer)
        self.setCentralWidget(central_widget)

    def load_directory(self, path: Path, select_index: int = 0,
                       save_path_to_settings: bool = False,
                       restore_filter: str = ''):
        # Loading a directory can replace the entire tag universe; treat the
        # next tag-count update as a new baseline, not a user-driven deletion.
        self.skip_next_all_tags_removal_prompt = True
        self.is_syncing_tag_library_from_directory = True
        self.directory_path = path.resolve()
        if save_path_to_settings:
            self.settings.setValue('directory_path', str(self.directory_path))
        self.setWindowTitle(path.name)
        # Store select_index for _on_directory_loaded; scanning is async.
        self._pending_directory_select_index = select_index
        # Filter to reapply once loading completes (empty clears the filter, as
        # every normal directory load does).
        self._pending_directory_filter = restore_filter
        self.image_list_model.load_directory(path)

    @Slot(object)
    def _on_tokenizer_loaded(self, tokenizer):
        self.proxy_image_list_model.set_tokenizer(tokenizer)
        self.image_tags_editor.set_tokenizer(tokenizer)
        self.image_list_model.set_tokenizer(tokenizer)
        # Re-sort if images are currently sorted by token count, which was not
        # computable before the tokenizer finished loading.
        if (self.image_list.sort_by_combo_box.currentData()
                == ImageListSortBy.TOKEN_COUNT.value):
            self.image_list.apply_current_sort()

    @Slot()
    def _on_directory_loaded(self):
        # Sort and commit the pending images in a single model reset.
        sort_by = self.image_list.sort_by_combo_box.currentData()
        sort_order = (SortOrder.DESCENDING
                      if self.image_list.is_sort_descending
                      else SortOrder.ASCENDING).value
        self.image_list_model.apply_pending_images(sort_by, sort_order)
        self.tag_counter_model.count_tags(self.image_list_model.images)
        self.is_syncing_tag_library_from_directory = False
        self.all_tags_editor.filter_line_edit.clear()
        # Clear the current index first to make sure that the `currentChanged`
        # signal is emitted even if the image at the index is already selected.
        self.image_list_selection_model.clearCurrentIndex()
        pending_filter = self._pending_directory_filter
        # Consume the pending filter so later reloads (refresh, settings import,
        # opening a new directory) clear the filter as before.
        self._pending_directory_filter = ''
        if pending_filter:
            # Reapplying the filter text triggers `set_image_list_filter`, which
            # filters the list and selects its first image; we then restore the
            # saved position within the filtered results below.
            self.image_list.filter_line_edit.setText(pending_filter)
        else:
            self.image_list.filter_line_edit.clear()
        select_index = self._pending_directory_select_index
        row_count = self.proxy_image_list_model.rowCount()
        if row_count:
            select_index = max(0, min(select_index, row_count - 1))
        self.image_list.list_view.setCurrentIndex(
            self.proxy_image_list_model.index(select_index, 0))
        self.centralWidget().setCurrentWidget(self.image_viewer)
        self.reload_directory_action.setDisabled(False)
        self.image_tags_editor.tag_input_box.setDisabled(False)
        self.image_tags_editor.natural_language_mode_check_box.setDisabled(False)
        self.image_tags_editor.natural_language_text_edit.setDisabled(False)
        self.auto_captioner.set_start_cancel_button_enabled(True)
        # If no images are present there will be no currentChanged signal and
        # therefore no first_image_rendered signal, so show the window now.
        if not self.image_list_model.images:
            self._show_window()

    @Slot()
    def select_and_load_directory(self):
        initial_directory = (str(self.directory_path)
                             if self.directory_path else '')
        load_directory_path = QFileDialog.getExistingDirectory(
            parent=self, caption='Select directory to load images from',
            dir=initial_directory)
        if not load_directory_path:
            return
        self.load_directory(Path(load_directory_path),
                            save_path_to_settings=True)

    @Slot()
    def reload_directory(self):
        if self.directory_path is None or not self.directory_path.exists():
            return
        self.refresh_changed_image_files()

    @Slot()
    def refresh_changed_image_files(self):
        # Runs when the window regains focus. The actual directory scan (which
        # can touch thousands of files) is done on a background thread so the
        # UI never freezes on re-focus, even for very large directories.
        if self.directory_path is None or not self.directory_path.exists():
            return
        # A scan is already running: skip so rapid focus changes don't pile up.
        # Any changes it misses are picked up on the next re-focus.
        if self._refresh_scanner is not None:
            return
        snapshot = {
            image.path: (image.file_modified_time_ns,
                         image.caption_file_modified_time_ns)
            for image in self.image_list_model.images
        }
        scanner = RefreshScanner(self.directory_path,
                                 self.image_list_model.get_image_suffixes(),
                                 self.image_list_model.tag_separator,
                                 snapshot)
        scanner.refresh_complete.connect(self.on_refresh_complete)
        self._refresh_scanner = scanner
        QThreadPool.globalInstance().start(scanner)

    @Slot(object)
    def on_refresh_complete(self, result):
        self._refresh_scanner = None
        # Discard the result if the user switched directories while scanning.
        if result is None or result.directory_path != self.directory_path:
            return
        structure_changed, changed_rows = (
            self.image_list_model.apply_refresh_result(result))
        if structure_changed:
            self.image_list.apply_current_sort()
            self.tag_counter_model.count_tags(self.image_list_model.images)
        if not changed_rows:
            return
        if self.image_viewer.is_grid_mode():
            # In grid mode a single-image reload is a no-op, so refresh the
            # affected grid cells directly. This keeps the synchronized grid
            # consistent with externally-changed files picked up on re-focus.
            images = self.image_list_model.images
            changed_paths = {
                str(images[row].path)
                for row in changed_rows if 0 <= row < len(images)}
            self.image_viewer.refresh_grid_paths(changed_paths)
            return
        current_proxy_index = self.image_list.list_view.currentIndex()
        if not current_proxy_index.isValid():
            return
        current_source_index = self.proxy_image_list_model.mapToSource(
            current_proxy_index)
        if current_source_index.row() not in changed_rows:
            return
        self.image_viewer.load_image(current_proxy_index)


    @Slot()
    def show_settings_dialog(self):
        settings_dialog = SettingsDialog(parent=self,
                                         tag_library_model=self.tag_library_model,
                                         dialog_font=QFont(self.app.font()))
        settings_dialog.theme_changed.connect(self.apply_theme)
        settings_dialog.keyboard_shortcuts_requested.connect(
            lambda: self.show_keyboard_shortcuts_dialog(settings_dialog))
        settings_dialog.theme_changed.connect(
            lambda: self.apply_dialog_title_bar_theme(settings_dialog))
        settings_dialog.image_list_font_size_changed.connect(
            self.refresh_image_list_font_size)
        settings_dialog.image_list_image_width_changed.connect(
            self.refresh_image_list_image_width)
        settings_dialog.image_list_resolution_badge_settings_changed.connect(
            self.refresh_image_list_resolution_badge_style)
        settings_dialog.image_list_completion_icon_settings_changed.connect(
            self.refresh_image_list_resolution_badge_style)
        settings_dialog.variant_grid_overlay_settings_changed.connect(
            self.image_viewer.refresh_grid_overlay_style)
        settings_dialog.token_limit_changed.connect(
            self.image_tags_editor.count_tokens)
        settings_dialog.tag_separator_changed.connect(
            self.refresh_tag_separator)
        settings_dialog.autocomplete_changed.connect(self.refresh_autocomplete)
        settings_dialog.remove_app_data_requested.connect(
            self.quit_after_removing_app_data)
        # Settings that trigger a directory/model scan are applied once on
        # close (by diffing) rather than on every keystroke.
        old_file_formats = self.settings.value(
            'image_list_file_formats',
            defaultValue=DEFAULT_SETTINGS['image_list_file_formats'], type=str)
        old_models_directory = self.settings.value(
            'models_directory_path',
            defaultValue=DEFAULT_SETTINGS['models_directory_path'], type=str)
        old_hidden_models = get_hidden_model_ids()
        self.apply_dialog_title_bar_theme(settings_dialog)
        settings_dialog.exec()
        # If the user removed app data, the app is quitting; don't read the
        # (now cleared) settings or trigger a rescan that would recreate caches.
        if self.is_removing_app_data:
            return
        new_file_formats = self.settings.value(
            'image_list_file_formats',
            defaultValue=DEFAULT_SETTINGS['image_list_file_formats'], type=str)
        if new_file_formats != old_file_formats:
            self.reload_directory_for_settings_change()
        new_models_directory = self.settings.value(
            'models_directory_path',
            defaultValue=DEFAULT_SETTINGS['models_directory_path'], type=str)
        if (new_models_directory != old_models_directory
                or get_hidden_model_ids() != old_hidden_models):
            self.auto_captioner.caption_settings_form.refresh_model_list()

    @Slot()
    def quit_after_removing_app_data(self):
        """Quit immediately after the user removed app data, without saving
        anything (which would recreate the just-deleted settings/caches)."""
        self.is_removing_app_data = True
        self.should_skip_save_state_on_close = True
        QApplication.quit()

    @Slot()
    def show_tag_library_dialog(self):
        if not hasattr(self, '_tag_library_dialog') or self._tag_library_dialog is None:
            self._tag_library_dialog = TagLibraryDialog(
                self.tag_library_model, image_list_model=self.image_list_model,
                parent=self)
            self._tag_library_dialog.danbooru_wiki_requested.connect(
                self.show_danbooru_wiki_dialog)
            self._tag_library_dialog.gelbooru_wiki_requested.connect(
                self.show_gelbooru_wiki_dialog)
            self.apply_dialog_title_bar_theme(self._tag_library_dialog)
        self._apply_tag_library_dialog_shortcuts()
        self._tag_library_dialog.show()
        self._tag_library_dialog.raise_()
        self._tag_library_dialog.activateWindow()

    @Slot()
    def show_export_settings_dialog(self):
        dialog = ExportSettingsDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        
        file_path, _ = QFileDialog.getSaveFileName(
            parent=self,
            caption='Export Settings',
            filter='JSON Files (*.json)',
            dir='taggui_settings.json'
        )
        if not file_path:
            return
        
        include_tag_library = dialog.get_include_tag_library()
        include_auto_captioner = dialog.get_include_auto_captioner()
        include_completed_images = dialog.get_include_completed_images()
        if save_settings_to_file(file_path, include_tag_library,
                                 include_auto_captioner,
                                 include_completed_images):
            QMessageBox.information(
                self, 'Export Successful',
                f'Settings exported to:\n{file_path}')
        else:
            QMessageBox.critical(
                self, 'Export Failed',
                'Failed to export settings. Please check file permissions.')

    @Slot()
    def show_import_settings_dialog(self):
        file_path, _ = QFileDialog.getOpenFileName(
            parent=self,
            caption='Import Settings',
            filter='JSON Files (*.json)'
        )
        if not file_path:
            return
        
        import_data = load_settings_from_file(file_path)
        if import_data is None:
            QMessageBox.critical(
                self, 'Import Failed',
                'Invalid settings file. Please select a valid exported settings file.')
            return
        
        preview_dialog = ImportSettingsPreviewDialog(import_data, parent=self)
        if preview_dialog.exec() != QDialog.DialogCode.Accepted:
            return
        
        selected_categories = preview_dialog.get_selected_categories()
        if not selected_categories:
            return
        
        if import_settings(import_data, selected_categories):
            # Completion marks apply without a restart: re-home imported marks
            # onto matching images (by hash) and refresh the completion icons
            # by re-scanning the current directory.
            if ('completed_images' in selected_categories
                    and self.directory_path is not None
                    and self.directory_path.exists()):
                self.load_directory(self.directory_path)
            reply = QMessageBox.question(
                self, 'Import Successful',
                'Settings imported successfully.\n\nRestart the application to apply all changes?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                # Set flag to prevent closeEvent from overwriting the imported settings
                self.should_skip_save_state_on_close = True
                self._restart_application()
        else:
            QMessageBox.critical(
                self, 'Import Failed',
                'Failed to import settings. Please check file permissions.')

    def _restart_application(self):
        """Restart the application.

        When TagGUI is launched through the managed launcher (start.bat /
        start.py), that launcher watches the app's exit code and relaunches it
        in the SAME console window. In that case we simply ask the app to quit
        with the special restart exit code, so no extra console window is
        opened and the original one is reused. Otherwise (a packaged .exe, or
        run_gui.py started directly), we fall back to spawning a fresh process
        ourselves, exactly as before.
        """
        launched_by_managed_launcher = (
            os.environ.get('TAGGUI_MANAGED_LAUNCHER') == '1'
            and not getattr(sys, 'frozen', False))
        if launched_by_managed_launcher:
            # run_gui.py reads this flag after the event loop stops and exits
            # with the restart code, which tells the launcher to relaunch us.
            self.app.restart_requested = True
            self.should_skip_save_state_on_close = True
            self.close()
            return

        # Fallback: no managed launcher. Spawn a new process directly.
        # Get the path to run_gui.py
        repo_root = Path(__file__).parent.parent.parent
        run_gui_script = repo_root / 'taggui' / 'run_gui.py'

        if getattr(sys, 'frozen', False):
            # Running as a .exe - just restart the executable
            subprocess.Popen([sys.executable])
        else:
            # Running from source - use Python to run run_gui.py
            subprocess.Popen([sys.executable, str(run_gui_script)])

        self.close()

    def get_shortcut_specs(self) -> list[tuple[str, str, str | list[str]]]:
        return [
            ('load_directory', 'Load Directory...', 'Ctrl+Alt+L'),
            ('reload_directory', 'Reload Directory', 'F5'),
            ('settings', 'Settings...', 'Ctrl+Alt+S'),
            ('exit', 'Exit', 'Ctrl+W'),
            ('undo', 'Undo', 'Ctrl+Z'),
            ('redo', 'Redo', 'Ctrl+Y'),
            ('find_replace', 'Find and Replace...', 'Ctrl+R'),
            ('batch_reorder_tags', 'Batch Reorder Tags...', 'Ctrl+B'),
            ('apply_default_batch_reorder', 'Apply Default Batch Reorder', ''),
            ('invert_selection', 'Invert Selection', 'Ctrl+I'),
            ('copy_tags', 'Copy Tags', 'Ctrl+C'),
            ('copy_caption', 'Copy Caption', 'Ctrl+Shift+C'),
            ('copy_prompt', 'Copy Natural Language Prompt', 'Ctrl+N'),
            ('paste_tags', 'Paste Tags', 'Ctrl+V'),
            ('copy_file_name', 'Copy File Name', 'Ctrl+Shift+P'),
            ('copy_path', 'Copy Path', 'Ctrl+P'),
            ('move_images', 'Move Images to...', 'Ctrl+M'),
            ('copy_images', 'Copy Images to...', 'Ctrl+Shift+M'),
            ('delete_images', 'Delete Images', 'Ctrl+Del'),
            ('open_image', 'Open Image in Default App', 'Ctrl+O'),
            ('open_image_editor', 'Open Image in Configured Editor',
             'Ctrl+E'),
            ('rename_image_file', 'Rename Image File...', 'F2'),
            ('open_caption_file', 'Open Caption File in Default App',
             'Ctrl+Shift+O'),
            ('open_danbooru_wiki', 'Danbooru Wiki...', 'Ctrl+D'),
            ('open_gelbooru_wiki', 'Gelbooru Wiki...', 'Ctrl+G'),
            ('find_duplicates', 'Find Duplicates...', ''),
            ('directory_analytics', 'Directory Analytics...', ''),
            ('tag_library', 'Tag Library...', 'Ctrl+L'),
            ('select_all_images', 'Select All Images', 'Ctrl+A'),
            ('mark_complete', 'Mark as Complete', 'Ctrl+K'),
            ('mark_incomplete', 'Mark as Incomplete', 'Ctrl+Shift+K'),
            ('go_to_previous_image', 'Go to Previous Image', 'Ctrl+Up'),
            ('go_to_next_image', 'Go to Next Image', 'Ctrl+Down'),
            ('jump_to_first_untagged_image', 'Jump to First Untagged Image',
             'Ctrl+J'),
            ('jump_to_first_incomplete_image',
             'Jump to First Incomplete Image', 'Ctrl+Shift+J'),
            ('reset_image_zoom', 'Reset Image Zoom', 'Ctrl+0'),
            ('zoom_in', 'Zoom In', ['Ctrl++', 'Ctrl+=']),
            ('zoom_out', 'Zoom Out', 'Ctrl+-'),
            ('pan_image_left', 'Pan Image Left', 'Alt+Left'),
            ('pan_image_right', 'Pan Image Right', 'Alt+Right'),
            ('pan_image_up', 'Pan Image Up', 'Alt+Up'),
            ('pan_image_down', 'Pan Image Down', 'Alt+Down'),
            ('focus_image_list_filter', 'Focus Images Filter Box',
             ['Alt+F', 'Ctrl+F']),
            ('focus_add_tag_box', 'Focus Add Tag Box', 'Alt+A'),
            ('focus_image_tags_list', 'Focus Image Tags List', 'Alt+I'),
            ('focus_all_tags_search', 'Focus All Tags Search Box', 'Alt+S'),
            ('focus_auto_caption_button', 'Focus Auto-Caption Button', 'Alt+C')
        ]

    def get_shortcut_action_map(self) -> dict[str, QAction]:
        image_list_shortcuts = {
            shortcut_id: action for shortcut_id, (_, action)
            in self.image_list.list_view.get_shortcut_actions().items()
        }
        return {
            'load_directory': self.load_directory_action,
            'reload_directory': self.reload_directory_action,
            'settings': self.settings_action,
            'exit': self.exit_action,
            'undo': self.undo_action,
            'redo': self.redo_action,
            'find_replace': self.find_and_replace_action,
            'batch_reorder_tags': self.batch_reorder_tags_action,
            'apply_default_batch_reorder':
                self.apply_default_batch_reorder_action,
            'open_danbooru_wiki': self.open_danbooru_wiki_action,
            'open_gelbooru_wiki': self.open_gelbooru_wiki_action,
            'find_duplicates': self.find_duplicates_action,
            'directory_analytics': self.directory_analytics_action,
            'tag_library': self.tag_library_action,
            'go_to_previous_image': self.go_to_previous_image_shortcut,
            'go_to_next_image': self.go_to_next_image_shortcut,
            'jump_to_first_untagged_image':
                self.jump_to_first_untagged_image_shortcut,
            'jump_to_first_incomplete_image':
                self.jump_to_first_incomplete_image_shortcut,
            'reset_image_zoom': self.reset_image_zoom_shortcut,
            'zoom_in': self.zoom_in_shortcut,
            'zoom_out': self.zoom_out_shortcut,
            'pan_image_left': self.pan_image_left_shortcut,
            'pan_image_right': self.pan_image_right_shortcut,
            'pan_image_up': self.pan_image_up_shortcut,
            'pan_image_down': self.pan_image_down_shortcut,
            'focus_image_list_filter': self.focus_filter_images_box_shortcut,
            'focus_add_tag_box': self.focus_add_tag_box_shortcut,
            'focus_image_tags_list': self.focus_image_tags_list_shortcut,
            'focus_all_tags_search': self.focus_search_tags_box_shortcut,
            'focus_auto_caption_button': self.focus_caption_button_shortcut,
            **image_list_shortcuts
        }

    def get_shortcut_handlers_map(self) -> dict[str, list]:
        """Slots to (re)connect for the global QShortcut-based actions.

        Used when creating supplemental ``QShortcut`` objects so one action can
        respond to more than one key sequence (a ``QShortcut`` itself only
        holds a single key). The lists mirror the connections made where the
        primary shortcuts are constructed in ``__init__``.
        """
        return {
            'focus_image_list_filter': [
                self.image_list.raise_,
                self.image_list.filter_line_edit.setFocus],
            'focus_add_tag_box': [
                self.image_tags_editor.raise_,
                self.image_tags_editor.tag_input_box.setFocus],
            'focus_image_tags_list': [
                self.image_tags_editor.focus_tags_list],
            'focus_all_tags_search': [
                self.all_tags_editor.raise_,
                self.all_tags_editor.filter_line_edit.setFocus],
            'focus_auto_caption_button': [
                self.auto_captioner.focus_start_cancel_button],
            'go_to_previous_image': [self.image_list.go_to_previous_image],
            'go_to_next_image': [self.image_list.go_to_next_image],
            'jump_to_first_untagged_image': [
                self.image_list.jump_to_first_untagged_image],
            'jump_to_first_incomplete_image': [
                self.image_list.jump_to_first_incomplete_image],
            'reset_image_zoom': [self.image_viewer.reset_view],
            'zoom_in': [self.image_viewer.zoom_in],
            'zoom_out': [self.image_viewer.zoom_out],
            'pan_image_left': [self.image_viewer.pan_left],
            'pan_image_right': [self.image_viewer.pan_right],
            'pan_image_up': [self.image_viewer.pan_up],
            'pan_image_down': [self.image_viewer.pan_down],
        }

    def apply_configured_shortcuts(self):
        configured_shortcuts = self.settings.value(
            'keyboard_shortcuts', defaultValue={})
        if not isinstance(configured_shortcuts, dict):
            configured_shortcuts = {}
        shortcut_specs = self.get_shortcut_specs()
        action_by_id = self.get_shortcut_action_map()
        handlers_by_id = self.get_shortcut_handlers_map()
        if not hasattr(self, '_extra_qshortcuts'):
            self._extra_qshortcuts = {}
        for shortcut_id, _, default_shortcut in shortcut_specs:
            action = action_by_id.get(shortcut_id)
            if action is None:
                continue
            if shortcut_id in configured_shortcuts:
                sequence_strings = normalize_shortcut_sequences(
                    configured_shortcuts[shortcut_id])
            else:
                sequence_strings = normalize_shortcut_sequences(
                    default_shortcut)
            key_sequences = [QKeySequence(sequence)
                             for sequence in sequence_strings]
            # The map holds both QActions (menu/list actions, which support
            # multiple shortcuts natively via setShortcuts) and QShortcuts (the
            # global navigation/focus shortcuts, which hold a single key each
            # and so need supplemental QShortcut objects for extra bindings).
            if isinstance(action, QShortcut):
                self._apply_qshortcut_sequences(
                    shortcut_id, action, key_sequences,
                    handlers_by_id.get(shortcut_id, []))
            else:
                action.setShortcuts(key_sequences)
        # Refresh menu to recalculate layout with shortcuts now set
        self.menu_bar.update()
        self._apply_tag_library_dialog_shortcuts()

    def _apply_qshortcut_sequences(self, shortcut_id: str,
                                   primary_shortcut: QShortcut,
                                   key_sequences: list[QKeySequence],
                                   handlers: list):
        """Bind a QShortcut-based action to any number of key sequences.

        The first sequence is applied to the existing (primary) QShortcut; each
        additional sequence gets its own supplemental QShortcut wired to the
        same handlers. Supplemental shortcuts from a previous call are discarded
        first so repeated applies don't accumulate.
        """
        for extra_shortcut in self._extra_qshortcuts.get(shortcut_id, []):
            extra_shortcut.setKey(QKeySequence())
            extra_shortcut.setEnabled(False)
            extra_shortcut.deleteLater()
        self._extra_qshortcuts[shortcut_id] = []
        if not key_sequences:
            primary_shortcut.setKey(QKeySequence())
            return
        primary_shortcut.setKey(key_sequences[0])
        for extra_sequence in key_sequences[1:]:
            extra_shortcut = QShortcut(extra_sequence, self)
            for handler in handlers:
                extra_shortcut.activated.connect(handler)
            self._extra_qshortcuts[shortcut_id].append(extra_shortcut)

    def _apply_tag_library_dialog_shortcuts(self):
        """Keep the Tag Library dialog's wiki shortcuts in sync with the
        (possibly customized) main window wiki shortcuts, so they work while
        that dialog is focused."""
        dialog = getattr(self, '_tag_library_dialog', None)
        if dialog is None:
            return
        dialog.set_wiki_shortcuts(
            self.open_danbooru_wiki_action.shortcut(),
            self.open_gelbooru_wiki_action.shortcut())

    @Slot()
    def show_keyboard_shortcuts_dialog(self, parent: QWidget | None = None):
        configured_shortcuts = self.settings.value(
            'keyboard_shortcuts', defaultValue={})
        if not isinstance(configured_shortcuts, dict):
            configured_shortcuts = {}
        keyboard_shortcuts_dialog = KeyboardShortcutsDialog(
            parent=parent or self,
            shortcut_specs=self.get_shortcut_specs(),
            configured_shortcuts=configured_shortcuts)
        if keyboard_shortcuts_dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.settings.setValue('keyboard_shortcuts',
                               keyboard_shortcuts_dialog.shortcut_by_id)
        self.apply_configured_shortcuts()

    @Slot()
    def refresh_image_list_resolution_badge_style(self):
        self.image_list.list_view.refresh_resolution_badge_style()

    def refresh_pane_title_fonts(self):
        dock_widgets = (self.image_list, self.image_tags_editor,
                        self.all_tags_editor,
                        self.auto_captioner)
        font_size = self.settings.value(
            'font_size', defaultValue=DEFAULT_SETTINGS['font_size'], type=int)
        palette = self.app.palette()
        title_color = palette.color(QPalette.ColorRole.WindowText).name()
        title_background_color = palette.color(QPalette.ColorRole.Window).name()
        dock_title_style = (
            f'QDockWidget {{ font-size: {font_size}pt; }}'
            f' QDockWidget::title {{'
            f' font-size: {font_size}pt;'
            f' color: {title_color};'
            f' background-color: {title_background_color};'
            f' }}')
        tab_font = QFont(self.app.font())
        tab_font.setPointSize(font_size)
        for tab_bar in self.findChildren(QTabBar):
            tab_bar.setFont(tab_font)
            tab_bar.style().unpolish(tab_bar)
            tab_bar.style().polish(tab_bar)
            tab_bar.updateGeometry()
            tab_bar.update()
        for dock_widget in dock_widgets:
            dock_widget.setStyleSheet('')
            dock_widget.setStyleSheet(dock_title_style)
            dock_widget.style().unpolish(dock_widget)
            dock_widget.style().polish(dock_widget)
            dock_widget.updateGeometry()
            dock_widget.update()

    @Slot()
    def refresh_image_list_font_size(self):
        image_list_font_size = self.settings.value(
            'image_list_font_size',
            defaultValue=DEFAULT_SETTINGS['image_list_font_size'], type=int)
        self.image_list.refresh_font_size(image_list_font_size)

    @Slot()
    def refresh_image_list_image_width(self):
        image_list_image_width = self.settings.value(
            'image_list_image_width',
            defaultValue=DEFAULT_SETTINGS['image_list_image_width'], type=int)
        self.image_list_model.set_image_width(image_list_image_width)
        self.image_list.list_view.refresh_image_width(image_list_image_width)
        self.refresh_image_list_resolution_badge_style()

    @Slot()
    def refresh_tag_separator(self):
        """Apply a changed tag separator (or insert-space option) live, without
        a restart, by pushing the new value into every object that cached it."""
        tag_separator = get_tag_separator()
        self.image_list_model.tag_separator = tag_separator
        self.proxy_image_list_model.tag_separator = tag_separator
        self.image_list.set_tag_separator(tag_separator)
        self.image_tags_editor.set_tag_separator(tag_separator)
        # The proxy filter matches against joined captions, so re-run it.
        if self.proxy_image_list_model.filter is not None:
            self.proxy_image_list_model.invalidateFilter()

    @Slot()
    def refresh_autocomplete(self):
        """Enable or disable tag autocomplete live, without a restart."""
        autocomplete_tags = self.settings.value(
            'autocomplete_tags',
            defaultValue=DEFAULT_SETTINGS['autocomplete_tags'], type=bool)
        self.image_tags_editor.set_autocomplete_enabled(autocomplete_tags)

    def reload_directory_for_settings_change(self):
        """Reload the current directory so a changed image-file-formats filter
        takes effect without a restart."""
        if self.directory_path is None or not self.directory_path.exists():
            return
        self.load_directory(self.directory_path)

    @Slot()
    def show_find_and_replace_dialog(self):
        find_and_replace_dialog = FindAndReplaceDialog(
            parent=self, image_list_model=self.image_list_model)
        find_and_replace_dialog.exec()

    @Slot()
    def show_find_duplicates_dialog(self):
        find_duplicates_dialog = FindDuplicatesDialog(
            parent=self, image_list_model=self.image_list_model,
            reload_callback=self.reload_directory)
        find_duplicates_dialog.exec()

    @Slot()
    def show_directory_analytics_dialog(self):
        directory_analytics_dialog = DirectoryAnalyticsDialog(
            parent=self, image_list_model=self.image_list_model,
            directory_path=self.directory_path)
        directory_analytics_dialog.exec()

    @Slot()
    def show_batch_reorder_tags_dialog(self):
        batch_reorder_tags_dialog = BatchReorderTagsDialog(
            parent=self, image_list_model=self.image_list_model,
            tag_counter_model=self.tag_counter_model,
            tag_library_model=self.tag_library_model)
        batch_reorder_tags_dialog.exec()

    @Slot()
    def apply_default_batch_reorder(self):
        default_option = self.settings.value(
            'default_batch_reorder',
            defaultValue=DEFAULT_SETTINGS['default_batch_reorder'], type=str)
        apply_batch_reorder(
            default_option, self.image_list_model, self.tag_counter_model,
            self.tag_library_model)

    def _selected_tag_for_wiki_shortcut(self) -> str:
        """Return the tag the wiki keyboard shortcut should auto-search, based
        on what currently has keyboard focus.

        Priority order:
        1. If one of the text input boxes (the Image Tags "Add Tag" box or the
           All Tags "Filter Tags" box) is focused, use the text typed in it.
           When that box is empty, fall back to the single highlighted tag in
           that box's own tag pane.
        2. If one of the tag panes (Image Tags or All Tags) is focused, use its
           single highlighted tag.
        3. Otherwise return '' so the wiki opens without a preset tag (e.g. when
           the shortcut is used from the Tools menu)."""
        # (input box, tag pane the box belongs to)
        editor = self.image_tags_editor
        # The Image Tags Add Tag box shares the pane with the grouped view, so
        # when it is empty fall back to the mode-aware selected tag (normal list
        # or Common/Differences panel).
        if editor.tag_input_box.hasFocus():
            text = editor.tag_input_box.text().strip()
            if text:
                return text
            return editor.selected_tag_for_wiki()
        if self.all_tags_editor.filter_line_edit.hasFocus():
            text = self.all_tags_editor.filter_line_edit.text().strip()
            if text:
                return text
            return self.all_tags_editor.all_tags_list.selected_tag_for_wiki()
        if editor.tag_pane_has_focus():
            return editor.selected_tag_for_wiki()
        if self.all_tags_editor.all_tags_list.hasFocus():
            return self.all_tags_editor.all_tags_list.selected_tag_for_wiki()
        return ''

    @Slot(str)
    def show_danbooru_wiki_dialog(self, tag: str = ''):
        in_group_mode = self.image_tags_editor.is_group_mode()
        danbooru_wiki_dialog = DanbooruWikiDialog(
            self, tag if tag.strip() else '',
            tag_library_model=self.tag_library_model,
            add_to_library_callback=self.add_wiki_tag_to_library,
            add_to_selected_images_callback=(
                self.add_wiki_tag_to_selected_images),
            selected_images_have_tag_callback=(
                self.wiki_selected_images_have_tag),
            add_to_current_image_callback=(
                self.add_wiki_tag_to_current_image if in_group_mode else None),
            current_image_has_tag_callback=(
                self.wiki_current_image_has_tag if in_group_mode else None))
        # Destroy the dialog (and its timers/threads) deterministically on the
        # GUI thread when it closes, instead of leaking one live dialog per
        # open.
        danbooru_wiki_dialog.setAttribute(
            Qt.WidgetAttribute.WA_DeleteOnClose, True)
        danbooru_wiki_dialog.exec()

    @Slot(str)
    def show_gelbooru_wiki_dialog(self, tag: str = ''):
        in_group_mode = self.image_tags_editor.is_group_mode()
        gelbooru_wiki_dialog = GelbooruWikiDialog(
            self, tag if tag.strip() else '',
            tag_library_model=self.tag_library_model,
            add_to_library_callback=self.add_wiki_tag_to_library,
            add_to_selected_images_callback=(
                self.add_wiki_tag_to_selected_images),
            selected_images_have_tag_callback=(
                self.wiki_selected_images_have_tag),
            add_to_current_image_callback=(
                self.add_wiki_tag_to_current_image if in_group_mode else None),
            current_image_has_tag_callback=(
                self.wiki_current_image_has_tag if in_group_mode else None))
        gelbooru_wiki_dialog.setAttribute(
            Qt.WidgetAttribute.WA_DeleteOnClose, True)
        gelbooru_wiki_dialog.exec()

    def create_menus(self):
        self.menu_bar = self.menuBar()

        file_menu = self.menu_bar.addMenu('File')
        self.load_directory_action = QAction('Load Directory...', parent=self)
        self.load_directory_action.triggered.connect(
            self.select_and_load_directory)
        file_menu.addAction(self.load_directory_action)
        self.reload_directory_action.triggered.connect(self.reload_directory)
        self.addAction(self.reload_directory_action)
        self.settings_action = QAction('Settings...', parent=self)
        self.settings_action.triggered.connect(self.show_settings_dialog)
        file_menu.addAction(self.settings_action)
        file_menu.addSeparator()
        self.export_settings_action = QAction('Export Settings...', parent=self)
        self.export_settings_action.triggered.connect(self.show_export_settings_dialog)
        file_menu.addAction(self.export_settings_action)
        self.import_settings_action = QAction('Import Settings...', parent=self)
        self.import_settings_action.triggered.connect(self.show_import_settings_dialog)
        file_menu.addAction(self.import_settings_action)
        file_menu.addSeparator()
        self.exit_action = QAction('Exit', parent=self)
        self.exit_action.triggered.connect(self.close)
        file_menu.addAction(self.exit_action)

        edit_menu = self.menu_bar.addMenu('Edit')
        self.undo_action.triggered.connect(self.image_list_model.undo)
        self.undo_action.setDisabled(True)
        edit_menu.addAction(self.undo_action)
        self.redo_action.triggered.connect(self.image_list_model.redo)
        self.redo_action.setDisabled(True)
        edit_menu.addAction(self.redo_action)
        self.find_and_replace_action = QAction('Find and Replace...', parent=self)
        self.find_and_replace_action.triggered.connect(
            self.show_find_and_replace_dialog)
        edit_menu.addAction(self.find_and_replace_action)
        self.batch_reorder_tags_action = QAction('Batch Reorder Tags...',
                                                 parent=self)
        self.batch_reorder_tags_action.triggered.connect(
            self.show_batch_reorder_tags_dialog)
        edit_menu.addAction(self.batch_reorder_tags_action)
        self.apply_default_batch_reorder_action = QAction(
            'Apply Default Batch Reorder', parent=self)
        self.apply_default_batch_reorder_action.triggered.connect(
            self.apply_default_batch_reorder)
        edit_menu.addAction(self.apply_default_batch_reorder_action)

        view_menu = self.menu_bar.addMenu('View')
        self.toggle_image_list_action.setCheckable(True)
        self.toggle_image_tags_editor_action.setCheckable(True)
        self.toggle_all_tags_editor_action.setCheckable(True)
        self.toggle_auto_captioner_action.setCheckable(True)
        self.toggle_image_list_action.triggered.connect(
            lambda is_checked: self.image_list.setVisible(is_checked))
        self.toggle_image_tags_editor_action.triggered.connect(
            lambda is_checked: self.image_tags_editor.setVisible(is_checked))
        self.toggle_all_tags_editor_action.triggered.connect(
            lambda is_checked: self.all_tags_editor.setVisible(is_checked))
        self.toggle_auto_captioner_action.triggered.connect(
            lambda is_checked: self.auto_captioner.setVisible(is_checked))
        view_menu.addAction(self.toggle_image_list_action)
        view_menu.addAction(self.toggle_image_tags_editor_action)
        view_menu.addAction(self.toggle_all_tags_editor_action)
        view_menu.addAction(self.toggle_auto_captioner_action)

        tools_menu = self.menu_bar.addMenu('Tools')
        self.open_danbooru_wiki_action.triggered.connect(
            lambda: self.show_danbooru_wiki_dialog(
                self._selected_tag_for_wiki_shortcut()))
        tools_menu.addAction(self.open_danbooru_wiki_action)
        self.open_gelbooru_wiki_action.triggered.connect(
            lambda: self.show_gelbooru_wiki_dialog(
                self._selected_tag_for_wiki_shortcut()))
        tools_menu.addAction(self.open_gelbooru_wiki_action)
        tools_menu.addSeparator()
        self.find_duplicates_action = QAction('Find Duplicates...', parent=self)
        self.find_duplicates_action.triggered.connect(
            self.show_find_duplicates_dialog)
        tools_menu.addAction(self.find_duplicates_action)
        self.directory_analytics_action = QAction('Directory Analytics...',
                                                  parent=self)
        self.directory_analytics_action.triggered.connect(
            self.show_directory_analytics_dialog)
        tools_menu.addAction(self.directory_analytics_action)
        tools_menu.addSeparator()
        self.tag_library_action = QAction('Tag Library...' + ' ' * 4, parent=self)
        self.tag_library_action.triggered.connect(self.show_tag_library_dialog)
        tools_menu.addAction(self.tag_library_action)

        help_menu = self.menu_bar.addMenu('Help')
        open_github_repository_action = QAction('GitHub', parent=self)
        open_github_repository_action.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl(GITHUB_REPOSITORY_URL)))
        help_menu.addAction(open_github_repository_action)

    @Slot()
    def update_undo_and_redo_actions(self):
        if self.image_list_model.undo_stack:
            undo_action_name = self.image_list_model.undo_stack[-1].action_name
            self.undo_action.setText(f'Undo "{undo_action_name}"')
            self.undo_action.setDisabled(False)
        else:
            self.undo_action.setText('Undo')
            self.undo_action.setDisabled(True)
        if self.image_list_model.redo_stack:
            redo_action_name = self.image_list_model.redo_stack[-1].action_name
            self.redo_action.setText(f'Redo "{redo_action_name}"')
            self.redo_action.setDisabled(False)
        else:
            self.redo_action.setText('Redo')
            self.redo_action.setDisabled(True)

    @Slot()
    def set_image_list_filter(self):
        filter_ = self.image_list.filter_line_edit.parse_filter_text()
        self.proxy_image_list_model.filter = filter_
        # Apply the new filter.
        self.proxy_image_list_model.invalidateFilter()
        if filter_ is None:
            all_tags_list_selection_model = (self.all_tags_editor
                                             .all_tags_list.selectionModel())
            all_tags_list_selection_model.clearSelection()
            # Clear the current index.
            self.all_tags_editor.all_tags_list.setCurrentIndex(QModelIndex())
            # Select the previously selected image in the unfiltered image
            # list.
            select_index = self.settings.value('image_index', type=int) or 0
            self.image_list.list_view.setCurrentIndex(
                self.proxy_image_list_model.index(select_index, 0))
        else:
            # Select the first image.
            self.image_list.list_view.setCurrentIndex(
                self.proxy_image_list_model.index(0, 0))

    @Slot()
    def save_image_index(self, proxy_image_index: QModelIndex):
        """Save the index of the currently selected image."""
        settings_key = ('image_index'
                        if self.proxy_image_list_model.filter is None
                        else 'filtered_image_index')
        self.settings.setValue(settings_key, proxy_image_index.row())

    def connect_image_list_signals(self):
        self.image_list.filter_line_edit.textChanged.connect(
            self.set_image_list_filter)
        self.image_list_selection_model.currentChanged.connect(
            self.save_image_index)
        self.image_list_selection_model.currentChanged.connect(
            self.image_list.update_image_index_label)
        self.image_list_selection_model.currentChanged.connect(
            self.image_viewer.load_image)
        self.image_list_selection_model.currentChanged.connect(
            self.image_tags_editor.load_image_tags)
        # Keep the grouped tag view and synchronized grid preview in sync with
        # the selection. Reconciled on both a changed selection and a moved
        # current image (arrow keys), after the single-image handlers above.
        self.image_list_selection_model.selectionChanged.connect(
            self._sync_variant_group_view)
        self.image_list_selection_model.currentChanged.connect(
            self._sync_variant_group_view)
        self.image_list_model.dataChanged.connect(
            lambda: self.tag_counter_model.count_tags(
                self.image_list_model.images))
        self.image_list_model.dataChanged.connect(
            self.image_tags_editor.reload_image_tags_if_changed)
        self.tag_library_model.modelReset.connect(
            self.image_list.list_view.viewport().update)
        self.tag_library_model.categories_changed.connect(
            self.image_list.list_view.viewport().update)
        self.image_list_model.update_undo_and_redo_actions_requested.connect(
            self.update_undo_and_redo_actions)
        self.image_list_model.directory_loaded.connect(
            self._on_directory_loaded)
        # Rows are inserted or removed from the proxy image list model when the
        # filter is changed.
        self.proxy_image_list_model.rowsInserted.connect(
            lambda: self.image_list.update_image_index_label(
                self.image_list.list_view.currentIndex()))
        self.proxy_image_list_model.rowsRemoved.connect(
            lambda: self.image_list.update_image_index_label(
                self.image_list.list_view.currentIndex()))
        self.image_list.list_view.directory_reload_requested.connect(
            self.refresh_changed_image_files)
        self.image_list.list_view.tags_paste_requested.connect(
            self.image_list_model.add_tags)
        self.image_viewer.clicked.connect(self.image_list.raise_)
        self.image_viewer.clicked.connect(self._focus_image_list_on_preview_click)
        self.image_viewer.grid_cell_clicked.connect(self._select_grid_cell)
        self.tag_counter_model.modelReset.connect(self.track_all_tags_changes)
        # Connecting the signal directly without `isVisible()` causes the menu
        # item to be unchecked when the widget is an inactive tab.
        self.image_list.visibilityChanged.connect(
            lambda: self.toggle_image_list_action.setChecked(
                self.image_list.isVisible()))

    @Slot(QModelIndex)
    def _select_grid_cell(self, proxy_index: QModelIndex):
        """Make a clicked grid cell the current image without changing selection.

        Mirrors the arrow-key path (switch_between_selected_images): only the
        current index moves, using NoUpdate so the selection set is untouched.
        Clicks that don't map to a currently-selected image are ignored.
        """
        if not proxy_index.isValid():
            return
        selected_proxy_indices = self.image_list.list_view.selectedIndexes()
        if proxy_index not in selected_proxy_indices:
            return
        if proxy_index == self.image_list_selection_model.currentIndex():
            return
        self.image_list_selection_model.setCurrentIndex(
            proxy_index, QItemSelectionModel.SelectionFlag.NoUpdate)

    @Slot()
    def _sync_variant_group_view(self, *args):
        """Reconcile the grouped tag view and grid preview with the selection.

        When the variant grid setting is on and two or more images are
        selected, the Image Tags pane shows the grouped Common/Differences view
        and the center preview shows a synchronized grid of the selection.
        Otherwise the panes fall back to the normal single-image view. This is
        safe to call repeatedly; it fully derives the desired state from the
        current selection each time.
        """
        grid_enabled = get_settings().value(
            'variant_grid_view_enabled',
            defaultValue=DEFAULT_SETTINGS['variant_grid_view_enabled'],
            type=bool)
        list_view = self.image_list.list_view
        selected_proxy_indices = sorted(list_view.selectedIndexes(),
                                        key=lambda index: index.row())
        if grid_enabled and len(selected_proxy_indices) >= 2:
            current_proxy_index = self.image_list_selection_model.currentIndex()
            selected_rows = [index.row() for index in selected_proxy_indices]
            current_row = current_proxy_index.row()
            current_position = (selected_rows.index(current_row)
                                if current_row in selected_rows else 0)
            source_indices = [
                self.proxy_image_list_model.mapToSource(index)
                for index in selected_proxy_indices]
            current_source_index = source_indices[current_position]
            cell_cap = get_settings().value(
                'variant_grid_cell_cap',
                defaultValue=DEFAULT_SETTINGS['variant_grid_cell_cap'],
                type=int)
            if self.image_tags_editor.is_group_mode():
                self.image_tags_editor.update_group_selection(
                    source_indices, current_source_index)
                self.image_viewer.update_grid_current(
                    selected_proxy_indices, current_position, cell_cap)
            else:
                self.image_tags_editor.enter_group_mode(
                    source_indices, current_source_index)
                self.image_viewer.show_grid(
                    selected_proxy_indices, current_position, cell_cap)
            return
        # Single-image (or disabled) view.
        was_grid = self.image_viewer.is_grid_mode()
        was_group = self.image_tags_editor.is_group_mode()
        self.image_tags_editor.exit_group_mode()
        if was_group:
            # currentChanged fires before selectionChanged, so load_image_tags
            # already ran once while group mode was still active and early
            # returned without refreshing the single-image tag list or the
            # green "Complete" indicator. Now that group mode is off, reload the
            # current image so both reflect its real state.
            current_proxy_index = self.image_list_selection_model.currentIndex()
            if current_proxy_index.isValid():
                self.image_tags_editor.load_image_tags(current_proxy_index)
        if was_grid:
            self.image_viewer.exit_grid()
            current_proxy_index = self.image_list_selection_model.currentIndex()
            if current_proxy_index.isValid():
                self.image_viewer.load_image(current_proxy_index)

    @Slot()
    def _focus_image_list_on_preview_click(self):
        """Move focus to the thumbnails after a preview click, unless the user
        was working in the Image Tags pane.

        Clicking or dragging in the preview normally focuses the Images pane so
        the arrow keys navigate images. But if focus is already in the Image
        Tags pane (e.g. the Add Tag input or the tag list), keep it there so
        zooming/dragging the preview doesn't interrupt tag entry or up/down
        cycling through tags."""
        focus_widget = QApplication.focusWidget()
        if (focus_widget is not None
                and self.image_tags_editor.isAncestorOf(focus_widget)):
            return
        self.image_list.list_view.setFocus()

    @Slot()
    def update_image_tags(self):
        if self.image_tags_editor.is_loading_image_tags:
            return
        image_index = self.image_tags_editor.image_index
        image: Image = self.image_list_model.data(image_index,
                                                  Qt.ItemDataRole.UserRole)
        old_tags = image.tags
        new_tags = list(dict.fromkeys(self.image_tag_list_model.stringList()))
        if old_tags == new_tags:
            return
        old_tags_count = len(old_tags)
        new_tags_count = len(new_tags)
        if new_tags_count > old_tags_count:
            self.image_list_model.add_to_undo_stack(
                action_name='Add Tag', should_ask_for_confirmation=False)
        elif new_tags_count == old_tags_count:
            if set(new_tags) == set(old_tags):
                self.image_list_model.add_to_undo_stack(
                    action_name='Reorder Tags',
                    should_ask_for_confirmation=False)
            else:
                self.image_list_model.add_to_undo_stack(
                    action_name='Rename Tag',
                    should_ask_for_confirmation=False)
        elif old_tags_count - new_tags_count == 1:
            self.image_list_model.add_to_undo_stack(
                action_name='Delete Tag', should_ask_for_confirmation=False)
        else:
            self.image_list_model.add_to_undo_stack(
                action_name='Delete Tags', should_ask_for_confirmation=False)
        self.image_list_model.update_image_tags(image_index, new_tags)

    @Slot(QModelIndex, QModelIndex, list)
    def handle_image_tag_list_data_changed(self, _top_left: QModelIndex,
                                           _bottom_right: QModelIndex,
                                           roles: list[int]):
        if roles and all(role == Qt.ItemDataRole.ForegroundRole
                         for role in roles):
            return
        self.update_image_tags()

    def connect_image_tags_editor_signals(self):
        # `rowsInserted` does not have to be connected because `dataChanged`
        # is emitted when a tag is added.
        self.image_tag_list_model.modelReset.connect(self.update_image_tags)
        self.image_tag_list_model.dataChanged.connect(
            self.handle_image_tag_list_data_changed)
        self.image_tag_list_model.rowsMoved.connect(self.update_image_tags)
        self.image_tags_editor.visibilityChanged.connect(
            lambda: self.toggle_image_tags_editor_action.setChecked(
                self.image_tags_editor.isVisible()))
        self.image_tags_editor.tag_input_box.tags_addition_requested.connect(
            self.image_list_model.add_tags)
        self.image_tags_editor.tag_input_box.new_library_tags_added.connect(
            self.record_newly_added_library_tags)
        self.image_tags_editor.grid_mark_paths_changed.connect(
            self.image_viewer.set_marked_paths)
        self.image_tags_editor.danbooru_wiki_requested.connect(
            self.show_danbooru_wiki_dialog)
        self.image_tags_editor.gelbooru_wiki_requested.connect(
            self.show_gelbooru_wiki_dialog)

    @Slot(list)
    def set_image_list_filter_text(self, selected_tags: list[str]):
        """
        Construct and set the image list filter text from the selected tags in
        the all tags list.
        """
        if not selected_tags:
            return
        escaped_selected_tags = []
        for selected_tag in selected_tags:
            escaped_selected_tag = (selected_tag.replace('\\', '\\\\')
                                    .replace('"', r'\"')
                                    .replace("'", r"\'"))
            escaped_selected_tags.append(f'tag:"{escaped_selected_tag}"')
        filter_logic = self.all_tags_editor.filter_logic_combo_box.currentText()
        filter_text = f' {filter_logic} '.join(escaped_selected_tags)
        self.image_list.filter_line_edit.setText(filter_text)

    @Slot(str)
    def add_tag_to_selected_images(self, tag: str):
        selected_image_indices = self.image_list.get_selected_image_indices()
        self.image_list_model.add_tags([tag], selected_image_indices)
        self.image_tags_editor.select_last_tag_or_flash()

    def prompt_category_for_new_library_tags(self, tags: list[str],
                                             prompt_parent=None):
        """Show the category-assignment prompt for brand-new Tag Library tags,
        honouring the same settings as the rest of the app. ``prompt_parent``
        lets the prompt appear on top of a dialog that triggered it."""
        tags = [tag for tag in tags if tag]
        if not tags:
            return
        default_category_id = self.settings.value(
            'tag_library_new_tag_default_category_id',
            defaultValue=DEFAULT_SETTINGS['tag_library_new_tag_default_category_id'],
            type=str).strip()
        category_ids = {
            category['id']
            for category in self.tag_library_model.get_categories()
        }
        if default_category_id and default_category_id not in category_ids:
            default_category_id = ''
            self.settings.setValue('tag_library_new_tag_default_category_id', '')
        should_ask = self.settings.value(
            'ask_before_assigning_new_tag_category',
            defaultValue=DEFAULT_SETTINGS['ask_before_assigning_new_tag_category'],
            type=bool)
        if not should_ask:
            if default_category_id:
                self.tag_library_model.assign_category(tags,
                                                       default_category_id)
            else:
                self.tag_library_model.clear_category(tags)
            return
        show_category_assignment_prompt(
            prompt_parent or self, self.tag_library_model, tags,
            default_category_id=default_category_id)

    def add_wiki_tag_to_library(self, tag: str, prompt_parent=None):
        """Add a tag from a wiki dialog to the Tag Library (lowercased) and
        prompt for its category if it is new."""
        tag = tag.strip().lower()
        if not tag or self.tag_library_model.has_tag(tag):
            return
        self.tag_library_model.add_tags([tag])
        self.prompt_category_for_new_library_tags([tag], prompt_parent)

    def add_wiki_tag_to_selected_images(self, tag: str, prompt_parent=None):
        """Add a tag from a wiki dialog (lowercased) to every selected image,
        prompting for its category if it is new to the Tag Library."""
        tag = tag.strip().lower()
        if not tag:
            return
        selected_image_indices = self.image_list.get_selected_image_indices()
        if not selected_image_indices:
            QMessageBox.information(
                prompt_parent or self, 'No Images Selected',
                'Select one or more images in the Images pane first, then try '
                'again.')
            return
        is_new_library_tag = not self.tag_library_model.has_tag(tag)
        # Add the tag to the Tag Library first so the automatic tag-tracking
        # machinery does not also queue its own (main-window-parented) category
        # prompt; we show a dialog-parented prompt below instead.
        if is_new_library_tag:
            self.tag_library_model.add_tags([tag])
        self.image_list_model.add_tags([tag], selected_image_indices)
        self.image_tags_editor.clear_add_tag_box_if_matches(tag)
        self.image_tags_editor.select_last_tag_or_flash()
        if is_new_library_tag:
            self.prompt_category_for_new_library_tags([tag], prompt_parent)

    def wiki_selected_images_have_tag(self, tag: str):
        """Report whether the currently selected images already contain the
        given wiki tag, for the wiki dialog's "Add to Selected Images" button.

        Returns None when no images are selected, True when every selected
        image already has the tag, and False when at least one selected image
        is missing it. The comparison mirrors ``ImageListModel.add_tags``
        (exact match against the lowercased wiki tag), so a True result means
        the button would add nothing.
        """
        tag = tag.strip().lower()
        if not tag:
            return None
        selected_image_indices = self.image_list.get_selected_image_indices()
        if not selected_image_indices:
            return None
        for image_index in selected_image_indices:
            image: Image = self.image_list_model.data(
                image_index, Qt.ItemDataRole.UserRole)
            if tag not in image.tags:
                return False
        return True

    def add_wiki_tag_to_current_image(self, tag: str, prompt_parent=None):
        """Add a wiki tag (lowercased) to just the current image in the grouped
        (grid) view, prompting for its category if it is new to the Tag
        Library."""
        tag = tag.strip().lower()
        if not tag:
            return
        current_index = self.image_tags_editor.current_group_image_index()
        if current_index is None:
            QMessageBox.information(
                prompt_parent or self, 'No Current Image',
                'There is no current image to add the tag to.')
            return
        is_new_library_tag = not self.tag_library_model.has_tag(tag)
        # Add to the Tag Library first so the automatic tag-tracking machinery
        # does not also queue its own (main-window-parented) category prompt;
        # a dialog-parented prompt is shown below instead.
        if is_new_library_tag:
            self.tag_library_model.add_tags([tag])
        self.image_list_model.add_tags([tag], [current_index])
        self.image_tags_editor.clear_add_tag_box_if_matches(tag)
        self.image_tags_editor.select_last_tag_or_flash()
        if is_new_library_tag:
            self.prompt_category_for_new_library_tags([tag], prompt_parent)

    def wiki_current_image_has_tag(self, tag: str):
        """Report whether the current grouped-view image already has the wiki
        tag, for the wiki dialog's "Add to Current Image" button.

        Returns None when there is no current image, True when it already has
        the tag, and False otherwise.
        """
        tag = tag.strip().lower()
        if not tag:
            return None
        current_index = self.image_tags_editor.current_group_image_index()
        if current_index is None:
            return None
        image: Image = self.image_list_model.data(
            current_index, Qt.ItemDataRole.UserRole)
        return tag in image.tags

    def prompt_remove_tags_from_tag_library(self, tags: list[str]):
        tag_library_tags = [tag for tag in tags if self.tag_library_model.has_tag(tag)]
        if not tag_library_tags:
            return

        KEEP = 'Keep'
        REMOVE = 'Remove'
        default_action = self.settings.value(
            'tag_library_keep_or_remove_default_choice',
            defaultValue=DEFAULT_SETTINGS['tag_library_keep_or_remove_default_choice'],
            type=str)
        if default_action not in (KEEP, REMOVE):
            default_action = DEFAULT_SETTINGS['tag_library_keep_or_remove_default_choice']
        remove_by_default = (default_action == REMOVE)
        should_ask = self.settings.value(
            'tag_library_ask_keep_or_remove',
            defaultValue=DEFAULT_SETTINGS['tag_library_ask_keep_or_remove'],
            type=bool)

        if not should_ask:
            if remove_by_default:
                self.tag_library_model.remove_tags(tag_library_tags)
            return

        if len(tag_library_tags) == 1:
            tag = tag_library_tags[0]
            dialog = QDialog(self)
            dialog.setWindowTitle('Remove from Tag Library')
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel(
                f'The tag "{tag}" was fully removed from All Tags.\n\n'
                f'Also remove the tag "{tag}" from the Tag Library?'))
            combo_box = QComboBox(dialog)
            combo_box.addItems([KEEP, REMOVE])
            combo_box.setCurrentIndex(1 if remove_by_default else 0)
            confirm_button = QPushButton('Confirm', dialog)
            confirm_button.setDefault(True)
            confirm_button.setAutoDefault(True)
            confirm_button.clicked.connect(dialog.accept)
            layout.addWidget(combo_box)
            layout.addWidget(confirm_button)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                if combo_box.currentText() == REMOVE:
                    self.tag_library_model.remove_tags(tag_library_tags)
        else:
            dialog = QDialog(self)
            dialog.setWindowTitle('Remove from Tag Library')
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel(
                'These tags were fully removed from All Tags.\n\n'
                'Also remove these tags from the Tag Library?'))

            bulk_layout = QHBoxLayout()
            keep_all_button = QPushButton('Keep All', dialog)
            keep_all_button.setAutoDefault(False)
            remove_all_button = QPushButton('Remove All', dialog)
            remove_all_button.setAutoDefault(False)
            bulk_layout.addWidget(keep_all_button)
            bulk_layout.addWidget(remove_all_button)
            layout.addLayout(bulk_layout)

            scroll_area = QScrollArea(dialog)
            scroll_area.setWidgetResizable(True)
            scroll_area.setMinimumHeight(180)
            scroll_area.setMaximumHeight(320)
            table_container = QWidget(scroll_area)
            grid_layout = QGridLayout(table_container)
            grid_layout.setContentsMargins(6, 6, 6, 6)
            grid_layout.setHorizontalSpacing(12)
            grid_layout.setVerticalSpacing(8)

            combo_boxes = []
            for row, tag in enumerate(tag_library_tags):
                grid_layout.addWidget(QLabel(tag, table_container), row, 0)
                combo_box = QComboBox(table_container)
                combo_box.addItems([KEEP, REMOVE])
                combo_box.setCurrentIndex(1 if remove_by_default else 0)
                grid_layout.addWidget(combo_box, row, 1)
                combo_boxes.append(combo_box)

            def set_all(choice: str):
                for cb in combo_boxes:
                    cb.setCurrentText(choice)

            keep_all_button.clicked.connect(lambda: set_all(KEEP))
            remove_all_button.clicked.connect(lambda: set_all(REMOVE))

            scroll_area.setWidget(table_container)
            layout.addWidget(scroll_area)

            confirm_button = QPushButton('Confirm', dialog)
            confirm_button.setDefault(True)
            confirm_button.setAutoDefault(True)
            confirm_button.clicked.connect(dialog.accept)
            layout.addWidget(confirm_button)

            if dialog.exec() == QDialog.DialogCode.Accepted:
                tags_to_remove = [
                    tag for tag, cb in zip(tag_library_tags, combo_boxes)
                    if cb.currentText() == REMOVE
                ]
                if tags_to_remove:
                    self.tag_library_model.remove_tags(tags_to_remove)

    @Slot(list)
    def record_newly_added_library_tags(self, tags: list[str]):
        # Called when the Image Tags pane adds brand-new tags to the Tag
        # Library. They are buffered until the tag counter updates so that
        # track_all_tags_changes can queue the category-assignment prompt.
        if self.is_syncing_tag_library_from_directory:
            return
        self.newly_added_library_tags_pending.update(tags)

    @Slot()
    def track_all_tags_changes(self):
        current_all_tags = set(self.tag_counter_model.tag_counter.keys())
        if self.skip_next_all_tags_removal_prompt:
            self.tag_library_model.add_tags(
                sorted(current_all_tags, key=str.casefold))
            self.previous_all_tags = current_all_tags
            self.skip_next_all_tags_removal_prompt = False
            self.removed_all_tags_pending.clear()
            self.category_assignment_tags_pending.clear()
            self.newly_added_library_tags_pending.clear()
            return
        added_tags = current_all_tags - self.previous_all_tags
        removed_tags = self.previous_all_tags - current_all_tags
        self.previous_all_tags = current_all_tags
        if added_tags:
            sorted_added_tags = sorted(added_tags, key=str.casefold)
            tags_to_add_to_local = [
                tag for tag in sorted_added_tags
                if not self.tag_library_model.has_tag(tag)
            ]
            if tags_to_add_to_local:
                self.tag_library_model.add_tags(tags_to_add_to_local)
            # Only prompt for a category when a tag is genuinely new to the Tag
            # Library. That covers tags added to the library here
            # (tags_to_add_to_local) as well as tags the Image Tags pane already
            # inserted into the library before this ran (recorded in
            # newly_added_library_tags_pending). Tags that already existed in
            # the Tag Library are in neither set, so they never trigger the
            # prompt when later added to an image.
            new_library_tags = set(tags_to_add_to_local)
            if self.newly_added_library_tags_pending:
                new_library_tags.update(
                    tag for tag in sorted_added_tags
                    if tag in self.newly_added_library_tags_pending)
                self.newly_added_library_tags_pending.difference_update(
                    sorted_added_tags)
            if new_library_tags:
                tags_to_prompt_for_category = [
                    tag for tag in sorted_added_tags
                    if (tag in new_library_tags
                        and self.tag_library_model.get_category_for_tag(tag)
                        is None)
                ]
                if tags_to_prompt_for_category:
                    self.category_assignment_tags_pending.update(
                        tags_to_prompt_for_category)
        if removed_tags:
            self.removed_all_tags_pending.update(removed_tags)
        self.schedule_tag_change_prompt_dispatch()

    def schedule_tag_change_prompt_dispatch(self):
        if self.tag_change_prompt_dispatch_scheduled:
            return
        if (not self.removed_all_tags_pending
                and not self.category_assignment_tags_pending):
            return
        self.tag_change_prompt_dispatch_scheduled = True
        QTimer.singleShot(0, self.process_pending_tag_change_prompts)

    @Slot()
    def process_pending_tag_change_prompts(self):
        self.tag_change_prompt_dispatch_scheduled = False
        if self.removed_all_tags_pending:
            removed_tags = sorted(self.removed_all_tags_pending,
                                  key=str.casefold)
            self.removed_all_tags_pending.clear()
            self.prompt_remove_tags_from_tag_library(removed_tags)
        if self.category_assignment_tags_pending:
            tags_for_category_prompt = sorted(
                self.category_assignment_tags_pending, key=str.casefold)
            self.category_assignment_tags_pending.clear()
            self.queue_category_assignment_for_new_tags(tags_for_category_prompt)

    @Slot(list)
    def handle_all_tags_deletion(self, tags: list[str]):
        self.image_list_model.delete_tags(tags)
        self.image_list.filter_line_edit.clear()

    @Slot(list, str)
    def handle_all_tags_rename(self, old_tags: list[str], new_tag: str):
        self.image_list_model.rename_tags(old_tags, new_tag)
        self.image_list.filter_line_edit.clear()

    def connect_all_tags_editor_signals(self):
        self.all_tags_editor.clear_filter_button.clicked.connect(
            self.image_list.filter_line_edit.clear)
        self.tag_counter_model.tags_renaming_requested.connect(
            self.handle_all_tags_rename)
        self.all_tags_editor.danbooru_wiki_requested.connect(
            self.show_danbooru_wiki_dialog)
        self.all_tags_editor.gelbooru_wiki_requested.connect(
            self.show_gelbooru_wiki_dialog)
        self.all_tags_editor.all_tags_list.image_list_filter_requested.connect(
            self.set_image_list_filter_text)
        self.all_tags_editor.all_tags_list.tag_addition_requested.connect(
            self.add_tag_to_selected_images)
        self.all_tags_editor.all_tags_list.tags_deletion_requested.connect(
            self.handle_all_tags_deletion)
        self.all_tags_editor.visibilityChanged.connect(
            lambda: self.toggle_all_tags_editor_action.setChecked(
                self.all_tags_editor.isVisible()))

    def connect_auto_captioner_signals(self):
        self.auto_captioner.captioning_started.connect(
            self.clear_pending_auto_caption_category_tags)
        self.auto_captioner.captioning_finished.connect(
            self.schedule_pending_auto_caption_category_prompt)
        self.auto_captioner.caption_generated.connect(
            self.handle_auto_caption_generated)
        self.auto_captioner.caption_generated.connect(
            lambda image_index, *_:
            self.image_tags_editor.reload_image_tags_if_changed(image_index,
                                                                image_index))
        self.auto_captioner.caption_generated.connect(
            self.sync_image_tags_mode_after_auto_caption)
        self.auto_captioner.visibilityChanged.connect(
            lambda: self.toggle_auto_captioner_action.setChecked(
                self.auto_captioner.isVisible()))

    @Slot(QModelIndex, str, list, str)
    def handle_auto_caption_generated(self, image_index: QModelIndex,
                                      _caption: str, tags: list[str],
                                      natural_language_prompt: str):
        self.image_list_model.update_image_caption(image_index, tags,
                                                   natural_language_prompt)

    @Slot()
    def clear_pending_auto_caption_category_tags(self):
        self.pending_auto_caption_category_tags.clear()
        self.is_collecting_auto_caption_category_tags = True

    def queue_category_assignment_for_new_tags(self, tags: list[str]):
        if not tags or self.is_syncing_tag_library_from_directory:
            return
        default_category_id = self.settings.value(
            'tag_library_new_tag_default_category_id',
            defaultValue=DEFAULT_SETTINGS['tag_library_new_tag_default_category_id'],
            type=str).strip()
        category_ids = {
            category['id'] for category in self.tag_library_model.get_categories()
        }
        if default_category_id and default_category_id not in category_ids:
            default_category_id = ''
            self.settings.setValue('tag_library_new_tag_default_category_id', '')
        should_ask = self.settings.value(
            'ask_before_assigning_new_tag_category',
            defaultValue=DEFAULT_SETTINGS['ask_before_assigning_new_tag_category'],
            type=bool)
        if not should_ask:
            if default_category_id:
                self.tag_library_model.assign_category(tags, default_category_id)
            else:
                self.tag_library_model.clear_category(tags)
            return
        if self.auto_captioner.is_captioning or (
                self.is_collecting_auto_caption_category_tags):
            for tag in tags:
                if tag not in self.pending_auto_caption_category_tags:
                    self.pending_auto_caption_category_tags.append(tag)
            return
        QTimer.singleShot(
            0,
            lambda prompt_tags=tags.copy(): show_category_assignment_prompt(
                self, self.tag_library_model, prompt_tags,
                default_category_id=default_category_id))

    @Slot()
    def prompt_for_pending_auto_caption_category_tags(self):
        # Drain any tag-change dispatch still queued from the final image so its
        # new tags join this single batch instead of triggering a second prompt.
        # is_collecting_auto_caption_category_tags is still True here, so these
        # tags are appended to pending_auto_caption_category_tags rather than
        # prompted immediately.
        if self.tag_change_prompt_dispatch_scheduled:
            self.process_pending_tag_change_prompts()
        self.is_collecting_auto_caption_category_tags = False
        if not self.pending_auto_caption_category_tags:
            return
        default_category_id = self.settings.value(
            'tag_library_new_tag_default_category_id',
            defaultValue=DEFAULT_SETTINGS['tag_library_new_tag_default_category_id'],
            type=str).strip()
        category_ids = {
            category['id'] for category in self.tag_library_model.get_categories()
        }
        if default_category_id and default_category_id not in category_ids:
            default_category_id = ''
            self.settings.setValue('tag_library_new_tag_default_category_id', '')
        should_ask = self.settings.value(
            'ask_before_assigning_new_tag_category',
            defaultValue=DEFAULT_SETTINGS['ask_before_assigning_new_tag_category'],
            type=bool)
        pending_tags = self.pending_auto_caption_category_tags.copy()
        if should_ask:
            show_category_assignment_prompt(
                self, self.tag_library_model, pending_tags,
                default_category_id=default_category_id)
        elif default_category_id:
            self.tag_library_model.assign_category(
                pending_tags, default_category_id)
        else:
            self.tag_library_model.clear_category(pending_tags)
        self.pending_auto_caption_category_tags.clear()

    @Slot()
    def schedule_pending_auto_caption_category_prompt(self):
        QTimer.singleShot(0, self.prompt_for_pending_auto_caption_category_tags)

    @Slot(QModelIndex, str, list, str)
    def sync_image_tags_mode_after_auto_caption(self, image_index: QModelIndex,
                                                _caption: str, _tags: list,
                                                _natural_language_prompt: str):
        if self.image_tags_editor.image_index is None:
            return
        if self.image_tags_editor.image_index != image_index:
            return
        should_use_natural_language_mode = (
            self.auto_captioner.current_caption_destination
            == CaptionDestination.NATURAL_LANGUAGE)
        if (self.image_tags_editor.natural_language_mode_check_box.isChecked()
                != should_use_natural_language_mode):
            self.image_tags_editor.natural_language_mode_check_box.setChecked(
                should_use_natural_language_mode)
        else:
            self.image_tags_editor.set_natural_language_mode()

    def restore(self):
        # Restore the window geometry and state.
        if self.settings.contains('geometry'):
            geometry_data = self.settings.value('geometry', type=bytes)
            self.restoreGeometry(geometry_data)
        else:
            self.setWindowState(Qt.WindowState.WindowMaximized)
        
        # Suppress floating dock windows before restoreState().
        # A floating QDockWidget is an independent top-level window that
        # restoreState() makes visible immediately - even while the main window
        # is still hidden - so it would flash on screen during startup.
        # Setting WA_DontShowOnScreen keeps it off-screen while still letting
        # restoreState() restore its floating geometry (verified: the floating
        # layout is preserved).  _show_window() clears the flag and shows the
        # docks once the app is fully ready.
        for dock in self.get_dock_widgets():
            dock.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)

        # Restore window state (dock visibility, layout, etc.)
        window_state_data = self.settings.value('window_state', type=bytes)
        if window_state_data:
            self.restoreState(window_state_data)
        
        # Get the last index of the last selected image. When a filter was
        # active on exit, the saved position refers to the filtered list, so
        # use that index and reapply the filter below.
        saved_filter = self.settings.value(
            'image_list_filter', defaultValue='', type=str)
        if saved_filter:
            image_index = self.settings.value(
                'filtered_image_index', type=int) or 0
        elif self.settings.contains('image_index'):
            image_index = self.settings.value('image_index', type=int)
        else:
            image_index = 0
        # Load the last loaded directory.
        if self.settings.contains('directory_path'):
            directory_path = Path(self.settings.value('directory_path',
                                                      type=str))
            if directory_path.is_dir():
                self.load_directory(directory_path, select_index=image_index,
                                    restore_filter=saved_filter)
                return
        # No valid saved directory - show immediately (nothing to wait for).
        QTimer.singleShot(0, self._show_window)
