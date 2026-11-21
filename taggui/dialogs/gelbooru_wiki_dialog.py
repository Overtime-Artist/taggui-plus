import html
import re
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from PySide6.QtCore import QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices

from dialogs.booru_originals import (ORIGINAL_INTERNAL_LINK_PREFIX,
                                     OriginalsMixin, decode_data_url_image)
from dialogs.wiki_dialog_base import (AUTOCOMPLETE_COUNT_SUFFIX_PATTERN,
                                      BaseWikiDialog, park_running_threads)
from models.tag_library_model import TagLibraryModel
from utils.gelbooru_posts import (AUTOCOMPLETE_TAG_TYPES, FETCH_WORKER_COUNT,
                                  POSTS_PER_PAGE, autocomplete_entries,
                                  fetch_post, fetch_tag_types, search_posts)
from utils.gelbooru_session import (GELBOORU_BASE_URL, api_key_problem,
                                    gelbooru_urlopen)

WIKI_INTERNAL_LINK_PREFIX = 'taggui-gelbooru-wiki:'
WIKI_RESULT_LINK_PATTERN = re.compile(
    r'href="index\.php\?page=wiki&amp;s=view&amp;id=(\d+)".*?>([^<]+)</a>',
    re.IGNORECASE | re.DOTALL
)
WIKI_GENERIC_LINK_PATTERN = re.compile(
    r'href="([^"]*page=wiki[^"]*id=\d+[^"]*)".*?>([^<]+)</a>',
    re.IGNORECASE | re.DOTALL
)
WIKI_VIEW_TITLE_PATTERN = re.compile(
    r'<h2[^>]*>\s*Now Viewing:\s*(.*?)\s*</h2>\s*(?:<br\s*/?>)?', re.IGNORECASE | re.DOTALL
)
WIKI_VIEW_BODY_PATTERN = re.compile(
    r'<table[^>]*>\s*<tr>\s*<td[^>]*>(.*?)</td>\s*<td',
    re.IGNORECASE | re.DOTALL
)
WIKI_OTHER_INFO_SECTION_PATTERN = re.compile(
    r'<b>\s*Other Wiki Information\s*</b>.*$',
    re.IGNORECASE | re.DOTALL
)
WIKI_ANCHOR_TEXT_PATTERN = re.compile(
    r'(<a\b[^>]*>)(.*?)(</a>)',
    re.IGNORECASE | re.DOTALL
)
WIKI_ANCHOR_WITH_SUFFIX_PATTERN = re.compile(
    r'(<a\b[^>]*>)(.*?)(</a>)([A-Za-z0-9_\']+)',
    re.IGNORECASE | re.DOTALL
)
WIKI_VIEW_ID_FALLBACK_PATTERN = re.compile(
    r'index\.php\?page=wiki&amp;s=(?:edit|history|manage)[^"]*?id=(\d+)',
    re.IGNORECASE | re.DOTALL
)
WIKI_TAG_TYPE_PATTERN = re.compile(
    r'<span>\s*<b>\s*Tag type:\s*([^<]*?)\s*</b>\s*</span>\s*(?:<br\s*/?>\s*)*',
    re.IGNORECASE)
POST_INTERNAL_LINK_PREFIX = 'taggui-post:'
POSTS_INTERNAL_LINK_PREFIX = 'taggui-posts:'
POSTS_SEARCH_PREFIX = 'posts:'
DOWNLOAD_DIRECTORY_SETTING = 'gelbooru_download_directory'
WIKI_PREVIEW_POST_COUNT = 20
# (light theme, dark theme) colours, matching gelbooru.com.
TAG_TYPE_COLORS = {
    'copyright': ('#a800aa', '#c973cd'),
    'character': ('#00aa00', '#35c745'),
    'artist': ('#a00000', '#e05a5a'),
    'general': ('#0073ff', '#6baef6'),
    'metadata': ('#ea7d00', '#f0a52a'),
    'deprecated': ('#888888', '#9aa0aa'),
}
POST_TAG_SECTIONS = (('copyright', 'Copyright'), ('character', 'Character'),
                     ('artist', 'Artist'), ('general', 'General'),
                     ('metadata', 'Metadata'), ('deprecated', 'Deprecated'))

def normalize_tag_for_lookup(tag: str) -> str:
    return str(tag).strip().replace(' ', '_').casefold()


def absolutize_relative_urls(html_fragment: str) -> str:
    def replace_url(match: re.Match) -> str:
        prefix = match.group(1)
        url_text = (match.group(2) or '').strip()
        if (url_text.startswith('http://') or url_text.startswith('https://')
                or url_text.startswith('data:') or url_text.startswith('mailto:')
                or url_text.startswith('#')):
            return match.group(0)
        absolute_url = f'{GELBOORU_BASE_URL}/{url_text.lstrip("/")}'
        return f'{prefix}{html.escape(absolute_url, quote=True)}"'

    return re.sub(r'((?:href|src)\s*=\s*")([^"]*)"', replace_url, html_fragment)


def normalize_display_tag(tag_text: str) -> str:
    return str(tag_text).replace('_', ' ').strip()


def normalize_anchor_display_text(html_fragment: str) -> str:
    def replace_anchor(match: re.Match) -> str:
        open_tag = match.group(1)
        anchor_inner_html = match.group(2)
        close_tag = match.group(3)
        if '<' in anchor_inner_html and '>' in anchor_inner_html:
            return match.group(0)
        normalized_text = html.unescape(anchor_inner_html).replace('_', ' ')
        return f'{open_tag}{html.escape(normalized_text)}{close_tag}'

    return WIKI_ANCHOR_TEXT_PATTERN.sub(replace_anchor, html_fragment)


def normalize_non_link_text_underscores(html_fragment: str) -> str:
    def replace_text_token(match: re.Match) -> str:
        return match.group(0).replace('_', ' ')

    parts = re.split(r'(<[^>]+>)', html_fragment)
    for index, part in enumerate(parts):
        if not part or part.startswith('<'):
            continue
        parts[index] = re.sub(
            r"\b[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+\b",
            replace_text_token,
            part
        )
    return ''.join(parts)


def include_trailing_link_suffixes(html_fragment: str) -> str:
    def replace_anchor_suffix(match: re.Match) -> str:
        open_tag = match.group(1)
        anchor_inner_html = match.group(2)
        close_tag = match.group(3)
        suffix = match.group(4)
        return f'{open_tag}{anchor_inner_html}{html.escape(suffix)}{close_tag}'

    return WIKI_ANCHOR_WITH_SUFFIX_PATTERN.sub(replace_anchor_suffix,
                                               html_fragment)


