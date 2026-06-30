"""
metadata_fetch.py

AniList + MangaDex metadata fetching for KINSHO.

Providers: search_anilist_manga() / search_mangadex_manga()
Confidence scoring: score_title_match(), score_all_candidates(), best_match()
Data write: resolve_field_value(), apply_resolved_metadata()

AniList is the primary source; MangaDex backs it up — filling fields AniList
is missing, supplying higher-resolution covers, and standing in entirely when
AniList is rate-limited or returns an ambiguous result set.

Nothing in this file imports from main.py. It receives load_manga_dims /
save_manga_dims as callable arguments so it stays independently testable
without needing the full FastAPI app to be running.
"""

import httpx
import re

ANILIST_API_URL = "https://graphql.anilist.co"

# All MediaFormat values that are valid under type: MANGA on AniList.
# Exposed here so the settings UI can build its checkbox/select list
# from the same source of truth rather than hardcoding them elsewhere.
ANILIST_MANGA_FORMATS = ["MANGA", "MANHWA", "MANHUA", "ONE_SHOT", "NOVEL"]

# Minimum AniList tag rank (0-100) to include in the written metadata.
# Tags below this threshold tend to be minor/spoilery — e.g. a tag with
# rank 17 means only ~17% of voters think it applies to the series.
# 60 is a reasonable starting default; substep 5 could expose this as
# a per-library setting later if needed.
DEFAULT_MIN_TAG_RANK = 60

# AniList GraphQL query — format is now a variable ($format: [MediaFormat])
# so the caller can pass one or more formats (or omit the filter entirely).
# type: MANGA is still hardcoded since we never want anime results here.
ANILIST_SEARCH_QUERY = """
query ($search: String, $perPage: Int, $format: [MediaFormat]) {
  Page(page: 1, perPage: $perPage) {
    media(search: $search, type: MANGA, format_in: $format) {
      id
      format
      title {
        romaji
        english
        native
      }
      description(asHtml: false)
      genres
      tags {
        name
        rank
      }
      status
      coverImage {
        extraLarge
        large
        medium
      }
      siteUrl
    }
  }
}
"""


async def search_anilist_manga(
    title: str,
    per_page: int = 5,
    formats: list[str] | None = None,
) -> list[dict]:
    """
    Query AniList for MANGA entries matching `title`.

    Args:
        title: The search string (e.g. a manga folder name).
        per_page: Max number of candidates to return (default 5).
        formats: Optional list of AniList MediaFormat values to restrict
            results to, e.g. ["MANGA", "MANHWA"]. Pass None (default) to
            search across all formats under type: MANGA. Valid values are
            the strings in ANILIST_MANGA_FORMATS.

    Returns:
        A list of plain dicts, one per AniList media result, shaped like:
        {
            "anilist_id": int,
            "format": str | None,        # e.g. "MANGA", "MANHWA", "NOVEL"
            "title_romaji": str | None,
            "title_english": str | None,
            "title_native": str | None,
            "description": str | None,
            "genres": list[str],
            "tags": [{"name": str, "rank": int}, ...],
            "status": str | None,
            "cover_url_extra_large": str | None,
            "cover_url_large": str | None,
            "cover_url_medium": str | None,
            "site_url": str | None,
        }
        Returns an empty list if AniList returns zero matches.

    Raises:
        httpx.HTTPStatusError: on non-2xx HTTP status (e.g. 429 rate limit).
        httpx.RequestError: on network-level failures (timeout, DNS, etc).
        RuntimeError: if AniList returns GraphQL-level errors in the body.

    Deliberately does NOT catch these exceptions — error policy belongs
    to the caller (substep 5's scan loop), not this low-level client.
    """
    variables: dict = {"search": title, "perPage": per_page}
    if formats:
        variables["format"] = formats
    # If formats is None/empty we omit the variable entirely; AniList
    # treats a missing $format as "no format filter" — all formats match.

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            ANILIST_API_URL,
            json={"query": ANILIST_SEARCH_QUERY, "variables": variables},
        )
        response.raise_for_status()
        payload = response.json()

    if "errors" in payload:
        raise RuntimeError(f"AniList GraphQL error: {payload['errors']}")

    raw_results = payload.get("data", {}).get("Page", {}).get("media", [])

    results = []
    for m in raw_results:
        title_obj = m.get("title") or {}
        cover_obj = m.get("coverImage") or {}
        results.append({
            "provider":         "anilist",
            "anilist_id":       m.get("id"),
            "format":           m.get("format"),
            "title_romaji":     title_obj.get("romaji"),
            "title_english":    title_obj.get("english"),
            "title_native":     title_obj.get("native"),
            "description":      m.get("description"),
            "genres":           m.get("genres") or [],
            "tags":             m.get("tags") or [],
            "status":           m.get("status"),
            "cover_url_extra_large": cover_obj.get("extraLarge"),
            "cover_url_large":  cover_obj.get("large"),
            "cover_url_medium": cover_obj.get("medium"),
            "site_url":         m.get("siteUrl"),
        })

    return results


