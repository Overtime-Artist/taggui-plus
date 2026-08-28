import sys

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QGridLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QScrollArea,
                               QVBoxLayout, QHBoxLayout, QWidget, QFrame,
                               QCheckBox)

from models.tag_library_model import TagLibraryModel
from auto_captioning.models_list import MODELS
from dialogs.remove_app_data_dialog import RemoveAppDataDialog
from utils.enums import ThemeMode
from utils.settings import (DEFAULT_SETTINGS, REORDER_OPTIONS, get_settings,
                            get_hidden_model_ids, save_hidden_model_ids)
from utils.settings_widgets import (SettingsBigCheckBox, SettingsComboBox,
                                    SettingsLineEdit, SettingsSpinBox,
                                    FocusedScrollSettingsComboBox,
                                    FocusedScrollSettingsSpinBox)
from utils.thumbnail_cache import clear_cache, get_cache_size_bytes


class IsolatedScrollArea(QScrollArea):
    """A scroll area that doesn't propagate wheel events to parent widgets."""
    
    def wheelEvent(self, event):
        """Handle wheel events without propagating to parent."""
        # Only scroll if the scroll area can actually scroll
        if self.verticalScrollBar().isVisible():
            super().wheelEvent(event)
            event.accept()
        else:
            event.ignore()


class SettingsSection(QFrame):
    """A modern card-style settings section with title and description."""
    
    def __init__(self, title: str, description: str = '', parent=None):
        super().__init__(parent)
        self.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Raised)
        self.setLineWidth(0)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 15, 0, 15)  # 0 left/right to eliminate extra space
        layout.setSpacing(12)
        
        # Title wrapper to center between grid columns
        title_wrapper = QHBoxLayout()
        title_wrapper.setContentsMargins(0, 0, 0, 0)
        title_wrapper.addStretch()
        
        title_label = QLabel(title)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_font = QFont()
        title_font.setPointSize(11)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_wrapper.addWidget(title_label)
        
        title_wrapper.addStretch()
        layout.addLayout(title_wrapper)
        
        # Description (optional)
        if description:
            desc_label = QLabel(description)
            desc_font = QFont()
            desc_font.setPointSize(9)
            desc_label.setFont(desc_font)
            desc_label.setStyleSheet('color: #999999;')
            layout.addWidget(desc_label)
        
        # Grid for settings (to be populated by parent)
        self.grid_layout = QGridLayout()
        self.grid_layout.setSpacing(10)
        self.grid_layout.setContentsMargins(0, 0, 15, 0)  # Right padding so content doesn't touch edge
        # Column 0: labels (right-aligned), Column 1: values (left-aligned)
        # Minimum width will be set by parent dialog after all sections are created
        self.grid_layout.setColumnStretch(0, 0)
        self.grid_layout.setColumnStretch(1, 0)
        
        # Center the grid horizontally with equal margins on both sides
        grid_wrapper = QHBoxLayout()
        grid_wrapper.setContentsMargins(0, 0, 0, 0)
        grid_wrapper.setSpacing(0)
        grid_wrapper.addStretch()
        grid_wrapper.addLayout(self.grid_layout)
        grid_wrapper.addStretch()
        layout.addLayout(grid_wrapper)
        
        layout.addStretch()  # Push all content to top
    
    def set_dark_mode(self, is_dark: bool):
        """Update colors for dark/light theme."""
        if is_dark:
            self.setStyleSheet(
                'SettingsSection { '
                'background-color: #2b2b2b; '
                'border-radius: 8px; '
                'border: 1px solid #3d3d3d; '
                'margin: 0px; '
                'padding: 0px; '
                '}'
            )
        else:
            self.setStyleSheet(
                'SettingsSection { '
                'background-color: #fafafa; '
                'border-radius: 8px; '
                'border: 1px solid #d0d0d0; '
                'margin: 0px; '
                'padding: 0px; '
                '}'
            )


