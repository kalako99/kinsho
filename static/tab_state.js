// ── LIBRARY TAB STATE: build + save (shared by the library page and the reader) ──
// One library tab's display state (Last Read / Random / Favourites /
// Collections rows, backdrop, first Last Updated page) is built here and
// saved to localStorage, so the library page can show it instantly on the
// next load -- including a cold start of the Android app, which loses
// sessionStorage whenever Android kills it.
//
// Design (2026-09-23): nothing the user can see should change after the
// library page appears.
//  - The shuffled rows (Random, Favourites, Collections) keep their saved
//    picks on every library-page load. They're reshuffled only by the
//    READER, on every reader open, which also re-saves every tab -- so the
//    new picks are already in place the next time the library appears.
//    (No more 1-hour timer; the Random category page's Shuffle button is
//    separate and unaffected.)
//  - The reader also re-saves after opening a chapter (Last Read order,
//    backdrop) and after each completed chapter (progress %), so closing
//    the app straight from the reader still leaves a correct copy.
//  - A library-page refresh (app start, a scan finishing, returning to the
//    app) therefore only changes what genuinely changed elsewhere -- new
//    chapters in the Last Updated grid, or reading done on another device.
//    Rows are keyed by manga id, so Vue only redraws the tiles that differ.
// Plain script (no modules), loaded before app.js / the reader's own script.

const TAB_ROW_SIZE = 20;
const COLLECTION_MEMBERSHIP_KEY = 'kinsho_collection_membership';

function tabUrl(path) {
  return window.apiUrl ? window.apiUrl(path) : path;
}

function tabCacheStorageKey(libraryId) {
  return `kinsho_tab_cache_${libraryId}`;
}

function loadPersistedTabState(libraryId) {
  try {
    const raw = localStorage.getItem(tabCacheStorageKey(libraryId));
    return raw ? JSON.parse(raw) : null;
  } catch (e) {
    return null;
  }
}

function persistTabState(libraryId, state) {
  try {
    localStorage.setItem(tabCacheStorageKey(libraryId), JSON.stringify(state));
  } catch (e) {
    // localStorage unavailable/full -- the next load just fetches fresh.
  }
}

// Library ids that currently have a saved tab copy -- the reader refreshes
// exactly these (the tabs the library page shows).
function persistedTabLibraryIds() {
  const ids = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const m = /^kinsho_tab_cache_(.+)$/.exec(localStorage.key(i) || '');
      if (m) ids.push(m[1]);
    }
  } catch (e) {}
  return ids;
}

function loadPersistedMembership() {
  try {
    return JSON.parse(localStorage.getItem(COLLECTION_MEMBERSHIP_KEY) || 'null') || {};
  } catch (e) {
    return {};
  }
}

async function fetchCollectionMembership() {
  const res = await fetch(tabUrl('/api/collections/membership'));
  const data = await res.json();
  const membership = data.membership || {};
  try { localStorage.setItem(COLLECTION_MEMBERSHIP_KEY, JSON.stringify(membership)); } catch (e) {}
  return membership;
}

// "<name>.<ext>" -> "<name>+.<ext>" -- the large-cover naming convention.
function deriveCoverLarge(coverUrl) {
  if (!coverUrl) return null;
  const lastSlash = coverUrl.lastIndexOf('/');
  const dir = coverUrl.slice(0, lastSlash + 1);
  const filename = coverUrl.slice(lastSlash + 1);
  const dotIdx = filename.lastIndexOf('.');
  if (dotIdx === -1) return null;
  return `${dir}${filename.slice(0, dotIdx)}+${filename.slice(dotIdx)}`;
}

// "X of Y chapters read" -- a plain count of chapters actually marked
// completed (h.completed_count), not the furthest position reached.
function historyProgress(h) {
  return h && h.total_chapters > 0 ? Math.round(h.completed_count / h.total_chapters * 100) : 0;
}

function shuffled(list) {
  return [...list].sort(() => Math.random() - 0.5);
}

// A shuffled row's picks, by id. reshuffle -> a fresh random pick.
// Otherwise the previous picks are kept, in order, minus any that no
// longer exist (a scan removed/renamed them, a favourite was removed);
// candidates the row doesn't include yet fill any free places (e.g. a new
// favourite when there are fewer than 20), so kept tiles never move.
function pickRowIds(prevIds, candidateIds, reshuffle) {
  if (reshuffle || !Array.isArray(prevIds)) return shuffled(candidateIds).slice(0, TAB_ROW_SIZE);
  const candidates = new Set(candidateIds);
  const kept = prevIds.filter(id => candidates.has(id));
  if (kept.length >= TAB_ROW_SIZE) return kept.slice(0, TAB_ROW_SIZE);
  const keptSet = new Set(kept);
  const extra = shuffled(candidateIds.filter(id => !keptSet.has(id)));
  return kept.concat(extra).slice(0, TAB_ROW_SIZE);
}

