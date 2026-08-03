# Based on
# https://huggingface.co/spaces/SmilingWolf/wd-tagger/blob/main/app.py.
import csv
import re
from datetime import datetime
from enum import Enum
from pathlib import Path

import huggingface_hub
import numpy as np
from PIL import Image as PilImage
from onnxruntime import InferenceSession

import auto_captioning.captioning_thread as captioning_thread
from auto_captioning.auto_captioning_model import AutoCaptioningModel
from utils.image import Image

KAOMOJIS = ['0_0', '(o)_(o)', '+_+', '+_-', '._.', '<o>_<o>', '<|>_<|>', '=_=',
            '>_<', '3_3', '6_9', '>_o', '@_@', '^_^', 'o_o', 'u_u', 'x_x',
            '|_|', '||_||']


class FilterTagMatchMode(str, Enum):
    EXACT = 'exact'
    STARTS_WITH_PHRASE = 'starts_with_phrase'
    ENDS_WITH_PHRASE = 'ends_with_phrase'
    CONTAINS_PHRASE = 'contains_phrase'


class FilterTagRule:
    def __init__(self, match_mode: FilterTagMatchMode, source_tag: str,
                 target_tag: str | None):
        self.match_mode = match_mode
        self.source_tag = source_tag
        self.target_tag = target_tag


class FilterTagRules:
    def __init__(self, exact_excluded_tags: set[str],
                 exact_replacement_tags: dict[str, str],
                 wildcard_exclusion_rules: list[FilterTagRule],
                 wildcard_replacement_rules: list[FilterTagRule]):
        self.exact_excluded_tags = exact_excluded_tags
        self.exact_replacement_tags = exact_replacement_tags
        self.wildcard_exclusion_rules = wildcard_exclusion_rules
        self.wildcard_replacement_rules = wildcard_replacement_rules


def normalize_tag_text(text: str) -> str:
    if not text or text.isspace():
        return ''
    return ' '.join(text.split())


def split_respecting_quotes(text: str, delimiter: str) -> list[str]:
    parts = []
    if not text:
        return parts
    current = []
    in_quotes = False
    for character in text:
        if character == '"':
            in_quotes = not in_quotes
            current.append(character)
        elif character == delimiter and not in_quotes:
            parts.append(''.join(current))
            current = []
        else:
            current.append(character)
    parts.append(''.join(current))
    return parts


def index_of_outside_quotes(text: str, target: str) -> int:
    in_quotes = False
    for index, character in enumerate(text):
        if character == '"':
            in_quotes = not in_quotes
        elif character == target and not in_quotes:
            return index
    return -1


def unquote_token(token: str) -> str:
    trimmed = token.strip()
    if (len(trimmed) >= 2 and trimmed[0] == '"'
            and trimmed[-1] == '"'):
        return trimmed[1:-1]
    return trimmed


def parse_filter_tag_match_mode(raw_source_tag: str
                                ) -> tuple[FilterTagMatchMode, str]:
    source_tag = normalize_tag_text(raw_source_tag)
    has_leading_wildcard = source_tag.startswith('*')
    has_trailing_wildcard = source_tag.endswith('*')
    core_tag = source_tag.strip('*')
    if not core_tag.strip() or '*' in core_tag:
        return FilterTagMatchMode.EXACT, source_tag
    normalized_source_tag = normalize_tag_text(core_tag)
    if has_leading_wildcard and has_trailing_wildcard:
        return FilterTagMatchMode.CONTAINS_PHRASE, normalized_source_tag
    if has_leading_wildcard:
        return FilterTagMatchMode.ENDS_WITH_PHRASE, normalized_source_tag
    if has_trailing_wildcard:
        return FilterTagMatchMode.STARTS_WITH_PHRASE, normalized_source_tag
    return FilterTagMatchMode.EXACT, normalized_source_tag


def is_word_character(character: str) -> bool:
    return character.isalnum()


def is_phrase_boundary_match(candidate_tag: str, start_index: int,
                             length: int) -> bool:
    end_index = start_index + length
    has_leading_boundary = (start_index <= 0
                            or not is_word_character(
                                candidate_tag[start_index - 1]))
    has_trailing_boundary = (end_index >= len(candidate_tag)
                             or not is_word_character(
                                 candidate_tag[end_index]))
    return has_leading_boundary and has_trailing_boundary