MANGADEX_API_URL = "https://api.mangadex.org"
MANGADEX_UPLOADS_URL = "https://uploads.mangadex.org"


async def search_mangadex_manga(title: str, per_page: int = 5) -> list[dict]:
    """
    Query MangaDex for series matching `title` and return normalized candidate
    dicts that mirror the AniList candidate shape closely enough that the same
    scoring (score_all_candidates) and field-resolution (resolve_field_value)
    helpers work on either provider's results.

    MangaDex is the fallback provider: it fills metadata fields AniList is
    missing, supplies higher-resolution covers (original uploaded scans,
    often 1500px+), and stands in entirely when AniList is rate-limited or
    returns an ambiguous set of results.

    Each result is shaped like:
        {
            "provider": "mangadex",
            "mangadex_id": str,
            "title_english": str | None,   # main title, used for scoring
            "title_romaji":  str | None,   # ja-ro alt title if present
            "title_native":  str | None,   # ja alt title if present
            "description": str | None,
            "genres": list[str],           # tags in MangaDex's "genre" group
            "tags":   list[str],           # tags in the "theme"/"content" groups
            "status": str | None,
            "cover_url": str | None,       # original-resolution cover image
        }

    Raises httpx errors on network/HTTP failure (caller decides policy).
    """
    params = [
        ("title", title),
        ("limit", per_page),
        ("includes[]", "cover_art"),
        ("contentRating[]", "safe"),
        ("contentRating[]", "suggestive"),
        ("contentRating[]", "erotica"),
        ("contentRating[]", "pornographic"),
        ("order[relevance]", "desc"),
    ]
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = await client.get(f"{MANGADEX_API_URL}/manga", params=params)
        response.raise_for_status()
        payload = response.json()

    results = []
    for m in payload.get("data", []):
        attrs = m.get("attributes") or {}

        title_map = attrs.get("title") or {}
        main_title = title_map.get("en") or next(iter(title_map.values()), None)
        romaji = native = None
        for alt in attrs.get("altTitles") or []:
            if not isinstance(alt, dict):
                continue
            if not romaji and alt.get("ja-ro"):
                romaji = alt["ja-ro"]
            if not native and alt.get("ja"):
                native = alt["ja"]

        desc_map = attrs.get("description") or {}
        description = desc_map.get("en") or next(iter(desc_map.values()), None)

        genres, tags = [], []
        for t in attrs.get("tags") or []:
            t_attrs = t.get("attributes") or {}
            name_map = t_attrs.get("name") or {}
            name = name_map.get("en") or next(iter(name_map.values()), None)
            if not name:
                continue
            group = t_attrs.get("group")
            if group == "genre":
                genres.append(name)
            elif group in ("theme", "content"):
                tags.append(name)

        cover_url = None
        for rel in m.get("relationships") or []:
            if rel.get("type") == "cover_art":
                fn = (rel.get("attributes") or {}).get("fileName")
                if fn:
                    cover_url = f"{MANGADEX_UPLOADS_URL}/covers/{m['id']}/{fn}"
                break

        results.append({
            "provider":      "mangadex",
            "mangadex_id":   m.get("id"),
            "title_english": main_title,
            "title_romaji":  romaji,
            "title_native":  native,
            "description":   description,
            "genres":        genres,
            "tags":          tags,
            "status":        attrs.get("status"),
            "cover_url":     cover_url,
        })

    return results


