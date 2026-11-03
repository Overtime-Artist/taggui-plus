"""
"Directory Analytics" dialog (design: DIRECTORY_ANALYTICS_PLAN).

Scans the currently loaded folder and shows a read-only report of dataset
statistics across five tabs (Overview, Resolution, Captions & tags,
Housekeeping, Per-subfolder). The whole report can be exported to CSV or
Markdown. Nothing is ever deleted, moved, or modified.

The dialog is modeless so it can stay open while working in the main window:
clicking a listed image selects it in the Images pane, and clicking a listed
non-image file shows it in the system file manager.

The scan runs off the UI thread with a cancellable progress dialog, mirroring
the "Find Duplicates" tool, so large folders stay responsive.
"""

import subprocess
import sys
from html import escape
from pathlib import Path

from PySide6.QtCore import (QObject, QRunnable, Qt, QThreadPool, QTimer, QUrl,
                            Signal, Slot)
from PySide6.QtGui import QDesktopServices, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QFileDialog,
                               QHBoxLayout, QHeaderView, QLabel, QMessageBox,
                               QProgressDialog, QPushButton, QTabWidget,
                               QTextBrowser, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

from models.image_list_model import ImageListModel
from utils.directory_analytics import (_EXTREME_ASPECT_RATIO,
                                        _VERY_SMALL_MAX_EDGE, AnalyticsReport,
                                        compute_analytics, format_megapixels,
                                        format_size, relative_path,
                                        report_to_csv, report_to_markdown,
                                        subfolder_summary, summary_row)

# Results of the main window's "select this image" callback.
SELECTED = 'selected'
HIDDEN_BY_FILTER = 'hidden'
NOT_LOADED = 'missing'

# Kinds of clickable list entries.
_IMAGE_LINK = 'image'
_FILE_LINK = 'file'

_SORT_ROLE = Qt.ItemDataRole.UserRole


def reveal_in_file_manager(path) -> bool:
    """Show ``path`` in the system file manager (selected where supported).
    Returns False if the file no longer exists."""
    path = Path(path)
    if not path.exists():
        return False
    if sys.platform == 'win32':
        subprocess.Popen(f'explorer /select,"{path}"')
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', '-R', str(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
    return True


class AnalyticsScanner(QObject, QRunnable):
    """Runs the (I/O bound) analytics computation off the UI thread."""

    walking = Signal(int)
    progress = Signal(int, int)
    finished = Signal(object)

    def __init__(self, images, directory_path, image_suffixes):
        QObject.__init__(self)
        QRunnable.__init__(self)
        self.images = images
        self.directory_path = directory_path
        self.image_suffixes = image_suffixes
        self._cancelled = False
        self.setAutoDelete(False)

    def cancel(self):
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @Slot()
    def run(self):
        report = compute_analytics(
            self.images, self.directory_path, self.image_suffixes,
            progress_callback=lambda done, total: self.progress.emit(done,
                                                                     total),
            should_cancel=lambda: self._cancelled,
            walk_callback=lambda found: self.walking.emit(found))
        self.finished.emit(report)


def _table(headers: list[str], rows: list[list[str]],
           right_align_from: int = 1) -> str:
    """Return a small HTML table for the given headers and rows."""
    head_cells = ''.join(f'<th style="text-align:left; padding:2px 10px;">'
                         f'{escape(str(h))}</th>' for h in headers)
    body = []
    for row in rows:
        cells = []
        for column, value in enumerate(row):
            align = 'right' if column >= right_align_from else 'left'
            cells.append(f'<td style="text-align:{align}; padding:2px 10px;">'
                         f'{escape(str(value))}</td>')
        body.append('<tr>' + ''.join(cells) + '</tr>')
    return (f'<table cellspacing="0" style="border-collapse:collapse;">'
            f'<tr>{head_cells}</tr>{"".join(body)}</table>')


class _SubfolderItem(QTreeWidgetItem):
    """Tree row that sorts by the value stored in ``_SORT_ROLE`` (numbers
    sort numerically; rows without a value sort first)."""

    def __lt__(self, other):
        tree = self.treeWidget()
        column = tree.sortColumn() if tree is not None else 0
        mine = self.data(column, _SORT_ROLE)
        theirs = other.data(column, _SORT_ROLE)
        if mine is None or theirs is None:
            return mine is None and theirs is not None
        return mine < theirs