def contains_phrase_boundary_match(candidate_tag: str, source_tag: str) -> bool:
    search_index = 0
    lowercase_candidate_tag = candidate_tag.casefold()
    lowercase_source_tag = source_tag.casefold()
    while search_index < len(candidate_tag):
        match_index = lowercase_candidate_tag.find(lowercase_source_tag,
                                                   search_index)
        if match_index < 0:
            return False
        if is_phrase_boundary_match(candidate_tag, match_index,
                                    len(source_tag)):
            return True
        search_index = match_index + 1
    return False


def apply_wildcard_replacement(candidate_tag: str,
                               rule: FilterTagRule) -> str:
    normalized_candidate = normalize_tag_text(candidate_tag)
    if (not normalized_candidate or not rule or not rule.source_tag
            or rule.target_tag is None):
        return normalized_candidate
    if rule.match_mode == FilterTagMatchMode.STARTS_WITH_PHRASE:
        if (normalized_candidate.casefold().startswith(
                rule.source_tag.casefold())
                and is_phrase_boundary_match(normalized_candidate, 0,
                                             len(rule.source_tag))):
            return rule.target_tag + normalized_candidate[len(rule.source_tag):]
        return normalized_candidate
    if rule.match_mode == FilterTagMatchMode.ENDS_WITH_PHRASE:
        start_index = len(normalized_candidate) - len(rule.source_tag)
        if (start_index >= 0
                and normalized_candidate.casefold().endswith(
                    rule.source_tag.casefold())
                and is_phrase_boundary_match(normalized_candidate, start_index,
                                             len(rule.source_tag))):
            return normalized_candidate[:start_index] + rule.target_tag
        return normalized_candidate
    if rule.match_mode == FilterTagMatchMode.CONTAINS_PHRASE:
        search_index = 0
        builder = []
        lowercase_candidate = normalized_candidate.casefold()
        lowercase_source = rule.source_tag.casefold()
        while search_index < len(normalized_candidate):
            match_index = lowercase_candidate.find(lowercase_source,
                                                   search_index)
            if match_index < 0:
                builder.append(normalized_candidate[search_index:])
                break
            if not is_phrase_boundary_match(normalized_candidate, match_index,
                                            len(rule.source_tag)):
                builder.append(normalized_candidate[
                    search_index:match_index + 1])
                search_index = match_index + 1
                continue
            builder.append(normalized_candidate[search_index:match_index])
            builder.append(rule.target_tag)
            search_index = match_index + len(rule.source_tag)
        return ''.join(builder)
    return normalized_candidate


def matches_filter_rule(candidate_tag: str, source_tag: str,
                        match_mode: FilterTagMatchMode) -> bool:
    normalized_candidate = normalize_tag_text(candidate_tag)
    if not normalized_candidate or not source_tag:
        return False
    if match_mode == FilterTagMatchMode.EXACT:
        return normalized_candidate.casefold() == source_tag.casefold()
    if match_mode == FilterTagMatchMode.STARTS_WITH_PHRASE:
        return (normalized_candidate.casefold().startswith(source_tag.casefold())
                and is_phrase_boundary_match(normalized_candidate, 0,
                                             len(source_tag)))
    if match_mode == FilterTagMatchMode.ENDS_WITH_PHRASE:
        start_index = len(normalized_candidate) - len(source_tag)
        return (start_index >= 0
                and normalized_candidate.casefold().endswith(
                    source_tag.casefold())
                and is_phrase_boundary_match(normalized_candidate, start_index,
                                             len(source_tag)))
    return contains_phrase_boundary_match(normalized_candidate, source_tag)


