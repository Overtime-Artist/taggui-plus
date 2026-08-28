import json
from pathlib import Path

from utils.completion_store import get_completion_store
from utils.enums import (CaptionDestination, CaptionPosition,
                         NaturalLanguagePosition, ThemeMode)
from utils.settings import (DEFAULT_SETTINGS, REORDER_OPTIONS,
                            SETTINGS_KEY_MIGRATIONS,
                            get_settings,
                            save_tag_library_aliases,
                            save_tag_library_implications,
                            save_tag_library_profiles,
                            save_tag_library_tags,
                            save_tag_library_categories, save_tag_library_category_by_tag,
                            save_hidden_model_ids)

# Sentinel returned by validators when a value cannot be accepted. Using a
# unique object (rather than None) lets validators legitimately return
# ``None``/``0``/``''`` as valid results without ambiguity.
_INVALID = object()


def _validate_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ('true', '1', 'yes'):
            return True
        if lowered in ('false', '0', 'no'):
            return False
    return _INVALID


def _make_int_validator(minimum=None, maximum=None):
    def _validate(value):
        # ``bool`` is a subclass of ``int`` but is never a valid integer
        # setting, so reject it explicitly.
        if isinstance(value, bool):
            return _INVALID
        if isinstance(value, int):
            result = value
        elif isinstance(value, float) and value.is_integer():
            result = int(value)
        elif isinstance(value, str):
            try:
                result = int(value.strip())
            except (ValueError, AttributeError):
                return _INVALID
        else:
            return _INVALID
        if minimum is not None and result < minimum:
            return _INVALID
        if maximum is not None and result > maximum:
            return _INVALID
        return result

    return _validate


def _validate_str(value):
    if isinstance(value, str):
        return value
    return _INVALID


def _make_float_validator(minimum=None, maximum=None):
    def _validate(value):
        # ``bool`` is a subclass of ``int``/``float`` but is never a valid
        # numeric setting, so reject it explicitly.
        if isinstance(value, bool):
            return _INVALID
        if isinstance(value, (int, float)):
            result = float(value)
        elif isinstance(value, str):
            try:
                result = float(value.strip())
            except (ValueError, AttributeError):
                return _INVALID
        else:
            return _INVALID
        if minimum is not None and result < minimum:
            return _INVALID
        if maximum is not None and result > maximum:
            return _INVALID
        return result

    return _validate


def _validate_tag_separator(value):
    # The separator must be a non-empty string and cannot be a newline
    # (mirrors the runtime guard in ``get_tag_separator``).
    if isinstance(value, str) and value and value != '\n':
        return value
    return _INVALID


def _make_choice_validator(valid_choices):
    valid = set(valid_choices)

    def _validate(value):
        if isinstance(value, str) and value in valid:
            return value
        return _INVALID

    return _validate


def _validate_auto_apply_implications(value):
    # 'All images' was renamed to 'All selected images'; map the legacy value
    # so settings exported by older versions still import cleanly.
    if value == 'All images':
        return 'All selected images'
    if value in ('Off', 'Single image only', 'All selected images'):
        return value
    return _INVALID


def _validate_caption_destination_by_model(value):
    if not isinstance(value, dict):
        return _INVALID
    valid_destinations = {member.value for member in CaptionDestination}
    cleaned = {}
    for model_id, destination in value.items():
        if not isinstance(model_id, str):
            continue
        if isinstance(destination, str) and destination in valid_destinations:
            cleaned[model_id] = destination
    return cleaned


