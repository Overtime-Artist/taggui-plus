"""Gelbooru posts and tag types, from the API when an API key is saved, else
from the public web pages.

Every function returns the same normalised shapes either way, so the wiki
dialog doesn't need to know which source was used. A rejected key falls back
to the web pages for the rest of the session.
"""
import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlencode, urlparse

from utils.gelbooru_session import (GELBOORU_BASE_URL, GelbooruApiKeyRejected,
                                    gelbooru_urlopen, has_usable_api_key)

# Matches the website, so page N here is page N there.
POSTS_PER_PAGE = 42
FETCH_WORKER_COUNT = 8
TAG_BATCH_SIZE = 100
# Without a key each tag type is one autocomplete request, so cap how many a
# wiki page may trigger.
MAX_WEB_TAG_TYPE_LOOKUPS = 150
VIDEO_EXTENSIONS = ('mp4', 'webm', 'mkv', 'avi', 'mov', 'swf', 'zip')

# Tag types, keyed by the names used for colouring.
API_TAG_TYPES = {0: 'general', 1: 'artist', 3: 'copyright', 4: 'character',
                 5: 'metadata', 6: 'deprecated'}
AUTOCOMPLETE_TAG_TYPES = {'tag': 'general', 'general': 'general',
                          'artist': 'artist', 'copyright': 'copyright',
                          'character': 'character', 'metadata': 'metadata',
                          'meta': 'metadata', 'deprecated': 'deprecated'}
RATING_LABELS = {'g': 'general', 's': 'sensitive', 'q': 'questionable',
                 'e': 'explicit'}

ARTICLE_PATTERN = re.compile(
    r'<article\b[^>]*class="thumbnail-preview"[^>]*>(.*?)</article>',
    re.IGNORECASE | re.DOTALL)
ARTICLE_ID_PATTERN = re.compile(r'<a\b[^>]*\bid="p(\d+)"', re.IGNORECASE)
ARTICLE_IMG_PATTERN = re.compile(r'<img\b([^>]*)>', re.IGNORECASE | re.DOTALL)
ATTRIBUTE_PATTERN = re.compile(r'([\w-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')',
                               re.DOTALL)
NEXT_PAGE_PATTERN = re.compile(r'<a\b[^>]*\balt="next"', re.IGNORECASE)
IMAGE_CONTAINER_PATTERN = re.compile(
    r'<section\b([^>]*class="[^"]*image-container[^"]*"[^>]*)>',
    re.IGNORECASE | re.DOTALL)
MAIN_IMAGE_PATTERN = re.compile(r'<img\b([^>]*\bid="image"[^>]*)>',
                                re.IGNORECASE | re.DOTALL)
VIDEO_PATTERN = re.compile(r'<video\b([^>]*)>(.*?)</video>',
                           re.IGNORECASE | re.DOTALL)
VIDEO_SOURCE_PATTERN = re.compile(r'<source\b[^>]*\bsrc="([^"]+)"',
                                  re.IGNORECASE)
ORIGINAL_LINK_PATTERN = re.compile(
    r'<a\b[^>]*\bhref="([^"]+)"[^>]*>\s*Original image\s*</a>', re.IGNORECASE)
TAG_ITEM_PATTERN = re.compile(
    r'<li\b[^>]*class="tag-type-([a-z]+)"[^>]*>(.*?)</li>',
    re.IGNORECASE | re.DOTALL)
TAG_ITEM_LINK_PATTERN = re.compile(
    r'href="[^"]*[?&](?:amp;)?tags=([^"&]+)"', re.IGNORECASE)
SIZE_PATTERN = re.compile(r'<li>\s*Size:\s*(\d+)\s*x\s*(\d+)', re.IGNORECASE)
RATING_PATTERN = re.compile(r'<li>\s*Rating:\s*([A-Za-z]+)', re.IGNORECASE)
SCORE_PATTERN = re.compile(r'<li>\s*Score:\s*<span[^>]*>\s*(-?\d+)',
                           re.IGNORECASE)
