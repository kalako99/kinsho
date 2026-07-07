from fastapi import FastAPI, Request, BackgroundTasks, Query
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse, FileResponse
from pydantic import BaseModel
import uvicorn
import json
import os
import re
import time
import random
import shutil
import zipfile
import io
import uuid
from PIL import Image
from typing import List, Optional
from datetime import datetime
import asyncio
import threading
from fastapi.responses import Response
from urllib.parse import quote, urlparse
import auth
import httpx
import metadata_fetch
import opds
import comicinfo
import integrity
from fastapi.middleware.cors import CORSMiddleware

try:
    import rarfile
    RAR_SUPPORT = True
except ImportError:
    RAR_SUPPORT = False

try:
    import pymupdf
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

try:
    import ebooklib
    from ebooklib import epub as ebooklib_epub
    EPUB_SUPPORT = True
except ImportError:
    EPUB_SUPPORT = False

from contextlib import asynccontextmanager
import socket

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

@asynccontextmanager
async def lifespan(app):
    auth._ensure_admin_exists()
    bg_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backgrounds")
    if os.path.exists(bg_folder):
        app.mount("/backgrounds", StaticFiles(directory=bg_folder), name="backgrounds")
    ip = get_local_ip()
    print(f"\n  Kinsho running at: http://{ip}:8000\n")
    task = asyncio.create_task(periodic_library_rescan())
    integrity_task = asyncio.create_task(run_integrity_check_loop())
    yield
    task.cancel()
    integrity_task.cancel()

app = FastAPI(lifespan=lifespan)

# The native app (Capacitor, Android) is a WebView that stays on its own
# local origin and makes cross-origin fetches to whatever server IP the user
# enters (see static/api.js / templates/login.html) — it never runs on the
# server's own origin, so it needs an explicit CORS allowance. Capacitor's
# default Android WebView origin is https://localhost; http://localhost and
# capacitor://localhost are included for older/alternate scheme configs.
# Browser-served mode doesn't need CORS at all (same-origin relative fetches).
NATIVE_APP_ORIGINS = [
    "https://localhost",
    "http://localhost",
    "capacitor://localhost",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=NATIVE_APP_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-Auth-Token"],
)

# ── AUTH GATE ───────────────────────────────────────────────────────────────
# Every /api/* endpoint requires a valid session, EXCEPT the auth endpoints
# under /api/auth/ (login/me/logout/change-password each enforce their own
# rules — there is no public register; accounts are admin-only, created via
# POST /api/admin/users). Anonymous API requests get a 401 here rather than
# being silently served as admin. Page routes handle their own /login
# redirect; static assets (/static) are just JS/CSS and are not gated. Cover
# images (/covers) are their own authenticated route (see get_cover_image)
# rather than a plain mount.
PUBLIC_API_PREFIXES = ("/api/auth/",)

last_activity_ts: float = time.time()

@app.middleware("http")
async def require_auth_for_api(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not path.startswith(PUBLIC_API_PREFIXES) and request.method != "OPTIONS":
        # Session/token traffic (the web/native app) counts as "someone is
        # using this" for the integrity-check idle gate; Basic Auth (OPDS
        # clients polling in the background) doesn't — an automated reader
        # checking for new chapters shouldn't block the idle-only scan from
        # ever running.
        session_user = auth.get_current_user(request)
        if session_user is not None:
            global last_activity_ts
            last_activity_ts = time.time()
        elif auth.get_user_from_basic_auth(request) is None:
            return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return await call_next(request)

@app.middleware("http")
async def default_no_store(request: Request, call_next):
    """
    Mark every response that doesn't declare its own caching policy as
    no-store. Page routes and redirects reflect LOGIN STATE — a cache layer
    replaying them stale is catastrophic: the Android app's WebView once
    cached the logged-out "/" redirect + login page and served them forever,
    making login impossible no matter what the server did. Routes that WANT
    caching (page images, dims, thumbnails) already send explicit
    Cache-Control headers, which this leaves untouched; /static is skipped
    so its ETag/Last-Modified revalidation keeps working for JS/CSS.
    """
    response = await call_next(request)
    if "cache-control" not in response.headers and not request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store"
    return response

def is_idle(threshold_seconds: int = 1200) -> bool:
    """No session/token-authenticated request in the last `threshold_seconds` (default 20 min)."""
    return (time.time() - last_activity_ts) >= threshold_seconds

app.mount("/static", StaticFiles(directory="static"), name="static")

def get_covers_dir():
    data_path = get_data_path()
    if not data_path:
        return None
    covers = os.path.join(data_path, "covers")
    os.makedirs(covers, exist_ok=True)
    return covers

def get_thumbs_dir(library_id: int, manga_name: str, source_id: str) -> Optional[str]:
    """Return the thumbs directory for a specific volume or chapter."""
    covers_dir = get_covers_dir()
    if not covers_dir:
        return None
    thumbs = os.path.join(covers_dir, str(library_id), manga_name, "thumbs", source_id)
    os.makedirs(thumbs, exist_ok=True)
    return thumbs

# Tracks thumb extraction progress: key = (library_id, manga_id, source_id)
# value = {"total": int, "done": int, "running": bool}
_thumb_progress: dict = {}
_thumb_progress_lock = threading.Lock()

_scan_running: set = set()  # library_ids currently being scanned

templates = Jinja2Templates(directory="templates")

# ── COVER IMAGES ──
# Served through an authenticated route rather than a plain StaticFiles mount:
# cover art for a denied library / blocked manga is the same content leak as
# the JSON endpoints, just as image bytes instead of text.
@app.get("/covers/{library_id}/{manga_name}/{filename}")
def get_cover_image(request: Request, library_id: int, manga_name: str, filename: str):
    username = auth.get_opds_user(request)
    if username is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Not found"}, status_code=404)

    lib_manga_data = load_app_data().get("manga_data", {}).get(str(library_id))
    manga = next((m for m in (lib_manga_data or {}).get("mangas", []) if m.get("name") == manga_name), None)
    if manga and auth.is_manga_blocked(username, load_manga_dims(library_id, manga_name).get("tags", [])):
        return JSONResponse({"error": "Not found"}, status_code=404)

    covers_dir = get_covers_dir()
    if not covers_dir:
        return JSONResponse({"error": "Not found"}, status_code=404)

    covers_root = os.path.realpath(covers_dir)
    manga_dir   = os.path.realpath(os.path.join(covers_root, str(library_id), manga_name))
    if os.path.commonpath([covers_root, manga_dir]) != covers_root:
        return JSONResponse({"error": "Not found"}, status_code=404)
    file_path = os.path.realpath(os.path.join(manga_dir, filename))
    if os.path.commonpath([manga_dir, file_path]) != manga_dir:
        return JSONResponse({"error": "Not found"}, status_code=404)
    if not os.path.isfile(file_path):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(file_path)

# ── DATA MODELS ──
class Library(BaseModel):
    id: int
    name: str
    path: Optional[str] = None
    paths: Optional[List[str]] = None

class AppData(BaseModel):
    data_path: str
    libraries: List[Library] = []

# ── DATA.JSON HELPERS ──
# data_path is stored separately in a bootstrap file so we know where to find data.json
BOOTSTRAP_FILE = "bootstrap.json"

def get_data_path() -> Optional[str]:
    if not os.path.exists(BOOTSTRAP_FILE):
        return None
    with open(BOOTSTRAP_FILE, "r") as f:
        return json.load(f).get("data_path")

def get_data_file() -> Optional[str]:
    data_path = get_data_path()
    if not data_path:
        return None
    return os.path.join(data_path, "data.json")

def load_app_data() -> dict:
    data_file = get_data_file()
    if not data_file or not os.path.exists(data_file):
        return {"data_path": get_data_path() or "", "libraries": []}
    with open(data_file, "r") as f:
        return json.load(f)

def save_app_data(data: dict):
    data_file = get_data_file()
    if not data_file:
        return
    os.makedirs(os.path.dirname(data_file), exist_ok=True)
    with open(data_file, "w") as f:
        json.dump(data, f, indent=2)

# ── MANGA DIMS FILE HELPERS ──
def get_manga_dims_file(library_id: int, manga_name: str) -> Optional[str]:
    covers_dir = get_covers_dir()
    if not covers_dir:
        return None
    return os.path.join(covers_dir, str(library_id), manga_name, "dims.json")

def load_manga_dims(library_id: int, manga_name: str) -> dict:
    path = get_manga_dims_file(library_id, manga_name)
    if not path or not os.path.exists(path):
        return {"chapters": {}, "tags": [], "genres": [], "description": ""}
    with open(path, "r") as f:
        return json.load(f)

def _is_manga_id_blocked(username: str, library_id: int, manga_id: str) -> bool:
    """
    Whether the manga behind this id carries one of the user's blocked tags.
    False (not blocked) if the manga can't be resolved — endpoints that only
    key off manga_id (bookmarks, reading history) already treat an unknown id
    as "nothing there" rather than an error.
    """
    manga_data = load_app_data().get("manga_data", {}).get(str(library_id))
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None) if manga_data else None
    if not manga:
        return False
    dims = load_manga_dims(library_id, manga["name"])
    return auth.is_manga_blocked(username, dims.get("tags", []))

def save_manga_dims(library_id: int, manga_name: str, data: dict):
    path = get_manga_dims_file(library_id, manga_name)
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# ── SCANNING ──

def natural_sort_key(s):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', s)]

def make_id(name: str) -> str:
    import hashlib
    return hashlib.sha1(name.encode('utf-8')).hexdigest()[:10]

def is_chapter_folder(name: str) -> bool:
    return bool(re.search(r'chapter', name, re.IGNORECASE))

ARCHIVE_EXTENSIONS = {'.cbz', '.cbr', '.zip', '.rar'}
IMAGE_EXTENSIONS   = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}