// Pure: one Last Updated page, joined with reading history for progress.
async function fetchLastUpdatedPage(libraryId, page, historyByMangaId, columns) {
  try {
    const needsHistory = !historyByMangaId;
    const [res, historyRes] = await Promise.all([
      fetch(tabUrl(`/api/mangas/${libraryId}?sort=last_updated&page=${page}&columns=${columns}`)),
      needsHistory ? fetch(tabUrl(`/api/reading/history/${libraryId}`)) : Promise.resolve(null),
    ]);
    const data = await res.json();
    if (needsHistory) {
      const historyData = await historyRes.json();
      historyByMangaId = {};
      for (const entry of (historyData.history || [])) historyByMangaId[entry.manga_id] = entry;
    }
    const mangas = data.mangas.map((m) => {
      const h = historyByMangaId[m.id];
      return {
        id:          m.id,
        title:       m.name,
        path:        m.path,
        cover:       m.cover_url,
        chapters:    m.chapters,
        is_complete: m.is_complete || false,
        is_oneshot:      m.manga_type === 'oneshot',
        last_chapter_id: h ? h.last_chapter_id : null,
        last_page:       h ? h.last_page : 0,
        progress:        historyProgress(h),
      };
    });
    return { mangas, page: data.page, total: data.total, columns };
  } catch (e) {
    console.error('Failed to load last updated:', e);
    return null;
  }
}

// Pure: this library's Collections row (lib= scopes it server-side),
// ordered by the kept/reshuffled picks.
async function fetchCollectionsRow(libraryId, prevIds, reshuffle) {
  try {
    const res  = await fetch(tabUrl(`/api/collections?lib=${libraryId}`));
    const data = await res.json();
    const byId = new Map((data.collections || []).map(c => [c.id, c]));
    const ids = pickRowIds(prevIds, [...byId.keys()], reshuffle);
    return ids.map(id => byId.get(id)).map(c => ({
      id:          c.id,
      title:       c.name,
      cover:       c.cover_url,
      is_complete: false,
    }));
  } catch (e) {
    console.error('Failed to load collections row:', e);
    return [];
  }
}

// Pure: fetches + computes one tab's full state. Never touches any page's
// live display -- callers decide what to apply/persist.
//   opts.prev             previous state for this tab (keeps the shuffled rows' picks)
//   opts.reshuffle        true only from the reader
//   opts.columns          Last Updated grid column count the page request is aligned to
//   opts.settingsPromise  shared /api/settings fetch for a batch
//   opts.onCoreReady      called with the rows+backdrop as soon as they're computed
//   opts.afterCore/restGate  round-robin hooks for the library page's background loads
async function buildTabState(libraryId, lastUpdatedPage = 1, opts = {}) {
  let coreSignalled = false;
  const signalCore = () => { if (!coreSignalled) { coreSignalled = true; opts.afterCore?.(); } };
  const prev = opts.prev || null;
  try {
    const [allRes, settings, historyRes] = await Promise.all([
      fetch(tabUrl(`/api/mangas/${libraryId}?sort=alphabetical`)),
      opts.settingsPromise || fetch(tabUrl('/api/settings')).then(r => r.json()),
      fetch(tabUrl(`/api/reading/history/${libraryId}`)),
    ]);
    const allData  = await allRes.json();
    const historyData = await historyRes.json();

    const favouriteIds = new Set(
      (settings.favourites || [])
        .filter(f => String(f.library_id) === String(libraryId))
        .map(f => f.manga_id)
    );

    const historyByMangaId = {};
    for (const entry of (historyData.history || [])) historyByMangaId[entry.manga_id] = entry;

    const mangas = allData.mangas.map((m) => {
      const h = historyByMangaId[m.id];
      return {
        id:          m.id,
        title:       m.name,
        path:        m.path,
        cover:       m.cover_url,
        // Prefer the server's own cover_url_large (carries that exact
        // file's own cache-busting version) over deriving one.
        coverLarge:  m.cover_url_large || deriveCoverLarge(m.cover_url),
        chapters:    m.chapters,
        is_complete: m.is_complete || false,
        is_case2:    m.manga_type === 'case2',
        // Flat-scan oneshots skip manga_detail.html entirely, so these are
        // what let the tile's context menu offer a real "Continue Reading".
        is_oneshot:      m.manga_type === 'oneshot',
        last_chapter_id: h ? h.last_chapter_id : null,
        last_page:       h ? h.last_page : 0,
        is_favourite: favouriteIds.has(m.id),
        progress:     historyProgress(h),
      };
    });
    const mangaById = Object.fromEntries(mangas.map(m => [m.id, m]));

    // Last Read: history is already sorted by last_read desc.
    const lastRead = (historyData.history || [])
      .slice(0, TAB_ROW_SIZE)
      .map(h => mangaById[h.manga_id])
      .filter(Boolean);

    const randomIds = pickRowIds(prev && prev.randomIds, mangas.map(m => m.id), opts.reshuffle);
    const favouriteRowIds = pickRowIds(prev && prev.favouriteIds, mangas.filter(m => favouriteIds.has(m.id)).map(m => m.id), opts.reshuffle);
    const random     = randomIds.map(id => mangaById[id]);
    const favourites = favouriteRowIds.map(id => mangaById[id]);

    // Ambient blurred background from the most recently read manga,
    // falling back to the first manga (natural sort) in this library.
    let bgManga = null;
    if (lastRead.length > 0 && lastRead[0].coverLarge) {
      bgManga = lastRead[0];
    } else if (mangas.length > 0) {
      bgManga = [...mangas].sort((a, b) =>
        a.title.localeCompare(b.title, undefined, { numeric: true, sensitivity: 'base' })
      )[0];
    }
    const backdropEnabled = settings.backdrop_list !== false;
    const lockBackdrop = settings.lock_backdrop === true;
    let bgLayerStyle = null, bgIsRaster = false, bgUrlToLock = null;
    if (lockBackdrop && settings.locked_backdrop_url) {
      bgLayerStyle = { backgroundImage: `url('${settings.locked_backdrop_url}')` };
      bgIsRaster = true;
    } else if (backdropEnabled && bgManga && bgManga.coverLarge) {
      bgLayerStyle = { backgroundImage: `url('${bgManga.coverLarge}')` };
      bgIsRaster = true;
      if (lockBackdrop) bgUrlToLock = bgManga.coverLarge;
    }

    opts.onCoreReady?.({ lastRead, random, favourites, bgLayerStyle, bgIsRaster, bgUrlToLock });
    signalCore();
    if (opts.restGate) await opts.restGate;

    // Last Updated + Collections, fetched together (independent).
    const showCollections = settings.show_collections_row !== false;
    const [lu, collectionsRow] = await Promise.all([
      fetchLastUpdatedPage(libraryId, lastUpdatedPage, historyByMangaId, opts.columns || 1),
      showCollections
        ? fetchCollectionsRow(libraryId, prev && prev.collectionIds, opts.reshuffle)
        : Promise.resolve([]),
    ]);

    return {
      lastRead, random, favourites, collectionsRow,
      randomIds, favouriteIds: favouriteRowIds, collectionIds: collectionsRow.map(c => c.id),
      bgLayerStyle, bgIsRaster, bgUrlToLock,
      lastUpdated:        lu ? lu.mangas  : [],
      lastUpdatedPage:    lu ? lu.page    : 1,
      lastUpdatedTotal:   lu ? lu.total   : 0,
      lastUpdatedColumns: lu ? lu.columns : (opts.columns || 1),
    };
  } catch (e) {
    console.error('Failed to load mangas:', e);
    signalCore();  // a failed tab must not hold up the others' round-robin
    return null;
  }
}

