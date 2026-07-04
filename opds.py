"""
opds.py — OPDS 1.2 + OPDS-PSE catalog feeds for KINSHO.

Pure feed-building: every function here takes already-resolved plain data
(dicts/lists main.py assembled after its own auth/permission checks) and
returns an XML Response. No data access, no auth, no filtering happens in
this module — that all stays in main.py, same as how metadata_fetch.py stays
out of auth/permissions and just does the fetching work it's asked to do.
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote
from fastapi.responses import Response

ATOM_NS = "http://www.w3.org/2005/Atom"
PSE_NS = "http://vaemendis.net/opds-pse/ns"

NAV_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
OPENSEARCH_TYPE = "application/opensearchdescription+xml"

ET.register_namespace("", ATOM_NS)
ET.register_namespace("pse", PSE_NS)

IMAGE_MEDIA_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sub(parent, tag, text=None):
    el = ET.SubElement(parent, f"{{{ATOM_NS}}}{tag}")
    if text is not None:
        el.text = text
    return el


def _link(parent, rel, href, type_=None, **attrs):
    el = ET.SubElement(parent, f"{{{ATOM_NS}}}link")
    el.set("rel", rel)
    el.set("href", href)
    if type_:
        el.set("type", type_)
    for k, v in attrs.items():
        if v is not None:
            el.set(k.replace("_", ":", 1) if k.startswith("pse_") else k, str(v))
    return el


def _feed_root(feed_id, title, self_href, self_type=NAV_TYPE):
    root = ET.Element(f"{{{ATOM_NS}}}feed")
    # Declared explicitly as a plain attribute rather than via ElementTree's
    # namespace machinery: the pse:count/pse:lastRead attributes below are
    # set as plain "pse:foo" strings (simpler and more predictable than
    # fighting ElementTree's clark-notation attribute handling), so the
    # prefix needs a real xmlns:pse declaration to be well-formed XML.
    root.set("xmlns:pse", PSE_NS)
    _sub(root, "id", feed_id)
    _sub(root, "title", title)
    _sub(root, "updated", _now())
    _sub(_sub(root, "author"), "name", "KINSHO")
    _link(root, "self", self_href, self_type)
    return root


def _render(root) -> Response:
    body = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
    return Response(content=body, media_type="application/atom+xml;charset=utf-8")


def _ext_media_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return IMAGE_MEDIA_TYPES.get(ext, "image/jpeg")


# ── ROOT: one entry per accessible library ──────────────────────────────────

def build_root_feed(libraries: list) -> Response:
    root = _feed_root("kinsho:root", "KINSHO", "/opds/")
    _link(root, "start", "/opds/", NAV_TYPE)
    _link(root, "search", "/opds/search-description.xml", OPENSEARCH_TYPE)
    for lib in libraries:
        entry = _sub(root, "entry")
        _sub(entry, "title", lib["name"])
        _sub(entry, "id", f"kinsho:library:{lib['id']}")
        _sub(entry, "updated", _now())
        _link(entry, "subsection", f"/opds/library/{lib['id']}", NAV_TYPE)
    return _render(root)


# ── LIBRARY: one entry per manga, paginated ──────────────────────────────────

def build_library_feed(library_id: int, library_name: str, mangas: list, page: int, total_pages: int) -> Response:
    self_href = f"/opds/library/{library_id}?page={page}"
    root = _feed_root(f"kinsho:library:{library_id}", library_name, self_href)
    if page > 1:
        _link(root, "previous", f"/opds/library/{library_id}?page={page - 1}", NAV_TYPE)
    if page < total_pages:
        _link(root, "next", f"/opds/library/{library_id}?page={page + 1}", NAV_TYPE)
    for m in mangas:
        entry = _sub(root, "entry")
        _sub(entry, "title", m["name"])
        _sub(entry, "id", f"kinsho:manga:{library_id}:{m['id']}")
        _sub(entry, "updated", _now())
        _link(entry, "subsection", f"/opds/manga/{library_id}/{m['id']}", ACQ_TYPE)
        if m.get("cover_url"):
            _link(entry, "http://opds-spec.org/image", m["cover_url"])
            _link(entry, "http://opds-spec.org/image/thumbnail", m["cover_url"])
    return _render(root)


# ── SEARCH ───────────────────────────────────────────────────────────────────

def build_search_description() -> Response:
    root = ET.Element("OpenSearchDescription", {"xmlns": "http://a9.com/-/spec/opensearch/1.1/"})
    ET.SubElement(root, "ShortName").text = "KINSHO"
    ET.SubElement(root, "Description").text = "Search your KINSHO library"
    ET.SubElement(root, "Url", {
        "type": "application/atom+xml;profile=opds-catalog;kind=acquisition",
        "template": "/opds/search?q={searchTerms}",
    })
    body = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
    return Response(content=body, media_type="application/opensearchdescription+xml;charset=utf-8")


def build_search_feed(query: str, mangas: list) -> Response:
    """mangas: list of {"library_id", "id", "name", "cover_url"} across every accessible library."""
    root = _feed_root("kinsho:search", f"Search: {query}", f"/opds/search?q={quote(query)}", ACQ_TYPE)
    for m in mangas:
        entry = _sub(root, "entry")
        _sub(entry, "title", m["name"])
        _sub(entry, "id", f"kinsho:manga:{m['library_id']}:{m['id']}")
        _sub(entry, "updated", _now())
        _link(entry, "subsection", f"/opds/manga/{m['library_id']}/{m['id']}", ACQ_TYPE)
        if m.get("cover_url"):
            _link(entry, "http://opds-spec.org/image", m["cover_url"])
            _link(entry, "http://opds-spec.org/image/thumbnail", m["cover_url"])
    return _render(root)


# ── MANGA: acquisition feed, one entry per chapter/volume ───────────────────

def build_manga_feed(library_id: int, manga_id: str, manga_name: str, description: str, items: list) -> Response:
    """
    items: list of {
      "id", "name", "cover_url", "page_count", "first_page_url", "last_read_page"
    } — one per chapter or volume, already resolved by main.py (both share the
    same URL shape: .../page/{index}, so this function doesn't care which).
    """
    root = _feed_root(
        f"kinsho:manga:{library_id}:{manga_id}", manga_name,
        f"/opds/manga/{library_id}/{manga_id}", ACQ_TYPE,
    )
    if description:
        _sub(root, "subtitle", description)
    for item in items:
        entry = _sub(root, "entry")
        _sub(entry, "title", item["name"])
        _sub(entry, "id", f"kinsho:item:{library_id}:{manga_id}:{item['id']}")
        _sub(entry, "updated", _now())
        if item.get("cover_url"):
            _link(entry, "http://opds-spec.org/image", item["cover_url"])
            _link(entry, "http://opds-spec.org/image/thumbnail", item["cover_url"])
        if item["page_count"] > 0:
            pse_attrs = {"pse_count": item["page_count"]}
            if item.get("last_read_page"):
                pse_attrs["pse_lastRead"] = item["last_read_page"]
            _link(
                entry, "http://vaemendis.net/opds-pse/stream", item["first_page_url"],
                _ext_media_type(item["first_page_url"]), **pse_attrs,
            )
    return _render(root)
