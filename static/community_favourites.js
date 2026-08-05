const { createApp, defineComponent } = Vue;

// ── MANGA THUMBNAIL COMPONENT ── (same as app.js/category_list.js)
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
        <div class="thumb-title">{{ manga.title }}</div>
      </div>
    </div>
  `
});

// ── MAIN APP ──
// Cross-library, viewer-permission-filtered view of one other user's
// favourites -- reached only via the Community settings section's "View
// more" link. No shuffle (favourites here aren't randomized) and no
// pinned/sessionStorage handoff (that mechanism exists to keep the home
// page's own reshuffling Random/Favourites row stable across navigation;
// this page is always reached from a stable, non-reshuffling list).
const app = createApp({
  components: { MangaThumb },

  data() {
    return {
      targetUsername: window.__TARGET_USERNAME__,

      mangas:  [],
      page:    1,
      total:   0,
      perPage: 50,
      loading: true,

      activeTheme: null,
      bgLayerStyle: null,
      bgIsRaster: false,
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
    await this.loadPage(1);
  },

  methods: {
    async loadPage(page) {
      this.loading = true;
      try {
        const url = `/api/community/${encodeURIComponent(this.targetUsername)}/favourites?page=${page}`;
        const res = await fetch(url);
        const data = await res.json();
        this.mangas  = data.mangas || [];
        this.page    = data.page;
        this.total   = data.total;
        this.perPage = data.per_page;
        window.scrollTo({ top: 0, behavior: 'instant' });
      } catch (e) {
        console.error('Failed to load community favourites:', e);
      }
      this.loading = false;
    },

    openManga(libraryId, mangaId) {
      window.location.href = `/manga/${libraryId}/${mangaId}`;
    },
    goBack() { window.kinshoGoBack('/settings'); },

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