def is_archive(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in ARCHIVE_EXTENSIONS

def is_pdf(name: str) -> bool:
    return os.path.splitext(name)[1].lower() == '.pdf'

def is_epub(name: str) -> bool:
    return os.path.splitext(name)[1].lower() == '.epub'

def open_archive(path: str):
    """Return an open zipfile.ZipFile or rarfile.RarFile, or None on failure."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in {'.cbz', '.zip'}:
            return zipfile.ZipFile(path, 'r')
        elif ext in {'.cbr', '.rar'}:
            if not RAR_SUPPORT:
                print(f"[Archive] rarfile not installed, cannot open {path}")
                return None
            return rarfile.RarFile(path, 'r')
    except Exception as e:
        print(f"[Archive] Failed to open {path}: {e}")
    return None

def list_archive_images(arc) -> list:
    """Return natural-sorted list of image entry names inside an open archive."""
    try:
        names = arc.namelist()
    except Exception:
        return []
    images = [n for n in names if os.path.splitext(n)[1].lower() in IMAGE_EXTENSIONS]
    images.sort(key=lambda n: natural_sort_key(n))
    return images

def read_archive_entry_bytes(arc, entry_name: str) -> bytes | None:
    """Read raw bytes of one entry from an open archive."""
    try:
        return arc.read(entry_name)
    except Exception as e:
        print(f"[Archive] Failed to read entry {entry_name}: {e}")
        return None

def checkable_items_for_manga(dims: dict) -> list:
    """
    Build the ordered list of {"item_type", "item_id", "item_name",
    "source_path", "source_type"} for a manga's chapters/volumes — volumes
    take priority over chapters (a manga has one or the other, never both),
    sorted the same way the rest of the app orders them. Skips pdf/epub
    (both ComicInfo.xml reading and the integrity checker are a CBZ/CBR +
    loose-folder convention only — used by both find_comicinfo_for_manga()
    and the integrity-check background loop).
    """
    volumes = dims.get("volumes", {})
    chapters = dims.get("chapters", {})
    items = []
    if volumes:
        for vid, v in sorted(volumes.items(), key=lambda kv: natural_sort_key(kv[1].get("name", ""))):
            vol_type = v.get("source", "archive")
            if vol_type in ("archive", "loose") and v.get("path"):
                items.append({
                    "item_type": "volume", "item_id": vid, "item_name": v.get("name", vid),
                    "source_path": v["path"], "source_type": vol_type,
                })
    elif chapters:
        for cid, c in sorted(chapters.items(), key=lambda kv: natural_sort_key(kv[1].get("name", ""))):
            if c.get("path"):
                source_type = "archive" if c.get("source") == "archive" else "loose"
                items.append({
                    "item_type": "chapter", "item_id": cid, "item_name": c.get("name", cid),
                    "source_path": c["path"], "source_type": source_type,
                    # case1: this chapter's subfolder inside the shared
                    # archive — scopes the integrity check to the chapter,
                    # same as thumbnails/page-serving use it. Empty for
                    # case3 (one archive per chapter) and loose.
                    "prefix": c.get("prefix", ""),
                })
    return items

def find_comicinfo_for_manga(manga_path: str, dims: dict) -> dict | None:
    """
    A ComicInfo.xml sitting directly in the manga's own folder (next to the
    cover, sibling to the chapter/volume subfolders) is treated as
    authoritative for the whole series and takes priority — it's a
    deliberate one-file-for-the-series choice, so there's no need to
    intersect it against individual chapters. Falls back to aggregating one
    from each chapter/volume when there's no manga-level file.

    manga_path may be a single archive file rather than a folder (a whole
    manga stored as one CBZ with chapter subdirectories inside it) — skip
    the root-level check in that case, since there's no sibling folder to
    look in; the per-chapter aggregation already reads that same archive.
    """
    if os.path.isdir(manga_path):
        root_level = comicinfo.locate_and_read(manga_path, "loose", open_archive, read_archive_entry_bytes)
        if root_level:
            return root_level
    ordered_items = checkable_items_for_manga(dims)
    if not ordered_items:
        return None
    return comicinfo.aggregate_for_manga(ordered_items, open_archive, read_archive_entry_bytes)

# ── INTEGRITY CHECK (corrupt archives / duplicate pages) ────────────────────
# A background-only, idle-gated pass — see is_idle() near the top of this
# file and run_integrity_check_loop() further down. integrity_issues.json is
# global (admin-facing), not per-user, same storage pattern as collections.json.

INTEGRITY_RECHECK_SECONDS = 90 * 24 * 60 * 60  # ~3 months

def _integrity_issues_file() -> Optional[str]:
    data_path = get_data_path()
    if not data_path:
        return None
    return os.path.join(data_path, "integrity_issues.json")

def load_integrity_issues() -> dict:
    path = _integrity_issues_file()
    if not path or not os.path.exists(path):
        return {"issues": []}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {"issues": []}

def save_integrity_issues(data: dict):
    path = _integrity_issues_file()
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def _clear_issues_for_item(issues_data: dict, library_id: int, manga_id: str, item_type: str, item_id: str):
    issues_data["issues"] = [
        i for i in issues_data["issues"]
        if not (i["library_id"] == library_id and i["manga_id"] == manga_id
                and i["item_type"] == item_type and i["item_id"] == item_id)
    ]

def record_integrity_result(
    library_id: int, manga_id: str, manga_name: str, item: dict, result: dict
) -> None:
    """
    Replaces any existing findings for this exact chapter/volume with whatever
    this check just found (empty result = the item is now clean, which is
    exactly how a Recheck clears a fixed issue). One issue per duplicate-page
    group, so each row in the admin list is one concrete problem.
    """
    issues_data = load_integrity_issues()
    _clear_issues_for_item(issues_data, library_id, manga_id, item["item_type"], item["item_id"])
    now = datetime.now().isoformat()

    if result["corrupt"]:
        issues_data["issues"].append({
            "id": uuid.uuid4().hex,
            "library_id": library_id, "manga_id": manga_id, "manga_name": manga_name,
            "item_type": item["item_type"], "item_id": item["item_id"], "item_name": item["item_name"],
            "type": "corrupt", "detail": result["corrupt"], "filenames": [],
            "detected_at": now,
        })
    for group in result.get("duplicate_groups", []):
        filenames = group["filenames"]
        similarity = group.get("similarity", 1.0)
        if similarity >= 1.0:
            detail = f"{len(filenames)} identical pages: {', '.join(filenames)}"
        else:
            detail = f"{len(filenames)} near-duplicate pages ({similarity:.0%} similar): {', '.join(filenames)}"
        issues_data["issues"].append({
            "id": uuid.uuid4().hex,
            "library_id": library_id, "manga_id": manga_id, "manga_name": manga_name,
            "item_type": item["item_type"], "item_id": item["item_id"], "item_name": item["item_name"],
            "type": "duplicate_pages",
            "detail": detail,
            "filenames": filenames, "detected_at": now,
        })
    save_integrity_issues(issues_data)

def run_integrity_check_for_item(library_id: int, manga_id: str, manga_name: str, dims: dict, item: dict) -> None:
    """Runs the check, records the result, and stamps this item's dims.json entry — used by
    both the background loop (routine sweeps) and the admin Recheck action (on-demand, targeted)."""
    result = integrity.check_item(item, open_archive, read_archive_entry_bytes)
    record_integrity_result(library_id, manga_id, manga_name, item, result)
    bucket = dims.get("volumes") if item["item_type"] == "volume" else dims.get("chapters")
    if bucket is not None and item["item_id"] in bucket:
        bucket[item["item_id"]]["integrity_checked_at"] = datetime.now().isoformat()
        save_manga_dims(library_id, manga_name, dims)

# ── LRU PAGE CACHE ──
# Keyed by (archive_path, entry_name) or (pdf_path, page_index)
# Returns raw image bytes. Cache up to 64 pages in memory.
_archive_page_cache: dict = {}
_pdf_page_cache: dict = {}
_archive_image_list_cache: dict = {}

# Persistent open archive handles — reuse across requests instead of re-opening
_open_archive_handles: dict = {}

def _get_open_archive(archive_path: str):
    """Return a cached open archive handle, opening it if necessary."""
    if archive_path in _open_archive_handles:
        return _open_archive_handles[archive_path]
    arc = open_archive(archive_path)
    if arc is not None:
        _open_archive_handles[archive_path] = arc
    return arc

def _cached_archive_page(archive_path: str, entry_name: str) -> bytes | None:
    key = (archive_path, entry_name)
    if key in _archive_page_cache:
        return _archive_page_cache[key]
    arc = _get_open_archive(archive_path)
    if arc is None:
        return None
    data = read_archive_entry_bytes(arc, entry_name)
    if data is not None:
        if len(_archive_page_cache) >= 128:
            _archive_page_cache.pop(next(iter(_archive_page_cache)))
        _archive_page_cache[key] = data
    return data

def _cached_archive_image_list(archive_path: str) -> list:
    if archive_path in _archive_image_list_cache:
        return _archive_image_list_cache[archive_path]
    arc = _get_open_archive(archive_path)
    if arc is None:
        return []
    images = list_archive_images(arc)
    _archive_image_list_cache[archive_path] = images
    return images

def _get_pdf_page_cache_path(library_id: int, manga_name: str, source_id: str, page_index: int) -> Optional[str]:
    thumbs_dir = get_thumbs_dir(library_id, manga_name, source_id)
    if not thumbs_dir:
        return None
    return os.path.join(thumbs_dir, f"p{page_index}.jpg")

_open_pdf_handles: dict = {}

def _get_open_pdf(pdf_path: str):
    """Return a cached open pymupdf.Document handle, opening it if necessary."""
    if pdf_path in _open_pdf_handles:
        return _open_pdf_handles[pdf_path]
    if not PDF_SUPPORT:
        return None
    try:
        doc = pymupdf.open(pdf_path)
        _open_pdf_handles[pdf_path] = doc
        return doc
    except Exception as e:
        print(f"[PDF] Failed to open {pdf_path}: {e}")
        return None

def _cached_pdf_page(pdf_path: str, page_index: int, scale: float = 1.5) -> bytes | None:
    key = (pdf_path, page_index, scale)
    if key in _pdf_page_cache:
        return _pdf_page_cache[key]
    if not PDF_SUPPORT:
        return None
    try:
        doc  = _get_open_pdf(pdf_path)
        if doc is None:
            return None
        page = doc.load_page(page_index)
        mat  = pymupdf.Matrix(scale, scale)
        pix  = page.get_pixmap(matrix=mat, alpha=False)
        data = pix.tobytes("jpeg", jpg_quality=88)
        if len(_pdf_page_cache) >= 128:
            _pdf_page_cache.pop(next(iter(_pdf_page_cache)))
        _pdf_page_cache[key] = data
        return data
    except Exception as e:
        print(f"[PDF] Failed to render page {page_index} of {pdf_path}: {e}")
        return None
    
def get_archive_image_dims(archive_path: str, entry_name: str) -> tuple[int, int]:
    """Return (w, h) for an image inside an archive, with fallback."""
    data = _cached_archive_page(archive_path, entry_name)
    if data:
        try:
            img = Image.open(io.BytesIO(data))
            return img.size
        except Exception:
            pass
    return (800, 1100)

def get_pdf_page_dims(pdf_path: str, page_index: int) -> tuple[int, int]:
    """Return (w, h) for a PDF page without rendering it."""
    if not PDF_SUPPORT:
        return (800, 1100)
    try:
        doc  = pymupdf.open(pdf_path)
        page = doc.load_page(page_index)
        rect = page.rect
        doc.close()
        return (int(rect.width * 1.5), int(rect.height * 1.5))  # match scale=1.5
    except Exception:
        return (800, 1100)

def list_archive_subdirs(arc) -> list:
    """Return list of unique top-level directory names inside the archive."""
    dirs = set()
    try:
        for name in arc.namelist():
            parts = name.split('/')
            if len(parts) > 1 and parts[0]:
                dirs.add(parts[0])
    except Exception:
        pass
    return list(dirs)

def classify_archive(path: str) -> str:
    """
    Peek inside an archive and return its case classification:
      'case1' — contains chapter-named subfolders (directly or one level deep)
      'case3' — the archive filename (without ext) contains 'chapter'
      'case2' — volumes (no chapter structure, not named chapter)
    """
    basename = os.path.splitext(os.path.basename(path))[0]
    arc = open_archive(path)
    if arc is None:
        # Can't open: fall back on filename
        if re.search(r'chapter', basename, re.IGNORECASE):
            return 'case3'
        return 'case2'
    with arc:
        subdirs = list_archive_subdirs(arc)
        # Check direct subdirs for chapter names
        for d in subdirs:
            if re.search(r'chapter', d, re.IGNORECASE):
                return 'case1'
        # Check one level deeper (manga_name/Chapter X/ structure)
        try:
            names = arc.namelist()
        except Exception:
            names = []
        for name in names:
            parts = name.split('/')
            if len(parts) > 2 and re.search(r'chapter', parts[1], re.IGNORECASE):
                return 'case1'
    # Not case1 — check filename
    if re.search(r'chapter', basename, re.IGNORECASE):
        return 'case3'
    return 'case2'

def get_epub_image_list(epub_path: str) -> list:
    """
    Return list of (href, bytes) tuples for images in epub reading order.
    Falls back to alphabetical order if ebooklib is unavailable.
    """
    if not EPUB_SUPPORT:
        # Fallback: treat epub as zip, return image entries sorted
        arc = zipfile.ZipFile(epub_path, 'r')
        with arc:
            return sorted(
                [n for n in arc.namelist() if os.path.splitext(n)[1].lower() in IMAGE_EXTENSIONS],
                key=natural_sort_key
            )
    try:
        book = ebooklib_epub.read_epub(epub_path)
        images = []
        for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            images.append(item.get_name())
        return images
    except Exception as e:
        print(f"[EPUB] Failed to parse {epub_path}: {e}")
        return []

_epub_page_cache: dict = {}

def _cached_epub_page(epub_path: str, entry_name: str) -> bytes | None:
    key = (epub_path, entry_name)
    if key in _epub_page_cache:
        return _epub_page_cache[key]
    try:
        arc = zipfile.ZipFile(epub_path, 'r')
        with arc:
            data = arc.read(entry_name)
        if data is not None:
            if len(_epub_page_cache) >= 64:
                _epub_page_cache.pop(next(iter(_epub_page_cache)))
            _epub_page_cache[key] = data
        return data
    except Exception as e:
        print(f"[EPUB] Failed to read {entry_name} from {epub_path}: {e}")
        return None

def _invalidate_stale_source_caches(path: str):
    """
    Drop any cached open handle / image list / page bytes for this exact
    source path (archive, PDF, or EPUB). Needed whenever scan_library detects
    the underlying file's content actually changed (a page deleted/replaced
    on disk) — without this, a long-lived open handle and the per-path
    in-memory caches above keep serving whatever was read before the edit
    indefinitely (they're process-scoped, only cleared by a server restart),
    even after dims.json is correctly rebuilt with the new page count. This
    is what caused deleted pages to keep showing as broken/black even after
    a full Reload scan picked up the change on disk.
    """
    handle = _open_archive_handles.pop(path, None)
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass
    _archive_image_list_cache.pop(path, None)
    for key in [k for k in _archive_page_cache if k[0] == path]:
        _archive_page_cache.pop(key, None)

    pdf_handle = _open_pdf_handles.pop(path, None)
    if pdf_handle is not None:
        try:
            pdf_handle.close()
        except Exception:
            pass
    for key in [k for k in _pdf_page_cache if k[0] == path]:
        _pdf_page_cache.pop(key, None)

    for key in [k for k in _epub_page_cache if k[0] == path]:
        _epub_page_cache.pop(key, None)

THUMB_WIDTH = 100

def _make_thumb_bytes(img_bytes: bytes) -> Optional[bytes]:
    """Resize image bytes to THUMB_WIDTH wide, return JPEG bytes or None on failure."""
    try:
        img = Image.open(io.BytesIO(img_bytes))
        w, h = img.size
        new_h = int(h * THUMB_WIDTH / w)
        img = img.resize((THUMB_WIDTH, new_h), Image.LANCZOS)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=70, optimize=True)
        return out.getvalue()
    except Exception as e:
        print(f"[Thumbs] Failed to make thumb: {e}")
        return None

def extract_thumbs_for_source(
    library_id: int,
    manga_name: str,
    source_id: str,
    source_type: str,   # 'archive', 'pdf', 'epub'
    source_path: str,
    prefix: str = "",   # for case1 chapters inside archives
):
    """
    Extract THUMB_WIDTH-wide JPEG thumbnails for every page of a source
    (archive chapter, volume, or pdf/epub volume) into the thumbs directory.
    Skips pages whose thumb file already exists.
    Updates _thumb_progress during extraction.
    """
    key = (library_id, manga_name, source_id)
    thumbs_dir = get_thumbs_dir(library_id, manga_name, source_id)
    if not thumbs_dir:
        return

    # Build the ordered list of raw page identifiers
    if source_type == "archive":
        all_images = _cached_archive_image_list(source_path)
        if prefix:
            images = [n for n in all_images if n.startswith(prefix)]
        else:
            images = all_images
        total = len(images)
    elif source_type == "pdf":
        if not PDF_SUPPORT:
            return
        doc = pymupdf.open(source_path)
        total = len(doc)
        doc.close()
        images = list(range(total))
    elif source_type == "epub":
        images = get_epub_image_list(source_path)
        total = len(images)
    else:
        return

    with _thumb_progress_lock:
        _thumb_progress[key] = {"total": total, "done": 0, "running": True}

    done = 0
    for i, entry in enumerate(images):
        thumb_path = os.path.join(thumbs_dir, f"{i}.jpg")
        if os.path.exists(thumb_path):
            done += 1
            with _thumb_progress_lock:
                _thumb_progress[key]["done"] = done
            continue

        # Extract raw bytes
        raw = None
        try:
            if source_type == "archive":
                raw = _cached_archive_page(source_path, entry)
            elif source_type == "pdf":
                raw = _cached_pdf_page(source_path, i)
            elif source_type == "epub":
                raw = _cached_epub_page(source_path, entry)
        except Exception as e:
            print(f"[Thumbs] Error reading page {i}: {e}")

        if raw:
            thumb_bytes = _make_thumb_bytes(raw)
            if thumb_bytes:
                try:
                    with open(thumb_path, "wb") as f:
                        f.write(thumb_bytes)
                except Exception as e:
                    print(f"[Thumbs] Error saving thumb {i}: {e}")

        done += 1
        with _thumb_progress_lock:
            _thumb_progress[key]["done"] = done

    with _thumb_progress_lock:
        _thumb_progress[key]["running"] = False

    print(f"[Thumbs] Done: {manga_name}/{source_id} ({done}/{total})")

def extract_thumbs_for_loose_chapter(
    library_id: int,
    manga_name: str,
    chapter_id: str,
    chapter_path: str,
):
    """
    Extract thumbnails for every image in a loose chapter folder.
    Skips pages whose thumb file already exists.
    """
    key = (library_id, manga_name, chapter_id)
    thumbs_dir = get_thumbs_dir(library_id, manga_name, chapter_id)
    if not thumbs_dir:
        return

    try:
        images = sorted(
            [f for f in os.listdir(chapter_path)
             if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS],
            key=natural_sort_key
        )
    except Exception as e:
        print(f"[Thumbs] Cannot list loose chapter {chapter_path}: {e}")
        return

    total = len(images)
    with _thumb_progress_lock:
        _thumb_progress[key] = {"total": total, "done": 0, "running": True}

    done = 0
    for i, fname in enumerate(images):
        thumb_path = os.path.join(thumbs_dir, f"{i}.jpg")
        if os.path.exists(thumb_path):
            done += 1
            with _thumb_progress_lock:
                _thumb_progress[key]["done"] = done
            continue
        try:
            with open(os.path.join(chapter_path, fname), "rb") as f:
                raw = f.read()
            thumb_bytes = _make_thumb_bytes(raw)
            if thumb_bytes:
                with open(thumb_path, "wb") as f:
                    f.write(thumb_bytes)
        except Exception as e:
            print(f"[Thumbs] Error processing loose image {fname}: {e}")
        done += 1
        with _thumb_progress_lock:
            _thumb_progress[key]["done"] = done

    with _thumb_progress_lock:
        _thumb_progress[key]["running"] = False

    print(f"[Thumbs] Done (loose): {manga_name}/{chapter_id} ({done}/{total})")

def _prerender_pdf_volume(
    library_id: int,
    manga_name: str,
    volume_id: str,
    pdf_path: str,
    page_count: int,
    scale: float = 1.5,
):
    """
    Pre-render all pages of a PDF volume to JPEG files on disk.
    Files are named p{index}.jpg inside the thumbs directory.
    Skips pages whose file already exists.
    Runs in a background thread.
    """
    if not PDF_SUPPORT:
        return
    thumbs_dir = get_thumbs_dir(library_id, manga_name, volume_id)
    if not thumbs_dir:
        return

    print(f"[PDF] Pre-rendering {page_count} pages for {manga_name}/{volume_id}...")
    try:
        doc = pymupdf.open(pdf_path)
    except Exception as e:
        print(f"[PDF] Cannot open {pdf_path}: {e}")
        return

    mat       = pymupdf.Matrix(scale, scale)
    thumb_mat = pymupdf.Matrix(0.2, 0.2)  # ~72dpi — enough for a 100px thumb
    rendered  = 0
    for i in range(page_count):
        out_path   = os.path.join(thumbs_dir, f"p{i}.jpg")
        thumb_path = os.path.join(thumbs_dir, f"thumb_{i}.jpg")
        need_page  = not os.path.exists(out_path)
        need_thumb = not os.path.exists(thumb_path)
        if not need_page and not need_thumb:
            continue
        try:
            page = doc.load_page(i)
            if need_page:
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                img.save(out_path, format="JPEG", quality=88, optimize=True)
            if need_thumb:
                tpix  = page.get_pixmap(matrix=thumb_mat, alpha=False)
                timg  = Image.frombytes("RGB", (tpix.width, tpix.height), tpix.samples)
                # Resize to exactly THUMB_WIDTH wide
                tw, th = timg.size
                if tw > 0:
                    new_h = int(th * THUMB_WIDTH / tw)
                    timg  = timg.resize((THUMB_WIDTH, new_h), Image.LANCZOS)
                timg.save(thumb_path, format="JPEG", quality=70, optimize=True)
            rendered += 1
        except Exception as e:
            print(f"[PDF] Error rendering page {i}: {e}")
    doc.close()
    print(f"[PDF] Pre-render done: {rendered} new pages for {manga_name}/{volume_id}.")

def extract_thumbs_for_library(library_id: int):
    """
    Post-scan background task: extract thumbnails for all compressed sources in a library.
    Skips loose chapters (their images are served directly from disk, fast enough).
    Uses mtime comparison to skip unchanged sources.
    Runs all sources sequentially to avoid server overload.
    """
    print(f"[Thumbs] Starting library-wide extraction for library {library_id}...")
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id), {})
    mangas = manga_data.get("mangas", [])

    for manga in mangas:
        manga_name = manga.get("name", "")
        manga_id   = manga.get("id", "")
        if not manga_name or not manga_id:
            continue

        dims = load_manga_dims(library_id, manga_name)

        # ── CHAPTERS: compressed only ──
        for chapter_id, chapter in dims.get("chapters", {}).items():
            source_type = chapter.get("source")
            if source_type != "archive":
                continue  # skip loose chapters

            source_path  = chapter.get("path", "")
            stored_mtime = chapter.get("mtime")

            try:
                current_mtime = os.path.getmtime(source_path)
            except Exception:
                continue

            thumbs_dir  = get_thumbs_dir(library_id, manga_name, chapter_id)
            total_pages = len(chapter.get("pages", []))
            if thumbs_dir and total_pages > 0:
                existing_thumbs = len([f for f in os.listdir(thumbs_dir) if f.endswith(".jpg")])
                if existing_thumbs >= total_pages and stored_mtime == current_mtime:
                    continue  # up to date

            if thumbs_dir and stored_mtime != current_mtime:
                for f in os.listdir(thumbs_dir):
                    if f.endswith(".jpg"):
                        try:
                            os.remove(os.path.join(thumbs_dir, f))
                        except Exception:
                            pass

            print(f"[Thumbs] Extracting chapter: {manga_name}/{chapter.get('name', chapter_id)}")
            prefix = chapter.get("prefix", "")
            extract_thumbs_for_source(
                library_id, manga_name, chapter_id,
                "archive", source_path, prefix
            )

        # ── VOLUMES (case2): always compressed ──
        for volume_id, volume in dims.get("volumes", {}).items():
            source_type  = volume.get("source", "archive")
            source_path  = volume.get("path", "")
            stored_mtime = volume.get("mtime")

            try:
                current_mtime = os.path.getmtime(source_path)
            except Exception:
                continue

            thumbs_dir  = get_thumbs_dir(library_id, manga_name, volume_id)
            total_pages = len(volume.get("pages", []))

            if source_type == "pdf":
                if thumbs_dir and total_pages > 0:
                    # Count p{n}.jpg pre-rendered pages
                    existing = len([f for f in os.listdir(thumbs_dir) if f.startswith("p") and f.endswith(".jpg")])
                    if existing >= total_pages and stored_mtime == current_mtime:
                        continue
                if thumbs_dir and stored_mtime != current_mtime:
                    for f in os.listdir(thumbs_dir):
                        if f.endswith(".jpg"):
                            try:
                                os.remove(os.path.join(thumbs_dir, f))
                            except Exception:
                                pass
                print(f"[PDF] Scheduling pre-render: {manga_name}/{volume.get('name', volume_id)}")
                _prerender_pdf_volume(library_id, manga_name, volume_id, source_path, total_pages)
            else:
                if thumbs_dir and total_pages > 0:
                    existing_thumbs = len([f for f in os.listdir(thumbs_dir) if f.endswith(".jpg")])
                    if existing_thumbs >= total_pages and stored_mtime == current_mtime:
                        continue
                if thumbs_dir and stored_mtime != current_mtime:
                    for f in os.listdir(thumbs_dir):
                        if f.endswith(".jpg"):
                            try:
                                os.remove(os.path.join(thumbs_dir, f))
                            except Exception:
                                pass
                print(f"[Thumbs] Extracting volume: {manga_name}/{volume.get('name', volume_id)}")
                extract_thumbs_for_source(
                    library_id, manga_name, volume_id,
                    source_type, source_path, ""
                )

    print(f"[Thumbs] Library-wide extraction complete for library {library_id}.")

def _get_source_info(library_id: int, manga_id: str, source_id: str, is_volume: bool):
    """
    Returns (manga, source_dict, source_type, source_path, prefix) or None on any error.
    source_type is 'archive'|'pdf'|'epub'|'loose'.
    prefix is only relevant for case1 archive chapters.
    """
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return None
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return None
    dims = load_manga_dims(library_id, manga["name"])
    if is_volume:
        source = dims.get("volumes", {}).get(source_id)
        if not source:
            return None
        source_type = source.get("source", "archive")
        return (manga, source, source_type, source.get("path", ""), "")
    else:
        source = dims.get("chapters", {}).get(source_id)
        if not source:
            return None
        source_type = source.get("source")
        if not source_type:
            return None
        return (manga, source, source_type, source.get("path", ""), source.get("prefix", ""))

def find_cover_image(folder: str):
    extensions = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
    try:
        all_files = os.listdir(folder)
    except Exception as e:
        print(f"[Cover] Cannot list folder {folder}: {e}")
        return None
    images = [
        f for f in all_files
        if os.path.isfile(os.path.join(folder, f))
        and os.path.splitext(f)[1].lower() in extensions
    ]
    print(f"[Cover] Looking in: {folder} — found {len(all_files)} entries, {len(images)} images")
    if not images:
        return None
    images.sort(key=natural_sort_key)
    print(f"[Cover] Picked: {images[0]}")
    return os.path.join(folder, images[0])

def _classify_loose_folder(folder_path: str) -> dict:
    """
    Recursively inspect a folder and classify it for loose manga scanning.

    Returns a dict:
      {
        "is_manga": bool,          # True if this folder qualifies as a manga
        "manga_type": "case1"|"case2"|None,
        "content_subfolders": [...],  # subfolder names that are direct content (images-only)
        "nested_manga_paths": [...],  # absolute paths of subfolders that are themselves mangas
      }

    Rules:
    - A subfolder is "skipped" if it contains no images anywhere in its tree.
    - A subfolder is a "content subfolder" if it contains only images (and
      optionally skippable subfolders).
    - A subfolder is a "nested manga" if it contains its own image-only sub-subfolders.
    - If at least one content subfolder name contains "chapter" (case-insensitive)
      -> manga_type = "case1"
    - Otherwise -> manga_type = "case2"
    - A folder is a manga if it has at least one content subfolder after removing
      nested mangas.
    """
    def _has_images_anywhere(path: str) -> bool:
        for root, dirs, files in os.walk(path):
            for f in files:
                if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS:
                    return True
        return False

    def _contains_only_images(path: str) -> bool:
        """
        True if `path` contains at least one image and no sub-subfolders that
        themselves have images (i.e. it is a flat image-only folder, ignoring
        empty/skippable subfolders).
        """
        try:
            entries = os.listdir(path)
        except Exception:
            return False
        has_image = False
        for entry in entries:
            full = os.path.join(path, entry)
            if os.path.isfile(full):
                if os.path.splitext(entry)[1].lower() in IMAGE_EXTENSIONS:
                    has_image = True
            elif os.path.isdir(full):
                if _has_images_anywhere(full):
                    return False   # has a non-empty sub-subfolder => not flat
        return has_image

    def _has_content_subfolders(path: str) -> bool:
        """True if `path` contains at least one image-only subfolder."""
        try:
            entries = os.listdir(path)
        except Exception:
            return False
        for entry in entries:
            full = os.path.join(path, entry)
            if os.path.isdir(full) and _contains_only_images(full):
                return True
        return False

    try:
        subdirs = sorted(
            [e for e in os.listdir(folder_path)
             if os.path.isdir(os.path.join(folder_path, e))],
            key=natural_sort_key
        )
    except Exception:
        return {"is_manga": False, "manga_type": None,
                "content_subfolders": [], "nested_manga_paths": []}

    content_subfolders = []
    nested_manga_paths = []

    for dirname in subdirs:
        full_sub = os.path.join(folder_path, dirname)

        if not _has_images_anywhere(full_sub):
            continue   # skip — no images anywhere

        if _has_content_subfolders(full_sub):
            # This subfolder is itself a manga
            nested_manga_paths.append(full_sub)
        elif _contains_only_images(full_sub):
            content_subfolders.append(dirname)
        # else: has images somewhere deeper but not directly a content folder
        # and not a nested manga — treated as nested manga candidate via recursion
        # handled because nested_manga_paths are registered independently

    if not content_subfolders:
        return {"is_manga": False, "manga_type": None,
                "content_subfolders": [], "nested_manga_paths": nested_manga_paths}

    has_chapter_name = any(
        re.search(r'chapter', name, re.IGNORECASE) for name in content_subfolders
    )
    manga_type = "case1" if has_chapter_name else "case2"

    return {
        "is_manga": True,
        "manga_type": manga_type,
        "content_subfolders": content_subfolders,
        "nested_manga_paths": nested_manga_paths,
    }

def process_manga_covers(manga_path: str, library_id: int, manga_name: str, stored_mtimes: dict) -> tuple[str | None, dict]:
    extensions = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
    covers_dir = get_covers_dir()
    if not covers_dir:
        return None, stored_mtimes

    try:
        all_files = os.listdir(manga_path)
    except Exception as e:
        print(f"[Covers] Cannot list folder {manga_path}: {e}")
        return None, stored_mtimes

    images = sorted(
        [f for f in all_files
         if os.path.isfile(os.path.join(manga_path, f))
         and os.path.splitext(f)[1].lower() in extensions],
        key=natural_sort_key
    )

    manga_covers_dir = os.path.join(covers_dir, str(library_id), manga_name)
    os.makedirs(manga_covers_dir, exist_ok=True)

    # Delete covers for images no longer in the manga folder
    current_filenames = set(images)
    for stored_filename in list(stored_mtimes.keys()):
        if stored_filename not in current_filenames:
            name, ext = os.path.splitext(stored_filename)
            small_dest = os.path.join(manga_covers_dir, stored_filename)
            large_dest = os.path.join(manga_covers_dir, name + '+' + ext)
            for f in [small_dest, large_dest]:
                if os.path.exists(f):
                    try:
                        os.remove(f)
                        print(f"[Covers] Deleted orphaned cover: {f}")
                    except Exception as e:
                        print(f"[Covers] Failed to delete {f}: {e}")
            del stored_mtimes[stored_filename]

    if not images:
        return None, stored_mtimes

    new_mtimes = dict(stored_mtimes)

    for image_filename in images:
        src_path = os.path.join(manga_path, image_filename)
        name, ext = os.path.splitext(image_filename)
        small_dest = os.path.join(manga_covers_dir, image_filename)
        large_dest = os.path.join(manga_covers_dir, name + '+' + ext)

        current_mtime = os.path.getmtime(src_path)
        stored_mtime  = stored_mtimes.get(image_filename)

        both_exist = os.path.exists(small_dest) and os.path.exists(large_dest)
        if both_exist and stored_mtime is not None and current_mtime == stored_mtime:
            print(f"[Covers] Skipping unchanged: {image_filename}")
            continue

        try:
            img = Image.open(src_path)
            small = img.copy()
            small.thumbnail((300, 450))
            small.save(small_dest, optimize=True, quality=85)

            large = img.copy()
            large.thumbnail((600, 900))
            large.save(large_dest, optimize=True, quality=85)

            new_mtimes[image_filename] = current_mtime
            print(f"[Covers] Processed {image_filename} -> small + large")
        except Exception as e:
            print(f"[Covers] Failed processing {image_filename}: {e}")
            try:
                shutil.copy2(src_path, small_dest)
                shutil.copy2(src_path, large_dest)
                new_mtimes[image_filename] = current_mtime
            except Exception as e2:
                print(f"[Covers] Raw copy also failed: {e2}")

    return images[0], new_mtimes

def process_cover_from_bytes(
    img_bytes: bytes,
    filename: str,
    library_id: int,
    manga_name: str,
    stored_mtimes: dict,
    source_mtime: float,
    large_size: tuple = (600, 900),
) -> tuple[str | None, dict]:
    """
    Given raw image bytes (extracted from an archive or PDF), save small+large
    cover thumbnails to the covers directory. Returns (cover_filename, new_mtimes).
    filename is used as the key and destination filename.
    """
    covers_dir = get_covers_dir()
    if not covers_dir:
        return None, stored_mtimes

    manga_covers_dir = os.path.join(covers_dir, str(library_id), manga_name)
    os.makedirs(manga_covers_dir, exist_ok=True)

    name, ext = os.path.splitext(filename)
    small_dest = os.path.join(manga_covers_dir, filename)
    large_dest = os.path.join(manga_covers_dir, name + '+' + ext)

    stored_mtime = stored_mtimes.get(filename)
    both_exist = os.path.exists(small_dest) and os.path.exists(large_dest)
    if both_exist and stored_mtime is not None and source_mtime == stored_mtime:
        print(f"[Covers] Skipping unchanged: {filename}")
        return filename, stored_mtimes

    new_mtimes = dict(stored_mtimes)
    try:
        img = Image.open(io.BytesIO(img_bytes))
        if ext.lower() in ('.jpg', '.jpeg') and img.mode != 'RGB':
            img = img.convert('RGB')
        small = img.copy()
        small.thumbnail((300, 450))
        small.save(small_dest, optimize=True, quality=85)
        large = img.copy()
        large.thumbnail(large_size)
        large.save(large_dest, optimize=True, quality=85)
        new_mtimes[filename] = source_mtime
        print(f"[Covers] Processed {filename} -> small + large")
    except Exception as e:
        print(f"[Covers] Failed processing {filename}: {e}")

    return filename, new_mtimes

# Target width (px) for a fetched cover. AniList's largest cover tops out
# around 500px, too soft for the full-width detail backdrop — when the
# AniList source is below this, we try MangaDex for a higher-res original.
FETCHED_COVER_TARGET_WIDTH = 900
# Large-variant cap for fetched covers, raised above the default 600x900 so
# a high-res MangaDex original isn't thrown away when rescaled.
FETCHED_COVER_LARGE_SIZE = (1000, 1500)

def _image_width(img_bytes: bytes) -> int:
    """Return the pixel width of an image, or 0 if it can't be read."""
    try:
        return Image.open(io.BytesIO(img_bytes)).width
    except Exception:
        return 0

async def fetch_and_set_cover(
    library_id: int,
    manga: dict,
    anilist_candidate: dict | None,
    mangadex_candidate: dict | None,
) -> bool:
    """
    Fetch a cover image for the matched manga, process it into the manga's
    covers directory (small + large), and set it as the manga's default cover
    (manga["cover"]). User-selected covers still take priority since the
    per-user override is checked before manga["cover"] when serving.

    Source selection: AniList's cover is used by default, but if it's narrower
    than FETCHED_COVER_TARGET_WIDTH the MangaDex candidate's original-resolution
    cover is used instead when it's actually larger. Either candidate may be
    None (e.g. MangaDex-only match, or no MangaDex fallback found).

    Mutates manga["cover"] in place; the caller is responsible for persisting
    the change via save_app_data. Returns True if a cover was set.
    """
    anilist_url = None
    if anilist_candidate:
        anilist_url = (
            anilist_candidate.get("cover_url_extra_large")
            or anilist_candidate.get("cover_url_large")
            or anilist_candidate.get("cover_url_medium")
        )

    chosen_bytes = None
    chosen_url = None
    width = 0
    if anilist_url:
        try:
            chosen_bytes = await metadata_fetch.download_cover_image(anilist_url)
            chosen_url = anilist_url
            width = _image_width(chosen_bytes)
        except Exception:
            chosen_bytes = None

    if width < FETCHED_COVER_TARGET_WIDTH and mangadex_candidate:
        md_url = mangadex_candidate.get("cover_url")
        if md_url:
            try:
                md_bytes = await metadata_fetch.download_cover_image(md_url)
                if _image_width(md_bytes) > width:
                    chosen_bytes = md_bytes
                    chosen_url = md_url
            except Exception:
                pass

    if not chosen_bytes:
        return False

    url_ext = os.path.splitext(urlparse(chosen_url).path)[1].lower()
    if url_ext not in ('.jpg', '.jpeg', '.png', '.webp'):
        url_ext = '.jpg'
    filename = f"anilist_cover{url_ext}"
    # Pass empty stored_mtimes so a re-fetch always re-processes the image
    # rather than skipping it as unchanged.
    result_fname, _ = process_cover_from_bytes(
        img_bytes=chosen_bytes,
        filename=filename,
        library_id=library_id,
        manga_name=manga["name"],
        stored_mtimes={},
        source_mtime=0.0,
        large_size=FETCHED_COVER_LARGE_SIZE,
    )
    if not result_fname:
        return False
    manga["cover"] = result_fname
    return True

def auto_organize_library_root(library: dict):
    """
    For each library root path, move any loose case2 archive/pdf/epub files
    sitting directly in that folder into their own named subfolder.
    Only operates at the top level — does not walk into subfolders.
    Skips case1 archives (those that contain chapter subfolders inside).
    Skips if the destination subfolder already exists.
    """
    raw_paths = library.get("paths") or ([library["path"]] if library.get("path") else [])

    for lib_path in raw_paths:
        if not os.path.exists(lib_path):
            continue
        try:
            entries = os.listdir(lib_path)
        except Exception as e:
            print(f"[Organize] Cannot list {lib_path}: {e}")
            continue

        for fname in entries:
            fpath = os.path.join(lib_path, fname)
            if not os.path.isfile(fpath):
                continue

            is_organizable = is_pdf(fname) or is_epub(fname) or is_archive(fname)
            if not is_organizable:
                continue

            # Skip case1 archives (they contain chapter subfolders)
            if is_archive(fname):
                if classify_archive(fpath) == 'case1':
                    continue

            name_no_ext = os.path.splitext(fname)[0]
            dest_folder = os.path.join(lib_path, name_no_ext)
            dest_file   = os.path.join(dest_folder, fname)

            if os.path.exists(dest_folder):
                print(f"[Organize] Skipping (folder exists): {dest_folder}")
                continue

            try:
                os.makedirs(dest_folder)
                shutil.move(fpath, dest_file)
                print(f"[Organize] Moved {fname} -> {name_no_ext}/{fname}")
            except Exception as e:
                print(f"[Organize] Failed to move {fname}: {e}")

def relocate_dims_paths(library_id: int, manga_name: str, old_manga_path: str, new_manga_path: str):
    """
    When a manga folder has moved (same name, different parent), update all
    absolute paths stored in dims.json by replacing the old path prefix with
    the new one. Does not touch reading progress in admin.json.
    """
    dims = load_manga_dims(library_id, manga_name)
    changed = False

    for section in ("chapters", "volumes"):
        for source_id, source in dims.get(section, {}).items():
            old_path = source.get("path", "")
            if not old_path:
                continue
            if old_path.startswith(old_manga_path):
                new_path = new_manga_path + old_path[len(old_manga_path):]
                if new_path != old_path:
                    print(f"[Relocate] {manga_name}/{source.get('name')}: {old_path} -> {new_path}")
                    source["path"] = new_path
                    changed = True

    if changed:
        # If no cover was set from newly-scanned volumes, fall back to
        # the cover_image already stored in dims for any existing volume.
        if mangas[manga_path].get("cover") is None:
            sorted_vids = sorted(
                dims.get("volumes", {}).keys(),
                key=lambda vid: natural_sort_key(dims["volumes"][vid].get("name", vid))
            )
            for vid in sorted_vids:
                fallback = dims["volumes"][vid].get("cover_image")
                if fallback:
                    mangas[manga_path]["cover"] = fallback
                    break

        save_manga_dims(library_id, manga_name, dims)

def scan_library(library: dict) -> tuple:
    """Returns (mangas, comicinfo_changed) — comicinfo_changed is True if any
    manga's genres/tags were filled in from ComicInfo.xml during this scan,
    telling the caller to rebuild all_tags/all_genres once manga_data is saved."""
    raw_paths = library.get("paths") or ([library["path"]] if library.get("path") else [])
    lib_paths = [p for p in raw_paths if p and os.path.exists(p)]
    library_id = library["id"]
    mangas = {}

    if not lib_paths:
        print(f"[ScanLib] No valid paths found for library {library_id}")
        return []

    existing_data = load_app_data()
    existing_mangas = existing_data.get("manga_data", {}).get(str(library_id), {}).get("mangas", [])
    existing_by_id = {m["id"]: m for m in existing_mangas if "id" in m}

    # ── PASS 1: loose folders (recursive classification) ──
    # We do a single os.walk and for every folder that _classify_loose_folder
    # identifies as a manga we register it. Nested mangas are collected and
    # processed as independent entries; the walk naturally descends into them
    # so they will be picked up when os.walk reaches their path.
    # We track which paths have already been registered to avoid double-registration.
    _loose_registered: set = set()

    def _register_loose_manga(manga_path: str, classification: dict):
        if manga_path in mangas or manga_path in _loose_registered:
            return
        _loose_registered.add(manga_path)

        manga_name   = os.path.basename(manga_path)
        manga_id     = make_id(manga_name)
        existing     = existing_by_id.get(manga_id, {})
        stored_mtime = existing.get("folder_mtime")
        stored_cover_mtimes = existing.get("cover_mtimes", {})
        current_mtime = os.path.getmtime(manga_path)
        manga_type    = classification["manga_type"]  # "case1" or "case2"

        def _dims_paths_valid(manga_name: str) -> bool:
            dims = load_manga_dims(library_id, manga_name)
            sources = {**dims.get("chapters", {}), **dims.get("volumes", {})}
            if not sources:
                return True  # nothing stored yet, not a stale-path problem
            return all(os.path.exists(s.get("path", "")) for s in sources.values())

        folder_unchanged = stored_mtime is not None and current_mtime == stored_mtime
        # The manga's own top folder only changes mtime when a DIRECT entry
        # is added/removed/renamed (a whole chapter/volume subfolder
        # appearing or disappearing) - deleting a page from INSIDE an
        # existing chapter/volume subfolder only bumps THAT subfolder's own
        # mtime, never the parent manga folder's. The per-subfolder check
        # further down is already correct, but relying on this folder-level
        # check alone meant it never got reached for that kind of edit.
        any_subfolder_changed = False
        if folder_unchanged:
            existing_dims_peek = load_manga_dims(library_id, manga_name)
            bucket = existing_dims_peek.get("chapters" if manga_type == "case1" else "volumes", {})
            id_infix = ":" if manga_type == "case1" else ":vol:"
            for dirname in classification["content_subfolders"]:
                sub_id = make_id(manga_name + id_infix + dirname)
                stored_sub_mtime = bucket.get(sub_id, {}).get("mtime")
                try:
                    current_sub_mtime = os.path.getmtime(os.path.join(manga_path, dirname))
                except OSError:
                    any_subfolder_changed = True
                    break
                if stored_sub_mtime != current_sub_mtime:
                    any_subfolder_changed = True
                    break

        if folder_unchanged and not any_subfolder_changed:
            print(f"[ScanLib] Skipping unchanged loose: {manga_name}")
            if existing.get("path") and existing["path"] != manga_path:
                relocate_dims_paths(library_id, manga_name, existing["path"], manga_path)
            mangas[manga_path] = {**existing, "path": manga_path}
        else:
            print(f"[ScanLib] Rescanning loose ({manga_type}): {manga_name}")
            default_cover, new_cover_mtimes = process_manga_covers(
                manga_path, library_id, manga_name, stored_cover_mtimes
            )
            mangas[manga_path] = {
                "id":           manga_id,
                "name":         manga_name,
                "path":         manga_path,
                "cover":        default_cover,
                "folder_mtime": current_mtime,
                "cover_mtimes": new_cover_mtimes,
                "last_updated": datetime.now().isoformat(),
                "manga_type":   "loose",
            }

        # Only rescan content subfolders if something actually changed.
        if folder_unchanged and not any_subfolder_changed:
            return

        dims = load_manga_dims(library_id, manga_name)

        if manga_type == "case1":
            # Content subfolders are chapters
            for dirname in classification["content_subfolders"]:
                chapter_full_path = os.path.join(manga_path, dirname)
                chapter_id        = make_id(manga_name + ":" + dirname)
                chapter_mtime     = os.path.getmtime(chapter_full_path)
                existing_chapter  = dims["chapters"].get(chapter_id, {})

                if existing_chapter.get("mtime") == chapter_mtime:
                    print(f"[ScanLib] Chapter unchanged: {dirname}")
                    continue

                print(f"[ScanLib] Scanning chapter: {dirname}")
                files = sorted(
                    [f for f in os.listdir(chapter_full_path)
                     if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS],
                    key=natural_sort_key
                )
                pages = []
                for fname in files:
                    try:
                        img = Image.open(os.path.join(chapter_full_path, fname))
                        w, h = img.size
                        pages.append({"w": w, "h": h})
                    except Exception:
                        pages.append({"w": 800, "h": 1100})

                dims["chapters"][chapter_id] = {
                    "name":      dirname,
                    "path":      chapter_full_path,
                    "mtime":     chapter_mtime,
                    "pages":     pages,
                    "source":    "loose",
                    "filenames": files,
                }

        else:
            # manga_type == "case2": content subfolders are volumes
            if "volumes" not in dims:
                dims["volumes"] = {}

            for dirname in classification["content_subfolders"]:
                vol_full_path = os.path.join(manga_path, dirname)
                vol_id        = make_id(manga_name + ":vol:" + dirname)
                vol_mtime     = os.path.getmtime(vol_full_path)
                existing_vol  = dims["volumes"].get(vol_id, {})

                if existing_vol.get("mtime") == vol_mtime:
                    print(f"[ScanLib] Loose volume unchanged: {dirname}")
                    continue

                print(f"[ScanLib] Scanning loose volume: {dirname}")
                files = sorted(
                    [f for f in os.listdir(vol_full_path)
                     if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS],
                    key=natural_sort_key
                )
                pages = []
                cover_fname = None
                for i, fname in enumerate(files):
                    try:
                        img = Image.open(os.path.join(vol_full_path, fname))
                        w, h = img.size
                        pages.append({"w": w, "h": h})
                    except Exception:
                        pages.append({"w": 800, "h": 1100})
                    if i == 0:
                        # Use first image as cover
                        cover_fname = dirname + "_" + fname
                        try:
                            with open(os.path.join(vol_full_path, fname), "rb") as cf:
                                cover_bytes = cf.read()
                            result_fname, new_cover_mtimes = process_cover_from_bytes(
                                cover_bytes, cover_fname,
                                library_id, manga_name,
                                mangas[manga_path].get("cover_mtimes", {}), vol_mtime
                            )
                            if result_fname:
                                cover_fname = result_fname
                                mangas[manga_path].setdefault("cover_mtimes", {}).update(new_cover_mtimes)
                        except Exception as e:
                            print(f"[ScanLib] Loose volume cover failed: {e}")
                            cover_fname = None

                dims["volumes"][vol_id] = {
                    "name":        dirname,
                    "path":        vol_full_path,
                    "mtime":       vol_mtime,
                    "pages":       pages,
                    "cover_image": cover_fname,
                    "source":      "loose",
                    "filenames":   files,
                }

                if mangas[manga_path].get("cover") is None and cover_fname is not None:
                    mangas[manga_path]["cover"] = cover_fname

        save_manga_dims(library_id, manga_name, dims)

    for lib_path in lib_paths:
        for dirpath, dirnames, filenames in os.walk(lib_path):
            dirnames.sort(key=natural_sort_key)
            if dirpath in _loose_registered:
                continue  # already processed as a nested manga
            classification = _classify_loose_folder(dirpath)
            if classification["is_manga"]:
                _register_loose_manga(dirpath, classification)
            # Nested mangas found inside this folder will be reached by os.walk naturally;
            # _loose_registered prevents double-processing.

    # ── PASS 2: scan folders for archive/pdf/epub files ──
    for lib_path in lib_paths:
        for dirpath, dirnames, filenames in os.walk(lib_path):
            dirnames.sort(key=natural_sort_key)
            filenames_sorted = sorted(filenames, key=natural_sort_key)

            # Collect archive/pdf/epub files directly in this folder
            archive_files = [f for f in filenames_sorted if is_archive(f)]
            pdf_files     = [f for f in filenames_sorted if is_pdf(f)]
            epub_files    = [f for f in filenames_sorted if is_epub(f)]

            # ── CASE 1: standalone cbz that is itself a manga ──
            for arc_file in archive_files:
                arc_path = os.path.join(dirpath, arc_file)
                case = classify_archive(arc_path)
                if case != 'case1':
                    continue

                manga_name = os.path.splitext(arc_file)[0]
                manga_id   = make_id(manga_name)
                # Use the cbz file path as the unique key
                if arc_path in mangas:
                    continue

                existing        = existing_by_id.get(manga_id, {})
                stored_mtime    = existing.get("file_mtime")
                current_mtime   = os.path.getmtime(arc_path)

                def _arc_dims_valid(manga_name: str, arc_path: str) -> bool:
                    dims = load_manga_dims(library_id, manga_name)
                    sources = {**dims.get("chapters", {}), **dims.get("volumes", {})}
                    if not sources:
                        return True
                    return all(os.path.exists(s.get("path", "")) for s in sources.values())

                if stored_mtime is not None and current_mtime == stored_mtime and _arc_dims_valid(manga_name, arc_path):
                    print(f"[ScanLib] Skipping unchanged case1: {manga_name}")
                    mangas[arc_path] = existing
                    continue

                print(f"[ScanLib] Scanning case1: {manga_name}")
                _invalidate_stale_source_caches(arc_path)
                stored_cover_mtimes = existing.get("cover_mtimes", {})
                new_cover_mtimes    = dict(stored_cover_mtimes)
                default_cover       = None

                arc = open_archive(arc_path)
                if arc is None:
                    continue
                with arc:
                    # Find chapter subdirs and loose images at chapter level
                    all_names = arc.namelist()
                    # Detect if there's a top-level manga wrapper folder
                    top_dirs = set()
                    for n in all_names:
                        parts = n.split('/')
                        if len(parts) > 1 and parts[0]:
                            top_dirs.add(parts[0])

                    has_wrapper = (
                        len(top_dirs) == 1
                        and not re.search(r'chapter', list(top_dirs)[0], re.IGNORECASE)
                    )
                    prefix = (list(top_dirs)[0] + '/') if has_wrapper else ''

                    # Collect chapter subdirs
                    chapter_dirs = sorted(set(
                        n[len(prefix):].split('/')[0]
                        for n in all_names
                        if n.startswith(prefix)
                        and len(n[len(prefix):].split('/')) > 1
                        and re.search(r'chapter', n[len(prefix):].split('/')[0], re.IGNORECASE)
                    ), key=natural_sort_key)

                    # Loose images at the chapter level (same depth as chapter dirs)
                    loose_images = sorted([
                        n for n in all_names
                        if n.startswith(prefix)
                        and '/' not in n[len(prefix):]
                        and os.path.splitext(n)[1].lower() in IMAGE_EXTENSIONS
                    ], key=lambda x: natural_sort_key(x))

                    covers_dir = get_covers_dir()
                    manga_covers_dir = os.path.join(covers_dir, str(library_id), manga_name)
                    os.makedirs(manga_covers_dir, exist_ok=True)

                    # Process loose cover images
                    for entry_name in loose_images:
                        fname = os.path.basename(entry_name)
                        img_bytes = read_archive_entry_bytes(arc, entry_name)
                        if img_bytes:
                            cover_filename, new_cover_mtimes = process_cover_from_bytes(
                                img_bytes, fname, library_id, manga_name,
                                new_cover_mtimes, current_mtime
                            )
                            if default_cover is None:
                                default_cover = fname

                    # If no loose covers, use first image of first chapter
                    if default_cover is None and chapter_dirs:
                        first_ch = chapter_dirs[0]
                        ch_images = sorted([
                            n for n in all_names
                            if n.startswith(prefix + first_ch + '/')
                            and os.path.splitext(n)[1].lower() in IMAGE_EXTENSIONS
                        ], key=natural_sort_key)
                        if ch_images:
                            fname = os.path.basename(ch_images[0])
                            img_bytes = read_archive_entry_bytes(arc, ch_images[0])
                            if img_bytes:
                                cover_filename, new_cover_mtimes = process_cover_from_bytes(
                                    img_bytes, fname, library_id, manga_name,
                                    new_cover_mtimes, current_mtime
                                )
                                default_cover = fname

                    # Scan chapters and page dims
                    dims = load_manga_dims(library_id, manga_name)
                    for ch_dir in chapter_dirs:
                        chapter_id = make_id(manga_name + ":" + ch_dir)
                        existing_ch = dims["chapters"].get(chapter_id, {})
                        # Use archive mtime as chapter mtime (no per-chapter mtime inside zip)
                        if existing_ch.get("mtime") == current_mtime:
                            print(f"[ScanLib] Chapter unchanged (case1): {ch_dir}")
                            continue
                        ch_images = sorted([
                            n for n in all_names
                            if n.startswith(prefix + ch_dir + '/')
                            and os.path.splitext(n)[1].lower() in IMAGE_EXTENSIONS
                        ], key=natural_sort_key)
                        pages = [{"w": w, "h": h} for w, h in
                                (get_archive_image_dims(arc_path, n) for n in ch_images)]
                        dims["chapters"][chapter_id] = {
                            "name":       ch_dir,
                            "path":       arc_path,
                            "prefix":     prefix + ch_dir + '/',
                            "mtime":      current_mtime,
                            "pages":      pages,
                            "source":     "archive",
                            "page_count": len(pages),
                        }
                    save_manga_dims(library_id, manga_name, dims)

                mangas[arc_path] = {
                    "id":           manga_id,
                    "name":         manga_name,
                    "path":         arc_path,
                    "cover":        default_cover,
                    "file_mtime":   current_mtime,
                    "cover_mtimes": new_cover_mtimes,
                    "last_updated": datetime.now().isoformat(),
                    "manga_type":   "case1",
                }

            # ── CASE 2 & 3: archives inside a manga folder ──
            # Only process if this folder itself contains archive/pdf/epub files
            # that are NOT case1 (i.e., don't have internal chapter structure)
            case2_files = []  # (filename, full_path, type)
            case3_files = []

            for arc_file in archive_files:
                arc_path = os.path.join(dirpath, arc_file)
                case = classify_archive(arc_path)
                if case == 'case1':
                    continue  # handled above
                elif case == 'case3':
                    case3_files.append((arc_file, arc_path, 'archive'))
                else:
                    case2_files.append((arc_file, arc_path, 'archive'))

            for pdf_file in pdf_files:
                case2_files.append((pdf_file, os.path.join(dirpath, pdf_file), 'pdf'))

            for epub_file in epub_files:
                case2_files.append((epub_file, os.path.join(dirpath, epub_file), 'epub'))

            # ── CASE 3 ──
            if case3_files:
                manga_path = dirpath
                manga_name = os.path.basename(manga_path)
                if manga_path in mangas:
                    # Already registered as loose — skip
                    pass
                else:
                    manga_id          = make_id(manga_name)
                    existing          = existing_by_id.get(manga_id, {})
                    stored_folder_mtime = existing.get("folder_mtime")
                    current_folder_mtime = os.path.getmtime(manga_path)
                    stored_cover_mtimes = existing.get("cover_mtimes", {})

                    folder_unchanged = stored_folder_mtime is not None and current_folder_mtime == stored_folder_mtime
                    # A folder's own mtime only moves when entries are added,
                    # removed, or renamed — editing an existing archive's
                    # contents in place (e.g. deleting one page from inside a
                    # .cbz) leaves the containing folder's mtime untouched,
                    # only the archive file's own mtime changes. Relying on
                    # the folder check alone meant that kind of edit was
                    # never picked up by ANY later scan. Also check each
                    # archive's own mtime against what's stored per-chapter.
                    any_archive_changed = False
                    if folder_unchanged:
                        existing_dims_peek = load_manga_dims(library_id, manga_name)
                        any_archive_changed = any(
                            existing_dims_peek.get("chapters", {}).get(
                                make_id(manga_name + ":" + os.path.splitext(af)[0]), {}
                            ).get("mtime") != os.path.getmtime(ap)
                            for af, ap, _ in case3_files
                        )

                    if folder_unchanged and not any_archive_changed:
                        print(f"[ScanLib] Skipping unchanged case3: {manga_name}")
                        if existing.get("path") and existing["path"] != manga_path:
                            relocate_dims_paths(library_id, manga_name, existing["path"], manga_path)
                        mangas[manga_path] = {**existing, "path": manga_path}
                    else:
                        print(f"[ScanLib] Scanning case3: {manga_name}")
                        # Process loose cover images in the folder
                        default_cover, new_cover_mtimes = process_manga_covers(
                            manga_path, library_id, manga_name, stored_cover_mtimes
                        )
                        mangas[manga_path] = {
                            "id":           manga_id,
                            "name":         manga_name,
                            "path":         manga_path,
                            "cover":        default_cover,
                            "folder_mtime": current_folder_mtime,
                            "cover_mtimes": new_cover_mtimes,
                            "last_updated": datetime.now().isoformat(),
                            "manga_type":   "case3",
                        }

                        dims = load_manga_dims(library_id, manga_name)
                        for arc_file, arc_path, _ in case3_files:
                            chapter_name = os.path.splitext(arc_file)[0]
                            chapter_id   = make_id(manga_name + ":" + chapter_name)
                            arc_mtime    = os.path.getmtime(arc_path)
                            existing_ch  = dims["chapters"].get(chapter_id, {})

                            if existing_ch.get("mtime") == arc_mtime:
                                print(f"[ScanLib] Chapter unchanged (case3): {chapter_name}")
                                continue

                            print(f"[ScanLib] Scanning case3 chapter: {chapter_name}")
                            _invalidate_stale_source_caches(arc_path)
                            arc = open_archive(arc_path)
                            if arc is None:
                                continue
                            with arc:
                                images = list_archive_images(arc)
                            pages = [{"w": w, "h": h} for w, h in
                                    (get_archive_image_dims(arc_path, n) for n in images)]
                            dims["chapters"][chapter_id] = {
                                "name":       chapter_name,
                                "path":       arc_path,
                                "mtime":      arc_mtime,
                                "pages":      pages,
                                "source":     "archive",
                                "page_count": len(pages),
                            }
                        save_manga_dims(library_id, manga_name, dims)

            # ── CASE 2 ──
            if case2_files:
                manga_path = dirpath
                manga_name = os.path.basename(manga_path)
                if manga_path in mangas:
                    pass  # Already registered
                else:
                    manga_id             = make_id(manga_name)
                    existing             = existing_by_id.get(manga_id, {})
                    stored_folder_mtime  = existing.get("folder_mtime")
                    current_folder_mtime = os.path.getmtime(manga_path)
                    stored_cover_mtimes  = existing.get("cover_mtimes", {})

                    folder_unchanged = stored_folder_mtime is not None and current_folder_mtime == stored_folder_mtime
                    # See the matching comment in the case3 branch above: a
                    # folder's own mtime doesn't move when an existing
                    # volume's contents are edited in place, only that
                    # file's own mtime does — so also check each volume file
                    # against what's stored, or that edit would never be
                    # noticed.
                    any_volume_changed = False
                    if folder_unchanged:
                        existing_dims_peek = load_manga_dims(library_id, manga_name)
                        any_volume_changed = any(
                            existing_dims_peek.get("volumes", {}).get(
                                make_id(manga_name + ":vol:" + os.path.splitext(vf)[0]), {}
                            ).get("mtime") != os.path.getmtime(vp)
                            for vf, vp, _ in case2_files
                        )

                    if folder_unchanged and not any_volume_changed:
                        print(f"[ScanLib] Skipping unchanged case2: {manga_name}")
                        if existing.get("path") and existing["path"] != manga_path:
                            relocate_dims_paths(library_id, manga_name, existing["path"], manga_path)
                        mangas[manga_path] = {**existing, "path": manga_path}
                    else:
                        print(f"[ScanLib] Scanning case2: {manga_name}")
                        # Process loose cover images in the manga folder
                        default_cover, new_cover_mtimes = process_manga_covers(
                            manga_path, library_id, manga_name, stored_cover_mtimes
                        )
                        mangas[manga_path] = {
                            "id":           manga_id,
                            "name":         manga_name,
                            "path":         manga_path,
                            "cover":        default_cover,
                            "folder_mtime": current_folder_mtime,
                            "cover_mtimes": new_cover_mtimes,
                            "last_updated": datetime.now().isoformat(),
                            "manga_type":   "case2",
                        }

                        dims = load_manga_dims(library_id, manga_name)
                        if "volumes" not in dims:
                            dims["volumes"] = {}

                        for vol_file, vol_path, vol_type in case2_files:
                            vol_name  = os.path.splitext(vol_file)[0]
                            vol_id    = make_id(manga_name + ":vol:" + vol_name)
                            vol_mtime = os.path.getmtime(vol_path)
                            existing_vol = dims["volumes"].get(vol_id, {})

                            if existing_vol.get("mtime") == vol_mtime:
                                print(f"[ScanLib] Volume unchanged (case2): {vol_name}")
                                continue

                            print(f"[ScanLib] Scanning case2 volume: {vol_name}")
                            _invalidate_stale_source_caches(vol_path)

                            # Initialise so we always have valid values reaching the dims write
                            pages       = []
                            cover_entry = None
                            cover_fname = None

                            if vol_type == 'archive':
                                arc = open_archive(vol_path)
                                if arc is None:
                                    print(f"[ScanLib] Cannot open archive: {vol_path}")
                                else:
                                    with arc:
                                        images = list_archive_images(arc)
                                    pages = [{"w": w, "h": h} for w, h in
                                            (get_archive_image_dims(vol_path, n) for n in images)]
                                    if images:
                                        cover_entry = images[0]
                                        cover_fname = vol_name + os.path.splitext(cover_entry)[1]
                                        cover_bytes = _cached_archive_page(vol_path, cover_entry)
                                        if cover_bytes:
                                            process_cover_from_bytes(
                                                cover_bytes, cover_fname,
                                                library_id, manga_name,
                                                new_cover_mtimes, vol_mtime
                                            )
                                            new_cover_mtimes[cover_fname] = vol_mtime

                            elif vol_type == 'pdf':
                                if not PDF_SUPPORT:
                                    print(f"[ScanLib] pymupdf not installed, skipping {vol_path}")
                                else:
                                    try:
                                        doc = pymupdf.open(vol_path)
                                        page_count = len(doc)
                                        doc.close()
                                        pages = [get_pdf_page_dims(vol_path, i) for i in range(page_count)]
                                        pages = [{"w": w, "h": h} for w, h in pages]
                                        cover_entry = 0
                                        cover_fname = vol_name + ".jpg"
                                        cover_bytes = _cached_pdf_page(vol_path, 0)
                                        if cover_bytes:
                                            result_fname, new_cover_mtimes = process_cover_from_bytes(
                                                cover_bytes, cover_fname,
                                                library_id, manga_name,
                                                new_cover_mtimes, vol_mtime
                                            )
                                            if result_fname:
                                                cover_fname = result_fname
                                                new_cover_mtimes[cover_fname] = vol_mtime
                                    except Exception as e:
                                        print(f"[ScanLib] Failed to process PDF {vol_path}: {e}")

                            elif vol_type == 'epub':
                                image_list = get_epub_image_list(vol_path)
                                for href in image_list:
                                    epub_bytes = _cached_epub_page(vol_path, href)
                                    if epub_bytes:
                                        try:
                                            img = Image.open(io.BytesIO(epub_bytes))
                                            pages.append({"w": img.width, "h": img.height})
                                        except Exception:
                                            pages.append({"w": 800, "h": 1100})
                                    else:
                                        pages.append({"w": 800, "h": 1100})
                                if image_list:
                                    cover_entry = image_list[0]
                                    cover_fname = vol_name + ".jpg"
                                    cover_bytes = _cached_epub_page(vol_path, cover_entry)
                                    if cover_bytes:
                                        process_cover_from_bytes(
                                            cover_bytes, cover_fname,
                                            library_id, manga_name,
                                            new_cover_mtimes, vol_mtime
                                        )
                                        new_cover_mtimes[cover_fname] = vol_mtime

                            # Always write the volume record, even if cover extraction failed
                            dims["volumes"][vol_id] = {
                                "name":        vol_name,
                                "path":        vol_path,
                                "type":        vol_type,
                                "mtime":       vol_mtime,
                                "pages":       pages,
                                "cover_image": cover_fname,
                                "source":      vol_type,
                                "page_count":  len(pages),
                            }
                            if default_cover is None and cover_fname is not None:
                                default_cover = cover_fname
                                mangas[manga_path]["cover"] = default_cover

                        # If no cover was set from newly-scanned volumes, fall back to
                        # the cover_image already stored in dims for any existing volume.
                        if default_cover is None:
                            sorted_vids = sorted(
                                dims.get("volumes", {}).keys(),
                                key=lambda vid: natural_sort_key(dims["volumes"][vid].get("name", vid))
                            )
                            for vid in sorted_vids:
                                fallback = dims["volumes"][vid].get("cover_image")
                                if fallback:
                                    default_cover = fallback
                                    mangas[manga_path]["cover"] = default_cover
                                    break

                        # Update cover_mtimes on the manga record
                        mangas[manga_path]["cover_mtimes"] = new_cover_mtimes
                        save_manga_dims(library_id, manga_name, dims)

                    
    # ── Deleted manga cleanup ──
    found_ids = {m["id"] for m in mangas.values()}
    covers_dir = get_covers_dir()
    for manga_id, existing_manga in existing_by_id.items():
        if manga_id not in found_ids:
            print(f"[ScanLib] Manga deleted: {existing_manga['name']}")
            if covers_dir:
                manga_covers_dir = os.path.join(covers_dir, str(library_id), existing_manga["name"])
                if os.path.exists(manga_covers_dir):
                    try:
                        shutil.rmtree(manga_covers_dir)
                        print(f"[ScanLib] Deleted covers folder: {manga_covers_dir}")
                    except Exception as e:
                        print(f"[ScanLib] Failed to delete covers folder: {e}")

    # ── COMPLETE flag: check if last chapter/volume name ends with word "END" ──
    comicinfo_changed = False
    for manga in mangas.values():
        manga_name = manga.get("name", "")
        dims = load_manga_dims(library_id, manga_name)
        last_name = None
        volumes = dims.get("volumes", {})
        chapters = dims.get("chapters", {})
        if volumes:
            sorted_vols = sorted(volumes.values(), key=lambda v: natural_sort_key(v.get("name", "")))
            last_name = sorted_vols[-1].get("name", "")
        elif chapters:
            sorted_chs = sorted(chapters.values(), key=lambda c: natural_sort_key(c.get("name", "")))
            last_name = sorted_chs[-1].get("name", "")
        if last_name and last_name.split()[-1] == "END":
            manga["is_complete"] = True
        else:
            manga["is_complete"] = False

        # ── ComicInfo.xml: fill in description/genres/tags left empty by
        # everything else (manual edit, a prior fetch, or a prior run of
        # this same pass) — never overwrites a value that's already set.
        if not dims.get("description") or not dims.get("genres") or not dims.get("tags"):
            found = find_comicinfo_for_manga(manga.get("path", ""), dims)
            if found:
                dims_changed = False
                if not dims.get("description") and found["description"]:
                    dims["description"] = found["description"]
                    dims_changed = True
                if not dims.get("genres") and found["genres"]:
                    dims["genres"] = found["genres"]
                    dims_changed = True
                    comicinfo_changed = True
                if not dims.get("tags") and found["tags"]:
                    dims["tags"] = found["tags"]
                    dims_changed = True
                    comicinfo_changed = True
                if dims_changed:
                    save_manga_dims(library_id, manga_name, dims)

    result = list(mangas.values())
    result.sort(key=lambda m: natural_sort_key(m["name"]))
    print(f"[ScanLib] Total mangas found: {len(result)}")
    return result, comicinfo_changed

async def periodic_library_rescan():
    INTERVAL_SECONDS = 12 * 60 * 60
    while True:
        now = time.time()
        next_boundary = (int(now) // INTERVAL_SECONDS + 1) * INTERVAL_SECONDS
        await asyncio.sleep(next_boundary - now)
        try:
            data = load_app_data()
            libraries = data.get("libraries", [])
            for lib in libraries:
                lib_id = lib.get("id")
                if lib_id is not None and lib_id not in _scan_running:
                    print(f"[AutoScan] Re-scanning library {lib_id}...")
                    await asyncio.to_thread(run_scan, lib_id)
        except Exception as e:
            print(f"[AutoScan] Error during periodic rescan: {e}")

def _integrity_due_items() -> list:
    """
    Every chapter/volume across every library that's due for a check (never
    checked, or checked longer than INTEGRITY_RECHECK_SECONDS ago), oldest
    first. Each entry: (library_id, manga_id, manga_name, item).
    """
    data = load_app_data()
    due = []
    for lib in data.get("libraries", []):
        library_id = lib.get("id")
        if library_id is None:
            continue
        manga_data = data.get("manga_data", {}).get(str(library_id), {})
        for manga in manga_data.get("mangas", []):
            manga_name = manga.get("name", "")
            dims = load_manga_dims(library_id, manga_name)
            for item in checkable_items_for_manga(dims):
                bucket = dims.get("volumes") if item["item_type"] == "volume" else dims.get("chapters")
                checked_at_str = (bucket or {}).get(item["item_id"], {}).get("integrity_checked_at")
                sort_key = ""  # never checked — sorts first (empty string < any ISO timestamp)
                if checked_at_str:
                    try:
                        age = (datetime.now() - datetime.fromisoformat(checked_at_str)).total_seconds()
                        if age < INTEGRITY_RECHECK_SECONDS:
                            continue
                    except Exception:
                        pass
                    sort_key = checked_at_str
                due.append((sort_key, library_id, manga.get("id"), manga_name, item))
    due.sort(key=lambda t: t[0])
    return [(lib_id, manga_id, manga_name, item) for (_, lib_id, manga_id, manga_name, item) in due]

async def run_integrity_check_loop():
    """
    Idle-gated background pass — only ever runs when is_idle() (no session/
    token-authenticated request recently), and bails out of the current batch
    the moment activity resumes, resuming at the same point (oldest-checked-
    first) on the next idle window rather than needing any separate pause/
    resume bookkeeping.
    """
    WAKE_INTERVAL_SECONDS = 10 * 60
    MAX_PER_WAKE = 500
    while True:
        await asyncio.sleep(WAKE_INTERVAL_SECONDS)
        try:
            if not is_idle():
                continue
            due = await asyncio.to_thread(_integrity_due_items)
            if not due:
                continue
            print(f"[Integrity] {len(due)} item(s) due for a check, starting idle pass...")
            checked = 0
            for library_id, manga_id, manga_name, item in due:
                if not is_idle():
                    print("[Integrity] Activity resumed, pausing until next idle window.")
                    break
                dims = await asyncio.to_thread(load_manga_dims, library_id, manga_name)
                await asyncio.to_thread(run_integrity_check_for_item, library_id, manga_id, manga_name, dims, item)
                checked += 1
                if checked >= MAX_PER_WAKE:
                    break
            print(f"[Integrity] Checked {checked} item(s) this pass.")
        except Exception as e:
            print(f"[Integrity] Error during integrity check loop: {e}")

@app.get("/api/admin/integrity/issues")
def get_integrity_issues_endpoint(request: Request):
    err = auth.require_admin(request)
    if err:
        return err
    issues = load_integrity_issues()["issues"]
    return JSONResponse({"issues": issues, "count": len(issues)})

@app.post("/api/admin/integrity/recheck")
async def recheck_integrity_issues_endpoint(request: Request):
    """Re-runs the check for whichever chapters/volumes the given issue_ids point
    at (or every currently-open issue if omitted), against their CURRENT path —
    not the old filename — so a replaced chapter/volume is checked fresh."""
    err = auth.require_admin(request)
    if err:
        return err
    body = await request.json()
    issue_ids = set(body.get("issue_ids") or [])
    issues_data = load_integrity_issues()
    targets = issues_data["issues"] if not issue_ids else [i for i in issues_data["issues"] if i["id"] in issue_ids]

    data = load_app_data()
    seen = set()
    checked = 0
    for issue in targets:
        key = (issue["library_id"], issue["manga_id"], issue["item_type"], issue["item_id"])
        if key in seen:
            continue
        seen.add(key)
        manga_data = data.get("manga_data", {}).get(str(issue["library_id"]), {})
        manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == issue["manga_id"]), None)
        if not manga:
            _clear_issues_for_item(issues_data, issue["library_id"], issue["manga_id"], issue["item_type"], issue["item_id"])
            save_integrity_issues(issues_data)
            continue
        dims = load_manga_dims(issue["library_id"], manga["name"])
        current_item = next(
            (it for it in checkable_items_for_manga(dims)
             if it["item_type"] == issue["item_type"] and it["item_id"] == issue["item_id"]),
            None,
        )
        if not current_item:
            # Chapter/volume no longer exists — nothing left to check, clear the finding.
            _clear_issues_for_item(issues_data, issue["library_id"], issue["manga_id"], issue["item_type"], issue["item_id"])
            save_integrity_issues(issues_data)
            continue
        await asyncio.to_thread(
            run_integrity_check_for_item, issue["library_id"], issue["manga_id"], manga["name"], dims, current_item
        )
        checked += 1

    updated = load_integrity_issues()["issues"]
    return JSONResponse({"ok": True, "checked": checked, "issues": updated, "count": len(updated)})

