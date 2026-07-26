"""
epub_reader.py — real text-chapter rendering for EPUB volumes (spine order,
table of contents, sanitized chapter HTML + the book's own CSS, embedded
assets), as opposed to get_epub_image_list()'s image-only extraction in
main.py (kept as-is, still used by the old image-strip reader/OPDS/thumbs).

Pure parsing: no auth, no data access of its own. main.py does every
permission check before calling in here, same convention as opds.py/comicinfo.py.

Uses lxml.html (not xml.etree.ElementTree, unlike opds.py/comicinfo.py) on
purpose: ebooklib already pulls lxml in as a hard dependency the moment
EPUB_SUPPORT is on, and ebooklib's own get_body_content() is itself built on
lxml's lenient HTML parser -- real-world "XHTML" chapter documents are
frequently not well-formed strict XML, so a strict parser would hard-fail on
content a real e-reader renders fine. The ElementTree convention elsewhere in
this project is about serializing app-controlled data into new XML, a
different problem from parsing untrusted, producer-varied third-party markup.
"""

import posixpath
import lxml.html as LH

try:
    import ebooklib
    from ebooklib import epub as ebooklib_epub
    EPUB_SUPPORT = True
except ImportError:
    EPUB_SUPPORT = False

# Manifest item types a rendered chapter's HTML/CSS can legitimately reference.
# Deliberately excludes ITEM_DOCUMENT (other chapters go through the chapter
# endpoint, not "asset") and ITEM_SCRIPT/AUDIO/VIDEO/SMIL (no v1 use) -- on
# top of the exact-manifest-match check, this narrows what's servable at all.
_ASSET_TYPES = None  # populated lazily below, once ebooklib's constants exist


def _asset_types():
    global _ASSET_TYPES
    if _ASSET_TYPES is None:
        _ASSET_TYPES = {
            ebooklib.ITEM_IMAGE, ebooklib.ITEM_STYLE, ebooklib.ITEM_FONT,
            ebooklib.ITEM_COVER, ebooklib.ITEM_VECTOR,
        }
    return _ASSET_TYPES


# Small cache of parsed EpubBook objects -- every chapter turn is now a fresh
# HTTP request (toc, each chapter, each asset), so re-parsing the whole file
# from scratch per request would add real, avoidable latency. Same unlocked
# plain-dict convention as _epub_page_cache/_archive_page_cache in main.py.
# Deliberately a much smaller cap than those -- a whole parsed book with every
# manifest item's bytes in memory is far heavier per entry than one page.
_book_cache: dict = {}
_BOOK_CACHE_MAX = 8


def get_book(epub_path: str):
    """Parsed EpubBook for this path, served from a small in-memory cache
    (see invalidate() below). None if EPUB_SUPPORT is off or the file fails
    to parse. main.py calls this once per request to get a handle to pass
    into the other functions below."""
    if not EPUB_SUPPORT:
        return None
    if epub_path in _book_cache:
        return _book_cache[epub_path]
    try:
        book = ebooklib_epub.read_epub(epub_path)
    except Exception as e:
        print(f"[EpubReader] Failed to parse {epub_path}: {e}")
        return None
    if len(_book_cache) >= _BOOK_CACHE_MAX:
        _book_cache.pop(next(iter(_book_cache)))
    _book_cache[epub_path] = book
    return book


def invalidate(epub_path: str):
    """Called by main.py's _invalidate_stale_source_caches() when a rescan
    detects this exact file changed on disk -- otherwise a stale parsed book
    would keep being served indefinitely (process-scoped cache)."""
    _book_cache.pop(epub_path, None)


def is_parseable(epub_path: str) -> bool:
    return get_book(epub_path) is not None


def build_reading_spine(book) -> list:
    """Ordered list of {"index", "name"} for every real content document in
    the book's raw spine order. Raw (not filtered to linear="yes") -- front
    matter is almost always linear="yes" too, so filtering wouldn't remove
    the "boring front matter" noise anyway, and would complicate mapping TOC
    entries to spine indices. The TOC (build_toc) is the fast-jump mechanism
    on top of this, not spine filtering."""
    spine_docs = []
    for idref, _linear in book.spine:
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        spine_docs.append({"index": len(spine_docs), "name": item.get_name()})
    return spine_docs


def _strip_fragment(href: str) -> str:
    return href.split("#", 1)[0] if href else href


def build_toc(book, spine_docs: list) -> list:
    """Nested {"title", "spine_index", "children"} tree from book.toc, mapped
    down to spine indices for chapter-jump navigation. spine_index is None
    for an entry that can't be resolved to any spine document (rendered as a
    non-navigable label by the caller) -- rather than erroring, since a TOC
    entry pointing at glossary/note content outside the reading spine, or an
    unresolvable EPUB2 NCX path (see below), isn't actually a defect in the
    book worth failing the whole TOC over.

    href resolution: nav.xhtml (EPUB3) hrefs are already zip-root-normalized
    by ebooklib. NCX (EPUB2) hrefs are NOT -- <content src="..."> is used raw,
    relative to wherever the NCX file itself sits, so an exact-path lookup
    misses a fair number of real EPUB2 books. Falls back to a basename-only
    match against the spine's document names, since real-world EPUB filenames
    are effectively unique across a package even when full paths differ.
    """
    by_name = {d["name"]: d["index"] for d in spine_docs}
    by_basename = {}
    for d in spine_docs:
        by_basename.setdefault(posixpath.basename(d["name"]), d["index"])

    def resolve(href: str):
        if not href:
            return None
        href = _strip_fragment(href)
        if href in by_name:
            return by_name[href]
        return by_basename.get(posixpath.basename(href))

    def walk(nodes) -> list:
        out = []
        for node in nodes:
            if isinstance(node, tuple):
                section, children = node
                out.append({
                    "title": section.title,
                    "spine_index": resolve(getattr(section, "href", None)),
                    "children": walk(children),
                })
            else:
                out.append({
                    "title": node.title,
                    "spine_index": resolve(node.href),
                    "children": [],
                })
        return out

    return walk(book.toc)


