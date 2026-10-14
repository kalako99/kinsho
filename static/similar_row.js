// ── SIMILAR MANGA ROW (2026-10-14) ──
// The "Similar manga" row under the chapter/volume list of Manga Details,
// Volume Details and Collection Details: manga from the same library with
// the most similar genres and tags (SUGGESTIONS in main.py). One horizontal,
// drag-scrollable row of library-page style tiles, no "View more". Hidden
// when the user switched it off (Settings -> Appearance -> Rows) or when
// there's nothing to suggest. Loads after the page, so it never delays it.
// Plain script; each detail page registers it: .component('similar-row', SimilarRow).

(function injectSimilarRowCss() {
  const css = `
    .sim-section { max-width: 900px; margin: -24px auto 0; padding: 0 0 40px; }
    .sim-section .sim-row {
      display: flex; gap: 12px; overflow-x: auto;
      padding: 16px 20px 10px; margin-top: 4px;
      scrollbar-width: none; cursor: grab; user-select: none;
    }
    .sim-section .sim-row::-webkit-scrollbar { display: none; }
    .sim-section .sim-row.dragging { cursor: grabbing; }
    .sim-thumb { flex: 0 0 clamp(100px, 13vw, 160px); width: clamp(100px, 13vw, 160px); cursor: pointer; transition: transform 0.2s; text-decoration: none; }
    .sim-thumb:hover { transform: translateY(-4px); }
    .sim-thumb:hover .sim-cover { box-shadow: 0 8px 24px rgba(0,0,0,0.55); }
    .sim-cover {
      width: 100%; height: calc(clamp(100px, 13vw, 160px) * 3 / 2);
      background: var(--color-secondary, #1d1113); border-radius: 11px; border: 1px solid #333;
      overflow: hidden; position: relative; box-shadow: 0 4px 14px rgba(0,0,0,0.45);
    }
    .sim-cover img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; border-radius: 11px; display: block; }
    .sim-cover .sim-nocover { display: flex; width: 100%; height: 100%; align-items: center; justify-content: center; color: #555; font-size: 0.7rem; }
    .sim-badge {
      position: absolute; top: 6px; right: 6px; background: var(--color-primary, #e94560); color: #fff;
      font-size: 0.6rem; font-weight: 700; letter-spacing: 1px; padding: 2px 6px; border-radius: 6px; pointer-events: none;
    }
    .sim-body { padding: 5px 2px 0; }
    .sim-progress { display: flex; align-items: center; gap: 5px; margin-bottom: 4px; }
    .sim-track { flex: 1; height: 6px; background: #333; border-radius: 2px; overflow: hidden; }
    .sim-bar { height: 100%; background: var(--color-primary, #e94560); border-radius: 2px; }
    .sim-pct { font-size: 0.6rem; color: #888; white-space: nowrap; flex-shrink: 0; width: 26px; text-align: right; }
    .sim-title {
      font-size: 0.72rem; color: var(--color-text, #f0f0f0); white-space: nowrap; overflow: hidden;
      text-overflow: ellipsis; text-shadow: 0 1px 4px rgba(0,0,0,0.5);
    }
  `;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);
})();

const SimilarRow = {
  name: 'SimilarRow',
  props: {
    url: { type: String, required: true },  // /api/manga/{lib}/{id}/similar or /api/collections/{id}/similar
  },
  data() {
    return { mangas: [] };
  },
  watch: {
    url() { this.load(); },
  },
  mounted() {
    this.load();
    this.initDrag();
  },
  methods: {
    async load() {
      try {
        const res = await fetch(apiUrl(this.url));
        const data = await res.json();
        this.mangas = data.mangas || [];
      } catch (e) {
        this.mangas = [];
      }
    },
    open(m) {
      window.location.href = `/manga/${m.library_id}/${encodeURIComponent(m.id)}`;
    },
    // Mouse drag scrolls the row; a drag never counts as a click on a tile
    // (same rule as the library page's rows). Touch scrolls natively.
    initDrag() {
      let down = false, startX = 0, startLeft = 0, moved = 0;
      const row = () => this.$refs.row;
      this.$el.addEventListener('mousedown', (e) => {
        if (!row() || !row().contains(e.target)) return;
        e.preventDefault();
        down = true; moved = 0;
        startX = e.pageX; startLeft = row().scrollLeft;
        row().classList.add('dragging');
      });
      document.addEventListener('mouseup', () => { down = false; if (row()) row().classList.remove('dragging'); });
      document.addEventListener('mousemove', (e) => {
        if (!down || !row()) return;
        moved = Math.max(moved, Math.abs(e.pageX - startX));
        row().scrollLeft = startLeft - (e.pageX - startX) * 1.5;
      });
      this.$el.addEventListener('click', (e) => {
        if (moved > 5) { e.stopPropagation(); e.preventDefault(); moved = 0; }
      }, true);
      this.$el.addEventListener('dragstart', (e) => e.preventDefault());
    },
  },
  template: `
    <div class="sim-section">
      <template v-if="mangas.length">
        <div class="sec-head"><span class="sec-title">Similar manga</span></div>
        <div class="sim-row" ref="row">
          <div v-for="m in mangas" :key="m.library_id + ':' + m.id" class="sim-thumb" @click="open(m)">
            <div class="sim-cover">
              <img v-if="m.cover_url" :src="m.cover_url" :alt="m.name" loading="lazy" draggable="false">
              <span v-else class="sim-nocover">No Cover</span>
              <span v-if="m.is_complete" class="sim-badge">COMPLETE</span>
            </div>
            <div class="sim-body">
              <div class="sim-progress">
                <div class="sim-track"><div class="sim-bar" :style="{ width: (m.progress || 0) + '%' }"></div></div>
                <span class="sim-pct" v-text="m.progress ? m.progress + '%' : ''"></span>
              </div>
              <div class="sim-title" v-text="m.name"></div>
            </div>
          </div>
        </div>
      </template>
    </div>
  `,
};
