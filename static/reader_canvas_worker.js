// ── LONG-STRIP CANVAS BUFFER WORKER (opt-in, 2026-09-21; one canvas per
// page 2026-09-22; memory-budgeted 2026-09-23; 1:1 source pixels 2026-09-23) ──
// Owns the OffscreenCanvas elements transferred from the main-thread
// long-strip reader (see the "CANVAS BUFFER" section of chapter_reader.html).
// Every fetch/decode/paint for this feature happens in here, never on the
// main thread (the 2026-09-03 canvas rewrite that was reverted 2026-09-20
// did its ctx.drawImage() calls on the main thread).
//
// Each canvas holds its page (or one piece of a very tall page) at the
// source image's own pixel size, 1:1 -- the compositor scales it to the
// displayed size exactly like it does an <img>. Screen-resolution canvases
// (the earlier design) were both bigger than the source (upscaled ~1.3x on
// the tablet) and, at 1848x8983, over the tablet GPU's 8192px texture limit,
// which the compositor silently shows as BLACK (proven on the Galaxy Tab S10
// Ultra, 2026-09-23). The main thread splits a page into pieces only when
// the source image itself is taller than the device's limit.
//
// Memory: a decoded bitmap is closed the moment the last paint job waiting
// on it has drawn it, and a canvas that leaves the buffer is shrunk to 1x1.
//
// Message protocol (main -> worker):
//   {type:'addSegments', segments:[{index, canvas /* transferred OffscreenCanvas */}]}
//   {type:'assign',      segmentIndex, width, height, globalIdx} -- backing size in source px
//   {type:'release',     segmentIndex}                             -- canvas left the buffer
//   {type:'paint',       segmentIndex, globalIdx, url, iw, sy0, rows} -- source rows [sy0, sy0+rows)
// Worker -> main:
//   {type:'paintFailed', segmentIndex, globalIdx} -- fetch/decode failed; main may retry

const ctxBySegment = new Map();   // segmentIndex -> CanvasRenderingContext2D
const assignedIdx  = new Map();   // segmentIndex -> globalIdx it currently shows (-1 = none)

// url -> { promise, users } -- one decode shared by every piece of a page
// (and any re-dispatch while a decode is in flight). Entries only live
// while at least one paint job is waiting.
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
      ctx.drawImage(bitmap, 0, msg.sy0, msg.iw, msg.rows, 0, 0, msg.iw, msg.rows);
    }
    releaseBitmap(msg.url, d, bitmap);
  }, err => {
    releaseBitmap(msg.url, d, null);
    console.error('[canvas buffer worker] paint failed', msg.url, err);
    self.postMessage({ type: 'paintFailed', segmentIndex: msg.segmentIndex, globalIdx: msg.globalIdx });
  });
}

self.onmessage = (e) => {
  const msg = e.data;
  try {
    switch (msg.type) {
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
      // Resizing also clears the canvas.
      ctx.canvas.width  = Math.max(1, msg.width);
      ctx.canvas.height = Math.max(1, msg.height);
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
