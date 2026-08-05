import html
import json
import re
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, quote, unquote, urlparse, urlencode
from urllib.request import urlopen

from PySide6.QtCore import (QBuffer, QByteArray, QIODevice, QThread, Qt,
                            QTimer, QUrl, Signal, Slot)
from PySide6.QtGui import (QColor, QDesktopServices, QFontMetrics, QPainter,
                           QPainterPath, QPixmap, QStandardItem,
                           QStandardItemModel)
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel,
                               QCompleter, QLineEdit, QPushButton, QTextBrowser,
                               QVBoxLayout)

from models.tag_library_model import TagLibraryModel
from dialogs.wiki_dialog_base import (AUTOCOMPLETE_ANY_SUFFIX_PATTERN, AUTOCOMPLETE_COUNT_SUFFIX_PATTERN,
                                     BaseWikiDialog, TAG_GROUP_NORMALIZED_PREFIX,
                                     TAG_GROUP_SEARCH_PREFIX)

DANBOORU_BASE_URL = 'https://danbooru.donmai.us'
# The Danbooru help page describing what tag deprecation means. Linked from the
# "This tag is deprecated..." notice and opened in the user's external browser.
DEPRECATION_NOTICE_URL = (
    f'{DANBOORU_BASE_URL}/wiki_pages/help:deprecation_notice')
WIKI_INTERNAL_LINK_PREFIX = 'taggui-wiki:'
WIKI_INTERNAL_LINK_PREFIX_LEGACY = 'taggui-wiki://'
# Internal link schemes for the in-app post browser. A single post is opened
# with ``taggui-post:id=<n>`` and a paginated tag search with
# ``taggui-posts:tags=<tag>&page=<n>``. These never leave the app; they are
# routed by handle_anchor_clicked just like the wiki links above.
POST_INTERNAL_LINK_PREFIX = 'taggui-post:'
POSTS_INTERNAL_LINK_PREFIX = 'taggui-posts:'
HEADING_WITH_ANCHOR_PATTERN = re.compile(
    r'^h([1-6])(?:#([A-Za-z0-9_-]+))?\.\s*(.*)$', re.IGNORECASE
)
POST_REFERENCE_PATTERN = re.compile(r'!?post\s+#(\d+)', re.IGNORECASE)
ASSET_REFERENCE_PATTERN = re.compile(r'!?asset\s+#(\d+)', re.IGNORECASE)
POOL_REFERENCE_PATTERN = re.compile(r'pool\s+#(\d+)', re.IGNORECASE)
BULLETED_REFERENCE_LINE_PATTERN = re.compile(
    r'^\s*[\*\-•]\s+(!?(?:post|asset)\s+#\d+)(.*)$',
    re.IGNORECASE
)
TOKEN_PATTERN = re.compile(
    r'"[^"]+":\[https?://[^\]\s]+\]'
    r'|"[^"]+":https?://[^\s"<\])]+'
    r'|https?://\S+|\[\[[^\]]+\]\]|\{\{[^}]+\}\}|pool\s+#\d+'
    r'|!?(?:post|asset)\s+#\d+|"[^"]+":#[A-Za-z0-9_-]+',
    re.IGNORECASE
)
NAMED_EXTERNAL_LINK_PATTERN = re.compile(
    r'^"(?P<label>[^"]+)":'
    r'(?:\[(?P<url_bracket>https?://[^\]\s]+)\]'
    r'|(?P<url_bare>https?://[^\s"<\])]+))$',
    re.IGNORECASE
)
WIKI_LINK_SUFFIX_PATTERN = re.compile(r"[A-Za-z0-9_']+")
FORMATTING_PATTERNS = (
    (re.compile(r'\[b\](.*?)\[/b\]', re.IGNORECASE | re.DOTALL), 'strong'),
    (re.compile(r'\[i\](.*?)\[/i\]', re.IGNORECASE | re.DOTALL), 'em'),
    (re.compile(r'\[u\](.*?)\[/u\]', re.IGNORECASE | re.DOTALL), 'u'),
    (re.compile(r'\[s\](.*?)\[/s\]', re.IGNORECASE | re.DOTALL), 's'),
)
# DText spoilers: [spoiler]hidden text[/spoiler]. Rendered as a black bar that
# hides the text (matching Danbooru's default state). QTextBrowser has no
# :hover support, so the text is revealed by selecting/highlighting it.
SPOILER_PATTERN = re.compile(r'\[spoiler\](.*?)\[/spoiler\]',
                             re.IGNORECASE | re.DOTALL)
# Anchor style attribute inside a spoiler, so links are hidden by the black bar
# too (Qt would otherwise draw them in the visible link colour).
SPOILER_ANCHOR_STYLE_PATTERN = re.compile(r'(<a\b[^>]*?)\s+style="[^"]*"',
                                          re.IGNORECASE)
MAX_PREVIEW_POSTS = 300
# How many post/asset/thumbnail requests to run at once when loading a wiki
# page. Danbooru pages can reference many posts, and the previous code fetched
# them one at a time, which made loading feel far slower than a web browser
# (which downloads everything in parallel). Fetching them concurrently is the
# single biggest speed-up. Kept modest to stay friendly to Danbooru's servers.
FETCH_WORKER_COUNT = 8
# Number of recent post thumbnails shown in the "Posts" section at the bottom
# of a wiki page.
WIKI_PREVIEW_POST_COUNT = 20
# Number of post thumbnails per page in the full post-search view.
POSTS_PER_PAGE = 20
# Typing this prefix in the search box (e.g. "posts:no humans") searches posts
# for that tag directly instead of opening its wiki page.
POSTS_SEARCH_PREFIX = 'posts:'
# Longest edge (in pixels) of Danbooru's "sample"/large image, which is what we
# embed on a single-post view. We never display it larger than this so the image
# is never upscaled (which would look blurry).
POST_VIEW_MAX_IMAGE_EDGE = 850
# Danbooru tag categories → display colours (light theme, dark theme).
TAG_CATEGORY_COLORS = {
    'copyright': ('#a800aa', '#c973cd'),
    'character': ('#00aa00', '#35c745'),
    'artist': ('#a00000', '#e05a5a'),
    'general': ('#0073ff', '#6baef6'),
    'meta': ('#ea7d00', '#f0a52a'),
}


def image_url_to_data_url(image_url: str, interruption_check=None) -> str:
    """Download an image and return it as a base64 ``data:`` URL.

    QTextBrowser does not fetch remote images over the network, so every image
    shown in the wiki/post views is embedded inline as a data URL. Returns an
    empty string on any failure (missing URL, network error, non-image, or when
    ``interruption_check`` reports the work should stop)."""
    if not image_url:
        return ''
    if callable(interruption_check) and interruption_check():
        return ''
    if image_url.startswith('/'):
        image_url = f'{DANBOORU_BASE_URL}{image_url}'
    elif not image_url.startswith('http://') and not image_url.startswith('https://'):
        image_url = f'{DANBOORU_BASE_URL}/{image_url}'
    try:
        with urlopen(image_url, timeout=10) as response:
            if response.status != 200:
                return ''
            image_bytes = response.read()
            if not image_bytes:
                return ''
            content_type = response.headers.get_content_type() or 'image/jpeg'
    except Exception:
        return ''
    if not content_type.startswith('image/'):
        return ''
    encoded_image = b64encode(image_bytes).decode('ascii')
    return f'data:{content_type};base64,{encoded_image}'


def build_post_summaries_concurrently(posts: list, interruption_check=None) -> list:
    """Turn raw Danbooru post dicts into lightweight summaries with an inline
    thumbnail data URL, downloading the thumbnails in parallel."""
    valid_posts = [post for post in posts
                   if isinstance(post, dict) and post.get('id') is not None]
    if not valid_posts:
        return []

    def thumbnail_for(post: dict) -> str:
        preview_url = str(post.get('preview_file_url')
                          or post.get('large_file_url')
                          or post.get('file_url') or '')
        return image_url_to_data_url(preview_url, interruption_check)

    summaries = []
    with ThreadPoolExecutor(max_workers=FETCH_WORKER_COUNT) as executor:
        thumbnail_futures = {
            post.get('id'): executor.submit(thumbnail_for, post)
            for post in valid_posts
        }
        for post in valid_posts:
            post_id = post.get('id')
            summaries.append({
                'id': post_id,
                'thumbnail_data_url': thumbnail_futures[post_id].result(),
                'image_width': post.get('image_width') or 0,
                'image_height': post.get('image_height') or 0,
            })
    return summaries


