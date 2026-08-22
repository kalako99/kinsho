const { createApp, defineComponent } = Vue;

// ── MANGA THUMBNAIL COMPONENT ── (same as app.js)
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

// ── MAIN APP ──
const app = createApp({
  components: { MangaThumb },

  data() {
    return {
      libraryId: window.__LIBRARY_ID__,
      category:  window.__CATEGORY__,
      categoryTitle: window.__CATEGORY_TITLE__,

      mangas: [],
      page: 1,
      total: 0,
      perPage: 50,

      seed: null,

      activeTheme: null,
      bgLayerStyle: null,
      bgIsRaster: false,

      collectionMembership: {},

      // ── ONESHOT OPEN CHOICE ──
      // See app.js's own copy of this popup for the full reasoning -- a
      // flat-scan oneshot has no detail page, so a plain click needs to ask
      // Continue vs. Start instead of always landing on page 1. This page's
      // manga list doesn't carry last_chapter_id/last_page (unlike app.js's
      // own, already history-joined lists), so the popup fetches them
      // on demand when opened rather than needing every category-list
      // response to eagerly join reading history for every manga.
      oneshotPopupOpen: false,
      oneshotManga:     null,
      oneshotLastPage:  0,
    };
  },

  computed: {
    totalPages() {
      if (this.perPage <= 0) return 1;
      return Math.max(1, Math.ceil(this.total / this.perPage));
    },
  },

  async mounted() {
    await this.loadTheme();
    if (this.category === 'random') {
      this.seed = this.getOrCreateSeed();
    }
    // One-shot handoff from the home page's row (see app.js's goMore()) --
    // removed immediately after reading so it only ever applies to this
    // one arrival, not a later manual refresh/back-forward on this page.
    let pinnedIds = null;
    const pinnedKey = `kinsho_category_pinned_${this.libraryId}_${this.category}`;
    try {
      const raw = sessionStorage.getItem(pinnedKey);
      if (raw) pinnedIds = JSON.parse(raw);
    } catch (e) {
      pinnedIds = null;
    }
    sessionStorage.removeItem(pinnedKey);

    await this.loadPage(1, pinnedIds);
    await this.loadCollectionMembership();
  },

  methods: {
    async loadCollectionMembership() {
      try {
        const res  = await fetch(apiUrl('/api/collections/membership'));
        const data = await res.json();
        this.collectionMembership = data.membership || {};
      } catch (e) {
        this.collectionMembership = {};
      }
    },

    getOrCreateSeed() {
      const override = sessionStorage.getItem(`category_random_seed_override_${this.libraryId}`);
      if (override) return override;
      // Changes automatically every 12 hours
      return String(Math.floor(Date.now() / (12 * 60 * 60 * 1000)));
    },

    shuffle() {
      const newSeed = String(Math.floor(Math.random() * 1000000000));
      sessionStorage.setItem(`category_random_seed_override_${this.libraryId}`, newSeed);
      this.seed = newSeed;
      this.loadPage(1);
    },

    async loadPage(page, pinnedIds) {
      try {
        let url = `/api/category-list/${this.libraryId}/${this.category}?page=${page}`;
        if (this.category === 'random' && this.seed !== null) {
          url += `&seed=${this.seed}`;
        }
        if (pinnedIds && pinnedIds.length > 0) {
          url += `&pinned=${pinnedIds.map(encodeURIComponent).join(',')}`;
        }
        const res = await fetch(url);
        const data = await res.json();
        this.mangas  = data.mangas || [];
        this.page    = data.page;
        this.total   = data.total;
        this.perPage = data.per_page;
        window.scrollTo({ top: 0, behavior: 'instant' });
      } catch (e) {
        console.error('Failed to load category list:', e);
      }
    },

    openManga(manga) {
      const cid = this.collectionMembership[`${this.libraryId}:${manga.id}`];
      if (cid) { window.location.href = `/collection/${cid}`; return; }
      if (manga.manga_type === 'oneshot') { this.openOneshotPopup(manga); return; }
      window.location.href = `/manga/${this.libraryId}/${manga.id}`;
    },

    async openOneshotPopup(manga) {
      this.oneshotManga     = manga;
      this.oneshotLastPage  = 0;
      this.oneshotPopupOpen = true;
      try {
        const res  = await fetch(apiUrl(`/api/reading/history/${this.libraryId}/${manga.id}`));
        const data = await res.json();
        if (this.oneshotManga === manga && data.last_chapter_id) {
          this.oneshotManga = { ...manga, last_chapter_id: data.last_chapter_id };
          this.oneshotLastPage = data.last_page || 0;
        }
      } catch (e) { /* no resume position available -- Start Reading still works */ }
    },

    closeOneshotPopup() {
      this.oneshotPopupOpen = false;
      this.oneshotManga = null;
    },

    // Same click-vs-text-selection-drag distinction used by every other
    // popup-overlay in the app -- a plain @click.self would also close the
    // popup when a drag that started on selectable text inside it happens
    // to release past the popup's border.
    onOverlayMouseDown(e) {
      this._overlayMouseDownSelf = (e.target === e.currentTarget);
    },
    onOverlayClick(e, closeFn) {
      if (e.target === e.currentTarget && this._overlayMouseDownSelf) closeFn();
    },

    oneshotOpen(page) {
      if (!this.oneshotManga) return;
      const manga = this.oneshotManga;
      this.closeOneshotPopup();
      const url = page > 0
        ? `/manga/${this.libraryId}/${manga.id}/chapter/${manga.last_chapter_id}?page=${page}`
        : `/manga/${this.libraryId}/${manga.id}`;
      window.location.href = url;
    },

    goBack()      { window.kinshoGoBack('/'); },

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
  }
});
app.config.errorHandler = (err, vm, info) => { console.error('Vue error:', err, info); };
app.mount('#app');