# Validators for each importable key in ``general_preferences``. Keys not
# listed here are ignored on import so that unknown/arbitrary keys from a
# tampered or future file cannot pollute QSettings. ``hidden_model_ids`` is
# intentionally omitted because it is imported via ``save_hidden_model_ids``.
_GENERAL_PREFERENCES_VALIDATORS = {
    'font_size': _make_int_validator(minimum=6, maximum=72),
    'theme': _make_choice_validator(member.value for member in ThemeMode),
    'image_list_font_size': _make_int_validator(minimum=6, maximum=72),
    'image_list_file_formats': _validate_str,
    'image_list_image_width': _make_int_validator(minimum=32, maximum=4096),
    'image_list_show_resolution_badge': _validate_bool,
    'image_list_resolution_badge_font_size':
        _make_int_validator(minimum=4, maximum=72),
    'image_list_resolution_badge_transparency':
        _make_int_validator(minimum=0, maximum=100),
    'image_list_show_completion_icon': _validate_bool,
    'image_list_auto_focus_add_tag_box': _validate_bool,
    'max_image_preview_zoom': _make_int_validator(minimum=1, maximum=100),
    'thumbnail_cache_max_size_mb':
        _make_int_validator(minimum=50, maximum=100000),
    'background_thumbnail_caching': _validate_bool,
    'default_batch_reorder': _make_choice_validator(REORDER_OPTIONS),
    'tag_separator': _validate_tag_separator,
    'insert_space_after_tag_separator': _validate_bool,
    'autocomplete_tags': _validate_bool,
    'disable_new_tag_auto_select': _validate_bool,
    'models_directory_path': _validate_str,
    'image_editor_executable_path': _validate_str,
    'image_tags_token_limit': _make_int_validator(minimum=0, maximum=100000),
    'tag_library_keep_or_remove_default_choice':
        _make_choice_validator(('Keep', 'Remove')),
    'tag_library_ask_keep_or_remove': _validate_bool,
    'tag_library_new_tag_default_category_id': _validate_str,
    'ask_before_assigning_new_tag_category': _validate_bool,
    'auto_apply_implications': _validate_auto_apply_implications,
    'variant_grid_view_enabled': _validate_bool,
    'variant_grid_cell_cap': _make_int_validator(minimum=4, maximum=64),
    'variant_grid_overlay_show': _validate_bool,
    'variant_grid_overlay_font_size': _make_int_validator(minimum=1, maximum=99),
    'variant_grid_overlay_transparency':
        _make_int_validator(minimum=0, maximum=100),
}


def _sanitize_general_preferences(category_data: dict) -> dict:
    """Return only the known preference keys whose values pass validation."""
    sanitized = {}
    for key, validator in _GENERAL_PREFERENCES_VALIDATORS.items():
        if key not in category_data:
            continue
        validated = validator(category_data[key])
        if validated is not _INVALID:
            sanitized[key] = validated
    return sanitized


# Validators for the portable Auto-Captioner tuning settings. Machine-specific
# keys (``device``, ``gpu_index``, ``model_id``) are intentionally excluded so
# that a config exported on one computer imports cleanly on another.
_AUTO_CAPTIONER_VALIDATORS = {
    'prompt': _validate_str,
    'caption_start': _validate_str,
    'caption_position':
        _make_choice_validator(member.value for member in CaptionPosition),
    'caption_destination':
        _make_choice_validator(member.value for member in CaptionDestination),
    'caption_destination_by_model': _validate_caption_destination_by_model,
    'natural_language_position':
        _make_choice_validator(
            member.value for member in NaturalLanguagePosition),
    'load_in_4_bit': _validate_bool,
    'remove_tag_separators': _validate_bool,
    # WD/PixAI tagger settings, including the manually-entered Tag filters.
    'wd_tagger_show_probabilities': _validate_bool,
    'wd_tagger_min_probability': _make_float_validator(minimum=0.01, maximum=1),
    'wd_tagger_max_tags': _make_int_validator(minimum=1, maximum=999),
    'wd_tagger_tags_to_exclude': _validate_str,
    # Generation parameters (advanced settings).
    'bad_words': _validate_str,
    'forced_words': _validate_str,
    'min_new_tokens': _make_int_validator(minimum=1, maximum=999),
    'max_new_tokens': _make_int_validator(minimum=1, maximum=999),
    'num_beams': _make_int_validator(minimum=1, maximum=99),
    'length_penalty': _make_float_validator(minimum=-5, maximum=5),
    'do_sample': _validate_bool,
    'temperature': _make_float_validator(minimum=0.01, maximum=2),
    'top_k': _make_int_validator(minimum=0, maximum=200),
    'top_p': _make_float_validator(minimum=0, maximum=1),
    'repetition_penalty': _make_float_validator(minimum=1, maximum=2),
    'no_repeat_ngram_size': _make_int_validator(minimum=0, maximum=5),
    'max_image_tokens': _make_int_validator(minimum=256, maximum=16384),
}


