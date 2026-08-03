import csv
import json
from pathlib import Path

import huggingface_hub
import numpy as np
from PIL import Image as PilImage
from onnxruntime import InferenceSession

from auto_captioning.models.wd_tagger import (KAOMOJIS, WdTagger,
                                              apply_filter_tag_rules)


class PixAiTaggerModel:
    def __init__(self, model_id: str):
        model_path = self.get_model_file_path(model_id, 'model.onnx')
        selected_tags_path = self.get_model_file_path(model_id,
                                                      'selected_tags.csv')
        thresholds_path = self.get_model_file_path(model_id, 'thresholds.csv')
        preprocess_path = self.get_model_file_path(model_id, 'preprocess.json')
        self.inference_session = InferenceSession(model_path)
        self.input_name = self.inference_session.get_inputs()[0].name
        self.output_names = [output.name for output in
                             self.inference_session.get_outputs()]
        self.tags = []
        self.categories = []
        self.thresholds_by_category = self.load_thresholds(thresholds_path)
        self.image_size, self.mean, self.std = self.load_preprocess(
            preprocess_path)
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
                                                        list[float]]:
        with open(preprocess_path, 'r', encoding='utf-8') as preprocess_file:
            preprocess = json.load(preprocess_file)
        resize_stage = next(
            stage for stage in preprocess['stages']
            if stage['type'] == 'resize')
        normalize_stage = next(
            stage for stage in preprocess['stages']
            if stage['type'] == 'normalize')
        width, height = resize_stage['size']
        return (width, height), normalize_stage['mean'], normalize_stage['std']

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
        return PixAiTaggerModel(self.model_id)

    def get_model_inputs(self, image_prompt: str, image) -> np.ndarray:
        pil_image = self.load_image(image)
        if pil_image.mode == 'RGBA':
            canvas = PilImage.new('RGBA', pil_image.size, (255, 255, 255))
            canvas.alpha_composite(pil_image)
            pil_image = canvas.convert('RGB')
        else:
            pil_image = pil_image.convert('RGB')
        if pil_image.size != self.model.image_size:
            pil_image = pil_image.resize(
                self.model.image_size, resample=PilImage.Resampling.BILINEAR)
        image_array = np.asarray(pil_image, dtype=np.float32) / 255.0
        image_array = np.transpose(image_array, (2, 0, 1))
        mean = np.asarray(self.model.mean, dtype=np.float32).reshape(3, 1, 1)
        std = np.asarray(self.model.std, dtype=np.float32).reshape(3, 1, 1)
        image_array = (image_array - mean) / std
        image_array = np.expand_dims(image_array, axis=0)
        return image_array.astype(np.float32)