def simplify_other_wiki_information(html_fragment: str) -> str:
    cleaned_html = WIKI_OTHER_INFO_SECTION_PATTERN.sub('', html_fragment).strip()
    return cleaned_html


def normalize_now_viewing_heading(html_fragment: str) -> str:
    def replace_heading(match: re.Match) -> str:
        normalized_title = normalize_display_tag(html.unescape(match.group(1)))
        return (f'<p style="margin: 0 0 2px 0;">'
                f'<font size="5"><b>Now Viewing: {html.escape(normalized_title)}</b></font></p>')

    return WIKI_VIEW_TITLE_PATTERN.sub(replace_heading, html_fragment)


def strip_tag_type_line(html_fragment: str) -> tuple[str, str]:
    """Remove the "Tag type: X" line from a wiki body; returns
    ``(body, tag type)`` with the type in the colour-key form ('' if absent)."""
    match = WIKI_TAG_TYPE_PATTERN.search(html_fragment)
    if match is None:
        return html_fragment, ''
    tag_type = AUTOCOMPLETE_TAG_TYPES.get(match.group(1).strip().lower(), '')
    return (html_fragment[:match.start()] + html_fragment[match.end():],
            tag_type)


WIKI_LINE_BREAK_PATTERN = r'<br\s*/?>'
WIKI_PARAGRAPH_BREAK_PATTERN = re.compile(
    rf'(?:\s*{WIKI_LINE_BREAK_PATTERN}\s*){{2,}}', re.IGNORECASE)
ALIAS_LINE_PATTERN = re.compile(
    rf'^\s*([^<]+?)\s*(?:{WIKI_LINE_BREAK_PATTERN}\s*)+', re.IGNORECASE)
NOT_ALIAS_CHARACTERS = set('.!?,;:()"\'。、！？：；（）')


def split_alias_line(body_html: str) -> tuple[list[str], str]:
    """Gelbooru wikis often open with an unmarked line of other names
    (e.g. "女の子 少女 girl"). Returns ``(names, remaining body)``; the names
    are only taken when the line is plain text with no sentence punctuation
    and at least one non-ASCII character, so a short English description is
    never mistaken for aliases."""
    match = ALIAS_LINE_PATTERN.match(body_html)
    if match is None:
        return [], body_html
    line = html.unescape(match.group(1)).strip()
    if (not line or line.isascii()
            or any(character in NOT_ALIAS_CHARACTERS for character in line)):
        return [], body_html
    return line.split(), body_html[match.end():]


def paragraphs_from_line_breaks(body_html: str) -> str:
    """Turn Gelbooru's ``<br /><br />`` paragraph gaps into paragraphs with
    the same spacing as the Danbooru wiki."""
    paragraphs = [part.strip() for part in
                  WIKI_PARAGRAPH_BREAK_PATTERN.split(body_html) if part.strip()]
    return ''.join(
        f'<p style="margin: 0 0 {"0" if index == len(paragraphs) - 1 else "0.9em"} 0;">'
        f'{paragraph}</p>' for index, paragraph in enumerate(paragraphs))


def gelbooru_link_target(url_text: str):
    """What a gelbooru.com link points at, as ``(kind, value)``: ``('wiki',
    tag)``, ``('wiki_id', id)``, ``('posts', tags)``, ``('post', id)``, or
    None for anything else."""
    parsed_url = urlparse(html.unescape(str(url_text or '')))
    host = (parsed_url.hostname or '').lower()
    if host and host not in ('gelbooru.com', 'www.gelbooru.com'):
        return None
    if not parsed_url.path.endswith('index.php'):
        return None
    query = {}
    for key, value in parse_qs(parsed_url.query).items():
        query[key[4:] if key.startswith('amp;') else key] = value
    page = (query.get('page') or [''])[0]
    # Gelbooru's own links sometimes repeat "s" (``s=&s=view``).
    actions = query.get('s') or ['']
    item_id = str((query.get('id') or [''])[0]).strip()
    if page == 'wiki':
        search = (query.get('search') or [''])[0].strip()
        if 'list' in actions and search:
            return 'wiki', search
        if 'view' in actions and item_id.isdigit():
            return 'wiki_id', item_id
    elif page == 'post':
        if 'view' in actions and item_id.isdigit():
            return 'post', item_id
        tags = (query.get('tags') or [''])[0].strip()
        if 'list' in actions and tags:
            return 'posts', tags
    return None


def image_url_to_data_url(image_url: str, interruption_check=None) -> str:
    """Download an image and return it as a base64 ``data:`` URL ('' on any
    failure). QTextBrowser can't fetch remote images itself."""
    if not image_url:
        return ''
    if callable(interruption_check) and interruption_check():
        return ''
    try:
        with gelbooru_urlopen(image_url, timeout=10,
                              use_api_key=False) as response:
            if getattr(response, 'status', 200) != 200:
                return ''
            image_bytes = response.read()
            content_type = response.headers.get_content_type() or 'image/jpeg'
    except Exception:
        return ''
    if not image_bytes or not content_type.startswith('image/'):
        return ''
    return (f'data:{content_type};base64,'
            f'{b64encode(image_bytes).decode("ascii")}')


def build_post_summaries_concurrently(posts: list,
                                      interruption_check=None) -> list:
    """Posts with their thumbnail embedded as a data URL, downloaded in
    parallel."""
    def summary_for(post: dict) -> dict:
        preview_url = str(post.get('preview_url') or '')
        data_url = image_url_to_data_url(preview_url, interruption_check)
        reason = ''
        if not data_url:
            reason = "Couldn't load" if preview_url else 'No preview'
        return {'id': str(post.get('id')), 'thumbnail_data_url': data_url,
                'unavailable_reason': reason}

    valid_posts = [post for post in posts if post.get('id')]
    if not valid_posts:
        return []
    with ThreadPoolExecutor(max_workers=FETCH_WORKER_COUNT) as executor:
        return list(executor.map(summary_for, valid_posts))


def wiki_body_link_tags(body_html: str) -> list[str]:
    tags = []
    for href in re.findall(r'href="([^"]+)"', body_html, re.IGNORECASE):
        target = gelbooru_link_target(href)
        if target and target[0] in ('wiki', 'posts') and ' ' not in target[1]:
            tags.append(target[1])
    return tags


