from PySide6.QtCore import QSettings


# The reorder operations offered in the Batch Reorder Tags dialog. This is the
# single source of truth used by the dialog, the Settings dialog dropdown and
# the "apply default batch reorder" keyboard shortcut.
REORDER_OPTIONS = [
    'Sort Tags Alphabetically',
    'Sort Tags by Frequency',
    'Sort Tags by Tag Category',
    'Reverse Order of Tags',
    'Shuffle Tags Randomly',
    'Move Tags to Front',
]


SETTINGS_KEY_MIGRATIONS = {
    'local_tags': 'tag_library_tags',
    'local_tag_categories': 'tag_library_categories',
    'local_tag_category_by_tag': 'tag_library_category_by_tag',
    'local_tags_remove_prompt_default_action': 'tag_library_keep_or_remove_default_choice',
    'ask_before_removing_local_tags': 'tag_library_ask_keep_or_remove',
    'new_local_tags_default_category_id': 'tag_library_new_tag_default_category_id',
    # Keys renamed to read more consistently with their Settings dialog labels.
    'max_image_zoom': 'max_image_preview_zoom',
    'image_list_type_to_add_tag': 'image_list_auto_focus_add_tag_box',
    'tag_library_remove_prompt_default_action':
        'tag_library_keep_or_remove_default_choice',
    'ask_before_removing_tag_library_tags': 'tag_library_ask_keep_or_remove',
    'new_tag_library_default_category_id':
        'tag_library_new_tag_default_category_id',
}


def migrate_settings():
    settings = QSettings('taggui', 'taggui')
    did_change = False
    for old_key, new_key in SETTINGS_KEY_MIGRATIONS.items():
        if settings.contains(old_key) and not settings.contains(new_key):
            settings.setValue(new_key, settings.value(old_key))
            did_change = True
    # Migrate auto_apply_implications from bool to string
    if settings.contains('auto_apply_implications'):
        val = settings.value('auto_apply_implications')
        if isinstance(val, bool):
            settings.setValue('auto_apply_implications',
                              'Single image only' if val else 'Off')
            did_change = True
        # The 'All images' option was renamed to 'All selected images' to
        # clarify that it applies to the current selection, not the whole
        # directory. Migrate any existing configs so they keep working.
        elif val == 'All images':
            settings.setValue('auto_apply_implications', 'All selected images')
            did_change = True
    if did_change:
        settings.sync()


# Defaults for settings that are accessed from multiple places.
DEFAULT_SETTINGS = {
    'font_size': 16,
    'theme': 'Dark',
    'image_list_font_size': 16,
    # Common image formats that are supported in PySide6.
    'image_list_file_formats': 'bmp, gif, jpg, jpeg, png, tif, tiff, webp',
    'image_list_image_width': 200,
    'image_list_sort_by': 'Path',
    'image_list_sort_order': 'Ascending',
    'image_list_show_resolution_badge': True,
    'image_list_resolution_badge_font_size': 10,
    'image_list_resolution_badge_transparency': 55,
    'image_list_show_completion_icon': True,
    # When enabled, typing a printable character while the Images pane
    # thumbnails have focus moves focus to the Add Tag input in the Image
    # Tags pane and starts the tag with that character.
    'image_list_auto_focus_add_tag_box': True,
    'tag_separator': ',',
    'insert_space_after_tag_separator': True,
    'autocomplete_tags': True,
    'models_directory_path': '',
    'image_editor_executable_path': '',
    'caption_destination': 'Tags',
    'caption_destination_by_model': {},
    'natural_language_position': 'Overwrite current text',
    'tag_library_tags': [],
    'tag_library_categories': [],
    'tag_library_category_by_tag': {},
    'tag_library_aliases': {},
    'tag_library_implications': {},
    'tag_library_profiles': {},
    'tag_library_keep_or_remove_default_choice': 'Keep',
    'tag_library_sort_by': 'Name (A\u2013Z)',
    'tag_library_ask_keep_or_remove': True,
    'tag_library_new_tag_default_category_id': '',
    'ask_before_assigning_new_tag_category': True,
    'auto_apply_implications': 'All selected images',
    'default_batch_reorder': REORDER_OPTIONS[0],
    'image_tags_token_limit': 75,
    'max_image_preview_zoom': 6,
    'hidden_model_ids': [],  # List of model IDs to hide from the model dropdown
    'thumbnail_cache_max_size_mb': 500,
    # When enabled, thumbnails for images that have never been cached are
    # generated silently in a low-priority background thread after a directory
    # loads, instead of only when an image scrolls into view.
    'background_thumbnail_caching': True,
    # Max perceptual-hash (dHash) Hamming distance for the "Find Duplicates"
    # tool to treat two images as near-duplicates. 0 = only visually identical
    # (exact / byte-identical). This is the default; raise it in the dialog to
    # also catch visually similar near-duplicates.
    'duplicate_detection_strictness': 0,
}


def get_settings() -> QSettings:
    settings = QSettings('taggui', 'taggui')
    return settings


def get_tag_separator() -> str:
    settings = get_settings()
    tag_separator = settings.value(
        'tag_separator', defaultValue=DEFAULT_SETTINGS['tag_separator'],
        type=str)
    if tag_separator == '\n':
        tag_separator = DEFAULT_SETTINGS['tag_separator']
        settings.setValue('tag_separator', tag_separator)
    insert_space_after_tag_separator = settings.value(
        'insert_space_after_tag_separator',
        defaultValue=DEFAULT_SETTINGS['insert_space_after_tag_separator'],
        type=bool)
    if insert_space_after_tag_separator:
        tag_separator += ' '
    return tag_separator