class DanbooruWikiFetchThread(QThread):
    fetch_succeeded = Signal(str, dict, dict, dict, list)
    fetch_failed = Signal(str, str)
    fetch_not_found = Signal(str, str)

    def __init__(self, request_key: str, normalized_tag: str = '',
                 wiki_page_id: str = ''):
        super().__init__()
        self.request_key = request_key
        self.normalized_tag = normalized_tag
        self.wiki_page_id = str(wiki_page_id).strip()

    def run(self):
        if self.wiki_page_id:
            self.fetch_wiki_page_by_id()
            return
        self.fetch_wiki_page_by_title()

    def fetch_wiki_page_by_title(self):
        query = urlencode({'search[title]': self.normalized_tag, 'limit': 1})
        api_url = f'{DANBOORU_BASE_URL}/wiki_pages.json?{query}'
        try:
            with urlopen(api_url, timeout=10) as response:
                if response.status != 200:
                    self.fetch_failed.emit(
                        self.request_key,
                        f'Danbooru returned status {response.status}.')
                    return
                payload = response.read().decode('utf-8', errors='replace')
        except Exception as exception:
            self.fetch_failed.emit(self.request_key, str(exception))
            return
        try:
            wiki_pages = json.loads(payload)
        except json.JSONDecodeError:
            self.fetch_failed.emit(
                self.request_key, 'Failed to parse Danbooru response.')
            return
        if not wiki_pages:
            self.fetch_not_found.emit(self.request_key, self.normalized_tag)
            return
        wiki_page = wiki_pages[0]
        self.fetch_post_details_and_emit(wiki_page)

    def fetch_wiki_page_by_id(self):
        api_url = f'{DANBOORU_BASE_URL}/wiki_pages/{quote(self.wiki_page_id)}.json'
        try:
            with urlopen(api_url, timeout=10) as response:
                if response.status != 200:
                    self.fetch_failed.emit(
                        self.request_key,
                        f'Danbooru returned status {response.status}.')
                    return
                payload = response.read().decode('utf-8', errors='replace')
        except Exception as exception:
            self.fetch_failed.emit(self.request_key, str(exception))
            return
        try:
            wiki_page = json.loads(payload)
        except json.JSONDecodeError:
            self.fetch_failed.emit(
                self.request_key, 'Failed to parse Danbooru response.')
            return
        if not isinstance(wiki_page, dict) or not wiki_page.get('title'):
            self.fetch_not_found.emit(self.request_key, self.wiki_page_id)
            return
        self.fetch_post_details_and_emit(wiki_page)

    def fetch_post_details_and_emit(self, wiki_page: dict):
        resolved_title = str(wiki_page.get('title') or '').strip()
        if resolved_title:
            self.normalized_tag = resolved_title
        post_ids = []
        asset_ids = []
        for post_match in POST_REFERENCE_PATTERN.finditer(str(wiki_page.get('body') or '')):
            post_id = post_match.group(1)
            if post_id not in post_ids:
                post_ids.append(post_id)
        for asset_match in ASSET_REFERENCE_PATTERN.finditer(str(wiki_page.get('body') or '')):
            asset_id = asset_match.group(1)
            if asset_id not in asset_ids:
                asset_ids.append(asset_id)
        capped_post_ids = post_ids[:MAX_PREVIEW_POSTS]
        capped_asset_ids = asset_ids[:MAX_PREVIEW_POSTS]
        if self.isInterruptionRequested():
            return
        # Fetch the post details, asset details and tag-relation lookups
        # concurrently rather than one after another. Each of these is an
        # independent network request; running them in parallel (like a web
        # browser does) makes loading dramatically faster on pages that
        # reference several posts or assets.
        with ThreadPoolExecutor(max_workers=FETCH_WORKER_COUNT) as executor:
            # Fetch the referenced posts' and assets' metadata in batched
            # requests (one request per ~200 ids) instead of one request per
            # post/asset. Danbooru rate-limits (HTTP 429) a burst of dozens of
            # individual post lookups, which previously left many thumbnails
            # missing on image-heavy wiki pages; a single batched query for all
            # ids avoids the rate limit entirely.
            posts_future = executor.submit(
                self.fetch_posts_details_batched, capped_post_ids)
            assets_future = executor.submit(
                self.fetch_assets_details_batched, capped_asset_ids)
            relations_future = executor.submit(
                self.fetch_tag_relations, resolved_title)
            # Fetch whether the tag itself is deprecated. This flag lives on the
            # tags API (not the wiki page), so it needs its own request; run it
            # in the same parallel batch as the other lookups.
            is_deprecated_future = executor.submit(
                self.fetch_tag_is_deprecated, resolved_title)
            # Fetch the recent posts for the "Posts" section shown at the
            # bottom of the wiki page in the same parallel batch.
            preview_posts_future = executor.submit(
                self.fetch_preview_posts, resolved_title)
            post_details_by_id = posts_future.result()
            asset_details_by_id = assets_future.result()
            relations = relations_future.result()
            is_deprecated = is_deprecated_future.result()
            wiki_posts = preview_posts_future.result()
        wiki_page['_taggui_alias_names'] = relations['aliases']
        wiki_page['_taggui_implication_names'] = relations['implications']
        wiki_page['_taggui_implied_by_names'] = relations['implied_by']
        wiki_page['_taggui_is_deprecated'] = is_deprecated
        self.fetch_succeeded.emit(self.request_key, wiki_page, post_details_by_id,
                                  asset_details_by_id, wiki_posts)

    def fetch_preview_posts(self, tag: str) -> list:
        """Fetch a handful of recent posts for the wiki page's Posts section.

        Skipped for tag-group pages (which are collections of tags, not a tag
        with posts) and when the dialog is being torn down."""
        tag = str(tag or '').strip()
        if (not tag or tag.casefold().startswith('tag_group:')
                or self.isInterruptionRequested()):
            return []
        query = urlencode({'tags': tag, 'limit': WIKI_PREVIEW_POST_COUNT})
        posts = self.fetch_json_list(f'{DANBOORU_BASE_URL}/posts.json?{query}')
        return build_post_summaries_concurrently(
            posts, self.isInterruptionRequested)

    def fetch_json_list(self, api_url: str) -> list:
        try:
            with urlopen(api_url, timeout=10) as response:
                if response.status != 200:
                    return []
                data = json.loads(response.read().decode('utf-8', errors='replace'))
        except Exception:
            return []
        return data if isinstance(data, list) else []

    def fetch_tag_relations(self, tag_name: str) -> dict:
        tag_name = str(tag_name or '').strip()
        empty = {'aliases': [], 'implications': [], 'implied_by': []}
        if not tag_name or self.isInterruptionRequested():
            return empty
        aliases_url = (
            f'{DANBOORU_BASE_URL}/tag_aliases.json?' + urlencode({
                'search[consequent_name]': tag_name,
                'search[status]': 'active',
                'limit': 100}))
        implications_url = (
            f'{DANBOORU_BASE_URL}/tag_implications.json?' + urlencode({
                'search[antecedent_name]': tag_name,
                'search[status]': 'active',
                'limit': 100}))
        implied_by_url = (
            f'{DANBOORU_BASE_URL}/tag_implications.json?' + urlencode({
                'search[consequent_name]': tag_name,
                'search[status]': 'active',
                'limit': 100}))
        # Fetch the three relation endpoints in parallel too.
        with ThreadPoolExecutor(max_workers=3) as executor:
            aliases_future = executor.submit(self.fetch_json_list, aliases_url)
            implications_future = executor.submit(
                self.fetch_json_list, implications_url)
            implied_by_future = executor.submit(
                self.fetch_json_list, implied_by_url)
            aliases_raw = aliases_future.result()
            implications_raw = implications_future.result()
            implied_by_raw = implied_by_future.result()

        def extract_names(items: list, key: str) -> list:
            names = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get(key) or '').strip()
                if name and name not in names:
                    names.append(name)
            return names

        return {
            'aliases': extract_names(aliases_raw, 'antecedent_name'),
            'implications': extract_names(implications_raw, 'consequent_name'),
            'implied_by': extract_names(implied_by_raw, 'antecedent_name'),
        }

    def fetch_tag_is_deprecated(self, tag_name: str) -> bool:
        """Look up whether the tag is marked deprecated on Danbooru.

        The deprecation flag is a property of the tag itself (returned by the
        tags API), not of its wiki page, so it needs its own request. Tag
        groups are pseudo-tags with no tag record, so skip them."""
        tag_name = str(tag_name or '').strip()
        if (not tag_name or tag_name.casefold().startswith('tag_group:')
                or self.isInterruptionRequested()):
            return False
        tags_url = (f'{DANBOORU_BASE_URL}/tags.json?' + urlencode({
            'search[name]': tag_name, 'limit': 1}))
        tags = self.fetch_json_list(tags_url)
        if not tags or not isinstance(tags[0], dict):
            return False
        return bool(tags[0].get('is_deprecated'))

    @staticmethod
    def _post_preview_url(post_data: dict) -> str:
        return str(post_data.get('preview_file_url')
                   or post_data.get('large_file_url')
                   or post_data.get('file_url') or '')

    @staticmethod
    def _asset_preview_url(asset_data: dict) -> str:
        variants = asset_data.get('variants')
        if not isinstance(variants, list):
            return ''
        preferred_variant = None
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            if str(variant.get('type') or '') == '180x180':
                preferred_variant = variant
                break
        if preferred_variant is None and variants:
            preferred_variant = variants[0]
        if isinstance(preferred_variant, dict):
            return str(preferred_variant.get('url') or '')
        return ''

    def fetch_posts_details_batched(self, post_ids: list) -> dict:
        """Fetch metadata + thumbnails for many posts using batched requests.

        Danbooru rate-limits (HTTP 429) a burst of individual ``/posts/{id}``
        lookups, so instead we ask for all ids at once via ``id:1,2,3`` (which
        counts as a single tag), at most ~200 ids per request, then download the
        thumbnail images concurrently (the image CDN is not rate-limited)."""
        details_by_id = {}
        if not post_ids or self.isInterruptionRequested():
            return details_by_id
        posts_by_id = {}
        batch_size = 200
        for start in range(0, len(post_ids), batch_size):
            if self.isInterruptionRequested():
                return details_by_id
            chunk = post_ids[start:start + batch_size]
            query = urlencode({'tags': 'id:' + ','.join(chunk),
                               'limit': batch_size})
            posts_url = f'{DANBOORU_BASE_URL}/posts.json?{query}'
            try:
                with urlopen(posts_url, timeout=15) as response:
                    if response.status != 200:
                        continue
                    posts = json.loads(
                        response.read().decode('utf-8', errors='replace'))
            except Exception:
                continue
            if isinstance(posts, list):
                for post in posts:
                    if isinstance(post, dict) and post.get('id') is not None:
                        posts_by_id[str(post.get('id'))] = post

        def build(post_id: str):
            post_data = posts_by_id.get(post_id)
            if not isinstance(post_data, dict):
                return None
            return {
                'post': post_data,
                'thumbnail_data_url': self.fetch_thumbnail_data_url(
                    self._post_preview_url(post_data))
            }
        with ThreadPoolExecutor(max_workers=FETCH_WORKER_COUNT) as executor:
            futures = {post_id: executor.submit(build, post_id)
                       for post_id in posts_by_id}
            for post_id, future in futures.items():
                data = future.result()
                if data is not None:
                    details_by_id[post_id] = data
        return details_by_id

    def fetch_assets_details_batched(self, asset_ids: list) -> dict:
        """Fetch metadata + thumbnails for many media assets in batched
        requests, for the same rate-limit reasons as
        ``fetch_posts_details_batched``."""
        details_by_id = {}
        if not asset_ids or self.isInterruptionRequested():
            return details_by_id
        assets_by_id = {}
        batch_size = 200
        for start in range(0, len(asset_ids), batch_size):
            if self.isInterruptionRequested():
                return details_by_id
            chunk = asset_ids[start:start + batch_size]
            query = urlencode({'search[id]': ','.join(chunk),
                               'limit': batch_size})
            assets_url = f'{DANBOORU_BASE_URL}/media_assets.json?{query}'
            try:
                with urlopen(assets_url, timeout=15) as response:
                    if response.status != 200:
                        continue
                    assets = json.loads(
                        response.read().decode('utf-8', errors='replace'))
            except Exception:
                continue
            if isinstance(assets, list):
                for asset in assets:
                    if isinstance(asset, dict) and asset.get('id') is not None:
                        assets_by_id[str(asset.get('id'))] = asset

        def build(asset_id: str):
            asset_data = assets_by_id.get(asset_id)
            if not isinstance(asset_data, dict):
                return None
            return {
                'asset': asset_data,
                'thumbnail_data_url': self.fetch_thumbnail_data_url(
                    self._asset_preview_url(asset_data))
            }
        with ThreadPoolExecutor(max_workers=FETCH_WORKER_COUNT) as executor:
            futures = {asset_id: executor.submit(build, asset_id)
                       for asset_id in assets_by_id}
            for asset_id, future in futures.items():
                data = future.result()
                if data is not None:
                    details_by_id[asset_id] = data
        return details_by_id

    def fetch_thumbnail_data_url(self, image_url: str):
        return image_url_to_data_url(image_url, self.isInterruptionRequested)


class DanbooruPostsSearchThread(QThread):
    """Fetches one page of a tag search and its thumbnails for the grid view."""
    posts_ready = Signal(str, str, int, list)
    posts_failed = Signal(str, str)

    def __init__(self, request_key: str, tags: str, page: int):
        super().__init__()
        self.request_key = request_key
        self.tags = str(tags or '').strip()
        self.page = max(1, int(page or 1))

    def run(self):
        query = urlencode({'tags': self.tags, 'page': self.page,
                           'limit': POSTS_PER_PAGE})
        posts_url = f'{DANBOORU_BASE_URL}/posts.json?{query}'
        try:
            with urlopen(posts_url, timeout=10) as response:
                if response.status != 200:
                    self.posts_failed.emit(
                        self.request_key,
                        f'Danbooru returned status {response.status}.')
                    return
                payload = response.read().decode('utf-8', errors='replace')
        except Exception as exception:
            self.posts_failed.emit(self.request_key, str(exception))
            return
        try:
            posts = json.loads(payload)
        except json.JSONDecodeError:
            self.posts_failed.emit(
                self.request_key, 'Failed to parse Danbooru response.')
            return
        if not isinstance(posts, list):
            posts = []
        summaries = build_post_summaries_concurrently(
            posts, self.isInterruptionRequested)
        self.posts_ready.emit(self.request_key, self.tags, self.page, summaries)


