"""Viewing and downloading a post's original (full-size) file. Shared by the
Danbooru and Gelbooru wiki dialogs."""
import html
import re
import shutil
import tempfile
from base64 import b64decode, b64encode
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from PySide6.QtCore import (QBuffer, QByteArray, QIODevice, QStandardPaths,
                            QThread, Qt, Signal, Slot)
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtWidgets import QFileDialog, QStyle

from dialogs.booru_image_viewer import BooruImageViewerDialog
from utils.settings import get_settings

# ``taggui-original:action=view&id=<n>`` opens the original in the full-size
# viewer, ``action=download`` saves it to disk.
ORIGINAL_INTERNAL_LINK_PREFIX = 'taggui-original:'
UNVIEWABLE_EXTENSIONS = ('mp4', 'webm', 'mkv', 'avi', 'mov', 'zip', 'swf')


def original_file_extension(post: dict) -> str:
    extension = str(post.get('file_ext') or '').lower()
    if not extension:
        extension = Path(urlparse(str(post.get('file_url') or '')).path
                         ).suffix.lstrip('.').lower()
    return re.sub(r'[^a-z0-9]', '', extension)


def original_is_viewable(post: dict) -> bool:
    """Whether the original is a still image this app can display."""
    extension = original_file_extension(post)
    if not extension or extension in UNVIEWABLE_EXTENSIONS:
        return False
    formats = {bytes(name).decode().lower()
               for name in QImageReader.supportedImageFormats()}
    return extension in formats


def decode_data_url_image(data_url: str) -> QImage:
    image = QImage()
    if ',' in data_url:
        try:
            image.loadFromData(b64decode(data_url.split(',', 1)[1]))
        except ValueError:
            pass
    return image


class OriginalFetchThread(QThread):
    """Downloads a post's original file into ``destination`` with
    ``urlopen(url, timeout=...)``."""
    original_ready = Signal(str, str)
    original_failed = Signal(str, str)

    def __init__(self, post_id: str, file_url: str, destination: Path,
                 urlopen, site_name: str):
        super().__init__()
        self.post_id = str(post_id)
        self.file_url = file_url
        self.destination = destination
        self.urlopen = urlopen
        self.site_name = site_name

    def run(self):
        try:
            # Originals can be 100 MB, hence the long timeout.
            with self.urlopen(self.file_url, timeout=300) as response:
                status = getattr(response, 'status', 200)
                if status != 200:
                    raise OSError(
                        f'{self.site_name} returned status {status}.')
                file_bytes = response.read()
            if self.isInterruptionRequested():
                return
            if not file_bytes:
                raise OSError(f'{self.site_name} returned an empty file.')
            if file_bytes.lstrip()[:15].lower().startswith(
                    (b'<!doctype html', b'<html')):
                raise OSError(f'{self.site_name} sent a web page instead of '
                              f'the file.')
            self.destination.write_bytes(file_bytes)
        except Exception as exception:
            self.original_failed.emit(self.post_id,
                                      str(exception) or 'Download failed.')
            return
        self.original_ready.emit(self.post_id, str(self.destination))


