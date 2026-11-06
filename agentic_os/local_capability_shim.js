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
 * 4. Tell the user when the local service is gone or failing: the vendored bundle has no error state and shows
 *    "Loading..." for ever when a request fails. One non-blocking role=alert notice (see showNotice).
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

  // The signed-out experience. Without a session the UI shell is a dead page ("Loading..." for ever, rail
  // buttons and badges that do nothing), so instead of a strip over its header this replaces it with ONE
  // focused sign-in panel: the shell root is hidden and inert, the panel is a <main> with an <h1> that takes
  // focus, the message is a role=alert region, and the developer-only instructions sit in a closed <details>.
  // Only DOM APIs and one <style> element are used (the CSP allows inline styles, not inline script).
  var ABOUT_HREF = "./about/";   // hook for the About and licences page (relative, so it follows PROJECTS_BASE_PATH); "" = no link
  var PANEL_CSS =
    ".ao-signin{box-sizing:border-box;min-height:100vh;display:grid;place-items:center;padding:24px;" +
    "background:#eef2f3;color:#0e1a1c;font:16px/1.5 system-ui,-apple-system,'Segoe UI',sans-serif}" +
    ".ao-signin *{box-sizing:border-box}" +
    ".ao-card{width:100%;max-width:34rem;background:#fff;border:1px solid #cbd7d8;border-radius:12px;padding:24px;" +
    "box-shadow:0 1px 2px rgba(14,26,28,.06),0 6px 20px rgba(14,26,28,.07)}" +
    ".ao-card h1{margin:0 0 12px;font-size:1.5rem;line-height:1.25;letter-spacing:-.01em}" +
    ".ao-card h1:focus{outline:none}" +
    ".ao-card p{margin:0 0 12px}" +
    ".ao-card details{margin:16px 0 0;padding-top:12px;border-top:1px solid #dde5e6}" +
    ".ao-card summary{cursor:pointer;font-weight:600;color:#0a6d62}" +
    ".ao-card details p{margin:8px 0 0;color:#3f5457}" +
    ".ao-card a{color:#0a6d62}" +
    ".ao-card .ao-foot{margin:16px 0 0;font-size:.9rem}" +
    ".ao-signin :focus-visible{outline:2px solid #0a6d62;outline-offset:2px;border-radius:4px}" +
    "@media (prefers-color-scheme:dark){.ao-signin{background:#0a1113;color:#e8f0f0}" +
    ".ao-card{background:#111b1d;border-color:#2c3d3f}.ao-card details{border-color:#223133}" +
    ".ao-card summary,.ao-card a{color:#5fd6c7}.ao-card details p{color:#b2c3c4}" +
    ".ao-signin :focus-visible{outline-color:#5fd6c7}}";

  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    if (attrs) { for (var k in attrs) { if (Object.prototype.hasOwnProperty.call(attrs, k)) { e.setAttribute(k, attrs[k]); } } }
    if (text !== undefined) { e.textContent = text; }
    return e;
  }

  function signInPanel() {
    if (shown) { return; }
    shown = true;
    function show() {
      var root = document.getElementById("root");
      if (root) {
        root.setAttribute("hidden", "");
        root.setAttribute("inert", "");
        root.setAttribute("aria-hidden", "true");
      }
      var style = el("style", null, PANEL_CSS);
      (document.head || document.body).appendChild(style);
      var main = el("main", { "class": "ao-signin", id: "agentic-signin" });
      var card = el("div", { "class": "ao-card" });
      var h1 = el("h1", { tabindex: "-1" }, "Sign in from the Agentic Apps R0-prime window");
      var alert = el("div", { role: "alert" });
      alert.appendChild(el("p", null,
        "Not signed in to the local Agentic Apps service: this page has no session. " +
        "Go back to the Agentic Apps R0-prime window and press \"Start & open\" to get a fresh link."));
      alert.appendChild(el("p", null,
        "A link works once, and a bookmark, a restored tab or a second tab cannot sign in on its own."));
      var details = el("details");
      details.appendChild(el("summary", null, "Running the server yourself?"));
      details.appendChild(el("p", null,
        "If you started the server yourself (the serve command or a container), open the one-time URL it printed in its output."));
      card.appendChild(h1);
      card.appendChild(alert);
      card.appendChild(details);
      if (ABOUT_HREF) {
        var foot = el("p", { "class": "ao-foot" });
        foot.appendChild(el("a", { href: ABOUT_HREF }, "About and licences"));
        card.appendChild(foot);
      }
      main.appendChild(card);
      document.body.appendChild(main);
      try { h1.focus(); } catch (e) { /* ignore */ }
    }
    if (document.body) { show(); } else { document.addEventListener("DOMContentLoaded", show); }
  }

  // Service-lost notice (UI-agent finding UIA-02). When the sidecar has stopped (network error) or answers HTTP 5xx, the vendored
  // bundle has no error state: it keeps "Loading..." on screen with no message. ONE role=alert strip tells the user what happened and
  // which launcher button to press. It is non-blocking by construction: it never moves focus, never hides or inerts the app, adds no
  // controls, and is removed as soon as a later same-origin /api/ request is answered below 500. It is not shown while the sign-in
  // panel owns the page, and failures of other origins or aborted requests never raise it. DOM APIs and one <style> element only.
  var NOTICE_TEXT_GONE = "The local Agentic Apps service is not responding. It may have stopped, so this page cannot load data. " +
    "Go back to the Agentic Apps R0-prime window and press \"Start & open\" to start it again and get a fresh link; " +
    "this page cannot reconnect on its own.";
  var NOTICE_CSS =
    ".ao-notice{position:fixed;left:0;right:0;bottom:0;z-index:2147483000;box-sizing:border-box;max-height:40vh;overflow:auto;" +
    "padding:12px 16px;background:#fff4d6;color:#3a2a00;border-top:2px solid #b45309;" +
    "font:14px/1.45 system-ui,-apple-system,'Segoe UI',sans-serif}" +
    "@media (prefers-color-scheme:dark){.ao-notice{background:#2b2000;color:#ffe9a8;border-top-color:#f59e0b}}";
  var notice = null;
  var noticeStyled = false;
  function showNotice(status) {
    if (shown || !document.body) { return; }
    var text = status ? "The local Agentic Apps service returned an error (HTTP " + status + "). " +
      "Reload this page; if it keeps happening, go back to the Agentic Apps R0-prime window and press \"Start & open\" to restart it." :
      NOTICE_TEXT_GONE;
    if (!noticeStyled) {
      noticeStyled = true;
      (document.head || document.body).appendChild(el("style", null, NOTICE_CSS));
    }
    if (notice) { notice.textContent = text; return; }
    notice = el("div", { "class": "ao-notice", role: "alert", id: "agentic-service-notice" }, text);
    document.body.appendChild(notice);
  }
  function clearNotice() {
    if (notice) {
      try { notice.remove(); } catch (e) { /* ignore */ }
      notice = null;
    }
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
      .then(function (j) { if (!adopt(j)) { forget(); signInPanel(); } });
  }

  function resume() {
    var t = load();
    if (!t) { signInPanel(); return Promise.resolve(); }
    return origFetch(SESSION, { credentials: "omit",
      headers: { "Accept": "application/json", "X-Agentic-Session": t } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (j && j.csrf_token) { token = t; csrf = j.csrf_token; } else { forget(); signInPanel(); }
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
        if (r.status === 401) { forget(); signInPanel(); }
        else if (r.status >= 500) { showNotice(r.status); }
        else { clearNotice(); }
        return r;
      }, function (e) {
        if (!(e && e.name === "AbortError")) { showNotice(0); }
        throw e;
      });
    });
  };
})();