@app.post("/api/admin/integrity/dismiss")
async def dismiss_integrity_issue_endpoint(request: Request):
    err = auth.require_admin(request)
    if err:
        return err
    body = await request.json()
    issue_id = body.get("issue_id")
    issues_data = load_integrity_issues()
    issues_data["issues"] = [i for i in issues_data["issues"] if i["id"] != issue_id]
    save_integrity_issues(issues_data)
    return JSONResponse({"ok": True, "count": len(issues_data["issues"])})

def run_scan(library_id: int):
    _scan_running.add(library_id)
    print(f"[Scan] Starting scan for library {library_id}...")
    data = load_app_data()
    libraries = data.get("libraries", [])
    lib = next((l for l in libraries if l["id"] == library_id), None)
    if not lib:
        print(f"[Scan] Library {library_id} not found in data.")
        _scan_running.discard(library_id)
        return

    raw_paths = lib.get("paths") or ([lib["path"]] if lib.get("path") else [])
    print(f"[Scan] Library paths: {raw_paths}")
    for p in raw_paths:
        print(f"[Scan] Path exists ({p}): {os.path.exists(p)}")
    print(f"[Scan] data_path: {get_data_path()}")
    print(f"[Scan] covers_dir: {get_covers_dir()}")

    auto_organize_library_root(lib)
    mangas, comicinfo_changed = scan_library(lib)
    print(f"[Scan] Found {len(mangas)} mangas.")

    data = load_app_data()
    if "manga_data" not in data:
        data["manga_data"] = {}
    data["manga_data"][str(library_id)] = {
        "mangas": mangas,
        "last_scanned": datetime.now().isoformat(),
    }
    if comicinfo_changed:
        data["all_tags"] = rebuild_all_tags(data)
        data["all_genres"] = rebuild_all_genres(data)
    save_app_data(data)
    _scan_running.discard(library_id)
    print(f"[Scan] Done. Saved to data.json.")
    t = threading.Thread(target=extract_thumbs_for_library, args=(library_id,), daemon=True)
    t.start()

    
