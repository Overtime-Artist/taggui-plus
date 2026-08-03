# Completion status preservation

This document describes how taggui preserves the per-image **"complete"**
workflow flag when images (and their caption `.txt` files) are moved, renamed,
or edited — including operations performed **outside** the program — and the
edge cases that are intentionally allowed to fail safely.

It is a reference for the logic implemented in
[`taggui/utils/completion_store.py`](../taggui/utils/completion_store.py), with
integration points in
[`taggui/models/image_list_model.py`](../taggui/models/image_list_model.py) and
[`taggui/widgets/main_window.py`](../taggui/widgets/main_window.py).

## What "complete" is

"Complete" is a workflow flag the user sets to mark an image as fully
tagged/reviewed. It is the user's own decision and is stored **separately** from
the caption files (in `completion_cache.json` in the app's cache directory, e.g.
`%LOCALAPPDATA%\taggui` on Windows). It is deliberately **never** written into
the caption `.txt`, so it never becomes part of exported training data.

## Goals

Preserve the "complete" flag across:

1. **External move** — image + caption moved to another folder outside the app.
2. **External rename** — image + caption renamed outside the app.
3. **External edit in place** — image edited outside the app but kept at the
   same path/name. (An image that is edited **and** renamed is treated as a new
   image — see edge cases.)
4. All of the above **without noticeable slowdown**.

## Identity model

Each complete image is remembered by three things:

| Signal | Purpose | Cost |
| --- | --- | --- |
| **File path** | Fast path. O(1) lookup; also covers edit-in-place. | Free |
| **Image content hash** (SHA-256 of the image bytes) | Confirms "the same image" after a move/rename. | Hashed rarely (see below) |
| **Caption content hash** (SHA-256 of the `.txt` bytes) | Confirms "the same finished work" so an unfinished caption is never marked complete. | Cheap (caption is tiny) |

File size and modification times are also stored, used only as cheap
prefilters/change-detectors — never as identity on their own.

## How preservation works

### 1. Marking complete (user action)
When the user marks an image complete, its image hash and caption hash are
computed **once**, then stored. This is the only place the image bytes are
hashed proactively.

### 2. Normal scan (opening/reloading a folder)
For every image the store is consulted **by path first** (O(1)). This covers the
common case and edit-in-place with zero hashing.

During the scan the store is *reconciled*:

- **Hash refresh (edited files):** For an image that is still complete at its
  path, its stored image hash is recomputed **only if its modification time
  changed** (i.e. it was actually edited) or was never hashed (legacy
  migration). The caption hash is likewise refreshed only when the caption
  file's modification time changed. Untouched images cost nothing beyond the
  `stat()` the scanner already performs.
- **Re-homing (moved/renamed files):** A complete entry whose stored path no
  longer exists on disk is an **orphan**. Newly-seen images are matched against
  orphans by **size (prefilter) → image hash → caption hash**. Every image whose
  image hash *and* caption hash match an orphan is marked complete, and the
  matched orphan entry is removed. Orphans that find no match are **kept** (they
  may re-home later when their new folder is scanned).

### 3. In-program caption edit
When a caption is saved inside the app for a complete image, its stored caption
hash is refreshed in memory immediately and flushed to disk on the next
save/reconcile or when the app closes. The image bytes are **not** re-hashed
(editing a caption never changes the image).

### 4. In-program move/copy
Handled directly (see PR #34): a move transfers the entry (with its hashes) to
the new path; a copy mirrors it. No content matching is needed because the app
knows the exact source and target.

## Why it stays fast

- The path lookup is O(1) and handles the normal case and edit-in-place.
- Image bytes are hashed only: (a) once when marking complete, (b) when an
  already-complete image's mtime shows it was edited, or (c) for move/rename
  candidates whose **size** matches a missing complete image.
- A scan with no external changes does **zero** extra hashing.
- Work scales with the number of images you actually changed, not with dataset
  size.

## Duplicate images

Byte-identical duplicates are **never merged or hidden** — taggui keeps the
user's files exactly as they are. When an orphan matches several current files
(same image **and** same caption), **all** of them are marked complete. Because
they are identical finished work, there is no ambiguity and no tie-break needed.

## Edge cases (and how they fail safely)

The design deliberately errs toward "ask the user to re-mark" (a false
**negative**) rather than "wrongly claim done" (a false **positive**), because
completion gates what is treated as finished training data.

| Situation | Behaviour | Safe? |
| --- | --- | --- |
| **Edited *and* renamed externally** | Image bytes changed, so the hash no longer matches; treated as a **new (not complete)** image. Matches goal 3's rule. | Fails safe (re-mark) |
| **Same image, caption changed externally, then moved** | Image hash matches but caption hash differs → **not** auto-completed. | Fails safe (re-mark) |
| **Same image bytes, genuinely different captions** (e.g. `hero.png` vs `hero_backup.png`) | Only files whose caption also matches are completed; the differing one is left alone. | Correct |
| **Moved into a folder not yet opened in the app** | Orphan is kept; it re-homes the first time that folder is scanned. | Deferred, harmless |
| **Caption edited externally in place (image unmoved)** | Still complete via the path fast path. The stored caption hash goes stale until a scan detects the caption mtime change and refreshes it. | Safe; refreshes on scan |
| **Two different images that happen to share a size** | Size is only a prefilter; the image hash must still match. | Correct |
| **SHA-256 collision** | Cryptographically implausible for real image files; would require deliberately crafted inputs. | Not a practical concern |
| **App closed without a scan after an in-program caption edit** | The refreshed caption hash is flushed in `closeEvent`. | Safe |
| **Legacy `completion_cache.json`** (path-only, pre-hash format) | Loads as complete with no hashes; hashes are filled in lazily the first time each image is scanned. Old entries keep working by path. | Backward compatible |
| **Unreadable/locked file during hashing** | Hashing returns no digest; the entry simply isn't matched this scan (no crash, no wrong match). | Fails safe |

## Files

- `taggui/utils/completion_store.py` — entry model, hashing, reconcile logic.
- `taggui/models/image_list_model.py` — scanner passes scan info to
  `reconcile`; caption save refreshes the caption hash.
- `taggui/widgets/main_window.py` — flushes the store on app close.
- `taggui/widgets/image_list.py` — in-program move/copy (from PR #34).