class GelbooruWikiFetchThread(QThread):
    fetch_succeeded = Signal(str, dict)
    fetch_failed = Signal(str, str)
    fetch_not_found = Signal(str, str)

    def __init__(self, request_key: str, normalized_tag: str = '',
                 wiki_id: str = ''):
        super().__init__()
        self.request_key = request_key
        self.normalized_tag = normalized_tag
        self.wiki_id = wiki_id.strip()

    def run(self):
        if self.wiki_id:
            self.fetch_wiki_by_id()
            return
        self.fetch_wiki_by_tag()

    def fetch_wiki_by_tag(self):
        search_query = urlencode({'page': 'wiki', 's': 'list',
                                  'search': self.normalized_tag})
        search_url = f'{GELBOORU_BASE_URL}/index.php?{search_query}'
        search_html = self.fetch_text(search_url)
        if search_html is None:
            return
        # An exact match takes Gelbooru straight to the wiki page.
        if WIKI_VIEW_TITLE_PATTERN.search(search_html):
            id_match = WIKI_VIEW_ID_FALLBACK_PATTERN.search(search_html)
            title = self.extract_wiki_title(search_html)
            if normalize_tag_for_lookup(title) == self.normalized_tag:
                self.emit_wiki_page(search_html,
                                    id_match.group(1) if id_match else '')
                return
        wiki_id, wiki_title = self.find_best_wiki_result(search_html)
        if not wiki_id:
            self.fetch_not_found.emit(self.request_key, self.normalized_tag)
            return
        self.fetch_wiki_by_id(wiki_id=wiki_id, fallback_title=wiki_title)

    def fetch_wiki_by_id(self, wiki_id: str = '', fallback_title: str = ''):
        target_wiki_id = wiki_id.strip() or self.wiki_id
        if not target_wiki_id:
            self.fetch_not_found.emit(self.request_key, self.normalized_tag)
            return
        view_query = urlencode({'page': 'wiki', 's': 'view',
                                'id': target_wiki_id})
        view_html = self.fetch_text(
            f'{GELBOORU_BASE_URL}/index.php?{view_query}')
        if view_html is None:
            return
        self.emit_wiki_page(view_html, target_wiki_id, fallback_title)

    def emit_wiki_page(self, view_html: str, wiki_id: str,
                       fallback_title: str = ''):
        title = self.extract_wiki_title(view_html) or fallback_title
        body_html = self.extract_wiki_body_html(view_html)
        if not body_html:
            body_html = '<p>(No wiki content available.)</p>'
        body_html, title_type = strip_tag_type_line(
            absolutize_relative_urls(body_html))
        tag = title.strip().replace(' ', '_')
        posts = []
        tag_types = {}
        if tag and not self.isInterruptionRequested():
            with ThreadPoolExecutor(max_workers=2) as executor:
                posts_future = executor.submit(self.fetch_wiki_posts, tag)
                types_future = executor.submit(
                    fetch_tag_types, wiki_body_link_tags(body_html),
                    self.isInterruptionRequested)
                posts = posts_future.result()
                try:
                    tag_types = types_future.result()
                except Exception:
                    tag_types = {}
        if title_type and tag:
            tag_types[tag] = title_type
        self.fetch_succeeded.emit(self.request_key, {
            'id': wiki_id,
            'title': title,
            'body_html': body_html,
            'tag_types': {name.casefold(): tag_type
                          for name, tag_type in tag_types.items()},
            'posts': posts,
        })

    def fetch_wiki_posts(self, tag: str) -> list:
        try:
            posts, _has_more = search_posts(tag, 1)
        except Exception:
            return []
        return build_post_summaries_concurrently(
            posts[:WIKI_PREVIEW_POST_COUNT], self.isInterruptionRequested)

    def fetch_text(self, url: str) -> str | None:
        try:
            with gelbooru_urlopen(url, timeout=12) as response:
                if response.status != 200:
                    self.fetch_failed.emit(
                        self.request_key,
                        f'Gelbooru returned status {response.status}.')
                    return None
                return response.read().decode('utf-8', errors='replace')
        except Exception as exception:
            self.fetch_failed.emit(self.request_key, str(exception))
            return None

    def find_best_wiki_result(self, search_html: str) -> tuple[str, str]:
        matches = list(WIKI_RESULT_LINK_PATTERN.finditer(search_html))
        if matches:
            for match in matches:
                title_text = html.unescape(match.group(2).strip())
                if normalize_tag_for_lookup(title_text) == self.normalized_tag:
                    return match.group(1).strip(), title_text
            first_match = matches[0]
            return first_match.group(1).strip(), html.unescape(
                first_match.group(2).strip())

        generic_candidates = []
        for generic_match in WIKI_GENERIC_LINK_PATTERN.finditer(search_html):
            href = html.unescape(generic_match.group(1))
            label = html.unescape(generic_match.group(2).strip())
            query = parse_qs(urlparse(href).query)
            if query.get('page', [''])[0] != 'wiki':
                continue
            wiki_id = str(query.get('id', [''])[0]).strip()
            if not wiki_id:
                continue
            if label.casefold() in {'edit', 'history', 'delete', 'lock',
                                    'create', 'list'}:
                continue
            generic_candidates.append((wiki_id, label))
        for wiki_id, label in generic_candidates:
            if normalize_tag_for_lookup(label) == self.normalized_tag:
                return wiki_id, label
        if generic_candidates:
            return generic_candidates[0]

        title_match = WIKI_VIEW_TITLE_PATTERN.search(search_html)
        id_match = WIKI_VIEW_ID_FALLBACK_PATTERN.search(search_html)
        if title_match is None or id_match is None:
            return '', ''
        title_text = html.unescape(
            re.sub(r'<[^>]+>', '', title_match.group(1))).strip()
        return id_match.group(1).strip(), title_text

    def extract_wiki_title(self, view_html: str) -> str:
        title_match = WIKI_VIEW_TITLE_PATTERN.search(view_html)
        if title_match is None:
            return ''
        return html.unescape(
            re.sub(r'<[^>]+>', '', title_match.group(1))).strip()

    def extract_wiki_body_html(self, view_html: str) -> str:
        body_match = WIKI_VIEW_BODY_PATTERN.search(view_html)
        if body_match is None:
            return ''
        return body_match.group(1).strip()