def parse_filter_tag_rules(filter_tags: str) -> FilterTagRules:
    exact_excluded_tags = set()
    exact_replacement_tags = {}
    wildcard_exclusion_rules = []
    wildcard_replacement_rules = []
    if not filter_tags or filter_tags.isspace():
        return FilterTagRules(exact_excluded_tags, exact_replacement_tags,
                              wildcard_exclusion_rules,
                              wildcard_replacement_rules)
    for raw_entry in split_respecting_quotes(filter_tags, ','):
        entry = raw_entry.strip()
        if not entry:
            continue
        separator_index = index_of_outside_quotes(entry, ':')
        if 0 < separator_index < len(entry) - 1:
            source_tag = unquote_token(entry[:separator_index])
            target_tag = normalize_tag_text(
                unquote_token(entry[separator_index + 1:]))
            if source_tag.strip() and target_tag.strip():
                match_mode, normalized_source_tag = (
                    parse_filter_tag_match_mode(source_tag))
                if match_mode == FilterTagMatchMode.EXACT:
                    exact_replacement_tags[
                        normalized_source_tag.casefold()] = target_tag
                else:
                    wildcard_replacement_rules.append(
                        FilterTagRule(match_mode, normalized_source_tag,
                                      target_tag))
                continue
        exclusion_mode, normalized_excluded_tag = parse_filter_tag_match_mode(
            unquote_token(entry))
        if exclusion_mode == FilterTagMatchMode.EXACT:
            exact_excluded_tags.add(normalized_excluded_tag.casefold())
        else:
            wildcard_exclusion_rules.append(
                FilterTagRule(exclusion_mode, normalized_excluded_tag, None))
    return FilterTagRules(exact_excluded_tags, exact_replacement_tags,
                          wildcard_exclusion_rules,
                          wildcard_replacement_rules)


def split_filtered_tag_output(tag: str) -> list[str]:
    output_tags = []
    for part in tag.split(','):
        normalized_part = normalize_tag_text(part)
        if normalized_part:
            output_tags.append(normalized_part)
    return output_tags


def apply_filter_tag_rules(
        tags_and_probabilities: list[tuple[str, float]],
        filter_tags: str) -> list[tuple[str, float]]:
    rules = parse_filter_tag_rules(filter_tags)
    updated_tags_and_probabilities = []
    for raw_tag, probability in tags_and_probabilities:
        tag = normalize_tag_text(raw_tag)
        if not tag:
            continue
        exact_replacement = rules.exact_replacement_tags.get(tag.casefold())
        if exact_replacement is not None:
            tag = exact_replacement
        if tag.casefold() in rules.exact_excluded_tags:
            continue
        for wildcard_replacement_rule in rules.wildcard_replacement_rules:
            if matches_filter_rule(tag, wildcard_replacement_rule.source_tag,
                                   wildcard_replacement_rule.match_mode):
                tag = apply_wildcard_replacement(tag, wildcard_replacement_rule)
                break
        is_wildcard_excluded = False
        for wildcard_exclusion_rule in rules.wildcard_exclusion_rules:
            if matches_filter_rule(tag, wildcard_exclusion_rule.source_tag,
                                   wildcard_exclusion_rule.match_mode):
                is_wildcard_excluded = True
                break
        if is_wildcard_excluded:
            continue
        for output_tag in split_filtered_tag_output(tag):
            updated_tags_and_probabilities.append((output_tag, probability))
    return updated_tags_and_probabilities


