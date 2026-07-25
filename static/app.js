const { createApp, defineComponent } = Vue;

// ── MANGA THUMBNAIL COMPONENT ──
const MangaThumb = defineComponent({
  name: 'MangaThumb',
  props: { manga: { type: Object, required: true } },
  data() { return { startPos: 0 }; },
  methods: {
    handleClick(e) {
      if (Math.abs(e.pageX - this.startPos) < 5) this.$emit('click');
    }
  },
  template: `
    <div class="manga-thumb"
      @mousedown="startPos = $event.pageX"
      @click="handleClick($event)">
      <div class="cover">
        <img v-if="manga.cover" :src="manga.cover" :alt="manga.title">
        <span v-else>No Cover</span>
        <span v-if="manga.is_complete" class="complete-badge">COMPLETE</span>
      </div>
      <div class="card-body">
        <div class="progress-wrap">
          <div class="progress-track">
            <div class="progress-bar" :style="{ width: (manga.progress || 0) + '%' }"></div>
          </div>
          <span class="progress-pct" v-if="manga.progress">{{ manga.progress }}%</span>
          <span class="progress-pct" v-else style="color: transparent">0%</span>
        </div>
        <div class="thumb-title">{{ manga.title }}</div>
      </div>
    </div>
  `
});

// ── DRAG TO SCROLL DIRECTIVE ──
// Handles horizontal drag-scroll on rows
const vDragScroll = {
  mounted(el) {
    let isDown = false;
    let startX, scrollLeft;

    el.addEventListener('dragstart', (e) => { e.preventDefault(); });

    el.addEventListener('mousedown', (e) => {
      e.preventDefault();
      isDown = true;
      el.classList.add('dragging');
      startX = e.pageX - el.offsetLeft;
      scrollLeft = el.scrollLeft;
    });
    document.addEventListener('mouseup', () => { isDown = false; el.classList.remove('dragging'); });
    el.addEventListener('mousemove', (e) => {
      if (!isDown) return;
      e.preventDefault();
      el.scrollLeft = scrollLeft - (e.pageX - el.offsetLeft - startX) * 1.5;
    });
  }
};

// ── LAST-UPDATED ROW-COMPLETION HELPERS ──
// .manga-grid's column count comes entirely from CSS
// (repeat(auto-fill, minmax(...))), driven by the live viewport width --
// there is no fixed number to hardcode, and it changes with screen size
// and orientation. Reading the resolved grid-template-columns gives the
// exact count the browser is actually rendering right now: one length
// value per column, regardless of how many (or how few) items currently
// occupy the grid.
function currentGridColumns() {
  const grid = document.querySelector('.manga-grid');
  if (!grid) return 1;
  const cols = getComputedStyle(grid).gridTemplateColumns.split(' ').filter(Boolean).length;
  return cols || 1;
}

// Rounds n UP to the next multiple of `multiple` (never down -- the goal
// is finishing a dangling row with a few more items, not cutting content
// that would otherwise have been shown).
function roundUpToMultiple(n, multiple) {
  if (!multiple || multiple <= 1) return n;
  const remainder = n % multiple;
  return remainder === 0 ? n : n + (multiple - remainder);
}

// ── RANDOM / FAVOURITES ROWS: STABLE FOR UP TO AN HOUR ──
// Both rows were reshuffled on every single mount() (every page
// navigation, since this is a traditional multi-page app -- Vue instance
// torn down and rebuilt each time) and on every visibilitychange back to
// visible (switching back to the app without navigating anywhere at
// all), which made them feel like noise rather than a stable pick.
// Capped to reshuffling at most once per hour per library instead,
// cached in localStorage (not a plain JS field, which wouldn't survive a
// page reload) as {ids, ts} -- only the ids persist, not full manga
// objects, so a cached pick's cover/progress/name always come from the
// current load, never stale, even though which manga are picked stays
// fixed. `rowName` keeps random and favourites in separate cache slots
// (different candidate pools, independent hour timers).
const STABLE_ROW_CACHE_MS = 60 * 60 * 1000;

