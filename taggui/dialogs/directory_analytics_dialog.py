"""
"Directory Analytics" dialog (design: DIRECTORY_ANALYTICS_PLAN).

Scans the currently loaded folder and shows a read-only report of dataset
statistics across five tabs (Overview, Resolution, Captions & tags,
Housekeeping, Per-subfolder). The whole report can be exported to CSV or
Markdown. Nothing is ever deleted, moved, or modified.

The scan runs off the UI thread with a cancellable progress dialog, mirroring
the "Find Duplicates" tool, so large folders stay responsive.
"""

from html import escape
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout,
                               QLabel, QMessageBox, QProgressDialog,
                               QPushButton, QTabWidget, QTextBrowser,
                               QVBoxLayout)

from models.image_list_model import ImageListModel
from utils.directory_analytics import (AnalyticsReport, compute_analytics,
                                        format_size, report_to_csv,
                                        report_to_markdown)


class AnalyticsScanner(QObject, QRunnable):
    """Runs the (I/O bound) analytics computation off the UI thread."""

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
            should_cancel=lambda: self._cancelled)
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


def _summary_row(label: str, summary, unit: str = '',
                 decimals: int = 0) -> list[str]:
    def fmt(value: float) -> str:
        text = f'{value:.{decimals}f}'
        return f'{text}{unit}' if unit else text
    return [label, fmt(summary.minimum), fmt(summary.median),
            fmt(summary.maximum), fmt(summary.average)]