async def download_cover_image(url: str) -> bytes:
    """
    Download the image at `url` and return its raw bytes.

    Used to fetch AniList cover images so the caller can process them into
    the local covers directory. Like search_anilist_manga, it deliberately
    does not swallow exceptions — error handling is the caller's job.

    Raises:
        httpx.HTTPStatusError: on non-2xx HTTP status.
        httpx.RequestError: on network-level failures.
    """
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


# ── SUBSTEP 2: matching / confidence scoring ──────────────────────────
#
# Pure logic, no network calls. Takes a query title and one AniList
# candidate dict (as returned by search_anilist_manga) and returns a
# similarity score between 0.0 (no resemblance) and 1.0 (identical).
#
# This does NOT decide policy (what to do with the score — auto-accept,
# show for manual choice, skip). That decision belongs to substep 5
# (the scan endpoint), since "what counts as confident enough" is a
# product decision that may need tuning after seeing real results,
# not something to bury inside the scoring function itself.

from difflib import SequenceMatcher


def _normalize_title(s: str) -> str:
    """
    Lowercase and strip characters that commonly differ between a folder
    name and an AniList title for reasons that have nothing to do with
    it being a different series — punctuation, extra whitespace, etc.
    Kept deliberately simple for now; this is the first thing to revisit
    if real-world folder names produce surprising low scores.
    """
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)   # punctuation -> space
    s = re.sub(r"\s+", " ", s).strip()
    return s


def score_title_match(query: str, candidate: dict) -> float:
    """
    Compare `query` (e.g. a manga folder name) against one AniList
    candidate's romaji/english/native titles, and return the BEST
    similarity score found across the three.

    Using the best of three (rather than e.g. averaging) is deliberate:
    a folder named exactly after the English title shouldn't be scored
    down just because the native Japanese title obviously won't match
    a Latin-alphabet query. We want "does this candidate match the
    query in ANY of the forms AniList knows it by", not "does it match
    in every form simultaneously".

    Args:
        query: The search/folder title.
        candidate: One dict as returned by search_anilist_manga.

    Returns:
        A float in [0.0, 1.0]. 1.0 means an exact match (after
        normalization) against at least one of the three title fields.
    """
    query_norm = _normalize_title(query)
    if not query_norm:
        return 0.0

    best = 0.0
    for field in ("title_romaji", "title_english", "title_native"):
        candidate_title = candidate.get(field)
        if not candidate_title:
            continue
        candidate_norm = _normalize_title(candidate_title)
        if not candidate_norm:
            continue
        ratio = SequenceMatcher(None, query_norm, candidate_norm).ratio()
        if ratio > best:
            best = ratio

    return best


def score_all_candidates(query: str, candidates: list[dict]) -> list[dict]:
    """
    Convenience wrapper: takes the list returned by search_anilist_manga
    and returns the same list of dicts, each with a new "match_score"
    key added, sorted by that score descending (best match first).

    This does not mutate the input list's dicts in place beyond adding
    the new key, and does not filter anything out — even a 0.0-scoring
    candidate is still returned, since substep 5's policy logic (not
    this function) is what decides what to do with low scores.
    """
    scored = []
    for c in candidates:
        c_copy = dict(c)
        c_copy["match_score"] = score_title_match(query, c)
        scored.append(c_copy)

    scored.sort(key=lambda c: c["match_score"], reverse=True)
    return scored


