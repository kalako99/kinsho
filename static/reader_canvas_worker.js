// ── LONG-STRIP CANVAS BUFFER WORKER (opt-in, 2026-09-21; one canvas per
// page 2026-09-22; memory-budgeted 2026-09-23; 1:1 source pixels 2026-09-23;
// decode-only, bitmaps sent back to the page 2026-09-24) ──
// Fetches and decodes the pages for the long-strip canvas buffer (see the
// "CANVAS BUFFER" section of chapter_reader.html), off the main thread, and
// sends each finished ImageBitmap back (transferred, not copied). The page
// hands it to its canvas with transferFromImageBitmap() -- no drawing
// anywhere. This worker used to own the canvases (OffscreenCanvas + 2D
// drawImage); that kept ~3.3 copies of every page on the GPU, see the
// section comment in chapter_reader.html.
//
// Bitmaps are 1:1 with the source image. A page taller than the device's GPU
// texture limit is sent as several pieces (the main thread decides the
// split): each piece is cropped from one shared decode, which is closed once
// every piece has its crop.
//
// Message protocol (main -> worker):
//   {type:'assign',  segmentIndex, globalIdx} -- canvas now shows this page
//   {type:'release', segmentIndex}            -- canvas left the buffer
//   {type:'paint',   segmentIndex, globalIdx, url, iw, sy0, rows} -- source rows [sy0, sy0+rows)
// Worker -> main:
//   {type:'bitmap',      segmentIndex, globalIdx, bitmap} -- transferred
//   {type:'paintFailed', segmentIndex, globalIdx}         -- fetch/decode failed; main may retry

const assignedIdx = new Map();   // segmentIndex -> globalIdx it currently shows (-1 = none)

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

function stillWanted(msg) {
  return assignedIdx.get(msg.segmentIndex) === msg.globalIdx;
}

function send(msg, bitmap) {
  self.postMessage({ type: 'bitmap', segmentIndex: msg.segmentIndex, globalIdx: msg.globalIdx, bitmap }, [bitmap]);
}

function paint(msg) {
  const d = acquireBitmap(msg.url);
  let released = false;  // this job's hold on the shared decode
  const release = (bitmap) => { if (!released) { released = true; releaseBitmap(msg.url, d, bitmap); } };
  d.promise.then(async bitmap => {
    // The canvas can be reassigned to a different page while the decode is in flight.
    if (!stillWanted(msg)) { release(bitmap); return; }
    const whole = msg.sy0 === 0 && msg.rows === bitmap.height && msg.iw === bitmap.width;
    if (whole && d.users === 1) {
      // The only job for this decode: send the bitmap itself. It now
      // belongs to the page, so it must not be closed here.
      released = true;
      d.users = 0;
      decoding.delete(msg.url);
      send(msg, bitmap);
      return;
    }
    let piece;
    try {
      piece = await createImageBitmap(bitmap, 0, msg.sy0, msg.iw, msg.rows);
    } finally {
      release(bitmap);
    }
    if (stillWanted(msg)) send(msg, piece);
    else piece.close();
  }).catch(err => {
    release(null);
    console.error('[canvas buffer worker] paint failed', msg.url, err);
    self.postMessage({ type: 'paintFailed', segmentIndex: msg.segmentIndex, globalIdx: msg.globalIdx });
  });
}

self.onmessage = (e) => {
  const msg = e.data;
  switch (msg.type) {
  case 'assign':
    assignedIdx.set(msg.segmentIndex, msg.globalIdx);
    break;
  case 'release':
    assignedIdx.set(msg.segmentIndex, -1);
    break;
  case 'paint':
    paint(msg);
    break;
  }
};
