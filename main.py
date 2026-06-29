from fastapi import FastAPI, Request, BackgroundTasks, Query
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse
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
from PIL import Image
from typing import List, Optional
from datetime import datetime
import asyncio
import threading
from fastapi.responses import Response
from urllib.parse import quote
import auth
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
    mount_covers()
    bg_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backgrounds")
    if os.path.exists(bg_folder):
        app.mount("/backgrounds", StaticFiles(directory=bg_folder), name="backgrounds")
    ip = get_local_ip()
    print(f"\n  Kinsho running at: http://{ip}:8000\n")
    task = asyncio.create_task(periodic_library_rescan())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

# Covers are mounted dynamically after data_path is known — see mount_covers()
covers_mounted = False
def mount_covers():
    global covers_mounted
    if covers_mounted:
        return
    covers_dir = get_covers_dir()
    if covers_dir:
        app.mount("/covers", StaticFiles(directory=covers_dir), name="covers")
        covers_mounted = True
templates = Jinja2Templates(directory="templates")

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
        small = img.copy()
        small.thumbnail((300, 450))
        small.save(small_dest, optimize=True, quality=85)
        large = img.copy()
        large.thumbnail((600, 900))
        large.save(large_dest, optimize=True, quality=85)
        new_mtimes[filename] = source_mtime
        print(f"[Covers] Processed {filename} -> small + large")
    except Exception as e:
        print(f"[Covers] Failed processing {filename}: {e}")

    return filename, new_mtimes

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

def scan_library(library: dict) -> list:
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

        if stored_mtime is not None and current_mtime == stored_mtime:
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

        # Only rescan content subfolders if the folder mtime changed
        if mangas[manga_path].get("folder_mtime") == existing.get("folder_mtime"):
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

                    if stored_folder_mtime is not None and current_folder_mtime == stored_folder_mtime:
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

                    if stored_folder_mtime is not None and current_folder_mtime == stored_folder_mtime:
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

    result = list(mangas.values())
    result.sort(key=lambda m: natural_sort_key(m["name"]))
    print(f"[ScanLib] Total mangas found: {len(result)}")
    return result

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

    mount_covers()
    auto_organize_library_root(lib)
    mangas = scan_library(lib)
    print(f"[Scan] Found {len(mangas)} mangas.")

    data = load_app_data()
    if "manga_data" not in data:
        data["manga_data"] = {}
    data["manga_data"][str(library_id)] = {
        "mangas": mangas,
        "last_scanned": datetime.now().isoformat(),
    }
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
        user = username or "admin"
        user_data = auth.load_user_data(user)
        active_name = user_data.get("active_theme", "Midnight Red")
        theme = next((t for t in BUILTIN_THEMES if t["name"] == active_name), BUILTIN_THEMES[0])
        visual_theme = user_data.get("active_visual_theme", "default")
        if visual_theme == "custom":
            cname = user_data.get("active_custom_theme_name", "")
            custom_css = user_data.get("custom_themes", {}).get(cname, "")
    except Exception:
        theme = BUILTIN_THEMES[0]
        visual_theme = "default"
    dt_val = visual_theme if visual_theme in ("default", "sharp", "custom") else "default"
    dt_script = f'<script>document.documentElement.setAttribute("data-theme","{dt_val}");</script>'
    custom_block = f'<style id="custom-theme-style">{custom_css}</style>' if custom_css else ""
    return (
        f'<link rel="stylesheet" href="/static/style.css">'
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
    username = auth.get_current_user(request) or "admin"
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
    })