def _resolve_asset_path(doc_name: str, src: str) -> str:
    """An <img src="..."> inside a chapter is relative to THAT chapter
    document's own location in the zip, not the zip root -- e.g.
    "../cover.jpeg" inside "OEBPS/p000_cover.xhtml" resolves to "cover.jpeg"
    at zip root; "links/images/i02.png" inside "OEBPS/p003_frontispiece.xhtml"
    resolves to "OEBPS/links/images/i02.png". Confirmed against a real file."""
    return posixpath.normpath(posixpath.join(posixpath.dirname(doc_name), src))


def _sanitize_and_rewrite(body_el, doc_name: str, asset_base_url: str):
    """Mutates body_el in place: strips <script> tags, on* event handler
    attributes, and every anchor's href (footnote/external links keep their
    text, just become non-clickable -- the sandboxed iframe they'll render in
    has no allow-scripts/allow-top-navigation, so a link click has no safe
    behavior anyway). Rewrites every <img>/<image> src/href to the fixed
    asset-URL prefix, resolved per the rule above."""
    for script in body_el.iter("script"):
        parent = script.getparent()
        if parent is not None:
            parent.remove(script)

    for el in body_el.iter():
        for attr in list(el.attrib):
            if attr.lower().startswith("on"):
                del el.attrib[attr]
        if el.tag == "a" and "href" in el.attrib:
            del el.attrib["href"]

    for el in body_el.iter("img"):
        src = el.get("src")
        if src and not src.startswith(("http://", "https://", "data:")):
            resolved = _resolve_asset_path(doc_name, src)
            el.set("src", f"{asset_base_url}/{resolved}")
    # SVG <image> uses xlink:href (or bare href in newer SVG2 markup) for its
    # source, same treatment as <img src>. lxml.html parses this in HTML mode
    # (not XML-namespace-aware), so the attribute key is the literal string
    # "xlink:href", not a Clark-notation namespaced name -- confirmed against
    # a real epub's cover page, which uses exactly this pattern.
    for el in body_el.iter("image"):
        for attr in ("xlink:href", "href"):
            href = el.get(attr)
            if href and not href.startswith(("http://", "https://", "data:")):
                resolved = _resolve_asset_path(doc_name, href)
                el.set(attr, f"{asset_base_url}/{resolved}")


def render_chapter(book, spine_docs: list, spine_index: int, asset_base_url: str):
    """Returns {"html", "css", "title"} for one spine position, or None if
    the index is out of range or the document fails to parse. asset_base_url
    is the fixed prefix (e.g. "/api/manga/{lib}/{id}/volume/{vol}/epub/asset")
    every rewritten <img src> is rooted under."""
    if spine_index < 0 or spine_index >= len(spine_docs):
        return None
    doc_name = spine_docs[spine_index]["name"]
    item = book.get_item_with_href(doc_name)
    if item is None:
        return None

    try:
        body_bytes = item.get_body_content()
    except Exception as e:
        print(f"[EpubReader] Failed to read body of {doc_name}: {e}")
        return None

    # Parsed as a full minimal document (not lxml's fragment parser) so a
    # single-child body isn't silently unwrapped, dropping the book's own
    # <body class="..."> attribute that its CSS may target directly.
    try:
        full_doc = "<html><head></head>" + body_bytes.decode("utf-8", errors="replace") + "</html>"
        root = LH.document_fromstring(full_doc)
    except Exception as e:
        print(f"[EpubReader] Failed to parse {doc_name}: {e}")
        return None
    body_el = root.find("body")
    if body_el is None:
        return None

    _sanitize_and_rewrite(body_el, doc_name, asset_base_url)
    # Retagged from body to div (attributes/classes kept as-is) so the
    # frontend can safely nest this inside its own pagination wrapper --
    # a literal second <body> can't be nested inside another element.
    body_el.tag = "div"
    html = LH.tostring(body_el, encoding="unicode")

    css_parts = []
    for style_item in book.get_items_of_type(ebooklib.ITEM_STYLE):
        try:
            css_parts.append(style_item.get_content().decode("utf-8", errors="replace"))
        except Exception:
            continue

    title = None
    h = body_el.find(".//h1")
    if h is not None and h.text_content().strip():
        title = h.text_content().strip()

    return {"html": html, "css": "\n".join(css_parts), "title": title}


def resolve_asset(book, internal_path: str):
    """Returns (bytes, media_type) for one internal epub file, validated
    against the manifest and restricted to types a chapter's HTML/CSS could
    legitimately reference -- or None if not found/not allowed. internal_path
    must exactly match a manifest item's own name; no path traversal beyond
    what an exact manifest lookup can return."""
    allowed = _asset_types()
    for item in book.get_items():
        if item.get_name() == internal_path and item.get_type() in allowed:
            return item.get_content(), item.media_type
    return None
