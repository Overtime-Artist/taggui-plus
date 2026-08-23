import html
import re

from PySide6.QtCore import QEvent, QMimeData, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import (QPalette, QStandardItem, QStandardItemModel,
                           QTextCursor)
from PySide6.QtWidgets import (QCompleter, QDialog, QHBoxLayout, QLabel,
                               QLineEdit, QProgressBar, QPushButton,
                               QStackedWidget, QTextBrowser, QVBoxLayout)

# Unicode object-replacement character that Qt uses to represent an embedded
# image (such as a synonym chip) inside a text document.
OBJECT_REPLACEMENT_CHARACTER = '\ufffc'
# Unicode paragraph separator that ``QTextCursor.selectedText`` inserts between
# blocks; converted back to a newline when copying.
PARAGRAPH_SEPARATOR_CHARACTER = '\u2029'


class ChipAwareTextBrowser(QTextBrowser):
    """A ``QTextBrowser`` that copies embedded chip images as their text.

    Synonym chips are drawn as small images (so their rounded corners render
    correctly), which means a normal copy would omit their text. This browser
    keeps a mapping from each chip image's source URL to the tag text it shows
    and substitutes that text when the user copies a selection, so chips can be
    selected and copied like ordinary text without changing how they look.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image_text_by_source = {}

    def register_image_text(self, source: str, text: str):
        """Remember that the chip image at ``source`` displays ``text``."""
        if source:
            self._image_text_by_source[source] = text

    def createMimeDataFromSelection(self):
        base_mime_data = super().createMimeDataFromSelection()
        cursor = self.textCursor()
        if not cursor.hasSelection() or not self._image_text_by_source:
            return base_mime_data
        start = cursor.selectionStart()
        end = cursor.selectionEnd()
        document = self.document()
        probe = QTextCursor(document)
        pieces = []
        position = start
        while position < end:
            probe.setPosition(position)
            probe.setPosition(position + 1,
                              QTextCursor.MoveMode.KeepAnchor)
            character = probe.selectedText()
            if character == OBJECT_REPLACEMENT_CHARACTER:
                char_format = probe.charFormat()
                source = ''
                if char_format.isImageFormat():
                    source = char_format.toImageFormat().name()
                pieces.append(self._image_text_by_source.get(source, ''))
            else:
                pieces.append(character)
            position += 1
        plain_text = ''.join(pieces).replace(
            PARAGRAPH_SEPARATOR_CHARACTER, '\n')
        # A fresh QMimeData is required: setting the text on the object returned
        # by the base class does not stick because its plain text is derived
        # from the selection's HTML (where chips are images, not text).
        mime_data = QMimeData()
        mime_data.setText(plain_text)
        if base_mime_data.hasHtml():
            mime_data.setHtml(base_mime_data.html())
        return mime_data

# Shared regexes/constants used by both the Danbooru and Gelbooru wiki dialogs.
AUTOCOMPLETE_COUNT_SUFFIX_PATTERN = re.compile(
    r'\s+\((?:\d+(?:\.\d+)?[MmKk]?|\d+)\)$'
)
AUTOCOMPLETE_ANY_SUFFIX_PATTERN = re.compile(r'\s*\([^)]+\)\s*$')
TAG_GROUP_SEARCH_PREFIX = 'tag group:'
TAG_GROUP_NORMALIZED_PREFIX = 'tag_group:'


class TagNameOnlyCompleter(QCompleter):
    """Completer that inserts only the tag name (without the count/label
    suffix) into the line edit."""

    def __init__(self, model, parent=None):
        super().__init__(model, parent)
        self.display_to_tag = {}
        self.prev_completion = ''

    def setCompletionPrefix(self, prefix: str):
        self.prev_completion = prefix
        super().setCompletionPrefix(prefix)

    def pathFromIndex(self, index) -> str:
        """Controls what text Qt puts in the line edit during navigation and
        on activation."""
        completion = super().pathFromIndex(index)
        tag_name = self.display_to_tag.get(completion, '')
        if tag_name:
            return tag_name.replace('_', ' ')
        return AUTOCOMPLETE_ANY_SUFFIX_PATTERN.sub(
            '', completion).strip().replace('_', ' ')

    def insertText(self, completion: str):
        tag_name = self.display_to_tag.get(completion, '')
        if tag_name:
            super().insertText(tag_name.replace('_', ' '))
        else:
            clean_text = AUTOCOMPLETE_ANY_SUFFIX_PATTERN.sub(
                '', completion).strip()
            super().insertText(clean_text.replace('_', ' '))


class BaseWikiDialog(QDialog):
    """Shared scaffolding for the Danbooru and Gelbooru wiki dialogs.

    Subclasses provide the site-specific pieces:
      * class attributes ``SITE_NAME``, ``WINDOW_TITLE``, ``EMPTY_TAG_MESSAGE``
        and ``AUTOCOMPLETE_THREAD_CLASS``;
      * the fetch/parse/render methods that differ per site (``load_tag``,
        ``load_wiki_page``, ``normalize_tag``, ``wiki_url_for_tag``,
        ``handle_anchor_clicked``, ``on_search_text_changed``,
        ``open_current_tag_in_browser`` and any site-specific helpers).

    Subclass ``__init__`` should call ``super().__init__(parent)``,
    ``self._init_wiki_state(tag_library_model)``, set any site-specific
    attributes, then ``self._build_wiki_ui()`` and ``self._finish_wiki_init(tag)``.
    """

    SITE_NAME = 'Wiki'
    WINDOW_TITLE = 'Wiki'
    EMPTY_TAG_MESSAGE = ''
    AUTOCOMPLETE_THREAD_CLASS = None

    def _init_wiki_state(self, tag_library_model,
                         add_to_library_callback=None,
                         add_to_selected_images_callback=None,
                         selected_images_have_tag_callback=None,
                         add_to_current_image_callback=None,
                         current_image_has_tag_callback=None):
        self.tag_library_model = tag_library_model
        self.add_to_library_callback = add_to_library_callback
        self.add_to_selected_images_callback = add_to_selected_images_callback
        self.selected_images_have_tag_callback = (
            selected_images_have_tag_callback)
        # Grouped (grid) view only: add the tag to just the current image.
        self.add_to_current_image_callback = add_to_current_image_callback
        self.current_image_has_tag_callback = current_image_has_tag_callback
        self.tag_history = []
        self.history_index = -1
        self.fetch_thread = None
        self.active_fetch_threads = []
        self.current_request_key = ''
        self.autocomplete_thread = None
        self.active_autocomplete_threads = []
        self.current_autocomplete_query = ''
        self.autocomplete_display_to_tag = {}
        self.pending_history_update = False
        self.pending_history_append_on_load = False
        # One-shot flag: when set, the next silent search-box text update also
        # re-selects the whole text. Used so a wiki opened pre-filled with a tag
        # keeps its text highlighted after the async page load rewrites the box.
        self._select_all_on_next_silent_set = False

    def _build_wiki_ui(self):
        self.setWindowTitle(self.WINDOW_TITLE)
        self.setMinimumSize(750, 500)
        self.resize(900, 650)

        self.status_label = QLabel('')
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft
                                       | Qt.AlignmentFlag.AlignVCenter)

        # Indeterminate "busy" loading bar shown while a wiki page is being
        # fetched. Its chunk uses the palette Highlight colour (the same blue
        # used throughout the app) and is refreshed on show so it follows the
        # current light/dark theme.
        self.loading_bar = QProgressBar()
        self.loading_bar.setRange(0, 0)
        self.loading_bar.setTextVisible(False)

        # Swap between the text status and the loading bar without changing the
        # row height, so the layout never jumps. Pin the row to one text line
        # so it stays compact (the progress bar fills that height).
        self.status_stack = QStackedWidget()
        self.status_stack.addWidget(self.status_label)
        self.status_stack.addWidget(self.loading_bar)
        self.status_stack.setContentsMargins(0, 0, 0, 0)
        self.status_stack.setFixedHeight(self.status_label.sizeHint().height())

        self.back_button = QPushButton('Back')
        self.forward_button = QPushButton('Forward')
        self.search_line_edit = QLineEdit()
        self.search_line_edit.setPlaceholderText('Search wiki tag')
        self.search_line_edit.setClearButtonEnabled(True)
        self.search_autocomplete_model = QStandardItemModel(self)
        self.search_completer = TagNameOnlyCompleter(
            self.search_autocomplete_model, self)
        self.search_completer.setCaseSensitivity(
            Qt.CaseSensitivity.CaseInsensitive)
        self.search_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.search_completer.setCompletionMode(
            QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self.search_completer.activated.connect(
            self.apply_autocomplete_selection)
        self.search_line_edit.setCompleter(self.search_completer)
        # Trigger autocomplete searches only from ``textEdited`` (genuine user
        # typing), NOT ``textChanged``. When the user arrows through the
        # autocomplete popup, Qt's completer writes the highlighted entry into
        # the line edit programmatically; that fires ``textChanged`` but not
        # ``textEdited``. Driving the search from ``textEdited`` therefore keeps
        # the suggestion list stable during arrow-key navigation and only
        # refreshes it when the user actually edits the text.
        self.search_line_edit.textEdited.connect(self.on_search_text_changed)
        # Still watch ``textChanged`` so clearing the box (e.g. the clear
        # button, which changes the text programmatically) resets the
        # autocomplete state and hides the popup.
        self.search_line_edit.textChanged.connect(
            self._reset_autocomplete_if_search_empty)
        self.search_autocomplete_timer = QTimer(self)
        self.search_autocomplete_timer.setSingleShot(True)
        self.search_autocomplete_timer.setInterval(180)
        self.search_autocomplete_timer.timeout.connect(
            self.run_autocomplete_search)
        self.search_button = QPushButton('Search')
        self.add_to_library_button = QPushButton('Add to Tag Library')
        self.add_to_images_button = QPushButton('Add to Selected Images')
        self.add_to_current_image_button = QPushButton('Add to Current Image')
        for button in (self.back_button, self.forward_button,
                       self.search_button, self.add_to_library_button,
                       self.add_to_images_button,
                       self.add_to_current_image_button):
            button.setAutoDefault(False)
            button.setDefault(False)
        self.back_button.clicked.connect(self.navigate_back)
        self.forward_button.clicked.connect(self.navigate_forward)
        self.search_button.clicked.connect(self.search_tag)
        self.add_to_library_button.clicked.connect(
            self.add_current_tag_to_library)
        self.add_to_images_button.clicked.connect(
            self.add_current_tag_to_selected_images)
        self.add_to_current_image_button.clicked.connect(
            self.add_current_tag_to_current_image)
        # The "Add to Selected Images" button is only useful when a callback
        # that knows how to reach the Images pane has been provided.
        self.add_to_images_button.setVisible(
            self.add_to_selected_images_callback is not None)
        # The "Add to Current Image" button is only shown in the grouped (grid)
        # view, signalled by the host passing this callback.
        self.add_to_current_image_button.setVisible(
            self.add_to_current_image_callback is not None)
        self.search_line_edit.returnPressed.connect(self.search_tag)

        nav_layout = QHBoxLayout()
        nav_layout.addWidget(self.back_button)
        nav_layout.addWidget(self.forward_button)
        nav_layout.addWidget(self.search_line_edit, stretch=1)
        nav_layout.addWidget(self.search_button)
        nav_layout.addStretch()

        self.content_browser = ChipAwareTextBrowser(self)
        self.content_browser.setOpenExternalLinks(False)
        self.content_browser.setOpenLinks(False)
        self.content_browser.anchorClicked.connect(self.handle_anchor_clicked)
        self.content_browser.highlighted.connect(self.on_link_hovered)

        self.open_browser_button = QPushButton('Open in Browser')
        self.open_browser_button.clicked.connect(
            self.open_current_tag_in_browser)
        self.open_browser_button.setAutoDefault(False)
        self.open_browser_button.setDefault(False)

        footer_layout = QHBoxLayout()
        footer_layout.addWidget(self.add_to_library_button)
        footer_layout.addWidget(self.add_to_images_button)
        footer_layout.addWidget(self.add_to_current_image_button)
        footer_layout.addStretch()
        footer_layout.addWidget(self.open_browser_button)

        # Floating status line shown at the bottom of the content area (over the
        # wiki text) that reveals the destination of an external link while the
        # pointer hovers over it. Parented to the browser (NOT its viewport) so
        # it stays pinned while the text scrolls — QTextBrowser scrolls its
        # viewport's child widgets along with the content, but children of the
        # browser frame itself stay put.
        self.hover_status_label = QLabel(self.content_browser)
        self.hover_status_label.setWordWrap(False)
        self.hover_status_label.setTextFormat(Qt.TextFormat.RichText)
        self.hover_status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.NoTextInteraction)
        self.hover_status_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hover_status_label.hide()
        self.content_browser.installEventFilter(self)

        layout = QVBoxLayout(self)
        layout.addLayout(nav_layout)
        layout.addWidget(self.status_stack)
        layout.addWidget(self.content_browser)
        layout.addLayout(footer_layout)

    def _finish_wiki_init(self, tag: str):
        self.load_tag(tag, add_to_history=True)
        if not tag.strip():
            self._set_browser_html(
                '<div style="color: #888; font-style: italic; margin: 20px;">'
                f'{self.EMPTY_TAG_MESSAGE}'
                '</div>')
            self.show_status_message('Ready.')
        self.search_line_edit.setFocus()
        # When the wiki opens pre-filled with a tag, highlight the whole search
        # text so the user can immediately type over it to start a new search.
        # The synchronous text set inside `load_tag` has already run above; the
        # asynchronous fetch completion will later rewrite the search box with
        # the resolved tag title (via `set_search_text_silently`), which would
        # clear any selection we make now. So we both select immediately (covers
        # instant/cached loads) and arm a one-shot flag so the text is
        # re-selected once that async rewrite lands. This runs only on the
        # initial open; later searches are unaffected.
        if tag.strip():
            self.search_line_edit.selectAll()
            self._select_all_on_next_silent_set = True

    def _apply_loading_bar_theme(self):
        """Colour the loading bar from the active palette (theme-aware)."""
        palette = self.palette()
        highlight = palette.color(QPalette.ColorRole.Highlight)
        groove = palette.color(QPalette.ColorRole.Base)
        self.loading_bar.setStyleSheet(
            'QProgressBar {'
            f' border: 1px solid {groove.darker(115).name()};'
            ' border-radius: 3px;'
            f' background-color: {groove.name()};'
            ' }'
            'QProgressBar::chunk {'
            f' background-color: {highlight.name()};'
            ' border-radius: 3px;'
            ' }')

    def show_loading(self):
        """Show the animated loading bar in place of the status text."""
        self._apply_loading_bar_theme()
        self.status_stack.setCurrentWidget(self.loading_bar)

    def show_status_message(self, message: str):
        """Show a text status message (hides the loading bar)."""
        self.status_label.setText(message)
        self.status_stack.setCurrentWidget(self.status_label)

    def hide_loading(self):
        """Finished loading successfully: clear the bar and any status text."""
        self.show_status_message('')

    def is_external_link(self, url_text: str) -> bool:
        """Return True when clicking ``url_text`` would open outside the app.

        Subclasses override this to match their ``handle_anchor_clicked``
        routing. The default treats any absolute http(s) URL as external.
        """
        url_text = str(url_text or '')
        return (url_text.startswith('http://')
                or url_text.startswith('https://'))

    @Slot(QUrl)
    def on_link_hovered(self, url: QUrl):
        """Show the destination of an external link in a floating status line
        at the bottom of the content area while the pointer hovers over it;
        hide it otherwise."""
        url_text = html.unescape(url.toString().strip())
        if not url_text or not self.is_external_link(url_text):
            self.hover_status_label.hide()
            self.hover_status_label.clear()
            return
        palette = self.palette()
        is_dark = palette.color(self.backgroundRole()).lightness() < 128
        tag_color = '#e0803a' if is_dark else '#b5651d'
        url_color = '#d7dae0' if is_dark else '#222222'
        panel_bg = palette.color(QPalette.ColorRole.Window)
        panel_bg.setAlpha(235)
        border_color = ('rgba(255, 255, 255, 40)' if is_dark
                        else 'rgba(0, 0, 0, 40)')
        self.hover_status_label.setStyleSheet(
            'QLabel {'
            f' background-color: rgba({panel_bg.red()}, {panel_bg.green()}, '
            f'{panel_bg.blue()}, {panel_bg.alpha()});'
            f' border: 1px solid {border_color};'
            ' border-left: none; border-bottom: none;'
            ' border-top-right-radius: 4px;'
            ' padding: 2px 8px;'
            ' }')
        self.hover_status_label.setText(
            f'<span style="color: {tag_color}; font-weight: bold;">'
            f'[EXTERNAL LINK]</span> '
            f'<span style="color: {url_color};">{html.escape(url_text)}</span>')
        self._reposition_hover_status_label()
        self.hover_status_label.show()
        self.hover_status_label.raise_()

    def _reposition_hover_status_label(self):
        """Pin the floating hover label to the bottom-left of the browser
        frame, clamped to its width and kept above any horizontal scrollbar."""
        browser = self.content_browser
        frame = browser.frameWidth()
        h_scrollbar = browser.horizontalScrollBar()
        scrollbar_height = (h_scrollbar.height()
                            if h_scrollbar and h_scrollbar.isVisible() else 0)
        self.hover_status_label.setMaximumWidth(
            max(0, browser.width() - 2 * frame))
        self.hover_status_label.adjustSize()
        label_height = self.hover_status_label.height()
        self.hover_status_label.move(
            frame, browser.height() - frame - scrollbar_height - label_height)

    def eventFilter(self, watched, event):
        if (watched is self.content_browser
                and event.type() == QEvent.Type.Resize
                and self.hover_status_label.isVisible()):
            self._reposition_hover_status_label()
        return super().eventFilter(watched, event)

    def _set_browser_html(self, html_content: str):
        if hasattr(self, 'hover_status_label'):
            self.hover_status_label.hide()
            self.hover_status_label.clear()
        self.content_browser.document().setDefaultStyleSheet(
            'a { text-decoration: none; }')
        self.content_browser.setHtml(html_content)
        doc = self.content_browser.document()
        fmt = doc.rootFrame().frameFormat()
        fmt.setLeftMargin(16)
        fmt.setRightMargin(16)
        fmt.setTopMargin(2)
        fmt.setBottomMargin(16)
        doc.rootFrame().setFrameFormat(fmt)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Activate the button the user has tabbed to. The nav/footer buttons
            # are deliberately not "default" buttons (so Enter in the search box
            # runs a search rather than clicking a button), which otherwise
            # leaves a focused button unresponsive to Enter.
            focus_widget = self.focusWidget()
            if isinstance(focus_widget, QPushButton) and focus_widget.isEnabled():
                focus_widget.click()
                event.accept()
                return
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            completer_popup = self.search_completer.popup()
            if completer_popup is not None and completer_popup.isVisible():
                super().keyPressEvent(event)
                return
            scroll_bar = self.content_browser.verticalScrollBar()
            step = max(scroll_bar.singleStep(), 20)
            if event.key() == Qt.Key.Key_Up:
                scroll_bar.setValue(scroll_bar.value() - step)
            else:
                scroll_bar.setValue(scroll_bar.value() + step)
            event.accept()
            return
        super().keyPressEvent(event)

    def current_tag(self) -> str:
        if 0 <= self.history_index < len(self.tag_history):
            return self.tag_history[self.history_index]
        return ''

    def extract_tag_name_from_search_text(self, search_text: str) -> str:
        normalized_text = str(search_text).strip()
        if not normalized_text:
            return ''
        mapped_tag = self.autocomplete_display_to_tag.get(normalized_text)
        if mapped_tag:
            return mapped_tag
        return AUTOCOMPLETE_COUNT_SUFFIX_PATTERN.sub('', normalized_text).strip()

    def format_post_count(self, post_count: int) -> str:
        if post_count >= 1_000_000:
            return f'{post_count / 1_000_000:.1f}M'
        if post_count >= 1_000:
            return f'{post_count // 1_000}k'
        return str(post_count)

    def update_navigation_buttons(self):
        self.back_button.setEnabled(self.history_index > 0)
        self.forward_button.setEnabled(
            0 <= self.history_index < len(self.tag_history) - 1)

    def get_disabled_button_stylesheet(self) -> str:
        window_color = self.palette().color(self.backgroundRole())
        is_dark_mode = window_color.lightness() < 128
        if is_dark_mode:
            return 'QPushButton { background-color: #3a3a3a; color: #666; }'
        return 'QPushButton { background-color: #d0d0d0; color: #888; }'

    def current_library_tag(self) -> str:
        """The current tag in the form used for the Tag Library and images:
        spaces instead of underscores and always lowercase."""
        normalized_tag = self.current_tag()
        if not normalized_tag:
            return ''
        return normalized_tag.replace('_', ' ').lower()

    def update_add_to_library_button_state(self):
        disabled_style = self.get_disabled_button_stylesheet()
        display_tag = self.current_library_tag()
        has_tag = (self.tag_library_model is not None and bool(display_tag)
                   and self.tag_library_model.has_tag(display_tag))

        # "Add to Selected Images" needs a tag to add and a callback that can
        # reach the Images pane. When every currently selected image already
        # has the tag, disable the button and relabel it -- mirroring the
        # "Already in Tag Library" treatment of the library button. The wiki
        # dialog is modal, so the image selection cannot change while it is
        # open, and this state stays accurate until the button is used.
        self.add_to_images_button.setText('Add to Selected Images')
        can_add_to_images = (self.add_to_selected_images_callback is not None
                             and bool(display_tag))
        if (can_add_to_images
                and self.selected_images_have_tag_callback is not None):
            # None -> no images selected (keep enabled so the dialog can still
            # guide the user); True -> all selected images already have the
            # tag, so there is nothing to add.
            if self.selected_images_have_tag_callback(display_tag) is True:
                can_add_to_images = False
                self.add_to_images_button.setText('Already in Selected Images')
        self.add_to_images_button.setEnabled(can_add_to_images)
        self.add_to_images_button.setStyleSheet(
            '' if can_add_to_images else disabled_style)

        # Mirror the "Add to Selected Images" logic for the grouped-view
        # "Add to Current Image" button: enabled when there is a tag and a
        # callback, disabled and relabelled when the current image already has
        # the tag.
        self.add_to_current_image_button.setText('Add to Current Image')
        can_add_to_current = (self.add_to_current_image_callback is not None
                              and bool(display_tag))
        if (can_add_to_current
                and self.current_image_has_tag_callback is not None):
            # None -> no current image (keep enabled); True -> the current
            # image already has the tag, so there is nothing to add.
            if self.current_image_has_tag_callback(display_tag) is True:
                can_add_to_current = False
                self.add_to_current_image_button.setText(
                    'Already in Current Image')
        self.add_to_current_image_button.setEnabled(can_add_to_current)
        self.add_to_current_image_button.setStyleSheet(
            '' if can_add_to_current else disabled_style)

        if self.tag_library_model is None or not display_tag:
            self.add_to_library_button.setEnabled(False)
            self.add_to_library_button.setText('Add to Tag Library')
            self.add_to_library_button.setStyleSheet(disabled_style)
            return
        if has_tag:
            self.add_to_library_button.setEnabled(False)
            self.add_to_library_button.setText('Already in Tag Library')
            self.add_to_library_button.setStyleSheet(disabled_style)
            return
        self.add_to_library_button.setEnabled(True)
        self.add_to_library_button.setText('Add to Tag Library')
        self.add_to_library_button.setStyleSheet('')

    @Slot()
    def add_current_tag_to_library(self):
        if self.tag_library_model is None:
            return
        display_tag = self.current_library_tag()
        if not display_tag:
            return
        if self.tag_library_model.has_tag(display_tag):
            self.update_add_to_library_button_state()
            return
        if self.add_to_library_callback is not None:
            # Let the host add the tag (lowercased) and show the settings-aware
            # category-assignment prompt, parented to this dialog.
            self.add_to_library_callback(display_tag, self)
        else:
            self.tag_library_model.add_tags([display_tag])
        self.update_add_to_library_button_state()

    @Slot()
    def add_current_tag_to_selected_images(self):
        if self.add_to_selected_images_callback is None:
            return
        display_tag = self.current_library_tag()
        if not display_tag:
            return
        self.add_to_selected_images_callback(display_tag, self)
        self.update_add_to_library_button_state()

    @Slot()
    def add_current_tag_to_current_image(self):
        if self.add_to_current_image_callback is None:
            return
        display_tag = self.current_library_tag()
        if not display_tag:
            return
        self.add_to_current_image_callback(display_tag, self)
        self.update_add_to_library_button_state()

    @Slot()
    def navigate_back(self):
        if self.history_index <= 0:
            return
        self.history_index -= 1
        self.load_tag(self.tag_history[self.history_index],
                      add_to_history=False)
        self.update_navigation_buttons()

    @Slot()
    def navigate_forward(self):
        if self.history_index >= len(self.tag_history) - 1:
            return
        self.history_index += 1
        self.load_tag(self.tag_history[self.history_index],
                      add_to_history=False)
        self.update_navigation_buttons()

    def set_search_text_silently(self, text: str):
        """Set the search box text without triggering autocomplete.

        Used for programmatic updates (navigation, resolved wiki titles)
        where the user is not typing and no suggestion dropdown is wanted.

        Because autocomplete searches are driven by ``textEdited`` (user
        typing) rather than ``textChanged``, a programmatic ``setText`` here
        never launches a search, so no signal juggling is required.
        """
        self.search_line_edit.setText(text)
        # If a pre-filled wiki open armed the one-shot selection flag, re-apply
        # the full-text selection here (the async page load lands via this
        # method and would otherwise drop the initial selection) and disarm it.
        if self._select_all_on_next_silent_set:
            self._select_all_on_next_silent_set = False
            self.search_line_edit.selectAll()
        self.current_autocomplete_query = ''
        self.search_autocomplete_timer.stop()
        popup = self.search_completer.popup()
        if popup is not None:
            popup.hide()

    def _reset_autocomplete_if_search_empty(self, text: str):
        """Clear autocomplete state when the search box becomes empty.

        Connected to ``textChanged`` so that clearing the field by any means
        (including the line edit's clear button, which updates the text
        programmatically and therefore does not emit ``textEdited``) tears
        down the suggestion list and hides the popup. Non-empty changes are
        ignored here; those are handled by ``on_search_text_changed`` when the
        user types.
        """
        if text.strip():
            return
        self.current_autocomplete_query = ''
        self.autocomplete_display_to_tag = {}
        self.search_autocomplete_model.clear()
        self.search_autocomplete_timer.stop()
        popup = self.search_completer.popup()
        if popup is not None:
            popup.hide()

    @Slot()
    def search_tag(self):
        popup = self.search_completer.popup()
        if popup is not None:
            popup.hide()
        self.stop_all_autocomplete_threads()
        search_text = self.extract_tag_name_from_search_text(
            self.search_line_edit.text())
        if not search_text:
            return
        self.search_line_edit.setText(search_text.replace('_', ' '))
        self.load_tag(search_text, add_to_history=True)

    def stop_all_fetch_threads(self):
        for fetch_thread in list(self.active_fetch_threads):
            try:
                if fetch_thread.isRunning():
                    fetch_thread.requestInterruption()
                    fetch_thread.quit()
                    fetch_thread.wait(12000)
            except RuntimeError:
                continue
        self.active_fetch_threads.clear()
        self.fetch_thread = None

    def stop_all_autocomplete_threads(self, wait: bool = False):
        """Stop the autocomplete worker threads.

        During normal use (``wait=False``) this only asks the threads to stop
        and lets them clean themselves up via
        ``handle_autocomplete_thread_finished`` so typing/searching stays
        responsive.

        When the dialog is closing (``wait=True``) we must actually block until
        every thread has finished running. Otherwise a thread can still be busy
        with its network request when the dialog object is garbage-collected,
        which destroys a running QThread and crashes the app with
        "QThread: Destroyed while thread '' is still running".
        """
        self.search_autocomplete_timer.stop()
        self.current_autocomplete_query = ''
        for autocomplete_thread in list(self.active_autocomplete_threads):
            try:
                if autocomplete_thread.isRunning():
                    autocomplete_thread.requestInterruption()
                    if wait:
                        autocomplete_thread.quit()
                        autocomplete_thread.wait(12000)
            except RuntimeError:
                continue
        # When waiting, every thread has now stopped running, so it is safe to
        # drop our references to them. When not waiting, do NOT clear
        # active_autocomplete_threads here — the threads clean themselves up via
        # handle_autocomplete_thread_finished, and premature clearing drops the
        # last Python reference while the C++ thread is still running → crash.
        if wait:
            self.active_autocomplete_threads.clear()
        self.autocomplete_thread = None

    @Slot()
    def handle_fetch_thread_finished(self):
        finished_thread = self.sender()
        if finished_thread is not None:
            finished_thread.deleteLater()
            if finished_thread in self.active_fetch_threads:
                self.active_fetch_threads.remove(finished_thread)
        if finished_thread is self.fetch_thread:
            self.fetch_thread = None

    @Slot()
    def handle_autocomplete_thread_finished(self):
        finished_thread = self.sender()
        if finished_thread is not None:
            finished_thread.deleteLater()
            if finished_thread in self.active_autocomplete_threads:
                self.active_autocomplete_threads.remove(finished_thread)
        if finished_thread is self.autocomplete_thread:
            self.autocomplete_thread = None

    @Slot()
    def run_autocomplete_search(self):
        query = self.current_autocomplete_query.strip()
        if len(query) < 1:
            self.autocomplete_display_to_tag = {}
            self.search_autocomplete_model.clear()
            return
        autocomplete_thread = self.AUTOCOMPLETE_THREAD_CLASS(query)
        self.autocomplete_thread = autocomplete_thread
        self.active_autocomplete_threads.append(autocomplete_thread)
        autocomplete_thread.suggestions_ready.connect(
            self.apply_autocomplete_suggestions)
        autocomplete_thread.finished.connect(
            self.handle_autocomplete_thread_finished)
        autocomplete_thread.start()

    @Slot(str, list)
    def apply_autocomplete_suggestions(self, query: str, suggestions: list):
        if query.strip() != self.current_autocomplete_query.strip():
            return
        self.autocomplete_display_to_tag = {}
        self.search_autocomplete_model.clear()
        for suggestion in suggestions:
            tag_name = str(suggestion.get('name') or '').strip()
            if not tag_name:
                continue
            is_tag_group = bool(suggestion.get('is_tag_group'))
            is_wiki_only = bool(suggestion.get('is_wiki_only'))
            tag_count = int(suggestion.get('post_count') or 0)
            human_readable_tag = tag_name.replace('_', ' ')
            if (is_tag_group
                    and human_readable_tag.casefold().startswith('tag group:')):
                display_text = f'{human_readable_tag} (Tag Group)'
            elif is_wiki_only:
                display_text = f'{human_readable_tag} (Wiki)'
            else:
                display_text = (f'{human_readable_tag} '
                                f'({self.format_post_count(tag_count)})')
            self.autocomplete_display_to_tag[display_text] = tag_name
            self.search_autocomplete_model.appendRow(QStandardItem(display_text))
        self.search_completer.display_to_tag = self.autocomplete_display_to_tag
        if suggestions and self.search_line_edit.hasFocus():
            self.search_completer.complete()

    @Slot(str)
    def apply_autocomplete_selection(self, selected_text: str):
        if not selected_text:
            return
        selected_tag = self.extract_tag_name_from_search_text(selected_text)
        if not selected_tag:
            return
        self.load_tag(selected_tag, add_to_history=True)

    @Slot(str, str)
    def show_not_found(self, request_key: str, normalized_tag: str):
        if request_key != self.current_request_key:
            return
        self.pending_history_append_on_load = False
        self.show_status_message('No wiki page found for this tag.')
        self._set_browser_html(
            f'<h3>{html.escape(normalized_tag)}</h3>'
            f'<p>No wiki entry was found on {self.SITE_NAME}.</p>')
        self.update_add_to_library_button_state()

    @Slot(str, str)
    def show_error(self, request_key: str, error_message: str):
        if request_key != self.current_request_key:
            return
        self.pending_history_append_on_load = False
        self.show_status_message('Failed to load wiki page.')
        self._set_browser_html(
            f'<p>Failed to load wiki information from {self.SITE_NAME}.</p>'
            f'<p>{html.escape(error_message)}</p>')
        self.update_add_to_library_button_state()