SOURCE_PATTERN = re.compile(r'<li>\s*Source:\s*(?:<a\b[^>]*href="([^"]*)"|([^<]*))',
                            re.IGNORECASE)
NO_RESULTS_TEXT = 'Nobody here but us chickens'


class GelbooruFetchError(Exception):
    pass


def _attributes(tag_text: str) -> dict:
    values = {}
    for match in ATTRIBUTE_PATTERN.finditer(tag_text or ''):
        value = match.group(3) if match.group(3) is not None else match.group(4)
        values[match.group(1).lower()] = html.unescape(value or '')
    return values


def _absolute(url: str) -> str:
    url = html.unescape(str(url or '').strip())
    if not url:
        return ''
    if url.startswith('//'):
        return f'https:{url}'
    if url.startswith('/'):
        return f'{GELBOORU_BASE_URL}{url}'
    if not url.startswith(('http://', 'https://')):
        return f'{GELBOORU_BASE_URL}/{url}'
    return url


def _read_text(url: str, timeout=12) -> str:
    with gelbooru_urlopen(url, timeout=timeout) as response:
        status = getattr(response, 'status', 200)
        if status != 200:
            raise GelbooruFetchError(f'Gelbooru returned status {status}.')
        return response.read().decode('utf-8', errors='replace')


def _index_url(**params) -> str:
    return f'{GELBOORU_BASE_URL}/index.php?{urlencode(params)}'


def _read_api_json(**params):
    """Returns the parsed JSON, or raises GelbooruApiKeyRejected."""
    text = _read_text(_index_url(page='dapi', q='index', json=1, **params))
    try:
        return json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        raise GelbooruFetchError('Failed to parse the Gelbooru response.')


def file_extension(url: str) -> str:
    return Path(urlparse(str(url or '')).path).suffix.lstrip('.').lower()


def _split_tags(tag_text: str) -> list[str]:
    return [html.unescape(tag) for tag in str(tag_text or '').split() if tag]


def _normalized_rating(rating: str) -> str:
    rating = str(rating or '').strip().lower()
    return RATING_LABELS.get(rating, rating)


def _post_from_api(raw: dict) -> dict:
    file_url = _absolute(raw.get('file_url'))
    sample_url = ''
    if str(raw.get('sample') or '0') not in ('0', 'false', 'False', ''):
        sample_url = _absolute(raw.get('sample_url'))
    extension = file_extension(file_url) or file_extension(raw.get('image'))
    return {
        'id': str(raw.get('id')),
        'width': int(raw.get('width') or 0),
        'height': int(raw.get('height') or 0),
        'rating': _normalized_rating(raw.get('rating')),
        'score': raw.get('score'),
        'source': html.unescape(str(raw.get('source') or '')),
        'file_url': file_url,
        'sample_url': sample_url or file_url,
        'preview_url': _absolute(raw.get('preview_url')),
        'file_ext': extension,
        'tags': _split_tags(raw.get('tags')),
        'tag_types': {},
        'is_video': extension in VIDEO_EXTENSIONS,
    }


def _api_list(payload, key: str) -> list:
    if not isinstance(payload, dict):
        return []
    items = payload.get(key) or []
    if isinstance(items, dict):
        items = [items]
    return [item for item in items if isinstance(item, dict)]


# --- Posts search ------------------------------------------------------------

def search_posts(tags: str, page: int) -> tuple[list, bool]:
    """One page of a tag search: ``(posts, has_more)``. Each post has at least
    ``id``, ``preview_url``, ``tags`` and ``rating``."""
    page = max(1, int(page or 1))
    if has_usable_api_key():
        try:
            return _search_posts_api(tags, page)
        except GelbooruApiKeyRejected:
            pass
    return _search_posts_web(tags, page)


