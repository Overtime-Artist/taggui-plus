"""
Directory analytics for the loaded image folder (design:
DIRECTORY_ANALYTICS_PLAN).

This module is intentionally **UI-free** and **read-only**. Given the images the
app already scanned (each an :class:`~utils.image.Image` exposing ``path``,
``dimensions``, ``tags``, ``natural_language_prompt``,
``caption_file_modified_time_ns`` and ``is_complete``), the scan root directory,
and the list of recognised image suffixes, :func:`compute_analytics` returns an
:class:`AnalyticsReport` describing the dataset. The dialog layer renders it and
can export it to CSV / Markdown via :func:`report_to_csv` / :func:`report_to_markdown`.

The only new filesystem work is a single ``os.walk`` of the folder to read each
file's on-disk size and to discover caption ``.txt`` files and non-image files.
Nothing is decoded, moved, deleted, or modified.
"""

from __future__ import annotations

import os
import statistics
from dataclasses import dataclass, field
from pathlib import Path

# --- Classification thresholds (documented in DIRECTORY_ANALYTICS_PLAN) -------
# An image is flagged "very small" when its longer edge is below this many px.
_VERY_SMALL_MAX_EDGE = 256
# An image has an "extreme" aspect ratio when its long:short ratio is at least
# this value (e.g. 3.0 means 3:1 or wider/taller).
_EXTREME_ASPECT_RATIO = 3.0
# How close (relative) a ratio must be to 1:1 to count as square, and to a named
# ratio to be labelled with that name.
_SQUARE_TOLERANCE = 0.02
_RATIO_TOLERANCE = 0.03
# Longer-edge boundaries for the resolution buckets.
_BUCKET_EDGES = (512, 1024, 2048)
# How many "most common" tags to keep in the report.
_TOP_TAGS = 25
# Named aspect ratios, expressed as long-side / short-side (>= 1.0). Orientation
# (portrait vs landscape) is tracked separately.
_NAMED_RATIOS = (
    ('1:1', 1.0),
    ('5:4', 1.25),
    ('4:3', 4 / 3),
    ('7:5', 1.4),
    ('3:2', 1.5),
    ('16:10', 1.6),
    ('5:3', 5 / 3),
    ('16:9', 16 / 9),
    ('2:1', 2.0),
    ('21:9', 21 / 9),
    ('3:1', 3.0),
)


# --- Report data structures ---------------------------------------------------

@dataclass
class FormatCount:
    suffix: str
    count: int
    percent: float


@dataclass
class NumberSummary:
    """min / median / max / average for a set of numbers (all 0 when empty)."""
    minimum: float = 0.0
    median: float = 0.0
    maximum: float = 0.0
    average: float = 0.0


@dataclass
class SubfolderStats:
    name: str  # Path relative to the scan root ('.' for the root itself).
    image_count: int
    total_size_bytes: int
    average_megapixels: float
    caption_coverage_percent: float
    completion_percent: float


@dataclass
class AnalyticsReport:
    root_directory: str = ''

    # Overview
    total_images: int = 0
    total_size_bytes: int = 0
    subfolder_count: int = 0
    folders_with_images: int = 0
    format_breakdown: list[FormatCount] = field(default_factory=list)

    # Resolution & aspect ratio
    images_with_dimensions: int = 0
    images_without_dimensions: int = 0
    resolution_buckets: dict[str, int] = field(default_factory=dict)
    width_summary: NumberSummary = field(default_factory=NumberSummary)
    height_summary: NumberSummary = field(default_factory=NumberSummary)
    megapixel_summary: NumberSummary = field(default_factory=NumberSummary)
    orientation_counts: dict[str, int] = field(default_factory=dict)
    aspect_ratio_counts: list[FormatCount] = field(default_factory=list)
    very_small_count: int = 0
    very_small_examples: list[str] = field(default_factory=list)
    extreme_aspect_count: int = 0
    extreme_aspect_examples: list[str] = field(default_factory=list)

    # Captions & tags
    images_with_caption: int = 0
    images_without_caption: int = 0
    images_with_zero_tags: int = 0
    tags_per_image_summary: NumberSummary = field(default_factory=NumberSummary)
    total_unique_tags: int = 0
    total_tag_instances: int = 0
    most_common_tags: list[FormatCount] = field(default_factory=list)
    rare_tag_count: int = 0
    rare_tag_examples: list[str] = field(default_factory=list)
    images_with_prompt: int = 0
    prompt_length_summary: NumberSummary = field(default_factory=NumberSummary)
    complete_count: int = 0
    completion_percent: float = 0.0

    # Housekeeping
    orphan_caption_count: int = 0
    orphan_caption_examples: list[str] = field(default_factory=list)
    missing_caption_count: int = 0
    missing_caption_examples: list[str] = field(default_factory=list)
    non_image_file_count: int = 0
    non_image_file_examples: list[str] = field(default_factory=list)

    # Per-subfolder
    subfolders: list[SubfolderStats] = field(default_factory=list)


