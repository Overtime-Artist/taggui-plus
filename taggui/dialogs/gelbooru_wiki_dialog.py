import html
import json
import re
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from PySide6.QtCore import QThread, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QCompleter, QDialog, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QTextBrowser,
                               QVBoxLayout)

from models.tag_library_model import TagLibraryModel
from dialogs.wiki_dialog_base import (AUTOCOMPLETE_ANY_SUFFIX_PATTERN, AUTOCOMPLETE_COUNT_SUFFIX_PATTERN,
                                     BaseWikiDialog, TAG_GROUP_NORMALIZED_PREFIX,
                                     TAG_GROUP_SEARCH_PREFIX)

GELBOORU_BASE_URL = 'https://gelbooru.com'
WIKI_INTERNAL_LINK_PREFIX = 'taggui-gelbooru-wiki://'
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
        view_query = urlencode({'page': 'wiki', 's': 'view', 'id': target_wiki_id})
        view_url = f'{GELBOORU_BASE_URL}/index.php?{view_query}'
        view_html = self.fetch_text(view_url)
        if view_html is None:
            return

        parsed_title = self.extract_wiki_title(view_html) or fallback_title
        body_html = self.extract_wiki_body_html(view_html)
        if not body_html:
            body_html = '<p>(No wiki content available.)</p>'
        wiki_page = {
            'id': target_wiki_id,
            'title': parsed_title,
            'body_html': absolutize_relative_urls(body_html)
        }
        self.fetch_succeeded.emit(self.request_key, wiki_page)

    def fetch_text(self, url: str) -> str | None:
        request = Request(
            url,
            headers={'User-Agent': 'TagGUI/1.0 (Wiki Viewer)'}
        )
        try:
            with urlopen(request, timeout=12) as response:
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
            exact_match = None
            for match in matches:
                wiki_id = match.group(1).strip()
                title_text = html.unescape(match.group(2).strip())
                if normalize_tag_for_lookup(title_text) == self.normalized_tag:
                    exact_match = (wiki_id, title_text)
                    break
            if exact_match is not None:
                return exact_match
            first_match = matches[0]
            return first_match.group(1).strip(), html.unescape(
                first_match.group(2).strip())

        generic_candidates = []
        for generic_match in WIKI_GENERIC_LINK_PATTERN.finditer(search_html):
            href = html.unescape(generic_match.group(1))
            label = html.unescape(generic_match.group(2).strip())
            parsed_href = urlparse(href)
            query = parse_qs(parsed_href.query)
            if query.get('page', [''])[0] != 'wiki':
                continue
            wiki_id = str(query.get('id', [''])[0]).strip()
            if not wiki_id:
                continue
            normalized_label = label.casefold()
            if normalized_label in {'edit', 'history', 'delete', 'lock',
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
        title_text = html.unescape(re.sub(r'<[^>]+>', '', title_match.group(1))).strip()
        wiki_id = id_match.group(1).strip()
        if not wiki_id:
            return '', ''
        return wiki_id, title_text

    def extract_wiki_title(self, view_html: str) -> str:
        title_match = WIKI_VIEW_TITLE_PATTERN.search(view_html)
        if title_match is None:
            return ''
        return html.unescape(re.sub(r'<[^>]+>', '', title_match.group(1))).strip()

    def extract_wiki_body_html(self, view_html: str) -> str:
        body_match = WIKI_VIEW_BODY_PATTERN.search(view_html)
        if body_match is None:
            return ''
        return body_match.group(1).strip()


class GelbooruTagAutocompleteThread(QThread):
    suggestions_ready = Signal(str, list)
    tag_group_titles_cache = None

    def __init__(self, query_text: str):
        super().__init__()
        self.query_text = query_text

    def run(self):
        query = self.query_text.strip()
        if not query:
            self.suggestions_ready.emit(self.query_text, [])
            return
        query_normalized = query.replace(' ', '_')
        is_tag_group_query = (
            query.casefold().startswith(TAG_GROUP_SEARCH_PREFIX)
            or query_normalized.casefold().startswith(TAG_GROUP_NORMALIZED_PREFIX)
        )
        if is_tag_group_query:
            self.fetch_tag_group_suggestions(query_normalized)
            return
        autocomplete_query = urlencode({
            'page': 'autocomplete2',
            'term': query.replace(' ', '_')
        })
        autocomplete_url = f'{GELBOORU_BASE_URL}/index.php?{autocomplete_query}'
        request = Request(
            autocomplete_url,
            headers={'User-Agent': 'TagGUI/1.0 (Wiki Viewer)'}
        )
        try:
            with urlopen(request, timeout=8) as response:
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
            if not isinstance(tag, dict):
                continue
            tag_name = str(tag.get('value') or '').strip()
            if not tag_name:
                continue
            tag_count = int(tag.get('post_count') or 0)
            suggestions.append({'name': tag_name, 'post_count': tag_count})
        self.suggestions_ready.emit(self.query_text, suggestions)

    def fetch_tag_group_suggestions(self, normalized_query_text: str):
        query_text = normalized_query_text
        if query_text.casefold().startswith(TAG_GROUP_SEARCH_PREFIX):
            query_text = (TAG_GROUP_NORMALIZED_PREFIX
                          + query_text[len(TAG_GROUP_SEARCH_PREFIX):].lstrip())
        if not query_text.casefold().startswith(TAG_GROUP_NORMALIZED_PREFIX):
            self.suggestions_ready.emit(self.query_text, [])
            return
        group_filter = (query_text[len(TAG_GROUP_NORMALIZED_PREFIX):]
                        .strip().replace(' ', '_').casefold())
        titles = self.get_tag_group_titles()
        suggestions = []
        for title in titles:
            suffix = title[len(TAG_GROUP_NORMALIZED_PREFIX):].casefold()
            if group_filter and group_filter not in suffix:
                continue
            suggestions.append({'name': title, 'post_count': 0, 'is_tag_group': True})
        suggestions.sort(key=lambda s: s['name'])
        self.suggestions_ready.emit(self.query_text, suggestions[:15])

    def get_tag_group_titles(self) -> list:
        if GelbooruTagAutocompleteThread.tag_group_titles_cache is not None:
            return GelbooruTagAutocompleteThread.tag_group_titles_cache
        # Search without the colon to avoid Gelbooru redirecting to an exact-match page
        search_url = (f'{GELBOORU_BASE_URL}/index.php?'
                      + urlencode({'page': 'wiki', 's': 'list', 'search': 'tag_group'}))
        request = Request(search_url, headers={'User-Agent': 'TagGUI/1.0 (Wiki Viewer)'})
        try:
            with urlopen(request, timeout=10) as response:
                if response.status != 200:
                    GelbooruTagAutocompleteThread.tag_group_titles_cache = []
                    return []
                search_html = response.read().decode('utf-8', errors='replace')
        except Exception:
            GelbooruTagAutocompleteThread.tag_group_titles_cache = []
            return []
        titles = []
        seen: set = set()
        for m in WIKI_RESULT_LINK_PATTERN.finditer(search_html):
            title = html.unescape(m.group(2).strip()).replace(' ', '_')
            title_key = title.casefold()
            if not title_key.startswith(TAG_GROUP_NORMALIZED_PREFIX):
                continue
            if title_key in seen:
                continue
            seen.add(title_key)
            titles.append(title)
        GelbooruTagAutocompleteThread.tag_group_titles_cache = titles
        return titles


class GelbooruWikiDialog(BaseWikiDialog):
    SITE_NAME = 'Gelbooru'
    WINDOW_TITLE = 'Gelbooru Wiki'
    EMPTY_TAG_MESSAGE = ('Enter a tag name in the search box to view wiki '
                         'pages from Gelbooru.')
    AUTOCOMPLETE_THREAD_CLASS = GelbooruTagAutocompleteThread

    def __init__(self, parent, tag: str,
                 tag_library_model: TagLibraryModel | None = None,
                 add_to_library_callback=None,
                 add_to_selected_images_callback=None,
                 selected_images_have_tag_callback=None):
        super().__init__(parent)
        self._init_wiki_state(tag_library_model, add_to_library_callback,
                              add_to_selected_images_callback,
                              selected_images_have_tag_callback)
        self.current_wiki_id = ''
        self._torn_down = False
        self._build_wiki_ui()
        self._finish_wiki_init(tag)


    def _teardown(self):
        """Stop the loading animation and worker threads. Safe to call more
        than once; invoked from both ``done()`` and ``closeEvent`` so cleanup
        always runs before the dialog is destroyed on close."""
        if self._torn_down:
            return
        self._torn_down = True
        # Stop the animated (indeterminate) loading bar first, on the GUI
        # thread, so its internal timer isn't left running while we block
        # below waiting for worker threads to finish.
        self.show_status_message('')
        self.stop_all_fetch_threads()
        self.stop_all_autocomplete_threads(wait=True)
        self.search_autocomplete_timer.stop()

    def done(self, result):
        self._teardown()
        super().done(result)

    def closeEvent(self, event):
        self._teardown()
        super().closeEvent(event)


    def normalize_tag(self, tag: str) -> str:
        return str(tag).strip().replace(' ', '_')


    def wiki_url_for_tag(self, normalized_tag: str) -> str:
        return (f'{GELBOORU_BASE_URL}/index.php?page=wiki&s=list&search='
                f'{quote(normalized_tag)}')


    def load_tag(self, tag: str, add_to_history: bool):
        normalized_tag = self.normalize_tag(tag)
        if not normalized_tag:
            return
        self.pending_history_append_on_load = False
        if add_to_history:
            if self.history_index < len(self.tag_history) - 1:
                self.tag_history = self.tag_history[:self.history_index + 1]
            self.tag_history.append(normalized_tag)
            self.history_index = len(self.tag_history) - 1
            self.pending_history_update = False
        else:
            self.pending_history_update = True
        display_tag = normalized_tag.replace('_', ' ')
        self.current_request_key = f'tag:{normalized_tag.casefold()}'
        self.current_wiki_id = ''
        self.setWindowTitle(f'Gelbooru Wiki: {display_tag}')
        self.set_search_text_silently(display_tag)
        self.show_loading()
        self._set_browser_html('')
        self.update_navigation_buttons()
        self.update_add_to_library_button_state()
        fetch_thread = GelbooruWikiFetchThread(
            request_key=self.current_request_key,
            normalized_tag=normalized_tag.casefold()
        )
        self.fetch_thread = fetch_thread
        self.active_fetch_threads.append(fetch_thread)
        fetch_thread.fetch_succeeded.connect(self.load_wiki_page)
        fetch_thread.fetch_not_found.connect(self.show_not_found)
        fetch_thread.fetch_failed.connect(self.show_error)
        fetch_thread.finished.connect(self.handle_fetch_thread_finished)
        fetch_thread.start()

    def load_wiki_id(self, wiki_id: str, add_to_history: bool):
        normalized_wiki_id = str(wiki_id).strip()
        if not normalized_wiki_id:
            return
        self.pending_history_append_on_load = add_to_history
        self.pending_history_update = False
        self.current_request_key = f'id:{normalized_wiki_id}'
        self.current_wiki_id = normalized_wiki_id
        self.setWindowTitle('Gelbooru Wiki')
        self.show_loading()
        self._set_browser_html('')
        self.update_navigation_buttons()
        self.update_add_to_library_button_state()
        fetch_thread = GelbooruWikiFetchThread(
            request_key=self.current_request_key,
            wiki_id=normalized_wiki_id
        )
        self.fetch_thread = fetch_thread
        self.active_fetch_threads.append(fetch_thread)
        fetch_thread.fetch_succeeded.connect(self.load_wiki_page)
        fetch_thread.fetch_not_found.connect(self.show_not_found)
        fetch_thread.fetch_failed.connect(self.show_error)
        fetch_thread.finished.connect(self.handle_fetch_thread_finished)
        fetch_thread.start()


    @Slot(str, dict)
    def load_wiki_page(self, request_key: str, wiki_page: dict):
        if request_key != self.current_request_key:
            return
        title = self.normalize_tag(wiki_page.get('title', self.current_tag()))
        body_html = str(wiki_page.get('body_html') or '<p>(No wiki content available.)</p>')
        body_html = simplify_other_wiki_information(body_html)
        body_html = normalize_now_viewing_heading(body_html)
        body_html = include_trailing_link_suffixes(body_html)
        body_html = normalize_anchor_display_text(body_html)
        body_html = normalize_non_link_text_underscores(body_html)
        self.current_wiki_id = str(wiki_page.get('id') or '')
        is_dark = self.palette().color(self.backgroundRole()).lightness() < 128
        title_color = '#6baef6' if is_dark else '#0066cc'
        self._set_browser_html(
            f'<div style="line-height: 1.4;">{body_html}</div>'
        )
        self.hide_loading()
        if self.pending_history_update:
            self.tag_history[self.history_index] = title
            self.pending_history_update = False
        elif self.pending_history_append_on_load:
            if self.history_index < len(self.tag_history) - 1:
                self.tag_history = self.tag_history[:self.history_index + 1]
            self.tag_history.append(title)
            self.history_index = len(self.tag_history) - 1
            self.pending_history_append_on_load = False
            self.update_navigation_buttons()
        self.setWindowTitle(f'Gelbooru Wiki: {title.replace("_", " ")}')
        self.set_search_text_silently(title.replace('_', ' '))
        self.update_add_to_library_button_state()


    def is_external_link(self, url_text: str) -> bool:
        url_text = html.unescape(str(url_text or ''))
        if not url_text:
            return False
        if url_text.startswith(WIKI_INTERNAL_LINK_PREFIX):
            return False
        parsed_url = urlparse(url_text)
        is_gelbooru_host = (not parsed_url.netloc
                            or parsed_url.netloc.endswith('gelbooru.com'))
        if is_gelbooru_host and parsed_url.path.endswith('index.php'):
            raw_query = parse_qs(parsed_url.query)
            query = {}
            for key, value in raw_query.items():
                normalized_key = key[4:] if key.startswith('amp;') else key
                query[normalized_key] = value
            if query.get('page', [''])[0] == 'wiki':
                if query.get('s', [''])[0] == 'list' and query.get('search'):
                    return False
                if str(query.get('id', [''])[0]).strip():
                    return False
        return True

    @Slot(QUrl)
    def handle_anchor_clicked(self, url: QUrl):
        url_text = html.unescape(url.toString())
        if url_text.startswith(WIKI_INTERNAL_LINK_PREFIX):
            self.load_tag(unquote(url_text[len(WIKI_INTERNAL_LINK_PREFIX):]),
                          add_to_history=True)
            return
        parsed_url = urlparse(url_text)
        is_gelbooru_host = (not parsed_url.netloc
                            or parsed_url.netloc.endswith('gelbooru.com'))
        if is_gelbooru_host and parsed_url.path.endswith('index.php'):
            raw_query = parse_qs(parsed_url.query)
            query = {}
            for key, value in raw_query.items():
                normalized_key = key[4:] if key.startswith('amp;') else key
                query[normalized_key] = value
            if query.get('page', [''])[0] == 'wiki':
                if query.get('s', [''])[0] == 'list' and query.get('search'):
                    self.load_tag(unquote(query['search'][0]), add_to_history=True)
                    return
                wiki_id = str(query.get('id', [''])[0]).strip()
                if wiki_id:
                    self.load_wiki_id(wiki_id, add_to_history=True)
                    return
        QDesktopServices.openUrl(url)

    @Slot(str)
    def on_search_text_changed(self, text: str):
        if '(' in text and ')' in text:
            cleaned = AUTOCOMPLETE_COUNT_SUFFIX_PATTERN.sub('', text).strip()
            if cleaned and cleaned != text:
                self.search_line_edit.setText(cleaned)
                return
        trimmed = text.strip()
        if len(trimmed) < 1:
            self.current_autocomplete_query = ''
            self.autocomplete_display_to_tag = {}
            self.search_autocomplete_model.clear()
            self.search_autocomplete_timer.stop()
            return
        self.current_autocomplete_query = trimmed
        self.search_autocomplete_timer.start()


    @Slot()
    def open_current_tag_in_browser(self):
        if self.current_wiki_id:
            QDesktopServices.openUrl(QUrl(
                f'{GELBOORU_BASE_URL}/index.php?page=wiki&s=view&id={quote(self.current_wiki_id)}'))
            return
        normalized_tag = self.current_tag()
        if normalized_tag:
            QDesktopServices.openUrl(QUrl(self.wiki_url_for_tag(normalized_tag)))