class DanbooruPostViewThread(QThread):
    """Fetches a single post's details plus its (scaled) full image."""
    post_ready = Signal(str, dict)
    post_failed = Signal(str, str)

    def __init__(self, request_key: str, post_id: str):
        super().__init__()
        self.request_key = request_key
        self.post_id = str(post_id).strip()

    def run(self):
        post_url = f'{DANBOORU_BASE_URL}/posts/{quote(self.post_id)}.json'
        try:
            with urlopen(post_url, timeout=10) as response:
                if response.status != 200:
                    self.post_failed.emit(
                        self.request_key,
                        f'Danbooru returned status {response.status}.')
                    return
                payload = response.read().decode('utf-8', errors='replace')
        except Exception as exception:
            self.post_failed.emit(self.request_key, str(exception))
            return
        try:
            post = json.loads(payload)
        except json.JSONDecodeError:
            self.post_failed.emit(
                self.request_key, 'Failed to parse Danbooru response.')
            return
        if not isinstance(post, dict) or post.get('id') is None:
            self.post_failed.emit(self.request_key, 'Post not found.')
            return
        # Prefer the "sample" (large) image, which is already scaled down by
        # Danbooru, so we don't embed a multi-megabyte original as a data URL.
        image_url = str(post.get('large_file_url') or post.get('file_url')
                        or post.get('preview_file_url') or '')
        post['_taggui_image_data_url'] = image_url_to_data_url(
            image_url, self.isInterruptionRequested)
        self.post_ready.emit(self.request_key, post)


class DanbooruTagAutocompleteThread(QThread):
    suggestions_ready = Signal(str, list)
    tag_group_titles_cache = None

    def __init__(self, query_text: str):
        super().__init__()
        self.query_text = query_text

    def run(self):
        original_query_text = self.query_text.strip()
        if not original_query_text:
            self.suggestions_ready.emit(self.query_text, [])
            return
        query_text = original_query_text.replace(' ', '_')
        is_tag_group_query = (
            original_query_text.casefold().startswith(TAG_GROUP_SEARCH_PREFIX)
            or query_text.casefold().startswith(TAG_GROUP_NORMALIZED_PREFIX)
        )
        if is_tag_group_query:
            self.fetch_tag_group_suggestions(query_text)
            return
        query = urlencode({
            'search[query]': query_text,
            'search[type]': 'tag_query',
            'limit': 15
        })
        tags_url = f'{DANBOORU_BASE_URL}/autocomplete.json?{query}'
        try:
            with urlopen(tags_url, timeout=8) as response:
                if response.status != 200:
                    self.suggestions_ready.emit(self.query_text, [])
                    return
                payload = response.read().decode('utf-8', errors='replace')
                tags = json.loads(payload)
        except Exception:
            self.suggestions_ready.emit(self.query_text, [])
            return
        suggestions = []
        for tag in tags:
            tag_name = str(tag.get('value') or tag.get('name') or '').strip()
            if not tag_name:
                continue
            tag_count = int(
                tag.get('post_count')
                or (tag.get('tag') or {}).get('post_count')
                or 0
            )
            suggestions.append({
                'name': tag_name,
                'post_count': tag_count
            })
        # The 'tag_query' autocomplete only returns tags that currently have
        # posts, so wiki-only entries (e.g. deprecated tags like "meme_attire"
        # with a post_count of 0) never show up. Fetch the 'wiki_page'
        # autocomplete as well and merge in any entries not already present so
        # those wikis become discoverable.
        existing_names = {suggestion['name'].casefold()
                          for suggestion in suggestions}
        for wiki_suggestion in self.fetch_wiki_page_suggestions(query_text):
            if wiki_suggestion['name'].casefold() in existing_names:
                continue
            existing_names.add(wiki_suggestion['name'].casefold())
            suggestions.append(wiki_suggestion)
        self.suggestions_ready.emit(self.query_text, suggestions)

    def fetch_wiki_page_suggestions(self, query_text: str) -> list:
        query = urlencode({
            'search[query]': query_text,
            'search[type]': 'wiki_page',
            'limit': 15
        })
        wiki_url = f'{DANBOORU_BASE_URL}/autocomplete.json?{query}'
        try:
            with urlopen(wiki_url, timeout=8) as response:
                if response.status != 200:
                    return []
                entries = json.loads(
                    response.read().decode('utf-8', errors='replace'))
        except Exception:
            return []
        wiki_suggestions = []
        for entry in entries:
            name = str(entry.get('value') or entry.get('label') or '').strip()
            if not name:
                continue
            wiki_suggestions.append({
                'name': name,
                'post_count': 0,
                'is_wiki_only': True
            })
        return wiki_suggestions

    def fetch_tag_group_suggestions(self, normalized_query_text: str):
        query_text = normalized_query_text
        if query_text.casefold().startswith(TAG_GROUP_SEARCH_PREFIX):
            query_text = (TAG_GROUP_NORMALIZED_PREFIX
                          + query_text[len(TAG_GROUP_SEARCH_PREFIX):].lstrip())
        if not query_text.casefold().startswith(TAG_GROUP_NORMALIZED_PREFIX):
            self.suggestions_ready.emit(self.query_text, [])
            return

        group_filter = query_text[len(TAG_GROUP_NORMALIZED_PREFIX):].strip()
        group_filter = group_filter.replace(' ', '_').casefold()
        group_titles = self.get_tag_group_titles()
        if not group_titles:
            self.suggestions_ready.emit(self.query_text, [])
            return

        suggestions = []
        for title in group_titles:
            if not title.casefold().startswith(TAG_GROUP_NORMALIZED_PREFIX):
                continue
            suffix = title[len(TAG_GROUP_NORMALIZED_PREFIX):].casefold()
            if group_filter and group_filter not in suffix:
                continue
            suggestions.append({
                'name': title,
                'post_count': 0,
                'is_tag_group': True
            })
        suggestions.sort(key=lambda suggestion: suggestion['name'])
        suggestions = suggestions[:15]
        self.suggestions_ready.emit(self.query_text, suggestions)

    def get_tag_group_titles(self) -> list[str]:
        if DanbooruTagAutocompleteThread.tag_group_titles_cache is not None:
            return DanbooruTagAutocompleteThread.tag_group_titles_cache
        url = f'{DANBOORU_BASE_URL}/wiki_pages/tag_groups'
        try:
            with urlopen(url, timeout=10) as response:
                if response.status != 200:
                    DanbooruTagAutocompleteThread.tag_group_titles_cache = []
                    return []
                page_html = response.read().decode('utf-8', errors='replace')
        except Exception:
            DanbooruTagAutocompleteThread.tag_group_titles_cache = []
            return []
        matches = re.findall(
            r'/wiki_pages/(tag_group(?::|%3A)[^"?#&<\s]+)',
            page_html,
            flags=re.IGNORECASE
        )
        deduped_titles = []
        seen = set()
        for match in matches:
            title = unquote(str(match).strip())
            if not title or title in seen:
                continue
            seen.add(title)
            deduped_titles.append(title)
        DanbooruTagAutocompleteThread.tag_group_titles_cache = deduped_titles
        return deduped_titles