def _sanitize_auto_captioner(category_data: dict) -> dict:
    """Return only the known Auto-Captioner keys whose values pass validation."""
    sanitized = {}
    for key, validator in _AUTO_CAPTIONER_VALIDATORS.items():
        if key not in category_data:
            continue
        validated = validator(category_data[key])
        if validated is not _INVALID:
            sanitized[key] = validated
    return sanitized


# The app defaults for the exportable Auto-Captioner keys. Settings widgets only
# write to QSettings when the user changes them, so a setting left at its
# default is never stored. Without this fallback the export would silently omit
# every unchanged setting; instead we export the default so the file always
# contains the full, self-describing Auto-Captioner configuration.
#
# These mirror the widget defaults in ``widgets/auto_captioner.py``; keep them in
# sync if those defaults change.
AUTO_CAPTIONER_DEFAULTS = {
    'prompt': '',
    'caption_start': '',
    'caption_position': CaptionPosition.BEFORE_FIRST_TAG.value,
    'caption_destination': DEFAULT_SETTINGS['caption_destination'],
    'caption_destination_by_model':
        DEFAULT_SETTINGS['caption_destination_by_model'],
    'natural_language_position':
        DEFAULT_SETTINGS['natural_language_position'],
    'load_in_4_bit': True,
    'remove_tag_separators': True,
    'wd_tagger_show_probabilities': True,
    'wd_tagger_min_probability': 0.4,
    'wd_tagger_max_tags': 30,
    'wd_tagger_tags_to_exclude': '',
    'bad_words': '',
    'forced_words': '',
    'min_new_tokens': 1,
    'max_new_tokens': 100,
    'num_beams': 1,
    'length_penalty': 1.0,
    'do_sample': False,
    'temperature': 1.0,
    'top_k': 50,
    'top_p': 1.0,
    'repetition_penalty': 1.0,
    'no_repeat_ngram_size': 3,
    'max_image_tokens': 1280,
}


# Combined default lookup used when exporting so that settings left at their
# default (and therefore never written to QSettings) are still included. Keys
# absent from this map — custom keyboard shortcuts and window layout — have no
# meaningful scalar default and are only exported when the user has set them.
_EXPORT_DEFAULTS = {**DEFAULT_SETTINGS, **AUTO_CAPTIONER_DEFAULTS}


def _sanitize_keyboard_shortcuts(value) -> dict:
    """Keep only string-to-string entries from an imported shortcuts map."""
    if not isinstance(value, dict):
        return {}
    return {
        action: shortcut
        for action, shortcut in value.items()
        if isinstance(action, str) and isinstance(shortcut, str)
    }


def _make_json_serializable(obj):
    """Convert non-JSON-serializable types to serializable ones."""
    from PySide6.QtCore import QByteArray
    
    if isinstance(obj, QByteArray):
        return obj.toBase64().data().decode('utf-8')
    elif isinstance(obj, dict):
        return {k: _make_json_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_make_json_serializable(item) for item in obj]
    else:
        return obj

EXPORT_CATEGORIES = {
    'general_preferences': {
        'label': 'General Preferences',
        'keys': [
            'font_size', 'theme', 'image_list_font_size', 'image_list_file_formats',
            'image_list_image_width', 'image_list_show_resolution_badge',
            'image_list_resolution_badge_font_size',
            'image_list_resolution_badge_transparency',
            'image_list_show_completion_icon',
            'image_list_auto_focus_add_tag_box', 'max_image_preview_zoom',
            'thumbnail_cache_max_size_mb', 'background_thumbnail_caching',
            'default_batch_reorder',
            'tag_separator',
            'insert_space_after_tag_separator', 'autocomplete_tags',
            'disable_new_tag_auto_select',
            'models_directory_path', 'image_editor_executable_path',
            'image_tags_token_limit',
            'tag_library_keep_or_remove_default_choice', 'tag_library_ask_keep_or_remove',
            'tag_library_new_tag_default_category_id', 'ask_before_assigning_new_tag_category',
            'auto_apply_implications', 'hidden_model_ids',
            'variant_grid_view_enabled', 'variant_grid_cell_cap',
            'variant_grid_overlay_show', 'variant_grid_overlay_font_size',
            'variant_grid_overlay_transparency'
        ]
    },
    'keyboard_shortcuts': {
        'label': 'Keyboard Shortcuts',
        'keys': ['keyboard_shortcuts']
    },
    'auto_captioner': {
        'label': 'Auto-Captioner Settings',
        'keys': [
            'prompt', 'caption_start', 'caption_position',
            'caption_destination', 'caption_destination_by_model',
            'natural_language_position', 'load_in_4_bit',
            'remove_tag_separators', 'wd_tagger_show_probabilities',
            'wd_tagger_min_probability', 'wd_tagger_max_tags',
            'wd_tagger_tags_to_exclude', 'bad_words', 'forced_words',
            'min_new_tokens', 'max_new_tokens', 'num_beams', 'length_penalty',
            'do_sample', 'temperature', 'top_k', 'top_p', 'repetition_penalty',
            'no_repeat_ngram_size', 'max_image_tokens'
        ]
    },
    'ui_layout': {
        'label': 'UI Layout',
        'keys': ['geometry', 'window_state']
    },
    'tag_library': {
        'label': 'Tag Library',
        'keys': [
            'tag_library_tags',
            'tag_library_categories', 'tag_library_category_by_tag',
            'tag_library_aliases', 'tag_library_implications',
            'tag_library_profiles'
        ]
    },
    # Personal-dataset-specific: the set of images the user has marked
    # "complete". Not backed by QSettings keys (it lives in the completion
    # store / completion_cache.json), so export and import are handled
    # specially rather than through the ``keys`` loop.
    'completed_images': {
        'label': 'Completed Image Marks',
        'keys': []
    }
}


