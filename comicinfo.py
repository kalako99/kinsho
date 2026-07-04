"""
comicinfo.py — read ComicInfo.xml sidecar metadata (the Komga/Kavita/ComicRack
convention) embedded in a CBZ/CBR archive's root, or sitting next to the
images in a loose (unarchived) chapter/volume folder.

Pure parsing: no data access, no dims.json writes. main.py calls
locate_and_read() per chapter/volume and aggregate_for_manga() to combine
across a whole series, then decides what (if anything) to write.
"""

import os
import xml.etree.ElementTree as ET


def parse_comicinfo_xml(raw: bytes) -> dict | None:
    """Return {"description", "genres", "tags"} (any may be empty), or None if unparsable."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None

    def _text(tag):
        el = root.find(tag)
        return el.text.strip() if el is not None and el.text else None

    def _csv(tag):
        raw_val = _text(tag)
        if not raw_val:
            return []
        return [v.strip() for v in raw_val.split(",") if v.strip()]

    return {
        "description": _text("Summary") or "",
        "genres": _csv("Genre"),
        "tags": _csv("Tags"),
    }


def locate_and_read(source_path: str, source_type: str, open_archive_fn, read_entry_fn) -> dict | None:
    """
    source_type: "archive" (CBZ/CBR at source_path) or anything else (loose
    folder at source_path). open_archive_fn/read_entry_fn are main.py's
    open_archive()/read_archive_entry_bytes(), passed in to avoid a circular
    import.
    """
    if source_type == "archive":
        arc = open_archive_fn(source_path)
        if arc is None:
            return None
        try:
            names = arc.namelist()
            match = next((n for n in names if os.path.basename(n).lower() == "comicinfo.xml"), None)
            if not match:
                return None
            raw = read_entry_fn(arc, match)
        finally:
            arc.close()
    else:
        try:
            match = next((f for f in os.listdir(source_path) if f.lower() == "comicinfo.xml"), None)
        except Exception:
            return None
        if not match:
            return None
        try:
            with open(os.path.join(source_path, match), "rb") as f:
                raw = f.read()
        except Exception:
            return None

    if not raw:
        return None
    return parse_comicinfo_xml(raw)


def aggregate_for_manga(ordered_items: list, open_archive_fn, read_entry_fn) -> dict | None:
    """
    ordered_items: list of {"source_path", "source_type"} for a manga's
    chapters/volumes, already sorted (chapter/volume order — first item wins
    the description). Returns {"description", "genres", "tags"} or None if no
    item carried a ComicInfo.xml at all.

    description: the first item (in the given order) that has one.
    genres/tags: the intersection across every item that provided a
    non-empty list — a value repeated on every chapter is a real series-level
    attribute, not a per-chapter one.
    """
    description = None
    genre_sets: list[set] = []
    tag_sets: list[set] = []
    found_any = False

    for item in ordered_items:
        parsed = locate_and_read(item["source_path"], item["source_type"], open_archive_fn, read_entry_fn)
        if parsed is None:
            continue
        found_any = True
        if description is None and parsed["description"]:
            description = parsed["description"]
        if parsed["genres"]:
            genre_sets.append(set(parsed["genres"]))
        if parsed["tags"]:
            tag_sets.append(set(parsed["tags"]))

    if not found_any:
        return None

    genres = sorted(set.intersection(*genre_sets)) if genre_sets else []
    tags = sorted(set.intersection(*tag_sets)) if tag_sets else []
    return {"description": description or "", "genres": genres, "tags": tags}