class DanbooruWikiDialog(BaseWikiDialog):
    SITE_NAME = 'Danbooru'
    WINDOW_TITLE = 'Danbooru Wiki'
    EMPTY_TAG_MESSAGE = ('Enter a tag name in the search box to view wiki '
                         'pages from Danbooru.')
    AUTOCOMPLETE_THREAD_CLASS = DanbooruTagAutocompleteThread

    def __init__(self, parent, tag: str,
                 tag_library_model: TagLibraryModel | None = None,
                 add_to_library_callback=None,
                 add_to_selected_images_callback=None,
                 selected_images_have_tag_callback=None):
        super().__init__(parent)
        self._init_wiki_state(tag_library_model, add_to_library_callback,
                              add_to_selected_images_callback,
                              selected_images_have_tag_callback)
        self.post_details_by_id = {}
        self.asset_details_by_id = {}
        self._stored_wiki_body = ''
        self._stored_wiki_title = ''
        self._stored_other_names = []
        self._stored_alias_names = []
        self._stored_implication_names = []
        self._stored_implied_by_names = []
        self._stored_wiki_posts = []
        self._stored_is_deprecated = False
        # State for the in-app post browser (posts search grid + single post
        # view). ``_active_composer`` recomposes whatever view is currently on
        # screen so a window resize can reflow it (grid columns / image size).
        self._active_composer = None
        self._stored_post = None
        self._posts_view_state = None
        # Ordered post ids of the most recently shown grid, so a single-post
        # view opened from it can offer prev/next and a "back to search" link.
        self._grid_context = None
        self._posts_fetch_thread = None
        self._active_posts_threads = []
        self._torn_down = False
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(150)
        self._resize_timer.timeout.connect(self._rerender_current_page)
        self._build_wiki_ui()
        self._finish_wiki_init(tag)


    def _teardown(self):
        """Stop the loading animation and all worker threads. Safe to call more
        than once, and called from both ``done()`` (Escape/OK/programmatic
        close) and ``closeEvent`` (window close button) so cleanup always runs
        before the dialog is destroyed — important now that the dialog deletes
        itself on close."""
        if self._torn_down:
            return
        self._torn_down = True
        # Stop the animated (indeterminate) loading bar first, on the GUI
        # thread. That bar runs an internal timer; switching it off here means
        # no timer is left running while we block below waiting for the worker
        # threads — which avoids Qt's "QBasicTimer::stop: Failed. Possibly
        # trying to stop from a different thread" warning during teardown.
        self.show_status_message('')
        self._resize_timer.stop()
        self.stop_all_fetch_threads()
        self.stop_all_autocomplete_threads(wait=True)
        self.stop_all_posts_threads()

    def done(self, result):
        self._teardown()
        super().done(result)

    def closeEvent(self, event):
        self._teardown()
        super().closeEvent(event)

    def stop_all_posts_threads(self):
        for posts_thread in list(self._active_posts_threads):
            try:
                if posts_thread.isRunning():
                    posts_thread.requestInterruption()
                    posts_thread.quit()
                    posts_thread.wait(12000)
            except RuntimeError:
                continue
        self._active_posts_threads.clear()
        self._posts_fetch_thread = None

    @Slot()
    def handle_posts_thread_finished(self):
        finished_thread = self.sender()
        if finished_thread is not None:
            finished_thread.deleteLater()
            if finished_thread in self._active_posts_threads:
                self._active_posts_threads.remove(finished_thread)
        if finished_thread is self._posts_fetch_thread:
            self._posts_fetch_thread = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._active_composer is not None:
            self._resize_timer.start()

    def _rerender_current_page(self):
        if self._active_composer is None:
            return
        scroll_pos = self.content_browser.verticalScrollBar().value()
        self._set_browser_html(self._active_composer())
        self.content_browser.verticalScrollBar().setValue(scroll_pos)

    def _internal_tag_link_html(self, tag_name: str) -> str:
        name = str(tag_name or '').strip()
        if not name:
            return ''
        normalized = self.normalize_tag(name)
        label = name.replace('_', ' ')
        internal_url = f'{WIKI_INTERNAL_LINK_PREFIX}{quote(normalized)}'
        return f'<a href="{internal_url}">{html.escape(label)}</a>'

    def _render_chip_data_url(self, text: str, is_dark: bool) -> tuple:
        """Render a single synonym chip (rounded rectangle + text) to a PNG
        data URL. QTextBrowser ignores padding/border-radius on inline HTML,
        so we draw the chip as an image to get true rounded corners. Returns
        (data_url, logical_width, logical_height)."""
        scale = max(1.0, self.devicePixelRatioF())
        font = self.content_browser.font()
        metrics = QFontMetrics(font)
        padding_x = 8
        padding_y = 3
        text_width = metrics.horizontalAdvance(text)
        text_height = metrics.height()
        logical_width = text_width + padding_x * 2
        logical_height = text_height + padding_y * 2

        pixmap = QPixmap(round(logical_width * scale), round(logical_height * scale))
        pixmap.setDevicePixelRatio(scale)
        pixmap.fill(Qt.GlobalColor.transparent)

        background = QColor('#3a3f4b' if is_dark else '#eef1f5')
        text_color = QColor('#d7dae0' if is_dark else '#333333')

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setFont(font)
        path = QPainterPath()
        path.addRoundedRect(0.5, 0.5, logical_width - 1.0,
                            logical_height - 1.0, 5.0, 5.0)
        painter.fillPath(path, background)
        painter.setPen(text_color)
        painter.drawText(0, 0, logical_width, logical_height,
                         int(Qt.AlignmentFlag.AlignCenter), text)
        painter.end()

        byte_array = QByteArray()
        buffer = QBuffer(byte_array)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        pixmap.save(buffer, 'PNG')
        buffer.close()
        encoded = bytes(byte_array.toBase64()).decode('ascii')
        return (f'data:image/png;base64,{encoded}',
                logical_width, logical_height)

    def _build_other_names_html(self, is_dark: bool) -> str:
        names = [str(name).strip() for name in (self._stored_other_names or [])
                 if str(name).strip()]
        if not names:
            return ''
        # All chips share the same height (same font), so compute a tight,
        # pixel-based line-height: just the chip height plus a small gap so that
        # wrapped rows sit as close together as the inline gap between chips.
        metrics = QFontMetrics(self.content_browser.font())
        chip_pixel_height = metrics.height() + 6
        row_gap = 5
        line_height = chip_pixel_height + row_gap
        chips = []
        for name in names:
            label = name.replace('_', ' ')
            data_url, chip_width, chip_height = self._render_chip_data_url(
                label, is_dark)
            # Let the browser copy this chip image as its text.
            self.content_browser.register_image_text(data_url, label)
            chips.append(
                f'<img src="{data_url}" width="{chip_width}" '
                f'height="{chip_height}" '
                f'style="vertical-align: middle;">')
        return (f'<div style="margin: 2px 0 3px 0; line-height: {line_height}px;">'
                f'{"&nbsp;".join(chips)}</div>')

    def _build_deprecated_note_html(self, is_dark: bool) -> str:
        """The small "This tag is deprecated..." footnote, mirroring the notice
        Danbooru shows on the wiki page for a deprecated tag."""
        if not self._stored_is_deprecated:
            return ''
        note_color = '#9aa0aa' if is_dark else '#777777'
        link_color = '#6baef6' if is_dark else '#0066cc'
        deprecated_link = (
            f'<a href="{html.escape(DEPRECATION_NOTICE_URL, quote=True)}" '
            f'style="color: {link_color};">deprecated</a>')
        return (
            f'<p style="font-size: 0.9em; color: {note_color}; '
            f'margin: 12px 0 0 0;"><i>This tag is {deprecated_link} and '
            f"can't be added to new posts.</i></p>")

    def _build_relations_html(self, is_dark: bool) -> str:
        note_color = '#9aa0aa' if is_dark else '#777777'
        sections = []

        def join_links(names: list) -> str:
            links = [self._internal_tag_link_html(name) for name in names]
            return ', '.join(link for link in links if link)

        alias_links = join_links(self._stored_alias_names or [])
        if alias_links:
            sections.append(
                f'The following tags are aliased to this tag: {alias_links}.')

        implication_links = join_links(self._stored_implication_names or [])
        if implication_links:
            sections.append(f'This tag implicates {implication_links}.')

        implied_by_links = join_links(self._stored_implied_by_names or [])
        if implied_by_links:
            sections.append(
                f'The following tags implicate this tag: {implied_by_links}.')

        if not sections:
            return ''
        paragraphs = ''.join(
            f'<p style="font-size: 0.9em; color: {note_color}; '
            f'margin: 8px 0 0 0;"><i>{section}</i></p>'
            for section in sections)
        return (f'<div style="margin-top: 12px; border-top: 1px solid {note_color}; '
                f'padding-top: 4px;">{paragraphs}</div>')

    def _compose_wiki_html(self) -> str:
        is_dark = self.palette().color(self.backgroundRole()).lightness() < 128
        title_color = '#6baef6' if is_dark else '#0066cc'
        display_title = self._stored_wiki_title.replace('_', ' ')
        rendered_body = self.convert_dtext_to_html(self._stored_wiki_body)
        other_names_html = self._build_other_names_html(is_dark)
        deprecated_html = self._build_deprecated_note_html(is_dark)
        relations_html = self._build_relations_html(is_dark)
        posts_html = self._build_posts_section_html(is_dark)
        return (
            f'<h1 style="font-size: 1.5em; color: {title_color}; margin: 0 0 4px 0;">'
            f'{html.escape(display_title)}</h1>'
            f'{other_names_html}'
            f'<div style="line-height: 1.2; margin-top: 4px;">{rendered_body}</div>'
            f'{deprecated_html}'
            f'{relations_html}'
            f'{posts_html}'
        )

    def _thumbnail_columns(self, cell_width: int = 170) -> int:
        available = self.content_browser.viewport().width() - 32
        return max(1, int(available / cell_width))

    def _post_thumbnail_cell_html(self, post: dict) -> str:
        """One clickable thumbnail cell for a post grid (opens the post view)."""
        post_id = post.get('id')
        if post_id is None:
            return ''
        internal_url = (POST_INTERNAL_LINK_PREFIX
                        + urlencode({'id': post_id}))
        thumbnail_data_url = str(post.get('thumbnail_data_url') or '')
        if thumbnail_data_url:
            inner = (
                f'<img src="{html.escape(thumbnail_data_url, quote=True)}" '
                'style="max-width: 150px; max-height: 150px; '
                'border: 1px solid palette(mid);" '
                f'alt="Post #{html.escape(str(post_id))}">')
        else:
            # Some posts have no usable thumbnail (e.g. video/flash) — show a
            # simple labelled placeholder that still links to the post.
            inner = (
                '<span style="display: inline-block; padding: 24px 10px; '
                'border: 1px solid palette(mid); color: palette(mid);">'
                f'post #{html.escape(str(post_id))}</span>')
        return (
            f'<td align="center" style="padding: 6px; vertical-align: middle;">'
            f'<a href="{html.escape(internal_url, quote=True)}">{inner}</a>'
            f'</td>')

    def _post_grid_table_html(self, posts: list) -> str:
        cells = [self._post_thumbnail_cell_html(post) for post in posts]
        cells = [cell for cell in cells if cell]
        if not cells:
            return ''
        cols_per_row = self._thumbnail_columns()
        rows = []
        for i in range(0, len(cells), cols_per_row):
            rows.append(f'<tr>{"".join(cells[i:i + cols_per_row])}</tr>')
        return (f'<table style="border-collapse: collapse; margin: 8px 0 12px 0;">'
                f'{"".join(rows)}</table>')

    def _build_posts_section_html(self, is_dark: bool) -> str:
        """The "Posts" section shown at the bottom of a wiki page."""
        posts = self._stored_wiki_posts or []
        if not posts:
            return ''
        heading_color = '#6baef6' if is_dark else '#0066cc'
        note_color = '#9aa0aa' if is_dark else '#777777'
        grid_html = self._post_grid_table_html(posts)
        if not grid_html:
            return ''
        search_tag = self.normalize_tag(self._stored_wiki_title)
        view_all_url = (POSTS_INTERNAL_LINK_PREFIX
                        + urlencode({'tags': search_tag, 'page': 1}))
        # Record the ordered ids so a post opened from this grid can offer
        # prev/next and a link back to the full search.
        self._grid_context = {
            'tags': search_tag,
            'page': 1,
            'ids': [str(post.get('id')) for post in posts
                    if post.get('id') is not None],
            'origin': 'wiki',
        }
        return (
            f'<div style="margin-top: 16px; border-top: 1px solid {note_color}; '
            'padding-top: 6px;">'
            f'<h2 style="font-size: 1.2em; color: {heading_color}; '
            'margin: 4px 0;">Posts</h2>'
            f'{grid_html}'
            f'<p style="margin: 4px 0;"><a href="'
            f'{html.escape(view_all_url, quote=True)}">View all posts &raquo;</a></p>'
            '</div>'
        )


    def normalize_tag(self, tag: str) -> str:
        normalized = str(tag).strip()
        normalized_casefold = normalized.casefold()
        if normalized_casefold.startswith(TAG_GROUP_SEARCH_PREFIX):
            normalized = ('tag_group:'
                          + normalized[len(TAG_GROUP_SEARCH_PREFIX):].lstrip())
        elif normalized_casefold.startswith(TAG_GROUP_NORMALIZED_PREFIX):
            normalized = ('tag_group:'
                          + normalized[len(TAG_GROUP_NORMALIZED_PREFIX):].lstrip())
        normalized = normalized.replace(' ', '_')
        if normalized.casefold().startswith(TAG_GROUP_NORMALIZED_PREFIX):
            normalized = ('tag_group:'
                          + normalized[len(TAG_GROUP_NORMALIZED_PREFIX):]
                          .casefold())
        return normalized


    def wiki_url_for_tag(self, normalized_tag: str) -> str:
        return f'{DANBOORU_BASE_URL}/wiki_pages/{quote(normalized_tag)}'


    def search_tag(self):
        """Handle the search box. A leading "posts:" (e.g. "posts:no humans")
        searches posts for that tag directly instead of opening its wiki page.
        Everything else behaves like a normal wiki search."""
        raw_text = self.search_line_edit.text().strip()
        if raw_text.casefold().startswith(POSTS_SEARCH_PREFIX):
            remainder = raw_text[len(POSTS_SEARCH_PREFIX):].strip()
            popup = self.search_completer.popup()
            if popup is not None:
                popup.hide()
            self.stop_all_autocomplete_threads()
            tag = self.normalize_tag(
                self.extract_tag_name_from_search_text(remainder))
            if not tag:
                return
            self.load_posts_search(tag, 1, add_to_history=True)
            return
        super().search_tag()

    @Slot(str, str)
    def show_not_found(self, request_key: str, normalized_tag: str):
        """When a tag has no wiki page we fall back to showing its posts, so a
        search still lands somewhere useful. Only tag (title) lookups fall back;
        a bad numeric wiki-page id still shows the normal "not found" message."""
        if request_key != self.current_request_key:
            return
        if request_key.startswith('tag:') and str(normalized_tag or '').strip():
            self._fallback_to_posts_search(str(normalized_tag).strip())
            return
        super().show_not_found(request_key, normalized_tag)

    def _fallback_to_posts_search(self, tag: str):
        self.pending_history_append_on_load = False
        # Replace the (empty) wiki history entry for this tag with a posts entry
        # so Back/Forward and "Open in Browser" reflect what's on screen.
        view = self.current_view()
        if isinstance(view, dict) and view.get('type') == 'wiki':
            self.tag_history[self.history_index] = {
                'type': 'posts', 'tags': tag, 'page': 1}
        self.load_posts_search(tag, 1, add_to_history=False)

    def load_tag(self, tag: str, add_to_history: bool):
        normalized_tag = self.normalize_tag(tag)
        if not normalized_tag:
            return
        self.pending_history_append_on_load = False

        if add_to_history:
            if self.history_index < len(self.tag_history) - 1:
                self.tag_history = self.tag_history[:self.history_index + 1]
            self.tag_history.append({'type': 'wiki', 'tag': normalized_tag})
            self.history_index = len(self.tag_history) - 1
            self.pending_history_update = False
        else:
            self.pending_history_update = True

        display_tag = normalized_tag.replace('_', ' ')
        self.current_request_key = f'tag:{normalized_tag}'
        self.setWindowTitle(f'Danbooru Wiki: {display_tag}')
        self.set_search_text_silently(display_tag)
        self._active_composer = None
        self.show_loading()
        self._set_browser_html('')
        self.update_navigation_buttons()
        self.update_add_to_library_button_state()

        fetch_thread = DanbooruWikiFetchThread(
            request_key=self.current_request_key, normalized_tag=normalized_tag)
        self.fetch_thread = fetch_thread
        self.active_fetch_threads.append(fetch_thread)
        fetch_thread.fetch_succeeded.connect(self.load_wiki_page)
        fetch_thread.fetch_not_found.connect(self.show_not_found)
        fetch_thread.fetch_failed.connect(self.show_error)
        fetch_thread.finished.connect(self.handle_fetch_thread_finished)
        fetch_thread.start()

    def load_wiki_page_id(self, wiki_page_id: str, add_to_history: bool):
        normalized_wiki_page_id = str(wiki_page_id).strip()
        if not normalized_wiki_page_id:
            return
        self.pending_history_update = False
        self.pending_history_append_on_load = add_to_history
        self.current_request_key = f'id:{normalized_wiki_page_id}'
        self.setWindowTitle('Danbooru Wiki')
        self._active_composer = None
        self.show_loading()
        self._set_browser_html('')
        self.update_navigation_buttons()
        self.update_add_to_library_button_state()

        fetch_thread = DanbooruWikiFetchThread(
            request_key=self.current_request_key, wiki_page_id=normalized_wiki_page_id)
        self.fetch_thread = fetch_thread
        self.active_fetch_threads.append(fetch_thread)
        fetch_thread.fetch_succeeded.connect(self.load_wiki_page)
        fetch_thread.fetch_not_found.connect(self.show_not_found)
        fetch_thread.fetch_failed.connect(self.show_error)
        fetch_thread.finished.connect(self.handle_fetch_thread_finished)
        fetch_thread.start()

    # ---- View navigation (wiki page / posts search / single post) ----
    def current_view(self):
        if 0 <= self.history_index < len(self.tag_history):
            return self.tag_history[self.history_index]
        return None

    def current_tag(self) -> str:
        view = self.current_view()
        if isinstance(view, dict):
            return view.get('tag', '') if view.get('type') == 'wiki' else ''
        # Safety net for any legacy string entries.
        return view if isinstance(view, str) else ''

    @Slot()
    def navigate_back(self):
        if self.history_index <= 0:
            return
        self.history_index -= 1
        self._render_history_view(self.tag_history[self.history_index])
        self.update_navigation_buttons()

    @Slot()
    def navigate_forward(self):
        if self.history_index >= len(self.tag_history) - 1:
            return
        self.history_index += 1
        self._render_history_view(self.tag_history[self.history_index])
        self.update_navigation_buttons()

    def _render_history_view(self, view):
        """Re-open a history entry WITHOUT changing the history stack."""
        if isinstance(view, str):
            self.load_tag(view, add_to_history=False)
            return
        view_type = view.get('type')
        if view_type == 'wiki':
            self.load_tag(view.get('tag', ''), add_to_history=False)
        elif view_type == 'wiki_id':
            self.load_wiki_page_id(view.get('id', ''), add_to_history=False)
        elif view_type == 'posts':
            self.load_posts_search(view.get('tags', ''), view.get('page', 1),
                                   add_to_history=False)
        elif view_type == 'post':
            self.load_post(view.get('id', ''), add_to_history=False,
                           context=view)

    def load_posts_search(self, tags: str, page: int,
                          add_to_history: bool = True):
        tags = str(tags or '').strip()
        if not tags:
            return
        page = max(1, int(page or 1))
        if add_to_history:
            if self.history_index < len(self.tag_history) - 1:
                self.tag_history = self.tag_history[:self.history_index + 1]
            self.tag_history.append({'type': 'posts', 'tags': tags,
                                     'page': page})
            self.history_index = len(self.tag_history) - 1
        display_tags = tags.replace('_', ' ')
        self.current_request_key = f'posts:{tags}:{page}'
        self.setWindowTitle(f'Danbooru Posts: {display_tags} (page {page})')
        self.set_search_text_silently(display_tags)
        self._active_composer = None
        self.show_loading()
        self._set_browser_html('')
        self.update_navigation_buttons()
        self.update_add_to_library_button_state()

        posts_thread = DanbooruPostsSearchThread(
            self.current_request_key, tags, page)
        self._posts_fetch_thread = posts_thread
        self._active_posts_threads.append(posts_thread)
        posts_thread.posts_ready.connect(self.display_posts_search)
        posts_thread.posts_failed.connect(self.show_error)
        posts_thread.finished.connect(self.handle_posts_thread_finished)
        posts_thread.start()

    @Slot(str, str, int, list)
    def display_posts_search(self, request_key: str, tags: str, page: int,
                             posts: list):
        if request_key != self.current_request_key:
            return
        self._posts_view_state = {'tags': tags, 'page': page, 'posts': posts}
        self._active_composer = self._compose_posts_html
        self._set_browser_html(self._compose_posts_html())
        self.hide_loading()
        self.update_add_to_library_button_state()

    def _compose_posts_html(self) -> str:
        state = self._posts_view_state or {}
        tags = state.get('tags', '')
        page = int(state.get('page', 1) or 1)
        posts = state.get('posts', []) or []
        is_dark = self.palette().color(self.backgroundRole()).lightness() < 128
        heading_color = '#6baef6' if is_dark else '#0066cc'
        link_color = heading_color
        display_tags = tags.replace('_', ' ')
        # Record the ordered ids so a post opened from this grid can offer
        # prev/next and a link back to this search page.
        self._grid_context = {
            'tags': tags, 'page': page,
            'ids': [str(post.get('id')) for post in posts
                    if post.get('id') is not None],
            'origin': 'posts',
        }
        heading = (
            f'<h1 style="font-size: 1.4em; color: {heading_color}; '
            f'margin: 0 0 6px 0;">Posts: {html.escape(display_tags)}</h1>')
        if posts:
            body = self._post_grid_table_html(posts)
        elif page > 1:
            body = '<p>No more posts on this page.</p>'
        else:
            body = '<p>No posts found for this tag.</p>'
        nav_parts = []
        if page > 1:
            prev_url = (POSTS_INTERNAL_LINK_PREFIX
                        + urlencode({'tags': tags, 'page': page - 1}))
            nav_parts.append(
                f'<a href="{html.escape(prev_url, quote=True)}" '
                f'style="color: {link_color};">&laquo; Previous</a>')
        nav_parts.append(f'<span style="color: palette(mid);">Page {page}</span>')
        if len(posts) >= POSTS_PER_PAGE:
            next_url = (POSTS_INTERNAL_LINK_PREFIX
                        + urlencode({'tags': tags, 'page': page + 1}))
            nav_parts.append(
                f'<a href="{html.escape(next_url, quote=True)}" '
                f'style="color: {link_color};">Next &raquo;</a>')
        pagination = (
            '<p style="margin: 8px 0;">' + ' &nbsp;|&nbsp; '.join(nav_parts)
            + '</p>')
        return heading + body + pagination

    def load_post(self, post_id: str, add_to_history: bool = True,
                  context: dict = None):
        post_id = str(post_id).strip()
        if not post_id:
            return
        if add_to_history:
            neighbors, query, page = [], '', 1
            if context:
                neighbors = context.get('neighbors') or []
                query = context.get('query', '')
                page = context.get('page', 1)
            elif (self._grid_context
                  and post_id in self._grid_context.get('ids', [])):
                neighbors = self._grid_context.get('ids', [])
                query = self._grid_context.get('tags', '')
                page = self._grid_context.get('page', 1)
            if self.history_index < len(self.tag_history) - 1:
                self.tag_history = self.tag_history[:self.history_index + 1]
            self.tag_history.append({
                'type': 'post', 'id': post_id, 'query': query,
                'neighbors': list(neighbors), 'page': page})
            self.history_index = len(self.tag_history) - 1
        self.current_request_key = f'post:{post_id}'
        self.setWindowTitle(f'Danbooru Post #{post_id}')
        self._active_composer = None
        self.show_loading()
        self._set_browser_html('')
        self.update_navigation_buttons()
        self.update_add_to_library_button_state()

        post_thread = DanbooruPostViewThread(self.current_request_key, post_id)
        self._posts_fetch_thread = post_thread
        self._active_posts_threads.append(post_thread)
        post_thread.post_ready.connect(self.display_post)
        post_thread.post_failed.connect(self.show_error)
        post_thread.finished.connect(self.handle_posts_thread_finished)
        post_thread.start()

    @Slot(str, dict)
    def display_post(self, request_key: str, post: dict):
        if request_key != self.current_request_key:
            return
        self._stored_post = post
        self._active_composer = self._compose_post_html
        self._set_browser_html(self._compose_post_html())
        self.hide_loading()
        self.update_add_to_library_button_state()

    def _compose_post_html(self) -> str:
        post = self._stored_post or {}
        if not post:
            return '<p>Post not available.</p>'
        is_dark = self.palette().color(self.backgroundRole()).lightness() < 128
        view = self.current_view()
        view = view if isinstance(view, dict) else {}
        query = view.get('query', '')
        neighbors = view.get('neighbors') or []
        page = view.get('page', 1)
        post_id = str(post.get('id'))
        nav_html = self._build_post_nav_html(
            post_id, query, neighbors, page, is_dark)
        image_html = self._build_post_image_html(post)
        info_html = self._build_post_info_html(post, is_dark)
        tags_html = self._build_post_tags_html(post, is_dark)
        return nav_html + image_html + info_html + tags_html

    def _build_post_nav_html(self, post_id: str, query: str, neighbors: list,
                             page: int, is_dark: bool) -> str:
        link_color = '#6baef6' if is_dark else '#0066cc'
        parts = []
        if neighbors and post_id in neighbors:
            index = neighbors.index(post_id)
            if index > 0:
                prev_url = (POST_INTERNAL_LINK_PREFIX
                            + urlencode({'id': neighbors[index - 1]}))
                parts.append(
                    f'<a href="{html.escape(prev_url, quote=True)}" '
                    f'style="color: {link_color};">&lsaquo; Previous</a>')
            if index < len(neighbors) - 1:
                next_url = (POST_INTERNAL_LINK_PREFIX
                            + urlencode({'id': neighbors[index + 1]}))
                parts.append(
                    f'<a href="{html.escape(next_url, quote=True)}" '
                    f'style="color: {link_color};">Next &rsaquo;</a>')
        if query:
            search_url = (POSTS_INTERNAL_LINK_PREFIX
                          + urlencode({'tags': query, 'page': page}))
            parts.append(
                'Search: <a href="'
                f'{html.escape(search_url, quote=True)}" '
                f'style="color: {link_color};">'
                f'{html.escape(query.replace("_", " "))}</a>')
        if not parts:
            return ''
        return ('<p style="margin: 0 0 6px 0;">'
                + ' &nbsp;|&nbsp; '.join(parts) + '</p>')

    def _build_post_image_html(self, post: dict) -> str:
        data_url = str(post.get('_taggui_image_data_url') or '')
        if not data_url:
            file_url = str(post.get('large_file_url')
                           or post.get('file_url') or '')
            link = ''
            if file_url:
                link = ('<p style="margin: 4px 0;"><a href="'
                        f'{html.escape(file_url, quote=True)}">'
                        'Open image in browser</a></p>')
            return ('<p style="color: palette(mid); margin: 4px 0;">'
                    'This post cannot be previewed here '
                    '(it may be a video, animation, or restricted).</p>' + link)
        width = int(post.get('image_width') or 0)
        height = int(post.get('image_height') or 0)
        if width > 0 and height > 0:
            # `width`/`height` are the ORIGINAL dimensions, but the image we embed
            # is Danbooru's sample (long edge <= POST_VIEW_MAX_IMAGE_EDGE). Work
            # out the sample's natural width so we never scale it up.
            longest_edge = max(width, height)
            if longest_edge > POST_VIEW_MAX_IMAGE_EDGE:
                natural_width = max(
                    1, round(width * POST_VIEW_MAX_IMAGE_EDGE / longest_edge))
            else:
                natural_width = width
            viewport_width = self.content_browser.viewport().width() - 32
            display_width = max(1, min(natural_width, viewport_width))
            size_attr = f'width="{display_width}"'
        else:
            size_attr = 'style="max-width: 100%;"'
        return ('<p style="margin: 4px 0;"><img src="'
                f'{html.escape(data_url, quote=True)}" {size_attr}></p>')

    def _build_post_info_html(self, post: dict, is_dark: bool) -> str:
        note_color = '#9aa0aa' if is_dark else '#555555'
        rating_labels = {'g': 'General', 's': 'Sensitive',
                         'q': 'Questionable', 'e': 'Explicit'}
        rating = rating_labels.get(
            str(post.get('rating') or '').lower(), str(post.get('rating') or '—'))
        width = post.get('image_width') or '?'
        height = post.get('image_height') or '?'
        parts = [f'Rating: {html.escape(str(rating))}',
                 f'Size: {html.escape(str(width))}&times;{html.escape(str(height))}']
        score = post.get('score')
        if score is not None:
            parts.append(f'Score: {html.escape(str(score))}')
        fav_count = post.get('fav_count')
        if fav_count is not None:
            parts.append(f'Favorites: {html.escape(str(fav_count))}')
        file_size = post.get('file_size')
        if file_size:
            parts.append(f'File size: {self._format_file_size(file_size)}')
        source = str(post.get('source') or '').strip()
        if source.startswith('http://') or source.startswith('https://'):
            parts.append('Source: <a href="'
                         f'{html.escape(source, quote=True)}">'
                         f'{html.escape(source)}</a>')
        elif source:
            parts.append(f'Source: {html.escape(source)}')
        return (f'<p style="font-size: 0.9em; color: {note_color}; '
                'margin: 6px 0;">' + ' &nbsp;&bull;&nbsp; '.join(parts)
                + '</p>')

    @staticmethod
    def _format_file_size(num_bytes) -> str:
        try:
            size = float(num_bytes)
        except (TypeError, ValueError):
            return str(num_bytes)
        for unit in ('B', 'KB', 'MB', 'GB'):
            if size < 1024 or unit == 'GB':
                if unit == 'B':
                    return f'{int(size)} {unit}'
                return f'{size:.1f} {unit}'
            size /= 1024
        return f'{size:.1f} GB'

    def _colored_tag_link_html(self, tag_name: str, color: str) -> str:
        name = str(tag_name or '').strip()
        if not name:
            return ''
        normalized = self.normalize_tag(name)
        label = name.replace('_', ' ')
        internal_url = f'{WIKI_INTERNAL_LINK_PREFIX}{quote(normalized)}'
        return (f'<a href="{internal_url}" style="color: {color};">'
                f'{html.escape(label)}</a>')

    def _build_post_tags_html(self, post: dict, is_dark: bool) -> str:
        theme_index = 1 if is_dark else 0
        categories = (
            ('Copyright', 'tag_string_copyright', 'copyright'),
            ('Character', 'tag_string_character', 'character'),
            ('Artist', 'tag_string_artist', 'artist'),
            ('General', 'tag_string_general', 'general'),
            ('Meta', 'tag_string_meta', 'meta'),
        )
        blocks = []
        for label, key, color_key in categories:
            names = str(post.get(key) or '').split()
            if not names:
                continue
            color = TAG_CATEGORY_COLORS[color_key][theme_index]
            links = ', '.join(
                self._colored_tag_link_html(name, color) for name in names)
            blocks.append(
                f'<p style="margin: 3px 0; line-height: 1.6;">'
                f'<b>{label}:</b> {links}</p>')
        if not blocks:
            return ''
        return ('<div style="margin-top: 10px; border-top: 1px solid '
                'palette(mid); padding-top: 6px;">' + ''.join(blocks) + '</div>')


    def build_link_html(self, token: str, link_label_suffix: str = '') -> str:
        named_link_match = NAMED_EXTERNAL_LINK_PATTERN.match(token)
        if named_link_match:
            label = named_link_match.group('label').strip()
            url = (named_link_match.group('url_bracket')
                   or named_link_match.group('url_bare') or '')
            escaped_url = html.escape(url, quote=True)
            external_marker = ('<span style="font-size: 0.8em;">'
                               '&#8239;\u2197</span>')
            return (f'<a href="{escaped_url}">{html.escape(label)}'
                    f'{external_marker}</a>')

        if token.startswith('http'):
            escaped_url = html.escape(token, quote=True)
            return f'<a href="{escaped_url}">{html.escape(token)}</a>'

        if token.startswith('"') and '":#' in token:
            quoted_label, anchor_target = token.split('":#', 1)
            anchor_label = quoted_label[1:].strip()
            normalized_anchor_target = anchor_target.strip()
            if not normalized_anchor_target:
                return html.escape(token)
            return (
                f'<a href="#{html.escape(normalized_anchor_target, quote=True)}">'
                f'{html.escape(anchor_label)}</a>'
            )

        post_reference_match = POST_REFERENCE_PATTERN.match(token)
        if post_reference_match:
            post_id = post_reference_match.group(1)
            post_url = f'{DANBOORU_BASE_URL}/posts/{quote(post_id)}'
            return f'<a href="{html.escape(post_url, quote=True)}">post #{html.escape(post_id)}</a>'
        asset_reference_match = ASSET_REFERENCE_PATTERN.match(token)
        if asset_reference_match:
            asset_id = asset_reference_match.group(1)
            asset_url = f'{DANBOORU_BASE_URL}/media_assets/{quote(asset_id)}'
            return f'<a href="{html.escape(asset_url, quote=True)}">asset #{html.escape(asset_id)}</a>'
        pool_reference_match = POOL_REFERENCE_PATTERN.match(token)
        if pool_reference_match:
            pool_id = pool_reference_match.group(1)
            pool_url = f'{DANBOORU_BASE_URL}/pools/{quote(pool_id)}'
            return f'<a href="{html.escape(pool_url, quote=True)}">pool #{html.escape(pool_id)}</a>'

        # {{tag|label}} — link to Danbooru post search for the tag
        if token.startswith('{{') and token.endswith('}}'):
            inner = token[2:-2].strip()
            if '|' in inner:
                tag_name, label = inner.split('|', 1)
            else:
                tag_name, label = inner, inner
            tag_name = tag_name.strip()
            label = label.strip() or tag_name
            search_url = f'{DANBOORU_BASE_URL}/posts?tags={quote(tag_name)}'
            return f'<a href="{html.escape(search_url, quote=True)}">{html.escape(label)}</a>'

        inner_token = token[2:-2].strip()
        if '|' in inner_token:
            target, label = inner_token.split('|', 1)
            display_label = label.strip()
        else:
            target = inner_token
            display_label = target.strip().replace('_', ' ')
        normalized_target = self.normalize_tag(target)
        if not display_label:
            display_label = normalized_target.replace('_', ' ')
        link_label = display_label + link_label_suffix
        internal_url = f'{WIKI_INTERNAL_LINK_PREFIX}{quote(normalized_target)}'
        return f'<a href="{internal_url}">{html.escape(link_label)}</a>'

    def normalize_heading_anchor(self, raw_anchor: str, heading_text: str) -> str:
        anchor = str(raw_anchor or '').strip()
        if not anchor:
            anchor = heading_text.casefold()
            anchor = re.sub(r'[^a-z0-9]+', '-', anchor).strip('-')
        if not anchor.startswith('dtext-'):
            anchor = f'dtext-{anchor}'
        return anchor

    def linkify_inline_text(self, text: str, preserve_line_break_tokens: bool = True) -> str:
        if preserve_line_break_tokens:
            text = text.replace('[br]', '<br>')
        else:
            text = text.replace('[br]', ' ')
        html_parts = []
        position = 0
        for token_match in TOKEN_PATTERN.finditer(text):
            html_parts.append(html.escape(text[position:token_match.start()]).replace('&lt;br&gt;', '<br>'))
            token = token_match.group(0)
            trailing_suffix = ''
            new_position = token_match.end()
            if token.startswith('[['):
                trailing_text = text[token_match.end():]
                suffix_match = WIKI_LINK_SUFFIX_PATTERN.match(trailing_text)
                if suffix_match:
                    trailing_suffix = suffix_match.group(0)
                    new_position += len(trailing_suffix)
            html_parts.append(self.build_link_html(token, link_label_suffix=trailing_suffix))
            position = new_position
        html_parts.append(html.escape(text[position:]).replace('&lt;br&gt;', '<br>'))
        return self.apply_inline_formatting_tags(''.join(html_parts))

    def apply_inline_formatting_tags(self, text_html: str) -> str:
        formatted_text = text_html
        for pattern, html_tag in FORMATTING_PATTERNS:
            formatted_text = pattern.sub(
                lambda match: f'<{html_tag}>{match.group(1)}</{html_tag}>',
                formatted_text
            )
        return self.apply_spoiler_tags(formatted_text)

    def apply_spoiler_tags(self, text_html: str) -> str:
        """Render [spoiler]...[/spoiler] as a black bar hiding its contents.

        The text (and any links inside it) is drawn black-on-black so it is
        hidden by default, matching how Danbooru shows a collapsed spoiler.
        QTextBrowser cannot reveal it on hover the way a web browser does, so
        the user reveals it by selecting/highlighting the bar."""
        def replace_spoiler(match: re.Match) -> str:
            inner = match.group(1)
            # Drop any existing per-link colour, then force links to black so
            # the bar covers them too (otherwise Qt paints them link-blue).
            inner = SPOILER_ANCHOR_STYLE_PATTERN.sub(r'\1', inner)
            inner = inner.replace(
                '<a ', '<a style="color: #000000; text-decoration: none;" ')
            return ('<span style="background-color: #000000; '
                    f'color: #000000;">{inner}</span>')
        return SPOILER_PATTERN.sub(replace_spoiler, text_html)

    def _wrap_quote_html(self, inner_html: str,
                         collapse_top: bool = False) -> str:
        """Wrap already-rendered quote content in a left-bar block.

        Danbooru renders ``[quote]`` blocks as indented content with a thin
        vertical bar down the left. Qt's rich text renders table-cell borders
        reliably (block-level borders are flaky), so a single-cell table with a
        left border gives both the bar and the indentation. The trailing
        paragraph's bottom margin is trimmed so the bar hugs its content rather
        than leaving an extra blank line inside the quote.

        The outer top/bottom margins (0.8em / 1.1em) are tuned so the gap before
        and after a quote matches the gap between normal paragraphs. Qt does not
        collapse the margins of adjacent tables the way CSS collapses paragraph
        margins, so two back-to-back quotes would otherwise get a doubled gap;
        ``collapse_top`` drops this quote's top margin when it directly follows
        another quote, keeping consecutive quotes evenly spaced.
        """
        bottom_margin_marker = 'margin: 0 0 0.9em 0;'
        last_marker_index = inner_html.rfind(bottom_margin_marker)
        if last_marker_index != -1:
            inner_html = (
                inner_html[:last_marker_index] + 'margin: 0;'
                + inner_html[last_marker_index + len(bottom_margin_marker):])
        top_margin = '0' if collapse_top else '0.8em'
        return (
            f'<table style="border-collapse: collapse; '
            f'margin: {top_margin} 0 1.1em 0;">'
            '<tr><td style="border-left: 3px solid palette(mid); '
            'padding: 0 0 0 10px;">'
            f'{inner_html}</td></tr></table>'
        )

    def build_post_thumbnail_html(self, post_id: str) -> str:
        post_data = self.post_details_by_id.get(post_id)
        post_url = f'{DANBOORU_BASE_URL}/posts/{quote(post_id)}'
        if not isinstance(post_data, dict):
            return f'<a href="{html.escape(post_url, quote=True)}">post #{html.escape(post_id)}</a>'
        thumbnail_data_url = str(post_data.get('thumbnail_data_url') or '')
        if not thumbnail_data_url:
            return f'<a href="{html.escape(post_url, quote=True)}">post #{html.escape(post_id)}</a>'
        return (
            f'<a href="{html.escape(post_url, quote=True)}">'
            f'<img src="{html.escape(thumbnail_data_url, quote=True)}" '
            'style="max-width: 192px; max-height: 192px; vertical-align: middle; '
            'border: 1px solid palette(mid); margin: 0 4px 0 0;" '
            f'alt="Post #{html.escape(post_id)} thumbnail"></a>'
        )

    def build_asset_thumbnail_html(self, asset_id: str) -> str:
        asset_data = self.asset_details_by_id.get(asset_id)
        asset_url = f'{DANBOORU_BASE_URL}/media_assets/{quote(asset_id)}'
        if not isinstance(asset_data, dict):
            return f'<a href="{html.escape(asset_url, quote=True)}">asset #{html.escape(asset_id)}</a>'
        thumbnail_data_url = str(asset_data.get('thumbnail_data_url') or '')
        if not thumbnail_data_url:
            return f'<a href="{html.escape(asset_url, quote=True)}">asset #{html.escape(asset_id)}</a>'
        return (
            f'<a href="{html.escape(asset_url, quote=True)}">'
            f'<img src="{html.escape(thumbnail_data_url, quote=True)}" '
            'style="max-width: 192px; max-height: 192px; vertical-align: middle; '
            'border: 1px solid palette(mid); margin: 0 4px 0 0;" '
            f'alt="Asset #{html.escape(asset_id)} thumbnail"></a>'
        )

    def convert_dtext_to_html(self, body: str) -> str:
        lines = body.splitlines()
        html_lines = []
        thumbnail_buffer = []  # list of (thumb_html, caption_html)
        previous_line_was_blank = False
        in_expand_block = False
        # Index up to which lines have already been consumed by a block handler
        # (e.g. the body of a [quote]...[/quote]); the loop skips past them.
        skip_until = 0

        def flush_thumbnails():
            if not thumbnail_buffer:
                return
            # Strip trailing <br> from a preceding heading — the table's own
            # margin handles the gap, so we don't want the extra blank line.
            while html_lines and html_lines[-1] == '<br>':
                html_lines.pop()
            # Calculate columns dynamically from current viewport width.
            # Each cell renders at most 192px image + ~12px internal padding.
            # Use 170px as the practical column-width divisor to avoid under-counting.
            cell_width = 170
            available = self.content_browser.viewport().width() - 32
            cols_per_row = max(1, int(available / cell_width))
            cells = []
            for thumb_html, caption_html in thumbnail_buffer:
                cap = (f'<br><span style="font-size: 0.85em;">{caption_html}</span>'
                       if caption_html else '')
                cells.append(
                    f'<td align="center" style="padding: 4px 6px; vertical-align: top;">'
                    f'{thumb_html}{cap}</td>')
            rows = []
            for i in range(0, len(cells), cols_per_row):
                rows.append(f'<tr>{"".join(cells[i:i + cols_per_row])}</tr>')
            html_lines.append(
                f'<table style="border-collapse: collapse; margin: 8px 0 16px 0;">'
                f'{"".join(rows)}</table>')
            del thumbnail_buffer[:]

        for index, line in enumerate(lines):
            if index < skip_until:
                continue
            stripped_line = line.strip()
            if not stripped_line:
                previous_line_was_blank = True
                continue

            previous_line_was_blank = False

            casefolded_line = stripped_line.casefold()
            # DText block quotes: [quote] and [/quote] each sit on their own
            # line. Collect the inner lines (honouring nested quotes) and render
            # them recursively so paragraph/blank-line spacing matches the rest
            # of the body, then wrap the result in a left-bar block.
            if casefolded_line == '[quote]':
                flush_thumbnails()
                while html_lines and html_lines[-1] == '<br>':
                    html_lines.pop()
                depth = 1
                inner_lines = []
                scan_index = index + 1
                while scan_index < len(lines):
                    inner_casefolded = lines[scan_index].strip().casefold()
                    if inner_casefolded == '[quote]':
                        depth += 1
                    elif inner_casefolded == '[/quote]':
                        depth -= 1
                        if depth == 0:
                            break
                    inner_lines.append(lines[scan_index])
                    scan_index += 1
                # Skip past the consumed inner lines and the closing [/quote].
                skip_until = scan_index + 1
                inner_html = self.convert_dtext_to_html('\n'.join(inner_lines))
                # Collapse this quote's top margin when it directly follows
                # another quote (the previous emitted block is a quote table,
                # identified by its unique left-bar style) so back-to-back
                # quotes keep the same gap as a single quote.
                previous_block_was_quote = bool(html_lines) and (
                    'border-left: 3px solid palette(mid)' in html_lines[-1])
                html_lines.append(self._wrap_quote_html(
                    inner_html, collapse_top=previous_block_was_quote))
                continue
            # A stray closing tag with no matching opener: drop it silently
            # rather than printing the literal marker.
            if casefolded_line == '[/quote]':
                continue

            if casefolded_line.startswith('[expand=') and stripped_line.endswith(']'):
                flush_thumbnails()
                expand_title = stripped_line[len('[expand='):-1].strip()
                if not expand_title:
                    expand_title = 'Details'
                html_lines.append(
                    '<details open style="margin: 2px 0;">'
                    f'<summary>{html.escape(expand_title)}</summary>'
                )
                in_expand_block = True
                continue
            if stripped_line.casefold() == '[/expand]':
                flush_thumbnails()
                if in_expand_block:
                    html_lines.append('</details>')
                    in_expand_block = False
                continue

            bulleted_reference_line_match = BULLETED_REFERENCE_LINE_PATTERN.match(
                stripped_line)
            if bulleted_reference_line_match:
                reference_token = bulleted_reference_line_match.group(1)
                trailing_text = bulleted_reference_line_match.group(2).strip()
                post_match = POST_REFERENCE_PATTERN.match(reference_token)
                if post_match:
                    post_id = post_match.group(1)
                    thumb_html = self.build_post_thumbnail_html(post_id)
                    caption_text = trailing_text.lstrip(':').strip()
                    caption_html = self.linkify_inline_text(
                        caption_text, preserve_line_break_tokens=False
                    ) if caption_text else ''
                    thumbnail_buffer.append((thumb_html, caption_html))
                    continue
                asset_match = ASSET_REFERENCE_PATTERN.match(reference_token)
                if asset_match:
                    asset_id = asset_match.group(1)
                    thumb_html = self.build_asset_thumbnail_html(asset_id)
                    caption_text = trailing_text.lstrip(':').strip()
                    caption_html = self.linkify_inline_text(
                        caption_text, preserve_line_break_tokens=False
                    ) if caption_text else ''
                    thumbnail_buffer.append((thumb_html, caption_html))
                    continue
                # Non-thumbnail bulleted line — flush any pending thumbnails first.
                flush_thumbnails()

            post_reference_line_match = POST_REFERENCE_PATTERN.match(stripped_line)
            if post_reference_line_match and post_reference_line_match.group(0).lower() == stripped_line.lower():
                flush_thumbnails()
                post_id = post_reference_line_match.group(1)
                html_lines.append(
                    f'<p style="margin: 4px 0;">{self.build_post_thumbnail_html(post_id)}</p>')
                continue
            asset_reference_line_match = ASSET_REFERENCE_PATTERN.match(stripped_line)
            if (asset_reference_line_match
                    and asset_reference_line_match.group(0).lower() == stripped_line.lower()):
                flush_thumbnails()
                asset_id = asset_reference_line_match.group(1)
                html_lines.append(
                    f'<p style="margin: 4px 0;">{self.build_asset_thumbnail_html(asset_id)}</p>')
                continue

            heading_match = HEADING_WITH_ANCHOR_PATTERN.match(stripped_line)
            if heading_match:
                flush_thumbnails()
                # Strip trailing <br> left by a previous heading.
                while html_lines and html_lines[-1] == '<br>':
                    html_lines.pop()
                heading_level = heading_match.group(1)
                heading_anchor = self.normalize_heading_anchor(
                    heading_match.group(2), heading_match.group(3)
                )
                heading_text = self.linkify_inline_text(heading_match.group(3).strip())
                font_sizes = {'1': '1.4em', '2': '1.3em', '3': '1.2em',
                              '4': '1.2em', '5': '1.1em', '6': '1.05em'}
                font_size = font_sizes.get(heading_level, '1.1em')
                html_lines.append(
                    f'<h{heading_level} id="{html.escape(heading_anchor, quote=True)}" '
                    f'style="font-size: {font_size}; margin-top: 8px; margin-bottom: 0;">'
                    f'{heading_text}</h{heading_level}>'
                )
                # Qt doesn't force a line break after headings before inline content;
                # this <br> ensures thumbnails and other content start on a new line.
                html_lines.append('<br>')
                continue

            # List items: use inline <p> with text bullet to avoid Qt's
            # oversized <li> disc bullets and large default list indentation.
            # Check *** before ** before * since each is a prefix of the next.
            if stripped_line.startswith('*** '):
                flush_thumbnails()
                while html_lines and html_lines[-1] == '<br>':
                    html_lines.pop()
                list_item = self.linkify_inline_text(stripped_line[4:].strip())
                html_lines.append(
                    f'<p style="margin: 0 0 0 32px;">&bull;&nbsp;{list_item}</p>')
                continue
            if stripped_line.startswith('** '):
                flush_thumbnails()
                # Strip heading's trailing <br> so list items sit close to the heading.
                while html_lines and html_lines[-1] == '<br>':
                    html_lines.pop()
                list_item = self.linkify_inline_text(stripped_line[3:].strip())
                html_lines.append(
                    f'<p style="margin: 0 0 0 16px;">&bull;&nbsp;{list_item}</p>')
                continue
            if stripped_line.startswith('* '):
                flush_thumbnails()
                while html_lines and html_lines[-1] == '<br>':
                    html_lines.pop()
                list_item = self.linkify_inline_text(stripped_line[2:].strip())
                html_lines.append(
                    f'<p style="margin: 0;">&bull;&nbsp;{list_item}</p>')
                continue

            flush_thumbnails()
            # Strip heading's trailing <br> — block-level <p> doesn't need it.
            while html_lines and html_lines[-1] == '<br>':
                html_lines.pop()
            paragraph_line = self.linkify_inline_text(stripped_line)
            html_lines.append(
                f'<p style="margin: 0 0 0.9em 0; font-size: 1em; line-height: 1.2;">'
                f'{paragraph_line}</p>')

        flush_thumbnails()
        if in_expand_block:
            html_lines.append('</details>')
        return ''.join(html_lines)

    @Slot(str, dict, dict, dict, list)
    def load_wiki_page(self, request_key: str, wiki_page: dict,
                       post_details_by_id: dict, asset_details_by_id: dict,
                       wiki_posts: list):
        if request_key != self.current_request_key:
            return
        title = self.normalize_tag(wiki_page.get('title', self.current_tag()))
        body = str(wiki_page.get('body') or '')
        if not body.strip():
            body = '(No wiki content available.)'
        self.post_details_by_id = post_details_by_id or {}
        self.asset_details_by_id = asset_details_by_id or {}

        self._stored_wiki_body = body
        self._stored_wiki_title = title
        self._stored_other_names = wiki_page.get('other_names') or []
        self._stored_alias_names = wiki_page.get('_taggui_alias_names') or []
        self._stored_implication_names = (
            wiki_page.get('_taggui_implication_names') or [])
        self._stored_implied_by_names = (
            wiki_page.get('_taggui_implied_by_names') or [])
        self._stored_wiki_posts = wiki_posts or []
        self._stored_is_deprecated = bool(wiki_page.get('_taggui_is_deprecated'))
        self._active_composer = self._compose_wiki_html
        self._set_browser_html(self._compose_wiki_html())
        self.hide_loading()

        if self.pending_history_update:
            self.tag_history[self.history_index] = {'type': 'wiki',
                                                    'tag': title}
            self.pending_history_update = False
        elif self.pending_history_append_on_load:
            if self.history_index < len(self.tag_history) - 1:
                self.tag_history = self.tag_history[:self.history_index + 1]
            self.tag_history.append({'type': 'wiki', 'tag': title})
            self.history_index = len(self.tag_history) - 1
            self.pending_history_append_on_load = False
            self.update_navigation_buttons()
        display_tag = title.replace('_', ' ')
        self.setWindowTitle(f'Danbooru Wiki: {display_tag}')
        self.set_search_text_silently(display_tag)
        self.update_add_to_library_button_state()


    def is_external_link(self, url_text: str) -> bool:
        url_text = str(url_text or '')
        if not url_text:
            return False
        url = QUrl(url_text)
        if (not url.scheme() and url.fragment()
                and (not url.path() or url.path() == '/')):
            return False
        if url_text.startswith(WIKI_INTERNAL_LINK_PREFIX_LEGACY):
            return False
        if url_text.startswith(WIKI_INTERNAL_LINK_PREFIX):
            return False
        if (url_text.startswith(POST_INTERNAL_LINK_PREFIX)
                or url_text.startswith(POSTS_INTERNAL_LINK_PREFIX)):
            return False
        # The deprecation-notice help page is opened in the external browser
        # rather than navigated to inside the dialog.
        if url_text == DEPRECATION_NOTICE_URL:
            return True
        decoded_url_text = unquote(url_text)
        parsed_url = urlparse(decoded_url_text)
        is_danbooru_host = (not parsed_url.netloc
                            or parsed_url.netloc.endswith('danbooru.donmai.us'))
        wiki_path = parsed_url.path
        if wiki_path.startswith('wiki_pages/'):
            wiki_path = f'/{wiki_path}'
        if is_danbooru_host and '/wiki_pages/' in wiki_path:
            return False
        # Post and post-search links on Danbooru open in the in-app browser.
        if is_danbooru_host and re.match(r'^/?posts/\d+', parsed_url.path):
            return False
        if is_danbooru_host and parsed_url.path.rstrip('/').endswith('/posts'):
            return False
        return True

    @Slot(QUrl)
    def handle_anchor_clicked(self, url: QUrl):
        url_text = url.toString()
        if (not url.scheme() and url.fragment()
                and (not url.path() or url.path() == '/')):
            self.content_browser.scrollToAnchor(url.fragment())
            return
        # Open the deprecation-notice help page in the external browser instead
        # of routing it through the in-app wiki navigation below.
        if url_text == DEPRECATION_NOTICE_URL:
            QDesktopServices.openUrl(url)
            return
        if url_text.startswith(WIKI_INTERNAL_LINK_PREFIX_LEGACY):
            encoded_tag = url_text[len(WIKI_INTERNAL_LINK_PREFIX_LEGACY):]
            self.load_tag(unquote(encoded_tag), add_to_history=True)
            return
        if url_text.startswith(WIKI_INTERNAL_LINK_PREFIX):
            encoded_tag = url_text[len(WIKI_INTERNAL_LINK_PREFIX):]
            self.load_tag(unquote(encoded_tag), add_to_history=True)
            return
        if url_text.startswith(POST_INTERNAL_LINK_PREFIX):
            params = parse_qs(url_text[len(POST_INTERNAL_LINK_PREFIX):])
            post_id = (params.get('id') or [''])[0]
            if post_id:
                self.load_post(post_id, add_to_history=True)
            return
        if url_text.startswith(POSTS_INTERNAL_LINK_PREFIX):
            params = parse_qs(url_text[len(POSTS_INTERNAL_LINK_PREFIX):])
            search_tags = (params.get('tags') or [''])[0]
            try:
                search_page = int((params.get('page') or ['1'])[0])
            except ValueError:
                search_page = 1
            if search_tags:
                self.load_posts_search(search_tags, search_page,
                                       add_to_history=True)
            return

        decoded_url_text = unquote(url_text)
        parsed_url = urlparse(decoded_url_text)
        is_danbooru_host = (not parsed_url.netloc
                            or parsed_url.netloc.endswith('danbooru.donmai.us'))
        wiki_path = parsed_url.path
        if wiki_path.startswith('wiki_pages/'):
            wiki_path = f'/{wiki_path}'
        if is_danbooru_host and '/wiki_pages/' in wiki_path:
            wiki_tag = wiki_path.split('/wiki_pages/', 1)[1]
            wiki_tag = wiki_tag.split('?', 1)[0].split('#', 1)[0]
            wiki_tag = wiki_tag.strip('/')
            if wiki_tag.isdigit():
                self.load_wiki_page_id(wiki_tag, add_to_history=True)
                return
            self.load_tag(wiki_tag, add_to_history=True)
            return

        # Danbooru post / post-search links open inside the app.
        post_id_match = re.match(r'^/?posts/(\d+)', parsed_url.path)
        if is_danbooru_host and post_id_match:
            self.load_post(post_id_match.group(1), add_to_history=True)
            return
        if is_danbooru_host and parsed_url.path.rstrip('/').endswith('/posts'):
            posts_query = parse_qs(parsed_url.query)
            posts_tags = (posts_query.get('tags') or [''])[0]
            try:
                posts_page = int((posts_query.get('page') or ['1'])[0])
            except ValueError:
                posts_page = 1
            if posts_tags:
                self.load_posts_search(posts_tags, posts_page,
                                       add_to_history=True)
                return

        QDesktopServices.openUrl(url)

    @Slot(str)
    def on_search_text_changed(self, text: str):
        if '(' in text and ')' in text:
            cleaned = AUTOCOMPLETE_COUNT_SUFFIX_PATTERN.sub('', text).strip()
            if cleaned and cleaned != text:
                self.search_line_edit.setText(cleaned)
                return
        self.queue_autocomplete_search(text)

    @Slot(str)
    def queue_autocomplete_search(self, search_text: str):
        trimmed_search_text = search_text.strip()
        if len(trimmed_search_text) < 1:
            self.current_autocomplete_query = ''
            self.autocomplete_display_to_tag = {}
            self.search_autocomplete_model.clear()
            self.search_autocomplete_timer.stop()
            return
        self.current_autocomplete_query = trimmed_search_text
        self.search_autocomplete_timer.start()


    @Slot()
    def open_current_tag_in_browser(self):
        external_url = self._current_view_external_url()
        if external_url:
            QDesktopServices.openUrl(QUrl(external_url))

    def _current_view_external_url(self) -> str:
        """The danbooru.donmai.us URL matching whatever view is on screen, used
        by the "Open in Browser" button."""
        view = self.current_view()
        if isinstance(view, dict):
            view_type = view.get('type')
            if view_type == 'posts':
                query = urlencode({'tags': view.get('tags', ''),
                                   'page': view.get('page', 1)})
                return f'{DANBOORU_BASE_URL}/posts?{query}'
            if view_type == 'post':
                return f'{DANBOORU_BASE_URL}/posts/{quote(str(view.get("id", "")))}'
            if view_type == 'wiki_id':
                return f'{DANBOORU_BASE_URL}/wiki_pages/{quote(str(view.get("id", "")))}'
        normalized_tag = self.current_tag()
        if not normalized_tag:
            return ''
        return self.wiki_url_for_tag(normalized_tag)