# ── API ROUTES ──

def default_theme():
    return {
        "name": "Default",
        "primary":    "#e94560",
        "secondary":  "#1d1113",
        "background": "#120b0d",
        "text":       "#f0f0f0",
        "bg_image":   None,
    }

def get_theme_css(username: str = None) -> str:
    BUILTIN_THEMES = [
        {"name": "Midnight Red",  "primary": "#e94560", "secondary": "#1d1113", "background": "#120b0d", "text": "#f0f0f0"},
        {"name": "Ocean Deep",    "primary": "#38bdf8", "secondary": "#0d1e2e", "background": "#060f1c", "text": "#e2f0fb"},
        {"name": "Forest Ink",    "primary": "#4ade80", "secondary": "#141d16", "background": "#0b130d", "text": "#e6f4ea"},
        {"name": "Amber Noir",    "primary": "#f59e0b", "secondary": "#1c1608", "background": "#0f0c07", "text": "#fdf3dc"},
        {"name": "Royal Dusk",    "primary": "#a78bfa", "secondary": "#1a1228", "background": "#0e0a1a", "text": "#ede9fe"},
    ]
    custom_css = ""
    try:
        user_data = auth.load_user_data(username) if username else {}
        active_name = user_data.get("active_theme", "Midnight Red")
        theme = next((t for t in BUILTIN_THEMES if t["name"] == active_name), BUILTIN_THEMES[0])
        visual_theme = user_data.get("active_visual_theme", "default")
        if visual_theme == "custom":
            cname = user_data.get("active_custom_theme_name", "")
            custom_css = user_data.get("custom_themes", {}).get(cname, "")
    except Exception:
        theme = BUILTIN_THEMES[0]
        visual_theme = "default"
    dt_val = visual_theme if visual_theme in ("default", "sharp", "abyss", "custom") else "default"
    dt_script = f'<script>document.documentElement.setAttribute("data-theme","{dt_val}");</script>'
    custom_block = f'<style id="custom-theme-style">{custom_css}</style>' if custom_css else ""
    return (
        f'<link rel="stylesheet" href="/static/style.css">'
        f'<link rel="stylesheet" href="/static/theme-abyss.css">'
        f"<style>"
        f":root{{"
        f"--color-primary:{theme['primary']};"
        f"--color-secondary:{theme['secondary']};"
        f"--color-background:{theme['background']};"
        f"--color-text:{theme['text']};"
        f"--theme-primary:{theme['primary']};"
        f"}}"
        f"body{{background:{theme['background']};}}"
        f"</style>"
        f"{dt_script}"
        f"{custom_block}"
    )

@app.get("/api/settings")
def get_settings(request: Request):
    username = auth.get_current_user(request)
    data = load_app_data()
    user_data = auth.load_user_data(username)
    return JSONResponse({
        "data_path":           get_data_path() or "",
        "libraries":           data.get("libraries", []),
        "themes":              data.get("themes", [default_theme()]),
        "active_theme":        user_data.get("active_theme", "Midnight Red"),
        "active_visual_theme":      user_data.get("active_visual_theme", "default"),
        "active_custom_theme_name": user_data.get("active_custom_theme_name", ""),
        "custom_themes":            user_data.get("custom_themes", {}),
        "favourites":               user_data.get("favourites", []),
        "backdrop_list":            user_data.get("backdrop_list",   True),
        "backdrop_detail":          user_data.get("backdrop_detail", True),
        "hide_ble_scroller":        user_data.get("hide_ble_scroller", True),
        "hidden_libraries":         user_data.get("hidden_libraries", []),
        "show_collections_row":     user_data.get("show_collections_row", True),
        "hide_admin_collections":   user_data.get("hide_admin_collections", False),
    })

@app.post("/api/settings/data-path")
async def set_data_path(request: Request):
    err = auth.require_admin(request)
    if err:
        return err
    body = await request.json()
    new_path = body.get("data_path", "").strip()
    # Save to bootstrap file
    with open(BOOTSTRAP_FILE, "w") as f:
        json.dump({"data_path": new_path}, f, indent=2)
    # Migrate existing data.json if there is one
    existing = load_app_data()
    existing["data_path"] = new_path
    save_app_data(existing)
    return JSONResponse({"ok": True})

@app.post("/api/settings/libraries")
async def save_libraries(request: Request):
    err = auth.require_admin(request)
    if err:
        return err
    body = await request.json()
    data = load_app_data()
    old_ids = {lib["id"] for lib in data.get("libraries", []) if "id" in lib}
    new_libs = body.get("libraries", [])
    new_ids = {lib["id"] for lib in new_libs if "id" in lib}
    removed_ids = old_ids - new_ids
    if removed_ids:
        covers_dir = get_covers_dir()
        if covers_dir:
            for lib_id in removed_ids:
                lib_covers = os.path.join(covers_dir, str(lib_id))
                if os.path.exists(lib_covers):
                    # Close any open archive handles that may be locking files inside this folder
                    to_close = [p for p in list(_open_archive_handles.keys())
                                if p.startswith(lib_covers)]
                    for p in to_close:
                        try:
                            _open_archive_handles[p].close()
                        except Exception:
                            pass
                        del _open_archive_handles[p]

                    def _remove_readonly(func, path, _):
                        """On Windows, clear read-only flag and retry."""
                        import stat
                        os.chmod(path, stat.S_IWRITE)
                        func(path)

                    try:
                        shutil.rmtree(lib_covers, onerror=_remove_readonly)
                        print(f"[Settings] Deleted covers/dims for library {lib_id}")
                    except Exception as e:
                        print(f"[Settings] Failed to delete covers/dims for library {lib_id}: {e}")
        data["manga_data"] = {k: v for k, v in data.get("manga_data", {}).items() if k not in {str(i) for i in removed_ids}}
    data["libraries"] = new_libs
    save_app_data(data)
    return JSONResponse({"ok": True})

@app.post("/api/libraries/remove-path-covers")
async def remove_path_covers(request: Request):
    err = auth.require_admin(request)
    if err:
        return err
    body = await request.json()
    library_id = body.get("library_id")
    removed_path = body.get("removed_path", "").strip()

    if not library_id or not removed_path:
        return JSONResponse({"ok": False, "error": "Missing params"})

    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id), {})
    mangas = manga_data.get("mangas", [])
    covers_dir = get_covers_dir()

    removed_manga_ids = []
    for manga in mangas:
        manga_path = manga.get("path", "")
        # A manga belongs to this path if its path starts with the removed path
        if not manga_path.startswith(removed_path):
            continue
        removed_manga_ids.append(manga.get("id"))
        manga_name = manga.get("name", "")
        if covers_dir and manga_name:
            manga_covers_dir = os.path.join(covers_dir, str(library_id), manga_name)
            if os.path.exists(manga_covers_dir):
                def _remove_readonly(func, path, _):
                    import stat
                    os.chmod(path, stat.S_IWRITE)
                    func(path)
                try:
                    shutil.rmtree(manga_covers_dir, onerror=_remove_readonly)
                    print(f"[Settings] Deleted covers for {manga_name}")
                except Exception as e:
                    print(f"[Settings] Failed to delete covers for {manga_name}: {e}")

    # Remove these mangas from manga_data
    data["manga_data"][str(library_id)]["mangas"] = [
        m for m in mangas if m.get("id") not in removed_manga_ids
    ]
    save_app_data(data)
    return JSONResponse({"ok": True, "removed": len(removed_manga_ids)})

@app.post("/api/settings/themes")
async def save_themes(request: Request):
    username = auth.get_current_user(request)
    body = await request.json()
    data = load_app_data()
    data["themes"] = body.get("themes", [])
    save_app_data(data)
    user_data = auth.load_user_data(username)
    user_data["active_theme"] = body.get("active_theme", "Default")
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.post("/api/settings/visual-theme")
async def save_visual_theme(request: Request):
    username  = auth.get_current_user(request)
    body      = await request.json()
    user_data = auth.load_user_data(username)
    vt = body.get("active_visual_theme", "default")
    if vt not in ("default", "sharp", "abyss", "custom"):
        vt = "default"
    user_data["active_visual_theme"] = vt
    if vt == "custom" and "active_custom_theme_name" in body:
        user_data["active_custom_theme_name"] = body["active_custom_theme_name"]
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.post("/api/settings/custom-themes")
async def save_custom_theme(request: Request):
    username  = auth.get_current_user(request)
    body      = await request.json()
    name      = body.get("name", "").strip()
    css       = body.get("css", "")
    if not name:
        return JSONResponse({"ok": False, "error": "Name required"}, status_code=400)
    user_data = auth.load_user_data(username)
    if "custom_themes" not in user_data:
        user_data["custom_themes"] = {}
    user_data["custom_themes"][name] = css
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.delete("/api/settings/custom-themes/{name}")
async def delete_custom_theme(name: str, request: Request):
    username  = auth.get_current_user(request)
    user_data = auth.load_user_data(username)
    themes    = user_data.get("custom_themes", {})
    themes.pop(name, None)
    user_data["custom_themes"] = themes
    if user_data.get("active_custom_theme_name") == name:
        user_data["active_custom_theme_name"] = ""
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.post("/api/settings/backdrop")
async def save_backdrop(request: Request):
    username  = auth.get_current_user(request)
    body      = await request.json()
    user_data = auth.load_user_data(username)
    if "backdrop_list" in body:
        user_data["backdrop_list"]   = bool(body["backdrop_list"])
    if "backdrop_detail" in body:
        user_data["backdrop_detail"] = bool(body["backdrop_detail"])
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.post("/api/settings/ble-scroller")
async def save_ble_scroller_pref(request: Request):
    username  = auth.get_current_user(request)
    body      = await request.json()
    user_data = auth.load_user_data(username)
    if "hide_ble_scroller" in body:
        user_data["hide_ble_scroller"] = bool(body["hide_ble_scroller"])
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.post("/api/settings/library-visibility")
async def save_library_visibility(request: Request):
    """Per-user, self-service tab-visibility preference — separate from the
    admin-only access permission in permissions.json. Hiding a library here
    only removes its tab from this user's own home page; it doesn't revoke
    access (they can still reach it via search/collections/a direct link)."""
    username   = auth.get_current_user(request)
    body       = await request.json()
    library_id = str(body.get("library_id", ""))
    hidden     = bool(body.get("hidden"))
    if not library_id:
        return JSONResponse({"ok": False, "error": "Missing library_id"}, status_code=400)
    user_data = auth.load_user_data(username)
    hidden_libraries = set(str(x) for x in user_data.get("hidden_libraries", []))
    if hidden:
        hidden_libraries.add(library_id)
    else:
        hidden_libraries.discard(library_id)
    user_data["hidden_libraries"] = sorted(hidden_libraries)
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.post("/api/settings/collections-prefs")
async def save_collections_prefs(request: Request):
    username  = auth.get_current_user(request)
    body      = await request.json()
    user_data = auth.load_user_data(username)
    if "show_collections_row" in body:
        user_data["show_collections_row"]   = bool(body["show_collections_row"])
    if "hide_admin_collections" in body:
        user_data["hide_admin_collections"] = bool(body["hide_admin_collections"])
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.get("/api/backgrounds")
def list_backgrounds():
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backgrounds")
    if not os.path.exists(folder):
        os.makedirs(folder, exist_ok=True)
        return JSONResponse({"backgrounds": []})
    extensions = {".svg", ".jpg", ".jpeg", ".png", ".webp"}
    files = [
        f for f in os.listdir(folder)
        if os.path.splitext(f)[1].lower() in extensions
    ]
    files.sort()
    return JSONResponse({"backgrounds": files})

