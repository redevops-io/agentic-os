// Tiny DOM helpers. Data is only ever written with textContent / createTextNode: no markup string is ever parsed anywhere in this UI.
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v === null || v === undefined) continue;
    if (k === "class") el.className = v;
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

export function pill(text, cls) { return h("span", { class: `pill ${cls || ""}` }, text); }

export function sev(s) { return pill(s, `sev-${s}`); }

const STATE_CLASS = {
  AWAITING_APPROVAL: "st-warn", APPROVED: "st-warn", EXECUTED: "st-ok", FAILED: "st-bad", REJECTED: "st-neutral",
  CONTAINED: "st-ok", INVESTIGATING: "st-warn", OPEN: "st-warn", RESOLVED: "st-ok", CLOSED: "st-neutral",
  verified: "st-ok", unverified: "st-bad", failed: "st-bad", dry_run: "st-warn",
};
export function state(s) { return pill(String(s).replaceAll("_", " "), STATE_CLASS[s] || "st-neutral"); }

export function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
}

export function short(id, n = 14) { return id && id.length > n ? id.slice(0, n) + "…" : id || ""; }

// A table whose rows turn into labelled cards at narrow widths (CSS: table.stack). `cols` = [{label, cell(row) -> node|string}].
export function table(cols, rows, caption) {
  const head = h("tr", null, cols.map((c) => h("th", { scope: "col" }, c.label)));
  const body = rows.map((r) => h("tr", null, cols.map((c) => h("td", { "data-label": c.label }, c.cell(r)))));
  const t = h("table", { class: "stack" }, caption ? h("caption", { class: "muted" }, caption) : null, h("thead", null, head), h("tbody", null, body));
  return h("div", { class: "scroll", role: "region", "aria-label": caption || "table", tabindex: "0" }, t);
}