class WdTaggerModel:
    def __init__(self, model_id: str):
        model_path = Path(model_id) / 'model.onnx'
        if not model_path.is_file():
            model_path = huggingface_hub.hf_hub_download(model_id,
                                                         filename='model.onnx')
        tags_path = Path(model_id) / 'selected_tags.csv'
        if not tags_path.is_file():
            tags_path = huggingface_hub.hf_hub_download(
                model_id, filename='selected_tags.csv')
        self.inference_session = InferenceSession(model_path)
        self.tags = []
        self.rating_tags_indices = []
        self.general_tags_indices = []
        self.character_tags_indices = []
        with open(tags_path, 'r') as tags_file:
            reader = csv.DictReader(tags_file)
            for index, line in enumerate(reader):
                tag = line['name']
                if tag not in KAOMOJIS:
                    tag = tag.replace('_', ' ')
                self.tags.append(tag)
                category = line['category']
                if category == '9':
                    self.rating_tags_indices.append(index)
                elif category == '0':
                    self.general_tags_indices.append(index)
                elif category == '4':
                    self.character_tags_indices.append(index)

    def generate_tags(self, image_array: np.ndarray,
                      wd_tagger_settings: dict) -> tuple[tuple, tuple]:
        input_name = self.inference_session.get_inputs()[0].name
        output_name = self.inference_session.get_outputs()[0].name
        probabilities = self.inference_session.run(
            [output_name], {input_name: image_array})[0][0].astype(np.float32)
        # Exclude the rating tags.
        tags = [tag for index, tag in enumerate(self.tags)
                if index not in self.rating_tags_indices]
        probabilities = np.array([
            probability for index, probability in enumerate(probabilities)
            if index not in self.rating_tags_indices
        ])
        tags_and_probabilities = []
        for tag, probability in zip(tags, probabilities):
            if probability < wd_tagger_settings['min_probability']:
                continue
            tags_and_probabilities.append((tag, probability))
        tags_and_probabilities = apply_filter_tag_rules(
            tags_and_probabilities, wd_tagger_settings['tags_to_exclude'])
        # Sort the tags by probability.
        tags_and_probabilities.sort(key=lambda x: x[1], reverse=True)
        tags_and_probabilities = tags_and_probabilities[
                                 :wd_tagger_settings['max_tags']]
        if tags_and_probabilities:
            tags, probabilities = zip(*tags_and_probabilities)
        else:
            tags, probabilities = (), ()
        return tags, probabilities


class WdTagger(AutoCaptioningModel):
    image_mode = 'RGBA'

    def __init__(self,
                 captioning_thread_: 'captioning_thread.CaptioningThread',
                 caption_settings: dict):
        super().__init__(captioning_thread_, caption_settings)
        self.wd_tagger_settings = self.caption_settings['wd_tagger_settings']
        self.show_probabilities = self.wd_tagger_settings['show_probabilities']

    def get_error_message(self) -> str | None:
        return None

    def get_processor(self):
        return None

    def get_model(self):
        return WdTaggerModel(self.model_id)

    def get_captioning_message(self, are_multiple_images_selected: bool,
                               captioning_start_datetime: datetime) -> str:
        if are_multiple_images_selected:
            captioning_start_datetime_string = (
                self.get_captioning_start_datetime_string(
                    captioning_start_datetime))
            return (f'Generating tags... (start time: '
                    f'{captioning_start_datetime_string})')
        return 'Generating tags...'

    def get_model_inputs(self, image_prompt: str, image: Image) -> np.ndarray:
        pil_image = self.load_image(image)
        # Add a white background to the image in case it has transparent areas.
        canvas = PilImage.new('RGBA', pil_image.size, (255, 255, 255))
        canvas.alpha_composite(pil_image)
        pil_image = canvas.convert('RGB')
        # Pad the image to make it square.
        max_dimension = max(pil_image.size)
        canvas = PilImage.new('RGB', (max_dimension, max_dimension),
                              (255, 255, 255))
        horizontal_padding = (max_dimension - pil_image.width) // 2
        vertical_padding = (max_dimension - pil_image.height) // 2
        canvas.paste(pil_image, (horizontal_padding, vertical_padding))
        # Resize the image to the model's input dimensions.
        _, input_dimension, *_ = (self.model.inference_session.get_inputs()[0]
                                  .shape)
        if max_dimension != input_dimension:
            input_dimensions = (input_dimension, input_dimension)
            canvas = canvas.resize(input_dimensions,
                                   resample=PilImage.Resampling.BICUBIC)
        # Convert the image to a numpy array.
        image_array = np.array(canvas, dtype=np.float32)
        # Reverse the order of the color channels.
        image_array = image_array[:, :, ::-1]
        # Add a batch dimension.
        image_array = np.expand_dims(image_array, axis=0)
        return image_array

    def generate_caption(self, model_inputs: np.ndarray,
                         image_prompt: str) -> tuple[str, str]:
        tags, probabilities = self.model.generate_tags(model_inputs,
                                                       self.wd_tagger_settings)
        caption = self.thread.tag_separator.join(tags)
        if self.show_probabilities:
            console_output_caption = self.thread.tag_separator.join(
                f'{tag} ({probability:.2f})'
                for tag, probability in zip(tags, probabilities)
            )
        else:
            console_output_caption = caption
        return caption, console_output_caption
