# KINSHO — AniList Metadata Import: Implementation Plan

## Overview

Automatic metadata fetching from AniList for manga libraries. When enabled on a library, KINSHO can search AniList by manga folder name and import **description, genres, and tags** directly into each manga's `dims.json`. Covers are a planned extension (see below).

The guiding principle throughout: **each substep is independently testable before the next one depends on it**, so bugs are never in two unknown places at once.

---

## Design Decisions (settled)

| Question | Decision |
|---|---|
| Provider | AniList only for now. Others (MangaDex, MAL) can be added later as a separate task. |
| Trigger | A dedicated **"Scan for Metadata"** button per library in Settings — not tied to regular scan/rescan. |
| Which mangas get processed | Only mangas **without** a `metadata_mtime` in their `dims.json`. Already-fetched mangas are always skipped. |
| Automatic vs. manual matching | Single confident match → auto-accept. Multiple candidates → queue for manual review. No match / low confidence → skip silently. |
| Confidence threshold | Determined by `score_all_candidates()` (substep 2). Exact threshold value to be tuned in substep 5 after real-library testing. |
| Match tie-breaking | Format filtering (per library setting) eliminates ties caused by multiple editions of the same title (e.g. MANGA vs NOVEL). |
| Tags/genres on refetch | **Full overwrite** — delete and repopulate. No merge. If the user is refetching, they've decided the old data was wrong. |
| Tag spoiler filter | Two modes per library: **anti_spoiler** (exclude AniList-marked spoiler tags) or **all** (keep everything above rank threshold). |
| Tag rank floor | Default 60/100. Tags below this are excluded regardless of spoiler mode. |
| `metadata_mtime` | Written to `dims.json` alongside the metadata fields on every write. Presence of this key = "this manga has been fetched before." |
| Re-fetch | Per-manga only, triggered manually from the manga detail or volume detail page via a popup. User selects which fields (description / genres / tags) to reimport. |
| Covers | Planned extension — separate thread of work after the text metadata pipeline is complete. Covers would be downloaded into the manga's folder during the metadata scan, following the same naming convention the scan/rescan process already expects. User-added covers always take priority. |
| Storage | No changes to existing storage layout. `dims.json` stays at `covers_dir/<library_id>/<manga_name>/dims.json`, read/written via the existing `load_manga_dims` / `save_manga_dims` functions in `main.py`. |
| New dependency | `httpx` — async HTTP client. Install with `pip install httpx`. |

---

## New / Modified Files

| File | Status | Role |
|---|---|---|
| `metadata_fetch.py` | 🆕 New | All AniList client + scoring + write logic. No imports from `main.py`. |
| `test_metadata_fetch.py` | 🆕 New | Interactive terminal test for substeps 1–3. |
| `main.py` | ⏳ To be modified | New endpoint, library settings fields, skip-gate, scan loop (substeps 4–5). |
| `settings.html` / `settings.js` | ⏳ To be modified | Format selector, tag filter mode, "Scan for Metadata" button (substep 6). |
| `manga_detail.html` | ⏳ To be modified | "Refetch metadata" button + field-picker popup (substep 7). |

---

## Implementation Substeps

### ✅ Substep 1 — AniList client (`metadata_fetch.py`)
**Status: Complete and tested against live API.**

`search_anilist_manga(title, per_page, formats)` — takes a title string and optional list of AniList `MediaFormat` values, returns up to `per_page` candidate results as plain Python dicts.

Each result contains:
- `anilist_id`, `format`, `status`, `site_url`
- `title_romaji`, `title_english`, `title_native`
- `description` (plain text, HTML stripped by AniList)
- `genres` (list of strings)
- `tags` (list of dicts: `name`, `rank`, `isGeneralSpoiler`, `isMediaSpoiler`)
- `cover_url_large`, `cover_url_medium`

Format constants available as `ANILIST_MANGA_FORMATS = ["MANGA", "MANHWA", "MANHUA", "ONE_SHOT", "NOVEL"]`.

Errors (network, HTTP status, GraphQL-level) are raised, not swallowed — error policy is the caller's responsibility.

---

### ✅ Substep 2 — Confidence scoring (`metadata_fetch.py`)
**Status: Complete and tested.**

`score_title_match(query, candidate)` — compares a folder name against a candidate's romaji/english/native titles and returns the **best** similarity score (0.0–1.0) across the three. Uses `difflib.SequenceMatcher` — no new dependency.

`score_all_candidates(query, candidates)` — convenience wrapper that attaches `match_score` to each candidate dict and returns them sorted best-first.

`_normalize_title(s)` — lowercases and strips punctuation before comparing, so `"Berserk (1989)"` still matches `"Berserk"` reasonably well.

