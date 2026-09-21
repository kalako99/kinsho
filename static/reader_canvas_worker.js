// ── LONG-STRIP CANVAS BUFFER WORKER (opt-in, 2026-09-21) ──────────────────
// Owns a small, fixed set of OffscreenCanvas segments transferred from the
// main-thread long-strip reader (see the "canvasBuffer*" section of
// chapter_reader.html). Every fetch/decode/paint for this feature happens
// in here, never on the main thread -- see that section's own comment for
// why (the 2026-09-03 canvas rewrite that was reverted 2026-09-20 did all
// of its ctx.drawImage() calls on the main thread; this worker exists
// specifically to not repeat that).
//
// Message protocol (main -> worker), all fire-and-forget:
//   {type:'init',    segments:[{index, canvas /* transferred OffscreenCanvas */}], widthCss, dpr}
//   {type:'assign',  segmentIndex, startGlobalY, heightPx} -- segment reassigned to a new
//                    logical range; heightPx is that range's own height (segments are
//                    page-aligned, not a fixed grid, so this varies -- see chapter_reader.html's
//                    cbBuildChainCentered). dpr isn't sent -- the worker already tracks its own
//                    from init/setWidth. Resizing a canvas clears it AND resets the 2D context's
//                    transform, so this re-applies ctx.scale(dpr, dpr) too -- a separate
//                    clearRect is never needed here.
//   {type:'paint',   segmentIndex, jobs:[{url, iw, ih, sy0, sy1, destY, destH}]}
//   {type:'releaseUrl', url}                              -- a page fell out of every segment's range
//   {type:'setWidth', widthCss}                           -- reader width changed (zoom/rotate)
//
// No message is sent back to the main thread -- the main thread tracks its
// own optimistic "what have I already dispatched" bookkeeping (see
// cbSegments[].filledCount in chapter_reader.html) rather than waiting on
// acks, since over-dispatching a redundant paint job is harmless (the
// worker just processes its queue in order) and an ack round-trip buys
// nothing correctness-wise here.

const ctxBySegment = new Map();   // segmentIndex -> CanvasRenderingContext2D
let widthCss = 0;
let dpr = 1;

// url -> { bitmap: ImageBitmap|null, promise: Promise|null }
// A page can be mid-paint into more than one segment at once (it spans a
// segment boundary), so decodes are deduped by URL exactly like the
// existing main-thread tier1/tier2 systems dedupe by manifest index.
const bitmaps = new Map();

function ensureBitmap(url) {
  let entry = bitmaps.get(url);
  if (entry && (entry.bitmap || entry.promise)) return entry.promise || Promise.resolve(entry.bitmap);
  entry = { bitmap: null, promise: null };
  entry.promise = fetch(url)
    .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.blob(); })
    .then(blob => createImageBitmap(blob))
    .then(bitmap => {
      entry.bitmap = bitmap;
      entry.promise = null;
      return bitmap;
    })
    .catch(err => {
      bitmaps.delete(url);
      throw err;
    });
  bitmaps.set(url, entry);
  return entry.promise;
}

function paintJob(job) {
  return ensureBitmap(job.url).then(bitmap => {
    const ctx = ctxBySegment.get(job.segmentIndex);
    if (!ctx) return; // segment was reassigned/torn down while this decode was in flight
    ctx.drawImage(
      bitmap,
      0, job.sy0, job.iw, job.sy1 - job.sy0,   // source rect (bitmap's own natural px)
      0, job.destY, widthCss, job.destH        // dest rect (CSS-px space; ctx already scaled by dpr)
    );
  }).catch(() => {
    // A failed fetch/decode here just leaves this row range unpainted --
    // the main thread's own retry logic for the page itself (its ordinary
    // tier1/tier2 flow) isn't threaded through this worker, so a genuinely
    // broken page just stays black in the buffer. Acceptable for a first
    // cut of an opt-in, experimental feature; matches this worker's
    // no-ack design (see file header) -- revisit if real-device testing
    // shows this needs a retry.
  });
}

self.onmessage = (e) => {
  const msg = e.data;
  switch (msg.type) {
    case 'init': {
      widthCss = msg.widthCss;
      dpr = msg.dpr;
      for (const { index, canvas } of msg.segments) {
        const ctx = canvas.getContext('2d');
        ctx.scale(dpr, dpr);
        ctxBySegment.set(index, ctx);
      }
      break;
    }
    case 'setWidth': {
      widthCss = msg.widthCss;
      break;
    }
    case 'assign': {
      const ctx = ctxBySegment.get(msg.segmentIndex);
      if (ctx) {
        // Segments are page-aligned now (2026-09-22), not a fixed grid, so
        // a reassigned segment's own height can differ from what it had
        // before -- resize the backing store to match. This already
        // clears the canvas and resets the 2D context's transform (per
        // spec, changing width/height does both, even to the same value),
        // so the scale has to be re-applied right after.
        ctx.canvas.width  = Math.round(widthCss * dpr);
        ctx.canvas.height = Math.round(msg.heightPx * dpr);
        ctx.scale(dpr, dpr);
      }
      break;
    }
    case 'paint': {
      for (const job of msg.jobs) {
        job.segmentIndex = msg.segmentIndex;
        paintJob(job);
      }
      break;
    }
    case 'releaseUrl': {
      const entry = bitmaps.get(msg.url);
      if (entry) {
        try { entry.bitmap?.close(); } catch (err) {}
        bitmaps.delete(msg.url);
      }
      break;
    }
  }
};
