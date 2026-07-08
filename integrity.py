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

import numpy as np
from PIL import Image
import io

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}

# A perceptual-hash Hamming distance this loose lets two pages that AREN'T
# byte-identical through to the (expensive) SSIM pass as candidates — this
# threshold only decides what's worth double-checking, not what gets flagged.
PHASH_CANDIDATE_DISTANCE = 10   # out of 64 bits
# The real filter: two candidate pages are only flagged as duplicates once
# their SSIM score clears this. Exact byte/hash matches skip straight past
# this (their SSIM is trivially 1.0 — no point spending the compute).
SSIM_DUPLICATE_THRESHOLD = 0.80
SSIM_COMPARE_SIZE = 256   # both images resized to this before scoring — bounds
                          # compute cost regardless of source resolution and
                          # lets differently-sized re-releases of the same
                          # page still compare cleanly.
SSIM_WINDOW = 7           # matches the common default for 8-bit images.

# A page that's mostly one flat color (a solid-black transition panel, a
# mostly-white page with a small inset) trivially scores ~1.0 SSIM in that
# flat region as long as the OTHER image is also near that same level in the
# same spot — common, since white margins are near-universal in manga. With
# that region covering most of the page, it can pull the whole-image mean
# above SSIM_DUPLICATE_THRESHOLD even when the actual artwork is completely
# different. DOMINANT_COLOR_TOLERANCE is how close (in 0-255 grayscale
# levels) a pixel has to be to an image's single most common intensity to
# count as "that image's dominant color" for masking purposes.
DOMINANT_COLOR_TOLERANCE = 14
# If masking out both images' shared dominant-color pixels leaves less than
# this fraction of the frame, there isn't enough real content left to trust
# a similarity score either way — treated as inconclusive (returns 0.0, i.e.
# not a match) rather than falling back to the whole (bias-inflated) image.
MIN_COMPARABLE_FRACTION = 0.05


def _phash(data: bytes) -> int:
    """8x8 average hash — cheap way to shortlist near-duplicate candidates."""
    img = Image.open(io.BytesIO(data)).convert('L').resize((8, 8), Image.LANCZOS)
    pixels = list(img.getdata())
    avg = sum(pixels) / len(pixels)
    bits = 0
    for p in pixels:
        bits = (bits << 1) | (1 if p >= avg else 0)
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count('1')


def _box_filter(arr: np.ndarray, win: int) -> np.ndarray:
    """Mean over a win x win window, same-size output — a summed-area-table
    box filter standing in for SSIM's usual Gaussian window (no scipy
    dependency needed for this)."""
    pad = win // 2
    padded = np.pad(arr, pad, mode='edge')
    sat = np.cumsum(np.cumsum(padded, axis=0), axis=1)
    sat = np.pad(sat, ((1, 0), (1, 0)), mode='constant')
    h, w = arr.shape
    total = (sat[win:win + h, win:win + w] - sat[:h, win:win + w]
             - sat[win:win + h, :w] + sat[:h, :w])
    return total / (win * win)


def _dominant_color_mask(arr: np.ndarray, tolerance: float) -> np.ndarray:
    """Boolean mask marking pixels within `tolerance` grayscale levels of the
    array's single most common intensity — its dominant/background color."""
    values, counts = np.unique(np.round(arr).astype(np.uint8), return_counts=True)
    dominant = values[np.argmax(counts)]
    return np.abs(arr - dominant) <= tolerance


def _ssim_score(data_a: bytes, data_b: bytes) -> float:
    """Windowed SSIM between two images, decoded, grayscaled, and resized to
    a common size first so source resolution/aspect differences don't block
    the comparison. Pixels that are close to BOTH images' own dominant color
    at the same spot are excluded from the mean before scoring — see
    DOMINANT_COLOR_TOLERANCE above for why. Deliberately an intersection,
    not "either image's background": a flat region in only ONE image,
    compared against real content in the other, already scores low there and
    should keep counting against a match."""
    img_a = Image.open(io.BytesIO(data_a)).convert('L').resize(
        (SSIM_COMPARE_SIZE, SSIM_COMPARE_SIZE), Image.LANCZOS)
    img_b = Image.open(io.BytesIO(data_b)).convert('L').resize(
        (SSIM_COMPARE_SIZE, SSIM_COMPARE_SIZE), Image.LANCZOS)
    x = np.asarray(img_a, dtype=np.float64)
    y = np.asarray(img_b, dtype=np.float64)

    C1 = (0.01 * 255) ** 2
    C2 = (0.03 * 255) ** 2

    mu_x = _box_filter(x, SSIM_WINDOW)
    mu_y = _box_filter(y, SSIM_WINDOW)
    mu_x2, mu_y2, mu_xy = mu_x * mu_x, mu_y * mu_y, mu_x * mu_y

    sigma_x2 = _box_filter(x * x, SSIM_WINDOW) - mu_x2
    sigma_y2 = _box_filter(y * y, SSIM_WINDOW) - mu_y2
    sigma_xy = _box_filter(x * y, SSIM_WINDOW) - mu_xy

    ssim_map = ((2 * mu_xy + C1) * (2 * sigma_xy + C2)) / \
               ((mu_x2 + mu_y2 + C1) * (sigma_x2 + sigma_y2 + C2))

    shared_dominant = _dominant_color_mask(x, DOMINANT_COLOR_TOLERANCE) & \
                       _dominant_color_mask(y, DOMINANT_COLOR_TOLERANCE)
    included = ~shared_dominant
    if included.sum() < ssim_map.size * MIN_COMPARABLE_FRACTION:
        return 0.0
    return float(ssim_map[included].mean())