@app.get("/search")
def search_page(request: Request):
    return templates.TemplateResponse(request, "search_page.html")

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
    username = auth.get_current_user(request) or "admin"
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
    username  = auth.get_current_user(request) or "admin"
    body      = await request.json()
    user_data = auth.load_user_data(username)
    vt = body.get("active_visual_theme", "default")
    if vt not in ("default", "sharp", "custom"):
        vt = "default"
    user_data["active_visual_theme"] = vt
    if vt == "custom" and "active_custom_theme_name" in body:
        user_data["active_custom_theme_name"] = body["active_custom_theme_name"]
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.post("/api/settings/custom-themes")
async def save_custom_theme(request: Request):
    username  = auth.get_current_user(request) or "admin"
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
    username  = auth.get_current_user(request) or "admin"
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
    username  = auth.get_current_user(request) or "admin"
    body      = await request.json()
    user_data = auth.load_user_data(username)
    if "backdrop_list" in body:
        user_data["backdrop_list"]   = bool(body["backdrop_list"])
    if "backdrop_detail" in body:
        user_data["backdrop_detail"] = bool(body["backdrop_detail"])
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
    username = auth.get_current_user(request) or "admin"
    mount_covers()
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
    username = auth.get_current_user(request) or "admin"
    mount_covers()
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
    username = auth.get_current_user(request) or "admin"
    mount_covers()
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
    username = auth.get_current_user(request) or "admin"
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

@app.get("/api/lists")
def get_lists(request: Request):
    username = auth.get_current_user(request) or "admin"
    user_data = auth.load_user_data(username)
    return JSONResponse({"lists": user_data.get("lists", {})})

@app.post("/api/lists")
async def create_list(request: Request):
    username = auth.get_current_user(request) or "admin"
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Empty name"}, status_code=400)
    user_data = auth.load_user_data(username)
    lists = user_data.get("lists", {})
    if name in lists:
        return JSONResponse({"ok": False, "error": "List already exists"}, status_code=409)
    lists[name] = {"manga_ids": [], "manga_names": []}
    user_data["lists"] = lists
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True, "name": name})

@app.put("/api/lists/{list_name}/add")
async def add_to_list(list_name: str, request: Request):
    username = auth.get_current_user(request) or "admin"
    body = await request.json()
    manga_id   = body.get("manga_id", "").strip()
    manga_name = body.get("manga_name", "").strip()
    if not manga_id or not manga_name:
        return JSONResponse({"ok": False, "error": "Missing manga_id or manga_name"}, status_code=400)
    user_data = auth.load_user_data(username)
    lists = user_data.get("lists", {})
    if list_name not in lists:
        return JSONResponse({"ok": False, "error": "List not found"}, status_code=404)
    entry = lists[list_name]
    if manga_id not in entry["manga_ids"]:
        entry["manga_ids"].append(manga_id)
        entry["manga_names"].append(manga_name)
    user_data["lists"] = lists
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.put("/api/lists/{list_name}/remove")
async def remove_from_list(list_name: str, request: Request):
    username = auth.get_current_user(request) or "admin"
    body = await request.json()
    manga_id = body.get("manga_id", "").strip()
    if not manga_id:
        return JSONResponse({"ok": False, "error": "Missing manga_id"}, status_code=400)
    user_data = auth.load_user_data(username)
    lists = user_data.get("lists", {})
    if list_name not in lists:
        return JSONResponse({"ok": False, "error": "List not found"}, status_code=404)
    entry = lists[list_name]
    if manga_id in entry["manga_ids"]:
        idx = entry["manga_ids"].index(manga_id)
        entry["manga_ids"].pop(idx)
        entry["manga_names"].pop(idx)
    user_data["lists"] = lists
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.get("/api/tags")
def get_all_tags(request: Request):
    username = auth.get_current_user(request) or "admin"
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
    username = auth.get_current_user(request) or "admin"
    return JSONResponse(auth.load_user_data(username)) 

@app.get("/api/manga/{library_id}/{manga_id}/covers")
def get_manga_covers(request: Request, library_id: int, manga_id: str):
    username = auth.get_current_user(request) or "admin"
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
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
    username = auth.get_current_user(request) or "admin"
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
    username = auth.get_current_user(request) or "admin"
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
    if tag not in dims["tags"]:
        dims["tags"].append(tag)
    save_manga_dims(library_id, manga["name"], dims)
    data["all_tags"] = rebuild_all_tags(data)
    save_app_data(data)
    return JSONResponse({"ok": True, "tags": dims["tags"], "all_tags": data["all_tags"]})

