/*
 * The device's last 5 minutes, for the reader's debug-log button: POSTed to
 * /api/admin/debug-log, which saves it next to the server's own last 5
 * minutes (debug_log.py) as two txt files on the NAS. Every line is stamped
 * in UTC to the millisecond so the two files line up.
 *
 * Records: console output and uncaught errors, the browser's own network
 * record of every finished request (PerformanceObserver 'resource'), a
 * sample every 2s (main-thread long tasks, timer lag, JS heap, and the
 * Android app's device RAM/CPU when window.KinshoDebug exists), plus
 * whatever the page adds through kinshoDebugLog.log(). The reader also
 * registers a snapshot of its own state, taken at the moment of export.
 *
 * Kept in memory for the current page only -- opening the reader again
 * starts a new log.
 */
(function () {
  const WINDOW_MS = 5 * 60 * 1000;
  const SAMPLE_MS = 2000;
  const lines = [];  // [epoch ms, text]
  let head = 0;      // lines before this are older than WINDOW_MS
  let snapshot = null;
  let sampleExtra = null;

  function log(msg) {
    const now = Date.now();
    lines.push([now, msg]);
    const cutoff = now - WINDOW_MS;
    while (head < lines.length && lines[head][0] < cutoff) head++;
    if (head > 5000) { lines.splice(0, head); head = 0; }
  }

  const iso = (ms) => new Date(ms).toISOString();
  const mb = (n) => Math.round(n / 1048576) + 'MB';
  const shortUrl = (u) => { try { const x = new URL(u, location.href); return x.pathname + x.search; } catch (e) { return u; } };

  function fmt(a) {
    if (typeof a === 'string') return a;
    if (a instanceof Error) return a.stack || String(a);
    try { return JSON.stringify(a); } catch (e) { return String(a); }
  }

  for (const level of ['log', 'info', 'warn', 'error', 'debug']) {
    const orig = console[level];
    console[level] = function (...args) {
      try { log(`[console.${level}] ${args.map(fmt).join(' ')}`); } catch (e) {}
      return orig.apply(this, args);
    };
  }

  // Capture phase also sees failed loads of <img> elements in the page.
  addEventListener('error', (e) => {
    if (e instanceof ErrorEvent) {
      log(`[error] ${e.message} at ${e.filename}:${e.lineno}:${e.colno} ${e.error?.stack || ''}`);
    } else if (e.target?.tagName) {
      log(`[element error] <${e.target.tagName.toLowerCase()}> ${shortUrl(e.target.currentSrc || e.target.src || '')}`);
    }
  }, true);
  addEventListener('unhandledrejection', (e) => log(`[unhandledrejection] ${fmt(e.reason)}`));
  document.addEventListener('visibilitychange', () => log(`[page] ${document.visibilityState}`));
  addEventListener('resize', () => log(`[page] resize ${innerWidth}x${innerHeight}`));

  try {
    performance.setResourceTimingBufferSize(3000);
    performance.addEventListener('resourcetimingbufferfull', () => performance.clearResourceTimings());
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        const cached = e.transferSize === 0 && e.encodedBodySize > 0 ? ' (from cache)' : '';
        log(`[net] ${shortUrl(e.name)} ${e.initiatorType} status=${e.responseStatus ?? '?'} ` +
            `size=${e.encodedBodySize}B${cached} started=${iso(performance.timeOrigin + e.startTime)} ` +
            `ttfb=${Math.round(e.responseStart - e.startTime)}ms total=${Math.round(e.duration)}ms`);
      }
    }).observe({ type: 'resource' });
  } catch (e) {}

  // Main-thread busyness is the closest a page can get to its own CPU use.
  let longCount = 0, longMs = 0;
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) { longCount++; longMs += e.duration; }
    }).observe({ type: 'longtask' });
  } catch (e) {}

  let lastTick = performance.now();
  setInterval(() => {
    const now = performance.now();
    const elapsed = now - lastTick;
    lastTick = now;
    const parts = [
      `main thread busy ${Math.round(100 * longMs / elapsed)}% (${longCount} long tasks, ${Math.round(longMs)}ms), ` +
      `timer lag ${Math.max(0, Math.round(elapsed - SAMPLE_MS))}ms`,
    ];
    longCount = 0; longMs = 0;
    if (performance.memory) {
      parts.push(`js heap ${mb(performance.memory.usedJSHeapSize)} (rounded by the browser)`);
    }
    if (window.KinshoDebug) {
      try { parts.push(window.KinshoDebug.stats()); } catch (e) { parts.push('device stats failed: ' + e); }
    }
    if (sampleExtra) {
      try { parts.push(sampleExtra()); } catch (e) { parts.push('reader stats failed: ' + e); }
    }
    log('[res] ' + parts.join(' | '));
  }, SAMPLE_MS);

  async function exportLog() {
    const now = Date.now();
    let snap;
    try { snap = snapshot ? snapshot() : '(this page has no snapshot)'; }
    catch (e) { snap = 'snapshot failed: ' + (e.stack || e); }
    const cutoff = now - WINDOW_MS;
    const body = lines.slice(head).filter((l) => l[0] >= cutoff).map(([t, m]) => `${iso(t)} ${m}`).join('\n');
    const text =
      `Device: ${navigator.userAgent}\n` +
      `Screen ${screen.width}x${screen.height} dpr ${devicePixelRatio}, viewport ${innerWidth}x${innerHeight}, ` +
      `${navigator.hardwareConcurrency} cores, deviceMemory ${navigator.deviceMemory ?? '?'}GB\n\n` +
      `=== STATE AT EXPORT (${iso(now)}) ===\n${snap}\n\n` +
      `=== LAST 5 MINUTES ===\n${body}\n`;
    const res = await fetch(window.apiUrl('/api/admin/debug-log'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ log: text, page: location.href, sent_at: Date.now() }),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  }

  window.kinshoDebugLog = {
    log,
    exportLog,
    setSnapshot(fn) { snapshot = fn; },
    setSampleExtra(fn) { sampleExtra = fn; },
  };
  log(`[page] opened ${location.href}`);
})();
