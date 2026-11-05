/* About page renderer. No framework, no eval, no inline handler, no HTML parsing: every value from the build record and every
   byte of a shipped file is inserted as TEXT (textContent). Links are built from fixed slugs, never from record text. The page
   makes requests only to its own ./about.json and ./files/<slug> (GET), and never to the project API. ASCII only. */
(function () {
  "use strict";

  var VIEW_LIMIT = 256 * 1024;           // most bytes rendered at once; the whole file is one click away as plain text
  var FETCH_TIMEOUT_MS = 5000;
  var SLOT_LABELS = {
    "LEGAL.COPYRIGHT_LINE": "Copyright",
    "LEGAL.LICENCE_STATEMENT": "Licence",
    "LEGAL.NO_WARRANTY": "Warranty",
    "LEGAL.SECTION13_NOTICE": "Network use",
    "LEGAL.SOURCE_OFFER": "Offer"
  };
  var LEGAL_ORDER = ["LEGAL.COPYRIGHT_LINE", "LEGAL.LICENCE_STATEMENT", "LEGAL.NO_WARRANTY", "LEGAL.SECTION13_NOTICE"];

  var $ = function (id) { return document.getElementById(id); };
  var doc = null;                          // the parsed record
  var cache = {};                          // slug -> ArrayBuffer (verified)
  var opener = null;                       // the element that opened the viewer
  var current = "";                        // text currently in the viewer
  var filterTimer = 0;

  function el(tag, props, children) {
    var e = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (k) {
        if (k === "text") { e.textContent = props[k]; }
        else if (k === "class") { e.className = props[k]; }
        else { e.setAttribute(k, props[k]); }
      });
    }
    (children || []).forEach(function (c) { e.appendChild(c); });
    return e;
  }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  function say(msg) { $("live").textContent = ""; window.setTimeout(function () { $("live").textContent = msg; }, 30); }
  function kb(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",") + " bytes"; }

  // ----------------------------------------------------------------------------------------------- loading and states
  function setState(kind, title, lines, retry) {
    var s = $("state");
    clear(s);
    $("content").hidden = true;
    var box = el("div", { "class": "callout " + (kind === "error" ? "err" : "info") });
    var h = el("h2", { text: title, tabindex: "-1" });
    box.appendChild(h);
    lines.forEach(function (l) { box.appendChild(el("p", { text: l })); });
    if (retry) {
      var b = el("button", { type: "button", text: "Try again" });
      b.addEventListener("click", load);
      box.appendChild(b);
    }
    s.setAttribute("role", kind === "error" ? "alert" : "status");
    s.appendChild(box);
    h.focus();
  }

  function withTimeout(promise) {
    return new Promise(function (resolve, reject) {
      var t = window.setTimeout(function () { reject(new Error("timeout")); }, FETCH_TIMEOUT_MS);
      promise.then(function (v) { window.clearTimeout(t); resolve(v); }, function (e) { window.clearTimeout(t); reject(e); });
    });
  }

  function load() {
    var s = $("state");
    clear(s);
    s.setAttribute("role", "status");
    s.appendChild(el("p", { text: "Loading build information..." }));
    $("content").hidden = true;
    withTimeout(fetch("./about.json", { cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer" })).then(function (r) {
      if (r.status === 404) {
        setState("info", "Build information is not available", [
          "This installation does not include a build record (for example, it was started from a development checkout or a container). Version and component details cannot be shown.",
          "The licence files are in the Licenses folder of the application."], false);
        return null;
      }
      if (!r.ok) { throw new Error("status " + r.status); }
      return r.json();
    }).then(function (j) {
      if (j === null) { return; }
      if (j && j.error && typeof j.error.code === "string") {
        setState("error", "Build information could not be verified", [
          "Reason code: " + String(j.error.code).replace(/[^A-Z_]/g, "").slice(0, 40) + ". Nothing from the build record is shown, so that no unverified text appears.",
          "The licence files are in the Licenses folder of the application."], true);
        return;
      }
      if (!j || j.schema !== "redevops.about/1" || !j.product || !j.notices || !j.legal) { throw new Error("shape"); }
      doc = j;
      render();
    }).catch(function () {
      setState("error", "Build information could not be loaded", [
        "The build record did not arrive in time or was not readable.",
        "The licence files are in the Licenses folder of the application."], true);
    });
  }

  // ----------------------------------------------------------------------------------------------- rendering
  function fact(dl, name, value, mono) {
    dl.appendChild(el("dt", { text: name }));
    dl.appendChild(el("dd", { text: value, "class": mono ? "mono" : "" }));
  }

  function render() {
    var p = doc.product, b = doc.build || {}, src = b.source || {};
    var s = $("state");
    clear(s);
    s.setAttribute("role", "status");
    $("content").hidden = false;
    $("brand-name").textContent = p.name;
    $("foot-line").textContent = p.name + " " + p.version + ", release line " + p.release_line + ".";
    document.title = "About and licences - " + p.name;

    var dl = $("facts");
    clear(dl);
    fact(dl, "Product", p.name);
    var ver = el("dd", { text: p.version });
    dl.appendChild(el("dt", { text: "Version" }));
    dl.appendChild(ver);
    dl.appendChild(el("dt", { text: "Release line" }));
    var rl = el("dd", { text: p.release_line });
    if (b.mode === "private-candidate") { rl.appendChild(el("span", { "class": "tag", text: "private candidate" })); }
    dl.appendChild(rl);
    fact(dl, "Bundle identifier", p.bundle_identifier, true);
    fact(dl, "Source commit", src.commit + (src.dirty_files ? " (" + src.dirty_files + " changed files at build time)" : " (clean)"), true);
    fact(dl, "Source tree", src.tree, true);
    fact(dl, "Source commit time", src.commit_time_utc + " (UTC)");
    fact(dl, "Platform", b.platform);
    fact(dl, "Local service (sidecar)", p.sidecar.binary + ", Python package " + p.sidecar.python_package + " " + p.sidecar.python_package_version);
    fact(dl, "Launcher", p.launcher.binary + " " + p.launcher.version);
    var ui = b.ui_bundle || { id: "none", files: [] };
    fact(dl, "Projects UI bundle", ui.id, true);

    renderLegal();
    renderSource();
    renderFiles();
    renderComponents("");
    $("filter").value = "";
    document.title = "About and licences - " + p.name + " " + p.version;
  }

  function slotBlock(id, slot) {
    var filled = slot.state === "filled";
    var box = el("div", { "class": "slot" + (filled ? " filled" : ""), "data-slot": id });
    var t = el("span", { text: slot.text });
    if (filled) { t.setAttribute("lang", "en"); }
    box.appendChild(t);
    box.appendChild(el("small", { text: "Slot " + id + (filled ? "." : ". Pending a decision.") }));
    return box;
  }

  function renderLegal() {
    var root = $("legal");
    clear(root);
    var slots = doc.legal.slots || {};
    LEGAL_ORDER.forEach(function (id) {
      var s = slots[id];
      if (!s || s.state === "not-applicable") { return; }
      root.appendChild(el("span", { "class": "slot-label", text: SLOT_LABELS[id] }));
      root.appendChild(slotBlock(id, s));
    });
  }

  function renderSource() {
    var root = $("source");
    clear(root);
    if (doc.source_access && doc.source_access.state === "not-published") {
      root.appendChild(el("p", { text: "Source access for this build is not published yet (private candidate)." }));
    }
    var s = doc.legal.slots["LEGAL.SOURCE_OFFER"];
    if (s && s.state !== "not-applicable") {
      root.appendChild(el("span", { "class": "slot-label", text: SLOT_LABELS["LEGAL.SOURCE_OFFER"] }));
      root.appendChild(slotBlock("LEGAL.SOURCE_OFFER", s));
    }
  }

  function viewButton(label, onclick) {
    var b = el("button", { type: "button", text: "View", "aria-label": label });
    b.addEventListener("click", function () { onclick(b); });
    return b;
  }

  function renderFiles() {
    var ul = $("files");
    clear(ul);
    doc.licence_files.forEach(function (f) {
      var li = el("li");
      li.appendChild(el("span", { "class": "nm", text: f.file }));
      li.appendChild(el("span", { "class": "lic", text: f.label }));
      li.appendChild(el("span", { "class": "note", text: kb(f.bytes) }));
      li.appendChild(viewButton("View " + f.file, function (btn) {
        openViewer(btn, $("sec-texts"), f.file, f.id, 0, f.bytes, f.sha256, f.bytes, f.sha256);
      }));
      ul.appendChild(li);
    });
    var n = doc.notices;
    var li2 = el("li");
    li2.appendChild(el("span", { "class": "nm", text: n.file }));
    li2.appendChild(el("span", { "class": "lic", text: "Third-party notices (see the list below)" }));
    li2.appendChild(el("span", { "class": "note", text: kb(n.bytes) }));
    li2.appendChild(viewButton("View " + n.file, function (btn) {
      openViewer(btn, $("sec-texts"), n.file, "notices", 0, n.bytes, n.sha256, n.bytes, n.sha256);
    }));
    ul.appendChild(li2);
  }

  function renderComponents(query) {
    var ul = $("comps");
    clear(ul);
    var q = query.trim().toLowerCase();
    var all = doc.notices.components;
    var frag = document.createDocumentFragment();
    var shown = 0;
    all.forEach(function (c) {
      if (q && c.name.toLowerCase().indexOf(q) < 0 && c.declared.toLowerCase().indexOf(q) < 0) { return; }
      shown += 1;
      var li = el("li");
      li.appendChild(el("span", { "class": "nm", text: c.name }));
      li.appendChild(el("span", { "class": "note", text: c.version }));
      li.appendChild(el("span", { "class": "lic", text: c.declared }));
      if (c.status === "text-not-found") {
        li.appendChild(el("span", { "class": "badge-warn", text: "Licence text not found in this build" }));
      } else if (c.status === "not-third-party") {
        li.appendChild(el("span", { "class": "note", text: "Not a third-party notice" }));
      } else {
        li.appendChild(viewButton("View notice for " + c.name + " " + c.version, function (btn) {
          openViewer(btn, $("sec-third"), c.name + " " + c.version, "notices", c.offset, c.length, null,
                     doc.notices.bytes, doc.notices.sha256);
        }));
      }
      frag.appendChild(li);
    });
    ul.appendChild(frag);
    $("third-intro").textContent = all.length + " components are listed in the notices file. Licences are shown as declared in each package; they are not verified here.";
    $("filter-status").textContent = q ? ("Showing " + shown + " of " + all.length) : "";
    renderRefs();
    return shown;
  }

  function renderRefs() {
    var refs = doc.notices.reference_notices || [];
    $("refs-wrap").hidden = refs.length === 0;
    var ul = $("refs");
    clear(ul);
    refs.forEach(function (r) {
      var li = el("li");
      li.appendChild(el("span", { "class": "nm", text: r.title }));
      li.appendChild(el("span", { "class": "note", text: "" }));
      li.appendChild(el("span", { "class": "lic", text: r.declared }));
      if (r.status === "text-not-found") {
        li.appendChild(el("span", { "class": "badge-warn", text: "Licence text not found in this build" }));
      } else {
        li.appendChild(viewButton("View notice for " + r.title, function (btn) {
          openViewer(btn, $("sec-third"), r.title, "notices", r.offset, r.length, null, doc.notices.bytes, doc.notices.sha256);
        }));
      }
      ul.appendChild(li);
    });
  }

  // ----------------------------------------------------------------------------------------------- the text viewer
  function hex(buf) {
    var a = new Uint8Array(buf), s = "";
    for (var i = 0; i < a.length; i++) { s += (a[i] < 16 ? "0" : "") + a[i].toString(16); }
    return s;
  }

  function fetchVerified(slug, wantBytes, wantSha) {
    if (cache[slug]) { return Promise.resolve(cache[slug]); }
    return withTimeout(fetch("./files/" + slug, { cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer" })).then(function (r) {
      if (!r.ok) { throw new Error("status " + r.status); }
      return r.arrayBuffer();
    }).then(function (buf) {
      if (buf.byteLength !== wantBytes) { throw new Error("size"); }
      if (window.crypto && window.crypto.subtle) {
        return window.crypto.subtle.digest("SHA-256", buf).then(function (d) {
          if (hex(d) !== wantSha) { throw new Error("hash"); }
          cache[slug] = buf;
          return buf;
        });
      }
      cache[slug] = buf;
      return buf;
    });
  }

  function openViewer(btn, section, title, slug, offset, length, wholeSha, fileBytes, fileSha) {
    opener = btn;
    var v = $("viewer");
    section.appendChild(v);
    v.hidden = false;
    $("v-title").textContent = title;
    $("v-text").textContent = "Loading...";
    $("v-note").textContent = "";
    $("v-meta").textContent = "";
    current = "";
    $("v-title").focus();
    fetchVerified(slug, fileBytes, fileSha).then(function (buf) {
      var take = Math.min(length, VIEW_LIMIT);
      var slice = new Uint8Array(buf, offset, take);
      var text = new TextDecoder("utf-8").decode(slice);
      current = text;
      $("v-text").textContent = text;
      var meta = "sha256 of the whole file " + fileSha + (window.crypto && window.crypto.subtle ? " (checked in this page)" : "");
      $("v-meta").textContent = meta;
      var note = "";
      if (length > VIEW_LIMIT) {
        note = "Showing the first " + kb(VIEW_LIMIT) + " of " + kb(length) + ". ";
      }
      if (slug === "notices" && (offset !== 0 || length !== fileBytes)) {
        note += "This is one section of the notices file. ";
      }
      $("v-note").textContent = "";
      if (note || slug) {
        var n = $("v-note");
        if (note) { n.appendChild(document.createTextNode(note)); }
        var a = el("a", { href: "./files/" + slug, text: "Open the whole file as plain text" });
        n.appendChild(a);
      }
      say("Text loaded: " + title);
    }).catch(function () {
      $("v-text").textContent = "";
      $("v-note").textContent = "This file could not be loaded or did not match the build record, so nothing is shown.";
      say("The file could not be loaded.");
    });
  }

  function closeViewer() {
    $("viewer").hidden = true;
    current = "";
    if (opener) { opener.focus(); }
  }

  function copyText() {
    var t = current;
    if (!t) { return; }
    function fallback() {
      var r = document.createRange();
      r.selectNodeContents($("v-text"));
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(r);
      say("Text selected. Press the copy shortcut of your system to copy it.");
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(function () { say("Text copied."); }, fallback);
    } else { fallback(); }
  }

  // ----------------------------------------------------------------------------------------------- wiring
  document.addEventListener("DOMContentLoaded", function () {
    $("v-close").addEventListener("click", closeViewer);
    $("v-copy").addEventListener("click", copyText);
    $("viewer").addEventListener("keydown", function (e) {
      if (e.key === "Escape") { e.preventDefault(); closeViewer(); }
    });
    $("filter").addEventListener("input", function () {
      window.clearTimeout(filterTimer);
      filterTimer = window.setTimeout(function () { if (doc) { renderComponents($("filter").value.slice(0, 100)); } }, 200);
    });
    var main = $("about-main");
    document.querySelector(".skip").addEventListener("click", function () { window.setTimeout(function () { main.focus(); }, 0); });
    load();
  });
})();