class DirectoryAnalyticsDialog(QDialog):
    _SUBFOLDER_HEADERS = ['Folder', 'Images', 'Size', 'Avg MP', 'Caption %',
                          'Complete %']

    def __init__(self, parent, image_list_model: ImageListModel,
                 directory_path, select_image_callback=None):
        super().__init__(parent)
        self.image_list_model = image_list_model
        self.directory_path = directory_path
        # Called with an image path; returns SELECTED, HIDDEN_BY_FILTER or
        # NOT_LOADED.
        self.select_image_callback = select_image_callback
        self._scanner: AnalyticsScanner | None = None
        self._report: AnalyticsReport | None = None
        self._closed = False
        # (kind, path) for each clickable list entry; links are "item:<index>".
        self._link_targets: list[tuple[str, str]] = []

        self.setWindowTitle('Directory Analytics')
        self.resize(960, 640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        description = QLabel(
            'A read-only overview of the currently loaded folder. Nothing is '
            'changed, moved, or deleted. Click a listed image to select it in '
            'the Images pane, or a listed file to show it in your file '
            'manager.')
        description.setWordWrap(True)
        layout.addWidget(description)

        self.tabs = QTabWidget()
        self._browsers: dict[str, QTextBrowser] = {}
        for name in ('Overview', 'Resolution', 'Captions & tags',
                     'Housekeeping'):
            browser = QTextBrowser()
            # Links are handled here (select / reveal), never navigated to.
            browser.setOpenLinks(False)
            browser.anchorClicked.connect(self._on_link_clicked)
            self._browsers[name] = browser
            # Escape '&' so Qt doesn't treat it as a keyboard-shortcut marker
            # (which would hide the '&' and underline the next letter).
            self.tabs.addTab(browser, name.replace('&', '&&'))

        self.subfolder_tree = QTreeWidget()
        self.subfolder_tree.setColumnCount(len(self._SUBFOLDER_HEADERS))
        self.subfolder_tree.setHeaderLabels(self._SUBFOLDER_HEADERS)
        self.subfolder_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        self.subfolder_tree.setUniformRowHeights(True)
        header = self.subfolder_tree.header()
        header.setStretchLastSection(False)
        # Room between the right-aligned numbers of neighbouring columns.
        header.setMinimumSectionSize(90)
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        for column in range(1, len(self._SUBFOLDER_HEADERS)):
            self.subfolder_tree.headerItem().setTextAlignment(
                column, Qt.AlignmentFlag.AlignRight
                | Qt.AlignmentFlag.AlignVCenter)
        self.subfolder_tree.setSortingEnabled(True)
        self.subfolder_tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.tabs.addTab(self.subfolder_tree, 'Per-subfolder')
        layout.addWidget(self.tabs, stretch=1)

        button_row = QHBoxLayout()
        self.status_label = QLabel('')
        self.status_label.setWordWrap(True)
        button_row.addWidget(self.status_label, stretch=1)

        self.rescan_button = QPushButton('Rescan')
        self.rescan_button.clicked.connect(self.start_scan)
        button_row.addWidget(self.rescan_button)

        self.export_csv_button = QPushButton('Export CSV\u2026')
        self.export_csv_button.clicked.connect(self._export_csv)
        self.export_csv_button.setEnabled(False)
        button_row.addWidget(self.export_csv_button)

        self.export_markdown_button = QPushButton('Export Markdown\u2026')
        self.export_markdown_button.clicked.connect(self._export_markdown)
        self.export_markdown_button.setEnabled(False)
        button_row.addWidget(self.export_markdown_button)

        close_button = QPushButton('Close')
        close_button.clicked.connect(self.reject)
        button_row.addWidget(close_button)

        layout.addLayout(button_row)

        self._set_placeholder('Scanning\u2026')
        # Run the first scan once the dialog is on screen, so the progress
        # window appears over it rather than on its own beforehand.
        QTimer.singleShot(0, self.start_scan)

    def done(self, result):
        # Stop a running scan; its result is ignored once the dialog closes.
        self._closed = True
        if self._scanner is not None:
            self._scanner.cancel()
        super().done(result)

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    @Slot()
    def start_scan(self):
        if self._closed:
            return
        if self.directory_path is None or not Path(self.directory_path).exists():
            self.status_label.setText('No folder is loaded. Load a directory '
                                      'first, then reopen this tool.')
            self._set_placeholder('No folder loaded.')
            return
        images = list(self.image_list_model.images)
        if not images:
            self.status_label.setText('No images are loaded in this folder.')
            self._set_placeholder('No images loaded.')
            return

        self.rescan_button.setEnabled(False)
        self.export_csv_button.setEnabled(False)
        self.export_markdown_button.setEnabled(False)
        self.status_label.setText('Scanning\u2026')

        # Starts as a busy indicator while the folder is listed (the number of
        # files isn't known yet), then counts the images analysed.
        progress = QProgressDialog('Listing files\u2026', 'Cancel', 0, 0, self)
        progress.setWindowTitle('Directory Analytics')
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoReset(False)
        progress.setValue(0)

        scanner = AnalyticsScanner(
            images, self.directory_path,
            self.image_list_model.get_image_suffixes())
        self._scanner = scanner

        def on_walking(found):
            if not self._closed:
                progress.setLabelText(f'Listing files\u2026 ({found} found)')

        def on_progress(done, total):
            if self._closed:
                return
            if progress.maximum() != total:
                progress.setLabelText('Analyzing images\u2026')
                progress.setRange(0, total)
            progress.setValue(done)

        def on_finished(report):
            if self._closed:
                return
            was_cancelled = scanner.cancelled
            self._scanner = None
            progress.reset()
            self.rescan_button.setEnabled(True)
            if was_cancelled or report is None:
                self.status_label.setText('Scan cancelled.')
                self._set_placeholder('Scan cancelled.')
                return
            self._report = report
            self._render_report(report)
            self.export_csv_button.setEnabled(True)
            self.export_markdown_button.setEnabled(True)
            self.status_label.setText(
                f'Scanned {report.total_images} images '
                f'({format_size(report.total_size_bytes)}).')

        scanner.walking.connect(on_walking)
        scanner.progress.connect(on_progress)
        progress.canceled.connect(scanner.cancel)
        scanner.finished.connect(on_finished)
        QThreadPool.globalInstance().start(scanner)

    # ------------------------------------------------------------------
    # Clickable lists
    # ------------------------------------------------------------------

    @Slot(QUrl)
    def _on_link_clicked(self, url: QUrl):
        text = url.toString()
        if not text.startswith('item:'):
            return
        try:
            kind, path = self._link_targets[int(text[len('item:'):])]
        except (ValueError, IndexError):
            return
        name = Path(path).name
        if kind == _IMAGE_LINK:
            if self.select_image_callback is None:
                return
            result = self.select_image_callback(path)
            if result == SELECTED:
                self.status_label.setText(
                    f'Selected {name} in the Images pane.')
            elif result == HIDDEN_BY_FILTER:
                self.status_label.setText(
                    f'{name} is hidden by the Images pane filter. Clear the '
                    f'filter to select it.')
            else:
                self.status_label.setText(
                    f'{name} is no longer loaded (it may have been moved or '
                    f'deleted). Rescan to refresh the report.')
        else:
            if reveal_in_file_manager(path):
                self.status_label.setText(
                    f'Showing {name} in the file manager.')
            else:
                self.status_label.setText(
                    f'{name} no longer exists. Rescan to refresh the report.')

    def _paths_html(self, title: str, paths: list[str], kind: str) -> str:
        """A heading plus the full list of ``paths`` (relative to the loaded
        folder), each a link handled by ``_on_link_clicked``."""
        if not paths:
            return ''
        items = []
        for path in paths:
            index = len(self._link_targets)
            self._link_targets.append((kind, path))
            items.append(f'<li><a href="item:{index}">'
                         f'{escape(relative_path(self._report, path))}'
                         f'</a></li>')
        return (f'<h3>{escape(title)} ({len(paths)})</h3>'
                f'<ul>{"".join(items)}</ul>')

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _set_placeholder(self, text: str):
        self._link_targets = []
        for browser in self._browsers.values():
            browser.setHtml(f'<p style="color:gray;">{escape(text)}</p>')
        self.subfolder_tree.clear()

    def _render_report(self, report: AnalyticsReport):
        self._link_targets = []
        self._browsers['Overview'].setHtml(self._overview_html(report))
        self._browsers['Resolution'].setHtml(self._resolution_html(report))
        self._browsers['Captions & tags'].setHtml(self._captions_html(report))
        self._browsers['Housekeeping'].setHtml(self._housekeeping_html(report))
        self._populate_subfolder_tree(report)

    def _overview_html(self, report: AnalyticsReport) -> str:
        parts = ['<h2>Overview</h2>']
        parts.append(
            '<ul>'
            f'<li>Total images: <b>{report.total_images}</b></li>'
            f'<li>Total size on disk: '
            f'<b>{escape(format_size(report.total_size_bytes))}</b></li>'
            f'<li>Subfolders with images: '
            f'<b>{escape(subfolder_summary(report))}</b></li>'
            '</ul>')
        parts.append('<h3>File formats</h3>')
        rows = [[item.label, item.count, f'{item.percent:.1f}%']
                for item in report.format_breakdown]
        parts.append(_table(['Format', 'Count', '%'], rows))
        return ''.join(parts)

    def _resolution_html(self, report: AnalyticsReport) -> str:
        parts = ['<h2>Resolution &amp; aspect ratio</h2>']
        parts.append(
            '<ul>'
            f'<li>Images with readable dimensions: '
            f'<b>{report.images_with_dimensions}</b></li>'
            f'<li>Images with unreadable dimensions: '
            f'<b>{report.images_without_dimensions}</b></li>'
            '</ul>')
        parts.append('<h3>Size buckets (longer edge, px)</h3>')
        parts.append(_table(['Bucket', 'Count'],
                            [[label, count] for label, count
                             in report.resolution_buckets.items()]))
        parts.append('<h3>Dimensions</h3>')
        summary_rows = [
            summary_row('Width (px)', report.width_summary),
            summary_row('Height (px)', report.height_summary),
            summary_row('Megapixels', report.megapixel_summary, decimals=2),
        ]
        parts.append(_table(['Measure', 'Min', 'Median', 'Max', 'Average'],
                            summary_rows))
        parts.append('<h3>Orientation</h3>')
        parts.append(_table(['Orientation', 'Count'], [
            ['Landscape', report.orientation_counts.get('landscape', 0)],
            ['Portrait', report.orientation_counts.get('portrait', 0)],
            ['Square', report.orientation_counts.get('square', 0)],
        ]))
        parts.append('<h3>Aspect ratios</h3>')
        parts.append(_table(['Ratio', 'Count', '%'],
                            [[item.label, item.count, f'{item.percent:.1f}%']
                             for item in report.aspect_ratio_counts]))
        parts.append('<h3>Flags</h3>')
        parts.append(
            '<ul>'
            f'<li>Very small images (longer edge under '
            f'{_VERY_SMALL_MAX_EDGE}px): <b>{report.very_small_count}</b></li>'
            f'<li>Extreme aspect ratios ({_EXTREME_ASPECT_RATIO:.0f}:1 or '
            f'more): <b>{report.extreme_aspect_count}</b></li>'
            '</ul>')
        parts.append(self._paths_html('Very small images',
                                      report.very_small_paths, _IMAGE_LINK))
        parts.append(self._paths_html('Extreme aspect ratios',
                                      report.extreme_aspect_paths,
                                      _IMAGE_LINK))
        return ''.join(parts)

    def _captions_html(self, report: AnalyticsReport) -> str:
        parts = ['<h2>Captions &amp; tags</h2>']
        parts.append(
            '<ul>'
            f'<li>Images with a caption file: '
            f'<b>{report.images_with_caption}</b></li>'
            f'<li>Images missing a caption file: '
            f'<b>{report.images_without_caption}</b></li>'
            f'<li>Images with zero tags: '
            f'<b>{report.images_with_zero_tags}</b></li>'
            f'<li>Total unique tags: <b>{report.total_unique_tags}</b></li>'
            f'<li>Total tag instances: '
            f'<b>{report.total_tag_instances}</b></li>'
            f'<li>Images with a natural-language prompt: '
            f'<b>{report.images_with_prompt}</b></li>'
            f'<li>Completed images (is_complete): '
            f'<b>{report.complete_count}</b> '
            f'({report.completion_percent:.1f}%)</li>'
            f'<li>Rare tags (used once): '
            f'<b>{report.rare_tag_count}</b></li>'
            '</ul>')
        parts.append('<h3>Tags per image</h3>')
        parts.append(_table(['Measure', 'Min', 'Median', 'Max', 'Average'],
                            [summary_row('Tags',
                                         report.tags_per_image_summary)]))
        parts.append('<h3>Prompt length (characters)</h3>')
        parts.append(_table(['Measure', 'Min', 'Median', 'Max', 'Average'],
                            [summary_row('Length',
                                         report.prompt_length_summary)]))
        parts.append('<h3>Most common tags</h3>')
        parts.append(_table(['Tag', 'Images', '% of images'],
                            [[item.label, item.count, f'{item.percent:.1f}%']
                             for item in report.most_common_tags]))
        parts.append(self._paths_html('Images missing a caption file',
                                      report.missing_caption_paths,
                                      _IMAGE_LINK))
        if report.rare_tags:
            parts.append(f'<h3>Rare tags (used once) '
                         f'({len(report.rare_tags)})</h3>')
            parts.append(f'<p>{escape(", ".join(report.rare_tags))}</p>')
        return ''.join(parts)

    def _housekeeping_html(self, report: AnalyticsReport) -> str:
        parts = ['<h2>Housekeeping</h2>']
        parts.append(
            '<ul>'
            f'<li>Orphaned caption files (no matching image): '
            f'<b>{report.orphan_caption_count}</b></li>'
            f'<li>Other (non-image) files: '
            f'<b>{report.non_image_file_count}</b></li>'
            '</ul>')
        parts.append(self._paths_html('Orphaned caption files',
                                      report.orphan_caption_paths,
                                      _FILE_LINK))
        parts.append(self._paths_html('Non-image files',
                                      report.non_image_file_paths,
                                      _FILE_LINK))
        return ''.join(parts)

    def _populate_subfolder_tree(self, report: AnalyticsReport):
        """Show subfolders as a sortable tree under the main folder. Folders
        with no images of their own still get a (dimmed) row when a deeper
        folder has images, so every child sits under its real parent."""
        tree = self.subfolder_tree
        tree.setSortingEnabled(False)
        tree.clear()
        by_parts: dict[tuple, object] = {(): None}
        for sub in report.subfolders:
            by_parts[() if sub.name == '.' else Path(sub.name).parts] = sub
        for parts_key in list(by_parts):
            for length in range(1, len(parts_key)):
                by_parts.setdefault(parts_key[:length], None)

        dim_color = self.palette().color(QPalette.ColorRole.PlaceholderText)
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        items: dict[tuple, _SubfolderItem] = {}
        # Sorted, so every parent is created before its children.
        for name_parts in sorted(by_parts):
            sub = by_parts[name_parts]
            if name_parts:
                item = _SubfolderItem(items[name_parts[:-1]])
                name = name_parts[-1]
            else:
                item = _SubfolderItem(tree)
                root_name = (Path(report.root_directory).name
                             or report.root_directory or '.')
                name = f'{root_name} (main folder)'
            items[name_parts] = item
            item.setText(0, name)
            item.setData(0, _SORT_ROLE, name.casefold())
            if sub is None:
                values = [(None, '')] * (len(self._SUBFOLDER_HEADERS) - 1)
            else:
                values = [
                    (sub.image_count, str(sub.image_count)),
                    (sub.total_size_bytes, format_size(sub.total_size_bytes)),
                    (sub.average_megapixels,
                     format_megapixels(sub.average_megapixels)),
                    (sub.caption_coverage_percent,
                     f'{sub.caption_coverage_percent:.1f}%'),
                    (sub.completion_percent,
                     f'{sub.completion_percent:.1f}%'),
                ]
            for column, (sort_value, text) in enumerate(values, start=1):
                item.setText(column, text)
                item.setData(column, _SORT_ROLE, sort_value)
                item.setTextAlignment(column, right)
            if sub is None:
                for column in range(len(self._SUBFOLDER_HEADERS)):
                    item.setForeground(column, dim_color)
        tree.setSortingEnabled(True)
        tree.expandAll()

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _default_export_name(self, extension: str) -> str:
        base = 'directory_analytics'
        if self.directory_path is not None:
            name = Path(self.directory_path).name
            if name:
                base = f'{name}_analytics'
        return f'{base}.{extension}'

    def _export_csv(self):
        # utf-8-sig so Excel reads accented / non-Latin text correctly.
        self._export(report_to_csv, 'CSV files (*.csv)', 'csv',
                     encoding='utf-8-sig')

    def _export_markdown(self):
        self._export(report_to_markdown, 'Markdown files (*.md)', 'md')

    def _export(self, serializer, file_filter: str, extension: str,
                encoding: str = 'utf-8'):
        if self._report is None:
            return
        start_dir = (str(self.directory_path)
                     if self.directory_path is not None else '')
        suggested = str(Path(start_dir) / self._default_export_name(extension)
                        ) if start_dir else self._default_export_name(extension)
        file_path, _ = QFileDialog.getSaveFileName(
            self, 'Export Directory Analytics', suggested, file_filter)
        if not file_path:
            return
        try:
            # newline='' writes the text's own line endings unchanged (the CSV
            # writer already uses \r\n; translating again added blank rows).
            with open(file_path, 'w', encoding=encoding, newline='') as file:
                file.write(serializer(self._report))
        except OSError as error:
            QMessageBox.warning(self, 'Export failed',
                                f'Could not write the file:\n{error}')
            return
        self.status_label.setText(f'Exported to {file_path}')
