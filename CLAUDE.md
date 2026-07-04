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

## Deployment goal

The final target is to run Kinsho as a Docker container on an Ubuntu server and serve a personal manga/book library from it.

The `Dockerfile` already exists. Steps to reach this:

```bash
# On the Ubuntu server
docker pull <image>          # or build from source
docker run -d \
  --name kinsho \
  -p 8000:8000 \
  -v /path/to/manga:/manga \
  -v /path/to/kinsho-data:/data \
  kinsho
```

Key considerations:
- `unrar-free` is already installed in the Dockerfile for CBR support.
- The library path (`/manga`) and data path (`/data`) should be separate bind mounts so data survives container recreation.
- After first run, set the data path to `/data` and the library path(s) to wherever manga volumes are mounted, via the Settings page.
