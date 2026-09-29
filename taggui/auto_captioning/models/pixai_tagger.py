import csv
import json
from pathlib import Path

import huggingface_hub
from huggingface_hub.utils import EntryNotFoundError
import numpy as np
from PIL import Image as PilImage

from auto_captioning.models.wd_tagger import (KAOMOJIS, WdTagger,
                                              apply_filter_tag_rules,
                                              create_inference_session)
from utils.enums import CaptionDevice, TaggerPrecision

# Maps a precision option to the ONNX weight filename used by the PixAI Tagger
# v1.0 ONNX export. FP32 is the full-precision default; FP16 is aimed at GPUs;
# INT8 is a smaller/faster CPU-oriented quantization.
PRECISION_FILENAMES = {
    TaggerPrecision.FP32: 'model.onnx',
    TaggerPrecision.FP16: 'model_fp16.onnx',
    TaggerPrecision.INT8: 'model_int8.onnx',
}


def supports_precision_selection(model_id: str) -> bool:
    """Return True for PixAI tagger repositories that ship multiple precision
    weight files (FP32/FP16/INT8), so the UI can offer a precision dropdown."""
    lowercase_model_id = model_id.lower()
    return ('pixai' in lowercase_model_id and 'tagger' in lowercase_model_id
            and ('fp16' in lowercase_model_id or 'int8' in lowercase_model_id))


# Per-category probability thresholds recommended by the PixAI Tagger v1.0
# model card. These are used when a model repository does not ship its own
# thresholds.csv file (for example the ONNX exports of PixAI Tagger v1.0).
# The category numbers match the "category" column of selected_tags.csv:
# 0 = general, 1 = style, 3 = copyright, 4 = character, 5 = meta, 9 = rating.
DEFAULT_THRESHOLDS_BY_CATEGORY = {
    0: 0.17,
    1: 0.15,
    3: 0.24,
    4: 0.27,
    5: 0.17,
    9: 0.41,
}


def get_precision_filename(precision) -> str:
    """Resolve a precision value (a TaggerPrecision or its string value) to the
    matching ONNX weight filename, defaulting to full-precision model.onnx."""
    try:
        precision = TaggerPrecision(precision)
    except ValueError:
        precision = TaggerPrecision.FP32
    return PRECISION_FILENAMES[precision]


