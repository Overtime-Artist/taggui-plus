from auto_captioning.auto_captioning_model import AutoCaptioningModel
from auto_captioning.models.florence_2 import Florence2, Florence2Promptgen
from auto_captioning.models.joycaption import Joycaption
from auto_captioning.models.kosmos_2 import Kosmos2
from auto_captioning.models.llava_1_point_5 import Llava1Point5
from auto_captioning.models.llava_llama_3 import LlavaLlama3
from auto_captioning.models.llava_next import (LlavaNext34b, LlavaNextMistral,
                                               LlavaNextVicuna)
from auto_captioning.models.moondream import Moondream1, Moondream2
from auto_captioning.models.pixai_tagger import PixAiTagger
from auto_captioning.models.phi_3_vision import Phi3Vision
from auto_captioning.models.qwen_vl import (Qwen2Vl, Qwen2Point5Vl, Qwen3Vl,
                                            Qwen3VlThinking)
from auto_captioning.models.wd_tagger import WdTagger

MODELS = [
    'fancyfeast/llama-joycaption-beta-one-hf-llava',
    'microsoft/Florence-2-large-ft',
    'microsoft/Florence-2-large',
    'microsoft/Florence-2-base-ft',
    'microsoft/Florence-2-base',
    'MiaoshouAI/Florence-2-large-PromptGen-v2.0',
    'MiaoshouAI/Florence-2-base-PromptGen-v2.0',
    'Qwen/Qwen3-VL-8B-Thinking',
    'Qwen/Qwen3-VL-8B-Instruct',
    'Qwen/Qwen3-VL-4B-Thinking',
    'Qwen/Qwen3-VL-4B-Instruct',
    'Felldude/Qwen3-VL-8B-Instruct-Uncensored-V2',
    'Felldude/Qwen3-VL-4B-Instruct-Uncensored',
    'Qwen/Qwen2.5-VL-7B-Instruct',
    'Qwen/Qwen2.5-VL-3B-Instruct',
    'Qwen/Qwen2-VL-7B-Instruct',
    'Qwen/Qwen2-VL-2B-Instruct',
    'microsoft/Phi-3-vision-128k-instruct',
    'llava-hf/llava-v1.6-mistral-7b-hf',
    'llava-hf/llava-v1.6-vicuna-7b-hf',
    'llava-hf/llava-v1.6-vicuna-13b-hf',
    'llava-hf/llava-v1.6-34b-hf',
    'xtuner/llava-llama-3-8b-v1_1-transformers',
    'vikhyatk/moondream2',
    'SmilingWolf/wd-eva02-large-tagger-v3',
    'SmilingWolf/wd-vit-large-tagger-v3',
    'SmilingWolf/wd-swinv2-tagger-v3',
    'SmilingWolf/wd-convnext-tagger-v3',
    'SmilingWolf/wd-vit-tagger-v3',
    'deepghs/pixai-tagger-v0.9-onnx',
    'llava-hf/llava-1.5-7b-hf',
    'llava-hf/llava-1.5-13b-hf',
    'llava-hf/bakLlava-v1-hf'
]


def get_model_class(model_id: str) -> type[AutoCaptioningModel]:
    lowercase_model_id = model_id.lower()
    if 'florence' in lowercase_model_id:
        if 'promptgen' in lowercase_model_id:
            return Florence2Promptgen
        return Florence2
    if 'joycaption' in lowercase_model_id:
        return Joycaption
    if 'kosmos' in lowercase_model_id:
        return Kosmos2
    if 'llava-v1.6-34b' in lowercase_model_id:
        return LlavaNext34b
    if 'llava-v1.6-mistral' in lowercase_model_id:
        return LlavaNextMistral
    if 'llava-v1.6-vicuna' in lowercase_model_id:
        return LlavaNextVicuna
    if 'llava-llama-3' in lowercase_model_id:
        return LlavaLlama3
    if 'llava' in lowercase_model_id:
        return Llava1Point5
    if 'moondream1' in lowercase_model_id:
        return Moondream1
    if 'moondream2' in lowercase_model_id:
        return Moondream2
    if 'phi-3' in lowercase_model_id:
        return Phi3Vision
    if 'qwen3-vl' in lowercase_model_id or 'qwen3_vl' in lowercase_model_id:
        if 'thinking' in lowercase_model_id:
            return Qwen3VlThinking
        return Qwen3Vl
    if 'qwen2.5-vl' in lowercase_model_id or 'qwen2_5-vl' in lowercase_model_id:
        return Qwen2Point5Vl
    if 'qwen2-vl' in lowercase_model_id:
        return Qwen2Vl
    if 'pixai' in lowercase_model_id and 'tagger' in lowercase_model_id:
        return PixAiTagger
    if 'wd' in lowercase_model_id and 'tagger' in lowercase_model_id:
        return WdTagger
    return AutoCaptioningModel
