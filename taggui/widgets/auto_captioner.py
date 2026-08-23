import re
import sys
from pathlib import Path

from PySide6.QtCore import QModelIndex, QPoint, Qt, QTimer, Signal, Slot
from PySide6.QtGui import (QColor, QFontMetrics, QSyntaxHighlighter,
                           QTextCharFormat, QTextCursor)
from PySide6.QtWidgets import (QComboBox, QDockWidget, QFormLayout,
                               QFrame, QHBoxLayout, QLabel, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from auto_captioning.auto_captioning_model import release_memory
from auto_captioning.captioning_thread import CaptioningThread
from auto_captioning.models.wd_tagger import WdTagger
from auto_captioning.models.pixai_tagger import PixAiTagger
from auto_captioning.models_list import MODELS, get_model_class
from dialogs.caption_multiple_images_dialog import CaptionMultipleImagesDialog
from models.image_list_model import ImageListModel
from models.tag_library_model import TagLibraryModel
from utils.big_widgets import TallPushButton
from utils.enums import (CaptionDestination, CaptionDevice, CaptionPosition,
                         NaturalLanguagePosition)
from utils.settings import (DEFAULT_SETTINGS, get_settings, get_tag_separator,
                            get_hidden_model_ids)
from utils.settings_widgets import (FocusedScrollSettingsComboBox,
                                    FocusedScrollSettingsDoubleSpinBox,
                                    FocusedScrollSettingsSpinBox,
                                    SettingsBigCheckBox, SettingsLineEdit,
                                    SettingsPlainTextEdit)
from utils.utils import pluralize
from widgets.image_list import ImageList


def set_text_edit_height(text_edit: QPlainTextEdit, line_count: int):
    """
    Set the height of a text edit to the height of a given number of lines.
    """
    # From https://stackoverflow.com/a/46997337.
    document = text_edit.document()
    font_metrics = QFontMetrics(document.defaultFont())
    margins = text_edit.contentsMargins()
    height = int(font_metrics.lineSpacing() * line_count
                 + margins.top() + margins.bottom()
                 + document.documentMargin() * 2
                 + text_edit.frameWidth() * 2)
    text_edit.setFixedHeight(height)


# Matches a trailing probability suffix such as " (1.00)" or " (0.97)" that the
# taggers append after each tag when "Show probabilities" is enabled. The tag
# name itself is everything before this suffix.
PROBABILITY_SUFFIX = re.compile(r'\s*\(\d+(?:\.\d+)?\)\s*$')


class TagColorHighlighter(QSyntaxHighlighter):
    """
    Colors tags shown in the Auto-Captioner preview using their tag library
    category colors. Each line of preview text is a list of tags separated by
    the tag separator (e.g. ", "), and each tag may be followed by a
    probability such as "(0.97)". Only the tag name is colored, and only when
    the tag belongs to a category that has a color.
    """

    def __init__(self, document, tag_library_model: TagLibraryModel):
        super().__init__(document)
        self.tag_library_model = tag_library_model

    def highlightBlock(self, text: str):
        tag_separator = get_tag_separator()
        position = 0
        for segment in text.split(tag_separator):
            segment_start = position
            # Advance the running position past this segment and the separator
            # that follows it, so offsets stay correct for the next segment.
            position += len(segment) + len(tag_separator)
            # Remove a trailing probability suffix (e.g. " (0.97)") so only the
            # tag name is looked up and colored.
            match = PROBABILITY_SUFFIX.search(segment)
            tag_part = segment[:match.start()] if match else segment
            tag = tag_part.strip()
            if not tag:
                continue
            category = self.tag_library_model.get_category_for_tag(tag)
            if not category:
                continue
            color = QColor(category['color'])
            if not color.isValid():
                continue
            # Skip any leading whitespace so only the tag name is colored.
            leading_whitespace_count = len(tag_part) - len(tag_part.lstrip())
            tag_start = segment_start + leading_whitespace_count
            text_format = QTextCharFormat()
            text_format.setForeground(color)
            self.setFormat(tag_start, len(tag), text_format)


class HorizontalLine(QFrame):
    def __init__(self):
        super().__init__()
        self.setFrameShape(QFrame.Shape.HLine)
        self.setFrameShadow(QFrame.Shadow.Raised)


class CaptionSettingsForm(QVBoxLayout):
    def __init__(self):
        super().__init__()
        self.settings = get_settings()
        # Some environments raise errors other than ImportError when importing
        # bitsandbytes (e.g. a RuntimeError from a CUDA setup probe). Catch any
        # exception so a transient failure can't silently and permanently
        # disable the "Load in 4-bit" option.
        try:
            import bitsandbytes
            self.is_bitsandbytes_available = True
        except Exception:
            self.is_bitsandbytes_available = False
        basic_settings_form = QFormLayout()
        basic_settings_form.setRowWrapPolicy(
            QFormLayout.RowWrapPolicy.WrapAllRows)
        basic_settings_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.model_combo_box = FocusedScrollSettingsComboBox(
            key='model_id', default='deepghs/pixai-tagger-v0.9-onnx')
        # `setEditable()` must be called before `addItems()` to preserve any
        # custom model that was set.
        self.model_combo_box.setEditable(True)
        self.model_combo_box.addItems(self.get_local_model_paths())
        
        # Add visible models only
        hidden_models = get_hidden_model_ids()
        visible_models = [m for m in MODELS if m not in hidden_models]
        self.model_combo_box.addItems(visible_models)
        self.prompt_text_edit = SettingsPlainTextEdit(key='prompt')
        set_text_edit_height(self.prompt_text_edit, 4)
        self.caption_start_line_edit = SettingsLineEdit(key='caption_start')
        self.caption_start_line_edit.setClearButtonEnabled(True)
        self.caption_destination_combo_box = FocusedScrollSettingsComboBox(
            key='caption_destination',
            default=DEFAULT_SETTINGS['caption_destination'])
        self.caption_destination_combo_box.addItems(list(CaptionDestination))
        self.caption_position_combo_box = FocusedScrollSettingsComboBox(
            key='caption_position')
        self.caption_position_combo_box.addItems(list(CaptionPosition))
        self.natural_language_position_combo_box = (
            FocusedScrollSettingsComboBox(
                key='natural_language_position',
                default=DEFAULT_SETTINGS['natural_language_position']))
        self.natural_language_position_combo_box.addItems(
            list(NaturalLanguagePosition))
        self.device_combo_box = FocusedScrollSettingsComboBox(key='device')
        self.device_combo_box.addItems(list(CaptionDevice))
        self.load_in_4_bit_container = QWidget()
        load_in_4_bit_layout = QHBoxLayout()
        load_in_4_bit_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        load_in_4_bit_layout.setContentsMargins(0, 0, 0, 0)
        self.load_in_4_bit_check_box = SettingsBigCheckBox(
            key='load_in_4_bit', default=True)
        load_in_4_bit_layout.addWidget(QLabel('Load in 4-bit'))
        load_in_4_bit_layout.addWidget(self.load_in_4_bit_check_box)
        self.load_in_4_bit_container.setLayout(load_in_4_bit_layout)
        self.remove_tag_separators_container = QWidget()
        remove_tag_separators_layout = QHBoxLayout(
            self.remove_tag_separators_container)
        remove_tag_separators_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        remove_tag_separators_layout.setContentsMargins(0, 0, 0, 0)
        self.remove_tag_separators_check_box = SettingsBigCheckBox(
            key='remove_tag_separators', default=True)
        remove_tag_separators_label = QLabel(
            'Remove tag separators in caption')
        remove_tag_separators_layout.addWidget(remove_tag_separators_label)
        remove_tag_separators_layout.addWidget(
            self.remove_tag_separators_check_box)
        basic_settings_form.addRow('Model', self.model_combo_box)
        self.prompt_label = QLabel('Prompt')
        basic_settings_form.addRow(self.prompt_label, self.prompt_text_edit)
        self.caption_start_label = QLabel('Start caption with')
        basic_settings_form.addRow(self.caption_start_label,
                                   self.caption_start_line_edit)
        self.caption_destination_label = QLabel('Destination')
        basic_settings_form.addRow(self.caption_destination_label,
                                   self.caption_destination_combo_box)
        self.caption_position_label = QLabel('Tag position')
        basic_settings_form.addRow(self.caption_position_label,
                                   self.caption_position_combo_box)
        self.natural_language_position_label = QLabel(
            'Natural language position')
        basic_settings_form.addRow(self.natural_language_position_label,
                                   self.natural_language_position_combo_box)
        self.device_label = QLabel('Device')
        basic_settings_form.addRow(self.device_label, self.device_combo_box)
        basic_settings_form.addRow(self.load_in_4_bit_container)
        basic_settings_form.addRow(self.remove_tag_separators_container)

        self.wd_tagger_settings_form_container = QWidget()
        wd_tagger_settings_form = QFormLayout(
            self.wd_tagger_settings_form_container)
        wd_tagger_settings_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        wd_tagger_settings_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.show_probabilities_check_box = SettingsBigCheckBox(
            key='wd_tagger_show_probabilities', default=True)
        self.min_probability_spin_box = FocusedScrollSettingsDoubleSpinBox(
            key='wd_tagger_min_probability', default=0.4, minimum=0.01,
            maximum=1)
        self.min_probability_spin_box.setSingleStep(0.01)
        self.max_tags_spin_box = FocusedScrollSettingsSpinBox(
            key='wd_tagger_max_tags', default=30, minimum=1, maximum=999)
        tag_filters_form = QFormLayout()
        tag_filters_form.setRowWrapPolicy(
            QFormLayout.RowWrapPolicy.WrapAllRows)
        tag_filters_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.tags_to_exclude_text_edit = SettingsPlainTextEdit(
            key='wd_tagger_tags_to_exclude')
        tag_filters_form.addRow('Tag filters',
                                self.tags_to_exclude_text_edit)
        set_text_edit_height(self.tags_to_exclude_text_edit, 4)
        wd_tagger_settings_form.addRow('Show probabilities',
                                       self.show_probabilities_check_box)
        wd_tagger_settings_form.addRow('Minimum probability',
                                       self.min_probability_spin_box)
        wd_tagger_settings_form.addRow('Maximum tags', self.max_tags_spin_box)
        wd_tagger_settings_form.addRow(tag_filters_form)

        self.toggle_advanced_settings_form_button = TallPushButton(
            'Show Advanced Settings')

        self.advanced_settings_form_container = QWidget()
        advanced_settings_form = QFormLayout(
            self.advanced_settings_form_container)
        advanced_settings_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        advanced_settings_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        bad_forced_words_form = QFormLayout()
        bad_forced_words_form.setRowWrapPolicy(
            QFormLayout.RowWrapPolicy.WrapAllRows)
        bad_forced_words_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.bad_words_line_edit = SettingsLineEdit(key='bad_words')
        self.bad_words_line_edit.setClearButtonEnabled(True)
        self.forced_words_line_edit = SettingsLineEdit(key='forced_words')
        self.forced_words_line_edit.setClearButtonEnabled(True)
        bad_forced_words_form.addRow('Discourage from caption',
                                     self.bad_words_line_edit)
        bad_forced_words_form.addRow('Include in caption',
                                     self.forced_words_line_edit)
        self.min_new_token_count_spin_box = FocusedScrollSettingsSpinBox(
            key='min_new_tokens', default=1, minimum=1, maximum=999)
        self.max_new_token_count_spin_box = FocusedScrollSettingsSpinBox(
            key='max_new_tokens', default=100, minimum=1, maximum=999)
        self.beam_count_spin_box = FocusedScrollSettingsSpinBox(
            key='num_beams', default=1, minimum=1, maximum=99)
        self.length_penalty_spin_box = FocusedScrollSettingsDoubleSpinBox(
            key='length_penalty', default=1, minimum=-5, maximum=5)
        self.length_penalty_spin_box.setSingleStep(0.1)
        self.use_sampling_check_box = SettingsBigCheckBox(key='do_sample',
                                                          default=False)
        # The temperature must be positive.
        self.temperature_spin_box = FocusedScrollSettingsDoubleSpinBox(
            key='temperature', default=1, minimum=0.01, maximum=2)
        self.temperature_spin_box.setSingleStep(0.01)
        self.top_k_spin_box = FocusedScrollSettingsSpinBox(
            key='top_k', default=50, minimum=0, maximum=200)
        self.top_p_spin_box = FocusedScrollSettingsDoubleSpinBox(
            key='top_p', default=1, minimum=0, maximum=1)
        self.top_p_spin_box.setSingleStep(0.01)
        self.repetition_penalty_spin_box = FocusedScrollSettingsDoubleSpinBox(
            key='repetition_penalty', default=1, minimum=1, maximum=2)
        self.repetition_penalty_spin_box.setSingleStep(0.01)
        self.no_repeat_ngram_size_spin_box = FocusedScrollSettingsSpinBox(
            key='no_repeat_ngram_size', default=3, minimum=0, maximum=5)
        self.gpu_index_spin_box = FocusedScrollSettingsSpinBox(
            key='gpu_index', default=0, minimum=0, maximum=9)
        # Caps how many image patches ("visual tokens") a model receives per
        # image. Higher = more image detail but more VRAM used during
        # captioning; lower = less VRAM and faster. Only shown for models that
        # support it (e.g. Qwen2-VL/Qwen2.5-VL).
        self.max_image_tokens_spin_box = FocusedScrollSettingsSpinBox(
            key='max_image_tokens', default=1280, minimum=256, maximum=16384)
        self.max_image_tokens_spin_box.setSingleStep(128)
        self.max_image_tokens_label = QLabel('Max image tokens')
        # A small, muted hint that updates live to show roughly the largest
        # image (in pixels) kept at full detail for the current setting.
        self.max_image_tokens_example_label = QLabel()
        self.max_image_tokens_example_label.setStyleSheet(
            'color: gray; font-size: 11px;')
        # Wrap the hint so its text never forces the settings panel wider than
        # the dock (which would clip the other settings off the right edge).
        self.max_image_tokens_example_label.setWordWrap(True)
        self.update_max_image_tokens_example(
            self.max_image_tokens_spin_box.value())
        advanced_settings_form.addRow(bad_forced_words_form)
        advanced_settings_form.addRow(HorizontalLine())
        advanced_settings_form.addRow('Minimum tokens',
                                      self.min_new_token_count_spin_box)
        advanced_settings_form.addRow('Maximum tokens',
                                      self.max_new_token_count_spin_box)
        advanced_settings_form.addRow('Number of beams',
                                      self.beam_count_spin_box)
        advanced_settings_form.addRow('Length penalty',
                                      self.length_penalty_spin_box)
        advanced_settings_form.addRow('Use sampling',
                                      self.use_sampling_check_box)
        advanced_settings_form.addRow('Temperature',
                                      self.temperature_spin_box)
        advanced_settings_form.addRow('Top-k', self.top_k_spin_box)
        advanced_settings_form.addRow('Top-p', self.top_p_spin_box)
        advanced_settings_form.addRow('Repetition penalty',
                                      self.repetition_penalty_spin_box)
        advanced_settings_form.addRow('No repeat n-gram size',
                                      self.no_repeat_ngram_size_spin_box)
        advanced_settings_form.addRow(HorizontalLine())
        advanced_settings_form.addRow('GPU index', self.gpu_index_spin_box)
        advanced_settings_form.addRow(self.max_image_tokens_label,
                                      self.max_image_tokens_spin_box)
        advanced_settings_form.addRow('', self.max_image_tokens_example_label)
        self.advanced_settings_form_container.hide()

        self.addLayout(basic_settings_form)
        self.addWidget(self.wd_tagger_settings_form_container)
        self.horizontal_line = HorizontalLine()
        self.addWidget(self.horizontal_line)
        self.addWidget(self.toggle_advanced_settings_form_button)
        self.addWidget(self.advanced_settings_form_container)
        self.addStretch()

        self.model_combo_box.currentTextChanged.connect(
            self.show_settings_for_model)
        self.model_combo_box.currentTextChanged.connect(
            self.load_caption_destination_for_model)
        self.caption_destination_combo_box.currentTextChanged.connect(
            self.save_caption_destination_for_current_model)
        self.caption_destination_combo_box.currentTextChanged.connect(
            self.update_caption_destination_settings)
        self.device_combo_box.currentTextChanged.connect(
            self.set_load_in_4_bit_visibility)
        self.toggle_advanced_settings_form_button.clicked.connect(
            self.toggle_advanced_settings_form)
        # Make sure the minimum new token count is less than or equal to the
        # maximum new token count.
        self.min_new_token_count_spin_box.valueChanged.connect(
            self.max_new_token_count_spin_box.setMinimum)
        self.max_new_token_count_spin_box.valueChanged.connect(
            self.min_new_token_count_spin_box.setMaximum)
        self.max_image_tokens_spin_box.valueChanged.connect(
            self.update_max_image_tokens_example)

        # NOTE: These calls set widget visibility.  They must run only AFTER
        # this layout has been attached to a parent widget (see
        # AutoCaptioner.__init__), otherwise setVisible() acts on parentless
        # widgets, which Qt briefly shows as separate top-level windows,
        # causing small windows to flash on screen during startup.
        # AutoCaptioner calls initialize_visibility() at the right time.

    def initialize_visibility(self):
        self.show_settings_for_model(self.model_combo_box.currentText())
        self.load_caption_destination_for_model(self.model_combo_box.currentText())
        self.update_caption_destination_settings(
            self.caption_destination_combo_box.currentText())
        self.set_load_in_4_bit_visibility(self.device_combo_box.currentText())
        if not self.is_bitsandbytes_available:
            self.load_in_4_bit_check_box.setChecked(False)

    def get_local_model_paths(self) -> list[str]:
        models_directory_path = self.settings.value(
            'models_directory_path',
            defaultValue=DEFAULT_SETTINGS['models_directory_path'], type=str)
        if not models_directory_path:
            return []
        models_directory_path = Path(models_directory_path)
        print(f'Loading local auto-captioning model paths under '
              f'{models_directory_path}...')
        # Auto-captioning models have a `config.json` file.
        config_paths = set(models_directory_path.glob('**/config.json'))
        # WD Tagger models have a `selected_tags.csv` file.
        selected_tags_paths = set(
            models_directory_path.glob('**/selected_tags.csv'))
        model_directory_paths = [str(path.parent) for path
                                 in config_paths | selected_tags_paths]
        model_directory_paths.sort()
        print(f'Loaded {len(model_directory_paths)} model '
              f'{pluralize("path", len(model_directory_paths))}.')
        return model_directory_paths

    def refresh_model_list(self):
        """Repopulate the model dropdown from the current models directory and
        model-visibility settings, preserving the current selection. Lets the
        models directory and model visibility settings apply without a restart.
        """
        current_text = self.model_combo_box.currentText()
        hidden_models = get_hidden_model_ids()
        visible_models = [m for m in MODELS if m not in hidden_models]
        items = self.get_local_model_paths() + visible_models
        # Block signals so repopulating does not overwrite the saved `model_id`
        # or trigger per-model settings reloads. `QComboBox.addItems` is called
        # directly to bypass `SettingsComboBox`'s override, which would re-add a
        # persistence connection and reset the current text on every refresh.
        self.model_combo_box.blockSignals(True)
        self.model_combo_box.clear()
        QComboBox.addItems(self.model_combo_box, items)
        index = self.model_combo_box.findText(current_text)
        if index >= 0:
            self.model_combo_box.setCurrentIndex(index)
        else:
            self.model_combo_box.setEditText(current_text)
        self.model_combo_box.blockSignals(False)

    @Slot(str)
    def show_settings_for_model(self, model_id: str):
        wd_tagger_widgets = [self.wd_tagger_settings_form_container]
        non_wd_tagger_widgets = [
            self.prompt_label,
            self.prompt_text_edit,
            self.caption_start_label,
            self.caption_start_line_edit,
            self.device_label,
            self.device_combo_box,
            self.load_in_4_bit_container,
            self.remove_tag_separators_container,
            self.horizontal_line,
            self.toggle_advanced_settings_form_button,
            self.advanced_settings_form_container
        ]
        is_wd_tagger_model = get_model_class(model_id) in (WdTagger,
                                                           PixAiTagger)
        for widget in wd_tagger_widgets:
            widget.setVisible(is_wd_tagger_model)
        for widget in non_wd_tagger_widgets:
            widget.setVisible(not is_wd_tagger_model)
        # Only show the "Max image tokens" row for models whose processor
        # supports capping the number of image patches (e.g. Qwen models).
        supports_image_token_limit = getattr(
            get_model_class(model_id), 'supports_image_token_limit', False)
        self.max_image_tokens_label.setVisible(supports_image_token_limit)
        self.max_image_tokens_spin_box.setVisible(supports_image_token_limit)
        self.max_image_tokens_example_label.setVisible(
            supports_image_token_limit)
        self.set_load_in_4_bit_visibility(self.device_combo_box.currentText())

    @Slot(str)
    def set_load_in_4_bit_visibility(self, device: str):
        model_id = self.model_combo_box.currentText()
        is_wd_tagger_model = get_model_class(model_id) in (WdTagger,
                                                           PixAiTagger)
        if is_wd_tagger_model:
            self.load_in_4_bit_container.setVisible(False)
            return
        is_load_in_4_bit_available = (self.is_bitsandbytes_available
                                      and device == CaptionDevice.GPU)
        self.load_in_4_bit_container.setVisible(is_load_in_4_bit_available)

    @Slot(int)
    def update_max_image_tokens_example(self, max_image_tokens: int):
        # Show roughly the largest 4:3 image (in pixels) that is kept at full
        # detail for the current setting, so the effect of the value is
        # visible. Each token covers a 28x28 patch, so the pixel budget is
        # tokens * 28 * 28; solve for a 4:3 rectangle within that budget.
        pixel_budget = max_image_tokens * 28 * 28
        height = round((pixel_budget * 3 / 4) ** 0.5)
        width = round(height * 4 / 3)
        # Zero-width spaces (\u200b) are invisible but give the label wrap
        # points, so the condensed text can still break onto a second line in a
        # narrow panel instead of forcing the whole settings panel wider.
        self.max_image_tokens_example_label.setText(
            f'\u2248\u200b{width}\u00d7\u200b{height}\u200bpx')

    @Slot()
    def toggle_advanced_settings_form(self):
        if self.advanced_settings_form_container.isHidden():
            self.advanced_settings_form_container.show()
            self.toggle_advanced_settings_form_button.setText(
                'Hide Advanced Settings')
        else:
            self.advanced_settings_form_container.hide()
            self.toggle_advanced_settings_form_button.setText(
                'Show Advanced Settings')

    def get_caption_settings(self) -> dict:
        return {
            'model_id': self.model_combo_box.currentText(),
            'prompt': self.prompt_text_edit.toPlainText(),
            'caption_start': self.caption_start_line_edit.text(),
            'caption_destination':
                self.caption_destination_combo_box.currentText(),
            'caption_position': self.caption_position_combo_box.currentText(),
            'natural_language_position':
                self.natural_language_position_combo_box.currentText(),
            'device': self.device_combo_box.currentText(),
            'gpu_index': self.gpu_index_spin_box.value(),
            'max_image_tokens': self.max_image_tokens_spin_box.value(),
            'load_in_4_bit': self.load_in_4_bit_check_box.isChecked(),
            'remove_tag_separators':
                self.remove_tag_separators_check_box.isChecked(),
            'bad_words': self.bad_words_line_edit.text(),
            'forced_words': self.forced_words_line_edit.text(),
            'generation_parameters': {
                'min_new_tokens': self.min_new_token_count_spin_box.value(),
                'max_new_tokens': self.max_new_token_count_spin_box.value(),
                'num_beams': self.beam_count_spin_box.value(),
                'length_penalty': self.length_penalty_spin_box.value(),
                'do_sample': self.use_sampling_check_box.isChecked(),
                'temperature': self.temperature_spin_box.value(),
                'top_k': self.top_k_spin_box.value(),
                'top_p': self.top_p_spin_box.value(),
                'repetition_penalty': self.repetition_penalty_spin_box.value(),
                'no_repeat_ngram_size':
                    self.no_repeat_ngram_size_spin_box.value()
            },
            'wd_tagger_settings': {
                'show_probabilities':
                    self.show_probabilities_check_box.isChecked(),
                'min_probability': self.min_probability_spin_box.value(),
                'max_tags': self.max_tags_spin_box.value(),
                'tags_to_exclude':
                    self.tags_to_exclude_text_edit.toPlainText()
            }
        }

    @Slot(str)
    def update_caption_destination_settings(self, caption_destination: str):
        is_tag_destination = caption_destination == CaptionDestination.TAGS
        is_wd_tagger_model = get_model_class(
            self.model_combo_box.currentText()) in (WdTagger, PixAiTagger)
        self.caption_position_label.setVisible(is_tag_destination)
        self.caption_position_combo_box.setVisible(is_tag_destination)
        self.natural_language_position_label.setVisible(
            not is_tag_destination)
        self.natural_language_position_combo_box.setVisible(
            not is_tag_destination)
        self.remove_tag_separators_container.setVisible(
            is_tag_destination and not is_wd_tagger_model)

    def get_caption_destination_by_model(self) -> dict[str, str]:
        raw_mapping = self.settings.value(
            'caption_destination_by_model',
            defaultValue=DEFAULT_SETTINGS['caption_destination_by_model'])
        if not isinstance(raw_mapping, dict):
            return {}
        normalized_mapping = {}
        valid_destinations = set(CaptionDestination)
        for model_id, destination in raw_mapping.items():
            normalized_model_id = str(model_id).strip()
            normalized_destination = str(destination).strip()
            if (not normalized_model_id
                    or normalized_destination not in valid_destinations):
                continue
            normalized_mapping[normalized_model_id] = normalized_destination
        return normalized_mapping

    def save_caption_destination_by_model(self,
                                          caption_destination_by_model:
                                          dict[str, str]):
        self.settings.setValue('caption_destination_by_model',
                               caption_destination_by_model)

    @Slot(str)
    def save_caption_destination_for_current_model(self, caption_destination: str):
        model_id = self.model_combo_box.currentText().strip()
        if not model_id:
            return
        caption_destination_by_model = self.get_caption_destination_by_model()
        caption_destination_by_model[model_id] = caption_destination
        self.save_caption_destination_by_model(caption_destination_by_model)

    @Slot(str)
    def load_caption_destination_for_model(self, model_id: str):
        normalized_model_id = model_id.strip()
        caption_destination_by_model = self.get_caption_destination_by_model()
        is_wd_tagger_model = get_model_class(model_id) in (WdTagger,
                                                           PixAiTagger)
        smart_default = (CaptionDestination.TAGS if is_wd_tagger_model
                         else CaptionDestination.NATURAL_LANGUAGE)
        caption_destination = caption_destination_by_model.get(
            normalized_model_id, smart_default)
        self.caption_destination_combo_box.blockSignals(True)
        self.caption_destination_combo_box.setCurrentText(caption_destination)
        self.caption_destination_combo_box.blockSignals(False)
        self.settings.setValue('caption_destination', caption_destination)
        self.update_caption_destination_settings(caption_destination)


@Slot()
def restore_stdout_and_stderr():
    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__


class AutoCaptioner(QDockWidget):
    captioning_started = Signal()
    captioning_finished = Signal()
    caption_generated = Signal(QModelIndex, str, list, str)

    def __init__(self, image_list_model: ImageListModel,
                 image_list: ImageList,
                 tag_library_model: TagLibraryModel):
        super().__init__()
        self.image_list_model = image_list_model
        self.image_list = image_list
        self.tag_library_model = tag_library_model
        self.settings = get_settings()
        self.is_captioning = False
        self.captioning_thread = None
        self.processor = None
        self.model = None
        self.model_id: str | None = None
        self.model_device_type: str | None = None
        self.is_model_loaded_in_4_bit = None
        # Cache key for settings that only affect the processor (e.g. Qwen's
        # "Max image tokens"). When it changes, the processor is rebuilt even if
        # the model itself can be reused.
        self.processor_cache_key = None
        self.current_caption_destination: str | None = None
        # Whether the last block of text in the console text edit should be
        # replaced with the next block of text that is outputted.
        self.replace_last_console_text_edit_block = False

        # Each `QDockWidget` needs a unique object name for saving its state.
        self.setObjectName('auto_captioner')
        self.setWindowTitle('Auto-Captioner')
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea
                             | Qt.DockWidgetArea.RightDockWidgetArea)

        self.start_cancel_button = TallPushButton('Start Auto-Captioning')
        # Deliberately a plain (short) button rather than a TallPushButton so it
        # is smaller than the main action button and less likely to be clicked
        # by accident.
        self.unload_model_button = QPushButton('Unload model')
        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat('%v / %m images captioned (%p%)')
        self.progress_bar.hide()
        self.console_text_edit = QPlainTextEdit()
        set_text_edit_height(self.console_text_edit, 4)
        self.console_text_edit.setReadOnly(True)
        self.console_text_edit.hide()
        # Color tags shown in the preview using their tag library category
        # colors, matching how tags are colored elsewhere in the app.
        self.tag_color_highlighter = TagColorHighlighter(
            self.console_text_edit.document(), self.tag_library_model)
        # Re-apply the colors whenever categories or their colors change.
        self.tag_library_model.categories_changed.connect(
            self.tag_color_highlighter.rehighlight)
        self.tag_library_model.modelReset.connect(
            self.tag_color_highlighter.rehighlight)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(self.start_cancel_button)
        layout.addWidget(self.unload_model_button)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.console_text_edit)
        self.caption_settings_form = CaptionSettingsForm()
        layout.addLayout(self.caption_settings_form)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setWidget(container)
        self.setWidget(scroll_area)
        self.scroll_area = scroll_area
        # A floating (overlay) copy of the "Start Auto-Captioning" button. It is
        # a child of the scroll area's viewport so it is drawn on top of the
        # scrolling content. It stays hidden while the real (docked) button is
        # visible at the top of the pane, and appears pinned to the top of the
        # visible area once the docked button is scrolled out of view. This
        # keeps the button reachable while scrolling without altering the
        # pane's size or layout (the real button still occupies its space).
        self.floating_start_cancel_button = TallPushButton(
            'Start Auto-Captioning')
        self.floating_start_cancel_button.setParent(scroll_area.viewport())
        self.floating_start_cancel_button.hide()
        self.floating_start_cancel_button.clicked.connect(
            self.start_or_cancel_captioning)
        scroll_area.verticalScrollBar().valueChanged.connect(
            self.update_floating_button)
        scroll_area.verticalScrollBar().rangeChanged.connect(
            self.update_floating_button)
        # Now that the settings form is parented (via the scroll area's
        # container widget), it is safe to set initial widget visibility.
        # Doing this earlier would toggle visibility on parentless widgets,
        # which flash on screen as separate top-level windows during startup.
        self.caption_settings_form.initialize_visibility()

        self.start_cancel_button.clicked.connect(
            self.start_or_cancel_captioning)
        self.unload_model_button.clicked.connect(self.unload_model)

    def set_start_cancel_button_text(self, text: str):
        # Keep the docked button and its floating copy in sync.
        self.start_cancel_button.setText(text)
        self.floating_start_cancel_button.setText(text)

    def set_start_cancel_button_enabled(self, enabled: bool):
        # Keep the docked button and its floating copy in sync.
        self.start_cancel_button.setEnabled(enabled)
        self.floating_start_cancel_button.setEnabled(enabled)

    @Slot()
    def focus_start_cancel_button(self):
        """Bring the Auto-Captioner pane forward and put keyboard focus on the
        "Start Auto-Captioning" button (used by the Alt+C shortcut)."""
        self.raise_()

        def apply_focus():
            # Scroll back to the top so the real (docked) button is visible;
            # this also hides the floating overlay copy, so the focused button
            # is the one the user sees.
            self.scroll_area.verticalScrollBar().setValue(0)
            self.start_cancel_button.setFocus(
                Qt.FocusReason.ShortcutFocusReason)

        # Defer the focus until after the pane has finished being raised and
        # shown. Setting focus synchronously here is overridden by the
        # activation focus that Qt applies once the (possibly tabbed) dock
        # becomes the current one, which is why a plain setFocus left nothing
        # focused.
        QTimer.singleShot(0, apply_focus)

    @Slot()
    def update_floating_button(self):
        # Show the floating button pinned to the top of the visible area once
        # the real (docked) button has been scrolled out of view, and hide it
        # again when the user scrolls back to the top so the real button shows.
        scroll_area = self.scroll_area
        button = self.start_cancel_button
        content = scroll_area.widget()
        if content is None:
            return
        button_top = button.mapTo(content, QPoint(0, 0)).y()
        scroll_value = scroll_area.verticalScrollBar().value()
        if scroll_value > button_top:
            button_left = button.mapTo(content, QPoint(0, 0)).x()
            floating_button = self.floating_start_cancel_button
            floating_button.resize(button.width(), button.height())
            floating_button.move(button_left, 0)
            floating_button.show()
            floating_button.raise_()
        else:
            self.floating_start_cancel_button.hide()

    def resizeEvent(self, event):
        # Re-align the floating button when the pane is resized.
        super().resizeEvent(event)
        self.update_floating_button()

    @Slot()
    def start_or_cancel_captioning(self):
        if self.is_captioning:
            # Cancel captioning.
            self.captioning_thread.is_canceled = True
            self.set_start_cancel_button_enabled(False)
            self.set_start_cancel_button_text('Canceling Auto-Captioning...')
        else:
            # Start captioning.
            self.generate_captions()

    def set_is_captioning(self, is_captioning: bool):
        self.is_captioning = is_captioning
        button_text = ('Cancel Auto-Captioning' if is_captioning
                       else 'Start Auto-Captioning')
        self.set_start_cancel_button_text(button_text)
        # Disable unloading while a model is actively being used.
        self.unload_model_button.setEnabled(not is_captioning)

    @Slot()
    def unload_model(self):
        if self.is_captioning:
            QMessageBox.information(
                self, 'Captioning in progress',
                'Cancel or finish auto-captioning before unloading the model.')
            return
        if self.model is None:
            QMessageBox.information(
                self, 'No model loaded',
                'There is no model currently loaded.')
            return
        self.processor = None
        self.model = None
        self.model_id = None
        self.model_device_type = None
        self.is_model_loaded_in_4_bit = None
        self.processor_cache_key = None
        release_memory()
        self.update_console_text_edit('Unloaded model and freed memory.')

    @Slot(str)
    def update_console_text_edit(self, text: str):
        # '\x1b[A' is the ANSI escape sequence for moving the cursor up.
        if text == '\x1b[A':
            self.replace_last_console_text_edit_block = True
            return
        text = text.strip()
        if not text:
            return
        if self.console_text_edit.isHidden():
            self.console_text_edit.show()
        if self.replace_last_console_text_edit_block:
            self.replace_last_console_text_edit_block = False
            # Select and remove the last block of text.
            self.console_text_edit.moveCursor(QTextCursor.MoveOperation.End)
            self.console_text_edit.moveCursor(
                QTextCursor.MoveOperation.StartOfBlock,
                QTextCursor.MoveMode.KeepAnchor)
            self.console_text_edit.textCursor().removeSelectedText()
            # Delete the newline.
            self.console_text_edit.textCursor().deletePreviousChar()
        self.console_text_edit.appendPlainText(text)

    @Slot()
    def show_alert(self):
        if self.captioning_thread.is_canceled:
            return
        if self.captioning_thread.is_error:
            icon = QMessageBox.Icon.Critical
            text = ('An error occurred during captioning. See the '
                    'Auto-Captioner console for more information.')
        else:
            icon = QMessageBox.Icon.Information
            text = 'Captioning has finished.'
        alert = QMessageBox()
        alert.setIcon(icon)
        alert.setText(text)
        alert.exec()

    @Slot(QModelIndex, str, list, str)
    def forward_caption_generated(self, image_index: QModelIndex,
                                  caption: str, tags: list[str],
                                  natural_language_prompt: str):
        self.caption_generated.emit(image_index, caption, tags,
                                    natural_language_prompt)

    @Slot()
    def forward_captioning_finished(self):
        self.captioning_finished.emit()

    @Slot()
    def finalize_captioning(self):
        """Run end-of-captioning steps in a safe order.

        The completion alert is shown first and blocks (modal ``exec()``) until
        the user closes it. ``is_captioning`` is kept True across the alert so
        that any new-tag category-assignment prompts queued during the run stay
        queued (rather than firing inside the alert's nested event loop and
        appearing on top of it). Only after the alert closes is the captioning
        state cleared and ``captioning_finished`` emitted, which drains those
        queued tags as a single prompt.
        """
        if getattr(self, '_show_alert_when_finished', False):
            self.show_alert()
        self.set_is_captioning(False)
        self.forward_captioning_finished()

    @Slot()
    def _current_source_index(self) -> QModelIndex | None:
        """The source-model index of the current (highlighted) image.

        Used when the user chooses to caption only the current image from a
        multi-image selection. Falls back to ``None`` if there is no valid
        current index.
        """
        list_view = self.image_list.list_view
        current_proxy_index = list_view.selectionModel().currentIndex()
        if not current_proxy_index.isValid():
            return None
        return self.image_list.proxy_image_list_model.mapToSource(
            current_proxy_index)

    def generate_captions(self):
        selected_image_indices = self.image_list.get_selected_image_indices()
        selected_image_count = len(selected_image_indices)
        show_alert_when_finished = False
        if selected_image_count > 1:
            confirmation_dialog = CaptionMultipleImagesDialog(
                selected_image_count)
            reply = confirmation_dialog.exec()
            if reply != QMessageBox.StandardButton.Yes:
                return
            if confirmation_dialog.caption_current_image_only():
                # Reduce to a single-image run targeting the current image.
                # This mirrors captioning one image directly: no progress bar,
                # no multi-image undo confirmation, and no finish alert.
                current_index = self._current_source_index()
                if current_index is not None and current_index.isValid():
                    selected_image_indices = [current_index]
                else:
                    selected_image_indices = selected_image_indices[:1]
                selected_image_count = 1
            else:
                show_alert_when_finished = (confirmation_dialog
                                            .show_alert_check_box.isChecked())
        self.set_is_captioning(True)
        self.captioning_started.emit()
        caption_settings = self.caption_settings_form.get_caption_settings()
        self.current_caption_destination = caption_settings['caption_destination']
        if caption_settings['caption_destination'] == (
                CaptionDestination.NATURAL_LANGUAGE):
            caption_modifies_image = (
                caption_settings['natural_language_position']
                != NaturalLanguagePosition.DO_NOT_ADD)
        else:
            caption_modifies_image = (caption_settings['caption_position']
                                      != CaptionPosition.DO_NOT_ADD)
        if caption_modifies_image:
            self.image_list_model.add_to_undo_stack(
                action_name=f'Generate '
                            f'{pluralize("Caption", selected_image_count)}',
                should_ask_for_confirmation=selected_image_count > 1)
        if selected_image_count > 1:
            self.progress_bar.setRange(0, selected_image_count)
            self.progress_bar.setValue(0)
            self.progress_bar.show()
        tag_separator = get_tag_separator()
        models_directory_path = self.settings.value(
            'models_directory_path',
            defaultValue=DEFAULT_SETTINGS['models_directory_path'], type=str)
        models_directory_path = (Path(models_directory_path)
                                 if models_directory_path else None)
        self.captioning_thread = CaptioningThread(
            self, self.image_list_model, selected_image_indices,
            caption_settings, tag_separator, models_directory_path)
        self.captioning_thread.text_outputted.connect(
            self.update_console_text_edit)
        self.captioning_thread.clear_console_text_edit_requested.connect(
            self.console_text_edit.clear)
        self.captioning_thread.caption_generated.connect(
            self.forward_caption_generated)
        self.captioning_thread.progress_bar_update_requested.connect(
            self.progress_bar.setValue)
        self.captioning_thread.finished.connect(restore_stdout_and_stderr)
        self.captioning_thread.finished.connect(self.progress_bar.hide)
        self.captioning_thread.finished.connect(
            lambda: self.set_start_cancel_button_enabled(True))
        # Show the completion alert (if the user enabled it), then clear the
        # captioning state and emit ``captioning_finished`` from a single
        # finalizer, in that order. The alert is modal (``exec()`` runs a nested
        # event loop), so keeping ``is_captioning`` True until the alert closes
        # ensures any category-assignment prompts still queued for the run stay
        # queued (not shown on top of the alert) and are drained afterwards as a
        # single prompt via ``captioning_finished``.
        self._show_alert_when_finished = show_alert_when_finished
        self.captioning_thread.finished.connect(self.finalize_captioning)
        # Redirect `stdout` and `stderr` so that the outputs are displayed in
        # the console text edit.
        sys.stdout = self.captioning_thread
        sys.stderr = self.captioning_thread
        self.captioning_thread.start()
