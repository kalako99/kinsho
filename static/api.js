/**
 * api.js — Global API base URL resolver
 * Browser mode  (FastAPI serves the files): API_BASE = '' → relative paths unchanged.
 * Compiled mode (Capacitor / Tauri):        API_BASE = saved server URL from localStorage.
 */
(function () {
  'use strict';
  var saved    = localStorage.getItem('kinsho_server_url');
  var isNative = !!(window.__TAURI__ || (window.Capacitor && window.Capacitor.isNative));
  if (saved === null && !isNative) {
    // Running in a real browser served by FastAPI — no URL needed
    localStorage.setItem('kinsho_server_url', '');
    saved = '';
  }
  window.API_BASE       = (saved !== null) ? saved : null;
  window.KINSHO_LOCAL   = localStorage.getItem('kinsho_local_mode') === 'true';
  window.apiUrl         = function (path) { return (window.API_BASE || '') + path; };
})();