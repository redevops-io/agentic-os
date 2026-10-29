// Runs the <script> of the control-plane dashboard against a minimal DOM stub and prints what it rendered and which requests it made (no browser, no network).
// usage: node _dashboard_js_harness.js <page.html> <scenario.json>
//   scenario: {"responses": {"<url>": {"status": 200, "body": ...}}, "steps": [ {"type": [id, value]}, {"select": [id, value]}, {"click": id}, {"key": [id, "Enter"]},
//              {"tick": 1}, {"event": "pagehide"} ]}
const fs = require("fs"), vm = require("vm");
const html = fs.readFileSync(process.argv[2], "utf8");
const scenario = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).join("\n");

class El {
  constructor(tag, id) { this.tag = tag; this.id = id || ""; this.children = []; this.className = ""; this._text = ""; this.value = ""; this.listeners = {}; this.dataset = {}; this.style = {}; this.attrs = {}; }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(t, f) { this.listeners[t] = f; }
  set innerHTML(v) { this.children = []; this._text = ""; this._html = String(v); }
  get innerHTML() { return this._html || ""; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(" "); }
  setAttribute(k, v) { this.attrs[k] = v; }
  remove() {}
}
const byId = {};
for (const m of html.matchAll(/<(\w+)[^>]*\bid="([^"]+)"[^>]*>/g)) byId[m[2]] = new El(m[1], m[2]);
if (byId.credKind) byId.credKind.value = "key";
const calls = [], consoleOut = [], windowListeners = {}, intervals = [];
let storageTouched = false;
const trap = () => { storageTouched = true; throw new Error("browser storage used"); };
const sandbox = {
  document: { getElementById: id => byId[id] || null, createElement: t => new El(t), get cookie() { trap(); }, set cookie(v) { trap(); } },
  window: { addEventListener: (t, f) => { windowListeners[t] = f; } },
  get localStorage() { return trap(); }, get sessionStorage() { return trap(); }, get indexedDB() { return trap(); },
  location: { get hash() { trap(); }, get search() { trap(); } },
  console: { log: (...a) => consoleOut.push(a.join(" ")), error: (...a) => consoleOut.push(a.join(" ")), warn: (...a) => consoleOut.push(a.join(" ")), info: (...a) => consoleOut.push(a.join(" ")) },
  setInterval: (f, ms) => { intervals.push(f); return intervals.length; },
  fetch: async (url, opts) => {
    calls.push({ url: String(url), headers: (opts && opts.headers) || {}, credentials: opts && opts.credentials, cache: opts && opts.cache });
    const f = scenario.responses[String(url)] || { status: 404, body: {} };
    return { ok: f.status >= 200 && f.status < 300, status: f.status, json: async () => f.body, text: async () => JSON.stringify(f.body) };
  },
  Promise, setTimeout, String, Object, Array, JSON,
};
vm.createContext(sandbox);
const wait = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  try { vm.runInContext(script, sandbox); } catch (e) { console.log(JSON.stringify({ error: String(e) })); return; }
  await wait(30);
  const snapshots = [];
  const snap = () => { const o = {}; for (const id of ["summary", "groups", "approvals", "credState", "credValue"]) o[id] = { text: byId[id].textContent, value: byId[id].value }; return o; };
  snapshots.push({ step: "load", ...snap(), calls: calls.length });
  for (const st of scenario.steps || []) {
    if (st.type) byId[st.type[0]].value = st.type[1];
    if (st.select) byId[st.select[0]].value = st.select[1];
    if (st.click) await byId[st.click].listeners.click({});
    if (st.key) await byId[st.key[0]].listeners.keydown({ key: st.key[1] });
    if (st.tick) for (const f of intervals) await f();
    if (st.event) await windowListeners[st.event]({});
    await wait(30);
    snapshots.push({ step: JSON.stringify(st), ...snap(), calls: calls.length });
  }
  console.log(JSON.stringify({ calls, snapshots, console: consoleOut, storageTouched, windowEvents: Object.keys(windowListeners), intervals: intervals.length }));
})();