function pickStableRow(rowName, libraryId, mangas) {
  const key = `kinsho_${rowName}_row_${libraryId}`;
  let cached = null;
  try {
    cached = JSON.parse(localStorage.getItem(key) || 'null');
  } catch (e) {
    cached = null;
  }
  const isFresh = cached && (Date.now() - cached.ts) < STABLE_ROW_CACHE_MS;
  const mangaById = new Map(mangas.map(m => [m.id, m]));
  const rehydrated = isFresh ? cached.ids.map(id => mangaById.get(id)).filter(Boolean) : [];
  // Re-picks immediately if none of the cached ids still resolve (a
  // rescan removed/renamed manga, or a favourite was removed) rather
  // than showing an empty row for up to an hour; a partial match is fine
  // as-is -- the row just shows fewer than 20 until the next reshuffle.
  if (isFresh && rehydrated.length > 0) {
    return rehydrated;
  }

  const picked = [...mangas].sort(() => Math.random() - 0.5).slice(0, 20);
  try {
    localStorage.setItem(key, JSON.stringify({ ids: picked.map(m => m.id), ts: Date.now() }));
  } catch (e) {
    // localStorage unavailable/full -- fine to skip persisting, the row
    // still renders from `picked` for this one load.
  }
  return picked;
}

// ── MANGA LIST SCROLL MEMORY ──
// Remembered across a normal "click into a manga, hit back" round trip,
// but only until the chapter reader is actually opened -- chapter_reader.html
// clears this same key on its own load (see its comment there), since a
// scroll position from before you started reading isn't meaningful to
// restore anymore once you have, even after navigating all the way back
// through browser history later.
const MANGA_LIST_SCROLL_KEY = 'kinsho_manga_list_scroll';

