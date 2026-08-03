import re

import torch
from transformers import (AutoProcessor, BatchFeature,
                          Qwen2VLForConditionalGeneration,
                          Qwen2_5_VLForConditionalGeneration,
                          Qwen3VLForConditionalGeneration)

from typing import TYPE_CHECKING

from auto_captioning.auto_captioning_model import AutoCaptioningModel
from utils.image import Image

if TYPE_CHECKING:
    # Imported only for type hints to avoid a circular import at runtime.
    import auto_captioning.captioning_thread as captioning_thread


class QwenVl(AutoCaptioningModel):
    dtype = torch.bfloat16
    # This model supports the "Max image tokens" advanced setting, which caps
    # the number of image patches ("visual tokens") the processor produces.
    # Qwen2-VL/Qwen2.5-VL default to an enormous cap (up to 16384 tokens), so a
    # large photo is turned into a huge number of tokens. The vision encoder's
    # attention then allocates several extra gigabytes of VRAM *during*
    # captioning, which overflows a 16 GB card into slow shared system memory.
    # Capping the tokens keeps peak VRAM roughly constant regardless of the
    # input image size. The lower bound below matches the official Qwen2.5-VL
    # examples.
    supports_image_token_limit = True
    # Each visual token corresponds to a 28x28 patch of pixels.
    pixels_per_image_token = 28 * 28
    min_pixels = 256 * pixels_per_image_token

    def __init__(self,
                 captioning_thread_: 'captioning_thread.CaptioningThread',
                 caption_settings: dict):
        super().__init__(captioning_thread_, caption_settings)
        self.input_length = None
        # Convert the "Max image tokens" setting into the `max_pixels` value the
        # processor expects. Fall back to a safe default if the setting is
        # missing (e.g. for callers that build settings dicts directly).
        max_image_tokens = caption_settings.get('max_image_tokens', 1280)
        self.max_pixels = max_image_tokens * self.pixels_per_image_token

    def get_processor(self):
        return AutoProcessor.from_pretrained(
            self.model_id, trust_remote_code=True,
            min_pixels=self.min_pixels, max_pixels=self.max_pixels)

    def get_processor_cache_key(self):
        # The processor bakes in the "Max image tokens" setting via max_pixels,
        # so the cached processor must be rebuilt whenever this value changes.
        return self.max_pixels

    @staticmethod
    def get_default_prompt() -> str:
        return 'Describe the image in one sentence.'

    def format_prompt(self, prompt: str) -> str:
        conversation = [
            {
                'role': 'user',
                'content': [
                    {'type': 'image'},
                    {'type': 'text', 'text': prompt}
                ]
            }
        ]
        return self.processor.apply_chat_template(
            conversation, tokenize=False, add_generation_prompt=True)

    def get_input_text(self, image_prompt: str) -> str:
        return image_prompt + self.caption_start

    def get_model_inputs(self, image_prompt: str,
                         image: Image) -> BatchFeature:
        model_inputs = super().get_model_inputs(image_prompt, image)
        self.input_length = model_inputs['input_ids'].shape[1]
        return model_inputs

    def get_caption_from_generated_tokens(
            self, generated_token_ids: torch.Tensor, image_prompt: str) -> str:
        # The model echoes the prompt tokens, so keep only the newly generated
        # tokens before decoding.
        generated_token_ids = generated_token_ids[:, self.input_length:]
        return super().get_caption_from_generated_tokens(
            generated_token_ids, image_prompt)


class Qwen2Vl(QwenVl):
    transformers_model_class = Qwen2VLForConditionalGeneration


class Qwen2Point5Vl(QwenVl):
    transformers_model_class = Qwen2_5_VLForConditionalGeneration


class Qwen3Vl(QwenVl):
    # Qwen3-VL uses the same processor/prompt conventions as Qwen2.5-VL, so it
    # reuses the shared QwenVl behaviour (image-token capping, prompt
    # formatting, prompt-echo trimming). Only the underlying model class
    # differs. Requires Transformers >= 4.57.0.
    transformers_model_class = Qwen3VLForConditionalGeneration


class Qwen3VlThinking(Qwen3Vl):
    # "Thinking" Qwen3-VL variants emit a reasoning trace wrapped in
    # `<think>...</think>` before the actual answer. For captioning we only want
    # the final answer, so strip the reasoning trace out of the generated text.
    _think_pattern = re.compile(r'<think>.*?</think>', flags=re.DOTALL)

    @staticmethod
    def get_default_prompt() -> str:
        return 'Describe the image in one sentence.'

    def postprocess_generated_text(self, generated_text: str) -> str:
        # Remove any complete `<think>...</think>` block(s).
        generated_text = self._think_pattern.sub('', generated_text)
        # If generation was cut off inside the reasoning trace (an opening
        # `<think>` with no closing tag), drop everything up to and including
        # the opening tag so a half-finished thought is never saved as a
        # caption.
        if '<think>' in generated_text:
            generated_text = generated_text.rsplit('<think>', 1)[0]
        # A leftover closing tag can remain if the opening tag was trimmed with
        # the echoed prompt; drop everything up to and including it.
        if '</think>' in generated_text:
            generated_text = generated_text.rsplit('</think>', 1)[-1]
        return generated_text.strip()
