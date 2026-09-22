// ── LONG-STRIP CANVAS BUFFER WORKER (opt-in, 2026-09-21; one canvas per
// page 2026-09-22) ──────────────────────────────────────────────────────
// Owns a small, fixed set of OffscreenCanvas elements transferred from the
// main-thread long-strip reader (see the "CANVAS BUFFER" section of
// chapter_reader.html) -- one per PAGE now, not an arbitrary multi-page
// segment. Every fetch/decode/paint for this feature happens in here,
// never on the main thread -- see that section's own comment for why (the
// 2026-09-03 canvas rewrite that was reverted 2026-09-20 did all of its
// ctx.drawImage() calls on the main thread; this worker exists
// specifically to not repeat that -- the per-page-vs-per-segment
// granularity was never the actual freeze cause).
//
// Message protocol (main -> worker), all fire-and-forget:
//   {type:'init',    segments:[{index, canvas /* transferred OffscreenCanvas */}], widthCss, dpr}
//   {type:'assign',  segmentIndex, heightPx} -- slot reassigned to a new page; heightPx
//                    is that page's own scaledH (pages vary in height, unlike the old
//                    fixed-grid segment version). dpr isn't sent -- the worker already
//                    tracks its own from init/setWidth. Resizing a canvas clears it AND
//                    resets the 2D context's transform, so this re-applies
//                    ctx.scale(dpr, dpr) too -- a separate clearRect is never needed here.
//   {type:'paint',   segmentIndex, jobs:[{url, iw, ih, sy0, sy1, destY, destH}]} -- always
//                    exactly one job now (a whole page fills its whole canvas), but jobs
//                    stays an array for shape continuity with the paint-progress-tracking
//                    days; nothing currently sends more than one.
//   {type:'releaseUrl', url}                              -- a page fell out of every slot's range
//   {type:'setWidth', widthCss}                           -- reader width changed (zoom/rotate)
//
// No message is sent back to the main thread -- the main thread tracks its
// own optimistic "have I already dispatched this page's paint job"
// bookkeeping (see cbPages[].painted in chapter_reader.html) rather than
// waiting on acks, since over-dispatching a redundant paint job is
// harmless (the worker just processes its queue in order) and an ack
// round-trip buys nothing correctness-wise here.

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
  }).catch((err) => {
    // A failed fetch/decode here just leaves this row range unpainted --
    // the main thread's own retry logic for the page itself (its ordinary
    // tier1/tier2 flow) isn't threaded through this worker, so a genuinely
    // broken page just stays black in the buffer. Acceptable for a first
    // cut of an opt-in, experimental feature; matches this worker's
    // no-ack design (see file header) -- revisit if real-device testing
    // shows this needs a retry.
    //
    // Logged (2026-09-22) -- a real-device test found EVERY segment black
    // with no other symptom, which this silent catch could fully explain on
    // its own (every single paint job failing the same way, e.g. an auth/
    // cookie issue specific to a fetch() made from inside a worker) --
    // console.error from a worker surfaces in DevTools (including over CDP/
    // adb) same as any other console call, so this alone may be enough to
    // pin down the actual failure next time this is tested.
    console.error('[canvas buffer worker] paint job failed', job.url, err);
  });
}

self.onmessage = (e) => {
  const msg = e.data;
  // Wrapped in try/catch (added 2026-09-22) -- a real-device test found
  // EVERY segment black with no other symptom and refresh not recovering
  // it, which a synchronous throw right here (e.g. canvas.getContext('2d')
  // returning null on a device without OffscreenCanvas 2D context support,
  // making the next line's ctx.scale() throw) would fully explain: nothing
  // in chapter_reader.html currently listens for cbWorker.onerror, so an
  // uncaught exception here previously had no visible symptom at all beyond
  // "nothing ever paints." console.error surfaces in DevTools (including
  // over CDP/adb) the same as any other console call.
  try {
    switch (msg.type) {
    case 'init': {
      widthCss = msg.widthCss;
      dpr = msg.dpr;
      for (const { index, canvas } of msg.segments) {
        const ctx = canvas.getContext('2d');
        if (!ctx) throw new Error('canvas.getContext(2d) returned null for segment ' + index);
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
        // Each slot is exactly one page now, and pages vary in height, so
        // a reassigned slot's own height can differ from what it had
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
  } catch (err) {
    console.error('[canvas buffer worker] onmessage threw for', msg.type, err);
  }
};
