// ── LONG-STRIP CANVAS BUFFER WORKER (opt-in, 2026-09-21; one canvas per
// page 2026-09-22; memory-budgeted 2026-09-23) ──────────────────────────
// Owns the OffscreenCanvas elements transferred from the main-thread
// long-strip reader (see the "CANVAS BUFFER" section of chapter_reader.html)
// -- one per page. Every fetch/decode/paint for this feature happens in
// here, never on the main thread (the 2026-09-03 canvas rewrite that was
// reverted 2026-09-20 did its ctx.drawImage() calls on the main thread).
//
// Memory (2026-09-23, measured live on a Galaxy Tab S10 Ultra): a painted
// canvas holds raw RGBA pixels at device resolution -- a 1056x5133 CSS-px
// webtoon page at dpr 1.75 is ~66MB, regardless of how small the source
// WebP file is. The previous version of this file also kept every decoded
// ImageBitmap cached forever (its releaseUrl message was never sent), which
// took the renderer from ~265MB to ~1.7GB within seconds and froze this
// worker. So now: a decoded bitmap is closed the moment the last paint job
// waiting on it has drawn it (same as the 2026-09-03 version did), a canvas
// that leaves the buffer is shrunk to 1x1 to free its backing store, and
// the main thread decides how many pages to keep by a memory budget.
//
// Message protocol (main -> worker):
//   {type:'init',        widthCss, dpr}
//   {type:'addSegments', segments:[{index, canvas /* transferred OffscreenCanvas */}]}
//   {type:'assign',      segmentIndex, heightPx, globalIdx} -- canvas now shows manifest[globalIdx]
//   {type:'release',     segmentIndex}                      -- canvas left the buffer; free its memory
//   {type:'paint',       segmentIndex, globalIdx, url, iw, ih, destH}
// Worker -> main:
//   {type:'paintFailed', segmentIndex, globalIdx} -- fetch/decode failed; main may retry

const ctxBySegment = new Map();   // segmentIndex -> CanvasRenderingContext2D
const assignedIdx  = new Map();   // segmentIndex -> globalIdx it currently shows (-1 = none)
let widthCss = 0;
let dpr = 1;

// url -> { promise, users } -- deduplicates concurrent decodes of the same
// page (e.g. a slot re-dispatched while its first decode is still in
// flight). Entries only live while at least one paint job is waiting.
const decoding = new Map();

function acquireBitmap(url) {
  let d = decoding.get(url);
  if (!d) {
    d = {
      users: 0,
      promise: fetch(url)
        .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.blob(); })
        .then(blob => createImageBitmap(blob)),
    };
    decoding.set(url, d);
  }
  d.users++;
  return d;
}

function releaseBitmap(url, d, bitmap) {
  if (--d.users > 0) return;
  decoding.delete(url);
  if (bitmap) { try { bitmap.close(); } catch (err) {} }
}

function paint(msg) {
  const d = acquireBitmap(msg.url);
  d.promise.then(bitmap => {
    const ctx = ctxBySegment.get(msg.segmentIndex);
    // Only draw if this canvas still shows the page the job was for -- it
    // can be reassigned to a different page while the decode is in flight.
    if (ctx && assignedIdx.get(msg.segmentIndex) === msg.globalIdx) {
      ctx.drawImage(bitmap, 0, 0, msg.iw, msg.ih, 0, 0, widthCss, msg.destH);
    }
    releaseBitmap(msg.url, d, bitmap);
  }, err => {
    releaseBitmap(msg.url, d, null);
    console.error('[canvas buffer worker] paint failed', msg.url, err);
    self.postMessage({ type: 'paintFailed', segmentIndex: msg.segmentIndex, globalIdx: msg.globalIdx });
  });
}

function resize(ctx, heightPx) {
  // Changing width/height clears the canvas AND resets the 2D context's
  // transform (per spec, even to the same value), so the scale has to be
  // re-applied after every resize.
  ctx.canvas.width  = Math.max(1, Math.round(widthCss * dpr));
  ctx.canvas.height = Math.max(1, Math.round(heightPx * dpr));
  ctx.scale(dpr, dpr);
}

self.onmessage = (e) => {
  const msg = e.data;
  try {
    switch (msg.type) {
    case 'init':
      widthCss = msg.widthCss;
      dpr = msg.dpr;
      break;
    case 'addSegments':
      for (const { index, canvas } of msg.segments) {
        const ctx = canvas.getContext('2d');
        if (!ctx) throw new Error('canvas.getContext(2d) returned null for segment ' + index);
        ctxBySegment.set(index, ctx);
        assignedIdx.set(index, -1);
      }
      break;
    case 'assign': {
      const ctx = ctxBySegment.get(msg.segmentIndex);
      if (!ctx) break;
      resize(ctx, msg.heightPx);
      assignedIdx.set(msg.segmentIndex, msg.globalIdx);
      break;
    }
    case 'release': {
      const ctx = ctxBySegment.get(msg.segmentIndex);
      if (!ctx) break;
      ctx.canvas.width = 1;
      ctx.canvas.height = 1;
      assignedIdx.set(msg.segmentIndex, -1);
      break;
    }
    case 'paint':
      paint(msg);
      break;
    }
  } catch (err) {
    console.error('[canvas buffer worker] onmessage threw for', msg.type, err);
  }
};