@app.post("/api/manga/{library_id}/{manga_id}/tags/remove")
async def remove_tags(library_id: int, manga_id: str, request: Request):
    username = auth.get_current_user(request) or "admin"
    if not auth.resolve_permissions(username).get("tags"):
        return JSONResponse({"ok": False, "error": "Permission denied"}, status_code=403)
    body = await request.json()
    tags_to_remove = body.get("tags", [])
    remove_globally = body.get("global", False)
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
    username = auth.get_current_user(request) or "admin"
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
    if genre not in dims["genres"]:
        dims["genres"].append(genre)
    save_manga_dims(library_id, manga["name"], dims)
    data["all_genres"] = rebuild_all_genres(data)
    save_app_data(data)
    return JSONResponse({"ok": True, "genres": dims["genres"], "all_genres": data["all_genres"]})

@app.post("/api/manga/{library_id}/{manga_id}/genres/remove")
async def remove_genres(library_id: int, manga_id: str, request: Request):
    username = auth.get_current_user(request) or "admin"
    if not auth.resolve_permissions(username).get("genres"):
        return JSONResponse({"ok": False, "error": "Permission denied"}, status_code=403)
    body = await request.json()
    genres_to_remove = body.get("genres", [])
    remove_globally = body.get("global", False)
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
    dims["genres"] = [g for g in dims.get("genres", []) if g not in genres_to_remove]
    save_manga_dims(library_id, manga["name"], dims)
    save_app_data(data)
    return JSONResponse({"ok": True, "genres": dims["genres"], "all_genres": data.get("all_genres", [])})

@app.post("/api/manga/{library_id}/{manga_id}/description")
async def save_description(library_id: int, manga_id: str, request: Request):
    username = auth.get_current_user(request) or "admin"
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
    dims["description"] = description
    save_manga_dims(library_id, manga["name"], dims)
    return JSONResponse({"ok": True})

@app.post("/api/settings/last-tab")
async def save_last_tab(request: Request):
    username = auth.get_current_user(request) or "admin"
    body = await request.json()
    user_data = auth.load_user_data(username)
    user_data["last_tab"] = body.get("last_tab")
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.get("/api/manga/{library_id}/{manga_id}/bookmarks")
async def get_bookmarks(request: Request, library_id: int, manga_id: str):
    username  = auth.get_current_user(request) or "admin"
    user_data = auth.load_user_data(username)
    key       = f"{library_id}:{manga_id}"
    bookmarks = user_data.get("bookmarks", {}).get(key, [])
    return JSONResponse({"bookmarks": bookmarks})

@app.post("/api/manga/{library_id}/{manga_id}/bookmarks")
async def save_bookmarks(request: Request, library_id: int, manga_id: str):
    username  = auth.get_current_user(request) or "admin"
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
    username  = auth.get_current_user(request) or "admin"
    body      = await request.json()
    user_data = auth.load_user_data(username)
    user_data["tab_order"] = body.get("tab_order", [])
    auth.save_user_data(username, user_data)
    return JSONResponse({"ok": True})

@app.post("/api/settings/reader")
async def save_reader_settings(request: Request):
    username   = auth.get_current_user(request) or "admin"
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
    username = auth.get_current_user(request) or "admin"
    body = await request.json()
    library_id  = str(body.get("library_id", ""))
    manga_id    = str(body.get("manga_id", ""))
    chapter_id  = body.get("chapter_id")
    page        = body.get("page", 0)
    completed_chapter_id = body.get("completed_chapter_id")

    if not library_id or not manga_id:
        return JSONResponse({"ok": False, "error": "Missing library_id or manga_id"}, status_code=400)

    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(library_id, {})
    manga = next((m for m in manga_data.get("mangas", []) if m["id"] == manga_id), None)
    manga_name = manga["name"] if manga else None
    dims = load_manga_dims(int(library_id), manga["name"]) if manga else {}
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
    username  = auth.get_current_user(request) or "admin"
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

    username = auth.get_current_user(request) or "admin"
    mount_covers()
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
    username  = auth.get_current_user(request) or "admin"
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
    return templates.TemplateResponse(request, "search_page.html", {
        "theme_css": get_theme_css(username),
    })

@app.get("/")
def manga_list(request: Request):
    username = auth.get_current_user(request)
    if not username:
        return RedirectResponse("/login", status_code=302)
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
    return templates.TemplateResponse(request, "manga_list.html", {
        "libraries": libraries,
        "last_tab":  admin.get("last_tab", None),
        "theme_css": get_theme_css(username),
    })