class GelbooruPostsSearchThread(QThread):
    """One page of a tag search, with thumbnails, for the grid view."""
    posts_ready = Signal(str, str, int, list, bool, str)
    posts_failed = Signal(str, str)

    def __init__(self, request_key: str, tags: str, page: int):
        super().__init__()
        self.request_key = request_key
        self.tags = str(tags or '').strip()
        self.page = max(1, int(page or 1))

    def run(self):
        try:
            posts, has_more = search_posts(self.tags, self.page)
        except Exception as exception:
            self.posts_failed.emit(self.request_key, str(exception))
            return
        summaries = build_post_summaries_concurrently(
            posts, self.isInterruptionRequested)
        tag_type = ''
        if ' ' not in self.tags and not self.isInterruptionRequested():
            try:
                types = fetch_tag_types([self.tags])
            except Exception:
                types = {}
            tag_type = next(iter(types.values()), '')
        self.posts_ready.emit(self.request_key, self.tags, self.page,
                              summaries, has_more, tag_type)


class GelbooruPostViewThread(QThread):
    """A single post's details plus its sample image."""
    post_ready = Signal(str, dict)
    post_failed = Signal(str, str)

    def __init__(self, request_key: str, post_id: str):
        super().__init__()
        self.request_key = request_key
        self.post_id = str(post_id).strip()

    def run(self):
        try:
            post = fetch_post(self.post_id)
        except Exception as exception:
            self.post_failed.emit(self.request_key, str(exception))
            return
        if not post:
            self.post_failed.emit(self.request_key, 'Post not found.')
            return
        image_url = (post.get('preview_url') if post.get('is_video')
                     else post.get('sample_url') or post.get('file_url'))
        data_url = image_url_to_data_url(str(image_url or ''),
                                         self.isInterruptionRequested)
        post['_taggui_image_data_url'] = data_url
        post['_taggui_image'] = decode_data_url_image(data_url)
        self.post_ready.emit(self.request_key, post)


class GelbooruTagAutocompleteThread(QThread):
    suggestions_ready = Signal(str, list)

    def __init__(self, query_text: str):
        super().__init__()
        self.query_text = query_text

    def run(self):
        query = self.query_text.strip()
        if not query or query.casefold().startswith(POSTS_SEARCH_PREFIX):
            self.suggestions_ready.emit(self.query_text, [])
            return
        try:
            entries = autocomplete_entries(query.replace(' ', '_'), limit=15)
        except Exception:
            self.suggestions_ready.emit(self.query_text, [])
            return
        suggestions = []
        for entry in entries:
            tag_name = str(entry.get('value') or '').strip()
            if not tag_name:
                continue
            try:
                post_count = int(entry.get('post_count') or 0)
            except (TypeError, ValueError):
                post_count = 0
            suggestions.append({
                'name': tag_name, 'post_count': post_count,
                'category': AUTOCOMPLETE_TAG_TYPES.get(
                    str(entry.get('category') or '').lower(), '')})
        self.suggestions_ready.emit(self.query_text, suggestions)