@app.post("/api/scan/{library_id}")
async def trigger_scan(library_id: int, background_tasks: BackgroundTasks):
    background_tasks.add_task(run_scan, library_id)
    return JSONResponse({"ok": True, "message": "Scan started"})

@app.get("/api/scan/{library_id}/status")
def scan_status(library_id: int):
    if library_id in _scan_running:
        return JSONResponse({"scanned": False, "running": True})
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"scanned": False, "running": False})
    return JSONResponse({
        "scanned": True,
        "running": False,
        "manga_count": len(manga_data.get("mangas", [])),
        "last_scanned": manga_data.get("last_scanned"),
    })

@app.get("/api/mangas/{library_id}")
def get_mangas(
    request: Request,
    library_id: int,
    sort: Optional[str] = Query(default="last_updated"),
    page: int = Query(default=1, ge=1),
):
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"mangas": [], "total": 0, "page": page, "per_page": 50})
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"mangas": [], "total": 0, "page": page, "per_page": 50})
    mangas = manga_data.get("mangas", [])
    user_data = auth.load_user_data(username)
    user_covers = user_data.get("covers", {}).get(str(library_id), {})
 
    perms = auth.resolve_permissions(username)
    blocked_tags = perms.get("blocked_tags", []) if not perms.get("is_admin") else []
    result = []
    for manga in mangas:
        dims = load_manga_dims(library_id, manga["name"])
        if blocked_tags:
            if any(t in blocked_tags for t in dims.get("tags", [])):
                continue
        m = dict(manga)
        cover = user_covers.get(m["id"]) or m.get("cover")
        if cover:
            cover_filename = os.path.basename(cover)
            m["cover_url"] = f"/covers/{library_id}/{quote(m['name'])}/{cover_filename}"
        else:
            m["cover_url"] = None
        if "is_complete" not in m:
            volumes = dims.get("volumes", {})
            chapters = dims.get("chapters", {})
            last_name = None
            if volumes:
                sorted_vols = sorted(volumes.values(), key=lambda v: natural_sort_key(v.get("name", "")))
                last_name = sorted_vols[-1].get("name", "")
            elif chapters:
                sorted_chs = sorted(chapters.values(), key=lambda c: natural_sort_key(c.get("name", "")))
                last_name = sorted_chs[-1].get("name", "")
            m["is_complete"] = bool(last_name and last_name.split()[-1] == "END")
        result.append(m)
 
    if sort == "last_updated":
        result.sort(key=lambda m: m.get("last_updated") or "", reverse=True)
 
    total = len(result)
    if sort == "last_updated":
        if page == 1:
            per_page = 50
            offset = 0
        else:
            per_page = 100
            offset = 50 + (page - 2) * 100
        page_items = result[offset: offset + per_page]
    else:
        per_page = total
        page_items = result
 
    return JSONResponse({"mangas": page_items, "total": total, "page": page, "per_page": per_page})

@app.get("/api/mangas/{library_id}/search")
def get_mangas_for_search(request: Request, library_id: int):
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"mangas": []})
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"mangas": []})
    mangas = manga_data.get("mangas", [])
    user_data = auth.load_user_data(username)
    user_covers = user_data.get("covers", {}).get(str(library_id), {})

    perms = auth.resolve_permissions(username)
    blocked_tags = perms.get("blocked_tags", []) if not perms.get("is_admin") else []
    result = []
    for manga in mangas:
        m = dict(manga)
        cover = user_covers.get(m["id"]) or m.get("cover")
        if cover:
            cover_filename = os.path.basename(cover)
            m["cover_url"] = f"/covers/{library_id}/{quote(m['name'])}/{cover_filename}"
        else:
            m["cover_url"] = None
        dims = load_manga_dims(library_id, m["name"])
        m["tags"]   = dims.get("tags", [])
        m["genres"] = dims.get("genres", [])
        if blocked_tags and any(t in blocked_tags for t in m["tags"]):
            continue
        result.append(m)
 
    return JSONResponse({"mangas": result})

@app.get("/api/manga/{library_id}/{manga_id}")
def get_manga(request: Request, library_id: int, manga_id: str):
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    manga = dict(manga)
    user_data = auth.load_user_data(username)
    user_covers = user_data.get("covers", {}).get(str(library_id), {})
    cover = user_covers.get(manga_id) or manga.get("cover")
    if cover:
        cover_filename = os.path.basename(cover)
        name, ext = os.path.splitext(cover_filename)
        manga["cover_url"]       = f"/covers/{library_id}/{quote(manga['name'])}/{cover_filename}"
        manga["cover_url_large"] = f"/covers/{library_id}/{quote(manga['name'])}/{name}+{ext}"
    else:
        manga["cover_url"]       = None
        manga["cover_url_large"] = None
    favourites = user_data.get("favourites", []) 
    manga["is_favourite"] = any(
        f["library_id"] == library_id and f["manga_id"] == manga_id
        for f in favourites
    )
    dims = load_manga_dims(library_id, manga["name"])
    perms = auth.resolve_permissions(username)
    blocked_tags = perms.get("blocked_tags", []) if not perms.get("is_admin") else []
    if blocked_tags and any(t in blocked_tags for t in dims.get("tags", [])):
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    manga["tags"]        = dims.get("tags", [])
    manga["genres"]      = dims.get("genres", [])
    manga["description"] = dims.get("description", "")
    return JSONResponse(manga)

@app.post("/api/manga/{library_id}/{manga_id}/favourite")
async def toggle_favourite(request: Request, library_id: int, manga_id: str):
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    user_data = auth.load_user_data(username)
    favourites = user_data.get("favourites", [])
    existing = next(
        (i for i, f in enumerate(favourites)
         if f["library_id"] == library_id and f["manga_id"] == manga_id),
        None
    )
    if existing is not None:
        favourites.pop(existing)
        is_favourite = False
    else:
        favourites.append({"library_id": library_id, "manga_id": manga_id})
        is_favourite = True
    user_data["favourites"] = favourites
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True, "is_favourite": is_favourite})

# ── COLLECTIONS ──────────────────────────────────────────────────────────────
# A collection groups whole manga entries (possibly across libraries) under one
# name, with its own description/tags/genres and a member order. Collections
# created by an admin are *shared* (visible to everyone, filtered per-viewer by
# the same can_access_library/is_manga_blocked rules as everything else);
# collections created by anyone else are *private* to that user, same scope as
# the old `lists` feature this replaces.

def _collections_file() -> Optional[str]:
    data_path = get_data_path()
    if not data_path:
        return None
    return os.path.join(data_path, "collections.json")

def load_shared_collections() -> dict:
    path = _collections_file()
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_shared_collections(data: dict):
    path = _collections_file()
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def _new_collection_id() -> str:
    return uuid.uuid4().hex[:12]

def _find_collection(username: str, collection_id: str):
    """Return (record, is_shared) or (None, None)."""
    shared = load_shared_collections()
    if collection_id in shared:
        return shared[collection_id], True
    user_data = auth.load_user_data(username)
    private = user_data.get("collections", {})
    if collection_id in private:
        return private[collection_id], False
    return None, None

def _save_collection(username: str, collection_id: str, record: dict, is_shared: bool):
    if is_shared:
        shared = load_shared_collections()
        shared[collection_id] = record
        save_shared_collections(shared)
    else:
        user_data = auth.load_user_data(username)
        private = user_data.get("collections", {})
        private[collection_id] = record
        user_data["collections"] = private
        auth.save_user_data(username, user_data)

def _delete_collection_record(username: str, collection_id: str, is_shared: bool):
    if is_shared:
        shared = load_shared_collections()
        shared.pop(collection_id, None)
        save_shared_collections(shared)
    else:
        user_data = auth.load_user_data(username)
        private = user_data.get("collections", {})
        private.pop(collection_id, None)
        user_data["collections"] = private
        auth.save_user_data(username, user_data)

def _can_edit_collection(username: str, is_shared: bool) -> bool:
    if not is_shared:
        return True
    return bool(auth.resolve_permissions(username).get("is_admin"))

def _lookup_manga(library_id: int, manga_id: str) -> Optional[dict]:
    manga_data = load_app_data().get("manga_data", {}).get(str(library_id), {})
    return next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)

def _visible_members(username: str, members: list) -> list:
    """Members filtered by library access + blocked tags — same total-lockout rule as everywhere else."""
    visible = []
    for m in members:
        if not auth.can_access_library(username, m["library_id"]):
            continue
        manga = _lookup_manga(m["library_id"], m["manga_id"])
        if not manga:
            continue
        dims = load_manga_dims(m["library_id"], manga["name"])
        if auth.is_manga_blocked(username, dims.get("tags", [])):
            continue
        visible.append(m)
    return visible

def _resolve_member_cover_urls(username: str, library_id: int, manga_id: str) -> tuple:
    """Return (cover_url, cover_url_large) or (None, None)."""
    manga = _lookup_manga(library_id, manga_id)
    if not manga:
        return None, None
    user_data = auth.load_user_data(username)
    user_covers = user_data.get("covers", {}).get(str(library_id), {})
    cover = user_covers.get(manga_id) or manga.get("cover")
    if not cover:
        return None, None
    filename = os.path.basename(cover)
    name, ext = os.path.splitext(filename)
    base = f"/covers/{library_id}/{quote(manga['name'])}"
    return f"{base}/{filename}", f"{base}/{name}+{ext}"

def _resolve_member_cover_url(username: str, library_id: int, manga_id: str) -> Optional[str]:
    return _resolve_member_cover_urls(username, library_id, manga_id)[0]

def _resolve_collection_cover_urls(username: str, collection_id: str, visible_members: list) -> tuple:
    """Return (cover_url, cover_url_large) or (None, None)."""
    user_data = auth.load_user_data(username)
    override = user_data.get("collection_covers", {}).get(collection_id)
    if override:
        filename = override["filename"]
        name, ext = os.path.splitext(filename)
        base = f"/covers/{override['library_id']}/{quote(override['manga_name'])}"
        return f"{base}/{filename}", f"{base}/{name}+{ext}"
    if not visible_members:
        return None, None
    first = visible_members[0]
    return _resolve_member_cover_urls(username, first["library_id"], first["manga_id"])

def _resolve_collection_cover(username: str, collection_id: str, visible_members: list) -> Optional[str]:
    return _resolve_collection_cover_urls(username, collection_id, visible_members)[0]

def _sync_collection_derived_fields(record: dict):
    """Recompute description (from the first member) / tags / genres (union of
    all members) whenever membership or order changes — but only for fields the
    owner/admin hasn't manually customized. Uses the full member list, not any
    one viewer's filtered view, since these are shared/owner-level fields."""
    members = record.get("members", [])
    if not record.get("description_customized"):
        description = ""
        if members:
            first = members[0]
            manga = _lookup_manga(first["library_id"], first["manga_id"])
            if manga:
                description = load_manga_dims(first["library_id"], manga["name"]).get("description", "")
        record["description"] = description
    if not record.get("tags_customized") or not record.get("genres_customized"):
        tagset, genreset = set(), set()
        for m in members:
            manga = _lookup_manga(m["library_id"], m["manga_id"])
            if not manga:
                continue
            dims = load_manga_dims(m["library_id"], manga["name"])
            tagset.update(dims.get("tags", []))
            genreset.update(dims.get("genres", []))
        if not record.get("tags_customized"):
            record["tags"] = sorted(tagset)
        if not record.get("genres_customized"):
            record["genres"] = sorted(genreset)

def _collection_summary(username: str, cid: str, record: dict, is_shared: bool, visible_members: list) -> dict:
    cover_url, cover_url_large = _resolve_collection_cover_urls(username, cid, visible_members)
    return {
        "id":             cid,
        "name":           record.get("name", ""),
        "shared":         is_shared,
        "can_edit":       _can_edit_collection(username, is_shared),
        "member_count":   len(visible_members),
        "cover_url":      cover_url,
        "cover_url_large": cover_url_large,
    }

@app.get("/api/collections")
def get_collections(request: Request):
    username = auth.get_current_user(request)
    user_data = auth.load_user_data(username)
    result = []
    for cid, rec in user_data.get("collections", {}).items():
        visible = _visible_members(username, rec.get("members", []))
        result.append(_collection_summary(username, cid, rec, False, visible))
    if not user_data.get("hide_admin_collections", False):
        for cid, rec in load_shared_collections().items():
            visible = _visible_members(username, rec.get("members", []))
            # Total lockout only applies to viewers who can't manage the
            # collection themselves — an editor (any admin) must always see
            # their own shared collections, empty or not, or a brand-new
            # collection would 404 out of existence the instant it's created.
            if not visible and not _can_edit_collection(username, True):
                continue
            result.append(_collection_summary(username, cid, rec, True, visible))
    return JSONResponse({"collections": result})

@app.post("/api/collections")
async def create_collection(request: Request):
    username = auth.get_current_user(request)
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Empty name"}, status_code=400)
    is_shared = bool(auth.resolve_permissions(username).get("is_admin"))
    cid = _new_collection_id()
    record = {
        "id": cid, "name": name, "shared": is_shared,
        "members": [],
        "description": "", "description_customized": False,
        "tags": [], "tags_customized": False,
        "genres": [], "genres_customized": False,
        "created_at": datetime.now().isoformat(),
    }
    _save_collection(username, cid, record, is_shared)
    return JSONResponse({"ok": True, "id": cid, "shared": is_shared})

@app.get("/api/collections/membership")
def get_collections_membership(request: Request):
    """manga_id (per library) -> collection_id, for the app-wide click-interception:
    clicking a manga tile that belongs to a collection visible to this user opens
    the collection instead of the manga's own page."""
    username = auth.get_current_user(request)
    user_data = auth.load_user_data(username)
    membership = {}
    for cid, rec in user_data.get("collections", {}).items():
        for m in _visible_members(username, rec.get("members", [])):
            membership.setdefault(f"{m['library_id']}:{m['manga_id']}", cid)
    if not user_data.get("hide_admin_collections", False):
        for cid, rec in load_shared_collections().items():
            visible = _visible_members(username, rec.get("members", []))
            if not visible:
                continue
            for m in visible:
                membership.setdefault(f"{m['library_id']}:{m['manga_id']}", cid)
    return JSONResponse({"membership": membership})

@app.get("/api/collections/{collection_id}")
def get_collection(request: Request, collection_id: str):
    username = auth.get_current_user(request)
    record, is_shared = _find_collection(username, collection_id)
    if record is None:
        return JSONResponse({"error": "Collection not found"}, status_code=404)
    visible = _visible_members(username, record.get("members", []))
    if is_shared and not visible and not _can_edit_collection(username, is_shared):
        return JSONResponse({"error": "Collection not found"}, status_code=404)
    members_out = []
    for m in visible:
        manga = _lookup_manga(m["library_id"], m["manga_id"])
        cover_url, cover_url_large = _resolve_member_cover_urls(username, m["library_id"], m["manga_id"])
        members_out.append({
            "library_id":      m["library_id"],
            "manga_id":        m["manga_id"],
            "manga_name":      m["manga_name"],
            "cover_url":       cover_url,
            "cover_url_large": cover_url_large,
            "manga_type":      manga.get("manga_type") if manga else None,
            "is_complete":     manga.get("is_complete", False) if manga else False,
        })
    cover_url, cover_url_large = _resolve_collection_cover_urls(username, collection_id, visible)
    return JSONResponse({
        "id":              collection_id,
        "name":            record.get("name", ""),
        "shared":          is_shared,
        "can_edit":        _can_edit_collection(username, is_shared),
        "description":     record.get("description", ""),
        "tags":            record.get("tags", []),
        "genres":          record.get("genres", []),
        "members":         members_out,
        "cover_url":       cover_url,
        "cover_url_large": cover_url_large,
    })

@app.delete("/api/collections/{collection_id}")
def delete_collection(request: Request, collection_id: str):
    username = auth.get_current_user(request)
    record, is_shared = _find_collection(username, collection_id)
    if record is None:
        return JSONResponse({"ok": False, "error": "Collection not found"}, status_code=404)
    if not _can_edit_collection(username, is_shared):
        return JSONResponse({"ok": False, "error": "Permission denied"}, status_code=403)
    _delete_collection_record(username, collection_id, is_shared)
    return JSONResponse({"ok": True})

@app.post("/api/collections/{collection_id}/rename")
async def rename_collection(request: Request, collection_id: str):
    username = auth.get_current_user(request)
    record, is_shared = _find_collection(username, collection_id)
    if record is None:
        return JSONResponse({"ok": False, "error": "Collection not found"}, status_code=404)
    if not _can_edit_collection(username, is_shared):
        return JSONResponse({"ok": False, "error": "Permission denied"}, status_code=403)
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Empty name"}, status_code=400)
    record["name"] = name
    _save_collection(username, collection_id, record, is_shared)
    return JSONResponse({"ok": True})

@app.post("/api/collections/{collection_id}/description")
async def set_collection_description(request: Request, collection_id: str):
    username = auth.get_current_user(request)
    record, is_shared = _find_collection(username, collection_id)
    if record is None:
        return JSONResponse({"ok": False, "error": "Collection not found"}, status_code=404)
    if not _can_edit_collection(username, is_shared):
        return JSONResponse({"ok": False, "error": "Permission denied"}, status_code=403)
    body = await request.json()
    record["description"] = body.get("description", "").strip()
    record["description_customized"] = True
    _save_collection(username, collection_id, record, is_shared)
    return JSONResponse({"ok": True})

@app.post("/api/collections/{collection_id}/tags/add")
async def add_collection_tag(request: Request, collection_id: str):
    username = auth.get_current_user(request)
    record, is_shared = _find_collection(username, collection_id)
    if record is None:
        return JSONResponse({"ok": False, "error": "Collection not found"}, status_code=404)
    if not _can_edit_collection(username, is_shared):
        return JSONResponse({"ok": False, "error": "Permission denied"}, status_code=403)
    body = await request.json()
    tag = body.get("tag", "").strip()
    if not tag:
        return JSONResponse({"ok": False, "error": "Empty tag"}, status_code=400)
    tags = record.get("tags", [])
    if tag not in tags:
        tags.append(tag)
    record["tags"] = tags
    record["tags_customized"] = True
    _save_collection(username, collection_id, record, is_shared)
    return JSONResponse({"ok": True, "tags": tags})

@app.post("/api/collections/{collection_id}/tags/remove")
async def remove_collection_tags(request: Request, collection_id: str):
    username = auth.get_current_user(request)
    record, is_shared = _find_collection(username, collection_id)
    if record is None:
        return JSONResponse({"ok": False, "error": "Collection not found"}, status_code=404)
    if not _can_edit_collection(username, is_shared):
        return JSONResponse({"ok": False, "error": "Permission denied"}, status_code=403)
    body = await request.json()
    tags_to_remove = body.get("tags", [])
    record["tags"] = [t for t in record.get("tags", []) if t not in tags_to_remove]
    record["tags_customized"] = True
    _save_collection(username, collection_id, record, is_shared)
    return JSONResponse({"ok": True, "tags": record["tags"]})

@app.post("/api/collections/{collection_id}/genres/add")
async def add_collection_genre(request: Request, collection_id: str):
    username = auth.get_current_user(request)
    record, is_shared = _find_collection(username, collection_id)
    if record is None:
        return JSONResponse({"ok": False, "error": "Collection not found"}, status_code=404)
    if not _can_edit_collection(username, is_shared):
        return JSONResponse({"ok": False, "error": "Permission denied"}, status_code=403)
    body = await request.json()
    genre = body.get("genre", "").strip()
    if not genre:
        return JSONResponse({"ok": False, "error": "Empty genre"}, status_code=400)
    genres = record.get("genres", [])
    if genre not in genres:
        genres.append(genre)
    record["genres"] = genres
    record["genres_customized"] = True
    _save_collection(username, collection_id, record, is_shared)
    return JSONResponse({"ok": True, "genres": genres})

@app.post("/api/collections/{collection_id}/genres/remove")
async def remove_collection_genres(request: Request, collection_id: str):
    username = auth.get_current_user(request)
    record, is_shared = _find_collection(username, collection_id)
    if record is None:
        return JSONResponse({"ok": False, "error": "Collection not found"}, status_code=404)
    if not _can_edit_collection(username, is_shared):
        return JSONResponse({"ok": False, "error": "Permission denied"}, status_code=403)
    body = await request.json()
    genres_to_remove = body.get("genres", [])
    record["genres"] = [g for g in record.get("genres", []) if g not in genres_to_remove]
    record["genres_customized"] = True
    _save_collection(username, collection_id, record, is_shared)
    return JSONResponse({"ok": True, "genres": record["genres"]})

@app.put("/api/collections/{collection_id}/members/add")
async def add_collection_member(request: Request, collection_id: str):
    username = auth.get_current_user(request)
    record, is_shared = _find_collection(username, collection_id)
    if record is None:
        return JSONResponse({"ok": False, "error": "Collection not found"}, status_code=404)
    if not _can_edit_collection(username, is_shared):
        return JSONResponse({"ok": False, "error": "Permission denied"}, status_code=403)
    body = await request.json()
    library_id = body.get("library_id")
    manga_id   = body.get("manga_id", "").strip()
    manga_name = body.get("manga_name", "").strip()
    if library_id is None or not manga_id or not manga_name:
        return JSONResponse({"ok": False, "error": "Missing library_id, manga_id or manga_name"}, status_code=400)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"ok": False, "error": "Library not found"}, status_code=404)
    members = record.get("members", [])
    if not any(m["library_id"] == library_id and m["manga_id"] == manga_id for m in members):
        members.append({"library_id": library_id, "manga_id": manga_id, "manga_name": manga_name})
    record["members"] = members
    _sync_collection_derived_fields(record)
    _save_collection(username, collection_id, record, is_shared)
    return JSONResponse({"ok": True})

@app.put("/api/collections/{collection_id}/members/remove")
async def remove_collection_member(request: Request, collection_id: str):
    username = auth.get_current_user(request)
    record, is_shared = _find_collection(username, collection_id)
    if record is None:
        return JSONResponse({"ok": False, "error": "Collection not found"}, status_code=404)
    if not _can_edit_collection(username, is_shared):
        return JSONResponse({"ok": False, "error": "Permission denied"}, status_code=403)
    body = await request.json()
    library_id = body.get("library_id")
    manga_id   = body.get("manga_id", "").strip()
    members = record.get("members", [])
    record["members"] = [m for m in members if not (m["library_id"] == library_id and m["manga_id"] == manga_id)]
    _sync_collection_derived_fields(record)
    _save_collection(username, collection_id, record, is_shared)
    return JSONResponse({"ok": True})