class DirectoryAnalyticsDialog(QDialog):
    def __init__(self, parent, image_list_model: ImageListModel,
                 directory_path):
        super().__init__(parent)
        self.image_list_model = image_list_model
        self.directory_path = directory_path
        self._scanner: AnalyticsScanner | None = None
        self._report: AnalyticsReport | None = None

        self.setWindowTitle('Directory Analytics')
        self.resize(760, 620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        description = QLabel(
            'A read-only overview of the currently loaded folder. Nothing is '
            'changed, moved, or deleted.')
        description.setWordWrap(True)
        layout.addWidget(description)

        self.tabs = QTabWidget()
        self._browsers: dict[str, QTextBrowser] = {}
        for name in ('Overview', 'Resolution', 'Captions & tags',
                     'Housekeeping', 'Per-subfolder'):
            browser = QTextBrowser()
            browser.setOpenExternalLinks(False)
            self._browsers[name] = browser
            # Escape '&' so Qt doesn't treat it as a keyboard-shortcut marker
            # (which would hide the '&' and underline the next letter).
            self.tabs.addTab(browser, name.replace('&', '&&'))
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
        # Run the first scan automatically once the dialog is shown.
        self.start_scan()

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    @Slot()
    def start_scan(self):
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

        progress = QProgressDialog('Analyzing the folder\u2026', 'Cancel', 0,
                                   len(images), self)
        progress.setWindowTitle('Directory Analytics')
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoReset(False)
        progress.setValue(0)

        scanner = AnalyticsScanner(
            images, self.directory_path,
            self.image_list_model.get_image_suffixes())
        self._scanner = scanner
        scanner.progress.connect(lambda done, total: progress.setValue(done))
        progress.canceled.connect(scanner.cancel)

        def on_finished(report):
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

        scanner.finished.connect(on_finished)
        QThreadPool.globalInstance().start(scanner)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _set_placeholder(self, text: str):
        for browser in self._browsers.values():
            browser.setHtml(f'<p style="color:gray;">{escape(text)}</p>')

    def _render_report(self, report: AnalyticsReport):
        self._browsers['Overview'].setHtml(self._overview_html(report))
        self._browsers['Resolution'].setHtml(self._resolution_html(report))
        self._browsers['Captions & tags'].setHtml(self._captions_html(report))
        self._browsers['Housekeeping'].setHtml(self._housekeeping_html(report))
        self._browsers['Per-subfolder'].setHtml(self._subfolders_html(report))

    def _overview_html(self, report: AnalyticsReport) -> str:
        parts = ['<h2>Overview</h2>']
        parts.append(
            '<ul>'
            f'<li>Total images: <b>{report.total_images}</b></li>'
            f'<li>Total size on disk: '
            f'<b>{escape(format_size(report.total_size_bytes))}</b></li>'
            f'<li>Subfolders containing images: '
            f'<b>{report.subfolder_count}</b> '
            f'(folders with images: {report.folders_with_images})</li>'
            '</ul>')
        parts.append('<h3>File formats</h3>')
        rows = [[item.suffix, item.count, f'{item.percent:.1f}%']
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
            _summary_row('Width (px)', report.width_summary),
            _summary_row('Height (px)', report.height_summary),
            _summary_row('Megapixels', report.megapixel_summary, decimals=2),
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
                            [[item.suffix, item.count, f'{item.percent:.1f}%']
                             for item in report.aspect_ratio_counts]))
        parts.append('<h3>Flags</h3>')
        parts.append(
            '<ul>'
            f'<li>Very small images: <b>{report.very_small_count}</b></li>'
            f'<li>Extreme aspect ratios: '
            f'<b>{report.extreme_aspect_count}</b></li>'
            '</ul>')
        parts.append(self._examples_html('Very small images',
                                         report.very_small_examples))
        parts.append(self._examples_html('Extreme aspect ratios',
                                         report.extreme_aspect_examples))
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
                            [_summary_row('Tags',
                                          report.tags_per_image_summary)]))
        parts.append('<h3>Prompt length (characters)</h3>')
        parts.append(_table(['Measure', 'Min', 'Median', 'Max', 'Average'],
                            [_summary_row('Length',
                                          report.prompt_length_summary)]))
        parts.append('<h3>Most common tags</h3>')
        parts.append(_table(['Tag', 'Count', '% of instances'],
                            [[item.suffix, item.count, f'{item.percent:.1f}%']
                             for item in report.most_common_tags]))
        parts.append(self._examples_html('Rare tags (used once)',
                                         report.rare_tag_examples))
        return ''.join(parts)

    def _housekeeping_html(self, report: AnalyticsReport) -> str:
        parts = ['<h2>Housekeeping</h2>']
        parts.append(
            '<ul>'
            f'<li>Orphaned caption files (no matching image): '
            f'<b>{report.orphan_caption_count}</b></li>'
            f'<li>Images with no caption file: '
            f'<b>{report.missing_caption_count}</b></li>'
            f'<li>Other (non-image) files: '
            f'<b>{report.non_image_file_count}</b></li>'
            '</ul>')
        parts.append(self._examples_html('Orphaned caption files',
                                         report.orphan_caption_examples))
        parts.append(self._examples_html('Images with no caption file',
                                         report.missing_caption_examples))
        parts.append(self._examples_html('Non-image files',
                                         report.non_image_file_examples))
        return ''.join(parts)

    def _subfolders_html(self, report: AnalyticsReport) -> str:
        parts = ['<h2>Per-subfolder</h2>']
        rows = [[sub.name, sub.image_count, format_size(sub.total_size_bytes),
                 f'{sub.average_megapixels:.2f}',
                 f'{sub.caption_coverage_percent:.1f}%',
                 f'{sub.completion_percent:.1f}%']
                for sub in report.subfolders]
        parts.append(_table(
            ['Folder', 'Images', 'Size', 'Avg MP', 'Caption %', 'Complete %'],
            rows))
        return ''.join(parts)

    def _examples_html(self, title: str, examples: list[str]) -> str:
        if not examples:
            return ''
        items = ''.join(f'<li><code>{escape(str(e))}</code></li>'
                        for e in examples)
        return (f'<details><summary>{escape(title)} '
                f'(showing {len(examples)})</summary><ul>{items}</ul>'
                f'</details>')

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
        self._export(report_to_csv, 'CSV files (*.csv)', 'csv')

    def _export_markdown(self):
        self._export(report_to_markdown, 'Markdown files (*.md)', 'md')

    def _export(self, serializer, file_filter: str, extension: str):
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
            Path(file_path).write_text(serializer(self._report),
                                       encoding='utf-8')
        except OSError as error:
            QMessageBox.warning(self, 'Export failed',
                                f'Could not write the file:\n{error}')
            return
        self.status_label.setText(f'Exported to {file_path}')