# --- Helpers ------------------------------------------------------------------

def _summarize(values: list[float]) -> NumberSummary:
    if not values:
        return NumberSummary()
    return NumberSummary(
        minimum=float(min(values)),
        median=float(statistics.median(values)),
        maximum=float(max(values)),
        average=float(statistics.fmean(values)),
    )


def _percent(part: int, whole: int) -> float:
    return (100.0 * part / whole) if whole else 0.0


def _bucket_label(longer_edge: int) -> str:
    low, mid, high = _BUCKET_EDGES
    if longer_edge < low:
        return f'<{low}'
    if longer_edge < mid:
        return f'{low}-{mid}'
    if longer_edge < high:
        return f'{mid}-{high}'
    return f'>{high}'


def _classify_aspect(width: int, height: int) -> tuple[str, str, float]:
    """Return ``(orientation, ratio_name, long_over_short)`` for a size."""
    if width <= 0 or height <= 0:
        return 'unknown', 'other', 0.0
    long_side = max(width, height)
    short_side = min(width, height)
    ratio = long_side / short_side
    if abs(ratio - 1.0) <= _SQUARE_TOLERANCE:
        return 'square', '1:1', ratio
    orientation = 'landscape' if width > height else 'portrait'
    ratio_name = 'other'
    best_diff = _RATIO_TOLERANCE
    for name, value in _NAMED_RATIOS:
        diff = abs(ratio - value) / value
        if diff <= best_diff:
            best_diff = diff
            ratio_name = name
    return orientation, ratio_name, ratio


def _format_counts(counter: dict[str, int], whole: int,
                   limit: int | None = None) -> list[FormatCount]:
    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    if limit is not None:
        items = items[:limit]
    return [FormatCount(name, count, _percent(count, whole))
            for name, count in items]


# --- Main computation ---------------------------------------------------------