@app.put("/api/collections/{collection_id}/reorder")
async def reorder_collection_members(request: Request, collection_id: str):
    username = auth.get_current_user(request)
    record, is_shared = _find_collection(username, collection_id)
    if record is None:
        return JSONResponse({"ok": False, "error": "Collection not found"}, status_code=404)
    if not _can_edit_collection(username, is_shared):
        return JSONResponse({"ok": False, "error": "Permission denied"}, status_code=403)
    body = await request.json()
    order = body.get("order", [])  # [{library_id, manga_id}, ...] in the desired sequence
    members = record.get("members", [])
    key_order = [(o.get("library_id"), o.get("manga_id")) for o in order]
    by_key = {(m["library_id"], m["manga_id"]): m for m in members}
    reordered = [by_key[k] for k in key_order if k in by_key]
    # append anything the caller didn't include (shouldn't normally happen)
    reordered += [m for m in members if (m["library_id"], m["manga_id"]) not in key_order]
    record["members"] = reordered
    _sync_collection_derived_fields(record)
    _save_collection(username, collection_id, record, is_shared)
    return JSONResponse({"ok": True})

@app.get("/api/collections/{collection_id}/cover-options")
def get_collection_cover_options(request: Request, collection_id: str):
    username = auth.get_current_user(request)
    record, is_shared = _find_collection(username, collection_id)
    if record is None:
        return JSONResponse({"error": "Collection not found"}, status_code=404)
    visible = _visible_members(username, record.get("members", []))
    if is_shared and not visible and not _can_edit_collection(username, is_shared):
        return JSONResponse({"error": "Collection not found"}, status_code=404)
    user_data = auth.load_user_data(username)
    override = user_data.get("collection_covers", {}).get(collection_id)
    covers_dir = get_covers_dir()
    extensions = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
    options = []
    for m in visible:
        manga = _lookup_manga(m["library_id"], m["manga_id"])
        if not manga:
            continue
        manga_covers_dir = os.path.join(covers_dir, str(m["library_id"]), manga["name"]) if covers_dir else None
        if not manga_covers_dir or not os.path.exists(manga_covers_dir):
            continue
        large_files = sorted(
            [f for f in os.listdir(manga_covers_dir)
             if os.path.splitext(f)[0].endswith('+') and os.path.splitext(f)[1].lower() in extensions],
            key=natural_sort_key
        )
        for filename in large_files:
            name, ext = os.path.splitext(filename)
            small_name = name[:-1] + ext
            is_selected = bool(override and override.get("library_id") == m["library_id"]
                                and override.get("manga_id") == m["manga_id"] and override.get("filename") == small_name)
            options.append({
                "library_id":  m["library_id"],
                "manga_id":    m["manga_id"],
                "manga_name":  m["manga_name"],
                "filename":    small_name,
                "url_large":   f"/covers/{m['library_id']}/{quote(manga['name'])}/{filename}",
                "url_small":   f"/covers/{m['library_id']}/{quote(manga['name'])}/{small_name}",
                "is_selected": is_selected,
            })
    return JSONResponse({"covers": options, "has_override": bool(override)})

@app.post("/api/collections/{collection_id}/cover")
async def set_collection_cover(request: Request, collection_id: str):
    username = auth.get_current_user(request)
    record, is_shared = _find_collection(username, collection_id)
    if record is None:
        return JSONResponse({"ok": False, "error": "Collection not found"}, status_code=404)
    body = await request.json()
    library_id = body.get("library_id")
    manga_id   = body.get("manga_id", "").strip()
    manga_name = body.get("manga_name", "").strip()
    filename   = body.get("filename", "").strip()
    if library_id is None or not manga_id or not manga_name or not filename:
        return JSONResponse({"ok": False, "error": "Missing fields"}, status_code=400)
    user_data = auth.load_user_data(username)
    overrides = user_data.get("collection_covers", {})
    overrides[collection_id] = {"library_id": library_id, "manga_id": manga_id, "manga_name": manga_name, "filename": filename}
    user_data["collection_covers"] = overrides
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.delete("/api/collections/{collection_id}/cover")
def clear_collection_cover(request: Request, collection_id: str):
    username = auth.get_current_user(request)
    user_data = auth.load_user_data(username)
    overrides = user_data.get("collection_covers", {})
    overrides.pop(collection_id, None)
    user_data["collection_covers"] = overrides
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.get("/api/tags")
def get_all_tags(request: Request):
    username = auth.get_current_user(request)
    perms = auth.resolve_permissions(username)
    blocked = perms.get("blocked_tags", []) if not perms.get("is_admin") else []
    data = load_app_data()
    all_tags = [t for t in data.get("all_tags", []) if t not in blocked]
    return JSONResponse({"tags": all_tags})


def load_admin(username: str = "admin") -> dict:
    return auth.load_user_data(username)
 
def save_admin(data: dict, username: str = "admin"):
    auth.save_user_data(username, data)
    

@app.get("/api/admin/status")
def get_admin_status(request: Request):
    username = auth.get_current_user(request)
    return JSONResponse(auth.load_user_data(username)) 

@app.get("/api/manga/{library_id}/{manga_id}/covers")
def get_manga_covers(request: Request, library_id: int, manga_id: str):
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
    if auth.is_manga_blocked(username, dims.get("tags", [])):
        return JSONResponse({"error": "Manga not found"}, status_code=404)

    covers_dir = get_covers_dir()
    manga_covers_dir = os.path.join(covers_dir, str(library_id), manga["name"])
    if not os.path.exists(manga_covers_dir):
        return JSONResponse({"covers": []})
 
    extensions = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
    all_files = os.listdir(manga_covers_dir)
    large_files = sorted(
        [f for f in all_files
         if os.path.splitext(f)[0].endswith('+')
         and os.path.splitext(f)[1].lower() in extensions],
        key=natural_sort_key
    )
 
    user_data = auth.load_user_data(username)
    selected  = user_data.get("covers", {}).get(str(library_id), {}).get(manga_id)
 
    covers = []
    for filename in large_files:
        name, ext = os.path.splitext(filename)
        small_name = name[:-1] + ext
        covers.append({
            "filename":    small_name,
            "url_large":   f"/covers/{library_id}/{quote(manga['name'])}/{filename}",
            "url_small":   f"/covers/{library_id}/{quote(manga['name'])}/{small_name}",
            "is_selected": small_name == selected or (selected is None and small_name == manga.get("cover")),
        })
    return JSONResponse({"covers": covers, "selected": selected or manga.get("cover")})


@app.post("/api/manga/{library_id}/{manga_id}/cover")
async def set_manga_cover(request: Request, library_id: int, manga_id: str):
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    body = await request.json()
    filename = body.get("filename", "").strip()
    if not filename:
        return JSONResponse({"ok": False, "error": "No filename provided"}, status_code=400)
    user_data = auth.load_user_data(username)
    if "covers" not in user_data:
        user_data["covers"] = {}
    if str(library_id) not in user_data["covers"]:
        user_data["covers"][str(library_id)] = {}
    user_data["covers"][str(library_id)][manga_id] = filename
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})


@app.post("/api/manga/{library_id}/{manga_id}/covers/delete")
async def delete_manga_cover(request: Request, library_id: int, manga_id: str):
    """
    Delete one cover file (small + large '+' variant) from a manga's covers
    directory — for pruning wrong covers pulled in by the metadata fetch.
    Admin-only: the files are shared by every user. Cleans up every
    reference: the manga's default cover falls back to another remaining
    cover, and any per-user override pointing at the deleted file is
    removed so those users fall back to the default.
    """
    username = auth.get_current_user(request)
    if not auth.resolve_permissions(username).get("is_admin"):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    body = await request.json()
    filename = os.path.basename(body.get("filename", "").strip())
    if not filename:
        return JSONResponse({"ok": False, "error": "No filename provided"}, status_code=400)

    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)

    manga_covers_dir = os.path.join(get_covers_dir(), str(library_id), manga["name"])
    name, ext = os.path.splitext(filename)
    small_path = os.path.join(manga_covers_dir, filename)
    large_path = os.path.join(manga_covers_dir, f"{name}+{ext}")
    if not os.path.exists(small_path) and not os.path.exists(large_path):
        return JSONResponse({"ok": False, "error": "Cover not found"}, status_code=404)
    for p in (small_path, large_path):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass

    # Default cover pointed here → fall back to the first remaining cover.
    if manga.get("cover") == filename:
        extensions = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
        remaining_large = sorted(
            (f for f in os.listdir(manga_covers_dir)
             if os.path.splitext(f)[0].endswith('+')
             and os.path.splitext(f)[1].lower() in extensions),
            key=natural_sort_key,
        )
        if remaining_large:
            rname, rext = os.path.splitext(remaining_large[0])
            manga["cover"] = rname[:-1] + rext
        else:
            manga["cover"] = None
        save_app_data(data)

    # Per-user overrides pointing at the deleted file → remove, falling back
    # to the manga default.
    for u in auth._load_users().get("users", []):
        ud = auth.load_user_data(u["username"])
        sel = ud.get("covers", {}).get(str(library_id), {})
        if sel.get(manga_id) == filename:
            del sel[manga_id]
            auth.save_user_data(u["username"], ud)

    return JSONResponse({"ok": True, "new_default": manga.get("cover")})

def rebuild_all_tags(data: dict) -> list:
    tags = set()
    covers_dir = get_covers_dir()
    if not covers_dir:
        return []
    for lib_id, lib_data in data.get("manga_data", {}).items():
        for manga in lib_data.get("mangas", []):
            dims = load_manga_dims(int(lib_id), manga["name"])
            for tag in dims.get("tags", []):
                tags.add(tag)
    return sorted(tags)

@app.post("/api/manga/{library_id}/{manga_id}/tags/add")
async def add_tag(library_id: int, manga_id: str, request: Request):
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"ok": False, "error": "Library not found"}, status_code=404)
    if not auth.resolve_permissions(username).get("tags"):
        return JSONResponse({"ok": False, "error": "Permission denied"}, status_code=403)
    body = await request.json()
    tag = body.get("tag", "").strip()
    if not tag:
        return JSONResponse({"ok": False, "error": "Empty tag"}, status_code=400)
    perms = auth.resolve_permissions(username)
    if not perms.get("is_admin") and tag in perms.get("blocked_tags", []):
        return JSONResponse({"ok": False, "error": "Empty tag"}, status_code=400)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"ok": False, "error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"ok": False, "error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
    if auth.is_manga_blocked(username, dims.get("tags", [])):
        return JSONResponse({"ok": False, "error": "Manga not found"}, status_code=404)
    if tag not in dims["tags"]:
        dims["tags"].append(tag)
    save_manga_dims(library_id, manga["name"], dims)
    data["all_tags"] = rebuild_all_tags(data)
    save_app_data(data)
    return JSONResponse({"ok": True, "tags": dims["tags"], "all_tags": data["all_tags"]})

@app.post("/api/manga/{library_id}/{manga_id}/tags/remove")
async def remove_tags(library_id: int, manga_id: str, request: Request):
    username = auth.get_current_user(request)
    if not auth.resolve_permissions(username).get("tags"):
        return JSONResponse({"ok": False, "error": "Permission denied"}, status_code=403)
    body = await request.json()
    tags_to_remove = body.get("tags", [])
    remove_globally = body.get("global", False)
    if not remove_globally and not auth.can_access_library(username, library_id):
        return JSONResponse({"ok": False, "error": "Library not found"}, status_code=404)
    data = load_app_data()

    if remove_globally:
        for lib_id, lib_data in data.get("manga_data", {}).items():
            for m in lib_data.get("mangas", []):
                dims = load_manga_dims(int(lib_id), m["name"])
                dims["tags"] = [t for t in dims.get("tags", []) if t not in tags_to_remove]
                save_manga_dims(int(lib_id), m["name"], dims)
        data["all_tags"] = rebuild_all_tags(data)
    else:
        manga_data = data.get("manga_data", {}).get(str(library_id))
        if not manga_data:
            return JSONResponse({"ok": False, "error": "Library not found"}, status_code=404)
        manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
        if not manga:
            return JSONResponse({"ok": False, "error": "Manga not found"}, status_code=404)
        dims = load_manga_dims(library_id, manga["name"])
        if auth.is_manga_blocked(username, dims.get("tags", [])):
            return JSONResponse({"ok": False, "error": "Manga not found"}, status_code=404)
        dims["tags"] = [t for t in dims.get("tags", []) if t not in tags_to_remove]
        save_manga_dims(library_id, manga["name"], dims)

    save_app_data(data)
    return JSONResponse({"ok": True, "tags": dims.get("tags", []) if not remove_globally else [], "all_tags": data.get("all_tags", [])})

@app.get("/api/genres")
def get_all_genres():
    data = load_app_data()
    return JSONResponse({"genres": data.get("all_genres", [])})

def rebuild_all_genres(data: dict) -> list:
    genres = set()
    covers_dir = get_covers_dir()
    if not covers_dir:
        return []
    for lib_id, lib_data in data.get("manga_data", {}).items():
        for manga in lib_data.get("mangas", []):
            dims = load_manga_dims(int(lib_id), manga["name"])
            for genre in dims.get("genres", []):
                genres.add(genre)
    return sorted(genres)

@app.post("/api/manga/{library_id}/{manga_id}/genres/add")
async def add_genre(library_id: int, manga_id: str, request: Request):
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"ok": False, "error": "Library not found"}, status_code=404)
    if not auth.resolve_permissions(username).get("genres"):
        return JSONResponse({"ok": False, "error": "Permission denied"}, status_code=403)
    body = await request.json()
    genre = body.get("genre", "").strip()
    if not genre:
        return JSONResponse({"ok": False, "error": "Empty genre"}, status_code=400)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"ok": False, "error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"ok": False, "error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
    if auth.is_manga_blocked(username, dims.get("tags", [])):
        return JSONResponse({"ok": False, "error": "Manga not found"}, status_code=404)
    if genre not in dims["genres"]:
        dims["genres"].append(genre)
    save_manga_dims(library_id, manga["name"], dims)
    data["all_genres"] = rebuild_all_genres(data)
    save_app_data(data)
    return JSONResponse({"ok": True, "genres": dims["genres"], "all_genres": data["all_genres"]})

@app.post("/api/manga/{library_id}/{manga_id}/genres/remove")
async def remove_genres(library_id: int, manga_id: str, request: Request):
    username = auth.get_current_user(request)
    if not auth.resolve_permissions(username).get("genres"):
        return JSONResponse({"ok": False, "error": "Permission denied"}, status_code=403)
    body = await request.json()
    genres_to_remove = body.get("genres", [])
    remove_globally = body.get("global", False)
    if not remove_globally and not auth.can_access_library(username, library_id):
        return JSONResponse({"ok": False, "error": "Library not found"}, status_code=404)
    data = load_app_data()
    if remove_globally:
        for lib_id, lib_data in data.get("manga_data", {}).items():
            for m in lib_data.get("mangas", []):
                dims = load_manga_dims(int(lib_id), m["name"])
                dims["genres"] = [g for g in dims.get("genres", []) if g not in genres_to_remove]
                save_manga_dims(int(lib_id), m["name"], dims)
        data["all_genres"] = rebuild_all_genres(data)
        save_app_data(data)
        return JSONResponse({"ok": True, "genres": [], "all_genres": data["all_genres"]})
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"ok": False, "error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"ok": False, "error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
    if auth.is_manga_blocked(username, dims.get("tags", [])):
        return JSONResponse({"ok": False, "error": "Manga not found"}, status_code=404)
    dims["genres"] = [g for g in dims.get("genres", []) if g not in genres_to_remove]
    save_manga_dims(library_id, manga["name"], dims)
    save_app_data(data)
    return JSONResponse({"ok": True, "genres": dims["genres"], "all_genres": data.get("all_genres", [])})

@app.post("/api/manga/{library_id}/{manga_id}/description")
async def save_description(library_id: int, manga_id: str, request: Request):
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"ok": False, "error": "Library not found"}, status_code=404)
    if not auth.resolve_permissions(username).get("description"):
        return JSONResponse({"ok": False, "error": "Permission denied"}, status_code=403)
    body = await request.json()
    description = body.get("description", "").strip()
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"ok": False, "error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"ok": False, "error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
    if auth.is_manga_blocked(username, dims.get("tags", [])):
        return JSONResponse({"ok": False, "error": "Manga not found"}, status_code=404)
    dims["description"] = description
    save_manga_dims(library_id, manga["name"], dims)
    return JSONResponse({"ok": True})

def _metadata_field_done(dims: dict, field: str) -> bool:
    """
    Whether a metadata field has already been fetched for this manga.

    Reads the per-field timestamps under dims["metadata_mtimes"]. For manga
    imported before per-field mtimes existed, the legacy single
    dims["metadata_mtime"] is treated as covering the three text fields it
    used to stamp (description/genres/tags) but NOT cover, which was always
    a separate, later addition.
    """
    if field in (dims.get("metadata_mtimes") or {}):
        return True
    if field in ("description", "genres", "tags") and dims.get("metadata_mtime"):
        return True
    return False


@app.get("/api/manga/{library_id}/{manga_id}/local-metadata")
def get_local_metadata(request: Request, library_id: int, manga_id: str):
    """
    Aggregated ComicInfo.xml data for this manga, shaped as a selectable
    candidate for the Fetch Metadata popup — same candidate shape
    resolve_field_value() already knows how to read (plain-string genres/tags),
    so it plugs into the existing apply-metadata endpoint with no changes there.
    """
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
    if auth.is_manga_blocked(username, dims.get("tags", [])):
        return JSONResponse({"error": "Manga not found"}, status_code=404)

    found = find_comicinfo_for_manga(manga.get("path", ""), dims)
    if not found:
        return JSONResponse({"candidate": None})

    return JSONResponse({"candidate": {
        "source": "local",
        "title_romaji": "Local file (ComicInfo.xml)",
        "description": found["description"],
        "genres": found["genres"],
        "tags": found["tags"],
        "cover_url_medium": None,
    }})

@app.get("/api/manga/{library_id}/{manga_id}/search-metadata")
async def search_metadata_endpoint(request: Request, library_id: int, manga_id: str, q: str = ""):
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    perms = auth.resolve_permissions(username)
    if not (perms.get("is_admin") or perms.get("tags") or perms.get("genres") or perms.get("description")):
        return JSONResponse({"error": "Permission denied"}, status_code=403)
    query = q.strip() or manga_id
    try:
        results = await metadata_fetch.search_anilist_manga(query, per_page=8)
        scored  = metadata_fetch.score_all_candidates(query, results)
        return JSONResponse({"candidates": scored})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/manga/{library_id}/{manga_id}/apply-metadata")
async def apply_metadata_endpoint(request: Request, library_id: int, manga_id: str):
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    perms = auth.resolve_permissions(username)
    if not (perms.get("is_admin") or perms.get("tags") or perms.get("genres") or perms.get("description")):
        return JSONResponse({"error": "Permission denied"}, status_code=403)
    body = await request.json()
    candidate = body.get("candidate", {})
    fields = set(body.get("fields", ["description", "genres", "tags", "cover"]))
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    if auth.is_manga_blocked(username, load_manga_dims(library_id, manga["name"]).get("tags", [])):
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    try:
        # The chosen candidate (from the AniList search) is the primary source.
        # Look up the SAME SERIES on MangaDex to fill any field AniList is
        # missing and to provide a higher-res cover fallback.
        mangadex_best = None
        try:
            if candidate.get("anilist_id") is not None:
                # AniList candidate: exact ID join via MangaDex's links.al,
                # multi-name fuzzy fallback. Deliberately NO weaker fallback
                # beyond that — None means "no confident MangaDex counterpart",
                # and a missing cover beats a wrong-series cover.
                mangadex_best = await metadata_fetch.find_mangadex_for_anilist(candidate)
            else:
                # Non-AniList candidate (e.g. the "Local file" card): single
                # best-title match, as before.
                title = (
                    candidate.get("title_english")
                    or candidate.get("title_romaji")
                    or candidate.get("title_native")
                    or manga["name"]
                )
                md_results = await metadata_fetch.search_mangadex_manga(title)
                mangadex_best = metadata_fetch.best_match(title, md_results)
        except Exception:
            mangadex_best = None

        applied = [f for f in fields if f in ("description", "genres", "tags")]
        if "cover" in fields:
            if await fetch_and_set_cover(library_id, manga, candidate, mangadex_best):
                applied.append("cover")
                save_app_data(data)
        if applied:
            metadata_fetch.apply_resolved_metadata(
                library_id, manga["name"], candidate, mangadex_best, applied,
                load_manga_dims, save_manga_dims,
            )
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/libraries/{library_id}/scan-metadata")
async def scan_library_metadata(request: Request, library_id: int):
    username = auth.get_current_user(request)
    if not auth.resolve_permissions(username).get("is_admin"):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    requested_fields = set(body.get("fields", ["description", "genres", "tags", "cover"]))
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    mangas = manga_data.get("mangas", [])
    manga_formats = set(metadata_fetch.ANILIST_MANGA_FORMATS)
    auto_matched = skipped = no_match = errors = 0
    covers_changed = False
    # Once AniList rate-limits us, stop hitting it for the rest of the scan and
    # run on MangaDex alone — the whole point of the fallback.
    anilist_enabled = True

    for manga in mangas:
        dims = load_manga_dims(library_id, manga["name"])
        pending = [f for f in requested_fields if not _metadata_field_done(dims, f)]
        if not pending:
            skipped += 1
            continue
        try:
            name = manga["name"]

            # ── AniList: accept only a single format-matched candidate ──
            anilist_primary = None
            if anilist_enabled:
                try:
                    a_results = await metadata_fetch.search_anilist_manga(name, per_page=8)
                    a_typed = [r for r in a_results if (r.get("format") or "").upper() in manga_formats]
                    if len(a_typed) == 1:
                        anilist_primary = a_typed[0]
                except httpx.HTTPStatusError as e:
                    if e.response is not None and e.response.status_code == 429:
                        anilist_enabled = False
                await asyncio.sleep(0.7)

            # ── MangaDex ──
            # With an AniList match in hand: find the SAME SERIES on MangaDex
            # (exact links.al ID join, multi-name fuzzy fallback) — matching by
            # the folder name alone paired wrong-series covers with correct
            # AniList descriptions. Without one: fall back to the "single
            # confident entry" rule against the folder name, as before.
            mangadex_best = mangadex_single = None
            if anilist_primary:
                mangadex_best = await metadata_fetch.find_mangadex_for_anilist(anilist_primary)
            else:
                md_results = await metadata_fetch.search_mangadex_manga(name)
                scored_md = metadata_fetch.score_all_candidates(name, md_results)
                strong_md = [c for c in scored_md if c["match_score"] >= 0.85]
                mangadex_single = strong_md[0] if len(strong_md) == 1 else None

            # ── Decide source(s) ──
            # AniList single match → primary, MangaDex fills gaps + cover.
            # AniList ambiguous/none/rate-limited → fall back to MangaDex, but
            # only when it has a single (confident) entry, per the spec.
            if anilist_primary:
                primary, fallback = anilist_primary, mangadex_best
                anilist_for_cover, mangadex_for_cover = anilist_primary, mangadex_best
            elif mangadex_single:
                primary, fallback = mangadex_single, None
                anilist_for_cover, mangadex_for_cover = None, mangadex_single
            else:
                no_match += 1
                await asyncio.sleep(0.3)
                continue

            applied = [f for f in pending if f in ("description", "genres", "tags")]
            if "cover" in pending:
                if await fetch_and_set_cover(library_id, manga, anilist_for_cover, mangadex_for_cover):
                    applied.append("cover")
                    covers_changed = True
            if applied:
                metadata_fetch.apply_resolved_metadata(
                    library_id, name, primary, fallback, applied,
                    load_manga_dims, save_manga_dims,
                )
            auto_matched += 1
            await asyncio.sleep(0.3)
        except Exception:
            errors += 1
    if covers_changed:
        save_app_data(data)
    return JSONResponse({
        "auto_matched": auto_matched,
        "skipped": skipped,
        "no_match": no_match,
        "errors": errors,
        "total": len(mangas),
    })

@app.post("/api/settings/last-tab")
async def save_last_tab(request: Request):
    username = auth.get_current_user(request)
    body = await request.json()
    user_data = auth.load_user_data(username)
    user_data["last_tab"] = body.get("last_tab")
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.get("/api/manga/{library_id}/{manga_id}/bookmarks")
async def get_bookmarks(request: Request, library_id: int, manga_id: str):
    username  = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    if _is_manga_id_blocked(username, library_id, manga_id):
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    user_data = auth.load_user_data(username)
    key       = f"{library_id}:{manga_id}"
    bookmarks = user_data.get("bookmarks", {}).get(key, [])
    return JSONResponse({"bookmarks": bookmarks})

