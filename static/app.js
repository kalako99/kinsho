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
      activeTheme: null,
      bgLayerStyle: null,
      bgIsRaster: false,
    };
  },

  computed: {
    lastUpdatedTotalPages() {
      if (this.lastUpdatedTotal <= 50) return 1;
      return 1 + Math.ceil((this.lastUpdatedTotal - 50) / 100);
    },
  },

  async mounted() {
    await this.loadTheme();
    if (this.activeTab !== null) {
      await this.loadMangas(this.activeTab);
    }
    await this.loadCollectionsRow();
    await this.loadIntegrityBadge();
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

    // ── LOAD MANGAS FOR ACTIVE TAB FROM API ──
    async loadMangas(libraryId) {
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
        this.lastRead = (historyData.history || [])
          .slice(0, 20)
          .map(h => mangaById[h.manga_id])
          .filter(Boolean);

        this.random     = [...mangas].sort(() => Math.random() - 0.5).slice(0, 20);
        this.favourites = [...mangas]
          .filter(m => favouriteIds.has(m.id))
          .sort(() => Math.random() - 0.5)
          .slice(0, 20);

        // Set ambient blurred background from the most recently read manga,
        // falling back to the first manga (natural sort) in the active library
        let bgManga = null;
        if (this.lastRead.length > 0 && this.lastRead[0].coverLarge) {
          bgManga = this.lastRead[0];
        } else if (mangas.length > 0) {
          bgManga = [...mangas].sort((a, b) =>
            a.title.localeCompare(b.title, undefined, { numeric: true, sensitivity: 'base' })
          )[0];
        }

        const backdropEnabled = settings.backdrop_list !== false;
        const lockBackdrop = settings.lock_backdrop === true;
        if (lockBackdrop && this.bgLayerStyle) {
          // Keep whatever backdrop is already showing — don't recompute it.
        } else if (backdropEnabled && bgManga && bgManga.coverLarge) {
          this.bgLayerStyle = { backgroundImage: `url('${bgManga.coverLarge}')` };
          this.bgIsRaster = true;
        } else {
          this.bgLayerStyle = null;
          this.bgIsRaster = false;
        }

        // Load Last Updated page 1 separately (sorted + paginated), reuse history
        await this.loadLastUpdated(libraryId, 1, historyByMangaId);
      } catch (e) {
        console.error('Failed to load mangas:', e);
      }
    },

    // ── LOAD LAST UPDATED PAGE ──
    async loadLastUpdated(libraryId, page, historyByMangaId) {
      try {
        const needsHistory = !historyByMangaId;
        const [res, historyRes] = await Promise.all([
          fetch(`/api/mangas/${libraryId}?sort=last_updated&page=${page}`),
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

        this.lastUpdated = data.mangas.map((m) => {
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
        this.lastUpdatedPage  = data.page;
        this.lastUpdatedTotal = data.total;
      } catch (e) {
        console.error('Failed to load last updated:', e);
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
      window.location.href = `/manga/${this.activeTab}/category/${category}`;
    },

    openSettings()   { window.location.href = '/settings'; },

    switchTab(id) {
      this.activeTab = id;
      this.lastUpdatedPage  = 1;
      this.lastUpdatedTotal = 0;
      this.loadMangas(id);
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

