const { createApp, defineComponent } = Vue;

// ── LONG-PRESS-TO-CONTEXT-MENU TUNING ──
// Same hold-to-arm / jitter-to-cancel shape already established for the
// collection member drag-to-reorder gesture (collection_detail.html) --
// re-implemented on raw touch events rather than relying on the browser's
// own long-press-synthesizes-contextmenu behavior, which isn't consistent
// across WebViews/browsers. A plain scroll past a row and the start of an
// intended long-press both begin as a touchstart followed by (eventually)
// movement, so arming immediately on touchstart would make ordinary
// scrolling indistinguishable from opening the menu.
const CTX_MENU_HOLD_MS   = 450;
const CTX_MENU_JITTER_PX = 10;

// ── MANGA THUMBNAIL COMPONENT ──
// Click-vs-drag distinction for a thumb inside a drag-scrollable .manga-row
// lives in vDragScroll below (row-level, capture-phase click suppression) --
// this component just emits click plainly and trusts that a real drag never
// reaches here at all.
const MangaThumb = defineComponent({
  name: 'MangaThumb',
  props: {
    manga: { type: Object, required: true },
    // Collections in the Collections row have no meaningful reading
    // progress of their own -- pass :show-progress="false" there to drop
    // the bar entirely rather than render an always-empty one.
    showProgress: { type: Boolean, default: true },
  },
  emits: ['click', 'contextmenu'],
  methods: {
    onTouchStart(e) {
      const t = e.touches[0];
      this._ctxStartX = t.clientX;
      this._ctxStartY = t.clientY;
      this._ctxFired = false;
      clearTimeout(this._ctxTimer);
      this._ctxTimer = setTimeout(() => {
        this._ctxFired = true;
        this.$emit('contextmenu');
      }, CTX_MENU_HOLD_MS);
    },
    onTouchMove(e) {
      const t = e.touches[0];
      const dx = t.clientX - this._ctxStartX;
      const dy = t.clientY - this._ctxStartY;
      if (Math.hypot(dx, dy) > CTX_MENU_JITTER_PX) clearTimeout(this._ctxTimer);
    },
    onTouchEnd(e) {
      clearTimeout(this._ctxTimer);
      if (this._ctxFired) {
        // Suppresses the emulated click mobile browsers fire after a touch
        // sequence ends, so a long-press that opened the menu doesn't also
        // navigate into the manga's own page the way a plain tap would.
        e.preventDefault();
        this._ctxFired = false;
      }
    },
    onContextMenu(e) {
      e.preventDefault();
      this.$emit('contextmenu');
    },
  },
  template: `
    <div class="manga-thumb"
      @click="$emit('click')"
      @contextmenu="onContextMenu"
      @touchstart="onTouchStart"
      @touchmove="onTouchMove"
      @touchend="onTouchEnd"
      @touchcancel="onTouchEnd"
    >
      <div class="cover">
        <img v-if="manga.cover" :src="manga.cover" :alt="manga.title">
        <span v-else>No Cover</span>
        <span v-if="manga.is_complete" class="complete-badge">COMPLETE</span>
      </div>
      <div class="card-body">
        <div class="progress-wrap" v-if="showProgress">
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
// Handles horizontal drag-scroll on rows. Also owns the click-vs-drag
// distinction for every .manga-thumb inside the row: a mousedown/mouseup
// pair whose cursor position matches closely enough is a click (was
// previously checked per-thumb, comparing the *click* event's own pageX
// against the pageX recorded on that thumb's own mousedown -- fragile,
// since mousedown and mouseup landing on two *different* thumbs during a
// drag resolves the click's target to their nearest common ancestor
// instead of either thumb, so it never reached either thumb's own
// listener and the check silently never ran; a drag that happened to
// start and end back over the *same* thumb, or moved the thumb under a
// near-stationary cursor via the drag's 1.5x scroll multiplier below,
// could still read as "close enough" and wrongly navigate). Tracking
// cumulative movement here instead and suppressing the click in the
// capture phase (fires before it reaches any child .manga-thumb's own
// bubble-phase @click) reliably blocks it regardless of which element(s)
// the mousedown/mouseup actually landed on.
const vDragScroll = {
  mounted(el) {
    let isDown = false;
    let startX, scrollLeft;
    let moved = 0;                // max cumulative |displacement| this gesture
    const CLICK_DRAG_THRESHOLD = 5;  // px -- above this, suppress the next click

    el.addEventListener('dragstart', (e) => { e.preventDefault(); });

    el.addEventListener('mousedown', (e) => {
      e.preventDefault();
      isDown = true;
      moved = 0;
      el.classList.add('dragging');
      startX = e.pageX - el.offsetLeft;
      scrollLeft = el.scrollLeft;
    });
    document.addEventListener('mouseup', () => { isDown = false; el.classList.remove('dragging'); });
    el.addEventListener('mousemove', (e) => {
      if (!isDown) return;
      e.preventDefault();
      const x = e.pageX - el.offsetLeft;
      moved = Math.max(moved, Math.abs(x - startX));
      el.scrollLeft = scrollLeft - (x - startX) * 1.5;
    });
    el.addEventListener('click', (e) => {
      if (moved > CLICK_DRAG_THRESHOLD) {
        e.stopPropagation();
        e.preventDefault();
      }
    }, true);
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

// ── MANGA LIST SCROLL MEMORY ──
// Remembered across a normal "click into a manga, hit back" round trip,
// but only until the chapter reader is actually opened -- chapter_reader.html
// clears this same key on its own load (see its comment there), since a
// scroll position from before you started reading isn't meaningful to
// restore anymore once you have, even after navigating all the way back
// through browser history later.
const MANGA_LIST_SCROLL_KEY = 'kinsho_manga_list_scroll';

// ── FULL PAGE-STATE CACHE ──
// Building, saving and loading each tab's state lives in static/tab_state.js
// (loaded before this file), shared with the chapter reader -- see its header
// for the "nothing visible changes after the page appears" design.

// ── MAIN APP ──
const app = createApp({
  components: { MangaThumb },
  directives: { dragScroll: vDragScroll },

  data() {
    const tabs = window.__LIBRARIES__ || [];
    const activeTab = (() => {
      if (tabs.length === 0) return null;
      const last = window.__LAST_TAB__;
      const found = tabs.find(l => l.id === last);
      return found ? found.id : tabs[0].id;
    })();

    // ── PER-LIBRARY TAB CACHE, SEEDED SYNCHRONOUSLY FROM sessionStorage ──
    // library_id -> the full computed display state buildTabState() (see
    // methods below) returns for it. Read here, before this component ever
    // renders for the first time, so a back-navigation to this page shows
    // real content (rows, grid, backdrop) and the correct scroll position
    // on the very first paint -- no empty page filling in while covers
    // load, then jumping to the remembered scroll position after the
    // fact. loadMangas() still runs its normal fetch afterward regardless
    // (mounted()), to reconcile against anything that's changed since this
    // snapshot was taken -- this is a stale-while-revalidate seed, not a
    // replacement for ever fetching fresh data.
    const tabCache = {};
    for (const tab of tabs) {
      const persisted = loadPersistedTabState(tab.id);
      if (persisted) tabCache[tab.id] = persisted;
    }
    // One saved copy per library, overwritten on every save -- only a
    // deleted library's copy could linger, so drop those. Also drops the
    // old 1-hour row-pick caches (superseded by picks saved in each tab).
    try {
      const tabIds = new Set(tabs.map(t => String(t.id)));
      for (const id of persistedTabLibraryIds()) {
        if (!tabIds.has(String(id))) localStorage.removeItem(tabCacheStorageKey(id));
      }
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const k = localStorage.key(i) || '';
        if (/^kinsho_(random|favourites)_row_/.test(k)) localStorage.removeItem(k);
      }
    } catch (e) {}
    const activeState = activeTab !== null ? tabCache[activeTab] : null;

    return {
      // ── TABS — loaded from backend via window.__LIBRARIES__ ──
      tabs,
      activeTab,

      // ── TRACKS WHETHER EACH ROW IS SCROLLED TO THE END ──
      atEnd: { lastRead: false, random: false, favourites: false, collections: false },

      // ── ADMIN: INTEGRITY ISSUE BADGE ──
      isAdmin:             false,
      integrityIssueCount: 0,

      // ── TAG/GENRE EDIT PERMISSION (context menu gating) ──
      canEditTags:   false,
      canEditGenres: false,

      // ── MANGA TILE CONTEXT MENU (long-press / right-click) ──
      // Exists mainly for flat-scan oneshot manga, which skip straight to
      // the reader and never reach manga_detail.html -- the only page
      // that otherwise offers favourite/collection/tag/genre actions.
      ctxMenuOpen:          false,
      ctxManga:             null,
      ctxView:              'menu',
      ctxCollections:       [],
      ctxCollectionSearch:  '',
      ctxLiveCollectionSearch: '',
      ctxTagInput:          '',
      ctxGenreInput:        '',
      allTags:              [],
      allGenres:            [],

      // ── ONESHOT OPEN CHOICE ──
      // A flat-scan oneshot has no detail page of its own -- clicking it
      // would otherwise always land on page 1 via manga_detail()'s server-side
      // redirect, with no way to resume. This popup asks Continue vs. Start
      // before navigating, instead of the old context-menu-only "Continue
      // Reading" item (which needed a long-press to even find).
      oneshotPopupOpen: false,
      oneshotManga:     null,
      oneshotLastPage:  0,

      // ── ROW DATA ──
      lastRead:    activeState ? activeState.lastRead   : [],
      random:      activeState ? activeState.random     : [],
      favourites:  activeState ? activeState.favourites : [],
      collectionsRow:       activeState ? activeState.collectionsRow : [],
      showCollectionsRow:   true,
      collectionMembership: loadPersistedMembership(),

      // ── GRID DATA + PAGINATION ──
      lastUpdated:      activeState ? activeState.lastUpdated      : [],
      lastUpdatedPage:  activeState ? activeState.lastUpdatedPage  : 1,
      lastUpdatedTotal: activeState ? activeState.lastUpdatedTotal : 0,
      // Column count the most recent loadLastUpdated() fetch was aligned
      // to -- lastUpdatedTotalPages needs the same value the server used
      // to compute per-page counts, or its page-button count would drift
      // from what's actually being served.
      lastUpdatedColumns: activeState ? activeState.lastUpdatedColumns : 1,
      activeTheme: null,
      bgLayerStyle: activeState ? activeState.bgLayerStyle : null,
      bgIsRaster:   activeState ? activeState.bgIsRaster   : false,

      // ── PER-LIBRARY TAB CACHE ──
      // Populated above from sessionStorage for an instant first paint,
      // then kept current in-memory for the rest of this page's lifetime:
      // for the active tab on mount, then for every OTHER library in the
      // background (see mounted()) so switching tabs is an instant local
      // read instead of a fresh round-trip each time.
      tabCache,
    };
  },

  computed: {
    lastUpdatedTotalPages() {
      const perPage1 = roundUpToMultiple(50, this.lastUpdatedColumns);
      const perPageN = roundUpToMultiple(100, this.lastUpdatedColumns);
      if (this.lastUpdatedTotal <= perPage1) return 1;
      return 1 + Math.ceil((this.lastUpdatedTotal - perPage1) / perPageN);
    },

    // ── CONTEXT MENU: COLLECTION PICKER / TAG-GENRE SUGGESTIONS ──
    ctxFilteredCollections() {
      const q = this.ctxLiveCollectionSearch.trim().toLowerCase();
      return this.ctxCollections.filter(c => !q || c.name.toLowerCase().includes(q));
    },
    ctxExactCollectionMatch() {
      const q = this.ctxLiveCollectionSearch.trim().toLowerCase();
      return this.ctxCollections.some(c => c.name.toLowerCase() === q);
    },
    ctxFilteredTagSuggestions() {
      const q = this.ctxTagInput.trim().toLowerCase();
      return this.allTags.filter(t => !q || t.toLowerCase().includes(q)).slice(0, 30);
    },
    ctxFilteredGenreSuggestions() {
      const q = this.ctxGenreInput.trim().toLowerCase();
      return this.allGenres.filter(g => !q || g.toLowerCase().includes(q)).slice(0, 30);
    },
  },

  created() {
    // library_id -> in-flight loadMangas() promise (see loadMangas). Plain,
    // non-reactive bookkeeping -- nothing renders from it.
    this._tabLoads = {};
  },

  async mounted() {
    await this.loadTheme();
    // Global on/off setting for the Collections row (template v-if only --
    // buildTabState reads the same setting itself), and which manga open
    // their collection instead: both fetched in parallel, never gating the
    // tab loads. Membership is seeded from its saved copy (data()), so a
    // tap before this returns already goes to the right place.
    this.loadCollectionsSetting();
    this.loadCollectionMembership();

    // Restore the scroll position from before navigating away, if the
    // chapter reader hasn't been visited since (see MANGA_LIST_SCROLL_KEY).
    // Deliberately done BEFORE the loadMangas()/loadCollectionsRow() calls
    // below, not after: data() already seeded this component's rows/grid
    // synchronously from the persisted tab-state cache (loadPersistedTabState),
    // so on a cache hit the real content is already on screen the instant
    // this runs -- only $nextTick (letting that already-seeded data finish
    // its first paint) stands between mount and an immediate restore,
    // instead of waiting on a fresh network round-trip first. On a cache
    // miss there's normally no saved position to restore anyway (both are
    // always written together and cleared together), so scrolling early
    // against a still-short page is harmless -- it just clamps near zero.
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
    // mobile WebViews in particular. Registered early (before the fetches
    // below) so a scroll during that window is never missed.
    let scrollSaveScheduled = false;
    window.addEventListener('scroll', () => {
      if (scrollSaveScheduled) return;
      scrollSaveScheduled = true;
      requestAnimationFrame(() => {
        sessionStorage.setItem(MANGA_LIST_SCROLL_KEY, String(window.scrollY));
        scrollSaveScheduled = false;
      });
    }, { passive: true });

    if (this.activeTab !== null) {
      // Reconciles the (possibly cache-seeded, possibly empty) current
      // state against a fresh fetch regardless -- a stale-while-revalidate
      // follow-up, not a replacement for the seed above. Vue only touches
      // the DOM nodes that actually differ, so when the seed already
      // matched current server state this is invisible.
      //
      // Passing the cache-seeded lastUpdatedPage through here matters: without
      // it, this reconciliation fetch would silently hardcode page 1 and
      // overwrite an already-restored later page (e.g. a back-navigation that
      // remembered the user was on page 3) right after the correct page's
      // first paint -- undoing the remembered-position feature for pagination
      // specifically, even though the remembered scroll Y itself was fine.
      const activeCached = this.tabCache[this.activeTab];
      await this.loadMangas(this.activeTab, (activeCached && activeCached.lastUpdatedPage) || 1);
      // Warm every other library's tab cache in the background, whether
      // the user ever visits it this session or not, so switching tabs
      // later is an instant local read instead of a fresh round-trip.
      // Deliberately not awaited (and started only after the active tab's
      // own load above has finished) so this never delays first paint or
      // competes with it for the browser's connection pool. Same
      // preferred-page reasoning as above -- otherwise switching to a tab
      // that was left on a later page would show it reset to page 1.
      // Round-robin (2026-09-23): every other tab's core data (rows,
      // backdrop) loads first, in parallel; only once ALL of them have it
      // does any tab move on to its Last Updated grid/collections -- so a
      // big library can't hold up a small one's first view.
      const others = this.tabs.filter(t => t.id !== this.activeTab);
      let coresLeft = others.length;
      let releaseRest;
      const restGate = new Promise(r => { releaseRest = r; });
      const afterCore = () => { if (--coresLeft === 0) releaseRest(); };
      const settingsPromise = fetch('/api/settings').then(r => r.json());
      for (const tab of others) {
        const cached = this.tabCache[tab.id];
        this.loadMangas(tab.id, (cached && cached.lastUpdatedPage) || 1, { afterCore, restGate, settingsPromise });
      }
    }
    await this.loadIntegrityBadge();

    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && this.activeTab !== null) {
        this.loadMangas(this.activeTab);
      }
    });

    // ── REFRESH COLLECTION MEMBERSHIP AFTER A BFCACHE RESTORE ──
    // The in-app "back" button (kinshoGoBack, api.js) uses history.back(),
    // which the browser/WebView can satisfy by restoring this exact page
    // instance from bfcache instead of re-running mounted() -- so
    // collectionMembership (fetched once above) goes stale if a collection
    // was created or had a member added on whatever page this tab is coming
    // back from. event.persisted is only true for a bfcache restore, never a
    // normal fresh load (which already gets a correct fetch from mounted()).
    // Same restore also shows this page exactly as it was before the reader
    // opened -- but the reader has since re-saved every tab (reshuffled
    // rows, new Last Read). Adopt those saved copies right away, so the
    // refresh fetch that follows finds nothing left to change on screen.
    window.addEventListener('pageshow', (e) => {
      if (!e.persisted) return;
      for (const tab of this.tabs) {
        const saved = loadPersistedTabState(tab.id);
        if (saved) this.tabCache[tab.id] = saved;
      }
      const active = this.activeTab !== null ? this.tabCache[this.activeTab] : null;
      if (active) this.applyTabState(active);
      this.collectionMembership = loadPersistedMembership();
      this.loadCollectionMembership();
    });

    // ── PICK UP A SCAN THAT FINISHES WHILE THIS PAGE IS SITTING OPEN ──
    // visibilitychange (above) only catches a scan that ran while this page
    // was hidden/backgrounded. A scan triggered from Settings in another
    // tab/device, or the 12-hour periodic auto-rescan, can just as easily
    // finish while the user is sitting on this exact page the whole time --
    // there's nothing in that case to make mounted() ever run again, so the
    // grid/rows would otherwise stay frozen at pre-scan content until the
    // user happens to navigate away and back (or force a hard reload,
    // which a browser can do but this app's own UI has no equivalent for).
    // Polling the same lightweight status endpoint Settings' own scan
    // button already uses, and comparing last_scanned against what this
    // page's current content was actually built from, catches that case too.
    this._knownLastScanned = {};
    setInterval(() => this.pollScanForChanges(), 20000);
  },

  methods: {
    async loadIntegrityBadge() {
      try {
        const meRes  = await fetch(apiUrl('/api/auth/me'));
        const meData = await meRes.json();
        this.isAdmin = meData.ok && meData.role === 'admin';
        // Also doubles as this page's one fetch of tag/genre permission,
        // used to gate the manga tile context menu's Add Tag/Add Genre
        // rows -- same isAdmin-or-permission check manga_detail.html uses.
        const p = (meData.ok && meData.permissions) || {};
        this.canEditTags   = this.isAdmin || p.tags   === true;
        this.canEditGenres = this.isAdmin || p.genres === true;
        if (!this.isAdmin) return;
        const res  = await fetch(apiUrl('/api/admin/integrity/issues'));
        const data = await res.json();
        this.integrityIssueCount = data.count || 0;
      } catch (e) {
        this.isAdmin = false;
      }
    },

    // ── MANGA TILE CONTEXT MENU (long-press / right-click) ──
    // Exists mainly for flat-scan oneshot manga, which redirect straight
    // into the reader and never reach manga_detail.html -- the only page
    // that otherwise offers favourite/collection/tag/genre actions. Works
    // the same for any manga tile though, not just oneshots.
    openCtxMenu(manga) {
      this.ctxManga = manga;
      this.ctxView = 'menu';
      this.ctxCollections = [];
      this.ctxCollectionSearch = '';
      this.ctxLiveCollectionSearch = '';
      this.ctxTagInput = '';
      this.ctxGenreInput = '';
      this.ctxMenuOpen = true;
    },

    closeCtxMenu() {
      this.ctxMenuOpen = false;
      this.ctxManga = null;
    },

    // Same click-vs-text-selection-drag distinction used by every other
    // popup-overlay in the app (see manga_detail.html's own copy of this
    // pair) -- a plain @click.self would also close the popup when a drag
    // that started on selectable text inside it happens to release past
    // the popup's border.
    onOverlayMouseDown(e) {
      this._ctxOverlayMouseDownSelf = (e.target === e.currentTarget);
    },
    onOverlayClick(e, closeFn) {
      if (e.target === e.currentTarget && this._ctxOverlayMouseDownSelf) closeFn();
    },

    async ctxToggleFavourite() {
      if (!this.ctxManga) return;
      try {
        await fetch(apiUrl(`/api/manga/${this.activeTab}/${this.ctxManga.id}/favourite`), { method: 'POST' });
      } catch (e) { /* best-effort */ }
      this.closeCtxMenu();
      this.loadMangas(this.activeTab);
    },

    async ctxOpenCollections() {
      this.ctxView = 'collection';
      this.$nextTick(() => { if (this.$refs.ctxCollectionSearchRef) this.$refs.ctxCollectionSearchRef.focus(); });
      if (!this.ctxManga) { this.ctxCollections = []; return; }
      try {
        const res = await fetch(apiUrl('/api/collections'));
        const data = await res.json();
        const editable = (data.collections || []).filter(c => c.can_edit);
        const details = await Promise.all(
          editable.map(c => fetch(apiUrl(`/api/collections/${c.id}`)).then(r => r.json()))
        );
        this.ctxCollections = editable.map((c, i) => ({
          id:        c.id,
          name:      c.name,
          has_manga: (details[i].members || []).some(
            m => m.library_id === this.activeTab && m.manga_id === this.ctxManga.id
          ),
        }));
      } catch (e) { this.ctxCollections = []; }
    },

    async _ctxAddToCollection(collectionId) {
      await fetch(apiUrl(`/api/collections/${collectionId}/members/add`), {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ library_id: this.activeTab, manga_id: this.ctxManga.id, manga_name: this.ctxManga.title }),
      });
    },

    async _ctxRemoveFromCollection(collectionId) {
      await fetch(apiUrl(`/api/collections/${collectionId}/members/remove`), {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ library_id: this.activeTab, manga_id: this.ctxManga.id }),
      });
    },

    // Same one-collection-at-a-time behavior as manga_detail.html's own
    // toggleCollectionMembership: picking a different collection moves the
    // manga (removes from the current, adds to the new); picking the one
    // it's already in removes it (back to unassigned).
    async ctxToggleCollectionMembership(c) {
      const current = this.ctxCollections.find(x => x.has_manga);
      if (current && current.id === c.id) {
        await this._ctxRemoveFromCollection(c.id);
      } else {
        if (current) await this._ctxRemoveFromCollection(current.id);
        await this._ctxAddToCollection(c.id);
      }
      await this.ctxOpenCollections();
      this.loadCollectionMembership();
    },

    async ctxCreateAndAddToCollection() {
      const name = this.ctxCollectionSearch.trim();
      if (!name || !this.ctxManga) return;
      const current = this.ctxCollections.find(x => x.has_manga);
      if (current) await this._ctxRemoveFromCollection(current.id);
      const res  = await fetch(apiUrl('/api/collections'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
      const data = await res.json();
      if (data.ok) await this._ctxAddToCollection(data.id);
      this.ctxCollectionSearch = '';
      this.ctxLiveCollectionSearch = '';
      await this.ctxOpenCollections();
      this.loadCollectionMembership();
    },

    ctxOnCollectionSearchEnter() {
      if (this.ctxCollectionSearch.trim() && !this.ctxExactCollectionMatch) {
        this.ctxCreateAndAddToCollection();
      } else if (this.ctxFilteredCollections.length === 1) {
        this.ctxToggleCollectionMembership(this.ctxFilteredCollections[0]);
      }
    },

    async ctxOpenTag() {
      this.ctxView = 'tag';
      this.$nextTick(() => { if (this.$refs.ctxTagInputRef) this.$refs.ctxTagInputRef.focus(); });
      if (this.allTags.length) return;
      try {
        const res = await fetch(apiUrl('/api/tags'));
        const data = await res.json();
        this.allTags = data.tags || [];
      } catch (e) { this.allTags = []; }
    },

    async ctxSaveTag() {
      const tag = this.ctxTagInput.trim();
      if (!tag || !this.ctxManga) return;
      try {
        await fetch(apiUrl(`/api/manga/${this.activeTab}/${this.ctxManga.id}/tags/add`), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ tag }),
        });
      } catch (e) { /* best-effort */ }
      this.closeCtxMenu();
    },

    async ctxOpenGenre() {
      this.ctxView = 'genre';
      this.$nextTick(() => { if (this.$refs.ctxGenreInputRef) this.$refs.ctxGenreInputRef.focus(); });
      if (this.allGenres.length) return;
      try {
        const res = await fetch(apiUrl('/api/genres'));
        const data = await res.json();
        this.allGenres = data.genres || [];
      } catch (e) { this.allGenres = []; }
    },

    async ctxSaveGenre() {
      const genre = this.ctxGenreInput.trim();
      if (!genre || !this.ctxManga) return;
      try {
        await fetch(apiUrl(`/api/manga/${this.activeTab}/${this.ctxManga.id}/genres/add`), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ genre }),
        });
      } catch (e) { /* best-effort */ }
      this.closeCtxMenu();
    },

    // Compares the active tab's library against its own last_scanned
    // timestamp (same field Settings' pollScanStatus watches). The first
    // check after a tab becomes active just records the baseline -- that
    // manga list was, by definition, already loaded fresh moments ago --
    // only a LATER change against that recorded baseline means a scan
    // completed since, and is worth reloading for.
    async pollScanForChanges() {
      if (this.activeTab === null) return;
      const libraryId = this.activeTab;
      try {
        const res  = await fetch(apiUrl(`/api/scan/${libraryId}/status`));
        const data = await res.json();
        if (data.running) return;
        const known = this._knownLastScanned[libraryId];
        if (known === undefined) {
          this._knownLastScanned[libraryId] = data.last_scanned || null;
          return;
        }
        if (data.last_scanned && data.last_scanned !== known) {
          this._knownLastScanned[libraryId] = data.last_scanned;
          this.loadMangas(libraryId);
        }
      } catch (e) {
        // Network hiccup -- next tick tries again.
      }
    },

    // ── COLLECTIONS ROW: on/off SETTING (global, not per-library) ──
    async loadCollectionsSetting() {
      try {
        const res  = await fetch(apiUrl('/api/settings'));
        const data = await res.json();
        this.showCollectionsRow = data.show_collections_row !== false;
      } catch (e) {
        this.showCollectionsRow = true;
      }
    },

    async loadCollectionMembership() {
      try {
        this.collectionMembership = await fetchCollectionMembership();
      } catch (e) {
        // Keep whatever was seeded from the saved copy.
      }
    },

    openCollection(id) { window.location.href = `/collection/${id}`; },
    // "View more" on the Collections row is reached from a specific tab,
    // so the resulting page should show what's relevant to that tab, same
    // as the row itself does -- see collections_list.js for how the lib=
    // param gets read back out and threaded into its own /api/collections
    // call.
    goToCollections()  { window.location.href = `/collections?lib=${this.activeTab}`; },

    // ── LOAD MANGAS FOR A TAB, CACHE IT, APPLY IT IF IT'S THE ACTIVE ONE ──
    // Called both for the active tab (mounted()/switchTab()'s cache-miss
    // fallback/visibilitychange) and for every other library in the
    // background (mounted()'s prefetch loop) -- buildTabState() never
    // touches live display state itself, so a background call for a tab
    // the user isn't looking at can't clobber what's currently on screen.
    async loadMangas(libraryId, lastUpdatedPage, opts = {}) {
      // One load per tab at a time: a tap on a tab whose background load is
      // still running reuses it instead of starting a duplicate (measured on
      // the tablet: the duplicate doubled the wait).
      if (this._tabLoads[libraryId]) return this._tabLoads[libraryId];
      const run = this._loadMangas(libraryId, lastUpdatedPage, opts);
      this._tabLoads[libraryId] = run;
      try {
        return await run;
      } finally {
        delete this._tabLoads[libraryId];
      }
    },

    async _loadMangas(libraryId, lastUpdatedPage, opts) {
      // The backdrop and the Last Read/Random/Favourites rows only need the
      // FIRST of buildTabState()'s two fetch phases (mangas+settings+history)
      // -- the second phase (paginated Last Updated grid, Collections row) is
      // unrelated to any of them, but used to gate applying ANY of this
      // state until the whole thing resolved, which held the backdrop behind
      // two more full round-trips it never needed. Applying the core state
      // the moment it's ready (active tab only -- a background prefetch has
      // nothing on screen to update early) is what actually fixes the
      // backdrop's late pop-in; see buildTabState's own comment for the
      // other half (running that second phase's two fetches in parallel
      // instead of sequentially).
      // Checked when the core data ARRIVES, not when the load started -- a
      // background load for a tab the user switched to meanwhile paints its
      // rows/backdrop as soon as they're ready too.
      const onCoreReady = (core) => {
        if (libraryId === this.activeTab) this.applyCoreTabState(core);
      };
      const state = await buildTabState(libraryId, lastUpdatedPage, {
        ...opts,
        onCoreReady,
        // Keeps the shuffled rows' picks -- only the reader reshuffles. The
        // saved copy, not this page's memory: the reader may have re-saved
        // it (new picks, Last Read) since this page instance was built.
        prev: loadPersistedTabState(libraryId) || this.tabCache[libraryId],
        columns: currentGridColumns(),
      });
      if (!state) return;
      this.tabCache[libraryId] = state;
      // Also persisted to sessionStorage (not just kept in memory) so the
      // NEXT full page load -- a back-navigation, since this is a
      // traditional multi-page app -- can seed data() with it synchronously
      // instead of starting from an empty page. See loadPersistedTabState/
      // persistTabState's own comment for the full reasoning.
      persistTabState(libraryId, state);
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

    // Copies a buildTabState() result onto the live display fields —
    // separate from loadMangas() so switchTab() can apply an
    // already-cached state synchronously, with no fetch at all.
    applyTabState(state) {
      this.lastRead          = state.lastRead;
      this.random             = state.random;
      this.favourites         = state.favourites;
      this.collectionsRow     = state.collectionsRow;
      this.bgLayerStyle       = state.bgLayerStyle;
      this.bgIsRaster         = state.bgIsRaster;
      this.lastUpdated        = state.lastUpdated;
      this.lastUpdatedPage    = state.lastUpdatedPage;
      this.lastUpdatedTotal   = state.lastUpdatedTotal;
      this.lastUpdatedColumns = state.lastUpdatedColumns;
    },

    // Early-paint counterpart to applyTabState() -- sets just the fields
    // buildTabState()'s onCoreReady callback provides, leaving
    // lastUpdated/collectionsRow untouched (whatever they currently show
    // stays put until the full state arrives moments later via the normal
    // applyTabState() call loadMangas() already makes). bgUrlToLock isn't
    // applied here on purpose -- loadMangas() still handles persisting a
    // freshly-captured lock from the final, complete state, not this
    // early/partial one.
    applyCoreTabState(core) {
      this.lastRead     = core.lastRead;
      this.random        = core.random;
      this.favourites    = core.favourites;
      this.bgLayerStyle  = core.bgLayerStyle;
      this.bgIsRaster    = core.bgIsRaster;
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
      const result = await fetchLastUpdatedPage(libraryId, page, historyByMangaId, currentGridColumns());
      if (!result) return;
      this.lastUpdated        = result.mangas;
      this.lastUpdatedPage    = result.page;
      this.lastUpdatedTotal   = result.total;
      this.lastUpdatedColumns = result.columns;
      // Page-button navigation (unlike a back-navigation restore, which goes
      // through applyState()/mounted() instead and must keep the remembered
      // scroll Y) always lands the user back at the top -- otherwise, since
      // page >1 hides the rows above the grid (see the v-if on those
      // sections in manga_list.html), they'd stay scrolled to wherever the
      // pagination controls used to be, well past the now-shorter page.
      window.scrollTo(0, 0);
      // Keep the persisted snapshot (tabCache + sessionStorage) in sync with
      // what's actually on screen -- without this, clicking a page button only
      // ever updated the live view above, so the cache a later back-navigation
      // restores from still held whichever page was cached at the last full
      // loadMangas() (normally page 1 from the initial load). That's what made
      // "go to page 2, open a manga, hit back" always land back on page 1
      // instead of page 2, even though the scroll Y itself restored correctly.
      if (this.tabCache[libraryId]) {
        this.tabCache[libraryId] = {
          ...this.tabCache[libraryId],
          lastUpdated:        result.mangas,
          lastUpdatedPage:    result.page,
          lastUpdatedTotal:   result.total,
          lastUpdatedColumns: result.columns,
        };
        persistTabState(libraryId, this.tabCache[libraryId]);
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

    openManga(manga) {
      const cid = this.collectionMembership[`${this.activeTab}:${manga.id}`];
      if (cid) { window.location.href = `/collection/${cid}`; return; }
      if (manga.is_oneshot) { this.openOneshotPopup(manga); return; }
      window.location.href = `/manga/${this.activeTab}/${manga.id}`;
    },

    // A flat-scan oneshot skips manga_detail.html entirely (manga_detail()
    // redirects straight into the reader) -- this popup is what asks
    // Continue vs. Start before that redirect happens, since there's no
    // detail page left to offer the choice on. is_oneshot/last_chapter_id/
    // last_page are already joined onto every manga in this file's own
    // lists (buildTabState), so no extra fetch is needed here.
    openOneshotPopup(manga) {
      this.oneshotManga    = manga;
      this.oneshotLastPage = manga.last_chapter_id ? manga.last_page : 0;
      this.oneshotPopupOpen = true;
    },

    closeOneshotPopup() {
      this.oneshotPopupOpen = false;
      this.oneshotManga = null;
    },

    oneshotOpen(page) {
      if (!this.oneshotManga) return;
      const manga = this.oneshotManga;
      this.closeOneshotPopup();
      // Start Reading (page 0/none) reuses the plain manga-page URL --
      // manga_detail()'s own oneshot redirect already lands on page 1 with
      // no ?page= param, same as clicking any other fresh manga would.
      const url = page > 0
        ? `/manga/${this.activeTab}/${manga.id}/chapter/${manga.last_chapter_id}?page=${page}`
        : `/manga/${this.activeTab}/${manga.id}`;
      window.location.href = url;
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

  }
});
app.config.errorHandler = (err, vm, info) => { console.error('Vue error:', err, info); };
app.mount('#app');

