/* Downloads to the tablet (2026-11-12), Kinsho Android app only: the app injects window.KinshoDL (KinshoDLBridge.java); in a browser
 * (and in the app's offline copies of the pages) it does not exist, so none of this shows.
 * A Vue mixin for manga_detail.html (chapters) and volume_detail.html (volumes). The host page provides
 *   libraryId, mangaId, selectedItemIds (Set), cancelSelection(), and a method dlList() -> [{id, name}], and may set dlKind ('chapter' | 'volume').
 * The app does the downloading in a foreground service (it goes on when the app is closed); this only asks, and polls KinshoDL.status()
 * once a second for the rows' state: {folder, paused, busy, pendingAll, rows: {unitId: {s: 'done'|'queued'|'active'|'paused', p: 0..100 | -1}}}.
 */
window.kinshoDLMixin = {
  data() {
    return {
      dlKind: 'chapter',
      dlSt: { folder: true, server: true, paused: false, busy: false, pendingAll: false, rows: {} },
      dlConfirm: null,        // { title, text, ok, danger, run }
      dlNoFolder: false,
      dlTimer: null,
    };
  },
  computed: {
    dlAvailable() { return !!window.KinshoDL; },
    dlLabel() { return this.dlKind === 'volume' ? 'volumes' : 'chapters'; },
    dlOne() { return this.dlKind === 'volume' ? 'volume' : 'chapter'; },
    dlIds() { return (this.dlList() || []).map(u => u.id); },
    dlDoneCount() { return this.dlIds.filter(id => (this.dlSt.rows[id] || {}).s === 'done').length; },
    dlMissingCount() { return this.dlIds.length - this.dlDoneCount; },
    dlWorking() {
      return this.dlSt.pendingAll || this.dlIds.some(id => { const r = this.dlSt.rows[id]; return r && r.s !== 'done'; });
    },
    dlTopLabel() {
      if (this.dlWorking) return this.dlSt.paused ? 'Download paused' : 'Downloading…';
      if (this.dlMissingCount === 0 && this.dlDoneCount > 0) return 'Downloaded';
      if (this.dlDoneCount > 0) return 'Download remaining (' + this.dlMissingCount + ')';
      return 'Download';
    },
  },
  methods: {
    dlPoll() {
      if (!window.KinshoDL || document.hidden) return;
      try {
        const j = JSON.parse(KinshoDL.status(String(this.libraryId), String(this.mangaId)));
        if (!j.rows) j.rows = {};
        this.dlSt = j;
      } catch (e) { /* keep the last state */ }
    },
    dlRow(id) { return this.dlSt.rows[id] || null; },
    dlFill(id) { const r = this.dlSt.rows[id]; return !r || r.p < 0 ? '100%' : r.p + '%'; },
    dlStart(ids) {
      let r = 'nofolder';
      try { r = KinshoDL.download(String(this.libraryId), String(this.mangaId), ids ? JSON.stringify(ids) : 'null'); } catch (e) { /* below */ }
      if (r === 'nofolder') { this.dlNoFolder = true; return false; }
      if (r === 'noserver') { alert('The app does not know your server yet: open it once while connected.'); return false; }
      this.dlPoll();
      return true;
    },
    // the button at the top: everything that is missing (confirms first)
    dlTop() {
      if (this.dlWorking || this.dlMissingCount === 0) return;
      if (!this.dlSt.folder) { this.dlNoFolder = true; return; }
      const n = this.dlMissingCount;
      this.dlConfirm = {
        title: 'Download',
        text: 'Download ' + (n === this.dlIds.length ? (n === 1 ? '' : 'all ') + n : 'the ' + n + ' missing') + ' ' + this.dlName(n) + ' to this tablet?',
        ok: 'Download', danger: false, run: () => this.dlStart(null),
      };
    },
    // the delete button at the top: everything downloaded (confirms first)
    dlDeleteAll() {
      const n = this.dlDoneCount;
      if (!n) return;
      this.dlConfirm = {
        title: 'Delete downloads',
        text: 'Delete the ' + n + ' downloaded ' + this.dlName(n) + ' from this tablet? They stay on the server.',
        ok: 'Delete', danger: true, run: () => { KinshoDL.remove(String(this.libraryId), String(this.mangaId), 'null'); this.dlSt.rows = {}; setTimeout(() => this.dlPoll(), 1500); },
      };
    },
    // selection mode: just the chosen ones
    dlSelectedDownload() {
      const ids = [...this.selectedItemIds].filter(id => { const r = this.dlSt.rows[id]; return !r || r.s !== 'done'; });
      if (!ids.length) { this.cancelSelection(); return; }
      if (!this.dlSt.folder) { this.dlNoFolder = true; return; }
      if (this.dlStart(ids)) this.cancelSelection();
    },
    dlSelectedDelete() {
      const ids = [...this.selectedItemIds].filter(id => (this.dlSt.rows[id] || {}).s === 'done');
      if (!ids.length) { this.cancelSelection(); return; }
      this.dlConfirm = {
        title: 'Delete downloads',
        text: 'Delete ' + ids.length + ' downloaded ' + this.dlName(ids.length) + ' from this tablet? They stay on the server.',
        ok: 'Delete', danger: true,
        run: () => { KinshoDL.remove(String(this.libraryId), String(this.mangaId), JSON.stringify(ids)); this.cancelSelection(); setTimeout(() => this.dlPoll(), 1500); },
      };
    },
    dlConfirmRun() { const c = this.dlConfirm; this.dlConfirm = null; if (c && c.run) c.run(); },
    dlName(n) { return n === 1 ? this.dlOne : this.dlLabel; },
    dlPickFolder() { this.dlNoFolder = false; try { KinshoDL.selectFolder(); } catch (e) { /* no bridge */ } },
  },
  mounted() {
    if (!window.KinshoDL) return;
    try { KinshoDL.warm(String(this.libraryId), String(this.mangaId)); } catch (e) { /* an older app */ }
    this.dlPoll();
    this.dlTimer = setInterval(() => this.dlPoll(), 1000);
  },
  beforeUnmount() { if (this.dlTimer) clearInterval(this.dlTimer); },
};
