"""
integrity.py — scan-time corruption and duplicate-page detection for CBZ/CBR
archives and loose (unarchived) chapter/volume folders.

Pure checking: no data access, no scheduling, no persistence. main.py's
background loop calls check_item() per chapter/volume and decides what to do
with the result (write dims.json, add/clear an integrity_issues.json entry).

Scope: archive + loose only, same as comicinfo.py — PDF/EPUB volumes aren't
checked (a different, much more expensive kind of per-page validation would
be needed there, and corrupt/duplicate PDF pages are a much rarer concern).
"""

import hashlib
import os
from collections import defaultdict

from PIL import Image
import io

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}


def _find_duplicate_groups(hashes: dict) -> list:
    """hashes: {filename: hex_digest}. Returns [[filename, filename, ...], ...] for each hash shared by 2+ files."""
    by_hash = defaultdict(list)
    for name, h in hashes.items():
        by_hash[h].append(name)
    return [sorted(names) for names in by_hash.values() if len(names) > 1]


def check_archive(source_path: str, open_archive_fn, read_entry_fn) -> dict:
    """
    Reads every image entry once, which does double duty: zipfile/rarfile
    validate each entry's CRC as part of decompression, so a failed read IS
    the corruption check — no separate zipfile.testzip() pass needed. The
    same read gives us the bytes to hash for duplicate detection.

    Returns {"corrupt": str|None, "duplicate_groups": [[...], ...]}.
    """
    arc = open_archive_fn(source_path)
    if arc is None:
        return {"corrupt": "Failed to open archive", "duplicate_groups": []}
    try:
        try:
            names = [n for n in arc.namelist() if os.path.splitext(n)[1].lower() in IMAGE_EXTENSIONS]
        except Exception as e:
            return {"corrupt": f"Failed to list archive contents: {e}", "duplicate_groups": []}
        hashes = {}
        for name in names:
            data = read_entry_fn(arc, name)
            if data is None:
                return {"corrupt": f"Failed to read page: {os.path.basename(name)}", "duplicate_groups": []}
            hashes[name] = hashlib.sha1(data).hexdigest()
    finally:
        try:
            arc.close()
        except Exception:
            pass
    return {"corrupt": None, "duplicate_groups": _find_duplicate_groups(hashes)}


def check_loose(source_path: str) -> dict:
    """
    No archive container means no built-in CRC check — the only way to know
    a raw image file is intact is to actually decode it. Returns the same
    shape as check_archive().
    """
    try:
        filenames = sorted(
            f for f in os.listdir(source_path)
            if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS
        )
    except Exception as e:
        return {"corrupt": f"Failed to list folder contents: {e}", "duplicate_groups": []}

    hashes = {}
    for name in filenames:
        full_path = os.path.join(source_path, name)
        try:
            with open(full_path, "rb") as f:
                data = f.read()
            Image.open(io.BytesIO(data)).load()
        except Exception as e:
            return {"corrupt": f"Failed to read/decode page: {name} ({e})", "duplicate_groups": []}
        hashes[name] = hashlib.sha1(data).hexdigest()
    return {"corrupt": None, "duplicate_groups": _find_duplicate_groups(hashes)}


def check_item(item: dict, open_archive_fn, read_entry_fn) -> dict:
    """item: one entry from main.py's checkable_items_for_manga()."""
    if item["source_type"] == "archive":
        return check_archive(item["source_path"], open_archive_fn, read_entry_fn)
    return check_loose(item["source_path"])
