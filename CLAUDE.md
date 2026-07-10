# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
python main.py
```

Starts a uvicorn server on port 8000. The app prints its LAN IP on startup. First run auto-creates an `admin`/`admin` account.

Docker:

```bash
docker build -t kinsho .
docker run -p 8000:8000 -v /path/to/data:/data kinsho
```

No build step, no package manager beyond `pip install -r requirements.txt`.

## Architecture

Kinsho is a self-hosted manga/comic reader. The backend is a single FastAPI file (`main.py`, ~3500 lines) that serves both the API and Jinja2 HTML templates. The frontend is vanilla JavaScript in `static/`. There is no database — everything is stored in JSON files.

### Data layout

All persistent data lives under a configurable `data_path` (set via the Settings UI and stored in `bootstrap.json` next to `main.py`):

| File/Dir | Contents |
|---|---|
| `bootstrap.json` | Pointer to `data_path` |
| `{data_path}/data.json` | Libraries list, manga index, global tag/genre lists, themes |
| `{data_path}/users.json` | User accounts (hashed passwords, roles) |
| `{data_path}/sessions.json` | Active login sessions |
| `{data_path}/permissions.json` | Per-user library and tag permissions |
| `{data_path}/{username}.json` | Per-user reading history, bookmarks, favourites, theme, cover overrides |
| `{data_path}/covers/{lib_id}/{manga_name}/` | Processed cover images (small + large `+` variant) |
| `{data_path}/covers/{lib_id}/{manga_name}/dims.json` | Per-manga metadata: page dims, chapters/volumes, tags, genres, description |
| `{data_path}/covers/{lib_id}/{manga_name}/thumbs/{source_id}/` | Page thumbnails extracted in background |

`dims.json` is the primary per-manga store — it holds chapter/volume page dimensions, source file paths, and user-editable metadata (tags, genres, description).

### Manga classification ("cases")

The scanner (`scan_library` in `main.py`) classifies each manga into one of four structures:

- **case1** — a single archive (CBZ/CBR/ZIP) whose interior contains chapter-named subdirectories
- **case2** — a folder where each file is a volume (archive, PDF, or EPUB)
- **case3** — a folder where each archive file is a chapter (archive filename contains "chapter")
- **loose** — a folder tree where subdirectories contain raw image files directly (case1 = chapter subfolders, case2 = volume subfolders)

The reader routes and templates branch on this type. `/manga/{lib}/{id}` renders `manga_detail.html` for chapter-type manga or `volume_detail.html` for volume-type manga.

### Content format support

Supported formats (all optional except Pillow):

| Format | Library | Install |
|---|---|---|
| CBZ / ZIP | `zipfile` (stdlib) | always |
| CBR / RAR | `rarfile` | `pip install rarfile` + `unrar` binary |
| PDF | `pymupdf` | `pip install pymupdf` |
| EPUB | `ebooklib` | `pip install ebooklib` |

Missing libraries degrade gracefully — the app logs a message and skips those files.

### Authentication

`auth.py` handles all auth. Sessions are UUID tokens stored as cookies (`kinsho_session`) and `X-Auth-Token` headers (for native app clients). Sessions expire after 30 days. Admins have unrestricted access; regular users have per-library visibility and per-tag blocking configured via `permissions.json`.

### Background tasks

- **Library scan** (`run_scan`): triggered manually via API or on startup; classifies and indexes all manga, extracts cover images. Auto-rescans every 12 hours via `periodic_library_rescan`.
- **Thumbnail extraction** (`extract_thumbs_for_library`): runs in a daemon thread after each scan; generates 100px-wide JPEG thumbnails for every page of compressed sources (skips loose folders).
- **PDF pre-render** (`_prerender_pdf_volume`): renders PDF pages to full-size JPEG files on disk during thumbnail extraction.

### In-memory caches

`main.py` maintains several module-level LRU caches (max 64–128 entries): `_archive_page_cache`, `_pdf_page_cache`, `_epub_page_cache`, `_archive_image_list_cache`, and persistent open file handles (`_open_archive_handles`, `_open_pdf_handles`). These are process-scoped — restarting the server clears them.

### Frontend

Each page is a Jinja2 template that loads one or more vanilla JS files from `static/`. `static/api.js` sets `window.API_BASE` and `window.apiUrl()` — all fetch calls go through `apiUrl(path)` to support both browser-served mode (empty base) and a future native app mode (Capacitor/Tauri with a saved server URL in localStorage).

Key JS files:
- `static/app.js` — main library browser, search, tab management
- `static/settings.js` — settings page (libraries, themes, users, permissions)
- `static/category_list.js` — favourites / last-read / random category views

The chapter/volume reader is entirely in `templates/chapter_reader.html` (inline script).

---

## Coding principles

Follow **YAGNI** (You Aren't Gonna Need It): only build what is explicitly asked for right now. No speculative abstractions, no "we might need this later" helpers, no extra configuration options, no fallback paths for scenarios that don't exist yet. If a feature isn't in the current task, don't touch it.

---

## Git & GitHub workflow

The project is hosted at **https://github.com/kalako99/kinsho** (private). Always keep the remote in sync.

### Rules

- **Commit before starting any non-trivial change** — gives a clean revert point.
- **Push after every commit** — the GitHub copy is the canonical backup.
- **One logical change per commit** — don't bundle unrelated edits.
- Write commit messages in the imperative, lowercase, no period: `add backdrop toggle switches`, `fix cover extraction for case3 archives`.
- Never force-push `main`.

### Typical workflow

```bash
# Stage specific files (never `git add .` blindly — check git status first)
git add <files>

# Commit
git commit -m "your message"