def best_match(query: str, candidates: list[dict], min_score: float = 0.6) -> dict | None:
    """
    Return the single best-scoring candidate for `query` whose match_score
    clears `min_score`, or None if none do (or the list is empty).

    Works on either AniList or MangaDex candidates since both carry the
    title_* keys score_title_match reads. Used to pick the MangaDex entry
    that corresponds to the manga we're importing — both as a per-field
    fallback source and for cover selection.
    """
    if not candidates:
        return None
    scored = score_all_candidates(query, candidates)
    top = scored[0]
    return top if top.get("match_score", 0.0) >= min_score else None


# ── SUBSTEP 3: data write ─────────────────────────────────────────────
#
# Takes a confirmed primary candidate (AniList or MangaDex) plus an optional
# fallback candidate, and writes the chosen fields into the manga's dims.json
# via caller-supplied load/save functions. Per field, the primary's value is
# used; if the primary doesn't have that field, the fallback's value fills the
# gap. This is how "description missing in AniList → take it from MangaDex"
# works without dragging in unrelated fields.
#
# Per-field timestamps are stored under dims["metadata_mtimes"] (a dict keyed
# by field name), so a cover-only fetch no longer marks description/genres/tags
# as done. The legacy single dims["metadata_mtime"] is still honoured by the
# skip-gate in main.py for manga imported before this change.

from datetime import datetime as _datetime

# Image (cover) is fetched/written by main.py, but it shares the per-field
# mtime convention so the skip-gate treats it like any other metadata field.
METADATA_FIELDS = ("description", "genres", "tags", "cover")


def resolve_field_value(candidate: dict | None, field: str, min_tag_rank: int = DEFAULT_MIN_TAG_RANK):
    """
    Extract one metadata field's value from a candidate, normalized to the
    shape the app stores in dims.json, or None if the candidate doesn't
    provide it. Returning None (rather than an empty string/list) is what
    lets the caller decide to fall back to another provider for that field.

    Handles both candidate shapes: AniList tags are {name, rank} dicts
    (filtered by rank); MangaDex tags are already plain name strings.
    """
    if not candidate:
        return None
    if field == "description":
        return candidate.get("description") or None
    if field == "genres":
        genres = candidate.get("genres") or []
        return list(genres) if genres else None
    if field == "tags":
        raw_tags = candidate.get("tags") or []
        names = []
        for t in raw_tags:
            if isinstance(t, dict):
                if t.get("rank", 0) >= min_tag_rank and t.get("name"):
                    names.append(t["name"])
            elif isinstance(t, str):
                names.append(t)
        return names if names else None
    return None


def apply_resolved_metadata(
    library_id: int,
    manga_name: str,
    primary: dict | None,
    fallback: dict | None,
    fields,
    load_fn,
    save_fn,
    min_tag_rank: int = DEFAULT_MIN_TAG_RANK,
) -> None:
    """
    Write the chosen `fields` into the manga's dims.json, taking each field's
    value from `primary` first and `fallback` second (per-field fallback).

    Args:
        primary: The main candidate to import from (AniList or MangaDex).
        fallback: Optional second candidate whose values fill any field the
            primary doesn't provide. Pass None when there's nothing to fall
            back to.
        fields: Iterable of field names to write/stamp. May include "cover";
            the cover image itself is fetched and written by main.py, so here
            "cover" only contributes its per-field timestamp.

    Stamps dims["metadata_mtimes"][field] for every field in `fields`,
    regardless of whether a value was found — once a field has been attempted
    for a confirmed match, the skip-gate considers it done (the per-manga
    refetch can always force it again). Existing dims keys unrelated to these
    fields are preserved untouched.
    """
    dims = load_fn(library_id, manga_name)
    mtimes = dict(dims.get("metadata_mtimes") or {})
    now = _datetime.utcnow().isoformat()

    for field in fields:
        if field in ("description", "genres", "tags"):
            value = resolve_field_value(primary, field, min_tag_rank)
            if value is None:
                value = resolve_field_value(fallback, field, min_tag_rank)
            if field == "description":
                dims["description"] = value or ""
            elif field == "genres":
                dims["genres"] = value or []
            elif field == "tags":
                dims["tags"] = value or []
        mtimes[field] = now

    dims["metadata_mtimes"] = mtimes
    save_fn(library_id, manga_name, dims)