@app.post("/api/manga/{library_id}/{manga_id}/bookmarks")
async def save_bookmarks(request: Request, library_id: int, manga_id: str):
    username  = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    if _is_manga_id_blocked(username, library_id, manga_id):
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    body      = await request.json()
    user_data = auth.load_user_data(username)
    key       = f"{library_id}:{manga_id}"
    if "bookmarks" not in user_data:
        user_data["bookmarks"] = {}
    user_data["bookmarks"][key] = body.get("bookmarks", [])
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.post("/api/settings/tab-order")
async def save_tab_order(request: Request):
    username  = auth.get_current_user(request)
    body      = await request.json()
    user_data = auth.load_user_data(username)
    user_data["tab_order"] = body.get("tab_order", [])
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.post("/api/settings/reader")
async def save_reader_settings(request: Request):
    username   = auth.get_current_user(request)
    body       = await request.json()
    library_id = str(body.get("library_id", ""))
    if not library_id:
        return JSONResponse({"ok": False, "error": "Missing library_id"}, status_code=400)
    user_data    = auth.load_user_data(username)
    allowed_keys = {"mode", "padding", "direction", "stripWidth", "preloadRadius", "pdfScale"}
    by_tab       = user_data.get("reader_settings_by_tab", {})
    current      = by_tab.get(library_id, {})
    for key in allowed_keys:
        if key in body:
            current[key] = body[key]
    by_tab[library_id] = current
    user_data["reader_settings_by_tab"] = by_tab
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.post("/api/reading/progress")
async def save_reading_progress(request: Request):
    username = auth.get_current_user(request)
    body = await request.json()
    library_id  = str(body.get("library_id", ""))
    manga_id    = str(body.get("manga_id", ""))
    chapter_id  = body.get("chapter_id")
    page        = body.get("page", 0)
    completed_chapter_id = body.get("completed_chapter_id")

    if not library_id or not manga_id:
        return JSONResponse({"ok": False, "error": "Missing library_id or manga_id"}, status_code=400)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"ok": False, "error": "Library not found"}, status_code=404)

    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(library_id, {})
    manga = next((m for m in manga_data.get("mangas", []) if m["id"] == manga_id), None)
    manga_name = manga["name"] if manga else None
    dims = load_manga_dims(int(library_id), manga["name"]) if manga else {}
    if manga and auth.is_manga_blocked(username, dims.get("tags", [])):
        return JSONResponse({"ok": False, "error": "Manga not found"}, status_code=404)
    is_volume_manga = (manga.get("manga_type") == "case2" or bool(dims.get("volumes"))) if manga else False
    source_name = None
    if manga and chapter_id:
        src = (dims.get("chapters", {}).get(chapter_id)
               or dims.get("volumes", {}).get(chapter_id))
        if src:
            source_name = src.get("name")

    user_data = auth.load_user_data(username)
    history = user_data.setdefault("reading_history", {})
    lib_history = history.setdefault(library_id, {})

    if is_volume_manga:
        entry = lib_history.setdefault(manga_id, {
            "manga_name":          None,
            "last_read":           None,
            "last_volume_id":      None,
            "last_volume_name":    None,
            "last_page":           0,
            "furthest_volume":     None,
            "furthest_volume_name": None,
            "chapters":            {},
        })
        entry["last_read"]        = datetime.now().isoformat()
        entry["last_volume_id"]   = chapter_id
        entry["last_page"]        = page
        if manga_name:
            entry["manga_name"] = manga_name
        if source_name:
            entry["last_volume_name"] = source_name
        if chapter_id:
            ch_entry = entry["chapters"].setdefault(chapter_id, {})
            ch_entry["last_page"] = page

        if completed_chapter_id:
            completed_name = None
            src = dims.get("volumes", {}).get(completed_chapter_id)
            if src:
                completed_name = src.get("name")
            ch_entry = entry["chapters"].setdefault(completed_chapter_id, {})
            ch_entry["completed"] = True
            if completed_name:
                ch_entry["name"] = completed_name

            ordered_volumes = sorted(
                dims.get("volumes", {}).keys(),
                key=lambda vid: natural_sort_key(dims["volumes"][vid].get("name", vid))
            )
            furthest = None
            furthest_name = None
            for vid in ordered_volumes:
                if entry["chapters"].get(vid, {}).get("completed"):
                    furthest = vid
                    furthest_name = dims["volumes"][vid].get("name")
                else:
                    break
            entry["furthest_volume"]      = furthest
            entry["furthest_volume_name"] = furthest_name

    else:
        entry = lib_history.setdefault(manga_id, {
            "manga_name":            None,
            "last_read":             None,
            "last_chapter_id":       None,
            "last_chapter_name":     None,
            "last_page":             0,
            "furthest_chapter":      None,
            "furthest_chapter_name": None,
            "chapters":              {},
        })
        entry["last_read"]       = datetime.now().isoformat()
        entry["last_chapter_id"] = chapter_id
        entry["last_page"]       = page
        if manga_name:
            entry["manga_name"] = manga_name
        if source_name:
            entry["last_chapter_name"] = source_name

        if completed_chapter_id:
            completed_name = None
            src = (dims.get("chapters", {}).get(completed_chapter_id)
                   or dims.get("volumes", {}).get(completed_chapter_id))
            if src:
                completed_name = src.get("name")
            ch_entry = entry["chapters"].setdefault(completed_chapter_id, {})
            ch_entry["completed"] = True
            if completed_name:
                ch_entry["name"] = completed_name

            source_dict = dims.get("chapters") or dims.get("volumes", {})
            ordered_ids = sorted(
                source_dict.keys(),
                key=lambda cid: natural_sort_key(source_dict[cid].get("name", cid))
            )
            furthest = None
            furthest_name = None
            for cid in ordered_ids:
                if entry["chapters"].get(cid, {}).get("completed"):
                    furthest = cid
                    furthest_name = source_dict[cid].get("name")
                else:
                    break
            entry["furthest_chapter"]      = furthest
            entry["furthest_chapter_name"] = furthest_name

    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.get("/api/reading/history/{library_id}")
def get_reading_history(request: Request, library_id: int):
    username  = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"history": []})
    user_data = auth.load_user_data(username)
    lib_history = user_data.get("reading_history", {}).get(str(library_id), {})

    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id), {})
    mangas_by_id = {m["id"]: m for m in manga_data.get("mangas", [])}

    result = []
    for manga_id, entry in lib_history.items():
        if not entry.get("last_read"):
            continue
        manga = mangas_by_id.get(manga_id)
        if not manga:
            continue

        dims = load_manga_dims(library_id, manga["name"])
        if auth.is_manga_blocked(username, dims.get("tags", [])):
            continue
        is_volume_manga = manga.get("manga_type") == "case2"

        if is_volume_manga:
            ordered = sorted(
                dims.get("volumes", {}).keys(),
                key=lambda vid: natural_sort_key(dims["volumes"][vid].get("name", vid))
            )
            furthest_vid = entry.get("furthest_volume")
            furthest_idx = ordered.index(furthest_vid) + 1 if furthest_vid and furthest_vid in ordered else 0
            total_chapters = len(ordered)
            furthest_cid = furthest_vid
        else:
            furthest_cid = entry.get("furthest_chapter")
            furthest_idx = 0
            if furthest_cid:
                ordered = sorted(
                    dims.get("chapters", {}).keys(),
                    key=lambda cid: natural_sort_key(dims["chapters"][cid].get("name", cid))
                )
                if furthest_cid in ordered:
                    furthest_idx = ordered.index(furthest_cid) + 1
                total_chapters = len(ordered)
            else:
                total_chapters = len(dims.get("chapters", {}))

        result.append({
            "manga_id":             manga_id,
            "last_read":            entry.get("last_read"),
            "last_chapter_id":      entry.get("last_chapter_id"),
            "last_page":            entry.get("last_page", 0),
            "furthest_chapter":     furthest_cid,
            "furthest_chapter_idx": furthest_idx,
            "total_chapters":       total_chapters,
        })

    result.sort(key=lambda x: x["last_read"] or "", reverse=True)
    return JSONResponse({"history": result})

@app.get("/api/category-list/{library_id}/{category}")
def get_category_list(
    request: Request,
    library_id: int,
    category: str,
    page: int = Query(default=1, ge=1),
    seed: Optional[int] = Query(default=None),
):
    if category not in ("favourites", "last-read", "random"):
        return JSONResponse({"error": "Invalid category"}, status_code=400)

    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id), {})
    raw_mangas = manga_data.get("mangas", [])
    user_data = auth.load_user_data(username)
    user_covers = user_data.get("covers", {}).get(str(library_id), {})

    perms = auth.resolve_permissions(username)
    blocked_tags = perms.get("blocked_tags", []) if not perms.get("is_admin") else []

    mangas_by_id = {}
    for manga in raw_mangas:
        dims = load_manga_dims(library_id, manga["name"])
        if blocked_tags and any(t in blocked_tags for t in dims.get("tags", [])):
            continue
        m = dict(manga)
        cover = user_covers.get(m["id"]) or m.get("cover")
        if cover:
            cover_filename = os.path.basename(cover)
            m["cover_url"] = f"/covers/{library_id}/{quote(m['name'])}/{cover_filename}"
        else:
            m["cover_url"] = None
        mangas_by_id[m["id"]] = m

    lib_history = user_data.get("reading_history", {}).get(str(library_id), {})

    if category == "favourites":
        favourite_ids = [
            f["manga_id"] for f in user_data.get("favourites", [])
            if f.get("library_id") == library_id and f["manga_id"] in mangas_by_id
        ]
        rng = random.Random(seed) if seed is not None else random
        rng.shuffle(favourite_ids)
        ordered_ids = favourite_ids

    elif category == "last-read":
        history_entries = [
            (manga_id, entry) for manga_id, entry in lib_history.items()
            if entry.get("last_read") and manga_id in mangas_by_id
        ]
        history_entries.sort(key=lambda x: x[1]["last_read"], reverse=True)
        ordered_ids = [manga_id for manga_id, _ in history_entries]

    else:  # random
        all_ids = list(mangas_by_id.keys())
        rng = random.Random(seed) if seed is not None else random
        rng.shuffle(all_ids)
        ordered_ids = all_ids

    total = len(ordered_ids)
    per_page = 50
    offset = (page - 1) * per_page
    page_ids = ordered_ids[offset: offset + per_page]

    result = []
    for manga_id in page_ids:
        m = mangas_by_id[manga_id]
        entry = lib_history.get(manga_id)
        progress = 0
        if entry:
            dims = load_manga_dims(library_id, m["name"])
            is_volume_manga = m.get("manga_type") == "case2"
            if is_volume_manga:
                total_chapters = len(dims.get("volumes", {}))
                furthest_vid = entry.get("furthest_volume")
                ordered = sorted(
                    dims.get("volumes", {}).keys(),
                    key=lambda vid: natural_sort_key(dims["volumes"][vid].get("name", vid))
                )
                furthest_idx = ordered.index(furthest_vid) + 1 if furthest_vid and furthest_vid in ordered else 0
            else:
                total_chapters = len(dims.get("chapters", {}))
                furthest_cid = entry.get("furthest_chapter")
                furthest_idx = 0
                if furthest_cid:
                    ordered = sorted(
                        dims.get("chapters", {}).keys(),
                        key=lambda cid: natural_sort_key(dims["chapters"][cid].get("name", cid))
                    )
                    if furthest_cid in ordered:
                        furthest_idx = ordered.index(furthest_cid) + 1
            if total_chapters > 0:
                progress = round(furthest_idx / total_chapters * 100)
        result.append({
            "id":          m["id"],
            "title":       m["name"],
            "path":        m["path"],
            "cover":       m["cover_url"],
            "chapters":    m.get("chapters"),
            "is_complete": m.get("is_complete", False),
            "progress":    progress,
        })

    return JSONResponse({"mangas": result, "total": total, "page": page, "per_page": per_page})

@app.get("/api/reading/history/{library_id}/{manga_id}")
def get_manga_reading_history(request: Request, library_id: int, manga_id: str):
    username  = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    if _is_manga_id_blocked(username, library_id, manga_id):
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    user_data = auth.load_user_data(username)
    entry     = user_data.get("reading_history", {}).get(str(library_id), {}).get(manga_id, {})

    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id), {})
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    dims_for_check = load_manga_dims(library_id, manga["name"]) if manga else {}
    is_volume_manga = (manga.get("manga_type") == "case2" or bool(dims_for_check.get("volumes"))) if manga else False

    completed_ids = [
        cid for cid, ch in entry.get("chapters", {}).items()
        if ch.get("completed")
    ]

    if is_volume_manga:
        volume_pages = {
            cid: ch.get("last_page", 0)
            for cid, ch in entry.get("chapters", {}).items()
            if ch.get("last_page", 0) > 0
        }
        return JSONResponse({
            "completed_volume_ids": completed_ids,
            "last_volume_id":       entry.get("last_volume_id"),
            "last_page":            entry.get("last_page", 0),
            "volume_pages":         volume_pages,
        })
    else:
        return JSONResponse({
            "completed_chapter_ids": completed_ids,
            "last_chapter_id":       entry.get("last_chapter_id"),
            "last_page":             entry.get("last_page", 0),
        })

@app.get("/settings")
def settings_page(request: Request):
    username = auth.get_current_user(request)
    if not username:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "settings.html", {
        "theme_css": get_theme_css(username),
    })

@app.get("/search")
def search_page(request: Request):
    username = auth.get_current_user(request)
    if not username:
        return RedirectResponse("/login", status_code=302)
    if auth.must_change_password(username):
        return RedirectResponse("/settings", status_code=302)
    return templates.TemplateResponse(request, "search_page.html", {
        "theme_css": get_theme_css(username),
    })

@app.get("/collections")
def collections_list_page(request: Request):
    username = auth.get_current_user(request)
    if not username:
        return RedirectResponse("/login", status_code=302)
    if auth.must_change_password(username):
        return RedirectResponse("/settings", status_code=302)
    return templates.TemplateResponse(request, "collections_list.html", {
        "theme_css": get_theme_css(username),
    })

@app.get("/collection/{collection_id}")
def collection_detail_page(request: Request, collection_id: str):
    username = auth.get_current_user(request)
    if not username:
        return RedirectResponse("/login", status_code=302)
    if auth.must_change_password(username):
        return RedirectResponse("/settings", status_code=302)
    record, is_shared = _find_collection(username, collection_id)
    if record is not None:
        visible = _visible_members(username, record.get("members", []))
        if is_shared and not visible and not _can_edit_collection(username, is_shared):
            record = None
    if record is None:
        return RedirectResponse("/collections", status_code=302)
    return templates.TemplateResponse(request, "collection_detail.html", {
        "collection_id": collection_id,
        "theme_css": get_theme_css(username),
    })

@app.get("/")
def manga_list(request: Request):
    username = auth.get_current_user(request)
    if not username:
        return RedirectResponse("/login", status_code=302)
    if auth.must_change_password(username):
        return RedirectResponse("/settings", status_code=302)
    data = load_app_data()
    admin = load_admin()
    libraries = data.get("libraries", [])
    tab_order = admin.get("tab_order", [])
    if tab_order:
        order_index = {lib_id: i for i, lib_id in enumerate(tab_order)}
        libraries = sorted(libraries, key=lambda l: order_index.get(l["id"], len(tab_order)))
    perms = auth.resolve_permissions(username)
    if not perms.get("is_admin"):
        lib_perms = perms.get("libraries", {})
        libraries = [l for l in libraries if lib_perms.get(str(l["id"]), True) is not False]
    hidden_libraries = set(str(x) for x in auth.load_user_data(username).get("hidden_libraries", []))
    if hidden_libraries:
        libraries = [l for l in libraries if str(l["id"]) not in hidden_libraries]
    return templates.TemplateResponse(request, "manga_list.html", {
        "libraries": libraries,
        "last_tab":  admin.get("last_tab", None),
        "theme_css": get_theme_css(username),
    })

@app.get("/manga/{library_id}/{manga_id}")
def manga_detail(request: Request, library_id: int, manga_id: str):
    username = auth.get_current_user(request)
    if not username:
        return RedirectResponse("/login", status_code=302)
    if auth.must_change_password(username):
        return RedirectResponse("/settings", status_code=302)
    if not auth.can_access_library(username, library_id):
        return RedirectResponse("/", status_code=302)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id), {})
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if manga and auth.is_manga_blocked(username, load_manga_dims(library_id, manga["name"]).get("tags", [])):
        return RedirectResponse("/", status_code=302)
    manga_type = manga.get("manga_type", "loose") if manga else "loose"
    if manga_type == "case2":
        template = "volume_detail.html"
    elif manga_type == "loose":
        dims = load_manga_dims(library_id, manga["name"]) if manga else {}
        template = "volume_detail.html" if dims.get("volumes") else "manga_detail.html"
    else:
        template = "manga_detail.html"
    return templates.TemplateResponse(request, template, {
        "library_id": library_id,
        "manga_id": manga_id,
        "theme_css": get_theme_css(username),
    })

@app.get("/manga/{library_id}/category/{category}")
def category_list_page(request: Request, library_id: int, category: str):
    username = auth.get_current_user(request)
    if not username:
        return RedirectResponse("/login", status_code=302)
    if auth.must_change_password(username):
        return RedirectResponse("/settings", status_code=302)
    if not auth.can_access_library(username, library_id):
        return RedirectResponse("/", status_code=302)
    if category not in ("favourites", "last-read", "random"):
        return RedirectResponse("/", status_code=302)
    titles = {
        "favourites": "Favourites",
        "last-read":  "Last Read",
        "random":     "Random",
    }
    return templates.TemplateResponse(request, "category_list.html", {
        "library_id":    library_id,
        "category":      category,
        "category_title": titles[category],
        "theme_css":     get_theme_css(username),
    })

@app.get("/manga/{library_id}/{manga_id}/chapter/{chapter_id}")
def chapter_reader(request: Request, library_id: int, manga_id: str, chapter_id: str):
    username = auth.get_current_user(request)
    if not username:
        return RedirectResponse("/login", status_code=302)
    if auth.must_change_password(username):
        return RedirectResponse("/settings", status_code=302)
    if not auth.can_access_library(username, library_id):
        return RedirectResponse("/", status_code=302)
    if _is_manga_id_blocked(username, library_id, manga_id):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "chapter_reader.html", {
        "library_id": library_id,
        "manga_id": manga_id,
        "chapter_id": chapter_id,
        "theme_css": get_theme_css(username),
    })

@app.get("/api/manga/{library_id}/{manga_id}/chapters")
def get_chapters(request: Request, library_id: int, manga_id: str):
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
    if auth.is_manga_blocked(username, dims.get("tags", [])):
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    chapters = [
        {"id": cid, "name": ch["name"], "path": ch["path"]}
        for cid, ch in dims.get("chapters", {}).items()
    ]
    chapters.sort(key=lambda c: natural_sort_key(c["name"]))
    return JSONResponse({"chapters": chapters})


@app.get("/api/manga/{library_id}/{manga_id}/dims")
def get_manga_dims(request: Request, library_id: int, manga_id: str):
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
    if auth.is_manga_blocked(username, dims.get("tags", [])):
        return JSONResponse({"error": "Manga not found"}, status_code=404)

    # A flat max-age meant every reader could be looking at a rescanned
    # manga's stale page list/count for up to that whole window (or longer,
    # in practice, since browsers can hold onto a cached response well past
    # max-age without the tab ever having been reloaded) - the exact
    # "deleted a page, rescanned, still shows broken until a hard refresh"
    # symptom. Switched to a validator instead of a fixed window: an ETag
    # derived from the manga's own last-rescan timestamp (only ever changes
    # when scan_library actually rebuilt this manga's data - a plain
    # integrity Recheck alone never touches it, since Recheck was never
    # meant to change the page list either). "no-cache" (not "no-store")
    # still lets the browser cache the body, but forces it to send a
    # conditional request every time; a match is a bodyless 304, so a
    # reader gets exactly-fresh data on its very next normal load - no
    # arbitrary staleness window, and no manual hard-refresh needed.
    etag = f'"{manga.get("last_updated", "")}"'
    headers = {"Cache-Control": "private, no-cache", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(dims, headers=headers)

# ── VOLUME ROUTES (Case 2) ──

@app.get("/api/manga/{library_id}/{manga_id}/volumes")
def get_volumes(request: Request, library_id: int, manga_id: str):
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
    if auth.is_manga_blocked(username, dims.get("tags", [])):
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    volumes = [
        {"id": vid, "name": v["name"], "path": v["path"], "cover_image": v.get("cover_image"), "total_pages": len(v.get("pages", []))}
        for vid, v in dims.get("volumes", {}).items()
    ]
    volumes.sort(key=lambda v: natural_sort_key(v["name"]))
    return JSONResponse({"volumes": volumes})

@app.get("/manga/{library_id}/{manga_id}/volume/{volume_id}")
def volume_reader(request: Request, library_id: int, manga_id: str, volume_id: str):
    username = auth.get_current_user(request)
    if not username:
        return RedirectResponse("/login", status_code=302)
    if auth.must_change_password(username):
        return RedirectResponse("/settings", status_code=302)
    if not auth.can_access_library(username, library_id):
        return RedirectResponse("/", status_code=302)
    if _is_manga_id_blocked(username, library_id, manga_id):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "chapter_reader.html", {
        "library_id": library_id,
        "manga_id":   manga_id,
        "volume_id":  volume_id,
        "theme_css":  get_theme_css(username),
    })

@app.get("/api/manga/{library_id}/{manga_id}/volume/{volume_id}/pages")
def get_volume_pages(request: Request, library_id: int, manga_id: str, volume_id: str):
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
    if auth.is_manga_blocked(username, dims.get("tags", [])):
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    volume = dims.get("volumes", {}).get(volume_id)
    if not volume:
        return JSONResponse({"error": "Volume not found"}, status_code=404)

    vol_type = volume.get("source", "archive")
    base_url = f"/api/manga/{library_id}/{manga_id}/volume/{volume_id}/page"

    if vol_type == "archive":
        arc = open_archive(volume["path"])
        if arc is None:
            return JSONResponse({"error": "Cannot open archive"}, status_code=500)
        with arc:
            images = list_archive_images(arc)
        pages = [f"{base_url}/{i}" for i in range(len(images))]
    elif vol_type == "pdf":
        if not PDF_SUPPORT:
            return JSONResponse({"error": "PDF support not installed"}, status_code=500)
        try:
            doc = pymupdf.open(volume["path"])
            page_count = len(doc)
            doc.close()
        except Exception as e:
            print(f"[PDF] Failed to open {volume['path']}: {e}")
            return JSONResponse({"error": "Cannot open PDF"}, status_code=500)
        pages = [f"{base_url}/{i}" for i in range(page_count)]
    elif vol_type == "epub":
        image_list = get_epub_image_list(volume["path"])
        pages = [f"{base_url}/{i}" for i in range(len(image_list))]
    elif vol_type == "loose":
        try:
            files = sorted(
                [f for f in os.listdir(volume["path"])
                 if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS],
                key=natural_sort_key
            )
        except Exception:
            files = []
        pages = [f"{base_url}/{i}" for i in range(len(files))]
    else:
        pages = []

    return JSONResponse({"pages": pages, "count": len(pages)})