def _search_posts_api(tags: str, page: int) -> tuple[list, bool]:
    payload = _read_api_json(s='post', tags=tags, limit=POSTS_PER_PAGE,
                             pid=page - 1)
    posts = [_post_from_api(raw) for raw in _api_list(payload, 'post')
             if raw.get('id') is not None]
    attributes = payload.get('@attributes') if isinstance(payload, dict) else {}
    try:
        count = int((attributes or {}).get('count'))
        has_more = page * POSTS_PER_PAGE < count
    except (TypeError, ValueError):
        has_more = len(posts) >= POSTS_PER_PAGE
    return posts, has_more


def parse_posts_list_page(page_html: str) -> tuple[list, bool]:
    posts = []
    for article in ARTICLE_PATTERN.finditer(page_html):
        id_match = ARTICLE_ID_PATTERN.search(article.group(1))
        img_match = ARTICLE_IMG_PATTERN.search(article.group(1))
        if id_match is None:
            continue
        image = _attributes(img_match.group(1)) if img_match else {}
        tags, rating, score = [], '', None
        for token in str(image.get('title') or '').split():
            if token.startswith('rating:'):
                rating = token[len('rating:'):]
            elif token.startswith('score:'):
                score = token[len('score:'):]
            else:
                tags.append(token)
        posts.append({'id': id_match.group(1),
                      'preview_url': _absolute(image.get('src')),
                      'tags': tags, 'rating': _normalized_rating(rating),
                      'score': score})
    return posts, bool(NEXT_PAGE_PATTERN.search(page_html))


def _search_posts_web(tags: str, page: int) -> tuple[list, bool]:
    page_html = _read_text(_index_url(page='post', s='list', tags=tags,
                                      pid=(page - 1) * POSTS_PER_PAGE))
    return parse_posts_list_page(page_html)


# --- Single post -------------------------------------------------------------

def fetch_post(post_id: str) -> dict | None:
    """The post's details, with ``tag_types`` filled in, or None if Gelbooru
    has no such post."""
    post_id = str(post_id).strip()
    if has_usable_api_key():
        try:
            payload = _read_api_json(s='post', id=post_id)
            raws = _api_list(payload, 'post')
            if not raws:
                return None
            post = _post_from_api(raws[0])
            post['tag_types'] = fetch_tag_types(post['tags'])
            return post
        except GelbooruApiKeyRejected:
            pass
    page_html = _read_text(_index_url(page='post', s='view', id=post_id))
    return parse_post_page(page_html, post_id)


def parse_post_page(page_html: str, post_id: str = '') -> dict | None:
    # Video posts have no image container, only the <video> player.
    container_match = IMAGE_CONTAINER_PATTERN.search(page_html)
    image_match = MAIN_IMAGE_PATTERN.search(page_html)
    video_match = VIDEO_PATTERN.search(page_html)
    if container_match is None and image_match is None and video_match is None:
        return None
    data = _attributes(container_match.group(1)) if container_match else {}
    if not data.get('data-rating'):
        rating_match = RATING_PATTERN.search(page_html)
        if rating_match is not None:
            data['data-rating'] = rating_match.group(1)
    post_id = data.get('data-id') or str(post_id)
    original_match = ORIGINAL_LINK_PATTERN.search(page_html)
    file_url = _absolute(original_match.group(1)) if original_match else ''
    sample_url = ''
    if image_match is not None:
        sample_url = _absolute(_attributes(image_match.group(1)).get('src'))
    preview_url = ''
    is_video = False
    if image_match is None and video_match is not None:
        is_video = True
        preview_url = _absolute(_attributes(video_match.group(1)).get('poster'))
        if not file_url:
            sources = VIDEO_SOURCE_PATTERN.findall(video_match.group(2))
            file_url = _absolute(sources[0]) if sources else ''
    extension = (str(data.get('data-file-ext') or '').lstrip('.').lower()
                 or file_extension(file_url))
    if file_url and file_extension(file_url) in VIDEO_EXTENSIONS:
        is_video = True
    width = height = 0
    size_match = SIZE_PATTERN.search(page_html)
    if size_match is not None:
        width, height = int(size_match.group(1)), int(size_match.group(2))
    score_match = SCORE_PATTERN.search(page_html)
    source_match = SOURCE_PATTERN.search(page_html)
    source = ''
    if source_match is not None:
        source = html.unescape((source_match.group(1)
                                or source_match.group(2) or '').strip())
    source = source or data.get('data-source', '')
    tags, tag_types = [], {}
    for item in TAG_ITEM_PATTERN.finditer(page_html):
        link_match = TAG_ITEM_LINK_PATTERN.search(item.group(2))
        if link_match is None:
            continue
        name = unquote(html.unescape(link_match.group(1)).replace('+', ' '))
        if not name or name in tag_types:
            continue
        tags.append(name)
        tag_types[name] = AUTOCOMPLETE_TAG_TYPES.get(
            item.group(1).lower(), 'general')
    if not tags:
        tags = _split_tags(data.get('data-tags'))
    return {
        'id': str(post_id),
        'width': width,
        'height': height,
        'rating': _normalized_rating(data.get('data-rating')),
        'score': score_match.group(1) if score_match else data.get('data-score'),
        'source': source,
        'file_url': file_url,
        'sample_url': sample_url or ('' if is_video else file_url),
        'preview_url': preview_url,
        'file_ext': 'mp4' if is_video and extension not in VIDEO_EXTENSIONS
        else extension,
        'tags': tags,
        'tag_types': tag_types,
        'is_video': is_video,
    }


