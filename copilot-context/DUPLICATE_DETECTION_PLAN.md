# Duplicate Image Detection — Design Plan

> Status: **Phase 1 implemented.** Exact (SHA-256) + near-duplicate (perceptual
> dHash) detection and the review dialog are built and available under
> **Tools → Find Duplicates...**. Phase 2 (CLIP high-accuracy mode) is still
> planned. This document captures the original agreed design.

## Goal

Help the user find and manage duplicate and near-duplicate images in a loaded
folder. "Duplicate" covers three real-world cases, all of which are in scope:

1. **Exact copies** — the same file imported/copied twice (possibly renamed).
   Byte-for-byte identical.
2. **Resizes / re-saves** — the same image saved at a different size or JPEG
   quality. Visually identical, but the file bytes differ.
3. **Edited variants** — crops, colour changes, added watermarks, or "same
   scene, different frame." Visually similar but meaningfully changed.

Nothing should be destructive by default: detection only *finds* duplicates and
lets the user decide what to do.

## Two-tier detection strategy

Different cases need different techniques because their cost and accuracy differ.

```
Scan images in folder
        |
        +--> Tier 1: content hash (SHA-256)  --> identical bytes? --> Exact-duplicate group
        |
        +--> Tier 2: perceptual hash (+ optional CLIP embedding)
                                             --> similar enough?   --> Near-duplicate group
                                                                          |
Exact + near groups --> Review dialog: keep / delete / move / tag <-------+
```

### Tier 1 — Exact duplicates (cheap, always on)

- Compute a **content hash (SHA-256)** of each file's raw bytes.
- Files sharing a hash are byte-for-byte identical.
- Fast, 100% reliable, requires no AI model.
- Covers case 1 ("imported the same folder twice").

### Tier 2 — Near duplicates (configurable strictness)

Offer a **strictness setting**. Two techniques, ideally both available:

- **Perceptual hash (pHash / dHash)** — a small fingerprint of the image's
  visual structure. Cheap to compute; comparison is a fast count of how many
  bits differ (Hamming distance). Catches resizes and re-compressions well
  (cases 1 and 2). This is the sensible **default**.
- **CLIP image embeddings** — run each image through the CLIP *vision* model to
  get a semantic fingerprint, then compare by cosine similarity. Catches the
  harder case 3 (crops, colour shifts, minor edits). More accurate, but heavier
  (needs the model loaded and ideally a GPU) and can surface images that are
  merely *similar* rather than true duplicates.

Strictness slider: stricter = only obvious duplicates; looser = catches edited
variants but with more false positives.

> Note: the bundled `clip-vit-base-patch32` folder is currently used **only as a
> tokenizer** (for counting caption tokens). CLIP-embedding mode would be the
> first use of its **vision** side, so it reuses a model we already ship.

## How it fits taggui's existing architecture

- **Caching (reuse the `ScanCache` pattern):** store each image's content hash,
  perceptual hash, and (optionally) CLIP embedding in a JSON cache keyed by
  `(file_path, mtime_ns)`, alongside the existing dimension/caption caches. This
  makes re-scans instant and only recomputes files whose modification time
  changed. See `taggui/utils/scan_cache.py`.
- **Background work:** compute hashes/embeddings on a worker thread during or
  after the folder scan, using the existing `QThreadPool` / `QRunnable`
  machinery (see `taggui/widgets/main_window.py`), with a progress indicator so
  the UI never freezes.
- **Data model:** duplicate grouping is derived on demand and does **not** need
  to be persisted in the `Image` dataclass (`taggui/utils/image.py`). Optional:
  a transient flag or a `duplicate` tag if the user chooses the "tag" action.

## Review UI

A new **"Find Duplicates" dialog** (under an appropriate menu) that:

- Shows duplicate **groups**, each group displayed side by side with thumbnail,
  dimensions, file size, and full path.
- Lets the user **keep one** image per group and act on the rest:
  - **Delete to Recycle Bin** (recoverable — never a hard delete by default),
  - **Move to a folder**, or
  - **Add a tag** (e.g. `duplicate`) so nothing is removed, just marked.
- Offers sensible **auto-select helpers**, e.g. "keep the largest resolution" or
  "keep the newest file," which the user can review before applying.

## Suggested rollout

- **Phase 1 (first version):** Tier 1 (exact, SHA-256) + Tier 2 perceptual-hash
  near-duplicate detection, plus the review dialog. Fast, no GPU, no model
  loading — covers the most common cases.
- **Phase 2 (follow-up):** optional CLIP-embedding "high accuracy" mode for
  edited variants, reusing the already-bundled CLIP model.

## Open questions for implementation time

- Default strictness threshold values for perceptual hash and for CLIP cosine
  similarity.
- Whether duplicate scanning runs automatically on every folder load or only
  when the user opens the dialog (performance vs. convenience).
- Which new Python dependency to use for perceptual hashing (e.g. `imagehash`)
  vs. implementing a small dHash directly to avoid adding a dependency.