// Reader side, instant and offline: the manga just opened moves to the
// front of this library's saved Last Read row (and the backdrop follows it
// if it's the kind that follows Last Read), so the saved copy is right even
// if the app is closed before refreshSavedTabs() below gets to run.
function promoteInSavedLastRead(libraryId, mangaId) {
  const state = loadPersistedTabState(libraryId);
  if (!state || !Array.isArray(state.lastRead)) return;
  if (state.lastRead[0] && state.lastRead[0].id === mangaId) return;
  const rows = [state.lastRead, state.random, state.favourites, state.lastUpdated];
  let tile = null;
  for (const row of rows) {
    tile = (row || []).find(m => m && m.id === mangaId);
    if (tile) break;
  }
  if (!tile) return;  // not in any saved row -- refreshSavedTabs() will fetch it
  const oldFirst = state.lastRead[0];
  const followsLastRead = state.bgIsRaster && oldFirst && oldFirst.coverLarge &&
    state.bgLayerStyle && state.bgLayerStyle.backgroundImage === `url('${oldFirst.coverLarge}')`;
  state.lastRead = [tile, ...state.lastRead.filter(m => m.id !== mangaId)].slice(0, TAB_ROW_SIZE);
  if (followsLastRead && tile.coverLarge) {
    state.bgLayerStyle = { backgroundImage: `url('${tile.coverLarge}')` };
  }
  persistTabState(libraryId, state);
}

// Reader side: rebuild + save saved tab copies without any library page
// open. reshuffle=true on reader open (every saved tab), false after a
// completed chapter (only the library being read -- progress % changed).
async function refreshSavedTabs(libraryIds, reshuffle) {
  const settingsPromise = fetch(tabUrl('/api/settings')).then(r => r.json());
  await Promise.all(libraryIds.map(async (libraryId) => {
    const prev = loadPersistedTabState(libraryId);
    if (!prev) return;
    const state = await buildTabState(libraryId, prev.lastUpdatedPage || 1, {
      prev, reshuffle, settingsPromise, columns: prev.lastUpdatedColumns || 1,
    });
    // A lock-backdrop capture is the library page's job (it's about what
    // was actually on screen) -- never persisted from here.
    if (state) persistTabState(libraryId, { ...state, bgUrlToLock: null });
  }));
  try { await fetchCollectionMembership(); } catch (e) {}
}
