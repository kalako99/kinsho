// Collapsed description on the detail pages (manga, volume, collection;
// user's request 2026-10-08): the description is cut to whole lines so the
// column beside the cover (title, buttons, description, genres, tags) ends
// as near the cover's bottom as it can. Genres and tags are never cut and at
// least DESC_MIN_LINES lines of the description stay, so the column can still
// end lower than the cover. A "View more" button shows the rest. On a phone
// (cover above the text, not beside it) the description shows
// DESC_STACKED_LINES lines.
const DESC_MIN_LINES = 2;
const DESC_STACKED_LINES = 4;

// The height to cut the description to, in px, or null to show it whole.
// head: the column beside the cover; poster: the cover image (or its
// placeholder); desc: the description text; more: the "View more" button
// when it's on screen.
function descFitHeight(head, poster, desc, more) {
  if (!head || !poster || !desc) return null;
  const prev = desc.style.maxHeight;
  desc.style.maxHeight = 'none';
  const lh = parseFloat(getComputedStyle(desc).lineHeight) || 20;
  const full = desc.getBoundingClientRect().height;
  const stacked = poster.getBoundingClientRect().bottom <= head.getBoundingClientRect().top + 1;
  let lines;
  if (stacked) {
    lines = DESC_STACKED_LINES;
  } else {
    const moreH = more ? more.getBoundingClientRect().height + parseFloat(getComputedStyle(more).marginTop || 0) : 0;
    const headH = head.getBoundingClientRect().height - moreH;
    const target = poster.getBoundingClientRect().bottom - head.getBoundingClientRect().top;
    const excess = headH - target;
    desc.style.maxHeight = prev;
    if (excess <= 0) return null;
    // The button takes a line of its own under the cut text.
    lines = Math.floor((full - excess - (lh + 4)) / lh);
  }
  desc.style.maxHeight = prev;
  lines = Math.max(DESC_MIN_LINES, lines);
  return lines * lh < full - 1 ? lines * lh : null;
}
