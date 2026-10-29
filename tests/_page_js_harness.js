// Runs the <script> of a rendered permissions page against a minimal DOM stub and prints what it rendered (no browser, no network).
// usage: node _page_js_harness.js <page.html> <fixtures.json>   fixtures: {"<url suffix>": {"status":200,"body":<json>}}
const fs = require("fs"), vm = require("vm");
const html = fs.readFileSync(process.argv[2], "utf8");
const fixtures = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).join("\n");
const els = {}, calls = [];
function el(id) {
  if (!els[id]) els[id] = { id, value: fixtures.__values && fixtures.__values[id] || "", innerHTML: "", textContent: "", style: {}, className: "", dataset: {},
    listeners: {}, addEventListener(t, f) { this.listeners[t] = f; }, closest() { return null; }, querySelectorAll() { return []; } };
  return els[id];
}
const sandbox = {
  document: { getElementById: el, querySelectorAll: () => [] },
  // a page that keeps the admin key in memory never touches browser storage: with fixtures.__storage_throws any access is an error
  get localStorage() { if (fixtures.__storage_throws) throw new Error("browser storage used"); return { getItem: (k) => (fixtures.__ls || {})[k] || "", setItem: () => {} }; },
  get sessionStorage() { if (fixtures.__storage_throws) throw new Error("browser storage used"); return { getItem: () => "", setItem: () => {} }; },
  get indexedDB() { if (fixtures.__storage_throws) throw new Error("browser storage used"); return {}; },
  alert: () => {}, encodeURIComponent, console,
  fetch: async (url, opts) => {
    calls.push({ url: String(url), method: (opts && opts.method) || "GET", headers: (opts && opts.headers) || {} });
    const key = Object.keys(fixtures).filter(k => k !== "__values").sort((a, b) => b.length - a.length).find(k => String(url).endsWith(k));
    const f = key ? fixtures[key] : { status: 404, body: {} };
    return { ok: f.status < 400, status: f.status, json: async () => f.body, text: async () => JSON.stringify(f.body) };
  },
  setTimeout, Promise,
};
vm.createContext(sandbox);
(async () => {
  try { vm.runInContext(script, sandbox); } catch (e) { console.log(JSON.stringify({ error: String(e) })); return; }
  await new Promise(r => setTimeout(r, 50));
  // __keystrokes: {id: ["a","ab",...]} fires the element's 'input' listener, if any, once per prefix, like typing
  for (const [id, seq] of Object.entries(fixtures.__keystrokes || {})) for (const v of seq) { els[id].value = v; if (els[id].listeners.input) await els[id].listeners.input({ target: els[id] }); await new Promise(r => setTimeout(r, 20)); }
  for (const [id, v] of Object.entries(fixtures.__type || {})) { els[id].value = v; if (els[id].listeners.input) await els[id].listeners.input({ target: els[id] }); await new Promise(r => setTimeout(r, 50)); }
  for (const id of (fixtures.__press || [])) { await els[id].listeners.click({ target: { closest: () => null } }); await new Promise(r => setTimeout(r, 50)); }
  // simulate a click on the first remove button of every rendered table
  const clicks = [];
  for (const id of Object.keys(els)) {
    const l = els[id].listeners.click;
    const m = els[id].innerHTML.match(/data-id="([^"]*)"/);
    if (l && m && /button class="del"/.test(els[id].innerHTML)) {
      const dec = m[1].replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
      await l({ target: { closest: () => ({ dataset: { id: dec } }) } });
      clicks.push(dec);
    }
  }
  await new Promise(r => setTimeout(r, 50));
  const out = {}; for (const id of Object.keys(els)) out[id] = { innerHTML: els[id].innerHTML, textContent: els[id].textContent, value: els[id].value, listeners: Object.keys(els[id].listeners) };
  console.log(JSON.stringify({ els: out, calls, clicks }));
})();