# Fields kept for each exported "complete" image entry. Hashes let an imported
# mark re-home onto the matching image after a move/rename or on another
# machine; sizes/mtimes are cheap prefilters used by the scan reconciliation.
_COMPLETION_ENTRY_INT_FIELDS = (
    'image_size', 'image_mtime_ns', 'caption_mtime_ns')
_COMPLETION_ENTRY_STR_FIELDS = ('image_hash', 'caption_hash')


def _sanitize_completed_images(category_data) -> dict:
    """Return a clean ``{path: entry}`` map from imported completion data.

    Accepts the exported shape ``{'entries': {path: entry, ...}}``. Drops any
    malformed paths/entries and coerces each field to the expected type (str or
    int), so a hand-edited or corrupt file can never inject bad values.
    """
    if not isinstance(category_data, dict):
        return {}
    raw_entries = category_data.get('entries')
    if not isinstance(raw_entries, dict):
        return {}
    clean: dict[str, dict] = {}
    for path, entry in raw_entries.items():
        if not isinstance(path, str) or not path or not isinstance(entry, dict):
            continue
        clean_entry = {
            'image_hash': None, 'image_size': None, 'image_mtime_ns': None,
            'caption_hash': None, 'caption_mtime_ns': None,
        }
        for field in _COMPLETION_ENTRY_STR_FIELDS:
            value = entry.get(field)
            if isinstance(value, str):
                clean_entry[field] = value
        for field in _COMPLETION_ENTRY_INT_FIELDS:
            value = entry.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                clean_entry[field] = value
        clean[path] = clean_entry
    return clean


def _normalize_import_data(import_data: dict) -> dict:
    normalized_data = dict(import_data)
    raw_data = import_data.get('data', {})
    if not isinstance(raw_data, dict):
        normalized_data['data'] = {}
        return normalized_data

    data = dict(raw_data)
    if 'local_tags_and_categories' in data and 'tag_library' not in data:
        data['tag_library'] = data.pop('local_tags_and_categories')

    general_preferences = data.get('general_preferences')
    if isinstance(general_preferences, dict):
        migrated_preferences = dict(general_preferences)
        for old_key, new_key in SETTINGS_KEY_MIGRATIONS.items():
            if old_key in migrated_preferences and new_key not in migrated_preferences:
                migrated_preferences[new_key] = migrated_preferences.pop(old_key)
        data['general_preferences'] = migrated_preferences

    tag_library = data.get('tag_library')
    if isinstance(tag_library, dict):
        migrated_tag_library = dict(tag_library)
        legacy_tag_library_keys = {
            'local_tags': 'tag_library_tags',
            'local_tag_categories': 'tag_library_categories',
            'local_tag_category_by_tag': 'tag_library_category_by_tag',
        }
        for old_key, new_key in legacy_tag_library_keys.items():
            if old_key in migrated_tag_library and new_key not in migrated_tag_library:
                migrated_tag_library[new_key] = migrated_tag_library.pop(old_key)
        data['tag_library'] = migrated_tag_library

    normalized_data['data'] = data
    return normalized_data


