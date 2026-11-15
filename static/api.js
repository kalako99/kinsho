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

  // ── UNIFIED "BACK" NAVIGATION ──
  // Every in-app back button used to be a hardcoded window.location.href
  // redirect to one fixed URL, regardless of where the user actually came
  // from -- which made it behave differently from the browser/mouse back
  // button (real history navigation) and, e.g., sent "back" from a
  // collection page to the unscoped all-collections list instead of
  // wherever the user was actually browsing. history.length > 1 means this
  // tab has a real previous page to return to (the normal case, arrived via
  // an in-app click); history.length <= 1 means this page was opened
  // directly (a bookmarked/shared link as the tab's first navigation),
  // where there's nothing to go back to and the caller's fallback URL is
  // used instead, unchanged from the old always-redirect behavior.
  window.kinshoGoBack = function (fallbackUrl) {
    if (window.history.length > 1) {
      window.history.back();
    } else {
      window.location.href = fallbackUrl;
    }
  };

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
  // The chapter/volume reader (image-based and EPUB) holds a page's worth of
  // JS state that only lives in memory for that one page load -- the
  // materialized conveyor slots and their already-loaded images in
  // chapter_reader.html, mid-book position in epub_reader.html. A reload
  // triggered out from under an active read throws all of that away and
  // rebuilds from scratch, landing back at the last SAVED checkpoint rather
  // than the user's exact position -- a real, disruptive interruption, not
  // just a cosmetic reflow. Server-side permission/tag enforcement
  // (can_access_library/is_manga_blocked, see the security audit notes)
  // already applies to every actual content request regardless of what this
  // poll does, so skipping the reload here only defers when the *UI*
  // catches up, never what the server actually allows -- safe to defer
  // until the next real navigation away from the reader, which gets a fresh
  // script context (and therefore fresh auth state) for free anyway.
  var isReaderPage = function () {
    return /\/manga\/[^/]+\/[^/]+\/(chapter|volume)\//.test(location.pathname);
  };

  if (window.API_BASE !== null) {
    var PERMISSION_POLL_MS = 20000;
    var knownAuthState = null;

    var checkAuthState = function () {
      if (isReaderPage()) return;
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


/**
 * Themed pop-ups: kinshoConfirm(message, {ok, cancel, danger}) -> Promise<boolean>
 * and kinshoAlert(message) -> Promise. They replace the browser's confirm() /
 * alert(), which look like plain system dialogs, and take the page's theme
 * colours (--color-*). Await them; a click outside or Escape = Cancel.
 */
(function () {
  if (window.kinshoConfirm) return;
  function css() {
    if (document.getElementById('kdlg-css')) return;
    const s = document.createElement('style');
    s.id = 'kdlg-css';
    s.textContent = `
.kdlg-back{position:fixed;inset:0;z-index:100000;display:flex;align-items:center;justify-content:center;padding:16px;background:rgba(0,0,0,.6);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px)}
.kdlg{box-sizing:border-box;width:100%;max-width:420px;background:var(--color-secondary,#1a1a1a);color:var(--color-text,#f0f0f0);border:1px solid color-mix(in srgb,var(--color-text,#f0f0f0) 14%,transparent);border-radius:14px;padding:20px 20px 16px;box-shadow:0 12px 40px rgba(0,0,0,.55);font-family:inherit}
.kdlg-msg{font-size:15px;line-height:1.5;white-space:pre-wrap;word-break:break-word;margin:0 0 18px}
.kdlg-row{display:flex;justify-content:flex-end;gap:10px}
.kdlg-btn{font:inherit;font-size:14px;font-weight:600;padding:9px 18px;border-radius:9px;cursor:pointer;border:1px solid color-mix(in srgb,var(--color-text,#f0f0f0) 22%,transparent);background:transparent;color:var(--color-text,#f0f0f0)}
.kdlg-btn.ok{background:var(--color-primary,#e94560);border-color:var(--color-primary,#e94560);color:#fff}
.kdlg-btn:focus-visible{outline:2px solid var(--color-primary,#e94560);outline-offset:2px}`;
    document.head.appendChild(s);
  }
  function ask(message, opts, single) {
    opts = opts || {};
    css();
    return new Promise(resolve => {
      const back = document.createElement('div');
      back.className = 'kdlg-back';
      const box = document.createElement('div');
      box.className = 'kdlg';
      box.setAttribute('role', single ? 'alertdialog' : 'dialog');
      box.setAttribute('aria-modal', 'true');
      const msg = document.createElement('p');
      msg.className = 'kdlg-msg';
      msg.textContent = String(message);
      const row = document.createElement('div');
      row.className = 'kdlg-row';
      const prevFocus = document.activeElement;
      function done(v) {
        document.removeEventListener('keydown', onKey, true);
        back.remove();
        try { prevFocus && prevFocus.focus && prevFocus.focus(); } catch (e) {}
        resolve(v);
      }
      function mk(label, cls, v) {
        const b = document.createElement('button');
        b.type = 'button'; b.className = 'kdlg-btn ' + cls; b.textContent = label;
        b.addEventListener('click', () => done(v));
        return b;
      }
      if (!single) row.appendChild(mk(opts.cancel || 'Cancel', '', false));
      const okBtn = mk(opts.ok || 'OK', 'ok', true);
      row.appendChild(okBtn);
      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); done(single); }
        else if (e.key === 'Enter' && document.activeElement && document.activeElement.tagName !== 'BUTTON') { e.preventDefault(); done(true); }
      }
      document.addEventListener('keydown', onKey, true);
      back.addEventListener('mousedown', e => { if (e.target === back) done(single); });
      box.append(msg, row);
      back.appendChild(box);
      document.body.appendChild(back);
      okBtn.focus();
    });
  }
  window.kinshoConfirm = (m, o) => ask(m, o, false);
  window.kinshoAlert = m => ask(m, {}, true);
})();
