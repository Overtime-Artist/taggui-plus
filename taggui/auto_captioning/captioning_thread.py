from datetime import datetime
from pathlib import Path
from time import perf_counter

from PIL import UnidentifiedImageError
from PySide6.QtCore import QModelIndex, QThread, Qt, Signal

from auto_captioning.auto_captioning_model import AutoCaptioningModel
from auto_captioning.models_list import get_model_class
from models.image_list_model import ImageListModel
from utils.enums import (CaptionDestination, CaptionPosition,
                         NaturalLanguagePosition)
from utils.image import Image
from utils.settings import get_tag_separator


def add_caption_to_tags(tags: list[str], caption: str,
                        caption_position: CaptionPosition) -> list[str]:
    if caption_position == CaptionPosition.DO_NOT_ADD or not caption:
        return tags
    tag_separator = get_tag_separator()
    new_tags = [t.strip() for t in caption.split(tag_separator) if t.strip()]
    # Make a copy of the tags so that the tags in the image list model are not
    # modified.
    tags = tags.copy()
    if caption_position == CaptionPosition.BEFORE_FIRST_TAG:
        tags[:0] = new_tags
    elif caption_position == CaptionPosition.AFTER_LAST_TAG:
        tags.extend(new_tags)
    elif caption_position == CaptionPosition.OVERWRITE_FIRST_TAG:
        if tags:
            tags[:1] = new_tags
        else:
            tags = new_tags
    elif caption_position == CaptionPosition.OVERWRITE_ALL_TAGS:
        tags = new_tags
    return tags


def get_generated_caption_data(image: Image, caption: str,
                               caption_destination: CaptionDestination | str,
                               caption_position: CaptionPosition,
                               natural_language_position:
                               NaturalLanguagePosition | str
                               ) -> tuple[list[str], str]:
    if caption_destination == CaptionDestination.NATURAL_LANGUAGE:
        if (natural_language_position
                == NaturalLanguagePosition.DO_NOT_ADD):
            return image.tags.copy(), image.natural_language_prompt
        if not caption:
            return image.tags.copy(), image.natural_language_prompt
        existing_prompt = image.natural_language_prompt
        if not existing_prompt:
            return image.tags.copy(), caption
        if (natural_language_position
                == NaturalLanguagePosition.BEFORE_EXISTING_TEXT):
            return image.tags.copy(), f'{caption}\n{existing_prompt}'
        if (natural_language_position
                == NaturalLanguagePosition.AFTER_EXISTING_TEXT):
            return image.tags.copy(), f'{existing_prompt}\n{caption}'
        return image.tags.copy(), caption
    tags = add_caption_to_tags(image.tags, caption, caption_position)
    return tags, image.natural_language_prompt


def format_duration(seconds: float) -> str:
    seconds_per_minute = 60
    seconds_per_hour = 60 * seconds_per_minute
    seconds_per_day = 24 * seconds_per_hour
    if seconds < seconds_per_minute:
        return f'{seconds:.1f} seconds'
    if seconds < seconds_per_hour:
        minutes = seconds / seconds_per_minute
        return f'{minutes:.1f} minutes'
    if seconds < seconds_per_day:
        hours = seconds / seconds_per_hour
        return f'{hours:.1f} hours'
    days = seconds / seconds_per_day
    return f'{days:.1f} days'


class CaptioningThread(QThread):
    text_outputted = Signal(str)
    clear_console_text_edit_requested = Signal()
    # The image index, caption text, updated tags, and updated natural language
    # prompt. The third parameter must be declared as `list` instead of
    # `list[str]` for it to work.
    caption_generated = Signal(QModelIndex, str, list, str)
    progress_bar_update_requested = Signal(int)

    def __init__(self, parent, image_list_model: ImageListModel,
                 selected_image_indices: list[QModelIndex],
                 caption_settings: dict, tag_separator: str,
                 models_directory_path: Path | None):
        super().__init__(parent)
        self.image_list_model = image_list_model
        self.selected_image_indices = selected_image_indices
        self.caption_settings = caption_settings
        self.tag_separator = tag_separator
        self.models_directory_path = models_directory_path
        self.is_error = False
        self.is_canceled = False

    def run_captioning(self):
        model_id = self.caption_settings['model_id']
        model_class = get_model_class(model_id)
        model: AutoCaptioningModel = model_class(
            captioning_thread_=self, caption_settings=self.caption_settings)
        error_message = model.get_error_message()
        if error_message:
            self.is_error = True
            self.clear_console_text_edit_requested.emit()
            print(error_message)
            return
        model.load_processor_and_model()
        model.monkey_patch_after_loading()
        if self.is_canceled:
            print('Canceled captioning.')
            return
        self.clear_console_text_edit_requested.emit()
        selected_image_count = len(self.selected_image_indices)
        are_multiple_images_selected = selected_image_count > 1
        captioning_start_datetime = datetime.now()
        captioning_message = model.get_captioning_message(
            are_multiple_images_selected, captioning_start_datetime)
        print(captioning_message)
        caption_destination = self.caption_settings['caption_destination']
        caption_position = self.caption_settings['caption_position']
        natural_language_position = self.caption_settings[
            'natural_language_position']
        for i, image_index in enumerate(self.selected_image_indices):
            start_time = perf_counter()
            if self.is_canceled:
                print('Canceled captioning.')
                return
            image: Image = self.image_list_model.data(image_index,
                                                      Qt.ItemDataRole.UserRole)
            image_prompt = model.get_image_prompt(image)
            try:
                model_inputs = model.get_model_inputs(image_prompt, image)
            except UnidentifiedImageError:
                print(f'Skipping {image.path.name} because its file format is '
                      'not supported or it is a corrupted image.')
                continue
            caption, console_output_caption = model.generate_caption(
                model_inputs, image_prompt)
            # Generation may have been interrupted partway through by a cancel
            # request (via the stopping criteria), so discard the partial
            # caption instead of writing it to the image.
            if self.is_canceled:
                print('Canceled captioning.')
                return
            tags, natural_language_prompt = get_generated_caption_data(
                image, caption, caption_destination, caption_position,
                natural_language_position)
            self.caption_generated.emit(
                image_index, caption, tags, natural_language_prompt)
            if are_multiple_images_selected:
                self.progress_bar_update_requested.emit(i + 1)
            if i == 0 and not are_multiple_images_selected:
                self.clear_console_text_edit_requested.emit()
            if console_output_caption is None:
                console_output_caption = caption
            print(f'{image.path.name} ({perf_counter() - start_time:.1f} s):\n'
                  f'{console_output_caption}')
        if are_multiple_images_selected:
            captioning_end_datetime = datetime.now()
            total_captioning_duration = ((captioning_end_datetime
                                          - captioning_start_datetime)
                                         .total_seconds())
            average_captioning_duration = (total_captioning_duration /
                                           selected_image_count)
            print(f'Finished captioning {selected_image_count} images in '
                  f'{format_duration(total_captioning_duration)} '
                  f'({average_captioning_duration:.1f} s/image) at '
                  f'{captioning_end_datetime.strftime("%Y-%m-%d %H:%M:%S")}.')

    def run(self):
        try:
            self.run_captioning()
        except Exception as exception:
            self.is_error = True
            # Show the error message in the console text edit.
            raise exception

    def write(self, text: str):
        self.text_outputted.emit(text)