def export_settings(include_tag_library: bool = False,
                    include_auto_captioner: bool = True,
                    include_completed_images: bool = False) -> dict:
    """Export settings to a dictionary."""
    settings = get_settings()
    export_data = {
        'version': 1,
        'data': {}
    }
    
    for category_key, category_info in EXPORT_CATEGORIES.items():
        if category_key == 'tag_library' and not include_tag_library:
            continue
        if category_key == 'auto_captioner' and not include_auto_captioner:
            continue
        if category_key == 'completed_images':
            # Handled specially below (not backed by QSettings keys).
            continue
        
        category_data = {}
        for setting_key in category_info['keys']:
            value = settings.value(setting_key)
            # Settings widgets only write to QSettings when the user changes a
            # setting, so anything left at its default is never stored. Fall
            # back to the app default so the export is complete and
            # self-describing rather than silently dropping unchanged settings.
            # Keys with no defined default (e.g. custom keyboard shortcuts or
            # window layout) stay absent, which correctly means "use built-in".
            # The tag library is user content (not settings with defaults), so
            # it is exported as-is and an empty library stays omitted.
            if value is None and category_key != 'tag_library':
                value = _EXPORT_DEFAULTS.get(setting_key)
            if value is not None:
                category_data[setting_key] = value
        
        if category_data:
            export_data['data'][category_key] = category_data

    if include_completed_images:
        entries = get_completion_store().export_entries()
        if entries:
            export_data['data']['completed_images'] = {'entries': entries}

    return export_data


def save_settings_to_file(file_path: str, include_tag_library: bool = False,
                          include_auto_captioner: bool = True,
                          include_completed_images: bool = False) -> bool:
    """Export settings to a JSON file."""
    try:
        export_data = export_settings(include_tag_library,
                                      include_auto_captioner,
                                      include_completed_images)
        
        # Convert QByteArray and other non-JSON-serializable types
        export_data = _make_json_serializable(export_data)
        
        with open(file_path, 'w') as f:
            json.dump(export_data, f, indent=2)
        return True
    except Exception as e:
        print(f"Error saving settings: {e}")
        return False


def load_settings_from_file(file_path: str) -> dict | None:
    """Load settings from a JSON file."""
    try:
        with open(file_path, 'r') as f:
            data = json.load(f)
        if not isinstance(data, dict) or 'version' not in data or 'data' not in data:
            return None
        version = data.get('version')
        # Reject files whose version isn't a positive integer. Higher
        # versions are still accepted (best effort) so older builds can read
        # newer exports, but obviously malformed values are refused.
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            return None
        return _normalize_import_data(data)
    except Exception:
        return None


