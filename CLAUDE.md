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

---

## Security audit findings — auth.py & permissions (2026-07-02)

Audit of `auth.py` and the permission-enforcement paths in `main.py`. Findings are
ordered by severity. These are documented, not yet fixed.

### Critical

1. **Per-library permissions are not enforced on any data/content API (broken access control / IDOR).**
   The only place a user's `libraries` permission map is applied is the home-page tab
   list in `manga_list` (`main.py:3220-3223`) — it just hides tabs. Every actual data
   endpoint ignores it: `/api/mangas/{library_id}`, `/api/mangas/{library_id}/search`,
   `/api/manga/{library_id}/{manga_id}`, `.../chapters`, `.../volumes`, `.../dims`, and
   all page/thumbnail routes. Any logged-in user can read a library they were denied by
   simply requesting its `library_id` directly. Library visibility is cosmetic only.

2. **Blocked-tag filtering is bypassable at the content level.**
   `blocked_tags` is applied in `get_mangas`, `get_mangas_for_search`, and `get_manga`,
   but the reader/content endpoints (`get_chapters`, `get_manga_dims`, `get_volumes`,
   `get_volume_pages`, `get_volume_page`, `get_chapter_pages`, `get_chapter_page`, and
   the thumbnail routes) take no `request` and do no tag check. A user blocked from a tag
   can still open and read the pages of a blocked manga by hitting the reader endpoints
   directly with the manga_id.

3. **Username is never sanitized → path traversal / arbitrary JSON file overwrite.**
   `route_register` only checks length ≥ 3, a small reserved set, and uniqueness — no
   character validation. `_user_data_file` builds `os.path.join(data_path, f"{username}.json")`,
   so a username like `../../evil` (or any name with path separators) causes
   `save_user_data` to write outside `data_path`. Registering the username `permissions`
   (not in the reserved set) makes the per-user file collide with and clobber
   `permissions.json`. The `reserved` set (`{"data","bootstrap","sessions","users"}`) is
   incomplete and does not address traversal.

### High

4. **Weak password hashing.** `_hash_password` is a single round of HMAC-SHA256 with one
   hardcoded global salt (`b"kinsho_salt_v1"`) baked into source. No per-user salt and no
   slow KDF (bcrypt/scrypt/argon2/PBKDF2). Hashes are fast to brute-force, identical
   passwords yield identical hashes (visible in `users.json`), and one precomputed table
   works against every Kinsho install because the salt is public.

5. **Default `admin`/`admin` account is never forced to change.** `_ensure_admin_exists`
   creates it and only prints a console warning. Anyone reaching a fresh instance before
   the owner sets a password gets full admin.

6. **Open self-registration.** `/api/auth/register` is public. Any unauthenticated
   visitor can create a `user` account, and — because of finding #1 — that account
   immediately has full read access to every library. If self-registration isn't
   intended for a personal deployment, this is a direct foothold.

7. **Cover images are served without authentication.** The `/covers` mount
   (`main.py:130-137`) is a plain `StaticFiles` mount and is not covered by the
   `/api/*` auth gate. Cover art for any manga in any library — including libraries a
   user is denied and manga behind blocked tags — is readable by anyone with the URL,
   no login required.

### Medium / low

8. **Non-constant-time password comparison.** Login (`auth.py:311`) and change-password
   (`auth.py:414`) compare hashes with `!=` instead of `hmac.compare_digest`, leaking a
   timing side-channel.

9. **Password change does not invalidate existing sessions.** `route_change_password`
   updates the hash but leaves all `sessions.json` tokens valid, so a stolen session
   survives a password reset. There is also no "log out everywhere" path.

10. **`route_set_user_permissions` does not validate the target username.** It writes
    `perms_data[username] = perms` for any string (admin-only, so lower severity), so a
    typo or crafted name silently creates ghost/`_default`-shadowing entries.

11. **Permissive CORS.** `allow_origins=["*"]` with `allow_methods=["*"]`
    (`main.py:74-80`). Mitigated by `allow_credentials=False` (cookies aren't readable
    cross-origin), but any origin can call the API with a stolen `X-Auth-Token`.

### Remediation direction (decided)

Owner decisions that constrain how the above get fixed:

- **Denied libraries and blocked tags must be a total lockout — never visible or
  reachable by that user, through any route.** Hiding tabs is not enough. Enforcement
  has to move down into the data/content layer, not the page render:
  - Add a single server-side authorization helper (e.g. `can_access_library(username,
    library_id)` and `is_manga_blocked(username, library_id, manga_name)`) in `auth.py`
    or a shared module, driven by `resolve_permissions`.
  - Call it at the top of **every** endpoint that takes a `library_id` or `manga_id`:
    `get_mangas`, `get_mangas_for_search`, `get_manga`, `get_chapters`, `get_manga_dims`,
    `get_volumes`, `get_volume_pages`, `get_volume_page`, `get_chapter_pages`,
    `get_chapter_page`, both thumbnail routes, favourites/lists/bookmarks/reading-progress,
    and the page routes `manga_detail`, `volume_reader`, `chapter_reader`,
    `category_list_page`. Return 404 (not 403) for denied libraries/blocked manga so their
    existence isn't confirmed.
  - Content endpoints that currently take no `request` (finding #2) must be changed to
    accept `request: Request` and resolve the user so the check can run.
  - Cover images (finding #7) leak the same content: covers for denied libraries / blocked
    manga must also be gated. Replace the plain `/covers` `StaticFiles` mount with an
    authenticated route that resolves the user and applies the same
    `can_access_library` / `is_manga_blocked` check before serving the file.
  - Blocked tags apply per-manga: a manga carrying any blocked tag is treated as
    non-existent for that user everywhere, exactly like a denied library.

- **User accounts are created by an admin only — remove open self-registration.**
  - Remove/disable the public `POST /api/auth/register` route (finding #6), or gate it
    behind `require_admin`. Add an admin-only "create user" flow instead (an admin endpoint
    that sets username, initial password, role, and initial permissions).
  - Keep the `admin`/`admin` bootstrap only for first run, but treat finding #5 as still
    open — the default admin password should be force-changed on first login.
  - When adding the admin create-user path, fix finding #3 at the same time: validate the
    username against a strict charset (e.g. `^[a-z0-9_-]{3,32}$`), reject path separators
    and `..`, and expand the reserved set to cover every JSON filename under `data_path`
    (`data`, `bootstrap`, `sessions`, `users`, `permissions`).

These are direction notes, not yet implemented. Findings #4, #8, #9, #10, #11 remain
open and are not blocked by the two decisions above.

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