def compute_analytics(images, root_directory, image_suffixes,
                      progress_callback=None, should_cancel=None
                      ) -> AnalyticsReport | None:
    """Build an :class:`AnalyticsReport` for ``images`` under ``root_directory``.

    ``images`` is any iterable of objects exposing the ``Image`` fields listed in
    the module docstring. ``image_suffixes`` is the list of recognised image
    extensions (lower-case, dot-prefixed, e.g. ``['.jpg', '.png']``).

    ``progress_callback(done, total)`` is called as images are processed and
    ``should_cancel()`` (if given) is polled so a long scan can be aborted; on
    cancellation ``None`` is returned.
    """
    image_list = list(images)
    total = len(image_list)
    root = Path(root_directory)
    suffix_set = {s.lower() for s in image_suffixes}

    report = AnalyticsReport(root_directory=str(root))
    report.total_images = total

    # --- One filesystem walk: file sizes + caption / non-image discovery. -----
    # ``sizes`` maps str(path) -> on-disk byte size for every file in the tree.
    sizes: dict[str, int] = {}
    caption_stems: set[str] = set()   # str(path without suffix) for .txt files
    non_image_files: list[str] = []
    if root.exists():
        for dirpath, _dirnames, filenames in os.walk(root):
            if should_cancel is not None and should_cancel():
                return None
            for filename in filenames:
                file_path = Path(dirpath) / filename
                try:
                    sizes[str(file_path)] = file_path.stat().st_size
                except OSError:
                    sizes[str(file_path)] = 0
                suffix = file_path.suffix.lower()
                if suffix == '.txt':
                    caption_stems.add(str(file_path.with_suffix('')))
                elif suffix not in suffix_set:
                    non_image_files.append(str(file_path))

    # --- Accumulators ---------------------------------------------------------
    format_counter: dict[str, int] = {}
    bucket_counter: dict[str, int] = {label: 0 for label in (
        f'<{_BUCKET_EDGES[0]}', f'{_BUCKET_EDGES[0]}-{_BUCKET_EDGES[1]}',
        f'{_BUCKET_EDGES[1]}-{_BUCKET_EDGES[2]}', f'>{_BUCKET_EDGES[2]}')}
    orientation_counter: dict[str, int] = {
        'landscape': 0, 'portrait': 0, 'square': 0}
    ratio_counter: dict[str, int] = {}
    widths: list[float] = []
    heights: list[float] = []
    megapixels: list[float] = []
    tag_counter: dict[str, int] = {}
    tags_per_image: list[float] = []
    prompt_lengths: list[float] = []
    image_stems: set[str] = set()

    # Per-subfolder accumulators keyed by relative folder name.
    sub_images: dict[str, int] = {}
    sub_size: dict[str, int] = {}
    sub_mp: dict[str, list[float]] = {}
    sub_caption: dict[str, int] = {}
    sub_complete: dict[str, int] = {}

    for done, image in enumerate(image_list):
        if should_cancel is not None and should_cancel():
            return None
        path = Path(image.path)
        image_stems.add(str(path.with_suffix('')))

        # File format.
        suffix = path.suffix.lower() or '(none)'
        format_counter[suffix] = format_counter.get(suffix, 0) + 1

        # Size on disk (from the walk; fall back to a direct stat).
        size = sizes.get(str(path))
        if size is None:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
        report.total_size_bytes += size

        # Subfolder key (parent relative to root).
        try:
            rel_parent = path.parent.relative_to(root)
            folder_name = str(rel_parent) if str(rel_parent) != '.' else '.'
        except ValueError:
            folder_name = str(path.parent)
        sub_images[folder_name] = sub_images.get(folder_name, 0) + 1
        sub_size[folder_name] = sub_size.get(folder_name, 0) + size
        sub_mp.setdefault(folder_name, [])
        sub_caption.setdefault(folder_name, 0)
        sub_complete.setdefault(folder_name, 0)

        # Dimensions / resolution.
        dimensions = getattr(image, 'dimensions', None)
        if dimensions and dimensions[0] and dimensions[1]:
            width, height = int(dimensions[0]), int(dimensions[1])
            report.images_with_dimensions += 1
            widths.append(width)
            heights.append(height)
            mp = (width * height) / 1_000_000.0
            megapixels.append(mp)
            sub_mp[folder_name].append(mp)
            longer_edge = max(width, height)
            bucket_counter[_bucket_label(longer_edge)] += 1
            orientation, ratio_name, ratio = _classify_aspect(width, height)
            if orientation in orientation_counter:
                orientation_counter[orientation] += 1
            ratio_counter[ratio_name] = ratio_counter.get(ratio_name, 0) + 1
            if longer_edge < _VERY_SMALL_MAX_EDGE:
                report.very_small_count += 1
                if len(report.very_small_examples) < 20:
                    report.very_small_examples.append(str(path))
            if ratio >= _EXTREME_ASPECT_RATIO:
                report.extreme_aspect_count += 1
                if len(report.extreme_aspect_examples) < 20:
                    report.extreme_aspect_examples.append(str(path))
        else:
            report.images_without_dimensions += 1

        # Captions & tags.
        has_caption = getattr(image, 'caption_file_modified_time_ns',
                              None) is not None
        if has_caption:
            report.images_with_caption += 1
            sub_caption[folder_name] += 1
        else:
            report.images_without_caption += 1
            if len(report.missing_caption_examples) < 20:
                report.missing_caption_examples.append(str(path))

        tags = list(getattr(image, 'tags', None) or [])
        tags_per_image.append(len(tags))
        if not tags:
            report.images_with_zero_tags += 1
        for tag in tags:
            tag_counter[tag] = tag_counter.get(tag, 0) + 1

        prompt = getattr(image, 'natural_language_prompt', '') or ''
        if prompt.strip():
            report.images_with_prompt += 1
            prompt_lengths.append(len(prompt))

        if getattr(image, 'is_complete', False):
            report.complete_count += 1
            sub_complete[folder_name] += 1

        if progress_callback is not None:
            progress_callback(done + 1, total)

    # --- Finalise overview ----------------------------------------------------
    report.format_breakdown = _format_counts(format_counter, total)
    # A "subfolder" is any folder that holds images other than the root itself.
    folders = set(sub_images)
    report.folders_with_images = len(folders)
    report.subfolder_count = len([f for f in folders if f != '.'])

    # --- Finalise resolution --------------------------------------------------
    report.resolution_buckets = bucket_counter
    report.width_summary = _summarize(widths)
    report.height_summary = _summarize(heights)
    report.megapixel_summary = _summarize(megapixels)
    report.orientation_counts = orientation_counter
    report.aspect_ratio_counts = _format_counts(
        ratio_counter, report.images_with_dimensions)

    # --- Finalise captions & tags ---------------------------------------------
    report.tags_per_image_summary = _summarize(tags_per_image)
    report.total_unique_tags = len(tag_counter)
    report.total_tag_instances = sum(tag_counter.values())
    report.most_common_tags = _format_counts(
        tag_counter, report.total_tag_instances, limit=_TOP_TAGS)
    rare_tags = sorted(tag for tag, count in tag_counter.items() if count == 1)
    report.rare_tag_count = len(rare_tags)
    report.rare_tag_examples = rare_tags[:20]
    report.prompt_length_summary = _summarize(prompt_lengths)
    report.completion_percent = _percent(report.complete_count, total)

    # --- Finalise housekeeping ------------------------------------------------
    orphan_stems = sorted(caption_stems - image_stems)
    report.orphan_caption_count = len(orphan_stems)
    report.orphan_caption_examples = [stem + '.txt'
                                      for stem in orphan_stems[:20]]
    report.missing_caption_count = report.images_without_caption
    non_image_files.sort()
    report.non_image_file_count = len(non_image_files)
    report.non_image_file_examples = non_image_files[:20]

    # --- Finalise per-subfolder table -----------------------------------------
    subfolders: list[SubfolderStats] = []
    for name in sorted(sub_images):
        count = sub_images[name]
        mp_values = sub_mp.get(name, [])
        avg_mp = statistics.fmean(mp_values) if mp_values else 0.0
        subfolders.append(SubfolderStats(
            name=name,
            image_count=count,
            total_size_bytes=sub_size.get(name, 0),
            average_megapixels=avg_mp,
            caption_coverage_percent=_percent(sub_caption.get(name, 0), count),
            completion_percent=_percent(sub_complete.get(name, 0), count),
        ))
    report.subfolders = subfolders

    return report


