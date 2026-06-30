"""
metadata_fetch.py

AniList metadata fetching for KINSHO.

Substep 1 — AniList client: search_anilist_manga()
Substep 2 — Confidence scoring: score_title_match(), score_all_candidates()
Substep 3 — Data write: apply_anilist_metadata()

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


# ── SUBSTEP 3: data write ─────────────────────────────────────────────
#
# Takes one confirmed AniList candidate (already chosen — either by the
# auto-accept logic in substep 5 or by the user clicking in the review UI)
# and writes its fields into the manga's dims.json via the caller-supplied
# load/save functions. This keeps the write logic here while keeping
# this module free of direct imports from main.py.
#
# Fields written: description, genres, tags (filtered by rank), and
# metadata_mtime (ISO timestamp). Existing dims keys that have nothing
# to do with AniList (chapters, volumes, image dimensions, etc.) are
# preserved untouched — we only overwrite the four metadata fields.

from datetime import datetime as _datetime


def apply_anilist_metadata(
    library_id: int,
    manga_name: str,
    candidate: dict,
    fields: set[str],
    load_fn,
    save_fn,
    min_tag_rank: int = DEFAULT_MIN_TAG_RANK,
) -> None:
    """
    Write AniList metadata from `candidate` into the manga's dims.json.

    Args:
        library_id: Library ID, passed through to load_fn / save_fn.
        manga_name: Manga folder name, passed through to load_fn / save_fn.
        candidate: One dict from search_anilist_manga (with match_score
            already attached from score_all_candidates is fine but not
            required — extra keys are ignored).
        fields: Which fields to write. Subset of:
            {"description", "genres", "tags"}
            Controls the per-manga refetch UI (substep 7) where the user
            can choose to re-import only some fields. Pass all three for
            the initial automatic import.
        load_fn: Callable matching main.py's load_manga_dims signature:
            load_fn(library_id: int, manga_name: str) -> dict
        save_fn: Callable matching main.py's save_manga_dims signature:
            save_fn(library_id: int, manga_name: str, data: dict) -> None
        min_tag_rank: Tags with rank below this are excluded (default 60).

    Returns:
        None. Raises only if load_fn or save_fn raise (i.e. filesystem
        errors), which the caller (substep 5's scan loop) should handle.
    """
    dims = load_fn(library_id, manga_name)

    if "description" in fields:
        dims["description"] = candidate.get("description") or ""

    if "genres" in fields:
        # Full overwrite — per design decision: if the user triggers a
        # refetch, they've decided the old data was wrong, so we replace
        # rather than merge to avoid accumulating stale entries.
        dims["genres"] = candidate.get("genres") or []

    if "tags" in fields:
        # Filter by rank before writing, discarding low-confidence tags.
        # Store just the name strings (not the rank dicts) since that's
        # what the rest of the app expects in dims["tags"].
        raw_tags = candidate.get("tags") or []
        dims["tags"] = [
            t["name"]
            for t in raw_tags
            if isinstance(t, dict) and t.get("rank", 0) >= min_tag_rank
        ]

    # Always stamp metadata_mtime when any field is written, regardless
    # of which subset was requested — this is what substep 4's skip-gate
    # checks (presence of this key = "this manga has been fetched before").
    dims["metadata_mtime"] = _datetime.utcnow().isoformat()

    save_fn(library_id, manga_name, dims)