def get_tag_library_tags() -> list[str]:
    settings = get_settings()
    raw_tags = settings.value(
        'tag_library_tags', defaultValue=DEFAULT_SETTINGS['tag_library_tags'], type=list)
    if raw_tags is None:
        return []
    tag_library_tags = []
    for tag in raw_tags:
        normalized_tag = str(tag).strip()
        if not normalized_tag:
            continue
        tag_library_tags.append(normalized_tag)
    # Remove duplicates while preserving order.
    tag_library_tags = list(dict.fromkeys(tag_library_tags))
    return tag_library_tags


def save_tag_library_tags(tags: list[str]):
    normalized_tags = []
    seen_tags = set()
    for tag in tags:
        normalized_tag = tag.strip()
        if not normalized_tag or normalized_tag in seen_tags:
            continue
        normalized_tags.append(normalized_tag)
        seen_tags.add(normalized_tag)
    settings = get_settings()
    settings.setValue('tag_library_tags', normalized_tags)


def get_tag_library_categories() -> list[dict[str, str]]:
    settings = get_settings()
    raw_categories = settings.value(
        'tag_library_categories',
        defaultValue=DEFAULT_SETTINGS['tag_library_categories'], type=list)
    if not raw_categories:
        return []
    categories = []
    seen_ids = set()
    for category in raw_categories:
        if not isinstance(category, dict):
            continue
        category_id = str(category.get('id', '')).strip()
        name = str(category.get('name', '')).strip()
        color = str(category.get('color', '')).strip()
        if not category_id or not name or category_id in seen_ids:
            continue
        categories.append({'id': category_id, 'name': name, 'color': color})
        seen_ids.add(category_id)
    return categories


def save_tag_library_categories(categories: list[dict[str, str]]):
    normalized_categories = []
    seen_ids = set()
    for category in categories:
        category_id = str(category.get('id', '')).strip()
        name = str(category.get('name', '')).strip()
        color = str(category.get('color', '')).strip()
        if not category_id or not name or category_id in seen_ids:
            continue
        normalized_categories.append({
            'id': category_id,
            'name': name,
            'color': color
        })
        seen_ids.add(category_id)
    settings = get_settings()
    settings.setValue('tag_library_categories', normalized_categories)


def get_tag_library_category_by_tag() -> dict[str, str]:
    settings = get_settings()
    raw_mapping = settings.value(
        'tag_library_category_by_tag',
        defaultValue=DEFAULT_SETTINGS['tag_library_category_by_tag'])
    if not isinstance(raw_mapping, dict):
        return {}
    category_by_tag = {}
    for tag, category_id in raw_mapping.items():
        normalized_tag = str(tag).strip()
        normalized_category_id = str(category_id).strip()
        if not normalized_tag or not normalized_category_id:
            continue
        category_by_tag[normalized_tag] = normalized_category_id
    return category_by_tag


def save_tag_library_category_by_tag(category_by_tag: dict[str, str]):
    normalized_mapping = {}
    for tag, category_id in category_by_tag.items():
        normalized_tag = tag.strip()
        normalized_category_id = category_id.strip()
        if not normalized_tag or not normalized_category_id:
            continue
        normalized_mapping[normalized_tag] = normalized_category_id
    settings = get_settings()
    settings.setValue('tag_library_category_by_tag', normalized_mapping)


def get_tag_library_aliases() -> dict[str, str]:
    settings = get_settings()
    value = settings.value(
        'tag_library_aliases',
        defaultValue=DEFAULT_SETTINGS['tag_library_aliases'])
    if isinstance(value, dict):
        return value
    return {}


def save_tag_library_aliases(aliases: dict[str, str]):
    settings = get_settings()
    settings.setValue('tag_library_aliases', aliases)
    settings.sync()


def get_tag_library_implications() -> dict[str, list[str]]:
    settings = get_settings()
    value = settings.value(
        'tag_library_implications',
        defaultValue=DEFAULT_SETTINGS['tag_library_implications'])
    if isinstance(value, dict):
        return value
    return {}


def save_tag_library_implications(implications: dict[str, list[str]]):
    settings = get_settings()
    settings.setValue('tag_library_implications', implications)
    settings.sync()


def get_tag_library_profiles() -> dict[str, dict[str, str]]:
    settings = get_settings()
    value = settings.value(
        'tag_library_profiles',
        defaultValue=DEFAULT_SETTINGS['tag_library_profiles'])
    if isinstance(value, dict):
        return value
    return {}


def save_tag_library_profiles(profiles: dict[str, dict[str, str]]):
    settings = get_settings()
    settings.setValue('tag_library_profiles', profiles)
    settings.sync()


def get_hidden_model_ids() -> list[str]:
    """Get the list of model IDs that should be hidden from the model dropdown."""
    settings = get_settings()
    raw_hidden = settings.value(
        'hidden_model_ids', defaultValue=DEFAULT_SETTINGS['hidden_model_ids'], type=list)
    if raw_hidden is None:
        return []
    hidden_models = []
    for model_id in raw_hidden:
        normalized_id = str(model_id).strip()
        if normalized_id:
            hidden_models.append(normalized_id)
    return hidden_models


def save_hidden_model_ids(hidden_models: list[str]):
    """Save the list of model IDs that should be hidden from the model dropdown."""
    normalized_models = []
    seen_ids = set()
    for model_id in hidden_models:
        normalized_id = str(model_id).strip()
        if normalized_id and normalized_id not in seen_ids:
            normalized_models.append(normalized_id)
            seen_ids.add(normalized_id)
    settings = get_settings()
    settings.setValue('hidden_model_ids', normalized_models)