@app.get("/api/manga/{library_id}/{manga_id}/volume/{volume_id}/page/{page_index:int}")
def get_volume_page(request: Request, library_id: int, manga_id: str, volume_id: str, page_index: int, scale: float = 1.5):
    username = auth.get_opds_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
    if auth.is_manga_blocked(username, dims.get("tags", [])):
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    volume = dims.get("volumes", {}).get(volume_id)
    if not volume:
        return JSONResponse({"error": "Volume not found"}, status_code=404)

    vol_type = volume.get("source", "archive")

    if vol_type == "archive":
        images = _cached_archive_image_list(volume["path"])
        if page_index >= len(images):
            return JSONResponse({"error": "Page not found"}, status_code=404)
        img_bytes = _cached_archive_page(volume["path"], images[page_index])
        if img_bytes is None:
            return JSONResponse({"error": "Failed to read page"}, status_code=500)
        ext = os.path.splitext(images[page_index])[1].lower().lstrip('.')
        media_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                      "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")
        return StreamingResponse(
            io.BytesIO(img_bytes), media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )

    elif vol_type == "pdf":
        # Clamp scale to valid presets
        scale = min(max(scale, 1.0), 2.0)
        # Check in-memory cache first (covers repeated requests and back-navigation)
        cache_key = (volume["path"], page_index, scale)
        if cache_key in _pdf_page_cache:
            return Response(
                content=_pdf_page_cache[cache_key],
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=86400, immutable"},
            )
        thumbs_dir = get_thumbs_dir(library_id, manga["name"], volume_id)
        # Serve pre-rendered disk file at default scale, and warm the in-memory cache
        if thumbs_dir and abs(scale - 1.5) < 0.1:
            disk_path = os.path.join(thumbs_dir, f"p{page_index}.jpg")
            if os.path.exists(disk_path):
                with open(disk_path, "rb") as f:
                    img_bytes = f.read()
                if len(_pdf_page_cache) >= 128:
                    _pdf_page_cache.pop(next(iter(_pdf_page_cache)))
                _pdf_page_cache[cache_key] = img_bytes
                return Response(
                    content=img_bytes,
                    media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400, immutable"},
                )
        # Render on demand at requested scale
        img_bytes = _cached_pdf_page(volume["path"], page_index, scale=scale)
        if img_bytes is None:
            return JSONResponse({"error": "Failed to render PDF page"}, status_code=500)
        return Response(
            content=img_bytes,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )

    elif vol_type == "epub":
        image_list = get_epub_image_list(volume["path"])
        if page_index >= len(image_list):
            return JSONResponse({"error": "Page not found"}, status_code=404)
        img_bytes = _cached_epub_page(volume["path"], image_list[page_index])
        if img_bytes is None:
            return JSONResponse({"error": "Failed to read epub page"}, status_code=500)
        ext = os.path.splitext(image_list[page_index])[1].lower().lstrip('.')
        media_type = f"image/{ext}" if ext != 'jpg' else "image/jpeg"
        return StreamingResponse(
            io.BytesIO(img_bytes), media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )

    elif vol_type == "loose":
        try:
            files = sorted(
                [f for f in os.listdir(volume["path"])
                 if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS],
                key=natural_sort_key
            )
        except Exception:
            return JSONResponse({"error": "Cannot list volume folder"}, status_code=500)
        if page_index >= len(files):
            return JSONResponse({"error": "Page not found"}, status_code=404)
        file_path = os.path.join(volume["path"], files[page_index])
        ext = os.path.splitext(files[page_index])[1].lower().lstrip('.')
        media_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                      "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")
        try:
            with open(file_path, "rb") as f:
                img_bytes = f.read()
        except Exception:
            return JSONResponse({"error": "Failed to read page"}, status_code=500)
        return StreamingResponse(
            io.BytesIO(img_bytes), media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )

    return JSONResponse({"error": "Unknown volume type"}, status_code=500)

# ── ARCHIVE CHAPTER PAGE ROUTE (Case 1 & Case 3) ──

@app.get("/api/manga/{library_id}/{manga_id}/chapter/{chapter_id}/pages")
def get_chapter_pages(request: Request, library_id: int, manga_id: str, chapter_id: str):
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
    if auth.is_manga_blocked(username, dims.get("tags", [])):
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    chapter = dims.get("chapters", {}).get(chapter_id)
    if not chapter:
        return JSONResponse({"error": "Chapter not found"}, status_code=404)

    source = chapter.get("source")
    base_url = f"/api/manga/{library_id}/{manga_id}/chapter/{chapter_id}/page"

    if source == "archive":
        all_images = _cached_archive_image_list(chapter["path"])
        prefix = chapter.get("prefix", "")
        if prefix:
            images = [n for n in all_images if n.startswith(prefix)]
        else:
            images = all_images
        pages = [f"{base_url}/{i}" for i in range(len(images))]
        return JSONResponse({"pages": pages, "count": len(pages)})
    else:
        files = sorted(
            [f for f in os.listdir(chapter["path"])
             if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS],
            key=natural_sort_key
        )
        pages = [f"{base_url}/{fname}" for fname in files]
        return JSONResponse({"pages": pages, "count": len(pages)})

@app.get("/api/manga/{library_id}/{manga_id}/chapter/{chapter_id}/page/{filename_or_index}")
def get_chapter_page(request: Request, library_id: int, manga_id: str, chapter_id: str, filename_or_index: str):
    from fastapi.responses import FileResponse
    username = auth.get_opds_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
    if auth.is_manga_blocked(username, dims.get("tags", [])):
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    chapter = dims.get("chapters", {}).get(chapter_id)
    if not chapter:
        return JSONResponse({"error": "Chapter not found"}, status_code=404)

    source = chapter.get("source")

    if source == "archive":
        try:
            page_index = int(filename_or_index)
        except ValueError:
            return JSONResponse({"error": "Invalid page index"}, status_code=400)
        all_images = _cached_archive_image_list(chapter["path"])
        prefix = chapter.get("prefix", "")
        if prefix:
            images = [n for n in all_images if n.startswith(prefix)]
        else:
            images = all_images
        if page_index >= len(images):
            return JSONResponse({"error": "Page not found"}, status_code=404)
        img_bytes = _cached_archive_page(chapter["path"], images[page_index])
        if img_bytes is None:
            return JSONResponse({"error": "Failed to read page"}, status_code=500)
        ext = os.path.splitext(images[page_index])[1].lower().lstrip('.')
        media_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                      "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")
        return StreamingResponse(
            io.BytesIO(img_bytes), media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )
    elif filename_or_index.isdigit():
        # Numeric index into the sorted file list — same convention as
        # get_volume_page's loose branch. Real filenames always carry an
        # image extension (see IMAGE_EXTENSIONS below), so a bare digit
        # string can never collide with one; this only exists so callers
        # that want a plain incrementing page number (OPDS-PSE) don't have
        # to know the actual filenames on disk.
        try:
            files = sorted(
                [f for f in os.listdir(chapter["path"])
                 if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS],
                key=natural_sort_key
            )
        except Exception:
            return JSONResponse({"error": "Cannot list chapter folder"}, status_code=500)
        page_index = int(filename_or_index)
        if page_index >= len(files):
            return JSONResponse({"error": "Page not found"}, status_code=404)
        return FileResponse(os.path.join(chapter["path"], files[page_index]))
    else:
        file_path = os.path.join(chapter["path"], filename_or_index)
        if not os.path.exists(file_path):
            return JSONResponse({"error": "File not found"}, status_code=404)
        return FileResponse(file_path)

# ── THUMBNAIL ROUTES (on-demand, in-memory) ──

@app.get("/api/manga/{library_id}/{manga_id}/thumb/{source_id}/{page_index:int}")
def get_thumb_on_demand(request: Request, library_id: int, manga_id: str, source_id: str, page_index: int):
    """
    Extract and return a single thumbnail on demand, in memory, never written to disk.
    Reuses the existing page caches (_cached_archive_page, etc.).
    """
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)

    dims = load_manga_dims(library_id, manga["name"])
    if auth.is_manga_blocked(username, dims.get("tags", [])):
        return JSONResponse({"error": "Manga not found"}, status_code=404)

    # Resolve source: try chapters first, then volumes
    source = dims.get("chapters", {}).get(source_id)
    is_volume = source is None
    if is_volume:
        source = dims.get("volumes", {}).get(source_id)
    if not source:
        return JSONResponse({"error": "Source not found"}, status_code=404)

    source_type = source.get("source") if not is_volume else source.get("source", "archive")
    source_path = source.get("path", "")

    # ── Disk cache check for compressed sources ──
    if source_type in ("archive", "pdf"):
        from fastapi.responses import FileResponse as _FR
        thumbs_dir = get_thumbs_dir(library_id, manga["name"], source_id)
        if thumbs_dir:
            thumb_name = f"{page_index}.jpg" if source_type == "archive" else f"thumb_{page_index}.jpg"
            thumb_path = os.path.join(thumbs_dir, thumb_name)
            if os.path.exists(thumb_path):
                return _FR(
                    thumb_path,
                    media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400, immutable"},
                )

    raw = None
    try:
        if source_type == "archive":
            prefix = source.get("prefix", "") if not is_volume else ""
            all_images = _cached_archive_image_list(source_path)
            if prefix:
                images = [n for n in all_images if n.startswith(prefix)]
            else:
                images = all_images
            if page_index >= len(images):
                return JSONResponse({"error": "Page not found"}, status_code=404)
            raw = _cached_archive_page(source_path, images[page_index])
        elif source_type == "pdf":
            # Pre-rendered thumb not on disk yet — render at minimal scale on demand
            try:
                doc  = pymupdf.open(source_path)
                page = doc.load_page(page_index)
                pix  = page.get_pixmap(matrix=pymupdf.Matrix(0.2, 0.2), alpha=False)
                img  = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                tw   = THUMB_WIDTH
                th   = int(pix.height * tw / pix.width) if pix.width else tw
                img  = img.resize((tw, th), Image.LANCZOS)
                buf  = io.BytesIO()
                img.save(buf, format="JPEG", quality=70)
                raw  = buf.getvalue()
                doc.close()
            except Exception as e:
                print(f"[Thumbs] PDF on-demand thumb failed page {page_index}: {e}")
        elif source_type == "epub":
            image_list = get_epub_image_list(source_path)
            if page_index >= len(image_list):
                return JSONResponse({"error": "Page not found"}, status_code=404)
            raw = _cached_epub_page(source_path, image_list[page_index])
        else:
            # Loose chapter: read directly from disk
            pages = source.get("pages", [])
            if page_index >= len(pages):
                return JSONResponse({"error": "Page not found"}, status_code=404)
            chapter_path = source_path
            try:
                files = sorted(
                    [f for f in os.listdir(chapter_path)
                     if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS],
                    key=natural_sort_key
                )
                if page_index >= len(files):
                    return JSONResponse({"error": "Page not found"}, status_code=404)
                with open(os.path.join(chapter_path, files[page_index]), "rb") as f:
                    raw = f.read()
            except Exception as e:
                print(f"[Thumbs] Error reading loose page {page_index}: {e}")
    except Exception as e:
        print(f"[Thumbs] Error extracting page {page_index} for {source_id}: {e}")

    if not raw:
        return JSONResponse({"error": "Failed to read page"}, status_code=500)

    thumb_bytes = _make_thumb_bytes(raw)
    if not thumb_bytes:
        return JSONResponse({"error": "Failed to make thumbnail"}, status_code=500)

    return Response(
        content=thumb_bytes,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )

@app.get("/api/manga/{library_id}/{manga_id}/thumb-full/{source_id}/{page_index:int}")
def get_thumb_full_on_demand(request: Request, library_id: int, manga_id: str, source_id: str, page_index: int):
    """
    Return the full-resolution page image for a given source/page.
    Used by the thumb strip to pre-load the actual image behind each visible thumbnail.
    Delegates to the existing page endpoints' logic via the shared caches.
    """
    username = auth.get_current_user(request)
    if not auth.can_access_library(username, library_id):
        return JSONResponse({"error": "Library not found"}, status_code=404)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)

    dims = load_manga_dims(library_id, manga["name"])
    if auth.is_manga_blocked(username, dims.get("tags", [])):
        return JSONResponse({"error": "Manga not found"}, status_code=404)

    source = dims.get("chapters", {}).get(source_id)
    is_volume = source is None
    if is_volume:
        source = dims.get("volumes", {}).get(source_id)
    if not source:
        return JSONResponse({"error": "Source not found"}, status_code=404)

    source_type = source.get("source") if not is_volume else source.get("source", "archive")
    source_path = source.get("path", "")

    raw = None
    media_type = "image/jpeg"
    try:
        if source_type == "archive":
            prefix = source.get("prefix", "") if not is_volume else ""
            all_images = _cached_archive_image_list(source_path)
            images = [n for n in all_images if n.startswith(prefix)] if prefix else all_images
            if page_index >= len(images):
                return JSONResponse({"error": "Page not found"}, status_code=404)
            raw = _cached_archive_page(source_path, images[page_index])
            ext = os.path.splitext(images[page_index])[1].lower().lstrip(".")
            media_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                          "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")
        elif source_type == "pdf":
            raw = _cached_pdf_page(source_path, page_index)
            media_type = "image/png"
        elif source_type == "epub":
            image_list = get_epub_image_list(source_path)
            if page_index >= len(image_list):
                return JSONResponse({"error": "Page not found"}, status_code=404)
            raw = _cached_epub_page(source_path, image_list[page_index])
            ext = os.path.splitext(image_list[page_index])[1].lower().lstrip(".")
            media_type = f"image/{ext}" if ext != "jpg" else "image/jpeg"
        else:
            chapter_path = source_path
            files = sorted(
                [f for f in os.listdir(chapter_path)
                 if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS],
                key=natural_sort_key
            )
            if page_index >= len(files):
                return JSONResponse({"error": "Page not found"}, status_code=404)
            with open(os.path.join(chapter_path, files[page_index]), "rb") as f:
                raw = f.read()
            ext = os.path.splitext(files[page_index])[1].lower().lstrip(".")
            media_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                          "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")
    except Exception as e:
        print(f"[ThumbFull] Error reading page {page_index} for {source_id}: {e}")

    if not raw:
        return JSONResponse({"error": "Failed to read page"}, status_code=500)

    return StreamingResponse(
        io.BytesIO(raw),
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )

@app.get("/login")
def login_page(request: Request):
    # If already logged in, skip the login screen
    if auth.get_current_user(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html")
 
@app.post("/api/auth/login")
async def api_login(request: Request):
    return await auth.route_login(request)

@app.get("/auth/claim")
async def auth_claim(request: Request):
    return await auth.route_claim_session(request)

@app.post("/api/auth/logout")
async def api_logout(request: Request):
    return await auth.route_logout(request)

@app.post("/api/auth/logout-everywhere")
async def api_logout_everywhere(request: Request):
    return await auth.route_logout_everywhere(request)

@app.get("/api/auth/me")
def api_me(request: Request):
    return auth.route_me(request)

@app.post("/api/auth/change-password")
async def api_change_password(request: Request):
    return await auth.route_change_password(request)

@app.get("/api/admin/permissions")
def api_get_permissions(request: Request):
    return auth.route_get_permissions(request)

@app.post("/api/admin/permissions/{username}")
async def api_set_user_permissions(request: Request, username: str):
    return await auth.route_set_user_permissions(request, username)

@app.post("/api/admin/users")
async def api_create_user(request: Request):
    return await auth.route_admin_create_user(request)

@app.delete("/api/admin/users/{username}")
async def api_delete_user(request: Request, username: str):
    return await auth.route_admin_delete_user(request, username)

@app.post("/api/admin/users/{username}/role")
async def api_set_user_role(request: Request, username: str):
    return await auth.route_admin_set_role(request, username)

# ── READING SESSION ROUTES ────────────────────────────────────────────────────

@app.post("/api/reading/session/start")
async def reading_session_start(request: Request):
    username = auth.get_current_user(request)
    if not username:
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)
    body       = await request.json()
    manga_name = body.get("manga_name", "Unknown")
    library_id = str(body.get("library_id", ""))
    auth.append_reading_session(username, manga_name, library_id)
    return JSONResponse({"ok": True})


@app.post("/api/reading/session/tick")
async def reading_session_tick(request: Request):
    username = auth.get_current_user(request)
    if not username:
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)
    auth.tick_reading_session(username)
    return JSONResponse({"ok": True})


@app.get("/api/reading/stats")
def reading_stats_route(request: Request, manga_name: str = None, user: str = None):
    requester = auth.get_current_user(request)
    if not requester:
        return JSONResponse({"ok": False, "error": "Not authenticated"}, status_code=401)
    target = requester
    if user and user != requester:
        if not auth.resolve_permissions(requester).get("is_admin"):
            return JSONResponse({"ok": False, "error": "Admin access required"}, status_code=403)
        target = user
    data     = auth.load_user_data(target)
    sessions = data.get("reading_sessions", [])
    if manga_name:
        total = sum(s.get("minutes", 0) for s in sessions if s.get("manga_name") == manga_name)
        return JSONResponse({"ok": True, "total_minutes": total})
    manga_totals: dict = {}
    for s in sessions:
        name = s.get("manga_name", "Unknown")
        manga_totals[name] = manga_totals.get(name, 0) + s.get("minutes", 0)
    return JSONResponse({
        "ok":            True,
        "sessions":      sessions,
        "manga_totals":  manga_totals,
        "total_minutes": sum(s.get("minutes", 0) for s in sessions),
    })


@app.get("/api/reading/stats/all-users")
def reading_stats_all_users(request: Request):
    err = auth.require_admin(request)
    if err:
        return err
    users_data = auth._load_users()
    app_data   = load_app_data()
    libraries  = {str(lib["id"]): lib["name"] for lib in app_data.get("libraries", [])}
    result     = []
    for user in users_data["users"]:
        uname    = user["username"]
        udata    = auth.load_user_data(uname)
        sessions = udata.get("reading_sessions", [])
        by_library: dict = {}
        for s in sessions:
            lib_key  = str(s.get("library_id", ""))
            lib_name = libraries.get(lib_key, f"Library {lib_key}")
            manga    = s.get("manga_name", "Unknown")
            mins     = s.get("minutes", 0)
            by_library.setdefault(lib_name, {})
            by_library[lib_name][manga] = by_library[lib_name].get(manga, 0) + mins
        result.append({
            "username":      uname,
            "total_minutes": sum(s.get("minutes", 0) for s in sessions),
            "by_library":    by_library,
        })
    return JSONResponse({"ok": True, "users": result})

# ── OPDS + OPDS-PSE CATALOG ──────────────────────────────────────────────────
# A standard OPDS 1.2 catalog, with the Page Streaming Extension for comic
# clients (Chunky, and Mihon's generic OPDS source) that can page through a
# chapter/volume live instead of downloading a whole archive first. Not
# behind the /api/* auth middleware (same reasoning as /covers) since every
# real OPDS client only speaks HTTP Basic Auth, not the session cookie /
# X-Auth-Token the rest of the app uses — auth.get_opds_user() accepts either.

def _require_opds_user(request: Request):
    """Returns (username, None) or (None, 401 response with a WWW-Authenticate
    header so clients/browsers know to prompt for Basic Auth credentials)."""
    username = auth.get_opds_user(request)
    if username is None:
        return None, Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="KINSHO"'})
    return username, None

def _opds_cover_url(library_id: int, manga: dict, user_covers: dict) -> Optional[str]:
    cover = user_covers.get(manga["id"]) or manga.get("cover")
    if not cover:
        return None
    return f"/covers/{library_id}/{quote(manga['name'])}/{os.path.basename(cover)}"

def _opds_chapter_info(library_id: int, manga_id: str, chapter: dict) -> tuple:
    """Return (page_count, first_page_url) for a case1/case3 chapter."""
    base_url = f"/api/manga/{library_id}/{manga_id}/chapter/{chapter.get('id', '')}/page"
    if chapter.get("source") == "archive":
        all_images = _cached_archive_image_list(chapter["path"])
        prefix = chapter.get("prefix", "")
        images = [n for n in all_images if n.startswith(prefix)] if prefix else all_images
        count = len(images)
    else:
        try:
            count = len([f for f in os.listdir(chapter["path"]) if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS])
        except Exception:
            count = 0
    return count, f"{base_url}/0"

def _opds_volume_info(library_id: int, manga_id: str, volume_id: str, volume: dict) -> tuple:
    """Return (page_count, first_page_url) for a case2/loose volume."""
    base_url = f"/api/manga/{library_id}/{manga_id}/volume/{volume_id}/page"
    vol_type = volume.get("source", "archive")
    if vol_type == "archive":
        count = len(_cached_archive_image_list(volume["path"]))
    elif vol_type == "pdf":
        count = 0
        if PDF_SUPPORT:
            try:
                doc = pymupdf.open(volume["path"])
                count = len(doc)
                doc.close()
            except Exception:
                count = 0
    elif vol_type == "epub":
        count = len(get_epub_image_list(volume["path"]))
    elif vol_type == "loose":
        try:
            count = len([f for f in os.listdir(volume["path"]) if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS])
        except Exception:
            count = 0
    else:
        count = 0
    return count, f"{base_url}/0"

@app.get("/opds/")
def opds_root(request: Request):
    username, err = _require_opds_user(request)
    if err:
        return err
    data = load_app_data()
    libraries = data.get("libraries", [])
    perms = auth.resolve_permissions(username)
    if not perms.get("is_admin"):
        lib_perms = perms.get("libraries", {})
        libraries = [l for l in libraries if lib_perms.get(str(l["id"]), True) is not False]
    return opds.build_root_feed(libraries)

@app.get("/opds/search-description.xml")
def opds_search_description(request: Request):
    username, err = _require_opds_user(request)
    if err:
        return err
    return opds.build_search_description()

@app.get("/opds/library/{library_id}")
def opds_library(request: Request, library_id: int, page: int = Query(default=1, ge=1)):
    username, err = _require_opds_user(request)
    if err:
        return err
    if not auth.can_access_library(username, library_id):
        return Response(status_code=404)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id), {})
    perms = auth.resolve_permissions(username)
    blocked_tags = perms.get("blocked_tags", []) if not perms.get("is_admin") else []
    user_data = auth.load_user_data(username)
    user_covers = user_data.get("covers", {}).get(str(library_id), {})

    visible = []
    for m in manga_data.get("mangas", []):
        dims = load_manga_dims(library_id, m["name"])
        if blocked_tags and any(t in blocked_tags for t in dims.get("tags", [])):
            continue
        visible.append({"id": m["id"], "name": m["name"], "cover_url": _opds_cover_url(library_id, m, user_covers)})
    visible.sort(key=lambda m: natural_sort_key(m["name"]))

    per_page = 50
    total_pages = max(1, (len(visible) + per_page - 1) // per_page)
    page = min(page, total_pages)
    page_items = visible[(page - 1) * per_page: page * per_page]
    library_name = next((l["name"] for l in data.get("libraries", []) if l["id"] == library_id), str(library_id))
    return opds.build_library_feed(library_id, library_name, page_items, page, total_pages)

@app.get("/opds/search")
def opds_search(request: Request, q: str = Query(default="")):
    username, err = _require_opds_user(request)
    if err:
        return err
    query = q.strip()
    data = load_app_data()
    perms = auth.resolve_permissions(username)
    blocked_tags = perms.get("blocked_tags", []) if not perms.get("is_admin") else []
    results = []
    for lib in data.get("libraries", []):
        library_id = lib["id"]
        if not auth.can_access_library(username, library_id):
            continue
        manga_data = data.get("manga_data", {}).get(str(library_id), {})
        user_data = auth.load_user_data(username)
        user_covers = user_data.get("covers", {}).get(str(library_id), {})
        for m in manga_data.get("mangas", []):
            if query and query.lower() not in m["name"].lower():
                continue
            dims = load_manga_dims(library_id, m["name"])
            if blocked_tags and any(t in blocked_tags for t in dims.get("tags", [])):
                continue
            results.append({
                "library_id": library_id, "id": m["id"], "name": m["name"],
                "cover_url": _opds_cover_url(library_id, m, user_covers),
            })
    return opds.build_search_feed(query, results)

@app.get("/opds/manga/{library_id}/{manga_id}")
def opds_manga(request: Request, library_id: int, manga_id: str):
    username, err = _require_opds_user(request)
    if err:
        return err
    if not auth.can_access_library(username, library_id):
        return Response(status_code=404)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return Response(status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return Response(status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
    if auth.is_manga_blocked(username, dims.get("tags", [])):
        return Response(status_code=404)

    user_data = auth.load_user_data(username)
    user_covers = user_data.get("covers", {}).get(str(library_id), {})
    cover_url = _opds_cover_url(library_id, manga, user_covers)
    item_history = user_data.get("reading_history", {}).get(str(library_id), {}).get(manga_id, {}).get("chapters", {})

    items = []
    if dims.get("volumes"):
        for vid, v in dims["volumes"].items():
            count, first_url = _opds_volume_info(library_id, manga_id, vid, v)
            items.append({
                "id": vid, "name": v["name"], "cover_url": cover_url,
                "page_count": count, "first_page_url": first_url,
                "last_read_page": item_history.get(vid, {}).get("last_page", 0),
            })
    else:
        for cid, c in dims.get("chapters", {}).items():
            count, first_url = _opds_chapter_info(library_id, manga_id, {**c, "id": cid})
            items.append({
                "id": cid, "name": c["name"], "cover_url": cover_url,
                "page_count": count, "first_page_url": first_url,
                "last_read_page": item_history.get(cid, {}).get("last_page", 0),
            })
    items.sort(key=lambda i: natural_sort_key(i["name"]))

    return opds.build_manga_feed(library_id, manga_id, manga["name"], dims.get("description", ""), items)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)