// ── MAIN APP ──
const app = createApp({
  components: { MangaThumb },
  directives: { dragScroll: vDragScroll },

  data() {
    return {
      // ── TABS — loaded from backend via window.__LIBRARIES__ ──
      tabs: window.__LIBRARIES__ || [],
      activeTab: (() => {
        const libs = window.__LIBRARIES__ || [];
        if (libs.length === 0) return null;
        const last = window.__LAST_TAB__;
        const found = libs.find(l => l.id === last);
        return found ? found.id : libs[0].id;
      })(),

      // ── TRACKS WHETHER EACH ROW IS SCROLLED TO THE END ──
      atEnd: { lastRead: false, random: false, favourites: false, collections: false },

      // ── ADMIN: INTEGRITY ISSUE BADGE ──
      isAdmin:             false,
      integrityIssueCount: 0,

      // ── ROW DATA ──
      lastRead:    [],
      random:      [],
      favourites:  [],
      collectionsRow:       [],
      showCollectionsRow:   true,
      collectionMembership: {},

      // ── GRID DATA + PAGINATION ──
      lastUpdated:      [],
      lastUpdatedPage:  1,
      lastUpdatedTotal: 0,
      // Column count the most recent loadLastUpdated() fetch was aligned
      // to -- lastUpdatedTotalPages needs the same value the server used
      // to compute per-page counts, or its page-button count would drift
      // from what's actually being served.
      lastUpdatedColumns: 1,
      activeTheme: null,
      bgLayerStyle: null,
      bgIsRaster: false,

      // ── PER-LIBRARY TAB CACHE ──
      // library_id -> the full computed display state buildTabState()
      // returns for it (lastRead/random/favourites/lastUpdated/backdrop).
      // Populated for the active tab on mount, then for every OTHER
      // library in the background (see mounted()) so switching tabs is an
      // instant local read instead of a fresh round-trip each time.
      tabCache: {},
    };
  },

  computed: {
    lastUpdatedTotalPages() {
      const perPage1 = roundUpToMultiple(50, this.lastUpdatedColumns);
      const perPageN = roundUpToMultiple(100, this.lastUpdatedColumns);
      if (this.lastUpdatedTotal <= perPage1) return 1;
      return 1 + Math.ceil((this.lastUpdatedTotal - perPage1) / perPageN);
    },
  },

  async mounted() {
    await this.loadTheme();
    if (this.activeTab !== null) {
      await this.loadMangas(this.activeTab);
      // Warm every other library's tab cache in the background, whether
      // the user ever visits it this session or not, so switching tabs
      // later is an instant local read instead of a fresh round-trip.
      // Deliberately not awaited (and started only after the active tab's
      // own load above has finished) so this never delays first paint or
      // competes with it for the browser's connection pool.
      for (const tab of this.tabs) {
        if (tab.id !== this.activeTab) {
          this.loadMangas(tab.id);
        }
      }
    }
    await this.loadCollectionsRow();
    await this.loadIntegrityBadge();

    // Restore the scroll position from before navigating away, if the
    // chapter reader hasn't been visited since (see MANGA_LIST_SCROLL_KEY).
    // After $nextTick so the just-loaded rows/grid have actually flushed
    // to the DOM -- restoring against the page's pre-content height would
    // just clamp to whatever's currently the max scroll, usually near zero.
    const savedY = sessionStorage.getItem(MANGA_LIST_SCROLL_KEY);
    if (savedY !== null) {
      await this.$nextTick();
      window.scrollTo(0, parseInt(savedY, 10) || 0);
    }

    // Keeps the saved position continuously up to date while scrolling,
    // so whatever the very last position was before navigating away (a
    // manga tile click, the back button, anything) is already captured --
    // no reliance on a single beforeunload/pagehide event firing reliably
    // right at the moment of navigation, which is inconsistent across
    // mobile WebViews in particular.
    let scrollSaveScheduled = false;
    window.addEventListener('scroll', () => {
      if (scrollSaveScheduled) return;
      scrollSaveScheduled = true;
      requestAnimationFrame(() => {
        sessionStorage.setItem(MANGA_LIST_SCROLL_KEY, String(window.scrollY));
        scrollSaveScheduled = false;
      });
    }, { passive: true });

    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && this.activeTab !== null) {
        this.loadMangas(this.activeTab);
      }
    });
  },

  methods: {
    async loadIntegrityBadge() {
      try {
        const meRes  = await fetch(apiUrl('/api/auth/me'));
        const meData = await meRes.json();
        this.isAdmin = meData.ok && meData.role === 'admin';
        if (!this.isAdmin) return;
        const res  = await fetch(apiUrl('/api/admin/integrity/issues'));
        const data = await res.json();
        this.integrityIssueCount = data.count || 0;
      } catch (e) {
        this.isAdmin = false;
      }
    },

    // ── LOAD COLLECTIONS ROW ──
    async loadCollectionsRow() {
      try {
        const settingsRes = await fetch(apiUrl('/api/settings'));
        const settings = await settingsRes.json();
        this.showCollectionsRow = settings.show_collections_row !== false;
      } catch (e) {
        this.showCollectionsRow = true;
      }
      try {
        const res  = await fetch(apiUrl('/api/collections'));
        const data = await res.json();
        this.collectionsRow = this.showCollectionsRow
          ? (data.collections || []).slice().sort(() => Math.random() - 0.5).slice(0, 20).map(c => ({
              id:          c.id,
              title:       c.name,
              cover:       c.cover_url,
              is_complete: false,
              progress:    0,
            }))
          : [];
      } catch (e) {
        console.error('Failed to load collections row:', e);
        this.collectionsRow = [];
      }
      await this.loadCollectionMembership();
    },

    async loadCollectionMembership() {
      try {
        const res  = await fetch(apiUrl('/api/collections/membership'));
        const data = await res.json();
        this.collectionMembership = data.membership || {};
      } catch (e) {
        this.collectionMembership = {};
      }
    },

    openCollection(id) { window.location.href = `/collection/${id}`; },
    goToCollections()  { window.location.href = '/collections'; },

    // ── LOAD MANGAS FOR A TAB, CACHE IT, APPLY IT IF IT'S THE ACTIVE ONE ──
    // Called both for the active tab (mounted()/switchTab()'s cache-miss
    // fallback/visibilitychange) and for every other library in the
    // background (mounted()'s prefetch loop) -- buildTabState() never
    // touches live display state itself, so a background call for a tab
    // the user isn't looking at can't clobber what's currently on screen.
    async loadMangas(libraryId) {
      const state = await this.buildTabState(libraryId);
      if (!state) return;
      this.tabCache[libraryId] = state;
      if (libraryId === this.activeTab) {
        this.applyTabState(state);
        if (state.bgUrlToLock) {
          // First load with the lock-backdrop setting on and nothing
          // captured yet — lock in whatever's showing right now so it
          // persists from here on. Only done for the tab actually being
          // displayed, never for a background prefetch of a different one.
          this.persistLockedBackdrop(state.bgUrlToLock);
        }
      }
    },

    // Pure: fetches + computes everything one tab's view needs, without
    // touching `this.*` — safe to run for a library the user isn't
    // currently looking at (background prefetch) as well as the active one.
    async buildTabState(libraryId) {
      try {
        const [allRes, settingsRes, historyRes] = await Promise.all([
          fetch(`/api/mangas/${libraryId}?sort=alphabetical`),
          fetch('/api/settings'),
          fetch(`/api/reading/history/${libraryId}`),
        ]);
        const allData  = await allRes.json();
        const settings = await settingsRes.json();
        const historyData = await historyRes.json();

        const favouriteIds = new Set(
          (settings.favourites || [])
            .filter(f => f.library_id === libraryId)
            .map(f => f.manga_id)
        );

        // Build history lookup keyed by manga_id
        const historyByMangaId = {};
        for (const entry of (historyData.history || [])) {
          historyByMangaId[entry.manga_id] = entry;
        }

        const mangas = allData.mangas.map((m) => {
          const h = historyByMangaId[m.id];
          const progress = h && h.total_chapters > 0
            ? Math.round(h.furthest_chapter_idx / h.total_chapters * 100)
            : 0;
          return {
            id:          m.id,
            title:       m.name,
            path:        m.path,
            cover:       m.cover_url,
            coverLarge:  this.deriveCoverLarge(m.cover_url),
            chapters:    m.chapters,
            is_complete: m.is_complete || false,
            is_case2:    m.manga_type === 'case2',
            progress,
          };
        });

        const mangaById = Object.fromEntries(mangas.map(m => [m.id, m]));

        // Last Read: join history (already sorted by last_read desc) with mangas
        const lastRead = (historyData.history || [])
          .slice(0, 20)
          .map(h => mangaById[h.manga_id])
          .filter(Boolean);

        const random     = pickStableRow('random', libraryId, mangas);
        const favourites = pickStableRow('favourites', libraryId, mangas.filter(m => favouriteIds.has(m.id)));

        // Ambient blurred background from the most recently read manga,
        // falling back to the first manga (natural sort) in this library
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
          // A backdrop was already locked in (persisted server-side, so this
          // survives full page reloads, not just this Vue instance's lifetime).
          bgLayerStyle = { backgroundImage: `url('${settings.locked_backdrop_url}')` };
          bgIsRaster = true;
        } else if (backdropEnabled && bgManga && bgManga.coverLarge) {
          bgLayerStyle = { backgroundImage: `url('${bgManga.coverLarge}')` };
          bgIsRaster = true;
          if (lockBackdrop) bgUrlToLock = bgManga.coverLarge;
        }

        // Last Updated page 1, separately (sorted + paginated), reusing history
        const lu = await this.fetchLastUpdatedPage(libraryId, 1, historyByMangaId);

        return {
          lastRead, random, favourites,
          bgLayerStyle, bgIsRaster, bgUrlToLock,
          lastUpdated:        lu ? lu.mangas  : [],
          lastUpdatedPage:    lu ? lu.page    : 1,
          lastUpdatedTotal:   lu ? lu.total   : 0,
          lastUpdatedColumns: lu ? lu.columns : 1,
        };
      } catch (e) {
        console.error('Failed to load mangas:', e);
        return null;
      }
    },

    // Copies a buildTabState() result onto the live display fields —
    // separate from loadMangas() so switchTab() can apply an
    // already-cached state synchronously, with no fetch at all.
    applyTabState(state) {
      this.lastRead          = state.lastRead;
      this.random             = state.random;
      this.favourites         = state.favourites;
      this.bgLayerStyle       = state.bgLayerStyle;
      this.bgIsRaster         = state.bgIsRaster;
      this.lastUpdated        = state.lastUpdated;
      this.lastUpdatedPage    = state.lastUpdatedPage;
      this.lastUpdatedTotal   = state.lastUpdatedTotal;
      this.lastUpdatedColumns = state.lastUpdatedColumns;
    },

    async persistLockedBackdrop(url) {
      try {
        await fetch(apiUrl('/api/settings/backdrop'), {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ locked_backdrop_url: url }),
        });
      } catch (e) {
        console.error('Failed to persist locked backdrop:', e);
      }
    },

    // ── LOAD LAST UPDATED PAGE (active-tab pagination buttons call this directly) ──
    async loadLastUpdated(libraryId, page, historyByMangaId) {
      const result = await this.fetchLastUpdatedPage(libraryId, page, historyByMangaId);
      if (!result) return;
      this.lastUpdated        = result.mangas;
      this.lastUpdatedPage    = result.page;
      this.lastUpdatedTotal   = result.total;
      this.lastUpdatedColumns = result.columns;
    },

    // Pure: fetches + computes one Last Updated page without touching
    // `this.*` — used by both loadLastUpdated() above (which applies the
    // result live, for the active tab's own pagination) and
    // buildTabState() (which caches it, possibly for a tab that isn't
    // currently on screen).
    async fetchLastUpdatedPage(libraryId, page, historyByMangaId) {
      try {
        const columns = currentGridColumns();
        const needsHistory = !historyByMangaId;
        const [res, historyRes] = await Promise.all([
          fetch(`/api/mangas/${libraryId}?sort=last_updated&page=${page}&columns=${columns}`),
          needsHistory ? fetch(`/api/reading/history/${libraryId}`) : Promise.resolve(null),
        ]);
        const data = await res.json();

        if (needsHistory) {
          const historyData = await historyRes.json();
          historyByMangaId = {};
          for (const entry of (historyData.history || [])) {
            historyByMangaId[entry.manga_id] = entry;
          }
        }

        const mangas = data.mangas.map((m) => {
          const h = historyByMangaId[m.id];
          const progress = h && h.total_chapters > 0
            ? Math.round(h.furthest_chapter_idx / h.total_chapters * 100)
            : 0;
          return {
            id:          m.id,
            title:       m.name,
            path:        m.path,
            cover:       m.cover_url,
            chapters:    m.chapters,
            is_complete: m.is_complete || false,
            progress,
          };
        });
        return { mangas, page: data.page, total: data.total, columns };
      } catch (e) {
        console.error('Failed to load last updated:', e);
        return null;
      }
    },

    async loadTheme() {
      const BUILTIN_THEMES = [
        { name: 'Midnight Red',  primary: '#e94560', secondary: '#1d1113', background: '#120b0d', text: '#f0f0f0' },
        { name: 'Ocean Deep',    primary: '#38bdf8', secondary: '#0d1e2e', background: '#060f1c', text: '#e2f0fb' },
        { name: 'Forest Ink',    primary: '#4ade80', secondary: '#141d16', background: '#0b130d', text: '#e6f4ea' },
        { name: 'Amber Noir',    primary: '#f59e0b', secondary: '#1c1608', background: '#0f0c07', text: '#fdf3dc' },
        { name: 'Royal Dusk',    primary: '#a78bfa', secondary: '#1a1228', background: '#0e0a1a', text: '#ede9fe' },
      ];
      try {
        const res = await fetch('/api/settings');
        const data = await res.json();
        const activeName = data.active_theme || 'Midnight Red';
        const theme = BUILTIN_THEMES.find(t => t.name === activeName) || BUILTIN_THEMES[0];

        const root = document.documentElement;
        root.style.setProperty('--color-primary',    theme.primary);
        root.style.setProperty('--color-secondary',  theme.secondary);
        root.style.setProperty('--color-background', theme.background);
        root.style.setProperty('--color-text',       theme.text);
        document.body.style.background = theme.background;
        this.bgLayerStyle = null;
        this.bgIsRaster = false;
      } catch (e) {
        console.error('Failed to load theme:', e);
      }
    },

    openManga(id) {
      const cid = this.collectionMembership[`${this.activeTab}:${id}`];
      window.location.href = cid ? `/collection/${cid}` : `/manga/${this.activeTab}/${id}`;
    },

    goMore(section) {
      const map = {
        'last-read':  'last-read',
        'random':     'random',
        'favourites': 'favourites',
      };
      const category = map[section];
      if (!category) return;

      // Hands the category page the exact row it was opened from, in the
      // same order, so the items you land on top of are the ones you
      // just saw -- not a second, independently-computed selection (the
      // category page's own random seed/favourites shuffle previously had
      // no relationship to what this row happened to be showing).
      // sessionStorage, not a URL param, since it's a one-shot handoff
      // between two page loads, not app state worth bookmarking/sharing.
      const rowByCategory = { 'last-read': this.lastRead, 'random': this.random, 'favourites': this.favourites };
      const rowIds = (rowByCategory[category] || []).map(m => m.id);
      try {
        sessionStorage.setItem(`kinsho_category_pinned_${this.activeTab}_${category}`, JSON.stringify(rowIds));
      } catch (e) {
        // sessionStorage unavailable -- the category page just falls back
        // to its own normal ordering, same as before this feature existed.
      }
      window.location.href = `/manga/${this.activeTab}/category/${category}`;
    },

    openSettings()   { window.location.href = '/settings'; },

    switchTab(id) {
      this.activeTab = id;
      const cached = this.tabCache[id];
      if (cached) {
        // Already warmed by mounted()'s background prefetch (or a previous
        // visit this session) -- apply instantly, no fetch at all.
        this.applyTabState(cached);
        if (cached.bgUrlToLock) this.persistLockedBackdrop(cached.bgUrlToLock);
      } else {
        // Not ready yet (prefetch still in flight, or this library was
        // added after the page loaded) -- same fetch-then-render fallback
        // as before this cache existed.
        this.lastUpdatedPage  = 1;
        this.lastUpdatedTotal = 0;
        this.loadMangas(id);
      }
      fetch('/api/settings/last-tab', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ last_tab: id }),
      });
    },

    onTabDragStart(e, id) {
      this._dragTabId = id;
      // Delay adding the class so the browser snapshot doesn't show faded tab
      requestAnimationFrame(() => {
        const el = e.target;
        if (el) el.classList.add('dragging-source');
      });
      e.dataTransfer.effectAllowed = 'move';
    },

    onTabDragOver(e, id) {
      if (id === this._dragTabId) return;
      // Highlight the tab we're hovering over
      document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('drag-over'));
      e.target.closest('.tab-btn')?.classList.add('drag-over');
    },

    onTabDragLeave(e) {
      e.target.closest('.tab-btn')?.classList.remove('drag-over');
    },

    onTabDrop(e, targetId) {
      if (targetId === this._dragTabId) return;
      const fromIdx = this.tabs.findIndex(t => t.id === this._dragTabId);
      const toIdx   = this.tabs.findIndex(t => t.id === targetId);
      if (fromIdx === -1 || toIdx === -1) return;
      const reordered = [...this.tabs];
      const [moved] = reordered.splice(fromIdx, 1);
      reordered.splice(toIdx, 0, moved);
      this.tabs = reordered;
      fetch('/api/settings/tab-order', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tab_order: reordered.map(t => t.id) }),
      });
    },

    onTabDragEnd() {
      this._dragTabId = null;
      document.querySelectorAll('.tab-btn').forEach(el => {
        el.classList.remove('dragging-source');
        el.classList.remove('drag-over');
      });
    },
    openSearch()     { window.location.href = `/search?lib=${this.activeTab}`; },

    // Shows the View More button when the row is scrolled near the end
    onRowScroll(e, key) {
      const el = e.target;
      this.atEnd[key] = el.scrollLeft + el.clientWidth >= el.scrollWidth - 20;
    },

    // Derives the large cover URL from the thumbnail cover URL,
    // following the convention: "<name>.<ext>" -> "<name>+.<ext>"
    deriveCoverLarge(coverUrl) {
      if (!coverUrl) return null;
      const lastSlash = coverUrl.lastIndexOf('/');
      const dir = coverUrl.slice(0, lastSlash + 1);
      const filename = coverUrl.slice(lastSlash + 1);
      const dotIdx = filename.lastIndexOf('.');
      if (dotIdx === -1) return null;
      const name = filename.slice(0, dotIdx);
      const ext = filename.slice(dotIdx);
      return `${dir}${name}+${ext}`;
    },
  }
});
app.config.errorHandler = (err, vm, info) => { console.error('Vue error:', err, info); };
app.mount('#app');

