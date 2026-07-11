const { createApp } = Vue;

createApp({
  data() {
    return {
      // ── SECTION NAV ──
      activeSection: 'general',

      // ── DATA PATH ──
      dataPath: '',
      dataPathSaved: false,
      dataPathStatus: { msg: '', type: '' },

      // ── SERVER CONNECTION ──
      serverUrl:       localStorage.getItem('kinsho_server_url') || '',
      serverUrlStatus: { msg: '', type: '' },
      isNativeApp:     !!(window.__TAURI__ || (window.Capacitor && window.Capacitor.isNative)),
      isLoggedIn:      false,
      loginUsername:   '',
      loginPassword:   '',
      loginStatus:     { msg: '', type: '' },

      // ── LIBRARIES ──
      libraries: [],
      libStatus: { msg: '', type: '' },
      nextId: 1,
      scanStatus: {},
      metaScanStatus: {},
      metaScanFields: { description: true, genres: true, tags: true, cover: true },
      hiddenLibraries: [],
      libraryVisibilityStatus: { msg: '', type: '' },
      pageCounts: {},        // library_id (string) -> {total_pages, total_chapters, total_volumes}
      pageCountsGrand: null, // null while unloaded, so the UI can tell "loading" from "zero"

      // ── USERNAME ──
      username:           '',
      currentPassword:    '',
      newPassword:        '',
      passwordStatus:     { msg: '', type: '' },
      mustChangePassword: false,

      // ── BACKDROP ──
      backdropList:   true,
      backdropDetail: true,
      lockBackdrop:   false,
      backdropStatus: { msg: '', type: '' },

      // ── BLE SCROLLER ──
      hideBleScroller:   true,
      bleScrollerStatus: { msg: '', type: '' },

      // ── COLLECTIONS PREFERENCES ──
      showCollectionsRow:     true,
      hideAdminCollections:   false,
      collectionsPrefsStatus: { msg: '', type: '' },

      // ── ACCENT COLORS ──
      BUILTIN_THEMES: [
        { name: 'Midnight Red',  primary: '#e94560', secondary: '#1d1113', background: '#120b0d', text: '#f0f0f0', bg_image: null },
        { name: 'Ocean Deep',    primary: '#38bdf8', secondary: '#0d1e2e', background: '#060f1c', text: '#e2f0fb', bg_image: null },
        { name: 'Forest Ink',    primary: '#4ade80', secondary: '#141d16', background: '#0b130d', text: '#e6f4ea', bg_image: null },
        { name: 'Amber Noir',    primary: '#f59e0b', secondary: '#1c1608', background: '#0f0c07', text: '#fdf3dc', bg_image: null },
        { name: 'Royal Dusk',    primary: '#a78bfa', secondary: '#1a1228', background: '#0e0a1a', text: '#ede9fe', bg_image: null },
      ],
      activeTheme: 'Midnight Red',
      themeStatus: { msg: '', type: '' },

      // ── VISUAL THEME ──
      activeVisualTheme: 'default',
      visualThemeStatus: { msg: '', type: '' },

      // ── CUSTOM CSS EDITOR ──
      showCssEditor:         false,
      cssEditorName:         '',
      cssEditorContent:      '',
      customThemes:          {},
      activeCustomThemeName: '',
      CSS_EDITOR_DEFAULT_TEMPLATE: [
        '/* ---- Page: Home ---- */',
        '',
        '',
        '/* ---- Page: Manga Detail & Volume Detail ---- */',
        '',
        '',
        '/* ---- Page: Chapter Reader ---- */',
        '',
        '',
        '/* ---- Global ---- */',
        '',
      ].join('\n'),

      // ── ANALYTICS ──
      analyticsLoading:       false,
      analyticsSessions:      [],
      analyticsTotalMinutes:  0,
      analyticsPrecision:     'month',
      analyticsSelectedYear:  new Date().getFullYear(),
      analyticsSelectedMonth: new Date().getMonth() + 1,
      analyticsSelectedDay:   new Date().toISOString().slice(0, 10),
      _graphDragStartX:       null,
      _graphDragDelta:        0,
      // admin reading stats
      adminStatsUsers:        [],
      adminStatsExpanded:     null,
      adminStatsLoading:      false,

      // ── ROLE / MY PERMISSIONS ──
      isAdmin:       false,
      myPermissions: {},

      // ── ADMIN: INTEGRITY ISSUES ──
      integrityIssues:      [],
      integrityIssueCount:  0,
      integrityRechecking:  false,   // Recheck All (bulk) in progress
      recheckingIssueId:    null,    // single-row Recheck currently in progress, if any
      integrityStatus:      { msg: '', type: '' },

      // ── ADMIN: USER PERMISSIONS ──
      allUsers:           [],
      userPermissions:    {},
      expandedUser:       null,
      userSearch:         '',
      permStatus:         { msg: '', type: '' },
      // blocked-tag search per expanded user
      blockedTagInput:       '',
      blockedTagSuggestions: [],
      allTagsList:           [],

      // ── ADMIN: CREATE USER ──
      newUsername:      '',
      newUserPassword:  '',
      newRole:          'user',
      createUserStatus: { msg: '', type: '' },
    };
  },

  computed: {
    visibleLibraries() {
      if (this.isAdmin) return this.libraries;
      const libPerms = (this.myPermissions && this.myPermissions.libraries) || {};
      return this.libraries.filter(lib => libPerms[String(lib.id)] !== false);
    },
    filteredUsers() {
      const q = this.userSearch.trim().toLowerCase();
      if (!q) return this.allUsers;
      return this.allUsers.filter(u => u.username.toLowerCase().includes(q));
    },
    analyticsAvailableYears() {
      const years = new Set(this.analyticsSessions.map(s => new Date(s.start).getFullYear()));
      years.add(new Date().getFullYear());
      return [...years].sort((a, b) => b - a);
    },
    analyticsPeriodTotal() {
      const s = this.analyticsSessions;
      if (this.analyticsPrecision === 'year') {
        return s.filter(e => new Date(e.start).getFullYear() === this.analyticsSelectedYear)
                .reduce((n, e) => n + (e.minutes || 0), 0);
      }
      if (this.analyticsPrecision === 'month') {
        return s.filter(e => {
          const d = new Date(e.start);
          return d.getFullYear() === this.analyticsSelectedYear && d.getMonth() + 1 === this.analyticsSelectedMonth;
        }).reduce((n, e) => n + (e.minutes || 0), 0);
      }
      return s.filter(e => e.start.slice(0, 10) === this.analyticsSelectedDay)
               .reduce((n, e) => n + (e.minutes || 0), 0);
    },
  },

  async mounted() {
    await this.$nextTick();
    this.loadSettings();
    this.loadPageCounts();
    await this.loadAccount();
  },

  methods: {

    goBack() {
      window.location.href = '/';
    },

    saveServerUrl() {
      let url = this.serverUrl.trim();
      if (!url) return;
      if (!url.startsWith('http://') && !url.startsWith('https://')) url = 'http://' + url;
      url = url.replace(/\/$/, '');
      this.serverUrl = url;
      localStorage.setItem('kinsho_server_url', url);
      window.API_BASE = url;
      this.serverUrlStatus = { msg: '✓ Saved.', type: 'ok' };
      setTimeout(() => { this.serverUrlStatus = { msg: '', type: '' }; }, 3000);
    },

    // ── LOGIN FROM SETTINGS (local mode) ──
    async loginFromSettings() {
      const username = this.loginUsername.trim();
      const password = this.loginPassword;
      if (!username || !password) {
        this.loginStatus = { msg: 'Please fill in both fields.', type: 'err' };
        return;
      }
      try {
        const res  = await fetch(apiUrl('/api/auth/login'), {
          method:      'POST',
          headers:     { 'Content-Type': 'application/json' },
          credentials: 'include',
          body:        JSON.stringify({ username, password }),
        });
        const data = await res.json();
        if (data.ok) {
          localStorage.removeItem('kinsho_local_mode');
          window.KINSHO_LOCAL  = false;
          this.loginStatus     = { msg: '✓ Logged in.', type: 'ok' };
          this.loginUsername   = '';
          this.loginPassword   = '';
          // Reload account state
          await this.loadAccount();
        } else {
          this.loginStatus = { msg: data.error || 'Login failed.', type: 'err' };
        }
      } catch {
        this.loginStatus = { msg: 'Could not reach server.', type: 'err' };
      }
      setTimeout(() => { this.loginStatus = { msg: '', type: '' }; }, 3000);
    },

    // ── LOAD ACCOUNT SETTINGS ──
    async loadAccount() {
      // If in local mode with no server configured, skip auth entirely
      if (window.KINSHO_LOCAL && !window.API_BASE) {
        this.isLoggedIn = false;
        await this.loadAnalytics();
        return;
      }
      try {
        const res  = await fetch(apiUrl('/api/auth/me'));
        const data = await res.json();
        if (data.ok) {
          this.isLoggedIn         = true;
          this.username           = data.username;
          this.isAdmin            = data.role === 'admin';
          this.myPermissions      = data.permissions || {};
          this.mustChangePassword = !!data.must_change_password;
        } else {
          this.isLoggedIn = false;
          this.username   = '';
        }
      } catch (e) {
        this.isLoggedIn = false;
        this.username   = '';
        console.error('Failed to load account info:', e);
      }
      if (this.isLoggedIn && this.isAdmin) {
        await this.loadUserPermissions();
        await this.loadAdminStats();
        await this.loadIntegrityIssues();
      }
      await this.loadAnalytics();
    },
 
    async changePassword() {
      const current = this.currentPassword.trim();
      const next    = this.newPassword.trim();
      if (!current || !next) return;
 
      try {
        const res  = await fetch(apiUrl('/api/auth/change-password'), {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ current_password: current, new_password: next }),
        });
        const data = await res.json();
        if (data.ok) {
          // The password change just invalidated this session server-side
          // (every session, including this one) — send the user to a real
          // login with the new password instead of continuing on a session
          // that's already dead, same as logout()/logoutEverywhere().
          window.location.href = '/login';
          return;
        } else {
          this.passwordStatus = { msg: data.error || 'Something went wrong.', type: 'err' };
        }
      } catch (e) {
        this.passwordStatus = { msg: 'Could not reach server.', type: 'err' };
      }
      setTimeout(() => { this.passwordStatus = { msg: '', type: '' }; }, 3000);
    },
 
    async logout() {
      await fetch(apiUrl('/api/auth/logout'), { method: 'POST' });
      window.location.href = '/login';
    },

    async logoutEverywhere() {
      if (!confirm('End every other logged-in session for this account?')) return;
      await fetch(apiUrl('/api/auth/logout-everywhere'), { method: 'POST' });
      window.location.href = '/login';
    },

    // ── LOAD SETTINGS FROM API ──
    async loadSettings() {
      try {
        const res = await fetch(apiUrl('/api/settings'));
        const data = await res.json();

        if (data.data_path) {
          this.dataPath = data.data_path;
          this.dataPathSaved = true;
        }

        if (data.libraries && data.libraries.length > 0) {
          this.libraries = data.libraries.map(lib => ({
            ...lib,
            paths: lib.paths ?? (lib.path ? [lib.path] : ['']),
          }));
          this.nextId = data.libraries.length > 0 ? Math.max(...data.libraries.map(l => l.id)) + 1 : 1;
        }
        this.activeTheme = data.active_theme || 'Midnight Red';
        if (!this.BUILTIN_THEMES.find(t => t.name === this.activeTheme)) {
          this.activeTheme = 'Midnight Red';
        }
        this.backdropList           = data.backdrop_list            !== false;
        this.backdropDetail         = data.backdrop_detail          !== false;
        this.lockBackdrop           = data.lock_backdrop            === true;
        this.hideBleScroller        = data.hide_ble_scroller         !== false;
        this.hiddenLibraries        = (data.hidden_libraries || []).map(String);
        this.showCollectionsRow     = data.show_collections_row     !== false;
        this.hideAdminCollections   = data.hide_admin_collections   === true;
        this.activeVisualTheme      = data.active_visual_theme      || 'default';
        this.activeCustomThemeName  = data.active_custom_theme_name || '';
        this.customThemes           = data.custom_themes            || {};
        if (this.activeVisualTheme === 'custom' && this.activeCustomThemeName && this.customThemes[this.activeCustomThemeName]) {
          this.applyCustomThemeLocally(this.customThemes[this.activeCustomThemeName]);
        }
      } catch (e) {
        console.error('Failed to load settings:', e);
      }
    },

    // ── PAGE/CHAPTER/VOLUME COUNTS (Libraries section) ──
    async loadPageCounts() {
      try {
        const res  = await fetch(apiUrl('/api/settings/page-counts'));
        const data = await res.json();
        const byLib = {};
        for (const lib of (data.libraries || [])) {
          byLib[String(lib.library_id)] = {
            pages:    lib.total_pages,
            chapters: lib.total_chapters,
            volumes:  lib.total_volumes,
          };
        }
        this.pageCounts = byLib;
        this.pageCountsGrand = {
          pages:    data.grand_total_pages    ?? 0,
          chapters: data.grand_total_chapters ?? 0,
          volumes:  data.grand_total_volumes  ?? 0,
        };
      } catch (e) {
        console.error('Failed to load page counts:', e);
      }
    },

    // ── SAVE DATA PATH ──
    async saveDataPath() {
      const path = this.dataPath.trim();
      if (!path) return;

      try {
        const res = await fetch(apiUrl('/api/settings/data-path'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ data_path: path }),
        });
        const data = await res.json();

        if (data.ok) {
          this.dataPathSaved = true;
          this.dataPathStatus = { msg: '✓ Saved successfully.', type: 'ok' };
        } else {
          this.dataPathStatus = { msg: 'Something went wrong.', type: 'err' };
        }
      } catch (e) {
        this.dataPathStatus = { msg: 'Could not reach server.', type: 'err' };
      }

      // Clear status after 3 seconds
      setTimeout(() => { this.dataPathStatus = { msg: '', type: '' }; }, 3000);
    },

    // ── ADD LIBRARY ──
    addLibrary() {
      this.libraries.push({ id: this.nextId++, name: '', paths: [''] });
    },

    addPath(lib) {
      lib.paths.push('');
    },

    async removePath(lib, pathIndex) {
        if (lib.paths.length <= 1) return;
        const removedPath = lib.paths[pathIndex];
        lib.paths.splice(pathIndex, 1);
        try {
            await fetch(apiUrl('/api/settings/libraries'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ libraries: this.libraries }),
            });
            await fetch(apiUrl('/api/libraries/remove-path-covers'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ library_id: lib.id, removed_path: removedPath }),
            });
        } catch (e) {
            console.error('Failed to remove path:', e);
        }
    },

    // ── REMOVE LIBRARY ──
    async removeLibrary(index) {
      this.libraries.splice(index, 1);
      try {
        await fetch(apiUrl('/api/settings/libraries'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ libraries: this.libraries }),
        });
      } catch (e) {
        console.error('Failed to persist library removal:', e);
      }
    },

    // ── TRIGGER SCAN FOR ONE LIBRARY ──
    async scanLibrary(lib) {
        this.scanStatus = { ...this.scanStatus, [lib.id]: { msg: 'Scanning...', type: 'scanning' } };
        try {
            await fetch(apiUrl(`/api/scan/${lib.id}`), { method: 'POST' });
            this.pollScanStatus(lib.id);
        } catch (e) {
            this.scanStatus = { ...this.scanStatus, [lib.id]: { msg: 'Scan failed.', type: 'err' } };
        }
    },

    // ── BULK METADATA SCAN FOR ONE LIBRARY ──
    async scanLibraryMetadata(lib) {
        const fields = Object.entries(this.metaScanFields).filter(([, v]) => v).map(([k]) => k);
        if (fields.length === 0) {
            this.metaScanStatus = { ...this.metaScanStatus, [lib.id]: { msg: 'Select at least one field to import.', type: 'err' } };
            return;
        }
        this.metaScanStatus = { ...this.metaScanStatus, [lib.id]: { msg: 'Scanning…', type: 'scanning' } };
        try {
            const res  = await fetch(apiUrl(`/api/libraries/${lib.id}/scan-metadata`), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ fields }),
            });
            const data = await res.json();
            if (data.error) {
                this.metaScanStatus = { ...this.metaScanStatus, [lib.id]: { msg: data.error, type: 'err' } };
            } else {
                const parts = [`✓ ${data.auto_matched} matched`];
                if (data.no_match  > 0) parts.push(`${data.no_match} unresolved`);
                if (data.skipped   > 0) parts.push(`${data.skipped} skipped`);
                if (data.errors    > 0) parts.push(`${data.errors} errors`);
                this.metaScanStatus = { ...this.metaScanStatus, [lib.id]: { msg: parts.join(' · '), type: 'ok' } };
            }
        } catch (e) {
            this.metaScanStatus = { ...this.metaScanStatus, [lib.id]: { msg: 'Scan failed.', type: 'err' } };
        }
    },

    // ── POLL SCAN STATUS ──
    pollScanStatus(libraryId) {
        const interval = setInterval(async () => {
            try {
                const res = await fetch(apiUrl(`/api/scan/${libraryId}/status`));
                const data = await res.json();
                if (!data.running && data.scanned) {
                    const d = new Date(data.last_scanned);
                    const timeStr = d.toLocaleTimeString();
                    this.scanStatus = {
                        ...this.scanStatus,
                        [libraryId]: { msg: `✓ ${data.manga_count} mangas found — scanned at ${timeStr}`, type: 'ok' }
                    };
                    this.loadPageCounts();
                    clearInterval(interval);
                }
            } catch (e) {
                clearInterval(interval);
            }
        }, 1500);
    },

    // ── SAVE LIBRARIES ──
    async saveLibraries() {
      try {
        const res = await fetch(apiUrl('/api/settings/libraries'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ libraries: this.libraries }),
        });
        const data = await res.json();

        if (data.ok) {
          this.libStatus = { msg: '✓ Libraries saved.', type: 'ok' };
          for (const lib of this.libraries) {
            await this.scanLibrary(lib);
          }
        } else {
          this.libStatus = { msg: 'Something went wrong.', type: 'err' };
        }
      } catch (e) {
        this.libStatus = { msg: 'Could not reach server.', type: 'err' };
      }

      setTimeout(() => { this.libStatus = { msg: '', type: '' }; }, 3000);
    },

    async saveThemes() {
      const BUILTIN_THEMES = [
        { name: 'Midnight Red',  primary: '#e94560', secondary: '#1d1113', background: '#120b0d', text: '#f0f0f0' },
        { name: 'Ocean Deep',    primary: '#38bdf8', secondary: '#0d1e2e', background: '#060f1c', text: '#e2f0fb' },
        { name: 'Forest Ink',    primary: '#4ade80', secondary: '#141d16', background: '#0b130d', text: '#e6f4ea' },
        { name: 'Amber Noir',    primary: '#f59e0b', secondary: '#1c1608', background: '#0f0c07', text: '#fdf3dc' },
        { name: 'Royal Dusk',    primary: '#a78bfa', secondary: '#1a1228', background: '#0e0a1a', text: '#ede9fe' },
      ];
      const theme = BUILTIN_THEMES.find(t => t.name === this.activeTheme) || BUILTIN_THEMES[0];
      const r = document.documentElement;
      r.style.setProperty('--color-primary',    theme.primary);
      r.style.setProperty('--color-secondary',  theme.secondary);
      r.style.setProperty('--color-background', theme.background);
      r.style.setProperty('--color-text',       theme.text);
      r.style.setProperty('--color-border',     theme.secondary + 'aa');
      r.style.setProperty('--color-muted',      theme.text + '66');
      document.body.style.background = theme.background;
      try {
        const res = await fetch(apiUrl('/api/settings/themes'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ themes: this.BUILTIN_THEMES, active_theme: this.activeTheme }),
        });
        const data = await res.json();
        if (data.ok) {
          this.themeStatus = { msg: '✓ Theme saved.', type: 'ok' };
        } else {
          this.themeStatus = { msg: 'Something went wrong.', type: 'err' };
        }
      } catch (e) {
        this.themeStatus = { msg: 'Could not reach server.', type: 'err' };
      }
      setTimeout(() => { this.themeStatus = { msg: '', type: '' }; }, 3000);
    },

    async saveBackdrop(key) {
      try {
        const body = key === 'list'   ? { backdrop_list:   this.backdropList }
          : key === 'detail'          ? { backdrop_detail: this.backdropDetail }
          : { lock_backdrop: this.lockBackdrop };
        const res  = await fetch(apiUrl('/api/settings/backdrop'), {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify(body),
        });
        const data = await res.json();
        this.backdropStatus = data.ok
          ? { msg: '✓ Saved.', type: 'ok' }
          : { msg: 'Something went wrong.', type: 'err' };
      } catch (e) {
        this.backdropStatus = { msg: 'Could not reach server.', type: 'err' };
      }
      setTimeout(() => { this.backdropStatus = { msg: '', type: '' }; }, 2000);
    },

    async saveBleScrollerPref() {
      try {
        const res  = await fetch(apiUrl('/api/settings/ble-scroller'), {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ hide_ble_scroller: this.hideBleScroller }),
        });
        const data = await res.json();
        this.bleScrollerStatus = data.ok
          ? { msg: '✓ Saved.', type: 'ok' }
          : { msg: 'Something went wrong.', type: 'err' };
      } catch (e) {
        this.bleScrollerStatus = { msg: 'Could not reach server.', type: 'err' };
      }
      setTimeout(() => { this.bleScrollerStatus = { msg: '', type: '' }; }, 2000);
    },

    isLibraryHidden(lib) {
      return this.hiddenLibraries.includes(String(lib.id));
    },

    async toggleLibraryVisibility(lib) {
      const key    = String(lib.id);
      const hidden = !this.hiddenLibraries.includes(key);
      if (hidden) this.hiddenLibraries.push(key);
      else this.hiddenLibraries = this.hiddenLibraries.filter(id => id !== key);
      try {
        const res  = await fetch(apiUrl('/api/settings/library-visibility'), {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ library_id: lib.id, hidden }),
        });
        const data = await res.json();
        this.libraryVisibilityStatus = data.ok
          ? { msg: '✓ Saved.', type: 'ok' }
          : { msg: 'Something went wrong.', type: 'err' };
      } catch (e) {
        this.libraryVisibilityStatus = { msg: 'Could not reach server.', type: 'err' };
      }
      setTimeout(() => { this.libraryVisibilityStatus = { msg: '', type: '' }; }, 2000);
    },

    async saveCollectionsPrefs() {
      try {
        const res  = await fetch(apiUrl('/api/settings/collections-prefs'), {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({
            show_collections_row:   this.showCollectionsRow,
            hide_admin_collections: this.hideAdminCollections,
          }),
        });
        const data = await res.json();
        this.collectionsPrefsStatus = data.ok
          ? { msg: '✓ Saved.', type: 'ok' }
          : { msg: 'Something went wrong.', type: 'err' };
      } catch (e) {
        this.collectionsPrefsStatus = { msg: 'Could not reach server.', type: 'err' };
      }
      setTimeout(() => { this.collectionsPrefsStatus = { msg: '', type: '' }; }, 2000);
    },

    async saveVisualTheme() {
      document.documentElement.setAttribute('data-theme', this.activeVisualTheme);
      // Remove any custom style when switching away from custom
      if (this.activeVisualTheme !== 'custom') {
        document.getElementById('custom-theme-style')?.remove();
      }
      try {
        const res  = await fetch(apiUrl('/api/settings/visual-theme'), {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ active_visual_theme: this.activeVisualTheme }),
        });
        const data = await res.json();
        this.visualThemeStatus = data.ok
          ? { msg: '✓ Saved.', type: 'ok' }
          : { msg: 'Something went wrong.', type: 'err' };
      } catch (e) {
        this.visualThemeStatus = { msg: 'Could not reach server.', type: 'err' };
      }
      setTimeout(() => { this.visualThemeStatus = { msg: '', type: '' }; }, 2000);
    },

    onCustomThemeClick() {
      // Always open the editor when Custom radio is clicked
      this.activeVisualTheme = 'custom';
      this.openCssEditor();
    },

    openCssEditor() {
      // Pre-load active custom theme if one exists, otherwise start blank
      if (this.activeCustomThemeName && this.customThemes[this.activeCustomThemeName]) {
        this.cssEditorName    = this.activeCustomThemeName;
        this.cssEditorContent = this.customThemes[this.activeCustomThemeName];
      } else {
        this.cssEditorName    = '';
        this.cssEditorContent = this.CSS_EDITOR_DEFAULT_TEMPLATE;
      }
      this.showCssEditor = true;
    },

    loadCustomTheme(name) {
      this.cssEditorName    = name;
      this.cssEditorContent = this.customThemes[name] || '';
    },

    async saveCssTheme() {
      const name = this.cssEditorName.trim();
      if (!name) return;
      const css = this.cssEditorContent;
      try {
        await fetch(apiUrl('/api/settings/custom-themes'), {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ name, css }),
        });
        this.customThemes = { ...this.customThemes, [name]: css };
        this.activeCustomThemeName = name;
        this.activeVisualTheme     = 'custom';
        this.applyCustomThemeLocally(css);
        await fetch(apiUrl('/api/settings/visual-theme'), {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ active_visual_theme: 'custom', active_custom_theme_name: name }),
        });
        this.showCssEditor = false;
      } catch (e) {
        console.error('Failed to save custom theme:', e);
      }
    },

    async deleteCssTheme(name) {
      await fetch(apiUrl(`/api/settings/custom-themes/${encodeURIComponent(name)}`), { method: 'DELETE' });
      const updated = { ...this.customThemes };
      delete updated[name];
      this.customThemes = updated;
      if (this.activeCustomThemeName === name) {
        this.activeCustomThemeName = '';
        this.activeVisualTheme     = 'default';
        document.documentElement.setAttribute('data-theme', 'default');
        document.getElementById('custom-theme-style')?.remove();
        await fetch(apiUrl('/api/settings/visual-theme'), {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ active_visual_theme: 'default' }),
        });
      }
      // Reset editor to empty
      this.cssEditorName    = '';
      this.cssEditorContent = this.CSS_EDITOR_DEFAULT_TEMPLATE;
    },

    applyCustomThemeLocally(css) {
      let el = document.getElementById('custom-theme-style');
      if (!el) {
        el = document.createElement('style');
        el.id = 'custom-theme-style';
        document.head.appendChild(el);
      }
      el.textContent = css;
      document.documentElement.setAttribute('data-theme', 'custom');
    },

    async resetEditorToDefault() {
      this.cssEditorContent = this.CSS_EDITOR_DEFAULT_TEMPLATE;
    },

    async resetEditorToSharp() {
      try {
        const res = await fetch(apiUrl('/static/style.css'));
        this.cssEditorContent = await res.text();
      } catch (e) {
        console.error('Failed to fetch Sharp CSS:', e);
      }
    },

    async resetEditorToAbyss() {
      try {
        const res = await fetch(apiUrl('/static/theme-abyss.css'));
        this.cssEditorContent = await res.text();
      } catch (e) {
        console.error('Failed to fetch Abyss CSS:', e);
      }
    },

    // ── ANALYTICS ──
    async loadAnalytics() {
      this.analyticsLoading = true;
      try {
        const res  = await fetch(apiUrl('/api/reading/stats'));
        const data = await res.json();
        if (data.ok) {
          this.analyticsSessions    = data.sessions     || [];
          this.analyticsTotalMinutes = data.total_minutes || 0;
        }
      } catch (e) {
        console.error('Failed to load analytics:', e);
      }
      this.analyticsLoading = false;
      await this.$nextTick();
      this.renderGraph();
    },

    formatMinutes(mins) {
      if (!mins) return '0 m';
      if (mins < 60) return mins + ' m';
      const h = Math.floor(mins / 60);
      const m = mins % 60;
      return m > 0 ? `${h} h ${m} m` : `${h} h`;
    },

    monthName(m) {
      return ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][m - 1];
    },

    onPrecisionChange() {
      this.$nextTick(() => this.renderGraph());
    },

    analyticsPrev() {
      if (this.analyticsPrecision === 'day') {
        const d = new Date(this.analyticsSelectedDay);
        d.setDate(d.getDate() - 1);
        this.analyticsSelectedDay = d.toISOString().slice(0, 10);
      } else if (this.analyticsPrecision === 'month') {
        if (this.analyticsSelectedMonth === 1) { this.analyticsSelectedMonth = 12; this.analyticsSelectedYear--; }
        else this.analyticsSelectedMonth--;
      } else {
        this.analyticsSelectedYear--;
      }
      this.$nextTick(() => this.renderGraph());
    },

    analyticsNext() {
      if (this.analyticsPrecision === 'day') {
        const d = new Date(this.analyticsSelectedDay);
        d.setDate(d.getDate() + 1);
        this.analyticsSelectedDay = d.toISOString().slice(0, 10);
      } else if (this.analyticsPrecision === 'month') {
        if (this.analyticsSelectedMonth === 12) { this.analyticsSelectedMonth = 1; this.analyticsSelectedYear++; }
        else this.analyticsSelectedMonth++;
      } else {
        this.analyticsSelectedYear++;
      }
      this.$nextTick(() => this.renderGraph());
    },

    onGraphDragStart(e) { this._graphDragStartX = e.clientX; },
    onGraphDragMove()   { /* tracking handled on end */ },
    onGraphDragEnd(e) {
      if (this._graphDragStartX === null) return;
      const delta = e.clientX - this._graphDragStartX;
      this._graphDragStartX = null;
      if (Math.abs(delta) > 40) { if (delta > 0) this.analyticsPrev(); else this.analyticsNext(); }
    },
    onGraphTouchStart(e) { this._graphDragStartX = e.touches[0].clientX; },
    onGraphTouchMove()   { /* tracking handled on end */ },
    onGraphTouchEnd(e) {
      if (this._graphDragStartX === null) return;
      const delta = e.changedTouches[0].clientX - this._graphDragStartX;
      this._graphDragStartX = null;
      if (Math.abs(delta) > 40) { if (delta > 0) this.analyticsPrev(); else this.analyticsNext(); }
    },

    renderGraph() {
      const canvas = this.$refs.analyticsCanvas;
      if (!canvas) return;
      const parent = canvas.parentElement;
      const cssW   = parent.getBoundingClientRect().width || parent.offsetWidth || 400;
      const cssH   = 220;
      const dpr    = window.devicePixelRatio || 1;
      canvas.width        = Math.round(cssW * dpr);
      canvas.height       = Math.round(cssH * dpr);
      canvas.style.width  = cssW + 'px';
      canvas.style.height = cssH + 'px';
      const ctx = canvas.getContext('2d');
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, cssW, cssH);
      const st      = getComputedStyle(document.documentElement);
      const primary = st.getPropertyValue('--color-primary').trim()  || '#e94560';
      const textCol = st.getPropertyValue('--color-text').trim()     || '#f0f0f0';
      const muted   = st.getPropertyValue('--color-muted').trim()    || '#888888';
      const border  = st.getPropertyValue('--color-border').trim()   || '#2a2a2a';
      const pL = 44, pR = 16, pT = 20, pB = 28;
      const W  = cssW - pL - pR;
      const H  = cssH - pT - pB;
      if (W <= 0 || H <= 0) return;
      if      (this.analyticsPrecision === 'month') this._renderMonthGraph(ctx, primary, textCol, muted, border, pL, pT, W, H);
      else if (this.analyticsPrecision === 'year')  this._renderYearGraph (ctx, primary, textCol, muted, border, pL, pT, W, H);
      else                                           this._renderDayGraph  (ctx, primary, textCol, muted, border, pL, pT, W, H);
    },

    _drawGrid(ctx, muted, border, pL, pT, W, H, maxVal, steps = 4) {
      ctx.strokeStyle = border;
      ctx.lineWidth   = 0.5;
      ctx.fillStyle   = muted;
      ctx.font        = '9px Segoe UI, sans-serif';
      ctx.textAlign   = 'right';
      for (let i = 0; i <= steps; i++) {
        const y = pT + H - (i / steps) * H;
        ctx.beginPath(); ctx.moveTo(pL, y); ctx.lineTo(pL + W, y); ctx.stroke();
        ctx.fillText(Math.round((i / steps) * maxVal) + 'm', pL - 4, y + 3);
      }
    },

    _drawLine(ctx, primary, textCol, points) {
      if (points.length < 2) return;
      ctx.strokeStyle = primary;
      ctx.lineWidth   = 2;
      ctx.lineJoin    = 'round';
      ctx.beginPath();
      points.forEach((p, i) => { i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y); });
      ctx.stroke();
      for (const p of points) {
        if (p.v === 0) continue;
        ctx.fillStyle = primary;
        ctx.beginPath(); ctx.arc(p.x, p.y, 3, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = textCol;
        ctx.font      = 'bold 9px Segoe UI, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(p.v + 'm', p.x, p.y - 7);
      }
    },

    _renderYearGraph(ctx, primary, textCol, muted, border, pL, pT, W, H) {
      const year    = this.analyticsSelectedYear;
      const byMonth = Array(12).fill(0);
      for (const s of this.analyticsSessions) {
        const d = new Date(s.start);
        if (d.getFullYear() === year) byMonth[d.getMonth()] += s.minutes || 0;
      }
      const maxVal = Math.max(...byMonth, 1);
      // Grid lines
      const steps = 4;
      ctx.font      = '9px Segoe UI, sans-serif';
      ctx.textAlign = 'right';
      for (let i = 0; i <= steps; i++) {
        const y = pT + H - (i / steps) * H;
        ctx.strokeStyle = border; ctx.lineWidth = 0.5;
        ctx.beginPath(); ctx.moveTo(pL, y); ctx.lineTo(pL + W, y); ctx.stroke();
        ctx.fillStyle = muted;
        ctx.fillText(Math.round((i / steps) * maxVal) + 'm', pL - 4, y + 3);
      }
      // Bars
      const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
      const gap    = Math.max(2, W / 12 * 0.2);
      const barW   = Math.max(4, W / 12 - gap);
      for (let i = 0; i < 12; i++) {
        const v  = byMonth[i];
        const cx = pL + (i + 0.5) / 12 * W;
        const x  = cx - barW / 2;
        const bH = Math.max(v > 0 ? 2 : 0, (v / maxVal) * H);
        const y  = pT + H - bH;
        ctx.fillStyle = primary + 'cc';
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(x, y, barW, bH, [3, 3, 0, 0]);
        else ctx.rect(x, y, barW, bH);
        ctx.fill();
        if (v > 0) {
          ctx.fillStyle = textCol;
          ctx.font      = 'bold 8px Segoe UI, sans-serif';
          ctx.textAlign = 'center';
          ctx.fillText(v + 'm', cx, y - 4);
        }
        ctx.fillStyle = muted;
        ctx.font      = '9px Segoe UI, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(MONTHS[i], cx, pT + H + 14);
      }
      if (byMonth.every(v => v === 0)) {
        ctx.fillStyle = muted; ctx.font = '13px Segoe UI, sans-serif'; ctx.textAlign = 'center';
        ctx.fillText('No reading this year', pL + W / 2, pT + H / 2);
      }
    },

    _renderMonthGraph(ctx, primary, textCol, muted, border, pL, pT, W, H) {
      const year  = this.analyticsSelectedYear;
      const month = this.analyticsSelectedMonth;
      const days  = new Date(year, month, 0).getDate();
      const byDay = Array(days).fill(0);
      for (const s of this.analyticsSessions) {
        const d = new Date(s.start);
        if (d.getFullYear() === year && d.getMonth() + 1 === month)
          byDay[d.getDate() - 1] += s.minutes || 0;
      }
      const maxVal = Math.max(...byDay, 1);
      // Grid lines
      const steps = 4;
      ctx.font      = '9px Segoe UI, sans-serif';
      ctx.textAlign = 'right';
      for (let i = 0; i <= steps; i++) {
        const y = pT + H - (i / steps) * H;
        ctx.strokeStyle = border; ctx.lineWidth = 0.5;
        ctx.beginPath(); ctx.moveTo(pL, y); ctx.lineTo(pL + W, y); ctx.stroke();
        ctx.fillStyle = muted;
        ctx.fillText(Math.round((i / steps) * maxVal) + 'm', pL - 4, y + 3);
      }
      // Bars
      const gap  = Math.max(1, W / days * 0.15);
      const barW = Math.max(2, W / days - gap);
      const step = days > 20 ? 5 : days > 10 ? 2 : 1;
      for (let i = 0; i < days; i++) {
        const v  = byDay[i];
        const cx = pL + (i + 0.5) / days * W;
        const x  = cx - barW / 2;
        const bH = Math.max(v > 0 ? 2 : 0, (v / maxVal) * H);
        const y  = pT + H - bH;
        ctx.fillStyle = primary + 'cc';
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(x, y, barW, bH, [3, 3, 0, 0]);
        else ctx.rect(x, y, barW, bH);
        ctx.fill();
        if (v > 0 && barW >= 8) {
          ctx.fillStyle = textCol;
          ctx.font      = 'bold 8px Segoe UI, sans-serif';
          ctx.textAlign = 'center';
          ctx.fillText(v + 'm', cx, y - 4);
        }
        if (i % step === 0 || i === days - 1) {
          ctx.fillStyle = muted;
          ctx.font      = '9px Segoe UI, sans-serif';
          ctx.textAlign = 'center';
          ctx.fillText(i + 1, cx, pT + H + 14);
        }
      }
      if (byDay.every(v => v === 0)) {
        ctx.fillStyle = muted; ctx.font = '13px Segoe UI, sans-serif'; ctx.textAlign = 'center';
        ctx.fillText('No reading this month', pL + W / 2, pT + H / 2);
      }
    },

    _renderDayGraph(ctx, primary, textCol, muted, border, pL, pT, W, H) {
      const dayStr   = this.analyticsSelectedDay;
      const sessions = this.analyticsSessions.filter(s => s.start.slice(0, 10) === dayStr);
      // Vertical grid lines per hour
      ctx.lineWidth = 0.5;
      for (let h = 0; h <= 24; h++) {
        const x = pL + (h / 24) * W;
        ctx.strokeStyle = (h % 6 === 0) ? border : border + '55';
        ctx.beginPath(); ctx.moveTo(x, pT); ctx.lineTo(x, pT + H); ctx.stroke();
        if (h % 3 === 0) {
          ctx.fillStyle = muted; ctx.font = '9px Segoe UI, sans-serif'; ctx.textAlign = 'center';
          ctx.fillText(h + ':00', x, pT + H + 14);
        }
      }
      // Baseline
      ctx.strokeStyle = border; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(pL, pT + H); ctx.lineTo(pL + W, pT + H); ctx.stroke();
      // Bars
      const barH = Math.max(24, Math.min(40, H * 0.55));
      const barY  = pT + (H - barH) / 2;
      for (const s of sessions) {
        const sd    = new Date(s.start);
        const sMins = sd.getHours() * 60 + sd.getMinutes();
        const eMins = Math.min(sMins + (s.minutes || 0), 24 * 60);
        const x1    = pL + (sMins / 1440) * W;
        const x2    = pL + (eMins / 1440) * W;
        const bW    = Math.max(x2 - x1, 2);
        ctx.fillStyle = primary + 'cc';
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(x1, barY, bW, barH, 4);
        else ctx.rect(x1, barY, bW, barH);
        ctx.fill();
        if (bW > 16) {
          ctx.save();
          ctx.beginPath(); ctx.rect(x1, barY, bW, barH); ctx.clip();
          const maxChars = Math.max(1, Math.floor(bW / 6.5));
          const label    = s.manga_name.length > maxChars
            ? s.manga_name.slice(0, maxChars - 1) + '…'
            : s.manga_name;
          ctx.fillStyle = textCol; ctx.font = 'bold 9px Segoe UI, sans-serif'; ctx.textAlign = 'left';
          ctx.fillText(label, x1 + 4, barY + barH / 2 + 3.5);
          ctx.restore();
        }
      }
      if (sessions.length === 0) {
        ctx.fillStyle = muted; ctx.font = '13px Segoe UI, sans-serif'; ctx.textAlign = 'center';
        ctx.fillText('No reading sessions this day', pL + W / 2, pT + H / 2);
      }
    },

    // ── ADMIN READING STATS ──
    async loadAdminStats() {
      this.adminStatsLoading = true;
      try {
        const res  = await fetch(apiUrl('/api/reading/stats/all-users'));
        const data = await res.json();
        if (data.ok) this.adminStatsUsers = data.users;
      } catch (e) {
        console.error('Failed to load admin stats:', e);
      }
      this.adminStatsLoading = false;
    },

    toggleAdminStatsUser(username) {
      this.adminStatsExpanded = this.adminStatsExpanded === username ? null : username;
    },

    // ── ADMIN: INTEGRITY ISSUES ──
    libraryName(libraryId) {
      const lib = this.libraries.find(l => l.id === libraryId);
      return lib ? lib.name : `Library ${libraryId}`;
    },

    async loadIntegrityIssues() {
      try {
        const res  = await fetch(apiUrl('/api/admin/integrity/issues'));
        const data = await res.json();
        this.integrityIssues     = data.issues || [];
        this.integrityIssueCount = data.count || 0;
      } catch (e) {
        console.error('Failed to load integrity issues:', e);
      }
    },

    async recheckIssue(issueId) {
      // Own flag, separate from integrityRechecking (the Recheck All / bulk
      // flag) — otherwise a single-row click made the "Recheck All" button
      // itself flip to "Rechecking…", making it look like the whole batch
      // had been kicked off by one row's click.
      this.recheckingIssueId = issueId;
      this.integrityStatus   = { msg: '', type: '' };
      try {
        const res  = await fetch(apiUrl('/api/admin/integrity/recheck'), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ issue_ids: [issueId] }),
        });
        const data = await res.json();
        this.integrityIssues     = data.issues || [];
        this.integrityIssueCount = data.count || 0;
        this.integrityStatus     = { msg: '✓ Rechecked.', type: 'ok' };
      } catch (e) {
        this.integrityStatus = { msg: 'Recheck failed.', type: 'err' };
      }
      this.recheckingIssueId = null;
      setTimeout(() => { this.integrityStatus = { msg: '', type: '' }; }, 3000);
    },

    async recheckAllIssues() {
      // Walks the list issue-by-issue (same request shape as a single Recheck
      // click) rather than one giant backend call — gives visible progress and
      // means one bad/slow item can't stall the whole thing silently. Runs a
      // small pool of these concurrently instead of one at a time so waiting
      // on I/O (a slow/network drive) for one item overlaps with the others
      // instead of serializing the whole batch.
      const CONCURRENCY = 4;
      this.integrityRechecking = true;
      const ids = this.integrityIssues.map(i => i.id);
      const total = ids.length;
      let cursor = 0;
      let done = 0;

      const worker = async () => {
        while (cursor < ids.length) {
          const id = ids[cursor++];
          // A chapter/volume can carry more than one issue row; rechecking one
          // already re-checks and clears every row for that same item, so a
          // later id in this snapshot may already be gone — skip it if so.
          if (this.integrityIssues.some(iss => iss.id === id)) {
            try {
              const res  = await fetch(apiUrl('/api/admin/integrity/recheck'), {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ issue_ids: [id] }),
              });
              const data = await res.json();
              this.integrityIssues     = data.issues || [];
              this.integrityIssueCount = data.count || 0;
            } catch (e) {
              console.error('Recheck failed for issue', id, e);
            }
          }
          done++;
          this.integrityStatus = { msg: `Rechecking ${done} of ${total}…`, type: 'scanning' };
        }
      };

      await Promise.all(Array.from({ length: Math.min(CONCURRENCY, ids.length) }, worker));

      this.integrityStatus = { msg: '✓ Recheck complete.', type: 'ok' };
      this.integrityRechecking = false;
      setTimeout(() => { this.integrityStatus = { msg: '', type: '' }; }, 4000);
    },

    async dismissIssue(issueId) {
      try {
        const res  = await fetch(apiUrl('/api/admin/integrity/dismiss'), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ issue_id: issueId }),
        });
        const data = await res.json();
        this.integrityIssues     = this.integrityIssues.filter(i => i.id !== issueId);
        this.integrityIssueCount = data.count || 0;
      } catch (e) {
        console.error('Failed to dismiss issue:', e);
      }
    },

    async dismissAllIssues() {
      if (!confirm(`Dismiss all ${this.integrityIssues.length} issue(s)? This just clears the list — it doesn't fix anything.`)) return;
      try {
        const res  = await fetch(apiUrl('/api/admin/integrity/dismiss-all'), { method: 'POST' });
        const data = await res.json();
        this.integrityIssues     = [];
        this.integrityIssueCount = data.count || 0;
      } catch (e) {
        console.error('Failed to dismiss all issues:', e);
      }
    },

    // ── CREATE USER (admin only) ──
    async createUser() {
      const username = this.newUsername.trim().toLowerCase();
      const password = this.newUserPassword;
      if (!username || !password) {
        this.createUserStatus = { msg: 'Username and password required.', type: 'err' };
        return;
      }
      try {
        const res  = await fetch(apiUrl('/api/admin/users'), {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ username, password, role: this.newRole }),
        });
        const data = await res.json();
        if (data.ok) {
          this.createUserStatus = { msg: `✓ Created ${data.username}.`, type: 'ok' };
          this.newUsername = '';
          this.newUserPassword = '';
          this.newRole = 'user';
          await this.loadUserPermissions();
        } else {
          this.createUserStatus = { msg: data.error || 'Could not create user.', type: 'err' };
        }
      } catch (e) {
        this.createUserStatus = { msg: 'Could not reach server.', type: 'err' };
      }
      setTimeout(() => { this.createUserStatus = { msg: '', type: '' }; }, 3000);
    },

    // ── LOAD USER PERMISSIONS (admin only) ──
    async loadUserPermissions() {
      try {
        const res  = await fetch(apiUrl('/api/admin/permissions'));
        const data = await res.json();
        if (!data.ok) return;
        this.allUsers = data.users;
        const pm = { '_default': { ...data.default } };
        if (!Array.isArray(pm['_default'].blocked_tags)) pm['_default'].blocked_tags = [];
        for (const u of data.users) {
          pm[u.username] = { ...u.permissions, libraries: { ...u.permissions.libraries } };
          if (!Array.isArray(pm[u.username].blocked_tags)) pm[u.username].blocked_tags = [];
        }
        this.userPermissions = pm;
      } catch (e) {
        console.error('Failed to load user permissions:', e);
      }
      try {
        const res  = await fetch(apiUrl('/api/tags'));
        const data = await res.json();
        this.allTagsList = data.tags || [];
      } catch (e) {
        this.allTagsList = [];
      }
    },

    // ── CHANGE A USER'S ROLE (promote/demote admin) ──
    async changeUserRole(username, role) {
      if (!confirm(`Change ${username}'s role to ${role}?`)) {
        await this.loadUserPermissions();   // resets the <select>'s displayed value
        return;
      }
      try {
        const res  = await fetch(apiUrl(`/api/admin/users/${encodeURIComponent(username)}/role`), {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ role }),
        });
        const data = await res.json();
        this.permStatus = data.ok
          ? { msg: `✓ ${username} is now ${role}.`, type: 'ok' }
          : { msg: data.error || 'Something went wrong.', type: 'err' };
      } catch (e) {
        this.permStatus = { msg: 'Could not reach server.', type: 'err' };
      }
      await this.loadUserPermissions();
      setTimeout(() => { this.permStatus = { msg: '', type: '' }; }, 3000);
    },

    // ── DELETE A USER ──
    async deleteUser(username) {
      if (!confirm(`Permanently delete ${username}? This removes their account, reading history, favourites, and permissions. This can't be undone.`)) return;
      try {
        const res  = await fetch(apiUrl(`/api/admin/users/${encodeURIComponent(username)}`), { method: 'DELETE' });
        const data = await res.json();
        this.permStatus = data.ok
          ? { msg: `✓ Deleted ${username}.`, type: 'ok' }
          : { msg: data.error || 'Something went wrong.', type: 'err' };
        if (data.ok && this.expandedUser === username) this.expandedUser = null;
      } catch (e) {
        this.permStatus = { msg: 'Could not reach server.', type: 'err' };
      }
      await this.loadUserPermissions();
      setTimeout(() => { this.permStatus = { msg: '', type: '' }; }, 3000);
    },

    // ── EXPAND / COLLAPSE USER ROW ──
    toggleUserExpand(username) {
      if (this.expandedUser === username) {
        this.expandedUser = null;
        return;
      }
      this.expandedUser = username;
      // Ensure all current library IDs are initialised in this user's permissions
      const perms = this.userPermissions[username];
      if (perms) {
        if (!perms.libraries) perms.libraries = {};
        for (const lib of this.libraries) {
          const key = String(lib.id);
          if (!(key in perms.libraries)) {
            perms.libraries[key] = true;
          }
        }
      }
    },

    // ── SAVE PERMISSIONS FOR ONE USER (or _default) ──
    async saveUserPermissions(username) {
      const perms = this.userPermissions[username];
      if (!perms) return;
      try {
        const res  = await fetch(apiUrl(`/api/admin/permissions/${encodeURIComponent(username)}`), {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ permissions: perms }),
        });
        const data = await res.json();
        this.permStatus = data.ok
          ? { msg: '✓ Saved.', type: 'ok' }
          : { msg: data.error || 'Something went wrong.', type: 'err' };
      } catch (e) {
        this.permStatus = { msg: 'Could not reach server.', type: 'err' };
      }
      setTimeout(() => { this.permStatus = { msg: '', type: '' }; }, 3000);
    },

    // ── BLOCKED TAG SEARCH ──
    onBlockedTagInput(username) {
      const q = this.blockedTagInput.trim().toLowerCase();
      if (!q) { this.blockedTagSuggestions = []; return; }
      const already = this.userPermissions[username]?.blocked_tags || [];
      this.blockedTagSuggestions = this.allTagsList
        .filter(t => t.toLowerCase().includes(q) && !already.includes(t))
        .slice(0, 8);
    },

    async addBlockedTag(username, tag) {
      const perms = this.userPermissions[username];
      if (!perms || perms.blocked_tags.includes(tag)) return;
      perms.blocked_tags.push(tag);
      this.blockedTagInput       = '';
      this.blockedTagSuggestions = [];
      await this.saveUserPermissions(username);
    },

    async removeBlockedTag(username, tag) {
      const perms = this.userPermissions[username];
      if (!perms) return;
      perms.blocked_tags = perms.blocked_tags.filter(t => t !== tag);
      await this.saveUserPermissions(username);
    },

  }
}).mount('#app');