# --- Tag types ---------------------------------------------------------------

def fetch_tag_types(names, interruption_check=None) -> dict:
    """``{tag name: type}`` (type is one of the API_TAG_TYPES values) for as
    many of ``names`` as Gelbooru knows."""
    unique = []
    seen = set()
    for name in names or []:
        name = str(name or '').strip().replace(' ', '_')
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            unique.append(name)
    if not unique:
        return {}
    if has_usable_api_key():
        try:
            return _fetch_tag_types_api(unique, interruption_check)
        except GelbooruApiKeyRejected:
            pass
    return _fetch_tag_types_web(unique[:MAX_WEB_TAG_TYPE_LOOKUPS],
                                interruption_check)


def _fetch_tag_types_api(names: list, interruption_check=None) -> dict:
    types = {}
    for start in range(0, len(names), TAG_BATCH_SIZE):
        if callable(interruption_check) and interruption_check():
            break
        batch = names[start:start + TAG_BATCH_SIZE]
        try:
            payload = _read_api_json(s='tag', names=' '.join(batch),
                                     limit=len(batch))
        except GelbooruApiKeyRejected:
            raise
        except Exception:
            continue
        for tag in _api_list(payload, 'tag'):
            name = html.unescape(str(tag.get('name') or ''))
            try:
                tag_type = API_TAG_TYPES.get(int(tag.get('type')))
            except (TypeError, ValueError):
                tag_type = None
            if name and tag_type:
                types[name] = tag_type
    return types


def autocomplete_entries(term: str, limit: int = 10) -> list:
    """Raw entries from Gelbooru's (key-free) autocomplete."""
    text = _read_text(_index_url(page='autocomplete2', term=term,
                                 type='tag_query', limit=limit), timeout=8)
    entries = json.loads(text) if text.strip() else []
    return [entry for entry in entries if isinstance(entry, dict)] \
        if isinstance(entries, list) else []


def _fetch_tag_types_web(names: list, interruption_check=None) -> dict:
    def lookup(name: str):
        if callable(interruption_check) and interruption_check():
            return name, None
        try:
            entries = autocomplete_entries(name)
        except Exception:
            return name, None
        for entry in entries:
            if str(entry.get('value') or '').casefold() == name.casefold():
                return name, AUTOCOMPLETE_TAG_TYPES.get(
                    str(entry.get('category') or '').lower())
        return name, None

    with ThreadPoolExecutor(max_workers=FETCH_WORKER_COUNT) as executor:
        results = list(executor.map(lookup, names))
    return {name: tag_type for name, tag_type in results if tag_type}
