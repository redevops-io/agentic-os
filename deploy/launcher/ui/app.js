// Launcher page logic. External file (CSP script-src 'self'): no inline script, no eval, no HTML injection.
// Every value that reaches the DOM goes through textContent; no URL from data is ever turned into a link.
(() => {
  "use strict";

  const invoke = (name, args) => window.__TAURI__.core.invoke(name, args || {});
  const $ = (id) => document.getElementById(id);
  const out = $("out");
  const say = (t) => (out.textContent = t);
  const run = (name) => async () => {
    say("Working…");
    try { say(await invoke(name) || "Done."); }
    catch (e) { say("Error:\n" + e); }
  };

  $("check").onclick = run("device_report");

  // Native-first: start the control plane from the frozen sidecar (no Docker), then open it.
  $("start").onclick = async () => {
    say("Starting the control plane…");
    try {
      const url = await invoke("start_native");
      await invoke("open_ui");
      say("Running at " + url + " — opened in your browser." + (await legacyNote()));
    } catch (e) { say("Error:\n" + e); }
  };
  $("stop").onclick = run("stop_native");

  // Advanced Docker controls appear only when Docker is actually present.
  $("docker-up").onclick = run("advanced_docker_up");
  $("docker-down").onclick = run("advanced_docker_down");
  $("docker-purge").onclick = async () => {
    if (!confirm("Remove the container tier's data (volumes)? Native data is untouched.")) return;
    await run("advanced_docker_purge")();
  };
  // Warn (never act) when an OLD unauthenticated service is still running on port 8787.
  const legacyNote = async () => {
    try {
      const w = await invoke("legacy_service_warning");
      return w ? "\n\nWARNING: " + w : "";
    } catch (e) { return ""; }
  };
  legacyNote().then((n) => { if (n) say(out.textContent + n); });
  invoke("docker_available").then((ok) => {
    if (ok) $("advanced").hidden = false;
  }).catch(() => {});

  // ── About / Legal ──────────────────────────────────────────────────────────────────────
  // Data comes from two launcher commands that read fixed files of the app's own Licenses folder
  // (about_info: the build record redevops.about/1; about_text: one of six fixed text files). Nothing
  // here uses the network or the control plane, so it works with the service stopped.
  const ABOUT_SCHEMA = "redevops.about/1";
  const MAX_VIEW_BYTES = 256 * 1024;
  const MAX_ROWS = 1000;
  const SLOT_ORDER = [
    "LEGAL.COPYRIGHT_LINE", "LEGAL.LICENCE_STATEMENT", "LEGAL.NO_WARRANTY", "LEGAL.CONVEYANCE",
    "LEGAL.SECTION13_NOTICE", "LEGAL.SOURCE_OFFER", "DATA.SOURCE_LOCATION", "DATA.SOURCE_COMMIT",
  ];

  const str = (v) => (typeof v === "string" ? v : typeof v === "number" && Number.isFinite(v) ? String(v) : "");
  const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : {});
  const arr = (v) => (Array.isArray(v) ? v : []);
  const el = (tag, props, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === "text") n.textContent = v;
      else if (k === "class") n.className = v;
      else n.setAttribute(k, v);
    }
    for (const kid of kids) if (kid) n.append(kid);
    return n;
  };

  let record = null;       // the parsed build record, once loaded
  let noticesBytes = null; // THIRD_PARTY_NOTICES.txt as UTF-8 bytes, once loaded
  let viewerOpener = null; // the button that opened the viewer (focus returns there)

  const setStatus = (t) => { $("about-status").textContent = t; };
  const showError = (message) => {
    const box = $("about-error");
    box.replaceChildren(el("p", { text: message }));
    const retry = el("button", { type: "button", text: "Try again" });
    retry.onclick = () => loadAbout(true);
    box.append(retry);
    box.hidden = false;
    setStatus("");
    $("about-body").replaceChildren();
  };

  // The record carries the slot text (redevops.about/1: legal.slots.<id>.text, the placeholder while a decision is
  // open). It is shown verbatim as text. A slot without usable text fails safe to a neutral marker; the bracket
  // marker composed from `decisions` is only a fallback for a record that has no text field.
  const slotText = (key, slot) => {
    const s = obj(slot);
    if (typeof s.placeholder === "string" && s.placeholder) return s.placeholder;
    if (typeof s.text === "string" && s.text) return s.text;
    if (s.state === "placeholder") {
      const ids = arr(s.decisions).filter((d) => typeof d === "string" && /^[A-Za-z0-9.-]{1,24}$/.test(d));
      const what = key.startsWith("DATA.") ? "VALUE" : "LEGAL TEXT";
      return "[" + what + " PENDING DECISION" + (ids.length ? " " + ids.join(", ") : "") + "]";
    }
    return "[LEGAL TEXT NOT AVAILABLE IN THIS VIEW]";
  };
  const slotBlock = (key, slot) =>
    el("div", { class: "slot" }, el("span", { class: "slot-id", text: key }), el("span", { class: "slot-text", text: slotText(key, slot) }));

  const factRows = (rec) => {
    const p = obj(rec.product), b = obj(rec.build), src = obj(b.source);
    const rows = [
      ["Product", str(p.name)],
      ["Version", str(p.version)],
      ["Release line", str(p.release_line)],
      ["Build", str(b.mode)],
      ["Platform", str(b.platform)],
      ["Bundle identifier", str(p.bundle_identifier)],
      ["Launcher version", str(obj(p.launcher).version)],
      ["Source commit", str(src.commit)],
      ["Commit time (UTC)", str(src.commit_time_utc)],
    ];
    const dl = el("dl", { class: "facts" });
    for (const [k, v] of rows) {
      if (!v) continue;
      dl.append(el("dt", { text: k }), el("dd", { text: v, class: k === "Source commit" || k === "Bundle identifier" ? "mono" : "" }));
    }
    return dl;
  };

  const legalBlock = (rec) => {
    const slots = obj(obj(rec.legal).slots);
    const keys = Object.keys(slots).filter((k) => /^[A-Z0-9_.]{1,64}$/.test(k) && !k.startsWith("DATA.") && obj(slots[k]).state !== "not-applicable");
    keys.sort((a, b) => {
      const ia = SLOT_ORDER.indexOf(a), ib = SLOT_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
    });
    const wrap = el("div", { role: "group", "aria-label": "Copyright, warranty and licence" });
    for (const k of keys) {
      if (k === "LEGAL.SOURCE_OFFER") continue; // shown under "Source code access"
      wrap.append(slotBlock(k, slots[k]));
    }
    if (!wrap.children.length) wrap.append(el("p", { class: "note", text: "No legal text slots are listed in this build record." }));
    return wrap;
  };

  const sourceBlock = (rec) => {
    const sa = obj(rec.source_access);
    const slots = obj(obj(rec.legal).slots);
    const wrap = el("div");
    if (sa.state === "not-published") {
      wrap.append(el("p", { text: "Source access for this build is not published yet (private candidate)." }));
    } else if (sa.state === "offer-recorded") {
      wrap.append(el("p", { text: "A source access text is recorded for this build:" }));
    } else {
      wrap.append(el("p", { text: "Source access information is not available in this view." }));
    }
    for (const k of ["LEGAL.SOURCE_OFFER", "DATA.SOURCE_LOCATION", "DATA.SOURCE_COMMIT"]) {
      if (slots[k] === undefined || obj(slots[k]).state === "not-applicable") continue;
      wrap.append(slotBlock(k, slots[k]));
    }
    return wrap;
  };

  // ── viewer ──
  const closeViewer = () => {
    $("viewer").hidden = true;
    $("viewer-text").textContent = "";
    const back = viewerOpener;
    viewerOpener = null;
    if (back && back.isConnected) back.focus();
  };
  const openViewer = (opener, title, note, text) => {
    const enc = new TextEncoder();
    let shown = text, extra = "";
    const bytes = enc.encode(text);
    if (bytes.length > MAX_VIEW_BYTES) {
      shown = new TextDecoder().decode(bytes.subarray(0, MAX_VIEW_BYTES));
      extra = " Showing the first " + MAX_VIEW_BYTES + " of " + bytes.length + " bytes; the complete file is in the Licenses folder of the application.";
    }
    viewerOpener = opener;
    $("viewer-title").textContent = title;
    $("viewer-note").textContent = note + extra;
    $("viewer-text").textContent = shown;
    $("viewer").hidden = false;
    $("viewer").focus();
  };
  const viewFile = (opener, file, title, note) => async () => {
    setStatus("Loading " + title + "…");
    try {
      const text = await invoke("about_text", { slug: file });
      setStatus("");
      openViewer(opener(), title, note, text);
    } catch (e) {
      setStatus("Could not show the text: " + e);
    }
  };

  const viewButton = (label, onclick) => {
    const b = el("button", { type: "button", text: "View", "aria-label": label });
    b.onclick = onclick(b);
    return b;
  };

  const fileRows = (rec) => {
    const ul = el("ul", { class: "rows", "aria-label": "Licence files" });
    const files = arr(rec.licence_files).slice(0, 50);
    for (const f of files) {
      const o = obj(f), name = str(o.file);
      if (!name) continue;
      const li = el("li", null,
        el("span", { class: "name mono", text: name }),
        el("span", { text: str(o.label) }),
        el("span", { class: "note", text: str(o.bytes) ? str(o.bytes) + " bytes" : "" }));
      li.append(viewButton("View " + name, (b) => async () => {
        const t = "File " + name;
        await viewFile(() => b, name, t, str(o.sha256) ? "sha256 " + str(o.sha256) : "")();
      }));
      ul.append(li);
    }
    return ul;
  };

  const loadNotices = async () => {
    if (noticesBytes) return noticesBytes;
    const file = str(obj(record.notices).file);
    const text = await invoke("about_text", { slug: file });
    noticesBytes = new TextEncoder().encode(text);
    return noticesBytes;
  };

  // A notice is a byte range of the notices file as shipped (offset and length come from the record).
  const viewNotice = (aria, title, n, off, len) => viewButton(aria, (b) => async () => {
    setStatus("Loading " + title + "…");
    try {
      const bytes = await loadNotices();
      if (off + len > bytes.length) throw new Error("ABOUT_OFFSET_MISMATCH: the notice position does not fit the notices file of this build.");
      setStatus("");
      openViewer(b, title, "Part of " + str(n.file) + ", as shipped.", new TextDecoder().decode(bytes.subarray(off, off + len)));
    } catch (e) { setStatus("Could not show the text: " + e); }
  });

  const componentRows = (rec, filter) => {
    const n = obj(rec.notices);
    const comps = arr(n.components);
    const f = filter.trim().toLowerCase();
    const ul = el("ul", { class: "rows", "aria-label": "Third-party components" });
    let shown = 0, matched = 0;
    for (const c of comps) {
      const o = obj(c), name = str(o.name), ver = str(o.version);
      const label = (name + " " + ver).trim();
      if (!label) continue;
      if (f && !(label + " " + str(o.declared)).toLowerCase().includes(f)) continue;
      matched++;
      if (shown >= MAX_ROWS) continue;
      shown++;
      const li = el("li", null,
        el("span", { class: "name", text: label }),
        el("span", { class: "note", text: str(o.declared) ? "declared: " + str(o.declared) : "" }));
      if (o.status === "text-not-found") li.append(el("span", { class: "badge", text: "Licence text not found in this build" }));
      const off = o.offset, len = o.length;
      if (Number.isInteger(off) && Number.isInteger(len) && off >= 0 && len > 0 && o.status !== "text-not-found") {
        li.append(viewNotice("View notice for " + label, "Notice for " + label, n, off, len));
      }
      ul.append(li);
    }
    // statically linked libraries that are not package components (same file, same slicing)
    for (const r of arr(n.reference_notices)) {
      const o = obj(r), title = str(o.title);
      if (!title) continue;
      if (f && !(title + " " + str(o.declared)).toLowerCase().includes(f)) continue;
      matched++;
      if (shown >= MAX_ROWS) continue;
      shown++;
      const li = el("li", null,
        el("span", { class: "name", text: title }),
        el("span", { class: "note", text: str(o.declared) ? "declared: " + str(o.declared) : "" }));
      if (o.status === "text-not-found") li.append(el("span", { class: "badge", text: "Licence text not found in this build" }));
      if (Number.isInteger(o.offset) && Number.isInteger(o.length) && o.offset >= 0 && o.length > 0 && o.status !== "text-not-found") {
        li.append(viewNotice("View notice for " + title, "Notice for " + title, n, o.offset, o.length));
      }
      ul.append(li);
    }
    return { ul, matched, shown, total: comps.length + arr(n.reference_notices).length };
  };

  const noticesBlock = (rec) => {
    const n = obj(rec.notices);
    const wrap = el("div");
    const total = arr(n.components).length;
    wrap.append(el("p", { class: "note", text: total + " components listed. Licences are shown as declared in each package; they are not verified." }));
    if (str(n.file)) {
      const ul = el("ul", { class: "rows", "aria-label": "Notices file" });
      const li = el("li", null, el("span", { class: "name mono", text: str(n.file) }), el("span", { class: "note", text: str(n.bytes) ? str(n.bytes) + " bytes" : "" }));
      li.append(viewButton("View " + str(n.file), (b) => viewFile(() => b, str(n.file), "File " + str(n.file), str(n.sha256) ? "sha256 " + str(n.sha256) : "")));
      ul.append(li);
      wrap.append(ul);
    }
    const label = el("label", { class: "filter", for: "comp-filter", text: "Filter components" });
    const input = el("input", { id: "comp-filter", type: "search", maxlength: "100", autocomplete: "off" });
    const holder = el("div");
    const count = el("div", { role: "status", "aria-live": "polite", class: "note" });
    let timer = null;
    const draw = () => {
      const r = componentRows(rec, input.value);
      holder.replaceChildren(r.ul);
      count.textContent = r.matched + " of " + r.total + " entries" + (r.matched > r.shown ? " (first " + r.shown + " shown)" : "");
    };
    input.oninput = () => { clearTimeout(timer); timer = setTimeout(draw, 150); };
    wrap.append(label, input, count, holder);
    draw();
    return wrap;
  };

  const section = (id, title, ...kids) => el("section", { "aria-labelledby": id }, el("h3", { id, text: title }), ...kids);

  const renderAbout = (rec) => {
    $("about-error").hidden = true;
    $("about-body").replaceChildren(
      section("h-product", "Product and version", factRows(rec)),
      section("h-legal", "Copyright, warranty and licence", legalBlock(rec)),
      section("h-source", "Source code access", sourceBlock(rec)),
      section("h-texts", "Licence texts and third-party notices", fileRows(rec), noticesBlock(rec)),
    );
    setStatus("");
  };

  const loadAbout = async (force) => {
    if (record && !force) return;
    record = null; noticesBytes = null;
    $("about-error").hidden = true;
    $("about-body").replaceChildren();
    setStatus("Loading build information…");
    let raw;
    try { raw = await invoke("about_info"); }
    catch (e) { showError(String(e)); return; }
    let rec;
    try { rec = JSON.parse(raw); } catch (e) { showError("ABOUT_RECORD_INVALID: the build record is not valid JSON."); return; }
    if (obj(rec).schema !== ABOUT_SCHEMA) { showError("ABOUT_RECORD_INVALID: the build record has an unsupported schema."); return; }
    record = rec;
    renderAbout(rec);
  };

  const showAbout = () => {
    $("view-main").hidden = true;
    $("view-about").hidden = false;
    $("about-title").focus();
    loadAbout(false);
  };
  const hideAbout = () => {
    $("viewer").hidden = true;
    $("view-about").hidden = true;
    $("view-main").hidden = false;
    $("open-about").focus();
  };
  $("open-about").onclick = showAbout;
  $("about-back").onclick = hideAbout;
  $("viewer-close").onclick = closeViewer;
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    if (!$("viewer").hidden) closeViewer();
    else if (!$("view-about").hidden) hideAbout();
  });
})();