# --- Formatting helpers used by the report and exporters ----------------------

def format_size(num_bytes) -> str:
    """Return a short human-readable file size such as '1.2 MB'."""
    if num_bytes is None:
        return '?'
    size = float(num_bytes)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            if unit == 'B':
                return f'{int(size)} {unit}'
            return f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} TB'


def _summary_row(label: str, summary: NumberSummary, unit: str = '',
                 decimals: int = 0) -> list[str]:
    def fmt(value: float) -> str:
        text = f'{value:.{decimals}f}'
        return f'{text}{unit}' if unit else text
    return [label, fmt(summary.minimum), fmt(summary.median),
            fmt(summary.maximum), fmt(summary.average)]


# --- Exporters ----------------------------------------------------------------

def report_to_markdown(report: AnalyticsReport) -> str:
    """Render the report as a Markdown document."""
    lines: list[str] = []
    add = lines.append

    add(f'# Directory Analytics')
    add('')
    add(f'**Folder:** `{report.root_directory}`')
    add('')

    # Overview
    add('## Overview')
    add('')
    add(f'- Total images: **{report.total_images}**')
    add(f'- Total size on disk: **{format_size(report.total_size_bytes)}**')
    add(f'- Subfolders containing images: **{report.subfolder_count}** '
        f'(folders with images: {report.folders_with_images})')
    add('')
    add('| Format | Count | % |')
    add('| --- | ---: | ---: |')
    for item in report.format_breakdown:
        add(f'| {item.suffix} | {item.count} | {item.percent:.1f}% |')
    add('')

    # Resolution
    add('## Resolution & aspect ratio')
    add('')
    add(f'- Images with readable dimensions: **{report.images_with_dimensions}**'
        f' (unreadable: {report.images_without_dimensions})')
    add('')
    add('| Longer edge (px) | Count |')
    add('| --- | ---: |')
    for label, count in report.resolution_buckets.items():
        add(f'| {label} | {count} |')
    add('')
    add('| Measure | Min | Median | Max | Average |')
    add('| --- | ---: | ---: | ---: | ---: |')
    add('| ' + ' | '.join(_summary_row('Width (px)',
                                       report.width_summary)) + ' |')
    add('| ' + ' | '.join(_summary_row('Height (px)',
                                       report.height_summary)) + ' |')
    add('| ' + ' | '.join(_summary_row('Megapixels',
                                       report.megapixel_summary,
                                       decimals=2)) + ' |')
    add('')
    add(f'- Orientation: landscape {report.orientation_counts.get("landscape", 0)}'
        f', portrait {report.orientation_counts.get("portrait", 0)}'
        f', square {report.orientation_counts.get("square", 0)}')
    add('')
    add('| Aspect ratio | Count | % |')
    add('| --- | ---: | ---: |')
    for item in report.aspect_ratio_counts:
        add(f'| {item.suffix} | {item.count} | {item.percent:.1f}% |')
    add('')
    add(f'- Very small images (longer edge < {_VERY_SMALL_MAX_EDGE}px): '
        f'**{report.very_small_count}**')
    add(f'- Extreme aspect ratios (>= {_EXTREME_ASPECT_RATIO:.0f}:1): '
        f'**{report.extreme_aspect_count}**')
    add('')

    # Captions & tags
    add('## Captions & tags')
    add('')
    add(f'- Images with a caption file: **{report.images_with_caption}**')
    add(f'- Images missing a caption file: **{report.images_without_caption}**')
    add(f'- Images with zero tags: **{report.images_with_zero_tags}**')
    add(f'- Total unique tags: **{report.total_unique_tags}**')
    add(f'- Total tag instances: **{report.total_tag_instances}**')
    add('')
    add('| Tags per image | Min | Median | Max | Average |')
    add('| --- | ---: | ---: | ---: | ---: |')
    add('| ' + ' | '.join(_summary_row('Tags',
                                       report.tags_per_image_summary)) + ' |')
    add('')
    add(f'- Images with a natural-language prompt: '
        f'**{report.images_with_prompt}**')
    add('| Prompt length (chars) | Min | Median | Max | Average |')
    add('| --- | ---: | ---: | ---: | ---: |')
    add('| ' + ' | '.join(_summary_row('Length',
                                       report.prompt_length_summary)) + ' |')
    add('')
    add(f'- Completed images (is_complete): **{report.complete_count}** '
        f'({report.completion_percent:.1f}%)')
    add('')
    add(f'- Rare tags (used once): **{report.rare_tag_count}**')
    add('')
    add('| Most common tag | Count | % of instances |')
    add('| --- | ---: | ---: |')
    for item in report.most_common_tags:
        add(f'| {item.suffix} | {item.count} | {item.percent:.1f}% |')
    add('')

    # Housekeeping
    add('## Housekeeping')
    add('')
    add(f'- Orphaned caption files (no matching image): '
        f'**{report.orphan_caption_count}**')
    for example in report.orphan_caption_examples:
        add(f'  - `{example}`')
    add(f'- Images with no caption file: **{report.missing_caption_count}**')
    for example in report.missing_caption_examples:
        add(f'  - `{example}`')
    add(f'- Other (non-image) files: **{report.non_image_file_count}**')
    for example in report.non_image_file_examples:
        add(f'  - `{example}`')
    add('')

    # Per-subfolder
    add('## Per-subfolder')
    add('')
    add('| Folder | Images | Size | Avg MP | Caption % | Complete % |')
    add('| --- | ---: | ---: | ---: | ---: | ---: |')
    for sub in report.subfolders:
        add(f'| {sub.name} | {sub.image_count} | '
            f'{format_size(sub.total_size_bytes)} | '
            f'{sub.average_megapixels:.2f} | '
            f'{sub.caption_coverage_percent:.1f}% | '
            f'{sub.completion_percent:.1f}% |')
    add('')

    return '\n'.join(lines)