# Push
git push origin main
```

### Branch strategy

Work directly on `main` for now (solo project). If a feature is large or risky, create a short-lived branch:

```bash
git checkout -b feature/anilist-theme
# ... work ...
git checkout main && git merge feature/anilist-theme
git push origin main
git branch -d feature/anilist-theme
```

---

## Completed work

### 1. Backdrop toggle switches — done

Two independent per-user on/off switches in Settings (`backdrop_list` / `backdrop_detail` in `{data_path}/{username}.json`, `POST /api/settings/backdrop` in `main.py`) control the backdrop image on the manga list page and the manga/volume detail pages.

### 2. Accent Colors rename + Theme system — done

- **Accent Colors** — the old "Themes" color-switching feature, renamed everywhere in the settings UI. The five built-in palettes are unchanged.
- **Theme** — a separate visual-style system (shapes/spacing/borders, not colors), stored as `active_visual_theme` alongside `active_theme`. Options: **Default** (original CSS) and **Sharp** (grid layout, crisp corners, chapter list as sidebar — this replaced the originally-planned "AniList" name/style) plus **Custom** (see below).

### 3. Custom theme with CSS editor popup — done

Settings has a Custom Theme Editor popup (`templates/settings.html`): named CSS themes stored in `custom_themes` in the per-user JSON, applied live via an injected `<style>` tag, with "Reset to Default" / "Reset to Sharp" buttons to reload a baseline into the editor.

### 4. Fetch Metadata feature — done

`metadata_fetch.py` is wired in: `GET /api/manga/{library_id}/{manga_id}/search-metadata` and `POST .../apply-metadata` in `main.py`, with a "Fetch Metadata" popup in `manga_detail.html` (search AniList/MangaDex, pick a candidate, choose which fields to apply, save into `dims.json`).

### 5. Settings page reorganized into sections — done

Replaced the single long scrolling Settings page with a Jellyfin-dashboard-style
layout: a left sidebar (collapses to horizontal pill tabs under 720px, same
visual pattern as the library tabs on the home page) with four sections,
toggled via `activeSection` in `static/settings.js` — no functional change to
any individual setting, just regrouped:

- **General** — server connection (native app only), account (login/change
  password/logout/logout-everywhere), reading-time analytics.
- **Libraries** — data folder path, library management.
- **Appearance** — accent colors, theme, backdrop toggles, collections
  preferences (see below).
- **Permissions** (admin only) — create user, per-user permissions.

### 6. Collections — done

Replaces the old private-only `lists` feature (renamed for consistency with
other manga/comic readers — e.g. Komga's Collections). A collection groups
whole manga entries, possibly across libraries, under one name with its own
description/tags/genres and member order.

- **Shared vs. private.** Collections created by an admin are *shared*
  (visible to every user); collections created by anyone else are *private* to
  that user, same scope `lists` had. A regular user who wants a private
  collection as an admin uses a separate non-admin account for that — the app
  doesn't special-case it.
- **Storage.** Shared collections live in `{data_path}/collections.json`;
  private ones live under `collections` in `{data_path}/{username}.json`. Both
  share the same record shape: `id`, `name`, `shared`, `members` (ordered list
  of `{library_id, manga_id, manga_name}`), and `description`/`tags`/`genres`
  each paired with a `*_customized` flag.
- **Derived fields, until customized.** While `description_customized` is
  false, the description mirrors whichever member is currently first —
  reordering updates it live. While `tags_customized`/`genres_customized` are
  false, they're the union of every member's tags/genres, recomputed whenever
  membership changes. The first manual edit to a field flips its flag and
  locks it — future reorders/membership changes stop touching it.
  (`_sync_collection_derived_fields` in `main.py`.)
- **Cover is a per-viewer preference**, not a collection-wide setting — same
  mechanic as the existing per-user manga cover override
  (`user_data["covers"]`), just keyed by collection id
  (`user_data["collection_covers"]`). Default before any override: the
  current first member's own resolved cover.
  `GET /api/collections/{id}/cover-options` aggregates every visible member's
  available covers into one picker.
- **Visibility filtering.** Every read of a collection's members runs through
  `can_access_library`/`is_manga_blocked` per viewer — same total-lockout rule
  as the rest of the app. For a shared collection, if filtering leaves zero
  visible members for a given user, the whole collection is treated as
  nonexistent for them (omitted from listings, 404 on direct navigation) —
  not just shown empty.
- **Permissions.** Any admin can manage any shared collection (reorder, edit
  metadata, add/remove members) — same as any other admin-only resource in
  this app. A non-admin viewing a shared collection can only browse it and set
  their own cover override. Private collections are fully owned by their
  creator.
- **Pages.** `templates/collection_detail.html` (built by copying
  `volume_detail.html` and reworking it) shows the collection's hero
  (name/description/tags/genres, all editable if `can_edit`) plus a
  drag-to-reorder grid of member tiles — the tiles reuse the manga-list page's
  `.manga-thumb` styling, not `volume_detail`'s volume-card styling.
  `templates/collections_list.html` + `static/collections_list.js` is the
  `/collections` browse page (modeled on `category_list.html`), listing a
  user's own + visible shared collections as manga-styled tiles.
- **Click-interception.** If a manga belongs to a collection visible to the
  current user, clicking its tile *anywhere* in the app (library tab grid,
  Favourites/Last Read/Random rows, search results, category pages) opens the
  collection's page instead of the manga's own page — the frontend fetches a
  `manga_id → collection_id` membership map once
  (`GET /api/collections/membership`) and every tile click-handler consults it
  before navigating. The only way to reach a manga's own detail page when it's
  in a visible collection is by drilling into it from inside the collection
  page. This is implemented independently in `static/app.js`,
  `static/category_list.js`, and `templates/search_page.html`.
- **Settings toggles** (Appearance tab): show/hide the Collections row on the
  home page (`show_collections_row`), and hide the admin's shared collections
  entirely (`hide_admin_collections`) — both per-user.

### 7. OPDS + OPDS-PSE catalog — done

A standard OPDS 1.2 catalog under `/opds/*`, with the Page Streaming
Extension so comic-focused OPDS clients (Chunky, Mihon's generic OPDS
source) can page through a chapter/volume live instead of downloading a
whole archive first. Not compatible with Komga-specific clients (Komelia,
Tachiyomi/Mihon's dedicated Komga extension) — those speak Komga's own
proprietary API, not OPDS.

- **`opds.py`** — pure feed-building module (`xml.etree.ElementTree`, not
  hand-written string templates, so titles/tags containing `&`/`<` escape
  correctly). Takes only plain already-resolved data from `main.py`; does no
  auth, no data access, no permission filtering itself.
- **Auth**: every real OPDS client (Chunky, Panels, KOReader, Mihon) only
  speaks HTTP Basic Auth, not the session cookie / `X-Auth-Token` the rest of
  the app uses. `auth.get_user_from_basic_auth()` verifies Basic credentials
  against the existing `users.json` hashes; `auth.get_opds_user()` tries the
  normal session first (so previewing a feed in a logged-in browser tab still
  works) and falls back to Basic Auth. The blanket `/api/*` auth middleware
  and the cover-image/page-streaming routes all use `get_opds_user()` now
  instead of `get_current_user()` — a strict superset, so this changes
  nothing for the web/native app (which never sends a Basic Auth header) and
  only adds access for OPDS requests. 401s from the `/opds/*` routes
  specifically include a `WWW-Authenticate: Basic` header so clients know to
  prompt for credentials.
- **Routes**: `/opds/` (nav feed, one entry per accessible library),
  `/opds/library/{id}` (nav feed of manga, paginated 50/page),
  `/opds/manga/{lib}/{id}` (acquisition feed — one entry per chapter/volume,
  each carrying the PSE stream link + `pse:count` + `pse:lastRead` pulled
  from the user's existing reading history), `/opds/search-description.xml` +
  `/opds/search?q=...` (OpenSearch). All filtering reuses
  `can_access_library`/`is_manga_blocked` exactly like every other route —
  same total-lockout behavior (404, not a visibly-empty entry) for denied
  libraries/blocked-tag manga.
- **Loose-chapter numeric page index**: `get_chapter_page` previously only
  addressed loose (non-archive) chapter pages by literal filename
  (`/page/0007.jpg`), which doesn't fit OPDS-PSE's "client increments the
  trailing number in the URL" convention unless filenames happen to be
  clean sequential numbers. Added a numeric-index branch (`/page/{n}`,
  resolved against the sorted file list) mirroring what `get_volume_page`'s
  loose branch already did — backward compatible, the old filename-based
  URLs the web reader already uses still work unchanged.
- **No bulk CBZ-on-demand yet** — entries only carry the PSE stream link,
  not a plain `rel="acquisition"` download link. Fine for PSE-aware clients
  (Chunky); a client that only understands plain OPDS acquisition (no PSE)
  will see the entry but have nothing to open. Add this — zip a
  chapter/volume's pages into a CBZ on request, from whatever the source
  actually is (archive/loose/PDF pages/EPUB pages) — only if a client that
  needs it comes up in practice.
- **Not usable from Tachiyomi/Mihon** — that app has no generic "any OPDS
  server" source; each self-hosted server (Komga, Kavita, Suwayomi) ships its
  own bespoke extension talking to that server's own API, and Kinsho doesn't
  have one. Would require a separate Kotlin/Gradle/Android Studio project,
  not something planned for now — the native app + OPDS (Chunky/KOReader)
  cover external access instead.

### 8. ComicInfo.xml reading — done

Reads the Komga/Kavita/ComicRack-standard `ComicInfo.xml` sidecar — embedded
at a CBZ/CBR's root, or sitting next to the images in a loose (unarchived)
chapter/volume folder — and uses it to fill in a manga's `description`/
`genres`/`tags`.

- **`comicinfo.py`** — pure parsing module (`xml.etree.ElementTree`, same
  reasoning as `opds.py`): `parse_comicinfo_xml()` reads `<Summary>`/`<Genre>`/
  `<Tags>` from raw bytes; `locate_and_read()` finds `ComicInfo.xml`
  case-insensitively inside an open archive or a loose folder;
  `aggregate_for_manga()` combines it across a whole manga's chapters/volumes
  — **first chapter's `<Summary>` wins** for description (a chapter-level
  field being used as a series-level one), **intersection** of
  `<Genre>`/`<Tags>` across every chapter that has them wins for genres/tags
  (a value repeated on every chapter is treated as a real series-level
  attribute, not a per-chapter one — a lone chapter's one-off tag doesn't
  leak into the series). `main.py`'s `comicinfo_items_for_manga()` builds the
  ordered chapter/volume list from `dims.json` (skips PDF/EPUB volumes —
  this is a CBZ/CBR + loose-folder convention only).
  `find_comicinfo_for_manga()` is the actual entry point both call sites use:
  a `ComicInfo.xml` sitting directly in the manga's own folder (next to the
  cover, sibling to the chapter/volume subfolders — one file for the whole
  series) takes priority over the per-chapter aggregation when present, since
  it's a deliberate series-level choice rather than something to intersect
  against individual chapters.
- **Automatic, at scan/rescan time** — `scan_library`'s existing
  post-processing pass (the one that sets the COMPLETE flag) also runs this
  aggregation per manga and **only fills currently-empty**
  `description`/`genres`/`tags` fields — it never overwrites a value that's
  already there, whether from a manual edit, a prior AniList/MangaDex fetch,
  or a prior run of this same pass. When it does introduce new tags/genres,
  `scan_library` returns a `comicinfo_changed` flag alongside the manga list
  so `run_scan` can rebuild `all_tags`/`all_genres` once the new `manga_data`
  is actually saved (rebuilding earlier reads stale on-disk data — this bit
  the first pass at the feature and had to be fixed).
- **Manual, via the existing Fetch Metadata popup** — `GET
  /api/manga/{lib}/{id}/local-metadata` returns the same aggregation shaped
  as a selectable candidate (`{description, genres, tags}`, matching what
  `metadata_fetch.resolve_field_value()` already knows how to read — plain
  string tags/genres, same as a MangaDex candidate), fetched once when the
  popup opens and shown as one more card ("Local file", badged distinctly
  from the AniList match-score badges) alongside the AniList search results
  in `manga_detail.html`. Picking it and choosing fields to import goes
  through the exact same `/apply-metadata` endpoint as any other candidate —
  no backend changes needed there. This is how "should the XML or the web
  win" gets decided: by which candidate card the user clicks, not a separate
  prompt. (`volume_detail.html` never had the Fetch Metadata popup at all —
  a pre-existing gap, not touched here — but the automatic scan-time fill
  still applies to volume-type manga regardless, since that logic doesn't
  depend on the popup.)

### 9. Integrity checking (corrupt archives / duplicate pages) — done

An idle-gated background pass that flags corrupt CBZ/CBR archives and exact
duplicate pages within a chapter/volume, surfaced to the admin in a new
Settings section — scoped to archive + loose only (same as ComicInfo.xml;
PDF/EPUB aren't checked).

- **`integrity.py`** — pure checking module. `check_archive()` reads every
  image entry once, which does double duty: zipfile/rarfile validate each
  entry's CRC as part of decompression, so a failed read *is* the corruption
  check (no separate `zipfile.testzip()` pass needed), and the same read
  gives the bytes to hash (sha1) for duplicate detection. `check_loose()`
  does the same for unarchived folders, except there's no CRC layer to lean
  on — the only way to know a raw image file is intact is to actually decode
  it (`PIL.Image.open().load()`). Duplicates are scoped to *within* one
  chapter/volume, not across a whole series — a title/copyright page reused
  every chapter would otherwise flag as a false positive.
- **Idle detection**: `last_activity_ts` (main.py) is updated on every
  session/token-authenticated request in the existing auth middleware —
  deliberately *not* updated by Basic Auth (OPDS client) traffic, so an
  automated reader polling in the background can't keep the check from ever
  running. `is_idle()` checks whether it's been quiet for 20+ minutes.
- **Cadence**: not tied to file changes (a silently degrading drive doesn't
  touch a file's mtime) — each chapter/volume gets a rolling `dims.json`
  timestamp (`integrity_checked_at`) and is re-checked every ~3 months
  regardless, oldest-checked-first, so bit rot gets caught even when nothing
  was ever edited.
- **`run_integrity_check_loop()`** wakes every 10 minutes, and only starts a
  batch when idle; between each item it re-checks idleness and bails
  cleanly the moment activity resumes, picking back up at the same point
  (oldest-first) on the next idle window — no separate pause/resume
  bookkeeping needed.
- **Storage**: `{data_path}/integrity_issues.json`, global and admin-facing
  (not per-user) — same pattern as `collections.json`. One entry per
  concrete problem (a corrupt chapter is one entry; two separate duplicate
  groups in the same chapter are two entries), each carrying the full trace
  (library → manga → chapter/volume → filenames) needed to display and act
  on it directly.
- **Admin API** (`/api/admin/integrity/*`): `GET issues` (list + count),
  `POST recheck` (specific issue ids, or all when omitted — re-runs the
  check against the item's *current* path, not the old filename, so a fully
  replaced chapter is checked fresh rather than trying to re-verify
  something that may no longer exist), `POST dismiss` (plain delete, no
  separate dismissed-flag state to manage).
- **Settings UI**: a new admin-only "Issues" section — its sidebar nav item
  turns bright red with a count badge when anything's unresolved, plus a
  matching small badge on the topbar Settings gear icon (`app.js`'s
  `loadIntegrityBadge()`) so it's visible from the main library page too,
  before Settings is even opened. **Note for future edits to this file**:
  `settings.html` is *not* wrapped in `{% raw %}` (unlike
  `collection_detail.html`/`collections_list.html`), so any `{{ }}`
  Jinja/Vue mustache expression beyond a bare variable — attribute access
  (`issue.name`), a function call (`libraryName(x)`), a ternary (`a ? b :
  c`) — crashes the page at render time (Jinja tries to parse it as its own
  syntax and either fails outright or raises on the undefined variable).
  This bit the first pass at this feature. The established convention in
  this specific file is `v-text="..."` (or `:class`, `:style`, etc.) for
  anything beyond a plain variable name, since Jinja never parses the
  *inside* of a quoted attribute value.

### 10. Small settings/admin batch (2026-07-07) — done

Five independent, unrelated changes done together in one pass:

- **Hide-BLE-scroller toggle** — per-user, defaults to **on** (hidden), in
  the Appearance tab's "Display" card next to the backdrop toggles
  (`hide_ble_scroller` in `{data_path}/{username}.json`, `POST
  /api/settings/ble-scroller`). Since most viewers of a shared server don't
  own the Scroller-HD hardware, the feature stays out of their way unless
  they explicitly opt in. `chapter_reader.html`'s entire BLE block (topbar
  icon, connect popup, auto-reconnect, the rAF velocity scroll loop) is
  gated behind `!kinshoHideBleScroller` — when hidden, nothing in that block
  runs at all, so this also fully suppresses the M4 auto-connect scan on
  reader open, not just the icon.
- **Per-library "show on my home page" switch** — self-service, any user
  (including admins), one checkbox per library in the Libraries settings
  section (both the admin's full library-editor cards and the read-only
  non-admin card view). Stored as `hidden_libraries` (list of library id
  strings) in the viewer's own `{username}.json`, applied as a filter in
  `manga_list()` on top of (not instead of) the existing admin-controlled
  `permissions.json` access filter. Deliberately a **display preference,
  not an access control** — hiding a tab this way doesn't revoke reachability
  via search/collections/a direct link, unlike the admin permission toggle
  in the Accounts section, which actually 404s. `POST
  /api/settings/library-visibility` — `{library_id, hidden}`.
- **SSIM-confirmed near-duplicate detection** — `integrity.py`'s duplicate-page
  check was exact-SHA1-only, which by construction can never have a false
  positive (identical bytes decode to identical pixels), so a plain SSIM
  check bolted on top of it would have been a no-op. Instead added a second,
  independent detection path: a cheap 8×8 average-hash (`_phash`) shortlists
  pages that aren't byte-identical but look roughly alike (a page re-saved at
  different quality/size by a different release, or two genuinely different
  pages that happen to share overall tone/brightness), then a real windowed
  SSIM score (`_ssim_score` — box-filter implementation via a numpy
  summed-area table, standing in for the usual Gaussian window so no scipy
  dependency was needed) confirms or rejects each candidate pair at an
  **80%** threshold. Only candidates that clear SSIM get grouped; the
  perceptual-hash pre-filter only decides what's worth the more expensive
  SSIM check, not what gets flagged. Exact SHA1 matches still short-circuit
  straight to a duplicate group (their SSIM is trivially 1.0, no point
  spending the compute). `duplicate_groups` entries changed shape from a
  plain filename list to `{"filenames": [...], "similarity": float}`;
  `record_integrity_result` in `main.py` uses `similarity` to phrase the
  issue detail differently for exact ("N identical pages") vs. near
  ("N near-duplicate pages (NN% similar)") matches. Added `numpy` as a new
  hard dependency (`requirements.txt`) — the only non-Pillow image-processing
  dependency in the project, needed for the box-filter math to be fast
  enough to run per-chapter in the existing idle-gated background pass.
- **Dominant-color masking for SSIM (2026-07-08 follow-up)** — reported
  false positives on pages that are 80%+ a single flat color (a solid-black
  transition panel, a mostly-white page with a small inset). Cause: a
  window that's flat in both images trivially scores ~1.0 SSIM regardless
  of what the rest of the page looks like, as long as both images are near
  the same level there — common, since white margins are near-universal in
  manga, so two genuinely different pages sharing a big white background
  could clear the 80% threshold on the background alone. Fixed in
  `_ssim_score` (`integrity.py`): before averaging the SSIM map, pixels
  that are within `DOMINANT_COLOR_TOLERANCE` (14 grayscale levels) of BOTH
  images' own single most-common intensity (`_dominant_color_mask`) are
  excluded — deliberately an intersection, not either image's background
  alone, since a flat region in only one image compared against real
  content in the other already scores low there and should keep counting
  against a match. If masking leaves less than `MIN_COMPARABLE_FRACTION`
  (5%) of the frame, there's not enough real content left to trust a score
  either way, so it returns 0.0 (not a match) rather than falling back to
  the whole bias-inflated image. Verified with synthetic test images: two
  different pages sharing an 85% white background scored ~0.018 after the
  fix (previously would have cleared 0.80 on the shared background alone);
  a true near-duplicate sharing the same background still scored ~0.997, so
  real detection power is unaffected.
- **"Permissions" section renamed to "Accounts"** — label-only rename in
  `settings.html`'s sidebar nav; the internal `activeSection` key is still
  the string `'permissions'` (no reason to touch every conditional over a
  cosmetic rename).
- **Admin can delete a user or change their role** — `DELETE
  /api/admin/users/{username}` (removes the account from `users.json`, all
  of their sessions, their `permissions.json` entry, and their per-user
  `{username}.json` data file — reading history/favourites/private
  collections all live there, so deleting the file is what actually removes
  those) and `POST /api/admin/users/{username}/role` (`{role: "user"|"admin"}`)
  in `auth.py`, both admin-gated. Guardrails on both: an admin can't act on
  their **own** account through either endpoint (delete or role-change —
  avoids self-lockout mid-request), and the last remaining admin account
  can't be deleted or demoted (avoids a server with zero admins and no way
  back in). UI lives in the renamed Accounts section: a role `<select>` and
  a delete button added directly to each user row's header, next to the
  existing expand-to-edit-permissions chevron.

### 11. Fix deleted/edited pages still showing broken after a Reload scan (2026-07-08)

Reported bug: deleting an image from inside a manga's archive on disk, then
running a full library Reload scan, still left a black/broken placeholder at
that page's position. Two independent bugs in `scan_library`/serving,
compounding each other:

- **case2/case3 rescan was gated on the wrong mtime.** Both branches skipped
  re-scanning a manga entirely if the *containing folder's* own mtime hadn't
  changed — but editing an existing archive's contents in place (e.g.
  deleting one page from inside a `.cbz`) only changes that **archive
  file's** own mtime, not its parent folder's (a folder's mtime only moves
  when entries are added/removed/renamed). So that whole class of edit was
  silently never picked up by any later scan, for as long as the archive's
  filename stayed the same. Fixed by also checking each archive/volume
  file's own mtime against what's stored per-chapter/volume before trusting
  the folder-level "unchanged" skip. case1 (a single archive *is* the whole
  manga) was already checking the archive's own mtime directly and didn't
  have this bug.
- **Stale in-memory caches were never invalidated on rescan.** `_archive_page_cache`,
  `_archive_image_list_cache`, `_open_archive_handles` (and the PDF/EPUB
  equivalents) are keyed only by file path with no mtime component, and are
  process-scoped — nothing ever cleared an entry just because `scan_library`
  noticed the underlying file changed and rebuilt `dims.json`. A long-lived
  open archive handle in particular could keep returning pre-edit content
  indefinitely. Added `_invalidate_stale_source_caches(path)`, called at
  every point `scan_library` detects and is about to rescan a changed
  archive/volume (case1, case2, case3) — closes the stale handle and evicts
  any cached image list/page bytes for that exact path before it's reopened
  fresh.
- **The same folder-mtime bug also existed for loose (unarchived) manga
  (2026-07-08 follow-up)** — `_register_loose_manga` gated re-scanning a
  whole manga on whether its own top-level folder's mtime had changed, then
  only checked individual chapter/volume *subfolder* mtimes after that gate
  passed. Deleting a page from inside an existing chapter subfolder bumps
  that **subfolder's** own mtime (removing a directory entry always does)
  but never the parent manga folder's — so the correct per-subfolder check
  further down never got reached. Verified live with a throwaway loose
  manga folder: deleting a page left the manga-folder mtime unchanged while
  the chapter subfolder's mtime did update, exactly as expected, and a
  Reload scan silently no-opped until this was fixed. Same fix shape as the
  archive case: also compare each subfolder's own mtime against what's
  stored per-chapter/volume before trusting the folder-level skip.
- **A third, separate bug on top of both of the above (2026-07-08 follow-up):**
  even with `dims.json` correctly rebuilt server-side, the reader still
  showed broken pages until a hard refresh, because `GET
  /api/manga/{library_id}/{manga_id}/dims` sent a flat `Cache-Control:
  private, max-age=120` — any browser that had fetched that manga's dims
  in the last two minutes (and in practice, often quite a bit longer, since
  browsers can hold a cached response well past `max-age` if the tab is
  never hard-reloaded) kept using the stale copy regardless of what the
  server now had. Replaced the flat time window with a validator: an ETag
  built from the manga's own `last_updated` timestamp (only changes when
  `scan_library` actually rebuilt this manga — a plain integrity Recheck
  alone never touches it, since Recheck was never meant to change the page
  list either). `Cache-Control: private, no-cache` still lets the browser
  cache the body, but forces a conditional request every time; a match
  returns a bodyless 304, a mismatch returns the fresh body — so every
  viewer gets exactly-current data on their very next normal page load, no
  manual refresh needed by anyone, and no arbitrary staleness window either
  way. Verified live: matching `If-None-Match` → 304, non-matching → fresh
  200 with a full body.

### 12. Fix backdrop not showing for covers whose filename needs URL-encoding (2026-07-08)

Reported bug: the manga detail page's backdrop image (`hero-bg`, the blurred
full-bleed background behind the cover) silently didn't render for some
manga, even though the small cover thumbnail displayed fine for the same
manga.

Root cause: `manga_detail.html` (and the same `hero-bg` pattern in
`volume_detail.html`/`collection_detail.html`) builds the backdrop's inline
style via a Vue template literal —
`` `background-image:url('${manga.cover_url_large}')` `` — with the URL
wrapped in **single quotes** for CSS. Every place `main.py` builds a
`/covers/{library_id}/{manga_name}/{filename}` URL ran `quote()` over the
`manga_name` path segment, but never over the `filename` segment — the
cover's actual filename, which comes straight from whatever the source
archive/loose folder named that page image, and can contain anything
(apostrophes, parentheses, `+`, non-ASCII, etc.), unlike the manga name
which the app itself controls the character set of less strictly. A cover
filename containing an unescaped `'` breaks the single-quoted CSS `url()`
outright — the whole `background-image` declaration fails to parse, so
nothing renders. The `<img :src="manga.cover_url">` thumbnail elsewhere on
the same page was unaffected: browsers percent-encode literal special
characters in an attribute value before firing the actual request, but that
same rescue doesn't happen for a raw string interpolated into inline CSS.

Fixed by wrapping the filename segment in `quote()` too, at every call site
in `main.py` that builds this URL shape (manga list/detail, OPDS, and the
collection/admin cover-picker endpoints — 7 sites in total). Verified the
round-trip: `quote()` percent-encodes `'`, spaces, parens, and `+`
(`I'll Retire After Saving the World (Official)+.jpg` → `I%27ll%20Retire...`),
and `GET /covers/{library_id}/{manga_name}/{filename}`'s `filename` path
parameter is decoded automatically by Starlette the same way the already-quoted
`manga_name` segment already was — no route or JS change needed, just closing
the gap in what was being quoted server-side.

### 13. Library page/chapter/volume totals in Settings (2026-07-08)

`GET /api/settings/page-counts` (`main.py`) — for every library the current
user can access (`auth.can_access_library`, same as everywhere else — admins
see all, regular users see what their permissions allow), sums pages,
chapters, and volumes across every visible manga's `dims.json`
(`auth.is_manga_blocked`/`blocked_tags` filtering applied per-manga, same as
list/detail endpoints), via the new `manga_content_counts(dims)` helper next
to `checkable_items_for_manga`. A manga has chapters or volumes, never both,
so exactly one of those two counts is nonzero per manga — pages are just
`len(pages)` summed across whichever bucket is populated. Returns per-library
`{library_id, library_name, total_pages, total_chapters, total_volumes}` plus
`grand_total_pages`/`grand_total_chapters`/`grand_total_volumes` across every
included library.

Computed live from `dims.json` on each request rather than cached at scan
time — deliberate, since nothing in `data.json`'s manga records tracks a
running page/chapter/volume total today, and this endpoint is only hit when
the Libraries settings section loads or a scan finishes, not a hot path. If
it ever becomes slow on a very large install, the fix would be caching this
per-manga at scan time rather than optimizing the request path itself.

`static/settings.js`'s `loadPageCounts()` fetches this on mount and again
after any library scan completes (`pollScanStatus`'s completion branch), into
`pageCounts` (keyed by library id) and `pageCountsGrand`. `settings.html`
shows a grand-total line above the Libraries cards and a per-library line on
each library card (both the admin editor cards and the non-admin read-only
cards) — built with `v-text`, not `{{ }}` mustaches, since this file isn't
wrapped in `{% raw %}` (see the note under Integrity checking above about why
that matters here).

### 14. Fix popups closing when a text-selection drag ends outside them (2026-07-08)

Reported bug: selecting text inside a popup (e.g. the Fetch Metadata popup on
manga detail) by clicking and dragging, then releasing the mouse past the
popup's border, closed the popup instead of completing the selection.

Root cause: every popup in the app closes on click-outside via
`@click.self="closeFn"` on the `.popup-overlay`/`.cover-picker-overlay` div —
`.self` only checks that the *click event's* target is the overlay itself.
But a native `click` event's target isn't simply "whatever was under the
pointer at mouseup" — per spec it resolves to the nearest common ancestor of
the mousedown and mouseup targets. Starting a drag-select on text inside the
popup and releasing outside its border makes that common ancestor the
overlay (the popup's parent), so `.self` matched and the popup closed
mid-selection, even though the interaction started *inside* the popup.

Fixed by no longer trusting the click event's target alone: each app now
tracks whether the *mousedown* also landed directly on the overlay
(`onOverlayMouseDown`), and only closes on click if both the mousedown and
the click resolved to the overlay itself (`onOverlayClick(e, closeFn)`) —
`@mousedown="onOverlayMouseDown" @click="onOverlayClick($event, closeFn)"`
replacing `@click.self="closeFn"`. Applied to all 18 occurrences of this
pattern across `manga_detail.html`, `volume_detail.html`,
`collection_detail.html`, `collections_list.html`
(`static/collections_list.js`) — every popup/cover-picker overlay in the
app, not just the one reported, since it was the identical bug everywhere
this close-on-outside-click convention was used.

### 15. Exclude solid-color pages from duplicate matching entirely (2026-07-09)

Follow-up to the dominant-color-masking fix above (#9's follow-up):
requested that pages which are completely one flat color (a title card, a
scene-transition blackout) never get flagged as a "duplicate" of another
page — the masking fix already stopped a mostly-flat page from *false*-matching
genuinely different content, but did nothing for the exact-SHA1-match path,
which doesn't look at content at all: two solid-color pages that happen to be
byte-identical (or reach the SSIM path and score high because there's a
nonzero page.get("pages") worth of matching background) would still get
flagged, even though "these two blank pages are similar" isn't useful
information — a manga having several such pages is expected content, not
corruption or an accidental repeat.

Fixed with a pre-filter, not more masking: `_is_solid_color()` (`integrity.py`)
reuses `_dominant_color_mask()` at the same downsized grayscale used for
SSIM, and flags a page where ≥`SOLID_COLOR_FRACTION` (98%) of pixels are
within `DOMINANT_COLOR_TOLERANCE` of its own dominant color. `_find_duplicate_groups()`
drops any such page from both `hashes` and `raw_data` before exact-SHA1
grouping even runs — so a solid page can no longer enter a duplicate group
at all, whether by exact byte match or by SSIM, rather than relying on
`_ssim_score`'s existing `MIN_COMPARABLE_FRACTION` abstain (which only ever
applied to the near-duplicate path, never the exact-hash one). Verified: two
byte-identical solid-white pages and two solid-white pages with different
JPEG compression both now produce zero duplicate groups, while a real
near-duplicate pair (same artwork, recompressed) still gets flagged
correctly (~0.99 similarity) — the filter only removes flat pages from
consideration, it doesn't touch real-content matching.

### 16. Admin's covers seed new users' defaults + lockable library-page backdrop (2026-07-10)

Two small, unrelated additions:

- **New users start with the creating admin's cover choices.** Per-manga
  cover overrides (`user_data["covers"]`, set via `POST
  /api/manga/{library_id}/{manga_id}/cover`) were always per-account and
  started empty — a brand-new user fell back to `manga.get("cover")` (the
  scan-time default) for every manga until they picked their own. Now
  `auth.route_admin_create_user` copies whichever admin is *creating* the
  account's own `covers` dict into the new user's `{username}.json` at
  creation time (one-time seed, not a live sync — the new user can still
  override any of them afterward, same as before). Deliberately keyed off
  the requesting admin rather than a fixed "main admin" concept, since
  the app has no notion of a single primary admin among possibly several
  admin accounts.
- **"Lock backdrop" toggle (Appearance → Display).** `lock_backdrop`
  (`{data_path}/{username}.json`, `POST /api/settings/backdrop` alongside
  the existing `backdrop_list`/`backdrop_detail` keys — same endpoint,
  just a third optional key). When on, `static/app.js`'s `loadMangas`
  (the function that recomputes the library page's blurred backdrop from
  `this.lastRead[0]`, the most recently read manga, every time it runs —
  on tab switch, on `visibilitychange`, after a scan) skips recomputing
  `bgLayerStyle` whenever one is already set, leaving whatever's currently
  displayed alone instead of swapping it for the new tab's/session's
  last-read manga. The very first computation (page load, `bgLayerStyle`
  still `null`) still runs normally even with the lock on — there has to
  be an initial backdrop before there's anything to lock onto.

---

## Security audit — auth.py & permissions (2026-07-02, all findings fixed 2026-07-03)

Audit of `auth.py` and the permission-enforcement paths in `main.py`. All 11 findings
below are now fixed. Kept as a historical record of what was wrong and why, since the
reasoning still explains why the current code is shaped the way it is.

### Critical — fixed

1. **Per-library permissions were not enforced on any data/content API (broken access
   control / IDOR).** The only place a user's `libraries` permission map was applied was
   the home-page tab list in `manga_list` — it just hid tabs. Every actual data endpoint
   ignored it, so any logged-in user could read a denied library by requesting its
   `library_id` directly.
   **Fix:** `auth.can_access_library(username, library_id)`, called at the top of every
   endpoint that takes a `library_id`. Denied → 404, not 403, so a denied library isn't
   distinguishable from a nonexistent one.

2. **Blocked-tag filtering was bypassable at the content level.** `blocked_tags` was
   applied on the list/search/detail endpoints, but the reader/content endpoints
   (chapters, dims, volumes, pages, thumbnails) took no `request` and did no tag check —
   a user blocked from a tag could still open and read a blocked manga directly by id.
   **Fix:** `auth.is_manga_blocked(username, tags)`, called the same way as
   `can_access_library` across every content endpoint, the cover-image route, bookmarks,
   reading history/progress, metadata apply, tag/genre/description writes, and the page
   routes. Same 404-not-403 rule.

3. **Username was never sanitized → path traversal / arbitrary JSON file overwrite.**
   Registration only checked length ≥ 3 and a 4-word reserved set — no character
   validation. Since `_user_data_file` builds `os.path.join(data_path, f"{username}.json")`,
   a username like `../../evil` wrote outside `data_path`, and `permissions` (not in the
   reserved set) collided with `permissions.json`.
   **Fix:** usernames now validated against `^[a-z0-9_-]{3,32}$` before ever becoming a
   path component, and the reserved set covers every fixed JSON filename under
   `data_path` (`data`, `bootstrap`, `sessions`, `users`, `permissions`).

### High — fixed

4. **Weak password hashing.** Single-round HMAC-SHA256 with one hardcoded global salt
   baked into source — fast to brute-force, and one precomputed table worked against
   every Kinsho install.
   **Fix:** PBKDF2-HMAC-SHA256, 600k iterations, random per-user salt, stored as
   `pbkdf2_sha256$<iterations>$<salt>$<hash>`. Accounts still on the old hash verify fine
   and are silently upgraded to the new format on next successful login — no forced reset
   except finding #5's default admin.

5. **Default `admin`/`admin` account was never forced to change.** Anyone reaching a
   fresh instance before the owner set a password got full admin, indefinitely.
   **Fix:** the bootstrapped admin account is flagged `must_change_password`; every page
   route redirects to `/settings` until it's cleared by a successful password change,
   with a banner there explaining why.

6. **Open self-registration.** `POST /api/auth/register` was public, and thanks to
   finding #1, any account it created had full read access to every library by default.
   **Fix:** removed. Accounts are admin-only now, via `POST /api/admin/users`
   (`require_admin`-gated) and a "Create User" form in Settings. The sign-up face was
   also removed from the login page's flip card.

7. **Cover images were served without authentication.** The `/covers` mount was a plain
   `StaticFiles` mount, outside the `/api/*` auth gate — readable by anyone with the URL,
   no login, regardless of library permission or blocked tags.
   **Fix:** replaced with an authenticated `GET /covers/{library_id}/{manga_name}/{filename}`
   route that applies `can_access_library` / `is_manga_blocked` plus a `realpath`
   containment check against path traversal, before serving the file.

### Medium / low — fixed

8. **Non-constant-time password comparison.** Login and change-password compared hashes
   with `!=` instead of `hmac.compare_digest`, leaking a timing side-channel.
   **Fix:** closed as part of the #4 rewrite — `_verify_password` uses
   `hmac.compare_digest` throughout.

9. **Password change didn't invalidate existing sessions.** A stolen session survived
   the exact password reset meant to kill it, and there was no "log out everywhere" path.
   **Fix:** `route_change_password` now deletes every other session for that user on a
   successful change (keeping only the session that made the request alive), and
   `POST /api/auth/logout-everywhere` ends all sessions including the current one, wired
   up as a button in Settings. (Also fixed in passing: `route_logout` only ever checked
   the session cookie, never the `X-Auth-Token` header, so the native app's logout was a
   silent no-op server-side.)

10. **`route_set_user_permissions` didn't validate the target username.** It wrote
    `perms_data[username] = perms` for any string, so a typo or crafted name could
    silently create a ghost entry or overwrite the `_default` template.
    **Fix:** 404s unless the username is a real account or the literal `_default`
    sentinel.

11. **Permissive CORS.** `allow_origins=["*"]` with `allow_methods=["*"]`. Mitigated by
    `allow_credentials=False`, but any origin could call the API with a stolen
    `X-Auth-Token` header (which isn't subject to the same cross-origin restriction as a
    cookie).
    **Fix:** `allow_origins` narrowed to the Capacitor Android app's actual WebView
    origins (`https://localhost`, `http://localhost`, `capacitor://localhost`) — the only
    legitimate cross-origin caller, since browser-served mode never needed CORS at all
    (same-origin relative fetches). `allow_methods`/`allow_headers` narrowed to what's
    actually used.

### Notes for future audits

- `can_access_library` / `is_manga_blocked` / `must_change_password` all live in
  `auth.py`, driven by `resolve_permissions`. Any new endpoint that takes a `library_id`
  or `manga_id` needs the first two called at the top, same as every existing one.
- The username charset (`^[a-z0-9_-]{3,32}$`) and reserved-word set are the only things
  standing between a username and becoming a filesystem path component — don't add a way
  to set a username that skips `route_admin_create_user`'s validation.
- If the native app ever changes its WebView scheme/config, `NATIVE_APP_ORIGINS` in
  `main.py` is the one place to update.

## Deployment — done (2026-07-05)

Running in Docker on a home Ubuntu server (a NAS also running Jellyfin/Sonarr/
Radarr/etc. via Portainer), two independent instances sharing one manga
library. The `Dockerfile` builds the app image; `docker-compose.yml` +
`.env` drive the actual deployment. This section is the real, tested
procedure — not just the goal — including the two gotchas that only show
up once you actually run it as a non-root user on a real server.

### Why `docker-compose.yml` looks the way it does

It's fully generic — `container_name`, `ports`, `volumes`, and `user` are all
`${VAR}` placeholders, not real values — because it's committed to the repo,
and the repo may go public later. Real values live in a local `.env` file
(gitignored, see `.env.example` for the template), so:
- editing your own paths/ports never conflicts with `git pull`, and never
  needs a commit,
- the shared file has zero personal info in it, safe for anyone to clone,
- a stranger cloning the repo gets a working single-instance example without
  needing to untangle someone else's two-instance setup.

Only one service is defined on purpose — a second instance is deliberately
*not* a second service in this shared file (see "Second instance" below),
to keep the public-facing default simple for someone who just wants one.

### One-time server setup

```bash
sudo apt update && sudo apt install -y docker.io git
sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # log out/in after this
```

**Private repo access** — the server needs its own SSH key added to GitHub
as a read-only deploy key (Settings → Deploy keys on the repo), since it's
a private repo:
```bash
ssh-keygen -t ed25519 -C "kinsho-server" -f ~/.ssh/kinsho_deploy -N ""
cat ~/.ssh/kinsho_deploy.pub   # paste this into GitHub's deploy key field
chmod 600 ~/.ssh/kinsho_deploy
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/kinsho_deploy
EOF
ssh -T git@github.com   # confirms auth works before cloning
```

### Instance 1

```bash
git clone git@github.com:kalako99/kinsho.git ~/kinsho
cd ~/kinsho
cp .env.example .env
nano .env   # KINSHO_UID/GID, KINSHO_PORT, KINSHO_DATA_PATH, KINSHO_MANGA_PATH

mkdir -p /srv/appdata/kinsho
cat > /srv/appdata/kinsho/bootstrap.json <<'EOF'
{"data_path": "/data"}
EOF
sudo chown -R 1000:1000 /srv/appdata/kinsho

docker compose up -d --build
```

Then open `http://<server-ip>:<port>` → log in `admin`/`admin` (forced
password change immediately) → Settings → set data path to `/data` and add
`/manga` as a library path (container-side paths, matching the right side of
the volume mounts — not the real host paths).

**Two gotchas hit during the real install, both permission-related, both
worth understanding rather than just copy-pasting past:**

1. **`user: "${KINSHO_UID}:${KINSHO_GID}"` in the compose file** makes the
   container run as a normal user instead of root, so files it creates on
   the NAS aren't root-owned (which would otherwise require `sudo` for every
   later file operation outside Docker). But that means the *host* folder
   also has to already be owned by that same UID/GID — `mkdir` as root (or
   via `sudo`) leaves it root-owned, and the container then can't write to
   it at all. Symptom: `PermissionError: [Errno 13] Permission denied` in
   `docker logs`. Fix is the `chown -R` above — do it *before* first start.

2. **`bootstrap.json` needs to exist before the container's first boot, and
   in a very specific place.** `auth._get_data_path()` looks for it via a
   bare relative path (`"bootstrap.json"`, not an absolute one), which
   resolves against the process's working directory — `/app` inside the
   container. On a truly fresh install nothing has ever pointed `data_path`
   anywhere yet, so `auth._save_users()` falls back to writing `users.json`
   as a relative path too — into `/app`, which is baked into the image
   (root-owned, and *not* a volume — anything written there vanishes on the
   next rebuild). Result: same permission error as above, PLUS, even if it
   were writable, silently losing every account on the next update. Fix:
   pre-create `bootstrap.json` on the host pointing at `/data` (the volume
   that *does* persist), and bind-mount that one file directly to
   `/app/bootstrap.json` — already wired into `docker-compose.yml`'s
   volumes list, just needs the host file to exist first.

### Second instance

Deliberately *not* a second service in the shared `docker-compose.yml` (see
above) — set up as a plain second container reusing the image Compose
already built, so there's no separate build step:

```bash
mkdir -p /srv/appdata/kinsho2
cat > /srv/appdata/kinsho2/bootstrap.json <<'EOF'
{"data_path": "/data"}
EOF
sudo chown -R 1000:1000 /srv/appdata/kinsho2

docker run -d \
  --name kinsho2 \
  --restart unless-stopped \
  --user 1000:1000 \
  -p 8099:8000 \
  -v /srv/appdata/kinsho2:/data \
  -v /mnt/wdred/Media/KINSHO:/manga \
  -v /srv/appdata/kinsho2/bootstrap.json:/app/bootstrap.json \
  kinsho-kinsho
```

(`kinsho-kinsho` is Compose's auto-generated image tag —
`<project-folder-name>-<service-name>`.) Same two gotchas apply here too,
same fixes.

Since this one isn't Compose-managed, updating it later means recreating
it manually (`docker stop kinsho2 && docker rm kinsho2`, then the `docker
run` above again against the freshly rebuilt image) rather than a single
`docker compose up -d --build` — a deliberate tradeoff for keeping the
public repo's compose file simple. Portainer (already used for the other
containers on this server) can manage instance 1 as a Git-backed stack for
one-click/auto updates; doing the same for instance 2 was deferred until
after the repo goes public, since the setup would need to change at that
point anyway (Portainer needs its own separate Git credential, entered
directly in its UI, independent of the SSH deploy key above).

### Updating either instance later

```bash
cd ~/kinsho
git pull origin main
docker build -t kinsho-kinsho .   # rebuild the shared image once
docker compose up -d              # recreates instance 1 from the fresh image
docker stop kinsho2 && docker rm kinsho2
docker run -d --name kinsho2 --restart unless-stopped --user 1000:1000 \
  -p 8099:8000 -v /srv/appdata/kinsho2:/data -v /mnt/wdred/Media/KINSHO:/manga \
  -v /srv/appdata/kinsho2/bootstrap.json:/app/bootstrap.json kinsho-kinsho
```
The port/paths never affect whether an update lands — rebuilding the image
is what pulls in new code; recreating each container is what makes a
*running* one actually use that new image. Nothing under `/data` or
`/manga` is ever touched by any of this, since both live outside the image
entirely.

## BLE hardware scroller — feature complete, scroll-feel tuning in progress

Goal: physical scroll input (a small BLE peripheral) for high-definition
scrolling inside `templates/chapter_reader.html` on the Android app
specifically — turning a page/chapter's continuous image strip with a
dedicated slider instead of a touchscreen swipe.

**Current status (as of the 551279e commit): in the scroll-feel testing
phase.** Everything structural is done and confirmed working on real
hardware — pairing/connect UI (M3), auto-reconnect across chapters and
across manga switches without disconnecting (M4, confirmed live) — so
what's left is purely tuning how the motion itself feels, not plumbing.
Four real bugs have been found and fixed so far from live testing feedback
(not just guessed at): the low-speed whole-pixel stutter, a "jumps back
periodically" bug caused by the conveyor-belt virtualization's
`recenterConveyor()` fighting with kinsho's own tracked scroll position
mid-continuous-scroll (see "Jump back" fix entry below for the full
mechanism), a `KINSHO_MAX_ACCEL` unit-scaling bug (raw 1/120-notch units vs.
plain notches/sec²) that made the jump-back fix's replacement
constant-acceleration ramp run ~120x slower than intended — full-range ramp
took ~50s instead of ~0.2s, which live-tested as sluggish/unresponsive,
speed "accumulating" without settling, and scrolling continuing long after
the slider returned to center (fixed 2026-07-08 by scaling
`KINSHO_MAX_ACCEL` by `KINSHO_UNITS_PER_NOTCH` and retuning the default to
125 — see the tuning table below) — and, once that made the scroller
responsive enough to actually feel the next layer of the problem, an
unthrottled-progress-bar bug: `syncScrollPosition()` ran
`updateProgressBar()`/`scheduleProgressSave()` (several full-manifest linear
scans plus DOM queries each) on every native `scroll` event, up to ~60/sec
during continuous kinsho-driven scrolling, which was expensive enough to
occasionally blow the frame budget — a dropped/delayed frame produces a
catch-up jump whose visible size scales with current scroll speed, so it
live-tested as "smooth at low speed, increasingly choppy toward high speed,
plateauing at a max" even while holding the potentiometer at a constant
position (no acceleration/ramping involved at all at that point — this is
why it looked like a curve-shape problem but wasn't one). Fixed 2026-07-08
by throttling that bookkeeping to ~12Hz (`SCROLL_BOOKKEEPING_THROTTLE_MS`,
`chapter_reader.html`'s `syncScrollPosition`) with a trailing call so the
final state after scrolling stops is never more than one throttle window
stale. **Live-tested (2026-07-08): improved, not yet perfect** — confirmed
the right direction, so the same class of fix continued: `updateProgressBar()`
was still doing its DOM/manifest-scan work unconditionally on every throttled
tick even though the progress bar/chapter label/page slider it updates are
only ever visible for ~3s after a tap (`barVisible`, `hideAll()`/`showAll()`)
— most of a continuous BLE-scroller read happens with that UI hidden. Worse,
`rebuildFragProgress()` (removes/recreates one DOM element + 3 listeners per
page in the newly-entered chapter) fired on every chapter-boundary crossing
regardless of visibility, and crossing chapter boundaries gets more frequent
the faster you scroll — a second, independent way the per-tick cost scaled
with speed. Fixed by gating all of `updateProgressBar()` behind
`if (!barVisible) return`, with `showAll()` forcing one full call on reveal
so nothing is stale when the UI reappears (`chapter_reader.html`, same day).
**Pushed but not yet live-tested** — verify next on the tablet + Scroller-HD
hardware after redeploying (no app rebuild needed). If scrolling is still
choppy at high speed after this, the likely next suspect is the underlying
image decode/paint cost of the newly-visible page content itself (more
pixels cross into view per second at higher scroll speed, independent of any
JS bookkeeping), not the acceleration ramp — a native Android touch-event
synthesis approach (dispatching real `MotionEvent`s into the WebView so
Chromium's own compositor-driven scroll/momentum pipeline drives it, rather
than JS writing `scrollTop` every frame) was discussed as the option for if
JS-side optimization can't get there, but deliberately deferred — it's a
much bigger change living in the `kinsho-android` repo, worth doing only if
this direction turns out not to be enough.

- **`arduino/`** at the repo root holds the hardware sketches — gitignored
  (see `.gitignore`), since it's hardware source, not app source. Each
  device is its own sketch folder (Arduino IDE requires the folder name to
  match the `.ino` filename).
- **Scroller-HD (done, working)**: `arduino/scroller_hd/scroller_hd.ino` —
  XIAO nRF52840 + linear slide potentiometer (VCC→3V3, GND, SIG→A0), button
  on D2 (quick press = reverse direction, hold 1s = NO_CURSOR_MODE, triple
  press = clear bonds + pairing mode), BLE HID mouse with true
  high-definition scrolling on Windows. Position-based "throttle" model
  (dead zone 1.3–1.7V, cruise curve across most of the travel, steep boost
  zone in the last 0.5V at each end), EMA + position-hold-deadband
  filtering so a resting slider commands an exactly constant rate.
  `arduino/scroller_hd_usbtest/` is the same pipeline over USB (TinyUSB),
  kept as the known-good A/B reference for transport comparisons.
- **Hard-won HID facts baked into that sketch** (each cost a debugging
  round): Windows only negotiates the Resolution Multiplier if the
  descriptor matches Microsoft's Wheel.docx sample shape — a **2-bit**
  feature field (not 1-bit) wrapped in a Collection (Logical) together
  with the wheel Input; report IDs must be declared in the report map
  because the Adafruit/Seeed library's BLE Report Reference descriptors
  claim ID 1 unconditionally; Windows writes the feature **once at pairing
  install, not on reconnect**, so the negotiated state is persisted to
  flash and cleared only on bond-clear; Windows caches a bonded device's
  HID descriptor, so every descriptor change requires remove-device +
  re-pair before anything works.
- **Android answer (the previously open question): the HID route is a dead
  end on Android** — its Bluetooth HID host never negotiates the
  Resolution Multiplier, and a system-paired mouse adds a pointer overlay.
  The plan instead: a custom GATT rate-stream characteristic in the
  firmware, read directly by the app (no system pairing), driving a
  velocity-based rAF scroll loop in `chapter_reader.html`. **The full plan
  and milestone history live in the Android repo's CLAUDE.md** —
  https://github.com/kalako99/kinsho-android (private) — since the app
  owns the BLE plumbing.
- **Reader-side implementation (M3, this repo)**: the connect UI (topbar
  bluetooth icon, feature-detected on `window.KinshoBLE`) and the
  velocity-driven rAF scroll loop live in `templates/chapter_reader.html`,
  gated to long-strip mode with a 1s silence watchdog against a dropped
  link leaving the page drifting.
- **Auto-reconnect (M4, this repo) — CONFIRMED WORKING (2026-07-08)**: on
  every reader page load, `chapter_reader.html` checks
  `window.KinshoBLE.isConnected()` first — the native connection is owned by
  the Android app's `MainActivity`, not the page's JS context, so it already
  survives the full-page reload each chapter navigation does, and a true
  result just repaints the status dot with no rescan. Otherwise it silently
  reconnects if a `localStorage` flag (`kinshoAutoConnectScroller`) is set —
  set on any successful connection, cleared only by an explicit Disconnect
  tap (not by an unexpected drop), same mental model as a Bluetooth
  headphone reconnecting once paired. The matching Java-side scan timeout
  (10s, was previously unbounded) that makes a silent background
  auto-connect attempt safe lives in the Android repo's `KinshoBleBridge.java`.
  An earlier real-device test found this completely broken even after a full
  redeploy of both sides; temporary `console.log`/`Log.d` traces (since
  removed) confirmed the JS/Java logic itself was correct end to end — flag
  set correctly on connect, `isConnected()` read correctly on the next page,
  `connect()` invoked correctly when appropriate. The most likely explanation
  for that earlier failure was a stale WebView-cached copy of
  `chapter_reader.html` from before this logic existed (same class of issue
  as the `/static` caching bug documented above, just for a server-served
  page instead of a static asset) — a subsequent full APK reinstall is what
  actually resolved it. Verified live end to end: connects manually, survives
  navigating to a different manga's reader without disconnecting, and stays
  connected across the whole session as designed.
- **Connection priority fix (2026-07-08, kinsho-android repo)**: Android
  defaults a fresh GATT connection to `CONNECTION_PRIORITY_BALANCED`, which
  negotiates its own connection interval regardless of what the peripheral
  requests — this is almost certainly why the KINSHO rate stream was
  observed arriving at ~100ms instead of the firmware's intended 50ms (20Hz)
  during M1 testing. `KinshoBleBridge.java`'s `onConnectionStateChange` now
  calls `gatt.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH)`
  right when the link comes up, before service discovery. Needs an app
  rebuild + reinstall to take effect; there's no equivalent knob on the
  firmware side (the peripheral already requests the fastest interval it
  can via `setConnInterval`, but the *central* — the phone — has final say).
- **Low-speed stutter fix (2026-07-08, this repo)**: `chapter_reader.html`'s
  scroll loop used to accumulate fractional pixels and only write whole-pixel
  jumps to `reader.scrollTop` — at low `kinshoRate` values this could take
  ~2 seconds to accumulate a single displayable pixel, reading as the page
  repeatedly stopping and lurching forward instead of gliding. It now tracks
  a continuous float (`kinshoScrollPos`) and assigns it to `scrollTop`
  directly every frame, re-syncing from the DOM's actual position whenever
  scrolling resumes after an idle spell (dead zone, mode switch) so it never
  drifts from wherever touch-scrolling or the conveyor-belt recenter logic
  left the page.
- **"Jump back" fix + constant-acceleration ramp (2026-07-08 follow-up)**:
  reported after the stutter fix above — still choppy at constant speed and
  during deceleration, worse while continuously re-adjusting the slider, and
  periodically jumping backward. Two compounding causes:
  - The stutter fix's idle-spell resync (`if (kinshoScrollPos === null)
    kinshoScrollPos = reader.scrollTop`) only ever re-read the DOM's actual
    position when transitioning from idle back to active — never while
    *continuously* scrolling. But `recenterConveyor()` (see
    `templates/chapter_reader.html`'s conveyor-belt virtualization) can
    reposition the materialized window and adjust `reader.scrollTop`
    directly at any time, including mid-continuous-kinsho-scroll (its
    settled-check can spuriously trigger off two consecutive frames
    rounding to the same integer pixel, which happens easily at low
    speed). Kinsho's own tracked position had no way to know that had
    happened, so the very next frame it wrote its own stale value straight
    back over the recenter's adjustment — a real, visible jump backward
    (or forward). Fixed by checking every frame (not just on idle→active)
    whether `reader.scrollTop` has drifted from the tracked position by
    more than a few pixels (ordinary float→integer rounding from kinsho's
    own last write is at most ~1px) and, if so, adopting the DOM's actual
    position as the new baseline instead of fighting it.
  - Separately, replaced the exponential smoothing between received rate
    values with a constant-acceleration ramp (`KINSHO_MAX_ACCEL`) — the
    exponential's percentage-of-remaining-gap approach means a big jump in
    the raw rate (e.g. continuously re-targeting a new speed while already
    moving) produces a correspondingly large *instant* change in
    acceleration each time, a series of kinks rather than one smooth curve.
    A fixed rate of change per second regardless of jump size gives one
    predictable ramp through repeated re-targeting.

### Tuning the scroller's feel

Three separate places to adjust, depending on what feels wrong. None of
these require touching the GATT plumbing above — they only shape the signal
that flows through it.

**Firmware (`arduino/scroller_hd/scroller_hd.ino`) — reflash the device to
take effect:**

| Constant | Default | Controls | Raise it if... | Lower it if... |
|---|---|---|---|---|
| `DEADZONE_LOW_V` / `DEADZONE_HIGH_V` | 1.3 / 1.7 | Voltage band around center where the slider commands zero scroll | the resting slider still drifts | the dead zone feels too wide/insensitive near center |
| `VOLTAGE_SMOOTHING` | 0.6 | EMA factor on the raw ADC reading (0 disables) | speed still flutters when the slider is held still | movement feels laggy/delayed (this is ~25ms of lag at 0.6, 100Hz sampling) |
| `VOLTAGE_HOLD_DEADBAND_V` | 0.05 | Band the *held* voltage ignores before updating — keeps a steady slider at an exactly constant rate | speed still wobbles with residual ADC noise while held | deliberate slow slider movement feels sticky/steppy |
| `CRUISE_MAX_UNITS_PER_SEC` | 5.0 | Top scroll speed in the gentle "cruise" zone (dead-zone edge to boost zone) | normal reading-pace scrolling feels too slow even near full displacement | cruise-zone scrolling feels too fast for casual reading |
| `BOOST_MAX_UNITS_PER_SEC` | 25.0 | Top scroll speed in the "boost" zone at the physical ends (fast repositioning) | boost doesn't feel fast enough for jumping across a chapter | boost overshoots / flies past where you meant to stop |
| `BOOST_ZONE_V` | 0.5 | How many volts before each physical end count as "boost" rather than "cruise" | you want boost to kick in earlier (a bigger chunk of travel) | boost kicks in too early, cruise zone feels too short |
| `DISPLACEMENT_CURVE` | 3.0 | Cruise-zone response curve (`>1.0` = gentle near dead zone, ramps up toward the boost zone) | fine control near the dead zone feels twitchy | the cruise zone feels unresponsive/flat until pushed far |
| `BOOST_CURVE` | 2.0 | Boost-zone response curve, same idea as above but for the boost segment | boost ramps up too abruptly right at the zone's start | boost doesn't ramp up quickly enough near the physical end |
| `KINSHO_RATE_NOTIFY_INTERVAL_MS` | 50 (20Hz) | How often the Android GATT stream sends a rate update (independent of the HID path's adaptive interval) | — (this is already the ceiling; the Android connection-priority fix above is what actually determines whether updates arrive this often) | notifications feel like they're arriving faster than needed (unlikely to matter — bandwidth here is trivial) |

**Reader JS (`templates/chapter_reader.html`) — just reload the page to take
effect, no rebuild:**

| Constant | Default | Controls |
|---|---|---|
| `KINSHO_PX_PER_NOTCH` | 100 | Overall scroll sensitivity — pixels of on-screen movement per firmware "notch". Raise for faster scrolling at the same slider position, lower for slower. |
| `KINSHO_UNITS_PER_NOTCH` | 120 | Must match `RESOLUTION_MULTIPLIER` in the firmware — only change this if that constant changes too. |
| `KINSHO_MAX_ACCEL` | 125 (notches/sec²) | How fast the rendered scroll speed can ramp up/down toward each newly received rate — added 2026-07-08 because BLE notifications don't always arrive on a perfectly even ~50ms beat (Android's main thread can be busy laying out scrolled-in images right when one needs delivering), and jumping straight to each new value read as choppy. A **constant** acceleration cap (not a percentage-of-gap/exponential chase) so continuously re-targeting a new speed — e.g. the user still actively adjusting the slider — still produces one smooth curve rather than a series of kinks. Applied against `kinshoRate`/`kinshoDisplayRate` after multiplying by `KINSHO_UNITS_PER_NOTCH`, since those are in raw 1/120-notch units, not plain notches/sec — the original ship (60, unscaled) was a **bug**, not a conservative tuning choice: it made the real ramp 120x slower than the label implied (~50s to reach boost-zone max instead of ~0.2s), which is what read as sluggish/unresponsive and "keeps drifting after returning to center" in the first live test of this feature (2026-07-08 follow-up, fixed same day). 125 targets a ~0.2s full-range ramp (cruise max in ~40ms). Raise if scrolling still feels sluggish to reach the target speed; lower (smoother, but slower to reach a newly commanded speed) if it feels jerky on big/fast speed changes. |

**Android (`kinsho-android` repo, `KinshoBleBridge.java`) — rebuild + reinstall
to take effect:**

The connection-priority fix above is the one Android-side knob that affects
feel; there's no adjustable constant there, just the fixed
`CONNECTION_PRIORITY_HIGH` request.
