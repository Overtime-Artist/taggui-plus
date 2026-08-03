from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtGui import QIcon


def build_caption_text(tags: list[str], natural_language_prompt: str,
                       tag_separator: str) -> str:
    tags_text = tag_separator.join(tags)
    if not natural_language_prompt:
        return tags_text
    if tags_text:
        return f'{tags_text}\n{natural_language_prompt}'
    return f'\n{natural_language_prompt}'


def parse_caption_text(caption_text: str,
                       tag_separator: str) -> tuple[list[str], str]:
    if '\n' in caption_text:
        tags_text, natural_language_prompt = caption_text.split('\n', 1)
    else:
        tags_text = caption_text
        natural_language_prompt = ''
    if tags_text:
        tags = tags_text.split(tag_separator)
        tags = [tag.strip() for tag in tags]
        tags = [tag for tag in tags if tag]
        tags = list(dict.fromkeys(tags))
    else:
        tags = []
    natural_language_prompt = natural_language_prompt.rstrip('\n')
    return tags, natural_language_prompt


@dataclass
class Image:
    path: Path
    dimensions: tuple[int, int] | None
    tags: list[str] = field(default_factory=list)
    natural_language_prompt: str = ''
    thumbnail: QIcon | None = None
    thumbnail_loading: bool = False
    file_modified_time_ns: int | None = None
    caption_file_modified_time_ns: int | None = None
    # Workflow state marking whether the user considers this image fully
    # tagged / complete. This is intentionally not written to the caption
    # `.txt` file; it is stored separately so it never affects training data.
    is_complete: bool = False