class OriginalsMixin:
    """Adds "View full size" / "Download original" to a wiki dialog.

    The dialog provides ``SITE_NAME``, ``DOWNLOAD_DIRECTORY_SETTING``,
    ``content_browser``, ``show_status_message``, ``_torn_down`` and
    ``_stored_post`` (a dict with ``id``, ``file_url`` and, optionally,
    ``file_ext`` and ``_taggui_image_data_url``), and implements
    ``_open_original_url(url, timeout)`` and ``_original_dimensions(post)``.
    Call ``_init_originals_state()`` in ``__init__`` and
    ``_teardown_originals()`` when closing."""

    def _init_originals_state(self):
        # Originals downloaded this session ({post id: file path}) live in a
        # temporary folder removed when the dialog closes.
        self._originals_directory: Path | None = None
        self._original_paths: dict[str, Path] = {}
        # {post id: (thread, [(on_ready, on_failed), ...])} for downloads
        # still running, so a second request for the same post shares one.
        self._original_fetches: dict[str, tuple] = {}
        self._image_viewer = None

    def _teardown_originals(self, park_running_threads):
        park_running_threads([thread for thread, _callbacks
                              in self._original_fetches.values()])
        self._original_fetches.clear()
        if self._image_viewer is not None:
            self._image_viewer.close()
            self._image_viewer = None

    def _remove_originals_directory(self):
        if self._originals_directory is not None:
            shutil.rmtree(self._originals_directory, ignore_errors=True)
            self._originals_directory = None

    def _open_original_url(self, url: str, timeout: int):
        raise NotImplementedError

    def _original_dimensions(self, post: dict) -> tuple:
        return post.get('image_width') or 0, post.get('image_height') or 0

    def _ensure_originals_directory(self) -> Path:
        if self._originals_directory is None:
            self._originals_directory = Path(tempfile.mkdtemp(
                prefix=f'taggui-{self.SITE_NAME.lower()}-'))
        return self._originals_directory

    def _post_for_original_link(self, post_id: str):
        post = self._stored_post or {}
        if str(post.get('id')) != str(post_id) or not post.get('file_url'):
            return None
        return post

    def handle_original_link(self, url_text: str) -> bool:
        """Handle a ``taggui-original:`` link; False for any other link."""
        if not url_text.startswith(ORIGINAL_INTERNAL_LINK_PREFIX):
            return False
        params = parse_qs(url_text[len(ORIGINAL_INTERNAL_LINK_PREFIX):])
        post = self._post_for_original_link((params.get('id') or [''])[0])
        if post is not None:
            if (params.get('action') or [''])[0] == 'view':
                self.view_original(post)
            else:
                self.download_original(post)
        return True

    def _request_original(self, post: dict, on_ready, on_failed):
        """Call ``on_ready(post_id, path)`` once the post's original is on
        disk (straight away if it already is), or ``on_failed(post_id,
        message)``."""
        post_id = str(post.get('id'))
        path = self._original_paths.get(post_id)
        if path is not None and path.is_file():
            on_ready(post_id, str(path))
            return
        if post_id in self._original_fetches:
            self._original_fetches[post_id][1].append((on_ready, on_failed))
            return
        try:
            directory = self._ensure_originals_directory()
        except OSError as exception:
            on_failed(post_id, str(exception))
            return
        extension = original_file_extension(post) or 'bin'
        destination = directory / f'{post_id}.{extension}'
        thread = OriginalFetchThread(
            post_id, str(post.get('file_url')), destination,
            self._open_original_url, self.SITE_NAME)
        self._original_fetches[post_id] = (thread, [(on_ready, on_failed)])
        thread.original_ready.connect(self._on_original_ready)
        thread.original_failed.connect(self._on_original_failed)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    @Slot(str, str)
    def _on_original_ready(self, post_id: str, path_text: str):
        _thread, callbacks = self._original_fetches.pop(post_id, (None, []))
        if self._torn_down:
            return
        self._original_paths[post_id] = Path(path_text)
        self._prune_original_files()
        for on_ready, _on_failed in callbacks:
            on_ready(post_id, path_text)

    @Slot(str, str)
    def _on_original_failed(self, post_id: str, message: str):
        _thread, callbacks = self._original_fetches.pop(post_id, (None, []))
        if self._torn_down:
            return
        for _on_ready, on_failed in callbacks:
            on_failed(post_id, message)

    def _prune_original_files(self, keep: int = 3):
        """Originals can be huge, so only the most recent few stay on disk."""
        viewer_post_id = (str(self._image_viewer.post.get('id'))
                          if self._image_viewer is not None else None)
        for post_id in list(self._original_paths):
            if len(self._original_paths) <= keep:
                break
            if post_id == viewer_post_id:
                continue
            path = self._original_paths.pop(post_id)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_sample_file(self, post: dict) -> Path | None:
        """Save the sample image already shown on the post page, so the
        viewer can show it while the original downloads."""
        match = re.match(r'data:image/([a-z0-9.+-]+);base64,(.*)$',
                         str(post.get('_taggui_image_data_url') or ''),
                         re.IGNORECASE | re.DOTALL)
        if not match or self._originals_directory is None:
            return None
        extension = re.sub(r'[^a-z0-9]', '', match.group(1).lower())
        path = (self._originals_directory
                / f'sample_{post.get("id")}.{extension or "img"}')
        try:
            path.write_bytes(b64decode(match.group(2)))
        except (OSError, ValueError):
            return None
        return path

    def _viewer_post(self, post: dict) -> dict:
        width, height = self._original_dimensions(post)
        return dict(post, image_width=width, image_height=height)

    def view_original(self, post: dict):
        if self._image_viewer is None:
            viewer = BooruImageViewerDialog(self, self.SITE_NAME)
            viewer.download_requested.connect(
                lambda requested_post: self.download_original(
                    requested_post, self._image_viewer))
            viewer.finished.connect(
                lambda _result, closed=viewer:
                self._on_image_viewer_closed(closed))
            self._image_viewer = viewer
        viewer = self._image_viewer
        post_id = str(post.get('id'))
        cached_path = self._original_paths.get(post_id)
        if cached_path is not None and cached_path.is_file():
            viewer.show_post(self._viewer_post(post), None)
        else:
            try:
                self._ensure_originals_directory()
            except OSError:
                pass
            viewer.show_post(self._viewer_post(post),
                             self._write_sample_file(post))
        viewer.show()
        viewer.raise_()
        viewer.activateWindow()
        self._request_original(post, self._on_viewer_original_ready,
                               self._on_viewer_original_failed)

    def _on_viewer_original_ready(self, post_id: str, path_text: str):
        if self._image_viewer is not None:
            self._image_viewer.show_original(post_id, Path(path_text))

    def _on_viewer_original_failed(self, post_id: str, message: str):
        if self._image_viewer is not None:
            self._image_viewer.show_original_failed(post_id, message)

    def _on_image_viewer_closed(self, viewer):
        if viewer is self._image_viewer:
            self._image_viewer = None
        if viewer is not None:
            viewer.deleteLater()
        if not self._torn_down:
            self.activateWindow()
            self.content_browser.setFocus()

    def download_original(self, post: dict, dialog_parent=None):
        post_id = str(post.get('id'))
        if not post.get('file_url'):
            return
        settings = get_settings()
        directory = settings.value(self.DOWNLOAD_DIRECTORY_SETTING, '',
                                   type=str)
        if not directory or not Path(directory).is_dir():
            directory = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.DownloadLocation)
        extension = original_file_extension(post) or 'bin'
        suggested_path = str(Path(directory)
                             / f'{self.SITE_NAME.lower()}_{post_id}.{extension}')
        file_path, _ = QFileDialog.getSaveFileName(
            dialog_parent or self, 'Download Original Image', suggested_path,
            f'{extension.upper()} files (*.{extension});;All files (*)')
        if not file_path:
            return
        target = Path(file_path)
        settings.setValue(self.DOWNLOAD_DIRECTORY_SETTING, str(target.parent))
        self._set_download_status(
            post_id, f'Downloading the original of post #{post_id}\u2026')

        def save(ready_post_id: str, path_text: str):
            try:
                shutil.copyfile(path_text, target)
            except OSError as exception:
                self._set_download_status(
                    ready_post_id, f"Couldn't save {target.name}: {exception}")
                return
            self._set_download_status(ready_post_id, f'Saved {target}')

        def fail(failed_post_id: str, message: str):
            self._set_download_status(
                failed_post_id,
                f"Couldn't download post #{failed_post_id}: {message}")

        self._request_original(post, save, fail)

    def _set_download_status(self, post_id: str, message: str):
        self.show_status_message(message)
        viewer = self._image_viewer
        if (viewer is not None and str(viewer.post.get('id')) == post_id
                and viewer.showing_original):
            viewer.set_status(message)

    def _build_post_original_html(self, post: dict, is_dark: bool) -> str:
        if not post.get('file_url'):
            # Restricted posts don't reveal their file.
            return ''
        link_color = '#6baef6' if is_dark else '#0066cc'
        post_id = str(post.get('id'))
        links = []
        actions = ['download']
        if original_is_viewable(post):
            actions.insert(0, 'view')
        for action in actions:
            url = ORIGINAL_INTERNAL_LINK_PREFIX + urlencode(
                {'action': action, 'id': post_id})
            label = ('View full size' if action == 'view'
                     else 'Download original')
            links.append(f'<a href="{html.escape(url, quote=True)}" '
                         f'style="color: {link_color};">{label}</a>')
        return ('<p style="margin: 4px 0;">'
                + ' &nbsp;|&nbsp; '.join(links) + '</p>')

    def _fitted_post_image_html(self, post: dict) -> str:
        """The post page's ``<img>``: as wide as the downloaded image, but no
        wider than the page. QTextBrowser scales images without smoothing,
        which looks pixelated, so the image is pre-scaled smoothly to the
        exact on-screen pixel size (including display scaling)."""
        data_url = str(post.get('_taggui_image_data_url') or '')
        image = post.get('_taggui_image')
        if not isinstance(image, QImage):
            image = decode_data_url_image(data_url)
            post['_taggui_image'] = image
        if image.isNull():
            return (f'<img src="{html.escape(data_url, quote=True)}" '
                    'style="max-width: 100%;">')
        browser = self.content_browser
        viewport_width = browser.viewport().width() - 32
        if not browser.verticalScrollBar().isVisible():
            # Post pages almost always scroll; leave room for the scroll bar
            # so it appearing doesn't add a horizontal one.
            viewport_width -= browser.style().pixelMetric(
                QStyle.PixelMetric.PM_ScrollBarExtent, None, browser)
        display_width = max(1, min(image.width(), viewport_width))
        pixel_width = max(1, round(
            display_width * browser.devicePixelRatioF()))
        if pixel_width != image.width():
            cached = post.get('_taggui_scaled_image')
            if cached and cached[0] == pixel_width:
                data_url = cached[1]
            else:
                scaled = image.scaledToWidth(
                    pixel_width, Qt.TransformationMode.SmoothTransformation)
                image_format = 'PNG' if image.hasAlphaChannel() else 'JPEG'
                buffer_bytes = QByteArray()
                buffer = QBuffer(buffer_bytes)
                buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                scaled.save(buffer, image_format,
                            -1 if image_format == 'PNG' else 92)
                buffer.close()
                data_url = (f'data:image/{image_format.lower()};base64,'
                            + b64encode(bytes(buffer_bytes)).decode('ascii'))
                post['_taggui_scaled_image'] = (pixel_width, data_url)
        return (f'<img src="{html.escape(data_url, quote=True)}" '
                f'width="{display_width}">')

    def _link_image_to_original(self, post: dict, image_html: str) -> str:
        """Make the post page's image open the full-size viewer."""
        if not (post.get('file_url') and original_is_viewable(post)):
            return image_html
        view_url = ORIGINAL_INTERNAL_LINK_PREFIX + urlencode(
            {'action': 'view', 'id': str(post.get('id'))})
        return (f'<a href="{html.escape(view_url, quote=True)}">'
                f'{image_html}</a>')
