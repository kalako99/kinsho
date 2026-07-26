/**
 * api.js — Global API base URL resolver
 *
 * Pages are served two ways, and the ORIGIN — not the presence of a native
 * bridge — decides the mode:
 *  - From the Kinsho server itself (desktop browser, or the Android app's
 *    WebView after it navigates to the saved server): fetches are
 *    same-origin → API_BASE = ''. Capacitor injects `window.Capacitor` into
 *    these remote pages too (allowNavigation), so its presence must NOT be
 *    used to infer "app shell" here — that mistake left API_BASE = null on
 *    server-served pages inside the app, which re-showed the server-URL
 *    step on the server's own login page.
 *  - From the app bundle itself (capacitor://localhost or the local
 *    https://localhost shell, before redirecting to the server):
 *    API_BASE = saved server URL from this origin's localStorage
 *    (null if none saved yet → login page shows the server-URL step).
 */
(function () {
  'use strict';
  var isAppShell =
    location.protocol === 'capacitor:' ||
    location.protocol === 'file:' ||
    ((location.hostname === 'localhost' || location.hostname === '127.0.0.1') &&
      !!(window.__TAURI__ || window.Capacitor));
  window.API_BASE     = isAppShell ? localStorage.getItem('kinsho_server_url') : '';
  window.KINSHO_LOCAL = localStorage.getItem('kinsho_local_mode') === 'true';
  window.apiUrl       = function (path) { return (window.API_BASE || '') + path; };

  // ── LIVE PERMISSION / SESSION CHANGE DETECTION ──
  // An admin changing a user's permissions (blocked tags, library access,
  // tag/genre/description toggles, role) previously only took effect on
  // that user's NEXT full page load -- an already-open page had no way to
  // pick it up short of a manual reload, and the native app (no reload
  // button) had no way to force that short of logging out and back in.
  // Polling this page's own /api/auth/me here (loaded on every page) and
  // reloading on any change closes that gap everywhere at once: a changed
  // permissions object, a changed role, or the session itself having gone
  // away (logged out elsewhere, account deleted) all surface as the same
  // "state no longer matches what this page was built from" check, and a
  // full reload is the simplest way to make every affected bit of UI catch
  // up together instead of patching each page's own display logic.
  // Skipped entirely when there's no server to ask yet (the app-shell's
  // own pre-login screen, before a server URL is saved) -- window.API_BASE
  // is null only in that specific case, never for a same-origin '' base.
  if (window.API_BASE !== null) {
    var PERMISSION_POLL_MS = 20000;
    var knownAuthState = null;

    var checkAuthState = function () {
      fetch(window.apiUrl('/api/auth/me')).then(function (res) {
        return res.json();
      }).then(function (data) {
        if (!data.ok) {
          // Not logged in. Only worth a reload if we WERE logged in a
          // moment ago (session just invalidated) -- on the login page
          // itself this is always the case and should never reload.
          if (knownAuthState !== null) {
            knownAuthState = null;
            window.location.reload();
          }
          return;
        }
        var snapshot = JSON.stringify({ role: data.role, permissions: data.permissions });
        if (knownAuthState !== null && snapshot !== knownAuthState) {
          window.location.reload();
          return;
        }
        knownAuthState = snapshot;
      }).catch(function () {
        // Network hiccup -- next tick tries again.
      });
    };

    checkAuthState();
    setInterval(checkAuthState, PERMISSION_POLL_MS);
  }
})();
