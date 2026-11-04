/* Local capability bootstrap for the Projects UI (served by agentic_os.local_capability_ui).
 *
 * 1. If the URL fragment carries a one-time launch code (#lc=...), remove it from the address bar at
 *    once and exchange it (POST, JSON) for a session. The response body carries a session token and a
 *    CSRF token. No cookie is set: cookies are not port-scoped, so a hostile server on another
 *    127.0.0.1 port would receive one.
 * 2. The session token is kept in sessionStorage (per tab, per origin including the port), so a reload
 *    of the same tab resumes the session; a new tab or a bookmark needs a fresh launch code.
 * 3. Wrap window.fetch so same-origin /api/ requests wait for (1)/(2) and carry the X-Agentic-Session
 *    and X-Agentic-CSRF headers.
 * The code never reaches the server in a URL, a log line, or a Referer. No eval, no inline script.
 */
(function () {
  "use strict";
  var origFetch = window.fetch.bind(window);
  var script = document.currentScript;
  var base = "";
  try {
    var p = new URL(script.src, location.href).pathname;
    base = p.replace(/\/local-capability-shim\.js$/, "");
  } catch (e) { base = ""; }
  var SESSION = base + "/api/local/session";
  var API = base + "/api/";
  var KEY = "agentic_os.session";
  var token = null;
  var csrf = null;
  var shown = false;

  function load() { try { return window.sessionStorage.getItem(KEY); } catch (e) { return null; } }
  function save(t) { try { window.sessionStorage.setItem(KEY, t); } catch (e) { /* in-memory only */ } }
  function forget() { token = null; csrf = null; try { window.sessionStorage.removeItem(KEY); } catch (e) { /* ignore */ } }

  function launchCode() {
    var m = /(?:^#|&)lc=([A-Za-z0-9_-]{16,128})(?:&|$)/.exec(location.hash);
    if (!m) { return null; }
    try { history.replaceState(null, "", location.pathname + location.search); } catch (e) { /* ignore */ }
    return m[1];
  }

  function banner() {
    if (shown) { return; }
    shown = true;
    function show() {
      var d = document.createElement("div");
      d.setAttribute("role", "alert");
      d.setAttribute("style", "position:fixed;left:0;right:0;top:0;z-index:2147483647;padding:10px 16px;" +
        "background:#7f1d1d;color:#fff;font:14px system-ui,sans-serif");
      d.textContent = "Not signed in to the local Agentic Apps service: this page has no session. " +
        "Go back to the Agentic Apps window and press \"Start & open\" to get a fresh link. " +
        "A link works once, and a bookmark, a restored tab or a second tab cannot sign in on its own. " +
        "If you started the server yourself (the serve command or a container), open the one-time URL it printed in its output.";
      document.body.appendChild(d);
    }
    if (document.body) { show(); } else { document.addEventListener("DOMContentLoaded", show); }
  }

  function adopt(j) {
    if (j && j.session_token && j.csrf_token) {
      token = j.session_token;
      csrf = j.csrf_token;
      save(token);
      return true;
    }
    return false;
  }

  function exchange(code) {
    return origFetch(SESSION, { method: "POST", credentials: "omit",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ code: code }) })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { if (!adopt(j)) { forget(); banner(); } });
  }

  function resume() {
    var t = load();
    if (!t) { banner(); return Promise.resolve(); }
    return origFetch(SESSION, { credentials: "omit",
      headers: { "Accept": "application/json", "X-Agentic-Session": t } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (j && j.csrf_token) { token = t; csrf = j.csrf_token; } else { forget(); banner(); }
      });
  }

  var code = launchCode();
  var ready = (code ? exchange(code) : resume()).catch(function () { /* the UI's own fetches will surface it */ });

  window.fetch = function (input, init) {
    var url;
    try { url = new URL(typeof input === "string" ? input : input.url, location.href); } catch (e) { url = null; }
    if (!url || url.origin !== location.origin || url.pathname.indexOf(API) !== 0) {
      return origFetch(input, init);
    }
    return ready.then(function () {
      var o = Object.assign({}, init || {});
      var h = new Headers(o.headers || (typeof input !== "string" && input.headers) || undefined);
      if (token) { h.set("X-Agentic-Session", token); }
      if (csrf) { h.set("X-Agentic-CSRF", csrf); }
      o.headers = h;
      o.credentials = "omit";
      return origFetch(input, o).then(function (r) {
        if (r.status === 401) { forget(); banner(); }
        return r;
      });
    });
  };
})();
