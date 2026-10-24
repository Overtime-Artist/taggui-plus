import sys
from pathlib import Path

import numpy as np
from transformers import AutoModelForCausalLM, BatchFeature

from auto_captioning.auto_captioning_model import AutoCaptioningModel
from utils.image import Image
from utils.utils import list_with_and


class Florence2(AutoCaptioningModel):
    use_safetensors = None
    transformers_model_class = AutoModelForCausalLM
    task_prompts = [
        '<CAPTION>',
        '<DETAILED_CAPTION>',
        '<MORE_DETAILED_CAPTION>',
        '<OCR>'
    ]
    default_prompt = task_prompts[2]

    def get_model(self):
        """Load the model, patching the remote code first if it is the stale
        version that is incompatible with Transformers >= 4.54.

        The base implementation loads the model and only patches afterwards,
        but Florence-2's stale code crashes while the model is being
        constructed, so the patch has to happen between the failed first load
        and a second attempt.
        """
        model_load_arguments = self.get_model_load_arguments()
        try:
            return self.load_model(model_load_arguments)
        except AttributeError as exception:
            if '_supports_sdpa' not in str(exception):
                raise
            if not self.patch_source_code():
                raise
            print('Patched the Florence-2 source code for Transformers '
                  'compatibility. Reloading the model...')
            return self.load_model(model_load_arguments)

    def patch_source_code(self) -> bool:
        """Patch the remote code for compatibility with Transformers >= 4.54.

        Microsoft's Florence-2 code defines `_supports_sdpa` (and
        `_supports_flash_attn_2`) as properties that read
        `self.language_model`, but `PreTrainedModel.__init__` reads those
        attributes before `self.language_model` is assigned. The property then
        raises `AttributeError`, which Python surfaces as a missing attribute
        and crashes model construction. Guard the properties so they return
        `True` until `language_model` exists.
        """
        module = next((module for name, module in sys.modules.items()
                       if 'modeling_florence2' in name), None)
        if module is None or not getattr(module, '__file__', None):
            return False
        source_path = Path(module.__file__)
        source = source_path.read_text(encoding='utf-8')
        original_source = source
        guard = ('        if not hasattr(self, "language_model"):\n'
                 '            return True\n')
        for attribute in ('_supports_flash_attn_2', '_supports_sdpa'):
            return_line = f'        return self.language_model.{attribute}'
            if return_line in source and guard + return_line not in source:
                source = source.replace(return_line, guard + return_line, 1)
        if source == original_source:
            return False
        source_path.write_text(source, encoding='utf-8')
        del sys.modules[module.__name__]
        return True

    def get_additional_error_message(self) -> str | None:
        if self.prompt and self.prompt not in self.task_prompts:
            quoted_task_prompts = [f'"{task_prompt}"'
                                   for task_prompt in self.task_prompts]
            return (f'This model only supports the following prompts: '
                    f'{list_with_and(quoted_task_prompts)}. The default '
                    f'prompt is "{self.default_prompt}".')
        if self.caption_start:
            return 'This model does not support `Start caption with`.'
        return None

    def get_default_prompt(self) -> str:
        return self.default_prompt

    @staticmethod
    def get_additional_generation_parameters() -> dict:
        # Florence-2's remote code implements its own decoder with the legacy
        # tuple key/value cache, which is incompatible with the `Cache` objects
        # that Transformers >= 4.54 passes during generation (it crashes with
        # `'NoneType' object has no attribute 'shape'`). Disabling the cache
        # keeps `past_key_values` as `None` so the legacy code path is never
        # reached. Captions are identical; only generation speed is affected.
        return {'use_cache': False}


class Florence2Promptgen(Florence2):
    use_safetensors = True
    task_prompts = [
        '<GENERATE_TAGS>',
        '<CAPTION>',
        '<DETAILED_CAPTION>',
        '<MORE_DETAILED_CAPTION>',
        '<ANALYZE>',
        '<MIXED_CAPTION>',
        '<MIXED_CAPTION_PLUS>',
    ]
    default_prompt = task_prompts[1]

    def get_model_inputs(self, image_prompt: str,
                         image: Image) -> BatchFeature | dict | np.ndarray:
        model_inputs = super().get_model_inputs(image_prompt, image)
        model_inputs = {
            'input_ids': model_inputs['input_ids'],
            'pixel_values': model_inputs['pixel_values'],
        }
        return model_inputs
