"""
Pure-logic module for the per-library "Rename volumes" feature (case2 loose
manga only) -- mirrors opds.py/comicinfo.py/auto_extract.py's convention:
no auth, no filesystem/data access, just plain functions over already-read
strings. main.py owns actually renaming folders and remapping ids.

Naming convention (confirmed against real files, see main.py's caller):
  "Grand Blue Dreaming 22 (1r0n)"  -- raw/user-cleaned input
  "Volume 22 (1r0n)"               -- this module's own output
Both forms parse identically -- only the trailing number and optional
single bracket group are inspected, so re-running against already-renamed
folders is a no-op (idempotent) and re-parses cleanly on every rescan.

Provider "compaction": a real release only brackets the LAST volume of a
run of consecutive volumes from the same provider (e.g. only volume 19 of
1-19 carries "(danke-Empire)"). decode_providers() expands that into a
per-volume resolved provider; recompact_providers() picks, for display,
exactly one volume per run (the last) to carry the bracket. Running both
on every rescan (not just once) is what lets a newly-added volume shift an
existing bracket forward -- e.g. adding volume 25 with the same provider
as 24 moves the bracket from 24 to 25.
"""
import re


def parse_volume_name(name: str) -> tuple[int, str | None] | None:
    """Extract (volume_number, provider_or_None) from a volume folder name,
    or None if the name is ambiguous and must be left untouched (more than
    one number cluster, more than one bracket group, or any leftover text
    outside the recognized number + trailing bracket). Deliberately
    conservative -- a raw uncleaned nyaa release name like
    "Grand Blue Dreaming v01 (2017) (Digital) (1r0n)" has THREE bracket
    groups and correctly fails here rather than guessing which one is the
    provider.
    """
    s = name.strip()
    if not s:
        return None

    provider = None
    m = re.search(r'\s*\(([^()]*)\)\s*$', s)
    if m:
        provider = m.group(1).strip()
        if not provider:
            return None
        remainder = s[:m.start()]
    else:
        remainder = s

    if '(' in remainder or ')' in remainder:
        return None  # a second bracket group survived -- ambiguous

    digit_runs = re.findall(r'\d+', remainder)
    if len(digit_runs) != 1:
        return None  # zero, or more than one, number cluster -- ambiguous

    return int(digit_runs[0]), provider


def format_volume_name(number: int, provider: str | None) -> str:
    base = f"Volume {number:02d}"
    return f"{base} ({provider})" if provider else base


def decode_providers(ordered: list[tuple[str, str | None]]) -> dict[str, str | None]:
    """ordered: [(item_key, raw_provider_or_None), ...] in volume order.

    Expands the compact "bracket only on the last volume of a run" source
    encoding into a per-item RESOLVED provider -- a bracket on item N
    applies to N and every unbracketed item back to (not including) the
    previous bracketed one. A trailing run that never gets closed by a
    bracket (e.g. the newest volume, not yet tagged) stays unresolved
    (None) for every item in it.
    """
    resolved: dict[str, str | None] = {}
    pending: list[str] = []
    for key, raw in ordered:
        pending.append(key)
        if raw is not None:
            for k in pending:
                resolved[k] = raw
            pending = []
    for k in pending:
        resolved[k] = None
    return resolved


def recompact_providers(ordered_keys: list[str], resolved: dict[str, str | None]) -> dict[str, str | None]:
    """Given each item's resolved provider (in volume order), decide which
    single item in each consecutive same-provider run should carry the
    bracket for DISPLAY -- always the last item of the run, so that
    extending a run by one (e.g. a new volume 25 sharing volume 24's
    provider) moves the bracket forward on the very next recompute.
    """
    display: dict[str, str | None] = {k: None for k in ordered_keys}
    n = len(ordered_keys)
    for i, key in enumerate(ordered_keys):
        prov = resolved.get(key)
        if prov is None:
            continue
        is_last_of_run = (i == n - 1) or (resolved.get(ordered_keys[i + 1]) != prov)
        if is_last_of_run:
            display[key] = prov
    return display


def compute_renamed_volumes(ordered: list[tuple[str, str]]) -> dict[str, str | None]:
    """ordered: [(item_key, current_name), ...] in natural-sort volume
    order. Returns {item_key: new_name_or_None} -- None means "could not
    parse this one, leave its name untouched entirely".

    An unparseable item is excluded from the provider chain (it's neither
    a source nor a target of decode/recompact) rather than resetting the
    chain around it -- its neighbors on both sides still resolve normally
    against each other. This is a deliberate choice for the (expected to
    be rare) case of a manually-dropped-in messy name sitting among
    otherwise-clean volumes.
    """
    keys_in_order = [key for key, _ in ordered]
    names = dict(ordered)
    parsed = {key: parse_volume_name(names[key]) for key in keys_in_order}

    parseable_keys = [k for k in keys_in_order if parsed[k] is not None]
    raw_pairs = [(k, parsed[k][1]) for k in parseable_keys]
    resolved = decode_providers(raw_pairs)
    display = recompact_providers(parseable_keys, resolved)

    result: dict[str, str | None] = {}
    for k in keys_in_order:
        if parsed[k] is None:
            result[k] = None
            continue
        number, _ = parsed[k]
        result[k] = format_volume_name(number, display[k])
    return result
