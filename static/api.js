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
})();