def _find_duplicate_groups(hashes: dict, raw_data: dict) -> list:
    """
    hashes: {filename: sha1_hex}. raw_data: {filename: bytes}, same keys.

    Two stages:
    - Exact SHA1 match = automatic duplicate group (identical bytes, so any
      similarity score on them is guaranteed to be 1.0 — no need to spend
      the SSIM compute confirming the obvious).
    - Everything else is only a *candidate*: a cheap perceptual hash first
      shortlists pages that look roughly alike (catches a page re-saved at
      different quality/size by a different release), then an actual SSIM
      score confirms or rejects each candidate pair. This is what stops two
      genuinely different pages that just happen to share overall tone or
      layout from getting flagged as duplicates.

    Returns [{"filenames": [...], "similarity": float}, ...] — similarity is
    always 1.0 for exact groups, the measured SSIM score for near ones.
    """
    by_hash = defaultdict(list)
    for name, h in hashes.items():
        by_hash[h].append(name)
    exact_groups = [
        {"filenames": sorted(names), "similarity": 1.0}
        for names in by_hash.values() if len(names) > 1
    ]
    already_grouped = {name for g in exact_groups for name in g["filenames"]}

    remaining = [n for n in raw_data if n not in already_grouped]
    phashes = {}
    for name in remaining:
        try:
            phashes[name] = _phash(raw_data[name])
        except Exception:
            continue   # not decodable as an image — corruption is caught elsewhere

    near_groups = []
    seen = set()
    names_list = sorted(phashes.keys())
    for i, name_a in enumerate(names_list):
        if name_a in seen:
            continue
        group = [name_a]
        best_score = None
        for name_b in names_list[i + 1:]:
            if name_b in seen:
                continue
            if _hamming(phashes[name_a], phashes[name_b]) > PHASH_CANDIDATE_DISTANCE:
                continue
            try:
                score = _ssim_score(raw_data[name_a], raw_data[name_b])
            except Exception:
                continue
            if score >= SSIM_DUPLICATE_THRESHOLD:
                group.append(name_b)
                best_score = score if best_score is None else min(best_score, score)
        if len(group) > 1:
            near_groups.append({"filenames": sorted(group), "similarity": best_score})
            seen.update(group)

    return exact_groups + near_groups


def check_archive(source_path: str, open_archive_fn, read_entry_fn, prefix: str = "") -> dict:
    """
    Reads every image entry once, which does double duty: zipfile/rarfile
    validate each entry's CRC as part of decompression, so a failed read IS
    the corruption check — no separate zipfile.testzip() pass needed. The
    same read gives us the bytes to hash for duplicate detection.

    prefix: for case1 manga (one archive containing chapter subfolders) the
    same archive is checked once PER CHAPTER, and the check must be scoped
    to that chapter's entries — otherwise every chapter re-hashes the whole
    series, a credits page repeated in each chapter flags as a "duplicate"
    (the exact cross-chapter false positive duplicate detection is scoped to
    avoid), and the identical whole-archive finding is reported once per
    chapter. Empty prefix = whole archive (case2 volumes / case3 chapters).

    Returns {"corrupt": str|None, "duplicate_groups": [{"filenames": [...], "similarity": float}, ...]}.
    """
    arc = open_archive_fn(source_path)
    if arc is None:
        return {"corrupt": "Failed to open archive", "duplicate_groups": []}
    try:
        try:
            names = [n for n in arc.namelist() if os.path.splitext(n)[1].lower() in IMAGE_EXTENSIONS]
            if prefix:
                names = [n for n in names if n.startswith(prefix)]
        except Exception as e:
            return {"corrupt": f"Failed to list archive contents: {e}", "duplicate_groups": []}
        hashes = {}
        raw_data = {}
        for name in names:
            data = read_entry_fn(arc, name)
            if data is None:
                return {"corrupt": f"Failed to read page: {os.path.basename(name)}", "duplicate_groups": []}
            hashes[name] = hashlib.sha1(data).hexdigest()
            raw_data[name] = data
    finally:
        try:
            arc.close()
        except Exception:
            pass
    return {"corrupt": None, "duplicate_groups": _find_duplicate_groups(hashes, raw_data)}


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
    raw_data = {}
    for name in filenames:
        full_path = os.path.join(source_path, name)
        try:
            with open(full_path, "rb") as f:
                data = f.read()
            Image.open(io.BytesIO(data)).load()
        except Exception as e:
            return {"corrupt": f"Failed to read/decode page: {name} ({e})", "duplicate_groups": []}
        hashes[name] = hashlib.sha1(data).hexdigest()
        raw_data[name] = data
    return {"corrupt": None, "duplicate_groups": _find_duplicate_groups(hashes, raw_data)}


def check_item(item: dict, open_archive_fn, read_entry_fn) -> dict:
    """item: one entry from main.py's checkable_items_for_manga()."""
    if item["source_type"] == "archive":
        return check_archive(item["source_path"], open_archive_fn, read_entry_fn,
                             prefix=item.get("prefix", ""))
    return check_loose(item["source_path"])
