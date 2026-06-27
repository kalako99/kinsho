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

## Upcoming work (ordered easiest → most complex)

### 1. Backdrop toggle switches

Add two independent on/off switches in the Settings page to show/hide the backdrop image on:
- The manga list page (`manga_list.html` / `app.js`)
- The manga detail / volume detail page (`manga_detail.html` + `volume_detail.html` share the same backdrop behaviour)

These should be per-user preferences stored in `{data_path}/{username}.json`. Pure UI/CSS — no scanning or data-model changes.

### 2. Rename "Themes" → "Accent Colors" + add a real Theme system

Two separate concepts going forward:

- **Accent Colors** — rename the existing color-switching feature (currently called "themes" in `get_theme_css`, `data.json`, and the settings UI). Keep the five built-in palettes exactly as they are; only rename the label everywhere.
- **Theme** (new) — a system that changes visual style (shapes, spacing, borders, shadows, fonts), not colors. Start with two options:
  - **Default** — the current CSS as-is (no visual change for existing users).
  - **AniList** — a style inspired by the AniList website (card shapes, button styles, list layouts, typography).

The active theme injects a CSS class (e.g. `data-theme="anilist"`) on `<body>` or `<html>` so theme-specific rules can live in `static/style.css` under scoped selectors. Store the chosen theme name alongside `active_theme` in the per-user JSON.

### 3. Custom theme with CSS editor popup

Extends the Theme system from step 2 with a third option: **Custom**.

When the user selects Custom, a modal/popup opens containing:
- A full-page `<textarea>` (or lightweight code editor) pre-populated with the CSS of whichever built-in theme is currently active, so users start from a known baseline.
- CSS blocks separated by clear comment dividers per page/view (e.g. `/* ---- Page: Home ---- */`). `manga_detail.html` and `volume_detail.html` share one block.
- A **Name** text field so users can save multiple named custom themes.
- A **Save** button — persists the CSS to `{data_path}/{username}.json` (key `custom_themes: { name: cssString }`) and applies it immediately via a `<style>` tag injected into `<head>`.
- A **Reset to Default / Reset to AniList** button to reload the baseline CSS into the editor without saving.

Invalid CSS must not crash the app — browsers handle bad CSS gracefully; just inject whatever the user wrote and let the browser ignore broken rules.

### 4. Finish the Fetch Metadata feature

`metadata_fetch.py` (currently untracked) and `METADATA_FETCH_IMPLEMENTATION.md` contain a partially built metadata-fetching system. Read those files before starting. The goal is to wire it into the existing manga detail UI so users can search for a manga on an external source (e.g. AniList/MangaDex), preview the returned metadata (cover, description, tags, genres), and apply it with one click — saving into `dims.json` via the existing `save_description` / `add_tag` / `add_genre` API routes.

---

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
