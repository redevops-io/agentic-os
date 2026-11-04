/* Local capability bootstrap for the Projects UI (served by agentic_os.local_capability_ui).
 *
 * 1. If the URL fragment carries a one-time launch code (#lc=...), remove it from the address bar at
 *    once and exchange it (POST, JSON) for a session: the server sets an HttpOnly SameSite=Strict
 *    cookie; the response carries a CSRF token that is kept in memory only.
 * 2. Otherwise (reload) ask the server for the CSRF token of the existing cookie session.
 * 3. Wrap window.fetch so same-origin /api/ requests wait for (1)/(2) and carry the CSRF header.
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
  var csrf = null;

  function launchCode() {
    var m = /(?:^#|&)lc=([A-Za-z0-9_-]{16,128})(?:&|$)/.exec(location.hash);
    if (!m) { return null; }
    try { history.replaceState(null, "", location.pathname + location.search); } catch (e) { /* ignore */ }
    return m[1];
  }

  function banner(text) {
    function show() {
      var d = document.createElement("div");
      d.setAttribute("role", "alert");
      d.setAttribute("style", "position:fixed;left:0;right:0;top:0;z-index:2147483647;padding:10px 16px;" +
        "background:#7f1d1d;color:#fff;font:14px system-ui,sans-serif");
      d.textContent = text;
      document.body.appendChild(d);
    }
    if (document.body) { show(); } else { document.addEventListener("DOMContentLoaded", show); }
  }

  var code = launchCode();
  var ready = (code
    ? origFetch(SESSION, { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ code: code }) })
    : origFetch(SESSION, { credentials: "same-origin", headers: { "Accept": "application/json" } }))
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (j) {
      if (j && j.csrf_token) { csrf = j.csrf_token; return; }
      banner("Not signed in to the local Agentic Apps service. Open the app from its launcher " +
             "(or the one-time URL printed by the serve command); this link may have expired or been used.");
    })
    .catch(function () { /* the UI's own fetches will surface the failure */ });

  window.fetch = function (input, init) {
    var url;
    try { url = new URL(typeof input === "string" ? input : input.url, location.href); } catch (e) { url = null; }
    if (!url || url.origin !== location.origin || url.pathname.indexOf(API) !== 0) {
      return origFetch(input, init);
    }
    return ready.then(function () {
      var o = Object.assign({}, init || {});
      var h = new Headers(o.headers || (typeof input !== "string" && input.headers) || undefined);
      if (csrf) { h.set("X-Agentic-CSRF", csrf); }
      o.headers = h;
      o.credentials = "same-origin";
      return origFetch(input, o);
    });
  };
})();