class GelbooruWikiDialog(OriginalsMixin, BaseWikiDialog):
    SITE_NAME = 'Gelbooru'
    DOWNLOAD_DIRECTORY_SETTING = DOWNLOAD_DIRECTORY_SETTING
    WINDOW_TITLE = 'Gelbooru Wiki'
    EMPTY_TAG_MESSAGE = ('Enter a tag name in the search box to view wiki '
                         'pages from Gelbooru.')
    AUTOCOMPLETE_THREAD_CLASS = GelbooruTagAutocompleteThread

    def __init__(self, parent, tag: str,
                 tag_library_model: TagLibraryModel | None = None,
                 add_to_library_callback=None,
                 add_to_selected_images_callback=None,
                 selected_images_have_tag_callback=None,
                 add_to_current_image_callback=None,
                 current_image_has_tag_callback=None):
        super().__init__(parent)
        self._init_wiki_state(tag_library_model, add_to_library_callback,
                              add_to_selected_images_callback,
                              selected_images_have_tag_callback,
                              add_to_current_image_callback,
                              current_image_has_tag_callback)
        self.current_wiki_id = ''
        self._stored_wiki_title = ''
        self._stored_wiki_body = ''
        self._stored_wiki_posts = []
        # {casefolded tag name: type} for the current wiki page's tags.
        self._stored_tag_types = {}
        # Recomposes the view on screen so a resize can reflow it.
        self._active_composer = None
        self._stored_post = None
        self._posts_view_state = None
        # Ordered post ids of the last grid shown, for prev/next on a post.
        self._grid_context = None
        self._posts_fetch_thread = None
        self._active_posts_threads = []
        # Wiki-less single tags whose posts search found posts, so they can
        # still be added to the Tag Library / images.
        self._valid_posts_tags: set[str] = set()
        self._init_originals_state()
        self._torn_down = False
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(150)
        self._resize_timer.timeout.connect(self._rerender_current_page)
        self._build_wiki_ui()
        self._finish_wiki_init(tag)

    def hide_loading(self):
        """Keep a warning visible while the saved API key isn't working."""
        problem = api_key_problem()
        if not problem:
            self.status_label.setToolTip('')
            super().hide_loading()
            return
        self.show_status_message(
            'Your Gelbooru API key isn\'t working (hover for details).')
        self.status_label.setToolTip(f'<p>{html.escape(problem)}</p>')

    def _teardown(self):
        """Stop the loading animation and worker threads. Safe to call more
        than once; invoked from both ``done()`` and ``closeEvent``."""
        if self._torn_down:
            return
        self._torn_down = True
        self.show_status_message('')
        self._resize_timer.stop()
        # Never blocks: running workers are parked until they finish.
        self.stop_all_fetch_threads()
        self.stop_all_autocomplete_threads(wait=True)
        self.stop_all_posts_threads()
        self._teardown_originals(park_running_threads)
        self.search_autocomplete_timer.stop()
        self._remove_originals_directory()

    def done(self, result):
        self._teardown()
        super().done(result)

    def closeEvent(self, event):
        self._teardown()
        super().closeEvent(event)

    def _open_original_url(self, url: str, timeout: int):
        return gelbooru_urlopen(url, timeout=timeout, use_api_key=False)

    def _original_dimensions(self, post: dict) -> tuple:
        return post.get('width') or 0, post.get('height') or 0

    def stop_all_posts_threads(self):
        park_running_threads(list(self._active_posts_threads))
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

    # ---- Colours ----
    def _is_dark(self) -> bool:
        return self.palette().color(self.backgroundRole()).lightness() < 128

    def _link_color(self) -> str:
        return '#6baef6' if self._is_dark() else '#0066cc'

    def _tag_type_color(self, tag_type) -> str:
        colors = TAG_TYPE_COLORS.get(str(tag_type or ''))
        if not colors:
            return ''
        return colors[1 if self._is_dark() else 0]

    def suggestion_color(self, suggestion: dict) -> str:
        return self._tag_type_color(suggestion.get('category'))

    # ---- Tags and URLs ----
    def normalize_tag(self, tag: str) -> str:
        return str(tag).strip().replace(' ', '_')

    def wiki_url_for_tag(self, normalized_tag: str) -> str:
        return (f'{GELBOORU_BASE_URL}/index.php?page=wiki&s=list&search='
                f'{quote(normalized_tag)}')

    @staticmethod
    def _posts_display_tags(tags: str) -> str:
        return ', '.join(tag.replace('_', ' ') for tag in str(tags).split())

    # ---- History ----
    def current_view(self):
        if 0 <= self.history_index < len(self.tag_history):
            return self.tag_history[self.history_index]
        return None

    def current_tag(self) -> str:
        view = self.current_view()
        if isinstance(view, dict):
            return view.get('tag', '') if view.get('type') == 'wiki' else ''
        return view if isinstance(view, str) else ''

    def current_library_tag(self) -> str:
        tag = super().current_library_tag()
        if tag:
            return tag
        # A wiki-less tag shown as a posts search still counts as a tag.
        view = self.current_view()
        if isinstance(view, dict) and view.get('type') == 'posts':
            tags = str(view.get('tags', '')).strip()
            if tags and ' ' not in tags and tags in self._valid_posts_tags:
                return tags.replace('_', ' ').lower()
        return ''

    def _push_history(self, entry: dict):
        if self.history_index < len(self.tag_history) - 1:
            self.tag_history = self.tag_history[:self.history_index + 1]
        self.tag_history.append(entry)
        self.history_index = len(self.tag_history) - 1

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
        """Re-open a history entry without changing the history."""
        if isinstance(view, str):
            self.load_tag(view, add_to_history=False)
            return
        view_type = view.get('type')
        if view_type == 'wiki':
            self.load_tag(view.get('tag', ''), add_to_history=False)
        elif view_type == 'wiki_id':
            self.load_wiki_id(view.get('id', ''), add_to_history=False)
        elif view_type == 'posts':
            self.load_posts_search(view.get('tags', ''), view.get('page', 1),
                                   add_to_history=False)
        elif view_type == 'post':
            self.load_post(view.get('id', ''), add_to_history=False)

    def _start_view(self, request_key: str, window_title: str):
        self.current_request_key = request_key
        self.setWindowTitle(window_title)
        self._active_composer = None
        self.show_loading()
        self._set_browser_html('')
        self.update_navigation_buttons()
        self.update_add_to_library_button_state()

    # ---- Search ----
    def search_tag(self):
        """A leading "posts:" searches posts instead of opening a wiki page;
        commas separate tags ("posts: dog, cat ears")."""
        raw_text = self.search_line_edit.text().strip()
        if not raw_text.casefold().startswith(POSTS_SEARCH_PREFIX):
            super().search_tag()
            return
        if not self._claim_search_trigger():
            return
        popup = self.search_completer.popup()
        if popup is not None:
            popup.hide()
        self.stop_all_autocomplete_threads()
        tags = []
        for part in raw_text[len(POSTS_SEARCH_PREFIX):].split(','):
            tag = self.normalize_tag(
                self.extract_tag_name_from_search_text(part))
            if tag and tag not in tags:
                tags.append(tag)
        if tags:
            self.load_posts_search(' '.join(tags), 1, add_to_history=True)

    @Slot(str)
    def on_search_text_changed(self, text: str):
        # Drop a pasted autocomplete count like " (1.2k)". Only rewrite the
        # box when one was actually removed: tags such as "nice nature
        # (umamusume)" also contain parentheses, and stripping the text
        # anyway would eat the space typed after them.
        cleaned, removed = AUTOCOMPLETE_COUNT_SUFFIX_PATTERN.subn('', text)
        if removed:
            cleaned = cleaned.strip()
            if cleaned:
                self.search_line_edit.setText(cleaned)
                text = cleaned
        trimmed = text.strip()
        if len(trimmed) < 1:
            self.current_autocomplete_query = ''
            self.autocomplete_display_to_tag = {}
            self.search_autocomplete_model.clear()
            self.search_autocomplete_timer.stop()
            return
        self.current_autocomplete_query = trimmed
        self.search_autocomplete_timer.start()

    # ---- Wiki pages ----
    def _start_wiki_fetch(self, **thread_kwargs):
        fetch_thread = GelbooruWikiFetchThread(
            request_key=self.current_request_key, **thread_kwargs)
        self.fetch_thread = fetch_thread
        self.active_fetch_threads.append(fetch_thread)
        fetch_thread.fetch_succeeded.connect(self.load_wiki_page)
        fetch_thread.fetch_not_found.connect(self.show_not_found)
        fetch_thread.fetch_failed.connect(self.show_error)
        fetch_thread.finished.connect(self.handle_fetch_thread_finished)
        fetch_thread.start()

    def load_tag(self, tag: str, add_to_history: bool):
        normalized_tag = self.normalize_tag(tag)
        if not normalized_tag:
            return
        self.pending_history_append_on_load = False
        if add_to_history:
            self._push_history({'type': 'wiki', 'tag': normalized_tag})
            self.pending_history_update = False
        else:
            self.pending_history_update = True
        display_tag = normalized_tag.replace('_', ' ')
        self.current_wiki_id = ''
        self.set_search_text_silently(display_tag)
        self._start_view(f'tag:{normalized_tag.casefold()}',
                         f'Gelbooru Wiki: {display_tag}')
        self._start_wiki_fetch(normalized_tag=normalized_tag.casefold())

    def load_wiki_id(self, wiki_id: str, add_to_history: bool):
        normalized_wiki_id = str(wiki_id).strip()
        if not normalized_wiki_id:
            return
        self.pending_history_append_on_load = add_to_history
        self.pending_history_update = not add_to_history
        self.current_wiki_id = normalized_wiki_id
        self._start_view(f'id:{normalized_wiki_id}', 'Gelbooru Wiki')
        self._start_wiki_fetch(wiki_id=normalized_wiki_id)

    @Slot(str, dict)
    def load_wiki_page(self, request_key: str, wiki_page: dict):
        if request_key != self.current_request_key:
            return
        title = self.normalize_tag(wiki_page.get('title') or self.current_tag())
        self.current_wiki_id = str(wiki_page.get('id') or '')
        self._stored_wiki_title = title
        self._stored_wiki_body = str(wiki_page.get('body_html') or '')
        self._stored_tag_types = dict(wiki_page.get('tag_types') or {})
        self._stored_wiki_posts = list(wiki_page.get('posts') or [])
        self._active_composer = self._compose_wiki_html
        self._set_browser_html(self._compose_wiki_html())
        self.hide_loading()
        entry = {'type': 'wiki', 'tag': title}
        if self.pending_history_update:
            self.tag_history[self.history_index] = entry
            self.pending_history_update = False
        elif self.pending_history_append_on_load:
            self._push_history(entry)
            self.pending_history_append_on_load = False
            self.update_navigation_buttons()
        display_tag = title.replace('_', ' ')
        self.setWindowTitle(f'Gelbooru Wiki: {display_tag}')
        self.set_search_text_silently(display_tag)
        self.update_add_to_library_button_state()

    def _color_body_tag_links(self, body_html: str) -> str:
        def color_anchor(match: re.Match) -> str:
            target = gelbooru_link_target(match.group(2))
            if not target or target[0] not in ('wiki', 'posts'):
                return match.group(0)
            color = self._tag_type_color(
                self._stored_tag_types.get(target[1].casefold()))
            if not color:
                return match.group(0)
            return (f'{match.group(1)}{match.group(2)}"'
                    f' style="color: {color};"')

        return re.sub(r'(<a\b[^>]*?href=")([^"]*)"', color_anchor, body_html,
                      flags=re.IGNORECASE)

    def _compose_wiki_html(self) -> str:
        title = self._stored_wiki_title
        title_color = (self._tag_type_color(
            self._stored_tag_types.get(title.casefold()))
            or self._link_color())
        body_html = self._stored_wiki_body or '<p>(No wiki content available.)</p>'
        body_html = WIKI_VIEW_TITLE_PATTERN.sub('', body_html)
        body_html = simplify_other_wiki_information(body_html)
        body_html = include_trailing_link_suffixes(body_html)
        body_html = normalize_anchor_display_text(body_html)
        body_html = normalize_non_link_text_underscores(body_html)
        body_html = re.sub(r'^\s*(?:<br\s*/?>\s*)+', '', body_html,
                           flags=re.IGNORECASE)
        body_html = re.sub(r'(?:<br\s*/?>\s*|&nbsp;\s*)+$', '', body_html.strip(),
                           flags=re.IGNORECASE)
        body_html = self._color_body_tag_links(body_html)
        other_names, body_html = split_alias_line(body_html)
        other_names_html = self._build_chips_html(other_names, self._is_dark())
        return (
            f'<h1 style="font-size: 1.5em; color: {title_color}; '
            f'margin: 0 0 4px 0;">{html.escape(title.replace("_", " "))}</h1>'
            f'{other_names_html}'
            f'<div style="line-height: 1.2; margin-top: 4px;">'
            f'{paragraphs_from_line_breaks(body_html)}</div>'
            f'{self._build_wiki_posts_html()}')

    def _build_wiki_posts_html(self) -> str:
        posts = self._stored_wiki_posts
        grid_html = self._post_grid_table_html(posts)
        if not grid_html:
            return ''
        tag = self._stored_wiki_title
        self._grid_context = {'tags': tag, 'page': 1,
                              'ids': [post['id'] for post in posts]}
        view_all_url = POSTS_INTERNAL_LINK_PREFIX + urlencode(
            {'tags': tag, 'page': 1})
        note_color = '#9aa0aa' if self._is_dark() else '#777777'
        return (
            f'<div style="margin-top: 16px; border-top: 1px solid '
            f'{note_color}; padding-top: 6px;">'
            f'<h2 style="font-size: 1.2em; color: {self._link_color()}; '
            'margin: 4px 0;">Posts</h2>'
            f'{grid_html}'
            f'<p style="margin: 4px 0;"><a href="'
            f'{html.escape(view_all_url, quote=True)}">View all posts '
            '&raquo;</a></p></div>')

    @Slot(str, str)
    def show_not_found(self, request_key: str, normalized_tag: str):
        """A tag without a wiki page shows its posts instead."""
        if request_key != self.current_request_key:
            return
        tag = self.posts_tags_from_wiki_tag(normalized_tag)
        if not (request_key.startswith('tag:') and tag):
            super().show_not_found(request_key, normalized_tag)
            return
        self.pending_history_append_on_load = False
        self.pending_history_update = False
        view = self.current_view()
        if isinstance(view, dict) and view.get('type') == 'wiki':
            self.tag_history[self.history_index] = {
                'type': 'posts', 'tags': tag, 'page': 1}
        self.load_posts_search(tag, 1, add_to_history=False)

    # ---- Post grid ----
    def _thumbnail_columns(self, cell_width: int = 170) -> int:
        available = self.content_browser.viewport().width() - 32
        return max(1, int(available / cell_width))

    def _post_thumbnail_cell_html(self, post: dict) -> str:
        post_id = str(post.get('id') or '')
        if not post_id:
            return ''
        internal_url = POST_INTERNAL_LINK_PREFIX + urlencode({'id': post_id})
        thumbnail_data_url = str(post.get('thumbnail_data_url') or '')
        if thumbnail_data_url:
            inner = (f'<img src="{html.escape(thumbnail_data_url, quote=True)}"'
                     ' style="max-width: 150px; max-height: 150px; '
                     'border: 1px solid palette(mid);" '
                     f'alt="Post #{html.escape(post_id)}">')
        else:
            reason = str(post.get('unavailable_reason') or '')
            reason_html = f'<br>({html.escape(reason)})' if reason else ''
            inner = ('<span style="display: inline-block; padding: 24px 10px; '
                     'border: 1px solid palette(mid); color: palette(mid);">'
                     f'post #{html.escape(post_id)}{reason_html}</span>')
        return ('<td align="center" style="padding: 6px; '
                'vertical-align: middle;">'
                f'<a href="{html.escape(internal_url, quote=True)}">{inner}</a>'
                '</td>')

    def _post_grid_table_html(self, posts: list) -> str:
        cells = [cell for cell in map(self._post_thumbnail_cell_html, posts)
                 if cell]
        if not cells:
            return ''
        columns = self._thumbnail_columns()
        rows = ''.join(f'<tr>{"".join(cells[i:i + columns])}</tr>'
                       for i in range(0, len(cells), columns))
        return ('<table style="border-collapse: collapse; '
                f'margin: 8px 0 12px 0;">{rows}</table>')

    def load_posts_search(self, tags: str, page: int,
                          add_to_history: bool = True):
        tags = str(tags or '').strip()
        if not tags:
            return
        page = max(1, int(page or 1))
        if add_to_history:
            self._push_history({'type': 'posts', 'tags': tags, 'page': page})
        display_tags = self._posts_display_tags(tags)
        # A multi-tag search keeps its "posts:" prefix so Enter repeats it.
        self.set_search_text_silently(
            f'posts: {display_tags}' if ' ' in tags else display_tags)
        self._start_view(f'posts:{tags}:{page}',
                         f'Gelbooru Posts: {display_tags} (page {page})')
        posts_thread = GelbooruPostsSearchThread(
            self.current_request_key, tags, page)
        self._posts_fetch_thread = posts_thread
        self._active_posts_threads.append(posts_thread)
        posts_thread.posts_ready.connect(self.display_posts_search)
        posts_thread.posts_failed.connect(self.show_error)
        posts_thread.finished.connect(self.handle_posts_thread_finished)
        posts_thread.start()

    @Slot(str, str, int, list, bool, str)
    def display_posts_search(self, request_key: str, tags: str, page: int,
                             posts: list, has_more: bool, tag_type: str):
        if request_key != self.current_request_key:
            return
        tags = str(tags or '').strip()
        if posts and tags and ' ' not in tags:
            self._valid_posts_tags.add(tags)
        self._posts_view_state = {'tags': tags, 'page': page, 'posts': posts,
                                  'has_more': has_more, 'tag_type': tag_type}
        self._active_composer = self._compose_posts_html
        self._set_browser_html(self._compose_posts_html())
        self.hide_loading()
        self.update_add_to_library_button_state()

    def _compose_posts_html(self) -> str:
        state = self._posts_view_state or {}
        tags = state.get('tags', '')
        page = int(state.get('page', 1) or 1)
        posts = state.get('posts') or []
        link_color = self._link_color()
        heading_color = (self._tag_type_color(state.get('tag_type'))
                         or link_color)
        self._grid_context = {'tags': tags, 'page': page,
                              'ids': [post['id'] for post in posts]}
        heading = (f'<h1 style="font-size: 1.4em; color: {heading_color}; '
                   f'margin: 0 0 6px 0;">Posts: '
                   f'{html.escape(self._posts_display_tags(tags))}</h1>')
        if posts:
            body = self._post_grid_table_html(posts)
        elif page > 1:
            body = '<p>No more posts on this page.</p>'
        else:
            body = '<p>No posts found for this search.</p>'
        nav_parts = []
        if page > 1:
            prev_url = POSTS_INTERNAL_LINK_PREFIX + urlencode(
                {'tags': tags, 'page': page - 1})
            nav_parts.append(f'<a href="{html.escape(prev_url, quote=True)}" '
                             f'style="color: {link_color};">&laquo; Previous</a>')
        nav_parts.append(
            f'<span style="color: palette(mid);">Page {page}</span>')
        if state.get('has_more'):
            next_url = POSTS_INTERNAL_LINK_PREFIX + urlencode(
                {'tags': tags, 'page': page + 1})
            nav_parts.append(f'<a href="{html.escape(next_url, quote=True)}" '
                             f'style="color: {link_color};">Next &raquo;</a>')
        return (heading + body + '<p style="margin: 8px 0;">'
                + ' &nbsp;|&nbsp; '.join(nav_parts) + '</p>')

    # ---- Single post ----
    def load_post(self, post_id: str, add_to_history: bool = True):
        post_id = str(post_id).strip()
        if not post_id:
            return
        if add_to_history:
            neighbors, query, page = [], '', 1
            if self._grid_context and post_id in self._grid_context['ids']:
                neighbors = self._grid_context['ids']
                query = self._grid_context.get('tags', '')
                page = self._grid_context.get('page', 1)
            self._push_history({'type': 'post', 'id': post_id, 'query': query,
                                'neighbors': list(neighbors), 'page': page})
        self._start_view(f'post:{post_id}', f'Gelbooru Post #{post_id}')
        post_thread = GelbooruPostViewThread(self.current_request_key, post_id)
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
        self._update_open_viewer(post)

    def _compose_post_html(self) -> str:
        post = self._stored_post or {}
        if not post:
            return '<p>Post not available.</p>'
        is_dark = self._is_dark()
        view = self.current_view()
        view = view if isinstance(view, dict) else {}
        return (self._build_post_nav_html(str(post.get('id')), view)
                + self._build_post_image_html(post)
                + self._build_post_original_html(post, is_dark)
                + self._build_post_info_html(post, is_dark)
                + self._build_post_tags_html(post))

    def _build_post_nav_html(self, post_id: str, view: dict) -> str:
        link_color = self._link_color()
        neighbors = view.get('neighbors') or []
        query = view.get('query', '')
        parts = []
        if post_id in neighbors:
            index = neighbors.index(post_id)
            for offset, label in ((-1, '&lsaquo; Previous'),
                                  (1, 'Next &rsaquo;')):
                if 0 <= index + offset < len(neighbors):
                    url = POST_INTERNAL_LINK_PREFIX + urlencode(
                        {'id': neighbors[index + offset]})
                    parts.append(f'<a href="{html.escape(url, quote=True)}" '
                                 f'style="color: {link_color};">{label}</a>')
        if query:
            search_url = POSTS_INTERNAL_LINK_PREFIX + urlencode(
                {'tags': query, 'page': view.get('page', 1)})
            parts.append(f'Search: <a href="'
                         f'{html.escape(search_url, quote=True)}" '
                         f'style="color: {link_color};">'
                         f'{html.escape(self._posts_display_tags(query))}</a>')
        if not parts:
            return ''
        return ('<p style="margin: 0 0 6px 0;">'
                + ' &nbsp;|&nbsp; '.join(parts) + '</p>')

    def _post_page_url(self, post_id: str) -> str:
        return (f'{GELBOORU_BASE_URL}/index.php?'
                + urlencode({'page': 'post', 's': 'view', 'id': post_id}))

    def _build_post_image_html(self, post: dict) -> str:
        data_url = str(post.get('_taggui_image_data_url') or '')
        browser_link = (
            '<p style="margin: 4px 0;"><a href="'
            f'{html.escape(self._post_page_url(str(post.get("id"))), quote=True)}'
            '">Open post in browser</a></p>')
        note = ''
        if post.get('is_video'):
            note = ('<p style="color: palette(mid); margin: 4px 0;">'
                    'This post is a video, which can\'t be played here.</p>')
        if not data_url:
            return (note or '<p style="color: palette(mid); margin: 4px 0;">'
                    'This post cannot be previewed here.</p>') + browser_link
        image_html = self._fitted_post_image_html(post)
        if not post.get('is_video'):
            image_html = self._link_image_to_original(post, image_html)
            browser_link = ''
        return (f'{note}<p style="margin: 4px 0;">{image_html}</p>'
                f'{browser_link}')

    def _build_post_info_html(self, post: dict, is_dark: bool) -> str:
        note_color = '#9aa0aa' if is_dark else '#555555'
        rating = str(post.get('rating') or '').capitalize() or '\u2014'
        parts = [f'Rating: {html.escape(rating)}']
        if post.get('width') and post.get('height'):
            parts.append(f'Size: {int(post["width"])}&times;'
                         f'{int(post["height"])}')
        if post.get('score') not in (None, ''):
            parts.append(f'Score: {html.escape(str(post.get("score")))}')
        source = str(post.get('source') or '').strip()
        if source.startswith(('http://', 'https://')):
            parts.append(f'Source: <a href="{html.escape(source, quote=True)}">'
                         f'{html.escape(source)}</a>')
        elif source:
            parts.append(f'Source: {html.escape(source)}')
        return (f'<p style="font-size: 0.9em; color: {note_color}; '
                'margin: 6px 0;">' + ' &nbsp;&bull;&nbsp; '.join(parts)
                + '</p>')

    def _build_post_tags_html(self, post: dict) -> str:
        tag_types = post.get('tag_types') or {}
        grouped = {}
        for name in post.get('tags') or []:
            grouped.setdefault(tag_types.get(name) or 'general', []).append(
                name)
        blocks = []
        for tag_type, label in POST_TAG_SECTIONS:
            names = grouped.get(tag_type)
            if not names:
                continue
            color = self._tag_type_color(tag_type)
            links = ', '.join(
                f'<a href="{WIKI_INTERNAL_LINK_PREFIX}'
                f'{quote(self.normalize_tag(name))}" '
                f'style="color: {color};">'
                f'{html.escape(name.replace("_", " "))}</a>'
                for name in names)
            blocks.append(f'<p style="margin: 3px 0; line-height: 1.6;">'
                          f'<b>{label}:</b> {links}</p>')
        if not blocks:
            return ''
        return ('<div style="margin-top: 10px; border-top: 1px solid '
                'palette(mid); padding-top: 6px;">' + ''.join(blocks)
                + '</div>')

    # ---- Links ----
    def is_external_link(self, url_text: str) -> bool:
        url_text = html.unescape(str(url_text or ''))
        if not url_text:
            return False
        if url_text.startswith((WIKI_INTERNAL_LINK_PREFIX,
                                POST_INTERNAL_LINK_PREFIX,
                                POSTS_INTERNAL_LINK_PREFIX,
                                ORIGINAL_INTERNAL_LINK_PREFIX)):
            return False
        return gelbooru_link_target(url_text) is None

    @Slot(QUrl)
    def handle_anchor_clicked(self, url: QUrl):
        url_text = html.unescape(url.toString())
        if url_text.startswith(WIKI_INTERNAL_LINK_PREFIX):
            self.load_tag(unquote(url_text[len(WIKI_INTERNAL_LINK_PREFIX):]),
                          add_to_history=True)
            return
        if url_text.startswith(POST_INTERNAL_LINK_PREFIX):
            params = parse_qs(url_text[len(POST_INTERNAL_LINK_PREFIX):])
            self.load_post((params.get('id') or [''])[0])
            return
        if url_text.startswith(POSTS_INTERNAL_LINK_PREFIX):
            params = parse_qs(url_text[len(POSTS_INTERNAL_LINK_PREFIX):])
            try:
                page = int((params.get('page') or ['1'])[0])
            except ValueError:
                page = 1
            self.load_posts_search((params.get('tags') or [''])[0], page)
            return
        if self.handle_original_link(url_text):
            return
        target = gelbooru_link_target(url_text)
        if target is not None:
            kind, value = target
            if kind == 'wiki':
                self.load_tag(value, add_to_history=True)
            elif kind == 'wiki_id':
                self.load_wiki_id(value, add_to_history=True)
            elif kind == 'posts':
                self.load_posts_search(value, 1)
            else:
                self.load_post(value)
            return
        QDesktopServices.openUrl(url)

    @Slot()
    def open_current_tag_in_browser(self):
        external_url = self._current_view_external_url()
        if external_url:
            QDesktopServices.openUrl(QUrl(external_url))

    def _current_view_external_url(self) -> str:
        """The gelbooru.com page matching the view on screen."""
        view = self.current_view()
        if isinstance(view, dict):
            view_type = view.get('type')
            if view_type == 'posts':
                page = max(1, int(view.get('page', 1) or 1))
                return (f'{GELBOORU_BASE_URL}/index.php?' + urlencode({
                    'page': 'post', 's': 'list', 'tags': view.get('tags', ''),
                    'pid': (page - 1) * POSTS_PER_PAGE}))
            if view_type == 'post':
                return self._post_page_url(str(view.get('id', '')))
            if view_type == 'wiki_id':
                return (f'{GELBOORU_BASE_URL}/index.php?page=wiki&s=view&id='
                        f'{quote(str(view.get("id", "")))}')
        if self.current_wiki_id:
            return (f'{GELBOORU_BASE_URL}/index.php?page=wiki&s=view&id='
                    f'{quote(self.current_wiki_id)}')
        normalized_tag = self.current_tag()
        return self.wiki_url_for_tag(normalized_tag) if normalized_tag else ''