def report_to_csv(report: AnalyticsReport) -> str:
    """Render the report as CSV text (several labelled sections)."""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    def blank():
        writer.writerow([])

    writer.writerow(['Directory Analytics'])
    writer.writerow(['Folder', report.root_directory])
    blank()

    writer.writerow(['Overview'])
    writer.writerow(['Total images', report.total_images])
    writer.writerow(['Total size (bytes)', report.total_size_bytes])
    writer.writerow(['Total size', format_size(report.total_size_bytes)])
    writer.writerow(['Subfolders with images', report.subfolder_count])
    writer.writerow(['Folders with images', report.folders_with_images])
    blank()

    writer.writerow(['File formats'])
    writer.writerow(['Format', 'Count', 'Percent'])
    for item in report.format_breakdown:
        writer.writerow([item.suffix, item.count, f'{item.percent:.1f}'])
    blank()

    writer.writerow(['Resolution buckets (longer edge px)'])
    writer.writerow(['Bucket', 'Count'])
    for label, count in report.resolution_buckets.items():
        writer.writerow([label, count])
    blank()

    writer.writerow(['Resolution summary'])
    writer.writerow(['Measure', 'Min', 'Median', 'Max', 'Average'])
    writer.writerow(_summary_row('Width (px)', report.width_summary))
    writer.writerow(_summary_row('Height (px)', report.height_summary))
    writer.writerow(_summary_row('Megapixels', report.megapixel_summary,
                                 decimals=2))
    blank()

    writer.writerow(['Orientation'])
    writer.writerow(['Landscape', report.orientation_counts.get('landscape', 0)])
    writer.writerow(['Portrait', report.orientation_counts.get('portrait', 0)])
    writer.writerow(['Square', report.orientation_counts.get('square', 0)])
    blank()

    writer.writerow(['Aspect ratios'])
    writer.writerow(['Ratio', 'Count', 'Percent'])
    for item in report.aspect_ratio_counts:
        writer.writerow([item.suffix, item.count, f'{item.percent:.1f}'])
    blank()

    writer.writerow(['Flags'])
    writer.writerow(['Very small images', report.very_small_count])
    writer.writerow(['Extreme aspect ratios', report.extreme_aspect_count])
    writer.writerow(['Images without readable dimensions',
                     report.images_without_dimensions])
    blank()

    writer.writerow(['Captions & tags'])
    writer.writerow(['Images with caption', report.images_with_caption])
    writer.writerow(['Images without caption', report.images_without_caption])
    writer.writerow(['Images with zero tags', report.images_with_zero_tags])
    writer.writerow(['Total unique tags', report.total_unique_tags])
    writer.writerow(['Total tag instances', report.total_tag_instances])
    writer.writerow(['Images with prompt', report.images_with_prompt])
    writer.writerow(['Completed images', report.complete_count])
    writer.writerow(['Completion percent', f'{report.completion_percent:.1f}'])
    writer.writerow(['Rare tags (used once)', report.rare_tag_count])
    blank()

    writer.writerow(['Tags per image'])
    writer.writerow(['Measure', 'Min', 'Median', 'Max', 'Average'])
    writer.writerow(_summary_row('Tags', report.tags_per_image_summary))
    blank()

    writer.writerow(['Prompt length (chars)'])
    writer.writerow(['Measure', 'Min', 'Median', 'Max', 'Average'])
    writer.writerow(_summary_row('Length', report.prompt_length_summary))
    blank()

    writer.writerow(['Most common tags'])
    writer.writerow(['Tag', 'Count', 'Percent of instances'])
    for item in report.most_common_tags:
        writer.writerow([item.suffix, item.count, f'{item.percent:.1f}'])
    blank()

    writer.writerow(['Housekeeping'])
    writer.writerow(['Orphaned caption files', report.orphan_caption_count])
    for example in report.orphan_caption_examples:
        writer.writerow(['', example])
    writer.writerow(['Images with no caption', report.missing_caption_count])
    for example in report.missing_caption_examples:
        writer.writerow(['', example])
    writer.writerow(['Non-image files', report.non_image_file_count])
    for example in report.non_image_file_examples:
        writer.writerow(['', example])
    blank()

    writer.writerow(['Per-subfolder'])
    writer.writerow(['Folder', 'Images', 'Size (bytes)', 'Average megapixels',
                     'Caption coverage %', 'Completion %'])
    for sub in report.subfolders:
        writer.writerow([
            sub.name, sub.image_count, sub.total_size_bytes,
            f'{sub.average_megapixels:.2f}',
            f'{sub.caption_coverage_percent:.1f}',
            f'{sub.completion_percent:.1f}'])

    return buffer.getvalue()