def import_settings(import_data: dict, categories_to_import: list[str]) -> bool:
    """Import settings from loaded data."""
    try:
        settings = get_settings()
        data = _normalize_import_data(import_data).get('data', {})
        
        for category_key in categories_to_import:
            if category_key not in data:
                continue
            
            category_data = data[category_key]
            
            # Special handling for tag library
            if category_key == 'tag_library':
                if not isinstance(category_data, dict):
                    continue
                if 'tag_library_tags' in category_data:
                    save_tag_library_tags(category_data['tag_library_tags'])
                if 'tag_library_categories' in category_data:
                    save_tag_library_categories(category_data['tag_library_categories'])
                if 'tag_library_category_by_tag' in category_data:
                    save_tag_library_category_by_tag(category_data['tag_library_category_by_tag'])
                if 'tag_library_aliases' in category_data:
                    save_tag_library_aliases(category_data['tag_library_aliases'])
                if 'tag_library_implications' in category_data:
                    save_tag_library_implications(
                        category_data['tag_library_implications'])
                if 'tag_library_profiles' in category_data:
                    save_tag_library_profiles(
                        category_data['tag_library_profiles'])
            elif category_key == 'general_preferences':
                if not isinstance(category_data, dict):
                    continue
                # ``hidden_model_ids`` is validated/normalized by its own
                # saver; everything else is validated against a schema so
                # invalid types/ranges/enums and unknown keys are dropped.
                hidden_model_ids = category_data.get('hidden_model_ids')
                if isinstance(hidden_model_ids, list):
                    save_hidden_model_ids(hidden_model_ids)
                for key, value in _sanitize_general_preferences(
                        category_data).items():
                    settings.setValue(key, value)
            elif category_key == 'auto_captioner':
                if not isinstance(category_data, dict):
                    continue
                # Validated against a schema so invalid types/ranges/enums and
                # unknown keys are dropped.
                for key, value in _sanitize_auto_captioner(
                        category_data).items():
                    settings.setValue(key, value)
            elif category_key == 'keyboard_shortcuts':
                if not isinstance(category_data, dict):
                    continue
                shortcuts = _sanitize_keyboard_shortcuts(
                    category_data.get('keyboard_shortcuts'))
                settings.setValue('keyboard_shortcuts', shortcuts)
            elif category_key == 'ui_layout':
                if not isinstance(category_data, dict):
                    continue
                # Handle base64-encoded window state
                # Store as QByteArray so QSettings handles it correctly
                from PySide6.QtCore import QByteArray
                for key in ('geometry', 'window_state'):
                    value = category_data.get(key)
                    if isinstance(value, str):
                        try:
                            # Decode base64 string back to QByteArray and store
                            byte_array = QByteArray.fromBase64(value.encode('utf-8'))
                            settings.setValue(key, byte_array)
                        except Exception as e:
                            print(f"Error importing {key}: {e}")
            elif category_key == 'completed_images':
                # Merge the imported "complete" marks into the completion
                # store (union: never un-marks anything already complete).
                # Persisted immediately so a subsequent directory reload
                # re-homes any foreign paths onto matching images by hash.
                entries = _sanitize_completed_images(category_data)
                if entries:
                    get_completion_store().import_entries(entries)
            else:
                # Unknown category: ignore rather than writing arbitrary keys.
                continue
        
        # Ensure all settings are flushed to disk
        settings.sync()
        
        return True
    except Exception as e:
        print(f"Error importing settings: {e}")
        return False


def get_category_summary(import_data: dict, category_key: str) -> str:
    """Get a human-readable summary of what will be imported from a category."""
    import_data = _normalize_import_data(import_data)
    data = import_data.get('data', {}).get(category_key, {})
    
    if category_key == 'general_preferences':
        theme = data.get('theme', 'N/A')
        font_size = data.get('font_size', 'N/A')
        return f'Theme: {theme}, Font size: {font_size}pt'
    elif category_key == 'keyboard_shortcuts':
        shortcuts = data.get('keyboard_shortcuts', {})
        count = len(shortcuts) if isinstance(shortcuts, dict) else 0
        return f'{count} custom shortcuts'
    elif category_key == 'auto_captioner':
        min_probability = data.get('wd_tagger_min_probability', 'N/A')
        max_tags = data.get('wd_tagger_max_tags', 'N/A')
        tags_to_exclude = data.get('wd_tagger_tags_to_exclude', '')
        filter_count = len([
            part for part in str(tags_to_exclude).replace('\n', ',').split(',')
            if part.strip()
        ])
        return (f'Min probability: {min_probability}, Max tags: {max_tags}, '
                f'Tag filters: {filter_count}')
    elif category_key == 'ui_layout':
        has_geometry = 'geometry' in data
        has_state = 'window_state' in data
        parts = []
        if has_geometry:
            parts.append('window geometry')
        if has_state:
            parts.append('pane layout')
        return ', '.join(parts) if parts else 'None'
    elif category_key == 'tag_library':
        tags = data.get('tag_library_tags', [])
        categories = data.get('tag_library_categories', [])
        aliases = data.get('tag_library_aliases', {})
        implications = data.get('tag_library_implications', {})
        profiles = data.get('tag_library_profiles', {})
        tag_count = len(tags) if isinstance(tags, list) else 0
        cat_count = len(categories) if isinstance(categories, list) else 0
        alias_count = len(aliases) if isinstance(aliases, dict) else 0
        implication_count = len(implications) if isinstance(implications, dict) else 0
        profile_count = len(profiles) if isinstance(profiles, dict) else 0
        return (f'{tag_count} tags, {cat_count} categories, {alias_count} aliases, '
                f'{implication_count} implications, {profile_count} profiles')
    elif category_key == 'completed_images':
        entries = data.get('entries', {})
        count = len(entries) if isinstance(entries, dict) else 0
        return f'{count} completed image{"" if count == 1 else "s"}'
    
    return 'N/A'