class PixAiTaggerModel:
    def __init__(self, model_id: str,
                 precision: TaggerPrecision = TaggerPrecision.FP32,
                 device_setting: CaptionDevice = CaptionDevice.CPU,
                 gpu_index: int = 0):
        model_filename = get_precision_filename(precision)
        model_path = self.try_get_model_file_path(model_id, model_filename)
        if model_path is None:
            # The requested precision is not available in this repository; fall
            # back to the full-precision model.onnx.
            model_path = self.get_model_file_path(model_id, 'model.onnx')
        selected_tags_path = self.get_model_file_path(model_id,
                                                      'selected_tags.csv')
        thresholds_path = self.try_get_model_file_path(model_id,
                                                       'thresholds.csv')
        preprocess_path = self.get_model_file_path(model_id, 'preprocess.json')
        self.inference_session = create_inference_session(
            model_path, device_setting, gpu_index)
        self.input_name = self.inference_session.get_inputs()[0].name
        self.output_names = [output.name for output in
                             self.inference_session.get_outputs()]
        self.tags = []
        self.categories = []
        if thresholds_path is not None:
            self.thresholds_by_category = self.load_thresholds(thresholds_path)
        else:
            # Repositories such as the PixAI Tagger v1.0 ONNX exports do not
            # include a thresholds.csv; fall back to the recommended defaults.
            self.thresholds_by_category = dict(DEFAULT_THRESHOLDS_BY_CATEGORY)
        (self.image_size, self.mean, self.std,
         self.pad_to_square) = self.load_preprocess(preprocess_path)
        with open(selected_tags_path, 'r', encoding='utf-8') as tags_file:
            reader = csv.DictReader(tags_file)
            for line in reader:
                tag = line['name']
                if tag not in KAOMOJIS:
                    tag = tag.replace('_', ' ')
                self.tags.append(tag)
                self.categories.append(int(line['category']))

    @staticmethod
    def get_model_file_path(model_id: str, filename: str) -> Path:
        file_path = Path(model_id) / filename
        if file_path.is_file():
            return file_path
        return Path(huggingface_hub.hf_hub_download(model_id, filename=filename))

    @staticmethod
    def try_get_model_file_path(model_id: str, filename: str) -> Path | None:
        # Like get_model_file_path, but returns None instead of raising when
        # the file does not exist in the local folder or on the Hugging Face
        # repository.
        file_path = Path(model_id) / filename
        if file_path.is_file():
            return file_path
        try:
            return Path(huggingface_hub.hf_hub_download(model_id,
                                                        filename=filename))
        except EntryNotFoundError:
            return None

    @staticmethod
    def load_thresholds(thresholds_path: Path) -> dict[int, float]:
        thresholds_by_category = {}
        with open(thresholds_path, 'r', encoding='utf-8') as thresholds_file:
            reader = csv.DictReader(thresholds_file)
            for line in reader:
                thresholds_by_category[int(line['category'])] = float(
                    line['threshold'])
        return thresholds_by_category

    @staticmethod
    def load_preprocess(preprocess_path: Path) -> tuple[tuple[int, int],
                                                        list[float],
                                                        list[float],
                                                        bool]:
        with open(preprocess_path, 'r', encoding='utf-8') as preprocess_file:
            preprocess = json.load(preprocess_file)
        stages = preprocess['stages']
        normalize_stage = next(
            stage for stage in stages if stage['type'] == 'normalize')
        # v0.9 exports use a 'resize' stage (a stretch to a fixed size); the
        # v1.0 exports use a 'rescale_pad' stage (aspect-ratio-preserving
        # resize followed by padding to a square canvas).
        resize_stage = next(
            stage for stage in stages
            if stage['type'] in ('resize', 'rescale_pad'))
        pad_to_square = resize_stage['type'] == 'rescale_pad'
        size = resize_stage['size']
        if isinstance(size, (list, tuple)):
            width, height = size
        else:
            width = height = int(size)
        return ((width, height), normalize_stage['mean'],
                normalize_stage['std'], pad_to_square)

    def generate_tags(self, image_array: np.ndarray,
                      wd_tagger_settings: dict) -> tuple[tuple, tuple]:
        if 'prediction' in self.output_names:
            probabilities = self.inference_session.run(
                ['prediction'], {self.input_name: image_array}
            )[0][0].astype(np.float32)
        elif 'logits' in self.output_names:
            logits = self.inference_session.run(
                ['logits'], {self.input_name: image_array}
            )[0][0].astype(np.float32)
            probabilities = 1.0 / (1.0 + np.exp(-logits))
        else:
            output_name = self.output_names[0]
            probabilities = self.inference_session.run(
                [output_name], {self.input_name: image_array}
            )[0][0].astype(np.float32)
        tags_and_probabilities = []
        for tag, category, probability in zip(self.tags, self.categories,
                                              probabilities):
            category_threshold = self.thresholds_by_category.get(category, 0.0)
            min_probability = max(wd_tagger_settings['min_probability'],
                                  category_threshold)
            if probability < min_probability:
                continue
            tags_and_probabilities.append((tag, probability))
        tags_and_probabilities = apply_filter_tag_rules(
            tags_and_probabilities, wd_tagger_settings['tags_to_exclude'])
        tags_and_probabilities.sort(key=lambda x: x[1], reverse=True)
        tags_and_probabilities = tags_and_probabilities[
            :wd_tagger_settings['max_tags']]
        if tags_and_probabilities:
            tags, probabilities = zip(*tags_and_probabilities)
        else:
            tags, probabilities = (), ()
        return tags, probabilities


class PixAiTagger(WdTagger):
    def get_model(self):
        precision = self.wd_tagger_settings.get('precision',
                                                TaggerPrecision.FP32)
        return PixAiTaggerModel(self.model_id, precision, self.device_setting,
                                self.caption_settings.get('gpu_index', 0))

    def get_model_inputs(self, image_prompt: str, image) -> np.ndarray:
        pil_image = self.load_image(image)
        if pil_image.mode == 'RGBA':
            canvas = PilImage.new('RGBA', pil_image.size, (255, 255, 255))
            canvas.alpha_composite(pil_image)
            pil_image = canvas.convert('RGB')
        else:
            pil_image = pil_image.convert('RGB')
        if self.model.pad_to_square:
            pil_image = self.resize_and_pad(pil_image, self.model.image_size)
        elif pil_image.size != self.model.image_size:
            pil_image = pil_image.resize(
                self.model.image_size, resample=PilImage.Resampling.BILINEAR)
        image_array = np.asarray(pil_image, dtype=np.float32) / 255.0
        image_array = np.transpose(image_array, (2, 0, 1))
        mean = np.asarray(self.model.mean, dtype=np.float32).reshape(3, 1, 1)
        std = np.asarray(self.model.std, dtype=np.float32).reshape(3, 1, 1)
        image_array = (image_array - mean) / std
        image_array = np.expand_dims(image_array, axis=0)
        return image_array.astype(np.float32)

    @staticmethod
    def resize_and_pad(pil_image: 'PilImage.Image',
                       target_size: tuple[int, int]) -> 'PilImage.Image':
        # Resize the image so its longest side matches the target while keeping
        # the aspect ratio, then paste it centered on a black square canvas.
        target_width, target_height = target_size
        original_width, original_height = pil_image.size
        scale = min(target_width / original_width,
                    target_height / original_height)
        new_width = max(1, round(original_width * scale))
        new_height = max(1, round(original_height * scale))
        resized_image = pil_image.resize(
            (new_width, new_height), resample=PilImage.Resampling.BICUBIC)
        canvas = PilImage.new('RGB', (target_width, target_height), (0, 0, 0))
        left = (target_width - new_width) // 2
        top = (target_height - new_height) // 2
        canvas.paste(resized_image, (left, top))
        return canvas
