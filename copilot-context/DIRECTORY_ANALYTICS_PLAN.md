# Directory Analytics — Design Plan

> Status: **Phase 1 implemented.** A read-only analytics report over the loaded
> image folder is built and available under **Tools → Directory Analytics...**.
> This document captures the agreed design.

## Goal

Give the user a fast, **read-only** overview of the dataset in the currently
loaded folder so they can spot problems before training: how many images there
are and how big they are, whether resolutions and aspect ratios are consistent,
how well captions and tags cover the set, and any housekeeping issues (orphaned
caption files, images with no caption, stray non-image files).

Nothing is ever deleted, moved, or modified. The tool only *reports*.

## What it reports

The report is organised into five sections (shown as tabs in the dialog):

1. **Overview** — total images, total on-disk size, number of subfolders, and a
   per-file-format breakdown (count and % per extension).
2. **Resolution & aspect ratio** — distribution across size buckets (longer edge
   `<512`, `512–1024`, `1024–2048`, `>2048`), min/median/max of width, height
   and megapixels, an orientation split (portrait / landscape / square), the
   most common named aspect ratios (1:1, 4:3, 3:2, 16:9, …), and flags for
   *very small* images and *extreme* aspect ratios.
3. **Captions & tags** — images with vs. without a caption `.txt` sidecar,
   images with zero tags, tags-per-image statistics (average / median / min /
   max), total unique tags and total tag instances, the most common tags and
   the "used once" (rare) tags, natural-language-prompt coverage and length,
   and the completion rate (`is_complete`).
4. **Housekeeping** — orphaned `.txt` captions (no matching image), images that
   have no caption sidecar, and other (non-image, non-caption) files found in
   the tree.
5. **Per-subfolder** — a table with, for each folder: image count, total size,
   average resolution (megapixels), caption coverage %, and completion %.

The whole report can be exported to **CSV** or **Markdown**.

## How it fits taggui's existing architecture

This feature mirrors the structure and quality of the duplicate-detection
feature (`DUPLICATE_DETECTION_PLAN.md`).

- **Pure logic layer** (`taggui/utils/directory_analytics.py`): a
  `compute_analytics()` function that takes the already-loaded `Image` objects
  (`taggui/utils/image.py`), the scan root directory, and the list of
  recognised image suffixes, and returns an `AnalyticsReport` dataclass. It
  reuses data the scanner already parsed — `image.dimensions`, `image.tags`,
  `image.natural_language_prompt`, `image.caption_file_modified_time_ns` (used
  to know whether a caption sidecar exists) and `image.is_complete` — so it
  never re-decodes images. It reuses `collections.Counter` for tag/format
  frequencies, the same approach as `tag_counter_model`. The only new I/O is a
  single `os.walk` of the folder to read on-disk file sizes and discover
  caption / non-image files.
- **Background work**: the dialog runs `compute_analytics()` on a worker thread
  using the existing `QThreadPool` / `QRunnable` machinery with a
  `QProgressDialog`, exactly like the duplicate scanner, so the UI never freezes
  on large folders and the scan can be cancelled.
- **Data model**: analytics are derived on demand and are **not** persisted in
  the `Image` dataclass or any cache. Re-running the tool simply recomputes.

## Review UI

A new **"Directory Analytics" dialog** (Tools menu) that:

- Runs the scan with a cancellable progress dialog.
- Presents the five sections above as tabs.
- Offers **Export to CSV** and **Export to Markdown** buttons
  (`QFileDialog.getSaveFileName`).
- Is entirely read-only — it has no delete/move/edit actions.

## Thresholds (fixed constants, documented in code)

To avoid adding new settings, a few classification thresholds are module
constants in `directory_analytics.py`:

- *Very small* image: longer edge `< 256` px.
- *Extreme* aspect ratio: long:short `>= 3:1`.
- *Square*: within 2% of a 1:1 ratio.
- Size buckets: longer-edge boundaries at 512, 1024, 2048.

## Suggested rollout

- **Phase 1 (this version):** the read-only report described above with CSV /
  Markdown export. No new dependencies (uses the standard library, PySide6, and
  the already-parsed image data).
- **Possible follow-ups:** duplicate-caption detection, tag co-occurrence
  analysis, per-format size totals, or charts/graphs in the dialog.