@app.get("/manga/{library_id}/{manga_id}")
def manga_detail(request: Request, library_id: int, manga_id: str):
    username = auth.get_current_user(request)
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id), {})
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
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
    return templates.TemplateResponse(request, "chapter_reader.html", {
        "library_id": library_id,
        "manga_id": manga_id,
        "chapter_id": chapter_id,
        "theme_css": get_theme_css(username),
    })

@app.get("/api/manga/{library_id}/{manga_id}/chapters")
def get_chapters(library_id: int, manga_id: str):
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
    chapters = [
        {"id": cid, "name": ch["name"], "path": ch["path"]}
        for cid, ch in dims.get("chapters", {}).items()
    ]
    chapters.sort(key=lambda c: natural_sort_key(c["name"]))
    return JSONResponse({"chapters": chapters})


@app.get("/api/manga/{library_id}/{manga_id}/dims")
def get_manga_dims(library_id: int, manga_id: str):
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
    return JSONResponse(dims, headers={"Cache-Control": "private, max-age=120"})

# ── VOLUME ROUTES (Case 2) ──

@app.get("/api/manga/{library_id}/{manga_id}/volumes")
def get_volumes(library_id: int, manga_id: str):
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
    volumes = [
        {"id": vid, "name": v["name"], "path": v["path"], "cover_image": v.get("cover_image"), "total_pages": len(v.get("pages", []))}
        for vid, v in dims.get("volumes", {}).items()
    ]
    volumes.sort(key=lambda v: natural_sort_key(v["name"]))
    return JSONResponse({"volumes": volumes})

@app.get("/manga/{library_id}/{manga_id}/volume/{volume_id}")
def volume_reader(request: Request, library_id: int, manga_id: str, volume_id: str):
    return templates.TemplateResponse(request, "chapter_reader.html", {
        "library_id": library_id,
        "manga_id":   manga_id,
        "volume_id":  volume_id,
    })

@app.get("/api/manga/{library_id}/{manga_id}/volume/{volume_id}/pages")
def get_volume_pages(library_id: int, manga_id: str, volume_id: str):
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
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
def get_volume_page(library_id: int, manga_id: str, volume_id: str, page_index: int, scale: float = 1.5):
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
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
def get_chapter_pages(library_id: int, manga_id: str, chapter_id: str):
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
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
def get_chapter_page(library_id: int, manga_id: str, chapter_id: str, filename_or_index: str):
    from fastapi.responses import FileResponse
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)
    dims = load_manga_dims(library_id, manga["name"])
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
    else:
        file_path = os.path.join(chapter["path"], filename_or_index)
        if not os.path.exists(file_path):
            return JSONResponse({"error": "File not found"}, status_code=404)
        return FileResponse(file_path)

# ── THUMBNAIL ROUTES (on-demand, in-memory) ──

@app.get("/api/manga/{library_id}/{manga_id}/thumb/{source_id}/{page_index:int}")
def get_thumb_on_demand(library_id: int, manga_id: str, source_id: str, page_index: int):
    """
    Extract and return a single thumbnail on demand, in memory, never written to disk.
    Reuses the existing page caches (_cached_archive_page, etc.).
    """
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)

    dims = load_manga_dims(library_id, manga["name"])

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
def get_thumb_full_on_demand(library_id: int, manga_id: str, source_id: str, page_index: int):
    """
    Return the full-resolution page image for a given source/page.
    Used by the thumb strip to pre-load the actual image behind each visible thumbnail.
    Delegates to the existing page endpoints' logic via the shared caches.
    """
    data = load_app_data()
    manga_data = data.get("manga_data", {}).get(str(library_id))
    if not manga_data:
        return JSONResponse({"error": "Library not found"}, status_code=404)
    manga = next((m for m in manga_data.get("mangas", []) if m.get("id") == manga_id), None)
    if not manga:
        return JSONResponse({"error": "Manga not found"}, status_code=404)

    dims = load_manga_dims(library_id, manga["name"])

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
 
@app.post("/api/auth/register")
async def api_register(request: Request):
    return await auth.route_register(request)
 
@app.post("/api/auth/logout")
async def api_logout(request: Request):
    return await auth.route_logout(request)
 
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

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)