class SettingsDialog(QDialog):
    theme_changed = Signal()
    keyboard_shortcuts_requested = Signal()
    image_list_font_size_changed = Signal()
    image_list_image_width_changed = Signal()
    image_list_resolution_badge_settings_changed = Signal()
    variant_grid_overlay_settings_changed = Signal()
    image_list_completion_icon_settings_changed = Signal()
    token_limit_changed = Signal()
    tag_separator_changed = Signal()
    autocomplete_changed = Signal()
    remove_app_data_requested = Signal()

    def __init__(self, parent, tag_library_model: TagLibraryModel,
                 dialog_font: QFont | None = None):
        super().__init__(parent)
        # Set window flags early before native handle is created to avoid close button issues
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.WindowTitleHint |
            Qt.WindowType.WindowCloseButtonHint |
            Qt.WindowType.WindowSystemMenuHint
        )
        self.settings = get_settings()
        self.tag_library_model = tag_library_model
        if dialog_font is not None:
            self.setFont(dialog_font)
        self.setWindowTitle('Settings')
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        # Only allow vertical scrolling
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        main_layout.addWidget(scroll_area)
        
        # Floating warning bar at the bottom - always visible regardless of scroll position
        self.restart_warning = 'Restart the application to apply the new settings.'
        self.warning_label = QLabel(self.restart_warning)
        self.warning_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.warning_label.setWordWrap(True)
        self.warning_label.setContentsMargins(12, 8, 12, 8)
        main_layout.addWidget(self.warning_label)
        self.warning_label.hide()
        self.content_widget = QWidget(scroll_area)
        scroll_area.setWidget(self.content_widget)
        layout = QVBoxLayout(self.content_widget)
        layout.setContentsMargins(20, 20, 20, 20)  # Add back the horizontal padding for spacing
        layout.setSpacing(20)  # More spacing between card sections
        
        # Get current theme to apply to sections
        theme = self.settings.value('theme', defaultValue=DEFAULT_SETTINGS['theme'], type=str)
        is_dark = theme == ThemeMode.DARK
        
        # Store sections for theme updates
        self.sections = []

        # Helper to create modern card-style sections
        def add_section(title: str, description: str = ''):
            """Add a modern card-style section and return its grid layout."""
            section = SettingsSection(title, description)
            section.set_dark_mode(is_dark)
            self.sections.append(section)
            layout.addWidget(section)
            return section.grid_layout
        
        # Helper to create a restart indicator label - REMOVED, using floating warning bar instead

        # Global Settings
        grid_layout = add_section('Global Settings')
        row = 0

        font_size_spin_box = FocusedScrollSettingsSpinBox(
            key='font_size', default=DEFAULT_SETTINGS['font_size'],
            minimum=1, maximum=99)
        font_size_spin_box.valueChanged.connect(
            lambda _: self.show_restart_warning())
        theme_combo_box = FocusedScrollSettingsComboBox(
            key='theme',
            default=DEFAULT_SETTINGS['theme'])
        theme_combo_box.addItems(list(ThemeMode))
        theme_combo_box.setSizeAdjustPolicy(
            SettingsComboBox.SizeAdjustPolicy.AdjustToContents)
        theme_combo_box.currentTextChanged.connect(
            lambda _: self.theme_changed.emit())
        theme_combo_box.currentTextChanged.connect(
            lambda _: QTimer.singleShot(0, self.refresh_theme_sensitive_controls))
        theme_combo_box.currentTextChanged.connect(
            lambda _: QTimer.singleShot(120, self.refresh_theme_sensitive_controls))
        
        # Global Settings grid
        grid_layout.addWidget(QLabel('Font size (pt)'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(font_size_spin_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1
        grid_layout.addWidget(QLabel('Theme'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(theme_combo_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1
        
        self.keyboard_shortcuts_button = QPushButton('Configure...')
        self.keyboard_shortcuts_button.setAutoDefault(False)
        self.keyboard_shortcuts_button.setFixedWidth(
            int(self.keyboard_shortcuts_button.sizeHint().width() * 1.3))
        self.keyboard_shortcuts_button.clicked.connect(
            self.keyboard_shortcuts_requested.emit)
        grid_layout.addWidget(QLabel('Keyboard shortcuts'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(self.keyboard_shortcuts_button, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1

        # Images Panel
        grid_layout = add_section('Images Panel')
        row = 0
        
        image_list_font_size_spin_box = FocusedScrollSettingsSpinBox(
            key='image_list_font_size',
            default=DEFAULT_SETTINGS['image_list_font_size'],
            minimum=1, maximum=99)
        image_list_font_size_spin_box.valueChanged.connect(
            lambda _: self.image_list_font_size_changed.emit())
        file_types_line_edit = SettingsLineEdit(
            key='image_list_file_formats',
            default=DEFAULT_SETTINGS['image_list_file_formats'])
        # Applied without a restart: the main window reloads the current
        # directory on dialog close if this value changed (see
        # MainWindow.show_settings_dialog).
        # Images that are too small cause lag, so set a minimum width.
        image_list_image_width_spin_box = FocusedScrollSettingsSpinBox(
            key='image_list_image_width',
            default=DEFAULT_SETTINGS['image_list_image_width'],
            minimum=16, maximum=9999)
        image_list_image_width_spin_box.valueChanged.connect(
            lambda _: self.image_list_image_width_changed.emit())
        resolution_badge_font_size_spin_box = FocusedScrollSettingsSpinBox(
            key='image_list_resolution_badge_font_size',
            default=DEFAULT_SETTINGS['image_list_resolution_badge_font_size'],
            minimum=1, maximum=99)
        resolution_badge_font_size_spin_box.valueChanged.connect(
            lambda _: self.image_list_resolution_badge_settings_changed.emit())
        resolution_badge_transparency_spin_box = FocusedScrollSettingsSpinBox(
            key='image_list_resolution_badge_transparency',
            default=DEFAULT_SETTINGS['image_list_resolution_badge_transparency'],
            minimum=0, maximum=100)
        resolution_badge_transparency_spin_box.valueChanged.connect(
            lambda _: self.image_list_resolution_badge_settings_changed.emit())
        show_resolution_badge_check_box = SettingsBigCheckBox(
            key='image_list_show_resolution_badge',
            default=DEFAULT_SETTINGS['image_list_show_resolution_badge'])
        show_completion_icon_check_box = SettingsBigCheckBox(
            key='image_list_show_completion_icon',
            default=DEFAULT_SETTINGS['image_list_show_completion_icon'])
        show_completion_icon_check_box.stateChanged.connect(
            lambda _: self.image_list_completion_icon_settings_changed.emit())
        resolution_badge_font_size_label = QLabel('Resolution badge text size (pt)')
        resolution_badge_transparency_label = QLabel(
            'Resolution badge background transparency (%)')

        def update_badge_sub_settings_enabled(checked: bool):
            # Grey out the badge appearance controls when the badge is hidden.
            resolution_badge_font_size_spin_box.setEnabled(checked)
            resolution_badge_transparency_spin_box.setEnabled(checked)
            resolution_badge_font_size_label.setEnabled(checked)
            resolution_badge_transparency_label.setEnabled(checked)

        update_badge_sub_settings_enabled(
            show_resolution_badge_check_box.isChecked())
        show_resolution_badge_check_box.stateChanged.connect(
            lambda _: (
                update_badge_sub_settings_enabled(
                    show_resolution_badge_check_box.isChecked()),
                self.image_list_resolution_badge_settings_changed.emit()))
        
        grid_layout.addWidget(QLabel('Images list text size (pt)'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(image_list_font_size_spin_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1
        grid_layout.addWidget(QLabel('File types to show in image list'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(file_types_line_edit, row, 1)
        row += 1
        grid_layout.addWidget(QLabel('Image width in image list (px)'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(image_list_image_width_spin_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1
        grid_layout.addWidget(QLabel('Show resolution badge'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(show_resolution_badge_check_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1
        grid_layout.addWidget(QLabel('Show completion check icon'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(show_completion_icon_check_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1
        grid_layout.addWidget(resolution_badge_font_size_label, row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(resolution_badge_font_size_spin_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1
        grid_layout.addWidget(resolution_badge_transparency_label, row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(resolution_badge_transparency_spin_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1
        max_image_zoom_spin_box = FocusedScrollSettingsSpinBox(
            key='max_image_preview_zoom',
            default=DEFAULT_SETTINGS['max_image_preview_zoom'],
            minimum=1, maximum=100)
        grid_layout.addWidget(QLabel('Max image preview zoom (×)'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(max_image_zoom_spin_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1
        type_to_add_tag_check_box = SettingsBigCheckBox(
            key='image_list_auto_focus_add_tag_box',
            default=DEFAULT_SETTINGS['image_list_auto_focus_add_tag_box'])
        type_to_add_tag_label = QLabel(
            'Auto-focus Add Tag box when typing in Images pane')
        grid_layout.addWidget(type_to_add_tag_label, row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(type_to_add_tag_check_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1
        variant_grid_view_check_box = SettingsBigCheckBox(
            key='variant_grid_view_enabled',
            default=DEFAULT_SETTINGS['variant_grid_view_enabled'])
        variant_grid_view_label = QLabel(
            'Group tags & show grid when multiple images are selected')
        grid_layout.addWidget(variant_grid_view_label, row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(variant_grid_view_check_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1
        variant_grid_cell_cap_spin_box = FocusedScrollSettingsSpinBox(
            key='variant_grid_cell_cap',
            default=DEFAULT_SETTINGS['variant_grid_cell_cap'],
            minimum=4, maximum=64)
        grid_layout.addWidget(QLabel('Max grid cells for multi-image preview'),
                              row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(variant_grid_cell_cap_spin_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1
        grid_overlay_font_size_spin_box = FocusedScrollSettingsSpinBox(
            key='variant_grid_overlay_font_size',
            default=DEFAULT_SETTINGS['variant_grid_overlay_font_size'],
            minimum=1, maximum=99)
        grid_overlay_font_size_spin_box.valueChanged.connect(
            lambda _: self.variant_grid_overlay_settings_changed.emit())
        grid_overlay_transparency_spin_box = FocusedScrollSettingsSpinBox(
            key='variant_grid_overlay_transparency',
            default=DEFAULT_SETTINGS['variant_grid_overlay_transparency'],
            minimum=0, maximum=100)
        grid_overlay_transparency_spin_box.valueChanged.connect(
            lambda _: self.variant_grid_overlay_settings_changed.emit())
        show_grid_overlay_check_box = SettingsBigCheckBox(
            key='variant_grid_overlay_show',
            default=DEFAULT_SETTINGS['variant_grid_overlay_show'])
        grid_overlay_font_size_label = QLabel(
            'Grid page/count overlay text size (pt)')
        grid_overlay_transparency_label = QLabel(
            'Grid page/count overlay background transparency (%)')

        def update_grid_overlay_sub_settings_enabled(checked: bool):
            # Grey out the overlay appearance controls when the overlay is off.
            grid_overlay_font_size_spin_box.setEnabled(checked)
            grid_overlay_transparency_spin_box.setEnabled(checked)
            grid_overlay_font_size_label.setEnabled(checked)
            grid_overlay_transparency_label.setEnabled(checked)

        update_grid_overlay_sub_settings_enabled(
            show_grid_overlay_check_box.isChecked())
        show_grid_overlay_check_box.stateChanged.connect(
            lambda _: (
                update_grid_overlay_sub_settings_enabled(
                    show_grid_overlay_check_box.isChecked()),
                self.variant_grid_overlay_settings_changed.emit()))

        grid_layout.addWidget(QLabel('Show grid page/count overlay'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(show_grid_overlay_check_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1
        grid_layout.addWidget(grid_overlay_font_size_label, row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(grid_overlay_font_size_spin_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1
        grid_layout.addWidget(grid_overlay_transparency_label, row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(grid_overlay_transparency_spin_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        grid_layout = add_section('Thumbnail Cache')
        row = 0

        thumbnail_cache_max_size_spin_box = FocusedScrollSettingsSpinBox(
            key='thumbnail_cache_max_size_mb',
            default=DEFAULT_SETTINGS['thumbnail_cache_max_size_mb'],
            minimum=50, maximum=100000)
        grid_layout.addWidget(QLabel('Max cache size (MB)'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(thumbnail_cache_max_size_spin_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1

        background_thumbnail_caching_check_box = SettingsBigCheckBox(
            key='background_thumbnail_caching',
            default=DEFAULT_SETTINGS['background_thumbnail_caching'])
        grid_layout.addWidget(
            QLabel('Cache thumbnails in the background'), row, 0,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(background_thumbnail_caching_check_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1

        cache_size_mb = get_cache_size_bytes() / (1024 * 1024)
        self._cache_size_label = QLabel(f'{cache_size_mb:.1f} MB')
        grid_layout.addWidget(QLabel('Current cache size'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(self._cache_size_label, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1

        clear_cache_button = QPushButton('Clear Thumbnail Cache')
        clear_cache_button.setAutoDefault(False)
        clear_cache_button.setFixedWidth(
            int(clear_cache_button.sizeHint().width() * 1.3))
        clear_cache_button.clicked.connect(self._clear_thumbnail_cache)
        grid_layout.addWidget(QLabel(''), row, 0)
        grid_layout.addWidget(clear_cache_button, row, 1,
                              Qt.AlignmentFlag.AlignLeft)

        # Image Tags Panel
        grid_layout = add_section('Image Tags Panel')
        row = 0
        
        self.insert_space_after_tag_separator_check_box = SettingsBigCheckBox(
            key='insert_space_after_tag_separator',
            default=DEFAULT_SETTINGS['insert_space_after_tag_separator'])
        self.insert_space_after_tag_separator_check_box.stateChanged.connect(
            lambda _: self.tag_separator_changed.emit())
        tag_separator_line_edit = QLineEdit()
        tag_separator = self.settings.value(
            'tag_separator', defaultValue=DEFAULT_SETTINGS['tag_separator'],
            type=str)
        if tag_separator == '\n':
            tag_separator = DEFAULT_SETTINGS['tag_separator']
            self.settings.setValue('tag_separator', tag_separator)
        tag_separator_line_edit.setMaximumWidth(50)
        tag_separator_line_edit.setText(tag_separator)
        tag_separator_line_edit.textChanged.connect(
            self.handle_tag_separator_change)
        self.autocomplete_tags_check_box = SettingsBigCheckBox(
            key='autocomplete_tags',
            default=DEFAULT_SETTINGS['autocomplete_tags'])
        self.autocomplete_tags_check_box.stateChanged.connect(
            lambda _: self.autocomplete_changed.emit())
        self.disable_new_tag_auto_select_check_box = SettingsBigCheckBox(
            key='disable_new_tag_auto_select',
            default=DEFAULT_SETTINGS['disable_new_tag_auto_select'])
        
        grid_layout.addWidget(QLabel('Tag separator'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(tag_separator_line_edit, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1
        grid_layout.addWidget(QLabel('Insert space after tag separator'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(self.insert_space_after_tag_separator_check_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1
        grid_layout.addWidget(QLabel('Show tag autocomplete suggestions'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(self.autocomplete_tags_check_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1
        grid_layout.addWidget(
            QLabel('Do not auto-select newly added tags'), row, 0,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(self.disable_new_tag_auto_select_check_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1
        
        token_limit_spin_box = FocusedScrollSettingsSpinBox(
            key='image_tags_token_limit',
            default=DEFAULT_SETTINGS['image_tags_token_limit'],
            minimum=1, maximum=999999)
        token_limit_spin_box.valueChanged.connect(
            lambda _: self.token_limit_changed.emit())
        grid_layout.addWidget(QLabel('Token limit'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(token_limit_spin_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)
        row += 1

        default_batch_reorder_combo_box = FocusedScrollSettingsComboBox(
            key='default_batch_reorder',
            default=DEFAULT_SETTINGS['default_batch_reorder'])
        default_batch_reorder_combo_box.addItems(REORDER_OPTIONS)
        default_batch_reorder_combo_box.setSizeAdjustPolicy(
            SettingsComboBox.SizeAdjustPolicy.AdjustToContents)
        grid_layout.addWidget(QLabel('Default batch reorder'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(default_batch_reorder_combo_box, row, 1,
                              Qt.AlignmentFlag.AlignLeft)

        # Auto-Captioner Panel
        grid_layout = add_section('Auto-Captioner Panel')
        row = 0
        
        self.models_directory_line_edit = SettingsLineEdit(
            key='models_directory_path',
            default=DEFAULT_SETTINGS['models_directory_path'])
        self.models_directory_line_edit.setClearButtonEnabled(True)
        # Applied without a restart: the main window repopulates the
        # auto-captioner model dropdown on dialog close if this changed.
        self.models_directory_button = QPushButton('Select Directory...')
        self.models_directory_button.setAutoDefault(False)
        self.models_directory_button.setFixedWidth(
            int(self.models_directory_button.sizeHint().width() * 1.3))
        self.models_directory_button.clicked.connect(self.set_models_directory_path)
        
        models_dir_layout = QVBoxLayout()
        models_dir_layout.addWidget(self.models_directory_line_edit)
        models_dir_layout.addWidget(self.models_directory_button)
        grid_layout.addWidget(QLabel('Models directory'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addLayout(models_dir_layout, row, 1)
        row += 1
        
        # Model Visibility - scrollable area on same row as label
        grid_layout.addWidget(QLabel('Model visibility'), row, 0,
                              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        
        # Scrollable area for model checkboxes (using IsolatedScrollArea to prevent scroll conflicts)
        model_scroll_area = IsolatedScrollArea()
        model_scroll_area.setWidgetResizable(True)
        model_scroll_area.setMaximumHeight(350)
        model_scroll_area.setMinimumHeight(350)
        model_scroll_area.setStyleSheet('QScrollArea { border: none; }')
        
        # Content widget for scroll area
        model_scroll_content = QWidget()
        model_scroll_content_layout = QVBoxLayout(model_scroll_content)
        model_scroll_content_layout.setSpacing(6)
        model_scroll_content_layout.setContentsMargins(0, 0, 0, 0)
        
        # Create checkboxes for each model (single column)
        self.model_visibility_checkboxes = {}
        hidden_models = get_hidden_model_ids()
        
        for model_id in MODELS:
            checkbox = QCheckBox(model_id)
            is_visible = model_id not in hidden_models
            checkbox.setChecked(is_visible)
            checkbox.stateChanged.connect(
                lambda checked, mid=model_id: self.update_model_visibility(mid, checked))
            self.model_visibility_checkboxes[model_id] = checkbox
            model_scroll_content_layout.addWidget(checkbox)
        
        model_scroll_content_layout.addStretch()
        model_scroll_area.setWidget(model_scroll_content)
        # Set width to fit the content (not too wide)
        model_scroll_area.setMaximumWidth(400)
        grid_layout.addWidget(model_scroll_area, row, 1, Qt.AlignmentFlag.AlignLeft)
        row += 1
        
        # Select All / Deselect All buttons - below the scrollable area
        select_all_button = QPushButton('Select All')
        select_all_button.setAutoDefault(False)
        deselect_all_button = QPushButton('Deselect All')
        deselect_all_button.setAutoDefault(False)
        select_all_button.clicked.connect(self.select_all_models)
        deselect_all_button.clicked.connect(self.deselect_all_models)
        
        model_buttons_layout = QHBoxLayout()
        model_buttons_layout.addWidget(select_all_button)
        model_buttons_layout.addWidget(deselect_all_button)
        model_buttons_layout.addStretch()
        grid_layout.addLayout(model_buttons_layout, row, 1)

        # Tag Library
        grid_layout = add_section('Tag Library')
        row = 0
        
        self.remove_tag_library_tags_default_action_combo_box = FocusedScrollSettingsComboBox(
            key='tag_library_keep_or_remove_default_choice',
            default=DEFAULT_SETTINGS['tag_library_keep_or_remove_default_choice'])
        self.remove_tag_library_tags_default_action_combo_box.addItems(
            ['Keep', 'Remove'])
        self.remove_tag_library_tags_default_action_combo_box.setSizeAdjustPolicy(
            SettingsComboBox.SizeAdjustPolicy.AdjustToContents)
        self.ask_before_removing_tag_library_tags_check_box = SettingsBigCheckBox(
            key='tag_library_ask_keep_or_remove',
            default=DEFAULT_SETTINGS['tag_library_ask_keep_or_remove'])
        self.default_new_tag_library_category_combo_box = FocusedScrollSettingsComboBox(
            key='tag_library_new_tag_default_category_id',
            default=DEFAULT_SETTINGS['tag_library_new_tag_default_category_id'])
        self.default_new_tag_library_category_combo_box.addItem('No category', '')
        categories = self.tag_library_model.get_categories()
        for category in categories:
            self.default_new_tag_library_category_combo_box.addItem(
                category['name'], category['id'])
        selected_category_id = self.settings.value(
            'tag_library_new_tag_default_category_id',
            defaultValue=DEFAULT_SETTINGS['tag_library_new_tag_default_category_id'],
            type=str)
        selected_index = self.default_new_tag_library_category_combo_box.findData(
            selected_category_id)
        if selected_index < 0:
            selected_index = 0
            self.settings.setValue('tag_library_new_tag_default_category_id', '')
        self.default_new_tag_library_category_combo_box.setCurrentIndex(
            selected_index)
        self.default_new_tag_library_category_combo_box.currentIndexChanged.connect(
            lambda _: self.settings.setValue(
                'tag_library_new_tag_default_category_id',
                self.default_new_tag_library_category_combo_box.currentData()))
        self.default_new_tag_library_category_combo_box.setSizeAdjustPolicy(
            SettingsComboBox.SizeAdjustPolicy.AdjustToContents)
        self.ask_before_assigning_new_tag_category_check_box = SettingsBigCheckBox(
            key='ask_before_assigning_new_tag_category',
            default=DEFAULT_SETTINGS['ask_before_assigning_new_tag_category'])
        self.auto_apply_implications_combo_box = FocusedScrollSettingsComboBox(
            key='auto_apply_implications',
            default=DEFAULT_SETTINGS['auto_apply_implications'])
        self.auto_apply_implications_combo_box.addItems(
            ['Off', 'Single image only', 'All selected images'])
        self.auto_apply_implications_combo_box.setSizeAdjustPolicy(
            SettingsComboBox.SizeAdjustPolicy.AdjustToContents)
        
        grid_layout.addWidget(QLabel('Default choice: keep or remove from Tag Library'), row, 0,
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(self.remove_tag_library_tags_default_action_combo_box, row, 1,
                             Qt.AlignmentFlag.AlignLeft)
        row += 1
        grid_layout.addWidget(QLabel('Ask whether to keep or remove from Tag Library'), row, 0,
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(self.ask_before_removing_tag_library_tags_check_box, row, 1,
                             Qt.AlignmentFlag.AlignLeft)
        row += 1
        grid_layout.addWidget(QLabel('Default category for new tags'), row, 0,
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(self.default_new_tag_library_category_combo_box, row, 1,
                             Qt.AlignmentFlag.AlignLeft)
        row += 1
        grid_layout.addWidget(QLabel('Ask before assigning category to new tags'), row, 0,
                            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(self.ask_before_assigning_new_tag_category_check_box, row, 1,
                            Qt.AlignmentFlag.AlignLeft)
        row += 1
        grid_layout.addWidget(QLabel('Auto-apply implications when adding tags'), row, 0,
                            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addWidget(self.auto_apply_implications_combo_box, row, 1,
                            Qt.AlignmentFlag.AlignLeft)

        # External Tools
        grid_layout = add_section('External Tools')
        row = 0
        
        self.image_editor_executable_line_edit = SettingsLineEdit(
           key='image_editor_executable_path',
           default=DEFAULT_SETTINGS['image_editor_executable_path'])
        self.image_editor_executable_line_edit.setClearButtonEnabled(True)
        self.image_editor_executable_button = QPushButton('Select Executable...')
        self.image_editor_executable_button.setAutoDefault(False)
        self.image_editor_executable_button.setFixedWidth(
           int(self.image_editor_executable_button.sizeHint().width() * 1.3))
        self.image_editor_executable_button.clicked.connect(
           self.set_image_editor_executable_path)
         
        image_editor_layout = QVBoxLayout()
        image_editor_layout.addWidget(self.image_editor_executable_line_edit)
        image_editor_layout.addWidget(self.image_editor_executable_button)
        grid_layout.addWidget(QLabel('Image editor executable'), row, 0,
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        grid_layout.addLayout(image_editor_layout, row, 1)
        
        # Force layout processing so widget sizes are calculated
        QApplication.processEvents()
        
        # Calculate max column 0 width using sizeHint (works before dialog is shown)
        max_col0_width = 0
        for section in self.sections:
            for i in range(section.grid_layout.rowCount()):
                item = section.grid_layout.itemAtPosition(i, 0)
                if item:
                    if item.widget():
                        w = item.widget().sizeHint().width()
                        max_col0_width = max(max_col0_width, w)
                    elif item.layout():
                        for j in range(item.layout().count()):
                            sub_item = item.layout().itemAt(j)
                            if sub_item and sub_item.widget():
                                w = sub_item.widget().sizeHint().width()
                                max_col0_width = max(max_col0_width, w)
        
        # Add button section for reset and other actions
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        remove_app_data_button = QPushButton('Remove app data\u2026')
        remove_app_data_button.setAutoDefault(False)
        remove_app_data_button.clicked.connect(self.remove_app_data)
        button_layout.addWidget(remove_app_data_button)
        reset_to_defaults_button = QPushButton('Reset to Defaults')
        reset_to_defaults_button.setAutoDefault(False)
        reset_to_defaults_button.clicked.connect(self.reset_to_defaults)
        button_layout.addWidget(reset_to_defaults_button)
        layout.addLayout(button_layout)

        # Stretch pushes content to top within the scroll area
        layout.addStretch()
        
        # Calculate required width: column 0 + column 1 + padding + scrollbar
        # For center divider to align with section title center: col1_fixed = col0_min - spacing(10)
        # So both columns are nearly equal, centering the divider at the section midpoint
        grid_spacing = 10
        col0_min = max(max_col0_width + 40, 470)  # At least 470px for center alignment
        col1_fixed = col0_min - grid_spacing       # col1 = col0 - spacing → divider at center
        for section in self.sections:
            section.grid_layout.setColumnMinimumWidth(0, col0_min)
            section.grid_layout.setColumnMinimumWidth(1, col1_fixed)
        
        content_padding = 40    # 20px left + 20px right on content_widget
        scrollbar_width = 20
        total_width = col0_min + grid_spacing + col1_fixed + content_padding + scrollbar_width
        
        # Style the floating warning bar now that we know the theme
        warning_bg = '#3a2a00' if is_dark else '#fff3cd'
        warning_fg = '#ffcc44' if is_dark else '#856404'
        self.warning_label.setStyleSheet(
            f'background-color: {warning_bg}; color: {warning_fg}; '
            f'border-top: 1px solid {"#5a4a00" if is_dark else "#ffc107"}; '
            f'padding: 8px 12px; font-weight: bold;'
        )
        
        # Set fixed width, only allow height to vary with content
        self.setFixedWidth(total_width)
        self.resize(total_width, min(self.sizeHint().height(), 780))
        
        # Set content_widget to fixed width to prevent horizontal expansion
        self.content_widget.setFixedWidth(total_width - scrollbar_width)

    @Slot()
    def show_restart_warning(self):
        self.warning_label.setText(self.restart_warning)
        self.warning_label.show()

    @Slot()
    def refresh_theme_sensitive_controls(self):
        for check_box in (self.insert_space_after_tag_separator_check_box,
                          self.autocomplete_tags_check_box,
                          self.disable_new_tag_auto_select_check_box,
                          self.ask_before_removing_tag_library_tags_check_box,
                          self.ask_before_assigning_new_tag_category_check_box):
            check_box.style().unpolish(check_box)
            check_box.style().polish(check_box)
            check_box.update()
        
        # Update section colors for new theme
        theme = self.settings.value('theme', defaultValue=DEFAULT_SETTINGS['theme'], type=str)
        is_dark = theme == ThemeMode.DARK
        for section in self.sections:
            section.set_dark_mode(is_dark)

    @Slot(str)
    def handle_tag_separator_change(self, tag_separator: str):
        if not tag_separator:
            self.warning_label.setText('The tag separator cannot be empty.')
            self.warning_label.show()
            return
        if tag_separator == r'\n':
            self.warning_label.setText(
                'Newline is no longer supported as the tag separator because '
                'caption files can contain natural language text.')
            self.warning_label.show()
            return
        self.insert_space_after_tag_separator_check_box.setEnabled(True)
        self.settings.setValue('tag_separator', tag_separator)
        # Applied live (no restart): hide any prior warning and notify.
        self.warning_label.hide()
        self.tag_separator_changed.emit()

    @Slot(str, bool)
    def update_model_visibility(self, model_id: str, is_checked: bool):
        """Update the hidden models list based on checkbox state."""
        hidden_models = get_hidden_model_ids()
        
        if is_checked and model_id in hidden_models:
            # Model is now visible, remove from hidden list
            hidden_models.remove(model_id)
        elif not is_checked and model_id not in hidden_models:
            # Model is now hidden, add to hidden list
            hidden_models.append(model_id)
        
        save_hidden_model_ids(hidden_models)

    @Slot()
    def select_all_models(self):
        """Check all model visibility checkboxes."""
        for checkbox in self.model_visibility_checkboxes.values():
            checkbox.setChecked(True)
        # Clear hidden models list
        save_hidden_model_ids([])

    @Slot()
    def deselect_all_models(self):
        """Uncheck all model visibility checkboxes."""
        for checkbox in self.model_visibility_checkboxes.values():
            checkbox.setChecked(False)
        # Hide all models
        save_hidden_model_ids(list(MODELS))

    @Slot()
    def set_models_directory_path(self):
        models_directory_path = self.settings.value(
            'models_directory_path',
            defaultValue=DEFAULT_SETTINGS['models_directory_path'], type=str)
        if models_directory_path:
            initial_directory_path = models_directory_path
        elif self.settings.contains('directory_path'):
            initial_directory_path = self.settings.value('directory_path')
        else:
            initial_directory_path = ''
        models_directory_path = QFileDialog.getExistingDirectory(
            parent=self, caption='Select directory containing auto-captioning '
                                 'models',
            dir=initial_directory_path)
        if models_directory_path:
            self.models_directory_line_edit.setText(models_directory_path)

    @Slot()
    def set_image_editor_executable_path(self):
        current_path = self.settings.value(
            'image_editor_executable_path',
            defaultValue=DEFAULT_SETTINGS['image_editor_executable_path'],
            type=str)
        if sys.platform == 'win32':
            file_filter = 'Executable files (*.exe);;All files (*)'
        else:
            file_filter = 'All files (*)'
        image_editor_executable_path, _ = QFileDialog.getOpenFileName(
            parent=self,
            caption='Select image editor executable',
            dir=current_path,
            filter=file_filter)
        if image_editor_executable_path:
            self.image_editor_executable_line_edit.setText(
                image_editor_executable_path)

    @Slot()
    def _clear_thumbnail_cache(self):
        reply = QMessageBox.question(
            self,
            'Clear Thumbnail Cache',
            'Delete all cached thumbnails? They will be regenerated the next '
            'time each image is viewed.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        clear_cache()
        self._cache_size_label.setText('0.0 MB')

    @Slot()
    def remove_app_data(self):
        """Show the Remove app data dialog. If the user confirms and data is
        deleted, ask the main window to quit terminally so nothing is rewritten
        during shutdown."""
        dialog = RemoveAppDataDialog(parent=self, dialog_font=self.font())
        parent = self.parent()
        if hasattr(parent, 'apply_dialog_title_bar_theme'):
            parent.apply_dialog_title_bar_theme(dialog)
        dialog.exec()
        if dialog.data_removed:
            self.remove_app_data_requested.emit()
            self.accept()

    @Slot()
    def reset_to_defaults(self):
        """Reset all settings to their default values."""
        reply = QMessageBox.question(
            self,
            'Reset to Defaults',
            'Are you sure you want to reset all settings to their default values? '
            'This action cannot be undone.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        # Clear all settings and restore defaults
        self.settings.clear()
        
        # Re-apply all default values
        for key, value in DEFAULT_SETTINGS.items():
            self.settings.setValue(key, value)
        
        self.settings.sync()
        
        # Show confirmation message
        QMessageBox.information(
            self,
            'Reset Complete',
            'All settings have been reset to their default values. '
            'Please restart the application for all changes to take effect.',
            QMessageBox.StandardButton.Ok)
        
        # Close the settings dialog
        self.accept()
