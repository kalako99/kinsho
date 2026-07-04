const { createApp } = Vue;

const app = createApp({
  data() {
    return {
      collections:       [],
      loading:           true,

      showCreatePopup:   false,
      newCollectionName: '',

      bgLayerStyle: null,
      bgIsRaster:   false,
    };
  },

  async mounted() {
    await this.loadTheme();
    await this.loadCollections();
  },

  methods: {
    async loadCollections() {
      this.loading = true;
      try {
        const res  = await fetch(apiUrl('/api/collections'));
        const data = await res.json();
        this.collections = data.collections || [];
      } catch (e) {
        console.error('Failed to load collections:', e);
        this.collections = [];
      } finally {
        this.loading = false;
      }
    },

    openCollection(id) { window.location.href = `/collection/${id}`; },
    goBack()           { window.location.href = '/'; },

    async createCollection() {
      const name = this.newCollectionName.trim();
      if (!name) return;
      try {
        const res  = await fetch(apiUrl('/api/collections'), {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ name }),
        });
        const data = await res.json();
        if (data.ok) {
          this.showCreatePopup = false;
          this.newCollectionName = '';
          window.location.href = `/collection/${data.id}`;
        }
      } catch (e) {
        console.error('Failed to create collection:', e);
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
        const res  = await fetch('/api/settings');
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
