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
  just extra optional keys). When on, the library page's blurred backdrop
  stops following `this.lastRead[0]` (the most recently read manga) and
  stays on whichever cover was showing when the lock was captured.
  **First shipped as in-memory-only (skip recomputing `bgLayerStyle` in
  `static/app.js`'s `loadMangas` whenever one was already set) — broken by
  design, not just a bug**: `bgLayerStyle` is plain Vue component state, and
  the library page does a full reload (new page load, fresh Vue instance,
  `bgLayerStyle` back to `null`) every time you navigate to a manga to read
  it and then back — exactly the moment a new "most recently read" manga
  exists, so the lock defeated itself on almost every use. Fixed same day
  by persisting the actual locked-in cover URL server-side
  (`locked_backdrop_url` in `{username}.json`, same endpoint) instead of
  relying on component memory: the first time `loadMangas` runs with the
  lock on and no `locked_backdrop_url` saved yet, it computes the backdrop
  normally from `this.lastRead[0]` same as always, displays it, and POSTs
  that cover URL back to `/api/settings/backdrop` to persist it. Every
  subsequent `loadMangas` call — including after a full page reload —
  checks `locked_backdrop_url` first and, if set, uses that instead of
  recomputing from last-read at all. Unchecking the toggle clears
  `locked_backdrop_url` server-side (handled in the same `save_backdrop`
  handler in `main.py`), so re-enabling the lock later captures a fresh
  "current" backdrop rather than reusing a stale one.

### 17. "Recheck All" not clearing issues after a drive dropout + Dismiss All (2026-07-11)

Reported after a network-drive disconnect/reconnect had flagged a large batch
of integrity issues: clicking **Recheck** on each issue individually cleared
it, but **Recheck All** cleared nothing.

Root cause: `recheck_integrity_issues_endpoint` (`main.py`) looped over every
targeted issue with no per-item error handling — a single item throwing (a
manga drive still flaky partway through reconnecting, mid-batch, is exactly
the kind of transient failure a large bulk pass is more likely to hit than
any one isolated click) took down the **entire** request with an unhandled
500. The single-issue Recheck path never hit this because each click is its
own isolated request — one bad item there only fails that one request, not a
batch of a hundred others. On the frontend, `static/settings.js`'s
`recheckAllIssues()`/`recheckIssue()` both do `await res.json()` and only
update `this.integrityIssues` from a successful response; a 500 (HTML error
page, not JSON) throws inside that `await`, lands in the `catch` block, and
the catch handler never touches `this.integrityIssues` — so the list stayed
exactly as it was, with no visible error either, reading as "nothing
happened" even though some items earlier in the loop may have actually
cleared server-side already (`record_integrity_result` saves to disk
per-item as it goes, independent of the crashed response). Fixed by wrapping
each item's processing in its own `try/except` inside the loop — one item's
failure is now logged and skipped, and every other item in the batch still
gets checked and reflected in the response, matching "Recheck All" to
actually behave like pressing Recheck on every item.

Also added, as requested alongside this: a **Dismiss All** button next to
Recheck All in the Issues section, `POST /api/admin/integrity/dismiss-all`
(clears `integrity_issues.json` entirely — no per-item loop needed, unlike
Recheck), gated behind the same `confirm()` pattern already used for the
other destructive admin actions (delete user, log out everywhere).

**Follow-up, same day**: the try/except fix above stopped the crash, but
against a genuinely large batch (the same network-drive-dropout scenario
that originally flagged everything) the single request driving the whole
loop server-side was still reported as "slow and doesn't seem to be doing
anything after 20 minutes" — one HTTP request silently doing all the work
with zero progress feedback until it finally finishes (or the caller gives
up waiting). Reworked `recheckAllIssues()` (`static/settings.js`) to drop
the `{}` (recheck-everything) call entirely and instead walk
`this.integrityIssues` client-side, issuing the exact same
`{issue_ids: [id]}` request the single-row Recheck button already uses, one
at a time, in sequence — so it's now *literally* "press Recheck on every
row" as requested, not a different code path that happens to cover the same
issues. This gives a live `Rechecking N of M…` status between requests
instead of one opaque wait, and since each item is now its own independent
request, a stuck/slow one only delays the items after it rather than
blocking a single all-or-nothing response. Each snapshot id is checked
against the live `this.integrityIssues` before firing its request, since
one item's recheck can clear more than one row (multiple issues sharing a
chapter/volume) and an already-cleared id later in the same walk should be
skipped rather than re-checked pointlessly. The backend's
`recheck_integrity_issues_endpoint` still accepts an empty `issue_ids` for
a true whole-batch call (kept for any non-UI/API caller that wants it,
and it's still hardened by the per-item try/except from the same day) —
only the Recheck All *button* stopped using that path.

**Second follow-up, same day**: sequential one-at-a-time walking fixed the
silent-hang problem but was reported as genuinely slower overall than just
clicking each row's Recheck button by hand — expected, since a fully
sequential walk can't overlap any of the per-item I/O wait (opening/reading
a chapter's whole archive, especially over a network drive) the way a human
clicking at their own pace effectively also doesn't, but a *concurrent* walk
can. `recheckAllIssues()` now runs a small fixed-size pool (`CONCURRENCY =
4`) of workers pulling from a shared cursor over the same id list, instead
of one worker doing them one by one — each worker still fires the identical
single-item `{issue_ids: [id]}` request, just several in flight at once.

Testing this also surfaced a real, unrelated bug it happened to make more
visible: **the single-row Recheck button and the bulk "Recheck All" button
shared one `integrityRechecking` flag**, so clicking Recheck on a single row
also flipped the "Recheck All" button's own label to "Rechecking…" — making
a single click look like it had kicked off a check of the *entire* list,
which read as "even a single recheck got much slower" (it hadn't; the
button just lied about what was running). Fixed by splitting it into two
independent flags: `integrityRechecking` (bulk only) and a new
`recheckingIssueId` (which single row, if any, is mid-request) — each
button now only reflects and disables for its own operation, and the
per-row button also gets its own "Rechecking…" label while that specific
row is in flight.

Running genuinely concurrent rechecks also introduced a real race that
didn't exist under strict sequential processing: `record_integrity_result`
and the endpoint's own clear-on-gone-item paths each did an unguarded
load-mutate-save of `integrity_issues.json`, so two of them completing at
the same moment could each save a version missing the other's update,
silently losing a clear or an append. Fixed with `_integrity_issues_lock`
(`main.py`, a `threading.RLock` — re-entrant so `record_integrity_result`
can hold it across its whole load-mutate-save while its own internal
`load_integrity_issues()`/`save_integrity_issues()` calls re-acquire it from
the same thread without deadlocking) wrapping every read *and* write of
that file, not just writes — `save_integrity_issues()` truncates the file
before writing, so an unguarded concurrent read could otherwise observe a
half-written, unparseable file, not just a stale one. Added
`_clear_and_save_issue()` as the atomic equivalent of the old
load-mutate-save-inline pattern for the two "item no longer exists" branches
in the recheck endpoint, and the same lock now also guards `dismiss`/
`dismiss-all`.

Sequential vs. concurrent, and the shared-flag bug, were reported together
and initially looked like the same complaint ("recheck all is slow/broken")
— worth remembering they were actually three separate, independently-fixed
issues (throughput, a mislabeled button, and a write race the throughput
fix newly exposed), not one root cause.

**Third follow-up, same day**: reported again as still much slower than
before for the exact error class the original dropout produced en masse —
`Failed to list folder contents: [Errno 2] No such file or directory: ...`
(a chapter/volume folder that's flat-out gone) — 10-15s per row where it
used to be instant, meaning "Recheck All" across a whole dropped-drive's
worth of these would take *hours*, not the seconds it should. Root cause
this time wasn't concurrency or the UI — it's that **Recheck can never
actually clear this class of issue in the first place**:
`record_integrity_result` unconditionally re-appends a fresh "corrupt" entry
whenever `result["corrupt"]` is still truthy, which it always will be for a
folder that's genuinely gone — so clicking Recheck on real, permanent
content-deletion just replaces the issue with an identical copy of itself
(new id, new timestamp), forever. It only *looked* like it used to clear
instantly because the endpoint has a separate, genuinely-fast path: if
`checkable_items_for_manga(dims)` no longer lists the chapter/volume at all
(dims.json already updated by an earlier rescan to drop it), Recheck clears
it with zero filesystem access. Whether a given row hits that fast path or
the slow real-check path purely depends on whether a library rescan has
happened *since* the content actually disappeared — for a large batch of
issues from one big dropout, most of them hadn't been through a rescan yet,
so nearly every row was hitting the slow path that, on top of it all, was
never even going to succeed at clearing anything.

Fixed at the source instead of trying to make the slow path faster:
`prune_stale_integrity_issues()` (`main.py`), called from `run_scan()` right
after a library's fresh manga list is saved. A scan already reads the whole
library's current, authoritative state — this reuses that instead of
needing an admin to click Recheck, one slow-and-futile row at a time, on
content a scan already knows is gone. It walks every open issue for that
library, and for each one checks whether `checkable_items_for_manga()`
against the freshly-scanned `dims.json` still lists that exact
chapter/volume; if not (manga gone entirely, or just that one chapter),
the issue is dropped, in one single load-mutate-save of
`integrity_issues.json` for the *whole* library instead of one HTTP
round-trip per row. Real corruption on content that still exists is
untouched — only issues for content the fresh scan no longer knows about
get cleared, and only from a scan, never implicitly from a Recheck click.
Guarded against the exact scenario that caused all this in the first place:
if none of the library's root path(s) are reachable at scan time (`os.path.exists`),
pruning is skipped entirely for that scan — a rescan attempted while the
drive is *still* disconnected must never be allowed to look identical to
"every manga in this library got deleted" and wipe out every real issue
instead of just the genuinely-gone ones.

Also dropped `indent=2` from `save_integrity_issues()` — after a
whole-library dropout this file can hold thousands of entries and gets
read/written on every single recheck; pretty-printing that repeatedly is
pure wasted CPU for a file nobody hand-edits, unlike the smaller
config-shaped JSON files elsewhere that keep `indent=2` for that reason.

The practical upshot: after this fix, hitting Reload on a library is what
actually clears the backlog from a drive dropout (in one pass, in seconds)
— Recheck/Recheck All are for confirming whether content that's *still
there* is actually fixed, not for content that's simply gone.

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
    (**update, 2026-07-19**: the conveyor-belt fixes below eliminated the
    direct `reader.scrollTop` writes this bullet describes — `recenterConveyor`'s
    shift path no longer touches `scrollTop` at all, only the two spacer
    elements' heights — so this exact race is now structurally impossible
    for the shift path; the `>3px` tolerance check described here is left
    as-is since it's still a correct, harmless safety net for the
    `_rebuildConveyorWindow` "big jump" path, which still does write
    `scrollTop` directly)
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

## Long-strip reader: virtualized-scroll bug fixes (2026-07-19)

Six bugs reported by the user from real-device reading sessions (confirmed
reproducing in a desktop browser too, not Android/WebView-specific): a hard
scroll "floor" after roughly 40 pages that acted like end-of-chapter
regardless of the chapter's real length; an occasional black/unloaded page
mid-scroll while later pages were already loaded; the reading position
sometimes resetting to the top of the current image while scrolling with a
mouse; the segmented progress bar under-counting at the true last page (e.g.
35/40); the same bar occasionally flashing/teleporting between page 1 and
another page while scrolling normally; and not being able to drag-scroll via
the black letterbox bars that appear when zoomed out in long-strip mode.

Root-caused via a full trace of `chapter_reader.html`'s conveyor-belt
virtualization (`SLOT_COUNT = 80` materialized DOM slots out of a much
longer full-chapter `manifest[]`) plus a comparative study of how
SumatraPDF (`DisplayModel`/`RenderCache`) and PDF.js (`pdf_viewer.js`/
`pdf_rendering_queue.js`) solve the same "very long virtualized scroll"
problem — both separate the *scrollable range* (computed once from full
document geometry, never bounded by what's currently rendered) from the
*rendered range* (a much smaller, continuously-updated window). This
codebase had conflated the two: `#reader`'s native `scrollHeight` was simply
the sum of whatever ~80 slots happened to be materialized, and the window
only grew when scrolling settled (`scrollend` or a settle-poll fallback) —
never during one long continuous gesture. Decided against a PDF.js-style
full rewrite of the render/visibility pipeline (a multi-week undertaking for
a framework-free, test-free, ~4400-line solo-maintained file) in favor of a
surgical fix that removes the actual conflation:

- **Full-height spacer pair** (`topSpacer`/`bottomSpacer`, permanent
  siblings of the materialized slot pool inside `#reader`, sized by a new
  `_syncSpacers()`): `#reader`'s real `scrollHeight` is now always the true
  full-manifest height (`cursorY`), from first paint onward, never bounded
  by the conveyor window — this alone removes the false "floor," since a
  continuous scroll can never outrun the actual DOM's real scrollable
  extent. `_rebuildConveyorWindow`/`_shiftConveyorWindow` no longer write
  `reader.scrollTop` directly at all (previously the only two places that
  did) — `_syncSpacers()` replaces that manual compensation entirely, and
  `virtualY` simplifies from `manifest[windowLo].globalY + reader.scrollTop`
  to just `reader.scrollTop` (the two are now numerically identical by
  construction). A separate, concrete bug found alongside this: the
  last-page-popup check in `syncScrollPosition` compared against the
  **windowed** `reader.scrollHeight` instead of the true `cursorY` bound
  `momentumLoop` already used correctly — the mismatch between those two
  "are we at the end" checks is what made hitting the false floor
  specifically look like "the last page," not just an unresponsive scroll.
- **`overflow-anchor: none`** added to `#reader` — without it, the browser's
  own default scroll-anchoring could apply a second, independent, async
  `scrollTop` correction on top of the conveyor's manual one whenever slots
  were added/removed above the fold, which is what most likely caused the
  "position resets to top of image" / progress-bar flash bugs specifically
  during mouse-wheel scrolling (a physical wheel delivers discrete notches,
  each its own settle-eligible gesture, so window shifts fired far more
  often than during one long touch/trackpad fling). Eliminating the manual
  `scrollTop` writes above removes the second writer this was racing
  against in the first place.
- **Mid-gesture recenter trigger**: `syncScrollPosition` now also calls the
  existing `recenterConveyor()` proactively once the current position gets
  within `EDGE_MARGIN` (20 pages) of either edge of the materialized window
  — not only from `scrollend`/settle. Safe to do mid-gesture specifically
  *because* shifts no longer write `scrollTop` (the old warning about
  recentering fighting native momentum applied to the previous
  scrollTop-writing version) — this closes the one gap the spacer fix alone
  would leave open (an extreme-velocity single gesture outrunning the
  80-slot window before a settle event ever fires).
- **Progress-bar undercount**: six call sites (`updateProgressBar`,
  `getCurrentGlobalPageIdx`, `getVisibleChapterId`, `getCurrentPageIdx`,
  `checkChapterCompletion`, `getCurrentPosition` — one more than originally
  scoped; `getVisibleChapterId` used the identical pattern and was found
  during implementation) all computed "current position" from the viewport
  **midpoint** (`virtualY + viewH/2`), which can never reach past
  `cursorY - viewH/2` once `virtualY` is clamped at true max scroll — any
  trailing page/segment shorter than half a viewport could never be marked
  active. Fixed with a shared `currentTrackingY()` helper that returns the
  true `cursorY` once at true max scroll, and the ordinary midpoint
  everywhere else — deliberately not switching every site to bottom-edge
  tracking generally, which would change "current page" semantics
  everywhere and make chapter-boundary detection more sensitive to any
  transient bad `scrollTop`, risking the flash/teleport bug rather than
  helping it.
- **Letterbox drag-scroll**: `applyStripWidth()` (the zoom control) used to
  resize `#reader` itself (`style.width`/`left`/`right`), so the black
  letterbox area at reduced zoom was literally outside `#reader`'s own DOM
  bounds and could never receive scroll/touch/pointer input. Fixed by
  introducing `#reader-inner` (all slot-pool/spacer content now lives here
  instead of directly in `#reader`) — `applyStripWidth` now resizes/centers
  `readerInner` (`width` + `margin: 0 auto`) while `#reader` itself, along
  with every scroll-mechanics reference (`scrollTop`/`scrollHeight`/
  `clientHeight`, all `scroll`/`scrollend`/touch listeners), stays
  permanently full-bleed — the letterbox gutter is now squarely inside
  `#reader`'s own hit-test region at any zoom level.
- Housekeeping folded in: deleted `SLOT_RENDER_RADIUS = 40`, a dead constant
  (declared, never read anywhere) from an apparently earlier, abandoned
  attempt at this same decoupling.

**Verified so far**: JS syntax-checked after every edit; isolated headless
(no live server, synthetic large manifests) tests confirming — a
continuous, no-settle-ever scroll across a 500-page synthetic manifest in
both directions never hits a false floor and reaches the true start/end
exactly, with `scrollHeight` staying correct throughout; the last-page
popup fires exactly at the true end and nowhere before; the progress-tracking
fix reaches the true end/a short trailing page while leaving ordinary
mid-scroll tracking numerically identical to before; and the letterbox
gutter resolves to `#reader` itself (not `document.body`) as the
`elementFromPoint` hit target once zoomed out, with `#reader` confirmed
never resizing. **Not yet verified**: real on-device/live-server testing
with actual manga content, particularly the mouse-wheel position-reset bug
(3) and progress-bar flash (5), which the `overflow-anchor` fix is expected
to resolve but couldn't be confirmed synthetically (that's genuinely
browser-internal scroll-anchoring behavior, not simulable without a real
wheel-scroll session against real DOM mutations under real network timing).
Bug 2 (black/unloaded page mid-scroll) was assessed as mostly-expected
network completion ordering rather than a distinct bug, and its optional
refinements (widening the fetch-priority band, direction-aware prefetch for
long-strip mode) were deliberately left undone as polish, not urgent.

**Live-tested update (2026-07-19, same day)**: bugs 1/3/4/5/6 all confirmed
fixed and working well on real devices ("all scrolling is really good now").
Bug 2 turned out to be real and specifically cold-start-only, not just
theoretical: opening a chapter fresh showed a black page around page 4,
resolving again by page 9 — i.e. several pages in the *middle* of the
initial screenful(s), not just far-off pages, loaded out of order. Root
cause: the fixed `±2`-slot "high priority" `fetchPriority` band (`
_rebuildConveyorWindow`'s center-out load loop) is tiny relative to
`SLOT_COUNT=80` — everything beyond it, including pages the user reaches
within the first few seconds of reading, competed on equal `'low'` footing
with pages far outside the initial view, racing to complete in arbitrary
order. This only ever showed up at the very first materialization (all ~80
requests fire nearly simultaneously); ordinary scrolling only adds a couple
of new pages per shift, so it was never noticeable there — consistent with
what was reported. Fixed by replacing the fixed radius with
`HIGH_PRI_AHEAD`/`HIGH_PRI_BEHIND` (floors of 12/4 pages, plus a
viewport/page-height-scaled term for unusually short "cuts"), biased forward
since long-strip reading is essentially always downward even before any
scroll has happened to establish a direction. Verified the floor dominates
(≥12 pages ahead get priority) across a range of realistic viewport/page-height
combinations, not just a lucky specific case.

Separately (`kinsho-android` repo, not this one): the user also asked for
the visible Android scrollbar to disappear, since the segmented progress
bar is meant to be the only scroll indicator. `chapter_reader.html`'s CSS
already fully suppressed its own scrollbar (`scrollbar-width: none` +
`::-webkit-scrollbar{display:none}` on `#reader`, `overflow: hidden` on
`html`/`body`) — the visible one was Android WebView's own native fading
scrollbar overlay, a `View`-level feature drawn by the WebView widget itself
over the page, entirely independent of CSS. Fixed with
`webView.setVerticalScrollBarEnabled(false)` in `MainActivity.java`'s
`applyTotalFullscreen()` — see that repo's own CLAUDE.md/git history for
detail, since no code in this repo could have addressed it.

Full analysis, the SumatraPDF/PDF.js comparison, and the staged plan this
was implemented from are preserved in the `kinsho-android` repo's CLAUDE.md
bug-list entry and the session's plan file, for anyone who wants the full
reasoning rather than just this summary.

## Reader bugs from live testing (2026-07-25)

Three bugs reported after ~1 hour of live testing in both long-strip and single-page
mode, on loose-image chapters, via both the `tm_tm` and `kinsho-android` apps. Root-caused
via a forked investigation (multiple passes — the first pass's theories didn't survive
contact with follow-up details the user provided) rather than guessed at.

### Bug 1 — visible seams between images in long-strip mode — STILL OPEN, not yet fixed

Reported: reading with padding off and zoom at 60% (max zoom-out), entered via jumping to
chapter 107 through the in-reader chapter dropdown rather than starting from page 1. Fast
scrolling (~1-1.5 pages/sec) eventually produced visible dividing lines between images that
should render flush. Not tied to one specific timing (happened both on arrival and appearing
later mid-scroll) and not tied to broken/low-res pages specifically (also seen on normally,
fully-loaded pages) — ruling out bug 2's mechanism (below) as the cause of this one.

Two theories were investigated and ruled out with certainty, not just "didn't find it":
- **The app's own geometry math** (`globalY`/`scaledH` in `buildManifest`/`recomputeGeometry`)
  is provably exact — each page's start position and its own height are derived from the
  same running variable in the same statement (`entry.globalY = cursorY; cursorY +=
  scaledH...`), so `globalY[i+1] - globalY[i] === scaledH[i]` always holds by construction.
  No floating-point drift is possible at this scale in JS either (IEEE-754 doubles are exact
  integers up to 2^53). Confirmed no stray CSS margin/border could add a gap independent of
  the JS math either.
- **The chapter-jump entry point isn't a special code path.** Every chapter-navigation call
  site (dropdown, next/prev, bookmark jump, single-page chapter-boundary crossing) goes
  through `goToChapter()` — a full `window.location.replace()`, i.e. a fresh page boot
  identical to opening any chapter directly. The only thing that differs is *where* you
  start (`startGlobalIdx`/`virtualY`), not *how* geometry gets computed.

Current leading theory (medium confidence, unconfirmed): a **browser-level rendering-precision
artifact**, not an app bug. Jumping deep into a long-running webtoon means `cursorY`/`virtualY`
starts at a cumulative sum of everything before it — plausibly multi-million-pixel for a
100+-chapter series, versus near-zero starting fresh from page 1. At that magnitude,
Chromium/WebView2's own layout engine (finite-precision internally) can introduce its own
1-2px positioning error between two adjacent, individually-correctly-computed elements —
external to this app's own (exact) JS math entirely. Against the reader's solid black
background, even a 1-2px gap reads as a stark seam. Zoom level looks incidental to this
theory, not causal: lower zoom produces *smaller* cumulative heights (narrower pages ⇒
shorter pages at the same aspect ratio), which argues against zoom being the driver.

**Not yet fixed** — no code changed for this bug. If the depth theory holds, the real fix
is architectural (periodically rebasing the coordinate system so the browser is never asked
to position elements at multi-million-pixel offsets, rather than one ever-growing absolute
position for the whole manga) — a bigger undertaking than 2/3 below, deliberately deferred
until confirmed. Suggested confirmation test for whoever picks this up: read a webtoon from
page 1 all the way down to a comparable cumulative depth to chapter 107, at any zoom, and
check whether seams start appearing there too, independent of entry method — that would
directly confirm depth (not chapter-jump, not zoom) as the real variable.

### Bug 2 — long-strip page stuck at low resolution, never finishes loading — fixed

Rare: a page would load partially/low-res and never upgrade to full quality, even though
later pages (scrolled past it) loaded fine. Workaround was tapping that exact page number
on the segmented progress bar, which happened to force a reload as an accidental side effect.

Root cause: `entry.loaded` was set to `true` the moment a page's image *fetch was dispatched*
(`.src = url` assigned), not when the browser actually finished loading it — and there was
no `onerror`/timeout/retry handling anywhere in the file. Every load path (`loadEntry`,
`loadEntryTier1`) early-returns once `entry.loaded` is true, so a fetch that stalled or got
dropped (more likely during fast scrolling, which fires many concurrent slot loads at once
and can exceed the browser's ~6-connections-per-origin cap) was never retried — stuck in
whatever partial state it reached, forever. The workaround "worked" purely because
`_rebuildConveyorWindow` (triggered by jumping to a specific page via the progress bar)
resets `loaded = false` for the entire visible window and creates fresh `<img>` elements —
a full nuke-and-reload, not a designed retry path.

Fixed in `_applyUrlToSlot`/`loadEntry` and `loadEntryTier1` (`templates/chapter_reader.html`):
`entry.loaded` is now only set inside the image's own `onload` handler, and `onerror` retries
(a couple of times, flat 500ms delay, then gives up and leaves `loaded` false rather than
stuck-but-marked-done) instead of being set unconditionally at dispatch time. Applied to both
the simple long-strip loader (`_applyUrlToSlot`, used during conveyor-window shifts) and the
tiered loader (`loadEntryTier1`, shared by long-strip tier-1 and single-page mode) — same
underlying bug, same fix shape in both places. Verified nothing else in the file assumes
`entry.loaded` becomes true *synchronously* right after these functions are called (every
other read site is just an `if (entry.loaded) return` guard or a reset to `false`), so making
completion genuinely asynchronous doesn't regress anything.

### Bug 3 — single-page mode: a different page loads on top unprompted — fixed

After a while of normal reading (~15 minutes), advancing to a new page would show it
correctly, then — before the user started reading it — a *different* page would load and
display on top, with no corresponding tap. A single navigation cycle didn't stop it; it took
several back-and-forth flips before the symptom stopped reproducing.

Root cause: `loadEntryTier1`'s single-page "upgrade" path (`_upgradeSpImage`, which writes
straight to `spImage.src` once a prefetched page's load resolves) only guarded against
staleness by checking `spManifest[spCurrentIdx] === entry` — i.e. "is this still the current
page," with no relationship to *when* the load was scheduled. Single-page mode prefetches up
to 15 pages ahead during a fast-reading burst, rescheduling on every flip; the existing
cancellation (`_spSchedulePrefetch`'s `AbortController`) only stops the *next pending* timer,
never a request already in flight. Over several minutes with any faster stretches (normal
even in careful reading), multiple independent prefetch batches can be resolving in the
background at once, each capable of firing the moment the user's own pace organically catches
up to that same page index later — explaining why it took several navigation cycles to stop:
each cycle only cancels its own newly-superseded pending timer, not whatever earlier batches
were already dispatched and still resolving.

Fixed by capturing `_spAnimFrame` (the existing monotonic token every *other* async
single-page callback already uses to detect a superseded flip — see `spGotoIdx`) at the
moment `loadEntryTier1` is scheduled, and checking it alongside the existing
`spCurrentIdx`/`entry` identity check before writing to `spImage.src`. A stale load can now
never win regardless of how large a backlog accumulates or how long it takes to resolve,
since its captured token can never match `_spAnimFrame` again once a newer flip has happened.

Investigation note for future reference: this took two passes to land on the right theory
for bug 3. The first pass concluded a single stale callback was the whole story; the user's
follow-up detail (needed several back-and-forth cycles, not just one, to stop reproducing)
revealed the cancellation gap was actually a *backlog* problem (multiple independently-
resolving stale loads), not a single race — worth remembering that "it took a few tries to
fix itself" is a meaningfully different signal from "it happened once and went away," and
points at accumulation/backlog bugs rather than single-race ones.

## Favourites row staleness fix + cover-scan false alarm (2026-07-26)

### Favourites row not updating immediately — fixed

Reported: adding a manga to Favourites didn't show it in the home page's Favourites row
(or, by extension, the Favourites page reached via "View more") — favourites added the day
before eventually appeared, just very late.

Root cause: `pickStableRow()` (`static/app.js`, added 2026-07-25 as part of capping the
Random/Favourites row reshuffle to once per hour) caches each row's picked ids in
`localStorage` per library, and only re-picks when either the hour-long freshness window has
expired or none of the previously-picked ids still resolve against the current manga list.
`favouriteIds` itself is always fetched fresh on every load (`/api/settings`), but the stale
cached id list from before the add/remove was still "fresh" and still had at least one id
that resolved, so a newly favourited manga was silently withheld from the row — and from
`goMore()`'s pinned-row handoff to the category page, since that reads straight from the same
(stale) `this.favourites` — for up to an hour.

Fixed by giving `pickStableRow` a second invalidation signal, applied only to the
`'favourites'` row: a fingerprint of the current candidate pool (sorted favourite ids,
joined) is stored alongside the picked ids, and a mismatch against the stored fingerprint
invalidates the cache immediately regardless of the 1-hour window. Deliberately not applied
to `'random'`: its pool is "every manga in the library," which changes on essentially every
scan, so fingerprinting it too would reshuffle on every scan and defeat the whole point of
the stability cache — only `'favourites'`, a user-curated set where any change is a
deliberate action that should be reflected on the very next load, gets this treatment.

Shipped 2026-07-26 (commit `710b851`). Note: this fix sat as an uncommitted local edit for a
while after being written — a `git pull` on the NAS deploy correctly reported "Already up to
date" because there was nothing to pull yet, which briefly looked like the fix hadn't worked.
Worth remembering for future sessions: confirm a fix is actually committed and pushed before
concluding a redeploy "didn't pick it up."

### Cover images with apostrophe/hyphen "not picked up" during scan — investigated, not a
### real bug in the end

Reported: a case3 manga's loose cover image (sitting directly in the manga's own folder,
filename containing an apostrophe and a hyphen) showed "No Cover" after scanning. Suspected
the scan's cover-detection logic was rejecting the filename's characters.

Investigated by reproducing the exact scenario directly against `scan_library`/
`process_manga_covers`/`find_cover_image` (case3 manga, loose cover image, straight-ASCII
apostrophe+hyphen, then typographic Unicode `’`/`–`, then the user's actual real cover file)
— every attempt correctly detected and processed the cover, and per-file mtimes round-tripped
exactly with no Unicode mangling at the Windows/Python filesystem boundary.

While chasing this, found and fixed a real (but ultimately unrelated) gap: `scan_library`'s
case2/case3/loose rescan-skip shortcuts only checked archive/volume/subfolder mtimes against
what was stored, never a loose cover image's own mtime — so a cover added to an
already-scanned manga, with no archive/volume change alongside it, could be silently skipped
forever if the containing folder's own mtime didn't register as changed. **This fix
(`_loose_cover_images_changed()`, wired into all three skip-shortcuts) was written, verified
with a regression test, then reverted** at the user's request once the real cause below was
found — noted here only in case the same "folder mtime didn't move" symptom resurfaces for a
different reason later, since the underlying gap in the skip logic is real even though it
wasn't the cause this time and no code from it is currently in the tree.

Actual root cause (self-diagnosed by the user): they were looking at a backup copy of the
manga on a different drive, not the path Kinsho was actually configured to scan — so
rescanning never picked up a change that was never in the scanned folder to begin with.
Separately, the original cover file was `.jfif`, which isn't in `IMAGE_EXTENSIONS`
(`.jpg`/`.jpeg`/`.png`/`.webp`/`.gif`) — Kinsho doesn't recognize `.jfif` as an image
extension at all, so even in the right folder it would never have been picked up as a cover
candidate. Converting/renaming it to `.jpg` and pointing at the correct path resolved it. No
code change was needed or made for this half of the report.

## Fix loose-image volumes serving broken pages in the reader (2026-07-26)

Reported: opening a case2 volume whose pages are raw loose image files (not an archive)
showed a corrupted/broken-image icon in place of every page.

Root cause: `templates/chapter_reader.html` builds loose-volume page URLs straight from
`dims.json`'s stored `filenames` array (`.../volume/{id}/page/{actual filename}`) — the
same convention already used for loose *chapters*. But `get_volume_page`'s route was
declared `@app.get(".../volume/{volume_id}/page/{page_index:int}")` — the `:int` path
converter rejects any non-integer segment before the request even reaches the handler, so
a filename like `page 01.png` 404'd at the routing layer itself, never reaching the
handler's own (otherwise-correct) loose-file-serving logic. Confirmed via git blame this
bug existed since the very first commit — loose-image volumes had never worked. Archive/
PDF/EPUB-type volumes were unaffected, since their page URLs were always plain sequential
integers, which the `:int` route already handled fine.

Fixed by changing the route to accept a plain string (`{filename_or_index}`, mirroring
the equivalent chapter route `get_chapter_page` already does) and branching inside the
handler: the loose-volume case now accepts either a literal filename or a numeric index
(kept for the `/pages` listing endpoint and any other integer-index caller), while
archive/pdf/epub still parse the segment as an int. Verified end-to-end with `TestClient`
against a throwaway data path (never the real configured library): a loose volume with a
filename containing an apostrophe now serves correctly by filename and by numeric index, a
genuinely missing filename still 404s cleanly, and an existing CBZ-type volume's page
serving is unchanged (regression check).

## Fix tap-to-toggle-UI not working in the reader (2026-07-26)

Reported immediately after the fix above, once loose-image volumes could actually be read
for the first time: tapping the reader screen no longer showed/hid the topbar and
bottombar, in every reading mode and every source type — not specific to volumes.

Root cause: `loadEntryTier1` (`chapter_reader.html`) reads `_spAnimFrame` synchronously the
moment it's first invoked — which happens during the very first tier-1 image preload,
immediately after any chapter/volume opens. But `_spAnimFrame`'s `let` declaration lived
much further down the file, in the single-page-mode state block (added as part of the
2026-07-25/26 stale-single-page-load fix — see "Reader bugs from live testing" above,
bug 3). Accessing a `let` binding before its own declaration line executes throws a
temporal-dead-zone `ReferenceError`, which aborted the rest of that top-level script pass
— including the line that initializes `touchMoved`, the flag the tap-to-toggle click
listener reads. The listener itself had already been registered earlier in the file, so
every tap still fired it, but it then threw the same class of TDZ error trying to read
`touchMoved`, silently swallowing the tap instead of toggling the UI. Confirmed live via
Playwright: the exact `Cannot access '_spAnimFrame'/'touchMoved' before initialization`
errors reproduced pre-fix and disappeared post-fix.

Fixed by moving `let _spAnimFrame = 0;` up next to `loadEntryTier1`'s own definition,
before it can ever be invoked, removing the now-duplicate declaration from its original
spot. Verified repeated tap-toggle cycles and scrolling produce no further errors, with a
screenshot confirming the topbar/bottombar/progress bar all correctly appear on tap.

## EPUB text reader — real chapter text instead of image-only pages (2026-07-26)

Kinsho's EPUB support had always treated an EPUB exactly like a CBZ/PDF:
`get_epub_image_list()` (main.py) extracts every embedded *image* and serves each as a
reader "page" via the existing image-strip reader. For a real prose novel this meant the
reader showed only a handful of embedded illustrations and none of the actual chapter
text — confirmed against a real user file (a 364-spine-item Italian novel, "Il Trono di
Vetro" / *Throne of Glass* vol. 1) that had genuine XHTML chapter content and only 8
embedded images total.

Added as a **new, separate reader page** rather than extending the existing image-based
reader (`templates/chapter_reader.html`, ~4600 lines of image-virtualization/conveyor-belt/
BLE-scroller/PDF-scale logic that doesn't apply to flowing text) — deliberate, at the
user's own request, to guarantee zero regression risk to existing manga/comic/PDF/
loose-image reading. `chapter_reader.html` was not touched at all.

- **Routing** — `volume_reader()` (`GET /manga/{library_id}/{manga_id}/volume/{volume_id}`,
  main.py) now loads the volume's dims record and, if `source == "epub"` and the file
  actually parses (`epub_reader.is_parseable()`), renders the new `templates/epub_reader.html`
  instead of `chapter_reader.html`. Any failure (ebooklib not installed, corrupt file)
  falls straight through to the old image-based reader, unchanged — purely additive,
  never a hard requirement. No client-side routing changes were needed anywhere:
  `volume_detail.html`'s `startReading()`/`openVolume()` were already the only code
  building this URL, and they just navigate there unconditionally regardless of source.
- **`epub_reader.py`** (new, pure-logic module, mirrors `opds.py`/`comicinfo.py`'s
  no-auth/no-data-access convention) — `build_reading_spine()` walks the book's raw
  `book.spine` order (not filtered to `linear="yes"`; front matter is almost always
  `linear="yes"` too, so filtering wouldn't remove the "boring front matter" noise anyway,
  and would complicate mapping TOC entries to spine indices). `build_toc()` maps the
  book's real chapter titles (`book.toc`) down to spine indices for chapter-jump
  navigation — with a basename-only fallback match for EPUB2 NCX-sourced hrefs, which
  (unlike EPUB3 nav.xhtml hrefs) aren't zip-root-normalized by ebooklib and can otherwise
  silently fail an exact-path lookup. `render_chapter()` parses a chapter's body with
  `lxml.html` — deliberately **not** `xml.etree.ElementTree` despite that being this
  project's convention elsewhere (`opds.py`/`comicinfo.py`), because that convention is
  about serializing app-controlled data into new XML, a different problem from parsing
  untrusted, producer-varied third-party markup; ebooklib itself already depends on lxml
  and uses its lenient HTML parser internally for exactly this reason. Rewrites every
  `<img src>`/SVG `<image xlink:href>` to an absolute internal asset URL (resolved
  relative to *that chapter document's own location* in the zip, not the zip root — e.g.
  `../cover.jpeg` inside `OEBPS/p000_cover.xhtml` resolves to `cover.jpeg` at zip root),
  strips `<script>` tags/`on*` attributes/all anchor `href`s (footnote/external links keep
  their text, just become non-clickable), and attaches every one of the book's own
  `ITEM_STYLE` CSS manifest items — always all of them, never per-document detection,
  since real chapter documents were confirmed to have a genuinely empty `<head/>` with no
  per-document `<link rel="stylesheet">` at all. A small in-memory `EpubBook` cache
  (`_book_cache`, unlocked plain dict, same convention as `_epub_page_cache`) avoids
  re-parsing the whole file on every chapter turn/asset fetch; wired into
  `_invalidate_stale_source_caches()` via `epub_reader.invalidate()` so a rescan-detected
  file change doesn't keep serving stale parsed content indefinitely (the exact bug class
  fixed once already for the other per-path caches — see the "deleted/edited pages still
  showing broken" entry above).
- **New API endpoints** (main.py): `GET .../volume/{volume_id}/epub/toc`,
  `GET .../volume/{volume_id}/epub/chapter/{spine_index:int}`,
  `GET .../volume/{volume_id}/epub/asset/{path:path}` — each gated by the same
  `can_access_library`/`is_manga_blocked` checks as every other content route.
  `resolve_asset()` validates the requested internal path against the manifest
  **restricted to image/style/font/cover/vector item types** — excludes `ITEM_DOCUMENT`
  (whole chapters go through the chapter endpoint, not "asset") and script/audio/video/smil
  — on top of the exact-manifest-match check that already prevents path traversal.
- **`templates/epub_reader.html`** (new) — topbar/bottombar chrome styled to match
  `chapter_reader.html`'s visual conventions (same CSS class shapes, not shared/copied
  code) for product consistency: back button, a TOC dropdown (nested, matching the book's
  real chapter structure) with prev/next spine-position buttons, and a settings panel with
  a single font-size control. Chapter content renders inside a sandboxed
  `<iframe sandbox="allow-same-origin">` with **no** `allow-scripts` — this is the actual
  containment boundary that makes applying the book's own arbitrary CSS safe (chosen
  deliberately over Kinsho's own typography, at the user's request): the CSS is fully
  scoped inside the iframe's own document and can never leak out to break the reader's own
  chrome, and no embedded script can execute even if something slipped past the
  server-side sanitization above. Font-size is applied as one narrow, deliberate
  `html { font-size: N% !important; }` override appended as the last stylesheet, not a
  general `!important` free-for-all, so `em`/`rem`/`%`-based sizing in the book's own CSS
  scales proportionally (absolute-pixel decorative front matter won't reflow — an accepted
  v1 limitation). Reading progress reuses the existing generic `POST /api/reading/progress`
  unchanged (`chapter_id = volume_id`, `page = spine index`) — no backend schema changes.
  **Tap-to-toggle-UI vs. text selection**: a click inside a cross-document iframe never
  bubbles to the parent page, so `chapter_reader.html`'s single
  `document.addEventListener('click', ...)` pattern can't see taps inside chapter content.
  Solved with a transparent overlay `<div>` stacked on the iframe that's only
  `pointer-events: auto` while the chrome is hidden — tapping it shows the chrome and
  immediately flips itself to `pointer-events: none`, so normal click-and-drag text
  selection works whenever the chrome is visible; hiding again reuses the existing
  auto-hide-after-3s-of-inactivity pattern, so selection is only ever unavailable during
  the narrow window right after a chapter opens or right after the chrome auto-hides, not
  permanently. Bookmarks and mid-chapter scroll-position resume (resume is chapter-start
  only) are explicitly out of scope for this v1.
- **Real-world parsing bug found and fixed during implementation**: `lxml.html`'s HTML
  parser doesn't treat `xlink:href` as a namespaced attribute the way strict XML parsing
  would — it's the literal attribute-name string `"xlink:href"`, not Clark-notation
  `{http://www.w3.org/1999/xlink}href`. The first implementation looked up the namespaced
  form and silently failed to rewrite an SVG-wrapped cover image's source (found live,
  against the real test file's actual title page, which is exactly an
  `<svg><image xlink:href="cover.jpeg"/></svg>` wrapper with no separate `<h1>`/text at
  all) — fixed by checking the literal `"xlink:href"` string (with a bare `"href"`
  fallback for newer SVG2-style markup).
- **`ebooklib` (+ its `lxml`/`six` dependencies) added to `requirements.txt`**, at the
  user's request, so this feature is active out of the box on a fresh install rather than
  needing a manual `pip install ebooklib` first — same treatment `PyMuPDF` already gets
  for PDF support. The code still degrades gracefully if it's somehow missing (an
  `ImportError`-guarded `EPUB_SUPPORT` flag, same as `rarfile`/`pymupdf`): `is_parseable()`
  returns `False` and every epub volume falls through to the old image-only reader exactly
  as before, so a custom/stripped install without this dependency doesn't break, it just
  loses text rendering for EPUBs.
- **Verified** end-to-end against the real test file (over the network share, via a
  throwaway `data_path`/temporary local server, real `bootstrap.json` backed up and
  restored after — same pattern as the two fixes above): TOC/chapter/asset endpoints via
  `TestClient`, a non-epub volume in the same library still routing to the untouched
  `chapter_reader.html` (regression check), and a full Playwright pass — real chapter
  text and an embedded illustration both render correctly with the book's own CSS applied,
  TOC navigation jumps to the right chapter, font-size control visibly resizes text,
  repeated taps reliably show/hide the chrome, and double-clicking real paragraph text
  selects a word once the chrome is visible.

### EPUB reader follow-up: fixed page, paginated flip, no selection (2026-07-26)

Live-tested feedback on the v1 above, all addressed same day:

- **Full-bleed layout looked "terrible"** on a wide monitor — text spanned the entire
  window at an unreadable line length. Fixed with a fixed A4-portrait-ratio (`1/1.4142`)
  page box, sized via CSS `width: min(90vw, 900px, 63.6vh)` + `aspect-ratio` (so it never
  stretches to the window/container's own shape, just scales down proportionally on small
  screens) instead of the iframe filling the viewport directly.
- **Bottombar dropdown now lists volumes, not in-book chapters** — matches
  `chapter_reader.html`'s actual navigational structure, which this v1 had gotten
  backwards: the bottombar's dropdown + prev/next are *always* coarse chapter/volume-level
  navigation in the existing reader (a full page load to a different volume/chapter), never
  fine-grained page-by-page movement — that happens through a separate mechanism (the
  page slider/scroll in the image reader; here, the tap zones below). Fetches
  `/api/manga/{lib}/{id}/volumes` once at init; picking a different volume does a real
  `window.location.replace()` navigation, identical in shape to `goToChapter()`. The
  topbar was also corrected to show the constant manga name (matching
  `chapter_reader.html`'s `titleEl.textContent = manga.name`) instead of the current
  in-book chapter title, which this v1 had shown there instead.
- **Text selection removed**, and **tap-to-toggle-UI + page-flip now use the same
  three-zone model as `chapter_reader.html`'s single-page mode** (`.sp-zone-left/-center/-right`,
  30/40/30 split) — living permanently in the parent document, not a
  toggled overlay reaching into the iframe. This incidentally fixes v1's "click to hide UI
  doesn't work" report too: since selection is no longer a design goal, the zones can be
  permanent instead of conditionally `pointer-events`-gated, which sidesteps the cross-frame
  click-bubbling limitation entirely rather than working around it. Arrow keys
  (Left/Up = prev, Right/Down = next) do the same thing, matching the existing reader's
  single-page-mode keyboard support.
- **Real page-flip pagination, replacing v1's static single-screen-per-chapter view** —
  and where an actual, reproducible rendering bug was found and fixed. First attempt used
  the standard "CSS multi-column pagination" technique (each on-screen page = one
  `column-width` column matching the viewport, turning a page = `translateX` by one
  column-width) — confirmed via direct, isolated testing (a minimal non-app test page, no
  Kinsho code involved) that **this rendering engine does not reliably clip multi-column
  overflow** — a sliver of the next column's content visibly bled past the intended page
  edge regardless of `overflow: hidden`, `overflow: clip`, or `contain: paint`, on the
  column element itself or a wrapping ancestor, and reproduced even through an
  actually-sized iframe matching the column width exactly (ruling out "just a wider test
  viewport" as the explanation). Root-caused by testing systematically, not guessed at.
  Replaced with **vertical DOM-slice pagination** instead: a fixed-size, padded "picture
  frame" (`#pg-outer`, plain block element, `overflow: hidden`) around a naturally tall,
  single-column flow of the chapter's content (`#pg-inner`) — a page is a vertical slice
  of that flow, shown by `translateY`-ing `#pg-inner` so the slice's own top aligns with
  the frame's top. This relies only on ordinary block-level overflow clipping (used
  everywhere on the web, e.g. any "read more" fade box), which has none of the multi-column
  clipping history above. Page-break Y-offsets are computed by walking every text node's
  line boxes via `Range.getClientRects()` (one rect per wrapped line) plus every image's
  own top/bottom, then greedily picking the furthest such boundary that still fits within
  one frame-height of the previous break — so a break always falls between two lines (or a
  line and an image), never through the middle of one, the same requirement any real
  reflow-based paginator (Readium, EPUB.js in non-columns mode) has to satisfy. Reaching
  the last page of a chapter and flipping further crosses into the next spine index
  automatically (starting at its own page 0), and flipping backward from a chapter's own
  first page lands on the *last* computed page of the previous one — verified across a
  real multi-flip session, including a chapter boundary crossing with an embedded image
  still rendering correctly on the far side of it.
- **`epub_reader.py`'s `render_chapter()`** now retags the parsed body element from
  `<body>` to `<div>` before serializing (attributes/classes kept as-is) — needed once the
  returned markup had to be nested inside the frontend's own `#pg-inner` wrapper; a literal
  second `<body>` can't be nested inside another element in a real document.
- Font-size changes and window resizes both go through the same `renderChapterContent()`
  path with `land: 'clamp'` (recompute page breaks for the new dimensions, reposition at
  the same page index, clamped to the new total) — no separate code path for either.

### EPUB reader second follow-up: revert the fixed page, scroll+swipe instead of tap-paginated (2026-07-26)

More live feedback, same day, on the two points above:

- **The fixed A4 page box "may have been a bad idea"** — looked bad on a tablet. Reverted
  outright: `#epubViewport`/`#epubFrame` are back to plain full-bleed (`position: fixed;
  inset: 0`), no page-box wrapper, no `aspect-ratio`/`min()` sizing. Simplest possible
  fix, no replacement layout scheme requested.
- **Reworked the whole reading-interaction model, replacing tap-only pagination with
  scroll + swipe**, per explicit spec: horizontal scroll/swipe flips pages "like the
  original" (single-page mode's swipe-to-flip), vertical scroll reveals lines that don't
  fit the screen, and tap is repurposed to scroll the current page — reaching the actual
  end of it is what changes the page, same as before. This also **eliminates the previous
  round's line-boundary pagination machinery entirely** (`computePageBreaks`/
  `Range.getClientRects()`/the `#pg-outer`+`#pg-inner` translateY split) — no longer
  needed, since a chapter now just scrolls normally instead of being sliced into discrete
  same-height screens:
  - `#pg-outer` (the book's own retagged `<div>`, still wrapped once for consistent
    reading-margin padding) is simply `overflow-y: auto` at 100% width/height of the
    iframe — ordinary native browser scrolling, no JS driving it, so it's immune to the
    CSS-multi-column clipping bug from the previous round by construction (there's no
    multi-column layout left at all).
  - Interaction is attached directly to `#pg-outer` inside the iframe's own document, via
    listeners the *parent* script adds to that DOM (allowed under
    `sandbox="allow-same-origin"` even with no `allow-scripts` — that flag only blocks
    script tags embedded in the srcdoc itself, not a parent script reaching in to call
    `.addEventListener()` on nodes it already has permitted access to). This was the
    deciding reason it's structured this way instead of an overlay div in the parent
    (tried in the very first version): an overlay sitting on top of the iframe to catch
    taps would also have blocked the native wheel/touch scrolling this round now depends
    on.
  - A tap (mousedown+up / touchstart+end with under ~8px of total movement) in the
    left/right third scrolls the current chapter by one screen up/down, or crosses into
    the prev/next chapter once already at that scroll boundary; a tap in the middle third
    toggles the chrome, matching `chapter_reader.html`'s single-page-mode zone split
    (30/40/30) without needing actual overlay `.sp-zone` divs to get it — the split is
    just an x-position check inside one shared handler.
  - A clear horizontal drag (axis determined by whichever of dx/dy exceeds ~10px first)
    beyond a 60px commit threshold flips directly to the prev/next chapter regardless of
    current scroll position, independent of the tap-vs-scroll logic above.
  - Font-size changes now restore scroll position by **fraction** (`scrollTop / (scrollHeight
    - clientHeight)`) rather than a page index, since there's no discrete page index left
    to restore.
- **Found and fixed a real race while testing this**: the font-size `<input type="range">`
  fires `'input'` continuously while being dragged, and the handler rebuilt the whole
  srcdoc on every firing with no debounce — overlapping in-flight rebuilds could race,
  observed live as the restored scroll fraction landing at 0 instead of the pre-drag
  position (a later rebuild reading its "restore fraction" from a still-mid-rebuild
  document rather than the settled one). Fixed with a 200ms debounce on the font-size
  handler; verified a simulated rapid-fire slider drag (5 values in quick succession)
  now preserves the scroll fraction to within rounding (0.3198 → 0.3197) instead of
  collapsing to 0.
- **Testing note for whoever touches this next**: simulating the horizontal swipe via
  Playwright's real `page.mouse.move()` while a button is held down **hangs indefinitely**
  once the drag path crosses into the iframe's content area — reproduced in isolation
  (a plain non-iframe page with the identical move-sequence completes instantly, so
  it's specific to iframes, not the drag API in general), and is a known-shape category
  of Playwright/CDP flakiness with synthetic pointer input over iframes, not a bug in
  this code. Verified the actual gesture-handling logic instead by dispatching synthetic
  `MouseEvent`s directly at the `#pg-outer` element via `element.dispatchEvent(...)` in
  `page.evaluate()`, which exercises the same listeners without going through Playwright's
  OS-level input simulation — confirmed both drag directions correctly flip
  forward/backward. A real user's actual touch/mouse drag doesn't go through this
  synthetic-input pathway at all, so this is a test-authoring note, not a shipped
  limitation.

### EPUB reader bookmarks: full mirror of the manga reader's system (2026-07-26)

Ported `chapter_reader.html`'s bookmark system to the EPUB reader in full, per explicit
request — including the parts beyond plain "save my place": start/end position ranges,
auto-numbered name groups, and "end-to-end reading" (auto-jump from one bookmark's end
straight to the next bookmark in the same group, then the next group, while reading).
Confirmed with the user this was wanted in full rather than a simplified version, since
those extra layers read as manga-scene-rereading-specific and don't have an obvious
one-to-one equivalent for a novel.

- **Position model, the one real adaptation.** The manga reader's position is
  `{chapterId, chapterIdx, pageIdx}` — a chapter/volume plus a discrete page image index.
  This reader has no discrete "page" left in its scroll-based model (see the interaction
  rework above), so a position is instead `{volumeId, volumeIdx, spineIndex,
  scrollFraction}` — spine index plus a continuous scroll fraction within that chapter.
  `positionBefore`/`positionEqual` compare in that order (volume, then spine, then
  fraction, with a small epsilon on the fraction to avoid float-precision false
  negatives). The bookmark-row label is `"{volume name} — Ch. {spineIndex+1}, {percent}%"`
  — simpler than trying to resolve a nearby TOC chapter title (which would need an extra
  per-volume async fetch just for cosmetics), while still showing the same two-axis
  chapter+fine-position information the original's "Ch. X — Page Y" does.
- **Storage unchanged** — same generic, opaque `POST/GET /api/manga/{library_id}/
  {manga_id}/bookmarks` blob the manga reader already uses, keyed by `manga_id` (not per
  volume, exactly like the original — a manga's bookmarks are shared across all its
  volumes). No backend changes were needed at all for this feature.
- **Cross-volume jump.** A bookmark's start/end can point at a different volume than the
  one currently open (added while reading Volume 2, then opened from Volume 1's list, for
  instance). Same-volume jumps are handled in place (scroll within the current chapter, or
  load a different spine chapter and land at the saved fraction); a different volume does
  a real page navigation, carrying the target position through as `?page=N&frac=F` query
  params — the reader's existing `?page=` resume convention gained a `frac` sibling for
  exactly this. Verified live: bookmarking a spot in Volume 2, then triggering the jump
  from Volume 1, correctly lands on Volume 2 at the right spine index and scroll position
  after the full page load.
- **End-to-end trigger mechanism.** The manga reader piggy-backs its end-to-end check on
  its existing scroll-driven progress-save cycle. This reader didn't have an equivalent
  periodic hook, so one was added specifically for this: a debounced (250ms) `scroll`
  listener on `#pg-outer`, attached in the same place the tap/swipe gesture listeners
  already get (re-)attached after every chapter render, calling the ported
  `checkEndToEnd()`/`advanceEndToEnd()` pair unchanged from the original's logic (group
  membership, auto-numbering renumber-on-rename/delete, "next group by earliest position"
  ordering once a group is exhausted).
- **Verified end-to-end** (pun intended) against the real test file: add (custom name and
  empty-name auto-numbering), list with correct position labels, jump, close (including
  the swap-if-end-is-before-start correction and the "already closed, update?" confirm
  path), auto-numbered groups forming/renumbering correctly on a second same-base add, on
  rename (including a group correctly collapsing back to a bare name once only one member
  is left), and on delete; a full end-to-end run correctly auto-advanced through both
  members of a two-bookmark group and then reported "Bookmarks ended"; cross-volume jump
  confirmed via a real full-page navigation. One real test-harness gotcha hit along the
  way, not an app bug: reusing a bookmark name like "Chain 01" in a test collided with the
  auto-numbering convention itself (a trailing number is always treated as an existing
  auto-suffix, per `bmBaseName`'s regex, exactly matching the original's behavior) —
  switching to plain non-numeric test names resolved it.

## Single-page mode rebuilt as a horizontal snap-scroll reader (2026-07-27)

Started from a live-testing report describing single-page mode showing the wrong page on
flip (a stale, complete-looking image from a different page, not a broken-icon/loading
state) — on loose-image manga specifically, worsening over a reading session. Ended, the
same day, with single-page mode's entire page-turn mechanic rebuilt from scratch as a
horizontal analogue of `#reader`'s vertical conveyor, at the user's explicit request, after
several smaller patches to the old two-element animated-swap design kept fixing one symptom
and exposing another. Kept here in full because the reasoning across every attempt —
including the ones that got superseded — explains why the current code is shaped the way it
is, and because the *last* round of fixes was only findable by actually reproducing the bug
live rather than by reading code, which is itself worth remembering as a lesson for next
time something in this area misbehaves.

### Current architecture (what's live now)

Single-page mode is `#spStrip`, a horizontally-scrolling flex row using native CSS
scroll-snap (`scroll-snap-type: x mandatory` + `scroll-snap-stop: always` — the second
property is what actually guarantees "one flick = exactly one page," the first alone lets a
fast flick sail past several snap points). It's a **separate, parallel implementation** from
`#reader`'s vertical conveyor, deliberately not a generalized/shared version of it — touching
`#reader`'s already-hardened conveyor code to make it axis-agnostic was judged too risky
given how many hard-won fixes are already baked into it (see "Long-strip reader" above). The
new code follows the same *pattern* independently: `spLeftSpacer`/`spRightSpacer` flank a
small (`SP_SLOT_COUNT = 12`, vs. `#reader`'s 80 — a horizontal flick physically can't outrun
a window this size the way a long vertical fling can) sliding window of materialized
`.sp-slot` elements, sized/positioned via `_spSyncSpacers()`/`_spRebuildWindow()`/
`_spShiftWindow()`, mirroring `_syncSpacers`/`_rebuildConveyorWindow`/`_shiftConveyorWindow`
functionally but independently. One real simplification over `#reader`: every slot is
exactly viewport-width (pages are shown at a fixed size, not flowed with per-page heights
like long-strip's variable-aspect-ratio pages), so `spStrip.scrollLeft` is directly the
source of truth for position — no `virtualY`-style shadow variable needed at all.

Padding between pages is a fixed, always-on constant (`SP_GAP`, baked in as inner padding on
each slot so slot width stays exactly `viewW` with no fencepost math) — not a user setting,
deliberately independent of long-strip's own `readerSettings.padding`/`PAGE_GAP`, which
stays scoped entirely to long-strip mode as before.

Tap zones are a single `click` listener on `spStrip` checking `event.clientX` against a
30/40/30 split, not overlay `<div>`s — overlay zones would have intercepted/blocked the
native touch-scroll gestures this mode's whole page-turn mechanic now depends on, the same
tension already solved this same way for the EPUB reader's tap-vs-scroll handling (see
above). The old `_spDragStart/_spDragMove/_spDragEnd` 50px-threshold drag system (and the six
listeners it needed) is gone entirely — native scroll-snap handles swipe-to-flip on its own,
there's no gesture left for JS to track.

Chapter boundaries are unchanged in behavior — still a full `goToChapter()` page reload at
the first/last page of a chapter, never an in-place continuation into the next chapter's
pages, exactly like before this rebuild. This was a deliberate scope boundary: long-strip's
`manifest` already spans every chapter continuously (so long-strip scrolling *is* seamless
across chapters within one page load), but making single-page mode match would mean
rewiring all six functions that read `spCurrentIdx`/`spManifest` for progress-bar/bookmark
tracking (`getCurrentGlobalPageIdx`, `updateProgressBar`, `getVisibleChapterId`,
`getCurrentPageIdx`, `checkChapterCompletion`, `getCurrentPosition`) to scan across chapters
— real scope nobody asked for. At the true first/last page, `overscroll-behavior-x: contain`
plus the relevant spacer hitting width 0 gives a fully native rubber-band bounce with no
phantom slot needed; crossing chapters stays a deliberate tap-zone/keyboard action at the
true edge.

RTL reading direction is handled by one self-inverse conversion, `rOf(idx)` (`isRTL() ?
spManifest.length-1-idx : idx` — `rOf(rOf(x)) === x`), rather than a direction ternary
threaded through every window-management formula. All window state (`spWindowLo`/
`spWindowHi`/shift/recenter math) lives entirely in "reading-order" (`r`) space, which is
always physically left-to-right regardless of direction; `rOf` is the only place direction
gets resolved, applied at exactly three boundary points (`spGotoIdx` converting `idx→r`, the
paint path converting `r→spManifest[idx]`, and the settle handler converting a settled `r`
back to `idx`). The four pre-existing physical-direction ternaries (tap zones, keyboard)
didn't need to change at all — they already only ever called `spForward`/`spBackward` in
index space.

`spGotoIdx`/`spForward`/`spBackward` are still plain top-level `function` declarations (not
`const` arrows) — load-bearing, since two features monkeypatch-wrap them later in the file
(chapter-completion progress save on reaching the last page, and bookmark end-to-end
auto-advance) by reassigning the same names; switching to `const` would silently break both.

### The path to get there (superseded attempts, kept for the reasoning)

1. **`entry.loaded` gate** — the single-`<img>` swap design (already in place from an
   earlier session, see the reader-bugs history above this section) trusted `entry.url`
   alone as "safe to show," which for loose-image sources is set synchronously long before
   any prefetch actually finishes. Gated the swap on `entry.loaded` instead, showing a
   thumbnail placeholder in between. **Superseded** — didn't fix the report (see below), but
   the underlying "don't trust url-known as loaded" insight carried forward.

2. **Decode-gated `_runSlide` reveal** — investigated further after "nothing changed"; found
   the actual promotion between the two swap elements ran on a fixed `setTimeout(dur)`
   completely decoupled from whether the incoming element had actually finished decoding —
   on a slow decode (large pages, worse after a session's worth of them, matching "starts
   after 6-7 pages, not immediately") the promoted element kept showing whatever it held
   before. Gated the whole transition on `spImageBg.decode()`. **Superseded** by the full
   rebuild below, but confirmed the general principle that a fixed timer racing a real
   decode is a recurring failure shape in this codebase.

3. **"Always try full-res first, thumbnail only as a slow-path fallback"** — a follow-up
   report clarified the thumbnail-first behavior on *every* chapter open wasn't really about
   decode timing, it was that `entry.loaded` resets to `false` on every chapter boundary (a
   full page reload constructs fresh entry objects) even when the browser's own HTTP cache —
   warmed by the existing eager cross-chapter prefetch *before* the reload — already had the
   bytes. Reworked to attempt the real image directly, racing a short (150ms) fallback timer
   before showing a thumbnail. **This is the version that regressed** ("worse than before,"
   sometimes loading a thumb then the *previous* page on top) — root cause: two independent
   `_runSlide` calls (the real-image path and the fallback-thumbnail path) could both fire
   for the same flip and race on the same pair of swap elements, corrupting the promotion
   state. This is what prompted the full rebuild rather than another patch.

4. **Missing `Cache-Control` header on loose chapter routes** — a real, independent
   server-side bug found in the same investigation: `get_chapter_page`'s two loose-image
   branches (`main.py`) returned a bare `FileResponse` with no `Cache-Control` at all, unlike
   every other source type *and* unlike `get_volume_page`'s own loose branch, which already
   set `Cache-Control: public, max-age=86400, immutable`. Without it the browser couldn't
   trust a cached copy across a chapter-boundary reload and had to revalidate over the
   network. **Fixed and kept** — this one wasn't superseded, it's a real, permanent, still-
   correct fix independent of everything else in this section.

5. **Simplification to one persistent `<img>`, no animation** — after the racing-fallback
   regression, and prompted directly by the user questioning why this needed to be so
   complicated ("check how SumatraPDF does it... we're already doing it right in long-strip
   mode"), replaced the entire two-element slide-transition system with a single `<img>`
   reassigned directly on each flip — the same direct-`.src`-assignment approach long-strip's
   own slot images already used successfully, with no animation at all. This fixed
   correctness (confirmed) but gave up the page-turn feel. **Superseded** by the horizontal
   conveyor below, at the user's request to bring the animation back "done properly" via
   native scroll-snap instead of hand-rolled JS timing — but this was the necessary
   intermediate step that proved the *display* mechanism, once simplified enough, actually
   worked, isolating the remaining bugs to the buffer/window-management layer that the
   conveyor rebuild then addressed head-on.

6. **The horizontal conveyor itself** — designed via `EnterPlanMode` (two parallel `Explore`
   agents mapping `#reader`'s conveyor internals and single-page mode's surrounding
   call-sites/UI in full, then one `Plan` agent validating the proposed architecture against
   the real current code) given the size and the track record of under-planned attempts
   causing regressions earlier in this same session. See "Current architecture" above for
   the result. Landed cleanly except for two bugs caught immediately after deploying:
   - **Chrome not toggling on a center tap, toggling unexpectedly on a side tap** — the new
     single `click` listener on `spStrip` never called `e.stopPropagation()`, so every click
     also bubbled up to the pre-existing generic `document`-level click handler that drives
     "tap anywhere to toggle chrome" in long-strip mode (which has no zones). A center tap
     toggled chrome twice (net no visible change); a side tap toggled it once as an unwanted
     side effect of just changing the page. The old per-zone `<div>`s had each called
     `stopPropagation()` for exactly this reason — lost when they were consolidated into one
     listener. Fixed by adding it back.
   - **Some slots permanently blank after a shift** — `_spShiftWindow` called `_spLoadEntry`
     for newly-materialized slots *before* `spAssignedSlot` was updated to point at them
     (that mapping was only established by the DOM-rebuild-from-live-DOM loop afterward), so
     the load silently no-op'd (`si === -1`) and the slot's `<img>` never got a `src` at all
     — permanently, since nothing ever retried it. Fixed by moving the load loop to after
     the `spAssignedSlot` rebuild. **This fix turned out to be incomplete** — see below.

### Deep bugs found via live reproduction (same day)

The next report — "still bugged but just the broken image icon and images not loading" —
didn't yield to further code reading; the fix above had addressed a real bug but clearly not
the whole story, and reasoning about rapid-flip races purely from the source was no longer
productive. Installed Playwright locally (`pip install playwright && python -m playwright
install chromium` — not previously set up in this repo/environment) and built a throwaway
reproduction: a synthetic 40-page test manga (`PIL`-generated loose JPEGs, each with a
visible page number baked in, large — 1600×1100 — to actually stress decode cost) under a
scratch `data_path`, driven by a real headless Chromium executing rapid `ArrowRight`/
`ArrowLeft` key sequences while directly inspecting `.sp-slot img` elements' `naturalWidth`/
`complete`/`src` after each batch. This is what actually cracked it — none of the four bugs
below were reachable by re-reading the code again, only by watching it run.

(Getting the local server running at all needed one unrelated fix first: the local Python
env had `fastapi==0.95.0`/`starlette==0.26.1` installed, both far behind what
`requirements.txt` actually pins — `pip install -r requirements.txt --upgrade` resolved a
`ValueError: context must include a "request" key` on every page load, an artifact of the
older Starlette's different `TemplateResponse` signature. Worth remembering next time a
fresh local run of this app throws on template rendering: check installed versions against
`requirements.txt` before assuming it's a code bug.)

Four distinct, compounding bugs, all specific to rapid repeated navigation (holding an arrow
key, fast tapping — not ordinary one-flip-at-a-time reading):

1. **Tap-vs-drag misdetection.** `_spUserDragging` (added to distinguish "trust scroll
   position as truth" settles, which should only happen after a genuine native drag, from
   "spCurrentIdx is already correct" settles after programmatic navigation) was set on any
   `pointerdown` — including a plain tap with zero movement, e.g. the very same center-zone
   tap used to open the chrome bar before starting the test flip sequence. That latched it
   `true` for the rest of the session, making every later settle wrongly trust an irrelevant
   scroll position. Fixed by only setting it once the pointer actually moves past an 8px
   threshold after going down, mirroring the `touchMoved` convention already used elsewhere
   in this file.

2. **`spCurrentIdx` jumping backward mid-sequence.** Confirmed directly via an instrumented
   trace: holding an arrow key retargets `spStrip`'s smooth `scrollTo()` faster than any one
   animation can finish, so the *actual* scroll position can lag far behind the logical
   current page (observed: `spCurrentIdx` at 17, actual scroll position corresponding to
   page 5). The settle handler used to trust that lagging position as ground truth and
   "corrected" `spCurrentIdx` backward to match it — a real regression a user would see as
   the reader suddenly jumping back several pages mid-flip. Fixed: a settle that didn't
   follow a genuine drag (see #1) now just snaps the visible scroll position up to the
   already-correct `spCurrentIdx`, instead of trusting position as truth.

3. **Spurious full-window rebuilds.** The same lagging scroll position was *also* what
   `spGotoIdx` used to decide "is this target close enough to shift to, or far enough to need
   a full window rebuild" (`Math.abs(targetR - _spCurrentR()) > SP_SLOT_COUNT`). Since that
   position can lag arbitrarily during rapid retargeting, an ordinary sequential 1-at-a-time
   flip could spuriously read as a huge jump and trigger a full rebuild — tearing down and
   recreating every one of the 12 materialized slots, aborting every in-flight image load in
   the process — when a cheap 1-step shift was all that was actually needed. Fixed by
   deciding big-jump-vs-shift against the *logical* window bounds (`spWindowLo`/`spWindowHi`,
   which never lag) instead of the physical scroll position.

4. **Slots permanently stuck blank, the deepest one.** Even after fixes 1–3 made the window
   management itself provably correct (confirmed via trace: clean monotonic shifts, no more
   spurious rebuilds), most slots outside the very first materialized window still never
   loaded. Root cause: `_spLoadEntry` gated on `entry.loaded` — a flag *shared* with
   `#reader`'s own tier1/tier2 prefetch machinery (`loadEntryTier1`, reused here unchanged for
   wider-than-materialized lookahead, see the loading-pipeline-reuse note in "Current
   architecture" above). `loadEntryTier1` sets `entry.loaded = true` the moment it fetches
   bytes *anywhere* — including into a detached lookahead probe up to 15 pages ahead of
   wherever the user actually is — with no relationship to whether anything was ever painted
   into one of *this* mode's own slot `<img>` elements. Since `_spSchedulePrefetch` reaches
   that far ahead on every single flip, `entry.loaded` routinely went `true` for entries
   `_spLoadEntry` had never actually painted, and its own guard then skipped them forever.
   Fixed by decoupling the two concerns: `_spApplyUrlToSlot` now tracks per-slot-element
   readiness via `imgEl.dataset.spUrl` (does *this specific* `<img>` already show *this*
   entry's URL) instead of trusting the shared `entry.loaded` flag, which stays exactly as
   it was for `#reader`'s own purposes.

Verified via the same Playwright harness: zero broken images across 30 forward + 35 backward
+ 15 more forward flips, at both a realistic ~2.5 flips/sec pace and a deliberately
unrealistic ~8 flips/sec stress pace (holding an arrow key far faster than any real reader
would). Pushed as `b085332`, confirmed present and running on both `kinsho`/`kinsho2`
containers on the NAS.

### Testing infrastructure now available for next time

Playwright (Python) is now installed in the local dev environment (`pip install playwright`
+ `python -m playwright install chromium`) — wasn't set up before this session. For any
future reader bug that resists diagnosis by code reading alone (rapid-interaction races,
timing-dependent state, anything where "what actually happens across N events" matters more
than "what does this one function do"), the pattern used here is worth repeating rather than
reinventing: a throwaway `data_path` + synthetic test manga (loose images are cheapest to
generate — `PIL.Image` + `ImageDraw` to bake a visible page number into each one, so failures
are identifiable at a glance in a screenshot or via string comparison) + a headless-Chromium
script driving real key/mouse events and reading live DOM/element state back out, rather than
guessing from source alone. Remember the local venv needs `pip install -r requirements.txt
--upgrade` if it's drifted from what's pinned (see the Starlette note above) before the app
will even serve pages correctly.

### Not yet verified

Everything above was tested against desktop headless Chromium at a 480×900 viewport — real
verification on the Android app / actual touch hardware (not synthetic pointer events) is
still outstanding, along with RTL reading direction (the `rOf()` logic was reasoned through
carefully and matches the pre-existing physical-direction ternaries, but never actually
exercised against a real RTL manga this session). Worth checking next: real-device flick
feel (does scroll-snap's native momentum feel right at these page dimensions), RTL swipe
direction, and the bookmark end-to-end / chapter-completion-on-last-page features specifically
via a *swipe* (not tap/keyboard) — both route through `spGotoIdx`'s monkeypatch chain
correctly by construction now, but haven't been manually exercised via a real swipe gesture
since the horizontal rebuild.

## Loose-image slots showing a black flash when scrolling/swiping faster than they load (2026-07-28) — fix applied, NOT YET LIVE-TESTED

Reported from a real screen recording (`Screen_Recording_20260727_231948_Kinsho.mp4`, single-page
mode, loose-image manga): scrolling/swiping faster than pages can load shows a black
afterimage at the edge being scrolled toward. Confirmed visually by extracting a contact
sheet and edge slit-scans from the recording with `ffmpeg` (real solid-black regions at the
edges, not just inter-page gaps).

Root cause: every reader slot (`.sp-slot` in single-page mode, and the equivalent long-strip
slots) is `background: #000` — that's what shows before its `<img>` paints. Prefetching
(`_spSchedulePrefetch`/`prefetchAroundIndex`) runs ahead of the current position (up to
15 pages, velocity-scaled), but `loadEntryTier1` (`templates/chapter_reader.html`, was around
line 1876) only forced the browser to fully **decode** pixels ahead of time
(`img.decode()`) for compressed sources (archive/PDF/EPUB, gated behind `isCompressed`) —
for loose-image folders it only warmed the HTTP byte cache. So even with bytes already
downloaded, the moment a swipe/scroll reveals a new slot the browser still has to
synchronously decode a multi-megapixel page before painting, and a fast enough gesture
outruns that decode — showing the slot's raw black background underneath for a moment.
Matches this reader's established pattern of bugs specifically affecting loose-image content.
Confirmed via `git log -S` this `isCompressed` split goes back to the initial commit, not a
deliberate loose-vs-compressed design choice.

**Fix applied**: removed the `isCompressed` branch split in `loadEntryTier1` and unified
every source onto the same predecode-then-paint path already proven for compressed sources
— a detached `Image()` + `img.decode()` before assigning into the real slot, regardless of
source type. Since the browser's decoded-pixel cache is keyed by URL (not by element), the
later `slotPool[si].img.src = url` in the real slot reuses that decode and paints
immediately. Benefits both single-page mode (via `_upgradeSpSlot`) and long-strip mode,
since both share `loadEntryTier1`. Syntax-checked clean with `node --check` on the extracted
inline script — no live/browser verification performed.

**Known residual limitation, not addressed by this fix**: a gesture fast enough to outrun
the prefetch radius itself (not just the decode) — i.e. reaching a page before
`_spSchedulePrefetch` ever dispatched a request for it at all — still hits the same
underlying "black until decode finishes" behavior in `_spApplyUrlToSlot`/`_spLoadEntry`
(single-page mode's own direct slot-painting path, called at swipe time, which has no
predecode step of its own). Judged a much narrower edge case than ordinary fast reading,
which is what the recording showed, so deliberately left as-is rather than expanding scope.

**NOT YET TESTED** — the NAS is currently down (see troubleshooting notes elsewhere in this
session; SSH stopped responding, then went to a full network dropout after two hard
power-cycles) so this couldn't be verified live. **To test once the server is back up**:
reproduce the original bug first if possible (fast swipe/scroll on loose-image content,
watch for the black edge flash), then confirm it's gone/reduced after this fix, in both
single-page mode and long-strip mode, on real loose-image manga. The Playwright setup from
the single-page-mode rebuild session (see above) is the established way to reproduce
timing-sensitive reader bugs like this one if manual testing doesn't clearly confirm it.

## Auto-reload-on-permission-change interrupting an active read — fix applied, NOT YET TESTED (2026-07-28)

Raised as a hypothetical ("should a forced refresh after a recheck exclude the reader?") but
traced to a real gap in an already-shipped feature, not a new one. There is no existing
"force every user to hard-reload after a recheck/rescan" mechanism — what's shipped today
(`pollScanForChanges` in `static/app.js`) is a soft AJAX re-fetch scoped only to the
manga-list page, which already covers "see new manga immediately" without a heavier
broadcast, and never touches the reader at all (the reader doesn't load `app.js`).

The real risk turned out to be a different, already-shipped mechanism: `static/api.js`'s
`checkAuthState` (see "Security audit" section above — added so a changed permission/role/
session takes effect on an already-open page, not just the next full load) polls
`/api/auth/me` every 20s on **every** page, including the reader, and calls
`window.location.reload()` unconditionally on any change — no exclusion for a page with
meaningful in-memory state. A hard reload doesn't leave the conveyor's buffer *permanently*
blank (it's a full reboot — the reader re-fetches and rebuilds from scratch via the same
boot path as any fresh page load), but it does throw away the materialized slots/exact
scroll position and land back at the last **saved** checkpoint instead — a real, disruptive
interruption mid-read, not a cosmetic one.

**Fix applied** (`static/api.js`): `checkAuthState` now returns immediately, before either
the fetch or any reload, whenever `location.pathname` matches the reader's URL shape
(`/manga/{lib}/{id}/chapter/...` or `/manga/{lib}/{id}/volume/...` — covers both the
image-based reader and the EPUB reader, since both are served under those same two route
patterns). Deliberately safe to defer rather than needing some catch-up mechanism: server-side
`can_access_library`/`is_manga_blocked` enforcement already applies to every actual content
request regardless of what this client-side poll does, so skipping it only delays when the
*UI* catches up, never what the server actually allows — and the moment the user navigates
away from the reader for any reason, that's a real navigation with a fresh script context,
which picks up current auth state for free with no special resumption logic needed.
Syntax-checked clean with `node --check`.

**NOT YET TESTED** — same reason as the entry above (NAS down). **To test once the server is
back up**: with two accounts/tabs, start reading a manga in long-strip mode as a regular
user, then from an admin session change that user's permissions (blocked tags, library
access, or role) and confirm the reader tab does NOT reload/interrupt — then navigate away
and confirm the change *has* taken effect on the next page (manga list should reflect the
new permissions). Also worth confirming the pre-existing behavior is unchanged on every
*other* page (settings, manga list, collections, etc. should still reload promptly on a
permission change, exactly as before this fix).

## Long-strip mode: pages going low-res every 15th page, periodic across chapter boundaries — REPORTED, root cause NOT YET FOUND, no code changed

Reported live-testing bug, distinct from the two entries above and from "Bug 1 — visible
seams between images" in the 2026-07-25 write-up (that one is still open with no code
change; this is a different symptom the user associates with the same investigation
session, but it's a separate bug). Investigated at length via git history this session —
**no cause identified yet, nothing reverted, no code changed for this entry.**

**Symptom, in the user's own words/clarifications:**
- Long-strip mode, on a **loose-image** manga (not archive/PDF/EPUB).
- Happens **starting cold from page 1** — not tied to switching from single-page mode
  mid-session, not tied to any particular scroll speed.
- Roughly every **exactly 15 pages**, images degrade to a very low-res/incompletely-loaded
  appearance (not a blank/broken placeholder — a real but low-quality image, as if stuck on
  a thumbnail-tier placeholder rather than the full-res page).
- Tapping the current page number on the segmented progress bar force-reloads that region
  at full res — but the *same* periodicity reappears 15 pages later.
- **The 15-page period is continuous across chapter boundaries, not reset per chapter** —
  e.g. if the low-res state last appeared 2 pages before a chapter's end, it reappears
  exactly on page 13 of the *next* chapter (2 + 13 = 15). This means whatever's causing it
  is tracked against a running/global position, not anything that resets at a chapter edge.
- Happening on a **different manga** than the one in the original 2026-07-25 bug reports —
  rules out "one specific corrupt/unusual file" as an explanation.
- The reader Settings' preload-radius slider is **not even shown** for loose-image content
  (`preloadWrap.style.display = isCompressed ? '' : 'none'` in `chapter_reader.html` — only
  archive/PDF/EPUB sources show it), so this isn't a user-adjusted setting; whatever radius
  applies is the hardcoded default.
- The user recalls "15" possibly matching a buffer/window size, but a direct check found
  **no matching constant** — long-strip's own materialized-slot window (`SLOT_COUNT`) is 80,
  not 15; the only literal `15` in the file is single-page mode's unrelated
  velocity-scaled prefetch cap (`scaledT1 = Math.round(Math.min(15, ...))` in
  `_spSchedulePrefetch`), which only ever applies while actively in single-page mode. Worth
  keeping in mind as a real discrepancy, not dismissing the "15" as necessarily literal —
  could still be a coincidence of content page-height vs. viewport (`HIGH_PRI_AHEAD` in
  `recenterConveyor` scales with `viewH`/`avgPageH`), or a genuinely separate mechanism not
  yet identified.

**What was checked and ruled out this session** (see the two entries above for the
unrelated fixes made in the same conversation): every commit touching `chapter_reader.html`
or `main.py` between 2026-07-25 evening and 2026-07-27 (10 commits — the six single-page-mode
rebuild commits, `add missing Cache-Control header to loose chapter page routes`, `fix
loose-image volumes serving broken pages in the reader`, plus two EPUB-only commits) was
diff-reviewed by hunk line range against long-strip's shared loading path (`loadEntry`,
`loadEntryTier1`, `_applyUrlToSlot`, `TIER1_RADIUS`/`TIER2_RADIUS`, `prefetchAroundIndex`,
`_shiftConveyorWindow`, `recenterConveyor`). Only one shared-code touch was found
(`8bc2ecf` renaming `_upgradeSpImage`→`_upgradeSpSlot` inside `loadEntryTier1`) and it's
purely a single-page-mode-target adaptation with no effect on long-strip's own behavior.
**No revert was made — there is no confirmed single commit to point at**, and reverting
blind risks silently undoing one of the already-verified fixes from that same window (the
July 25 stuck-low-res fix itself, or the loose-volume serving fix).

**Next step, deferred until the server is reachable again**: set up Playwright (already
installed locally from the single-page-mode rebuild session) against a real or synthetic
loose-image manga, read cold in long-strip mode from page 1, and confirm the exactly-15
periodicity live — then instrument at the moment a page goes low-res (materialized window
bounds, `entry.loaded`, `dataset` markers, `HIGH_PRI_AHEAD`/`HIGH_PRI_BEHIND` values,
browser dev-tools network panel for per-origin connection-limit contention) to find the
actual mechanism, since static diffing across the commit range didn't turn up a clear cause.

## Long-strip mode's "Padding between images" toggle never actually did anything — fix applied, NOT YET TESTED (2026-07-28)

Reported: the padding switch in long-strip mode's reader settings has no visible effect at
all, in either direction.

Root cause: `PAGE_GAP` (`templates/chapter_reader.html`, `const PAGE_GAP = 16`) was only
ever added into the *virtual* scroll-position math — `buildManifest`/`recomputeGeometry`'s
`cursorY += scaledH + (usePadding ? PAGE_GAP : 0)` / `y += entry.scaledH + (usePadding ?
PAGE_GAP : 0)` — which feeds `globalY`/`cursorY`/spacer sizing/scroll-thumb math. Nothing
anywhere ever applied a matching real gap to the actual rendered `.page-slot` elements:
each slot's DOM height is set to exactly `scaledH` with no margin, and the `.page-slot` CSS
rule had no spacing of its own. So toggling the setting genuinely did recompute geometry and
rebuild the conveyor (real work, not a no-op) — it just never changed what was visually on
screen, since consecutive slots always rendered flush against each other regardless of the
setting. Confirmed via `git log -S "PAGE_GAP"` this has been broken since the very first
commit, not a regression from any later change — nobody had apparently noticed until now.
(This is a different bug from single-page mode's own `SP_GAP`, which *is* rendered
correctly via inner slot padding — long-strip's gap and single-page's gap have always been
deliberately independent, per the comments already in the single-page-mode rebuild code.)

**Fix applied**: added a `--page-gap` CSS custom property, referenced by `.page-slot`'s
existing rule (`margin-bottom: var(--page-gap, 0px)`), and a small `applyPageGapVar(usePadding)`
helper that sets it on `readerInner` — called once at initial manifest build and again
inside `applyPaddingSetting()` (the existing toggle handler), so the visible gap now tracks
the same `usePadding` flag the geometry math already used. No change to the geometry
functions themselves — they were already correct, just never had a rendering counterpart.
Syntax-checked clean with `node --check`.

**NOT YET TESTED** — same reason as the other entries in this session (NAS down). **To test
once the server is back up**: toggle "Padding between images" in long-strip mode's reader
settings and confirm a real, visible gap appears/disappears between pages immediately (the
existing toggle handler already re-anchors scroll position via `setVirtualY`, so also
confirm the reading position doesn't visibly jump when toggling), and confirm scrolling
through a full chapter shows a consistent gap throughout, not just near the toggle point.