**Known behaviour to keep in mind for substep 5:**
- Exact folder name → score `1.0`. Folder name with a year suffix (e.g. `"Berserk (1989)"`) → score ~`0.74` for the correct match — still clearly best, but below `1.0`. The confidence threshold in substep 5 should account for this.
- Abbreviations (e.g. `"orv"` for *Omniscient Reader's Viewpoint*) score low (`~0.20`) and correctly fall to the manual-review queue rather than being auto-accepted.

---

### ✅ Substep 3 — Data write (`metadata_fetch.py`)
**Status: Not tested.**

`apply_anilist_metadata(library_id, manga_name, candidate, fields, load_fn, save_fn, min_tag_rank, tag_filter)` — writes chosen fields from a confirmed candidate into `dims.json` via caller-supplied load/save functions.

- `fields`: subset of `{"description", "genres", "tags"}` — controls which fields are written. Unspecified fields are left completely untouched in the existing `dims.json`.
- `tag_filter`: `"anti_spoiler"` (default) or `"all"`. Anti-spoiler mode excludes any tag where `isGeneralSpoiler` or `isMediaSpoiler` is `True` on AniList, in addition to the `min_tag_rank` floor.
- `metadata_mtime`: always written as an ISO timestamp, regardless of which `fields` subset was requested. This is the skip-gate key checked in substep 4.
- Existing `dims.json` keys unrelated to AniList (`chapters`, `volumes`, image dimensions, etc.) are preserved untouched.

Tag filter mode constants: `TAG_FILTER_ANTI_SPOILER = "anti_spoiler"`, `TAG_FILTER_ALL = "all"`, `TAG_FILTER_MODES = [...]`.

Takes `load_fn` / `save_fn` as arguments (not direct imports) so the module stays testable without the full FastAPI app running. In production, pass `load_manga_dims` / `save_manga_dims` from `main.py`.

---

### ⏳ Substep 4 — Skip-gate (`main.py`)
**Status: Not started.**

A small helper that checks whether a manga already has `metadata_mtime` in its `dims.json`, and returns `True` (skip) or `False` (proceed). This is the sole gate controlling whether a manga gets processed during a metadata scan — it replaces no other logic, it just wraps the `load_manga_dims` check cleanly so substep 5's loop is readable.

```python
def has_metadata(library_id: int, manga_name: str) -> bool:
    dims = load_manga_dims(library_id, manga_name)
    return "metadata_mtime" in dims
```

Test: call it on a manga whose `dims.json` has `metadata_mtime` (written by substep 3 test), and one that doesn't. Confirm correct bool in each case.

---

### ⏳ Substep 5 — Scan endpoint (`main.py`)
**Status: Not started.**

New FastAPI endpoint: `POST /api/libraries/{library_id}/scan-metadata`

Loops over all mangas in the library that don't have `metadata_mtime` (via the skip-gate from substep 4). For each:

1. Call `search_anilist_manga(manga["name"], formats=library_formats)`.
2. Call `score_all_candidates(manga["name"], results)`.
3. Apply confidence policy:
   - **Score `≥ threshold` and only one candidate at that score** → auto-accept, call `apply_anilist_metadata`, mark as done.
   - **Score `≥ threshold` but tied** → add to pending-review queue.
   - **Score `< threshold`** or **no results** → add to pending-review queue (or skip entirely — TBD).
4. Rate-limit: AniList allows ~90 requests/minute. Add a small delay between requests in the loop (`asyncio.sleep(0.7)` ≈ safe margin).

Returns a JSON response:
```json
{
  "auto_matched": 12,
  "pending_review": [
    {
      "manga_name": "...",
      "candidates": [ { ...scored candidate... }, ... ]
    }
  ],
  "skipped": 3,
  "errors": [ { "manga_name": "...", "error": "..." } ]
}
```

The `pending_review` list is what the frontend (substep 6) walks the user through one by one.

Library-level settings needed by this endpoint (to be added to the library record in `data.json`):
- `metadata_formats`: list of `MediaFormat` strings (or `null` for all).
- `metadata_tag_filter`: `"anti_spoiler"` or `"all"`.
- `metadata_fields`: subset of `["description", "genres", "tags"]`.

---

### ⏳ Substep 6 — Settings UI (`settings.html` / `settings.js`)
**Status: Not started.**

Changes to the library card in Settings:

- **Format selector**: multi-select or checkbox group built from `ANILIST_MANGA_FORMATS`. Saved as `metadata_formats` on the library record.
- **Fields checkboxes**: `description`, `genres`, `tags` — which fields to import. Saved as `metadata_fields`.
- **Tag filter toggle**: radio or select between `anti_spoiler` and `all`. Saved as `metadata_tag_filter`.
- **"Scan for Metadata" button**: calls `POST /api/libraries/{library_id}/scan-metadata`, then opens a **review modal** that walks through `pending_review` entries one by one — each showing the manga folder name, and a small card per candidate (cover thumbnail, title, format, score) for the user to pick from or skip.

---

### ⏳ Substep 7 — Per-manga refetch (`manga_detail.html`)
**Status: Not started.**

A "Refetch Metadata" button on the manga detail page, visible only to admins/users with edit permission.

Opens a popup with:
- Field checkboxes: which of `description` / `genres` / `tags` to reimport (all checked by default).
- A "Search" trigger that calls `search_anilist_manga` for this manga's title and shows scored candidates.
- Candidate cards (cover thumbnail, title, format, score) for the user to pick from.
- On confirm: calls `apply_anilist_metadata` with the chosen candidate and field set. This overwrites the selected fields completely and updates `metadata_mtime`.

This reuses `metadata_fetch.py`'s functions entirely — the only new code is the endpoint wiring in `main.py` and the popup UI in `manga_detail.html`.

---

## Known Limitations / Future Work

- **Covers**: not included in this implementation. Planned as a follow-up: downloading AniList cover images into the manga's folder during metadata scan, with user-added covers always taking priority (matching the existing `user_covers` precedence logic in `main.py`).
- **Additional providers**: MangaDex, MyAnimeList (via Jikan), others. The `metadata_fetch.py` module is intentionally scoped to AniList only — a second provider would be a new module following the same function-signature contract.
- **`_normalize_title` tuning**: the current normalization (lowercase + strip punctuation) may produce lower-than-expected scores for folder names with year suffixes, volume numbers, or publisher tags (e.g. `"Berserk (1989)"`, `"One Piece Vol.1-10"`). If real-library testing in substep 5 reveals systematic mismatches, the normalization function is the first place to extend.
- **AniList rate limiting**: the scan loop will add `asyncio.sleep(0.7)` between requests (~85 req/min, safely under the ~90/min limit). For very large libraries this means the scan can take several minutes — the substep 6 UI should show a progress indicator rather than just a spinner.
