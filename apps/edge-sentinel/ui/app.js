import { h, clear, pill, sev, state, when, short, table } from "/ui/dom.js";
import { get, post, patch, del, q, setCsrf, ApiError } from "/ui/api.js";

const root = document.getElementById("app");
const ctx = { user: null, info: null, head: 0, timer: null, view: null };
const ROLE_RANK = { viewer: 0, operator: 1, admin: 2 };
const can = (role) => ctx.user && ROLE_RANK[ctx.user.role] >= ROLE_RANK[role];

// ── shell ────────────────────────────────────────────────────────────────────────────────────────────────
let mainEl = null, statusEl = null, navEl = null;

function nav() {
  const items = [["/ui/", "Overview"], ["/ui/events", "Events"], ["/ui/cases", "Cases"], ["/ui/actions", "Actions"], ["/ui/history", "History"]];
  if (can("admin")) items.push(["/ui/users", "Users"]);
  items.push(["/ui/about", "About"]);
  return items;
}

function renderShell() {
  clear(root);
  const tenant = ctx.info && ctx.info.tenant_label !== "Edge Sentinel" ? ctx.info.tenant_label : "";
  const mode = ctx.info ? ctx.info.mode : {};
  const badges = [];
  if (mode.demo) badges.push(pill("DEMO: synthetic data", "st-warn"));
  else if (mode.dry_run) badges.push(pill("DRY RUN: nothing is banned", "st-warn"));
  else badges.push(pill("LIVE: approved bans are enforced", "st-ok"));
  const bar = h("header", { class: "bar" },
    h("h1", null, "Edge Sentinel"), tenant ? h("span", { class: "tenant" }, tenant) : null, ...badges, h("span", { class: "spacer" }),
    h("span", { class: "muted" }, `${ctx.user.name} (${ctx.user.role})`),
    h("button", { type: "button", on: { click: logout } }, "Sign out"));
  navEl = h("nav", { class: "tabs", "aria-label": "Main" });
  statusEl = h("div", { class: "muted status", role: "status", "aria-live": "polite" });
  mainEl = h("main", { id: "main", tabindex: "-1" });
  root.append(bar, navEl, statusEl, mainEl);
}

function renderNav(path) {
  clear(navEl);
  for (const [href, label] of nav()) {
    const active = href === "/ui/" ? path === "/ui/" : path.startsWith(href);
    navEl.append(h("a", { href, "aria-current": active ? "page" : false, on: { click: go } }, label));
  }
}

function go(ev) {
  const a = ev.currentTarget;
  if (a.target || ev.metaKey || ev.ctrlKey || ev.shiftKey) return;
  ev.preventDefault();
  navigate(a.getAttribute("href"));
}

export function navigate(path) {
  if (path !== location.pathname + location.search) history.pushState({}, "", path);
  route();
}
window.addEventListener("popstate", route);

// ── helpers ──────────────────────────────────────────────────────────────────────────────────────────────
function link(href, text, cls) { return h("a", { href, class: cls || false, on: { click: go } }, text); }

function errorBox(e) {
  return h("div", { class: "banner bad", role: "alert" }, h("strong", null, e.code && e.code !== "error" ? `${e.code}: ` : ""), e.message || String(e));
}

async function guarded(fn, into) {
  try { return await fn(); } catch (e) {
    if (e instanceof ApiError && e.status === 401) { await showLogin("Your session ended. Sign in again."); return null; }
    into.append(errorBox(e instanceof ApiError ? e : new Error(String(e && e.message || e))));
    return null;
  }
}

function announce(msg) { if (statusEl) statusEl.textContent = msg; }

function empty(text, remedy) {
  return h("div", { class: "card muted" }, text, remedy ? h("div", null, remedy) : null);
}

function modeBanners() {
  const out = [];
  for (const u of (ctx.info && ctx.info.unavailable) || []) {
    if (["CORE_UNREACHABLE", "DEMO_MODE", "DRY_RUN", "NO_WATCHER_LOGIN"].includes(u.code)) {
      out.push(h("div", { class: `banner ${u.code === "CORE_UNREACHABLE" ? "bad" : "warn"}` }, h("strong", null, `${u.feature} unavailable: `), u.reason, u.remedy ? h("div", { class: "muted" }, u.remedy) : null));
    }
  }
  return out;
}

// ── views ────────────────────────────────────────────────────────────────────────────────────────────────
async function viewOverview() {
  const wrap = h("div");
  wrap.append(h("h2", { tabindex: "-1" }, "Overview"), ...modeBanners());
  const [posture, waiting, changes] = await Promise.all([get("/api/v1/posture"), get("/api/v1/actions?state=AWAITING_APPROVAL&limit=50"), get("/api/v1/changes?limit=10&after=0")]);
  const kpi = (label, value, note) => h("div", { class: "card" }, h("div", { class: "muted" }, label), h("div", { class: "kpi" }, String(value)), note ? h("div", { class: "muted" }, note) : null);
  wrap.append(h("div", { class: "grid" },
    kpi("Awaiting your approval", waiting.items.length, waiting.items.length ? "proposed actions need a decision" : "nothing is waiting"),
    kpi("Active decisions", posture.decisions_active, "bans CrowdSec holds"),
    kpi("Alerts", posture.alerts_total, `${posture.sources} source address(es)`),
    kpi("Data", posture.source === "fixture" ? "synthetic" : "live", posture.as_of ? `read ${when(posture.as_of)}` : "")));
  if (waiting.items.length) {
    wrap.append(h("h3", null, "Waiting for a decision"), table([
      { label: "Proposed", cell: (a) => `${a.capability.replace("sentinel.", "")} ${a.params.ip}` },
      { label: "Case", cell: (a) => link(`/ui/cases/${a.case_id}`, short(a.case_id, 22)) },
      { label: "State", cell: (a) => state(a.state) },
    ], waiting.items, "Actions waiting for a decision"));
  }
  const sevs = Object.entries(posture.by_severity || {});
  wrap.append(h("h3", null, "Alerts by severity"), sevs.length ? h("div", { class: "row" }, sevs.map(([k, v]) => h("span", null, sev(k), ` ${v}`))) : empty("No alerts are held by CrowdSec."));
  wrap.append(h("h3", null, "Recent changes"), changes.items.length ? h("ul", { class: "plain" }, [...changes.items].reverse().map((c) =>
    h("li", null, h("span", { class: "muted" }, when(c.at) + " "), h("strong", null, c.type), " ", c.actor, " ", c.case_id ? link(`/ui/cases/${c.case_id}`, short(c.case_id, 22)) : ""))) : empty("Nothing has happened yet."));
  return wrap;
}

async function viewEvents() {
  const wrap = h("div"), params = new URLSearchParams(location.search);
  wrap.append(h("h2", { tabindex: "-1" }, "Events"), ...modeBanners());
  const sel = h("select", { id: "f-sev" }, ["", "critical", "high", "medium", "low"].map((s) => h("option", { value: s, selected: params.get("severity") === s }, s || "any severity")));
  const ip = h("input", { id: "f-ip", value: params.get("source_ip") || "", autocomplete: "off" });
  wrap.append(h("form", { class: "inline", on: { submit: (e) => { e.preventDefault(); navigate("/ui/events" + q({ severity: sel.value, source_ip: ip.value.trim() })); } } },
    h("div", { class: "field" }, h("label", { for: "f-sev" }, "Severity"), sel), h("div", { class: "field" }, h("label", { for: "f-ip" }, "Source address"), ip), h("button", { type: "submit" }, "Filter")));
  const r = await get("/api/v1/events" + q({ severity: params.get("severity"), source_ip: params.get("source_ip"), limit: 100 }));
  wrap.append(r.items.length ? table([
    { label: "Time", cell: (e) => when(e.at) }, { label: "Severity", cell: (e) => sev(e.severity) }, { label: "Scenario", cell: (e) => h("code", null, e.scenario) },
    { label: "Source", cell: (e) => h("code", null, e.source_ip) }, { label: "Events", cell: (e) => String(e.events_count) },
    { label: "Case", cell: (e) => (e.case_id ? link(`/ui/cases/${e.case_id}`, short(e.case_id, 22)) : "not a case yet") }], r.items, "CrowdSec alerts, newest first")
    : empty("CrowdSec holds no alerts that match.", r.status && r.status.ok === false ? `The alert read failed: ${r.status.note}` : ""));
  return wrap;
}

async function viewCases() {
  const wrap = h("div"), params = new URLSearchParams(location.search);
  wrap.append(h("h2", { tabindex: "-1" }, "Cases"), ...modeBanners());
  const status = h("select", { id: "f-status" }, ["", "AWAITING_APPROVAL", "INVESTIGATING", "CONTAINED", "OPEN"].map((s) => h("option", { value: s, selected: params.get("status") === s }, s ? s.replaceAll("_", " ").toLowerCase() : "any status")));
  const text = h("input", { id: "f-q", value: params.get("q") || "", autocomplete: "off" });
  wrap.append(h("form", { class: "inline", on: { submit: (e) => { e.preventDefault(); navigate("/ui/cases" + q({ status: status.value, q: text.value.trim() })); } } },
    h("div", { class: "field" }, h("label", { for: "f-status" }, "Status"), status), h("div", { class: "field" }, h("label", { for: "f-q" }, "Search"), text), h("button", { type: "submit" }, "Filter")));
  const r = await get("/api/v1/cases" + q({ status: params.get("status"), q: params.get("q"), limit: 100 }));
  wrap.append(r.items.length ? table([
    { label: "Severity", cell: (c) => sev(c.severity) }, { label: "Case", cell: (c) => link(`/ui/cases/${c.case_id}`, c.title) },
    { label: "Status", cell: (c) => state(c.status) }, { label: "Proposed", cell: (c) => (c.open_action ? `${c.open_action.capability.replace("sentinel.", "")} ${c.open_action.parameters.ip}` : "none") },
    { label: "Last seen", cell: (c) => when(c.last_seen) }], r.items, "Security cases") : empty("There are no cases.", "A case opens when CrowdSec raises an alert, or when an operator stages one below."));
  if (can("operator")) wrap.append(manualForm());
  return wrap;
}

function manualForm() {
  const type = h("select", { id: "m-type" }, h("option", { value: "manual_block" }, "Ban an address"), h("option", { value: "manual_unblock" }, "Lift a ban"));
  const ip = h("input", { id: "m-ip", required: true, autocomplete: "off", inputmode: "text" });
  const reason = h("input", { id: "m-reason", maxlength: "200", autocomplete: "off" });
  const dur = h("input", { id: "m-dur", placeholder: "4h", size: "6", autocomplete: "off" });
  const msg = h("div", { role: "alert" });
  const f = h("form", { class: "inline", on: { submit: async (e) => {
    e.preventDefault(); clear(msg);
    try {
      const r = await post("/api/v1/cases", { type: type.value, ip: ip.value.trim(), reason: reason.value.trim(), duration: dur.value.trim() });
      navigate(`/ui/cases/${r.case_id}`);
    } catch (err) { msg.append(errorBox(err)); }
  } } },
  h("div", { class: "field" }, h("label", { for: "m-type" }, "Action"), type), h("div", { class: "field" }, h("label", { for: "m-ip" }, "Address"), ip),
  h("div", { class: "field" }, h("label", { for: "m-reason" }, "Reason"), reason), h("div", { class: "field" }, h("label", { for: "m-dur" }, "Duration"), dur),
  h("button", { type: "submit" }, "Stage for approval"));
  return h("section", { class: "card", "aria-labelledby": "manual-h" }, h("h3", { id: "manual-h" }, "Stage a manual action"),
    h("p", { class: "muted" }, "This only proposes. A person still has to approve it, and then execute it."), f, msg);
}

function kv(pairs) {
  return h("dl", { class: "kv" }, pairs.filter((p) => p[1] !== "" && p[1] !== null && p[1] !== undefined).flatMap(([k, v]) => [h("dt", null, k), h("dd", null, v)]));
}

function actionCard(c, a) {
  const info = ctx.info, dry = info.mode.dry_run;
  const d = a.decision, r = a.receipt, v = a.verification;
  const card = h("section", { class: "card", "aria-label": `Action ${a.request_id}` },
    h("div", { class: "row" }, h("strong", null, `${a.capability.replace("sentinel.", "")} ${a.params.ip}`), state(a.state)),
    kv([["Action", h("code", null, a.request_id)], ["Digest", h("code", null, short(a.digest, 20))], ["Parameters", h("code", null, JSON.stringify(a.params))],
        ["Decision", d ? `${d.approved ? "approved" : "rejected"} by ${d.actor} (${d.role || "n/a"}) via ${d.via} at ${when(d.at)}` : "none yet"],
        ["Rationale", d && d.rationale], ["Receipt", r ? `${r.status}${r.dry_run ? " (dry run)" : ""} at ${when(r.at)}${r.error ? `: ${r.error}` : ""}` : ""],
        ["Read-back", v ? h("span", null, state(v.state), " ", v.detail) : ""]]));
  const row = h("div", { class: "actions-row" });
  if (c.active && can("operator")) {
    if (a.state === "AWAITING_APPROVAL") {
      row.append(h("button", { type: "button", class: "primary", on: { click: () => confirmDialog({ title: "Approve this action", a, approved: true, dry, kind: "decision" }) } }, "Approve…"),
        h("button", { type: "button", on: { click: () => confirmDialog({ title: "Reject this action", a, approved: false, dry, kind: "decision" }) } }, "Reject…"));
    } else if (a.state === "APPROVED" || (a.state === "FAILED" && d && d.approved)) {
      row.append(h("button", { type: "button", class: "primary", on: { click: () => confirmDialog({ title: dry ? "Execute (dry run)" : "Execute now", a, dry, kind: "execute" }) } }, dry ? "Execute (dry run)…" : "Execute…"),
        h("button", { type: "button", on: { click: () => confirmDialog({ title: "Withdraw the approval", a, approved: false, dry, kind: "decision" }) } }, "Withdraw approval…"));
    }
  } else if (!c.active) {
    row.append(h("span", { class: "muted" }, "CrowdSec no longer holds this alert: the record is kept, but it cannot be acted on."));
  } else if (a.state === "AWAITING_APPROVAL" || a.state === "APPROVED") {
    row.append(h("span", { class: "muted" }, "Your role is viewer: deciding and executing need the operator role."));
  }
  card.append(row);
  return card;
}

function confirmDialog({ title, a, approved, dry, kind }) {
  const dlg = h("dialog", { "aria-labelledby": "dlg-t" });
  const rationale = h("textarea", { id: "dlg-r", rows: "2", maxlength: "200" });
  const err = h("div", { role: "alert" });
  const ip = a.params.ip;
  let consequence;
  if (kind === "decision") consequence = approved ? `Approving records your decision for exactly this action (digest ${short(a.digest, 12)}). Nothing is banned until it is executed.` : "Rejecting records your decision. The action will not run.";
  else consequence = dry ? `DRY RUN: the action is recorded as executed in dry-run mode and nothing is banned. Set SENTINEL_BLOCK_ENABLED=1 to enforce.` :
    `LIVE: CrowdSec will be told to ${a.capability === "sentinel.unblock_ip" ? "lift the ban on" : "ban"} ${ip}. The result is then read back from CrowdSec and shown here as verified or unverified.`;
  const confirm = h("button", { type: "button", class: kind === "execute" && !dry ? "primary danger" : "primary", on: { click: async () => {
    confirm.disabled = true; clear(err);
    try {
      const base = `/api/v1/cases/${ctxCaseId}/actions/${a.request_id}`;
      const out = kind === "decision" ? await post(`${base}/decision`, { approved, digest: a.digest, rationale: rationale.value.trim() }) : await post(`${base}/execute`, { digest: a.digest });
      dlg.close(); announce(kind === "decision" ? "Decision recorded." : (out.verification ? `Executed. Read-back: ${out.verification.state}.` : "Executed."));
      route();
    } catch (e) { confirm.disabled = false; err.append(errorBox(e)); }
  } } }, kind === "decision" ? (approved ? "Approve" : "Reject") : (dry ? "Execute (dry run)" : "Execute"));
  dlg.append(h("h3", { id: "dlg-t" }, title), h("p", null, consequence), kv([["Action", `${a.capability.replace("sentinel.", "")} ${ip}`], ["Digest", h("code", null, short(a.digest, 20))]]),
    kind === "decision" ? h("div", null, h("label", { for: "dlg-r" }, "Rationale (optional)"), rationale) : null, err,
    h("div", { class: "actions-row" }, confirm, h("button", { type: "button", on: { click: () => dlg.close() } }, "Cancel")));
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg); dlg.showModal();
}

let ctxCaseId = "";

async function viewCase(caseId) {
  ctxCaseId = caseId;
  const wrap = h("div");
  const c = await get(`/api/v1/cases/${encodeURIComponent(caseId)}`);
  const s = c.summary;
  wrap.append(h("p", null, link("/ui/cases", "← All cases")), h("h2", { tabindex: "-1" }, s.title), h("div", { class: "row" }, sev(s.severity), state(s.status), h("span", { class: "muted" }, `${c.case.case_type} · first seen ${when(s.first_seen)}`)));
  if (!c.active) wrap.append(h("div", { class: "banner warn" }, "CrowdSec no longer holds the alert behind this case. The stored evidence and history stay readable; the case cannot be decided or executed."));
  wrap.append(...modeBanners().filter((b) => /DRY RUN|DEMO|dry/i.test(b.textContent)));
  wrap.append(h("h3", null, "Proposed actions"), c.actions_detail.length ? h("div", { class: "grid" }, c.actions_detail.map((a) => actionCard({ active: c.active }, a))) : empty("This case proposes no action: it is a signal to investigate."));
  wrap.append(h("h3", null, "Findings"), c.findings.length ? h("ul", { class: "plain" }, c.findings.map((f) => h("li", null, f.claim, " ", h("span", { class: "muted" }, `(${f.status.toLowerCase()}, confidence ${f.confidence})`)))) : empty("No findings."));
  const proof = c.replay && c.replay.deterministic;
  wrap.append(h("h3", null, "Evidence"), h("p", { class: "muted" }, proof === true ? "Replay proof: the case id re-derives from the stored evidence." : proof === false ? "Replay proof: not reproducible from the stored evidence." : (c.replay && c.replay.detail) || ""));
  wrap.append(c.evidence.length ? h("ul", { class: "plain" }, c.evidence.map((e) => evidenceItem(caseId, e))) : empty("No evidence."));
  wrap.append(h("h3", null, "History"), c.history.length ? table([{ label: "Time", cell: (x) => when(x.at) }, { label: "Event", cell: (x) => x.type }, { label: "By", cell: (x) => `${x.actor}${x.via ? ` via ${x.via}` : ""}` },
    { label: "Action", cell: (x) => (x.request_id ? short(x.request_id, 22) : "") }], c.history, "Case history") : empty("No history."));
  return wrap;
}

function evidenceItem(caseId, e) {
  const pre = h("pre", { hidden: true, tabindex: "0", "aria-label": `Evidence ${e.artifact_id}` });
  const url = `/api/v1/cases/${encodeURIComponent(caseId)}/evidence/${encodeURIComponent(e.artifact_id)}`;
  const view = h("button", { type: "button", on: { click: async () => {
    if (!pre.hidden) { pre.hidden = true; view.textContent = "View"; return; }
    try { const r = await fetch(url, { credentials: "same-origin" }); const t = await r.text(); if (!r.ok) throw new Error(t.slice(0, 200)); pre.textContent = JSON.stringify(JSON.parse(t), null, 2); pre.hidden = false; view.textContent = "Hide"; }
    catch (err) { pre.textContent = String(err.message || err); pre.hidden = false; }
  } } }, "View");
  return h("li", null, h("div", { class: "row" }, h("code", null, e.artifact_id), h("span", { class: "muted" }, `${e.kind} from ${e.source}, acquired ${when(e.acquired_at)}, sha256 ${short(e.sha256, 16)}`),
    view, h("a", { href: url, download: `${e.artifact_id}.json` }, "Download")), pre);
}

async function viewActions() {
  const wrap = h("div"), params = new URLSearchParams(location.search), cur = params.get("state") || "AWAITING_APPROVAL";
  wrap.append(h("h2", { tabindex: "-1" }, "Actions"), ...modeBanners());
  const tabs = h("div", { class: "row", role: "group", "aria-label": "Filter by state" });
  for (const s of ["AWAITING_APPROVAL", "APPROVED", "EXECUTED", "FAILED", "REJECTED", ""]) {
    tabs.append(h("a", { href: "/ui/actions" + q({ state: s || "all" }), "aria-current": (s || "all") === cur || (s === "" && cur === "all") ? "page" : false, class: "btn", on: { click: go } }, s ? s.replaceAll("_", " ").toLowerCase() : "all"));
  }
  wrap.append(tabs);
  const r = await get("/api/v1/actions" + q({ state: cur === "all" ? "" : cur, limit: 100 }));
  wrap.append(r.items.length ? table([
    { label: "Action", cell: (a) => `${a.capability.replace("sentinel.", "")} ${a.params.ip}` }, { label: "State", cell: (a) => state(a.state) },
    { label: "Decision", cell: (a) => (a.decision ? `${a.decision.approved ? "approved" : "rejected"} by ${a.decision.actor} via ${a.decision.via}` : "none") },
    { label: "Receipt", cell: (a) => (a.receipt ? `${a.receipt.status}${a.receipt.dry_run ? " (dry run)" : ""}` : "") },
    { label: "Read-back", cell: (a) => (a.verification ? h("span", null, state(a.verification.state), " ", a.verification.detail) : "") },
    { label: "Case", cell: (a) => link(`/ui/cases/${a.case_id}`, short(a.case_id, 22)) }], r.items, "Actions") : empty("No actions in this state."));
  return wrap;
}

// /ui/actions/<act-id>: the deep link other products use for one action; it lands on the case that holds it
async function viewActionLink(requestId) {
  const a = await get(`/api/v1/actions/${encodeURIComponent(requestId)}`);
  history.replaceState({}, "", `/ui/cases/${a.case_id}`);
  return viewCase(a.case_id);
}

async function viewHistory() {
  const wrap = h("div");
  wrap.append(h("h2", { tabindex: "-1" }, "History"));
  const r = await get("/api/v1/history?limit=100");
  wrap.append(r.items.length ? table([{ label: "Time", cell: (x) => when(x.at) }, { label: "Event", cell: (x) => x.type }, { label: "By", cell: (x) => `${x.actor}${x.via ? ` via ${x.via}` : ""}` },
    { label: "Case", cell: (x) => (x.case_id ? link(`/ui/cases/${x.case_id}`, short(x.case_id, 22)) : "") }, { label: "Detail", cell: (x) => h("code", null, JSON.stringify(x.detail)) }], r.items, "Audit history, newest first") : empty("No history yet."));
  return wrap;
}

async function viewUsers() {
  const wrap = h("div");
  wrap.append(h("h2", { tabindex: "-1" }, "Users"));
  if (!can("admin")) return empty("Managing users needs the admin role.");
  const [users, links] = await Promise.all([get("/api/v1/users"), get("/api/v1/actor-links")]);
  const msg = h("div", { role: "alert" });
  const refresh = () => route();
  wrap.append(table([{ label: "Name", cell: (u) => u.name }, { label: "Role", cell: (u) => u.role }, { label: "State", cell: (u) => (u.disabled ? "disabled" : "active") }, { label: "Last sign-in", cell: (u) => when(u.last_login_at) || "never" },
    { label: "Change", cell: (u) => h("div", { class: "row" },
      h("select", { "aria-label": `Role of ${u.name}`, on: { change: async (e) => { try { await patch(`/api/v1/users/${u.name}`, { role: e.target.value }); refresh(); } catch (err) { clear(msg); msg.append(errorBox(err)); e.target.value = u.role; } } } },
        ["viewer", "operator", "admin"].map((r) => h("option", { value: r, selected: r === u.role }, r))),
      h("button", { type: "button", on: { click: async () => { try { await patch(`/api/v1/users/${u.name}`, { disabled: !u.disabled }); refresh(); } catch (err) { clear(msg); msg.append(errorBox(err)); } } } }, u.disabled ? "Enable" : "Disable")) }], users.items, "Users"));
  const name = h("input", { id: "u-name", required: true, autocomplete: "off" }), pw = h("input", { id: "u-pw", type: "password", required: true, minlength: "12", autocomplete: "new-password" });
  const role = h("select", { id: "u-role" }, ["viewer", "operator", "admin"].map((r) => h("option", { value: r, selected: r === "operator" }, r)));
  wrap.append(h("section", { class: "card" }, h("h3", null, "Add a user"), h("form", { class: "inline", on: { submit: async (e) => { e.preventDefault(); clear(msg); try { await post("/api/v1/users", { name: name.value.trim(), password: pw.value, role: role.value }); refresh(); } catch (err) { msg.append(errorBox(err)); } } } },
    h("div", { class: "field" }, h("label", { for: "u-name" }, "Name"), name), h("div", { class: "field" }, h("label", { for: "u-pw" }, "Password (12+ characters)"), pw), h("div", { class: "field" }, h("label", { for: "u-role" }, "Role"), role), h("button", { type: "submit" }, "Add user"))), msg);
  const ext = h("input", { id: "l-ext", placeholder: "projects:local-owner", required: true, autocomplete: "off" }), who = h("select", { id: "l-user" }, users.items.map((u) => h("option", { value: u.name }, u.name)));
  wrap.append(h("h3", null, "Projects links"), h("p", { class: "muted" }, "A Projects user can approve here only when an admin links them to a user with the operator role."),
    links.items.length ? table([{ label: "Projects principal", cell: (l) => h("code", null, l.external_actor) }, { label: "User", cell: (l) => l.user }, { label: "Remove", cell: (l) => h("button", { type: "button", on: { click: async () => { try { await del(`/api/v1/actor-links/${encodeURIComponent(l.external_actor)}`); refresh(); } catch (err) { msg.append(errorBox(err)); } } } }, "Remove") }], links.items, "Actor links") : empty("No links."),
    h("form", { class: "inline", on: { submit: async (e) => { e.preventDefault(); clear(msg); try { await post("/api/v1/actor-links", { external_actor: ext.value.trim(), user: who.value }); refresh(); } catch (err) { msg.append(errorBox(err)); } } } },
      h("div", { class: "field" }, h("label", { for: "l-ext" }, "Projects principal"), ext), h("div", { class: "field" }, h("label", { for: "l-user" }, "Edge Sentinel user"), who), h("button", { type: "submit" }, "Link")));
  return wrap;
}

async function viewAbout() {
  const i = await get("/api/v1/info"); ctx.info = i;
  const wrap = h("div");
  wrap.append(h("h2", { tabindex: "-1" }, "About this deployment"), kv([["Product", `${i.product} ${i.version}`], ["Core", `${i.core.name}, ${i.core.connected ? "reachable" : "not reachable"}${i.core.watcher ? ", watcher login configured" : ""}`],
    ["Mode", i.mode.demo ? "demo (synthetic data, no side effects)" : i.mode.dry_run ? "dry run (approved actions are recorded, nothing is banned)" : "live (approved bans are enforced and read back)"],
    ["Two-person rule", i.mode.require_two_person ? "on: the approver may not execute" : "off"], ["Store", i.store.persistent ? "SQLite on the data volume" : "in memory (nothing survives a restart)"], ["You", `${i.you.name}, role ${i.you.role}, via ${i.you.via}`]]));
  wrap.append(h("h3", null, "What is unavailable here, and why"), i.unavailable.length ? h("ul", { class: "plain" }, i.unavailable.map((u) => h("li", null, h("strong", null, u.feature), " ", pill(u.code, "st-neutral"), " ", u.reason, u.remedy ? h("div", { class: "muted" }, `To change this: ${u.remedy}`) : null))) : empty("Nothing is unavailable."));
  return wrap;
}

// ── login, session, routing ──────────────────────────────────────────────────────────────────────────────
async function showLogin(message) {
  clearTimeout(ctx.timer); ctx.user = null; setCsrf("");
  clear(root);
  const u = h("input", { id: "login-user", autocomplete: "username", required: true }), p = h("input", { id: "login-pass", type: "password", autocomplete: "current-password", required: true });
  const err = h("div", { role: "alert", class: "error" }, message || "");
  const btn = h("button", { type: "submit", class: "primary" }, "Sign in");
  root.append(h("main", { id: "main", class: "login" }, h("h1", null, "Edge Sentinel"), h("p", { class: "muted" }, "Sign in with your user name and password."),
    h("form", { on: { submit: async (e) => {
      e.preventDefault(); btn.disabled = true; err.textContent = "";
      try { const r = await post("/api/v1/session", { username: u.value.trim(), password: p.value }); setCsrf(r.csrf); await start(r.user); }
      catch (ex) { err.textContent = ex.code === "rate_limited" ? "Too many attempts. Wait a minute and try again." : "Wrong user name or password."; btn.disabled = false; p.value = ""; p.focus(); }
    } } }, h("label", { for: "login-user" }, "User name"), u, h("label", { for: "login-pass" }, "Password"), p, h("div", { class: "actions-row" }, btn), err)));
  u.focus();
}

async function logout() { try { await del("/api/v1/session"); } catch { /* the cookie may already be gone */ } await showLogin("You are signed out."); }

async function start(user) {
  ctx.user = user;
  ctx.info = await get("/api/v1/info");
  renderShell();
  const ch = await get("/api/v1/changes?limit=1&after=0");
  ctx.head = ch.head;
  poll();
  route();
}

const ROUTES = [[/^\/ui\/?$/, viewOverview], [/^\/ui\/events\/?$/, viewEvents], [/^\/ui\/cases\/?$/, viewCases], [/^\/ui\/cases\/([^/]+)\/?$/, viewCase],
  [/^\/ui\/actions\/?$/, viewActions], [/^\/ui\/actions\/([^/]+)\/?$/, viewActionLink], [/^\/ui\/history\/?$/, viewHistory], [/^\/ui\/users\/?$/, viewUsers], [/^\/ui\/about\/?$/, viewAbout]];
let renderToken = 0;

async function route() {
  if (!ctx.user) return;
  const path = location.pathname, token = ++renderToken;
  renderNav(path);
  const hit = ROUTES.map(([rx, fn]) => [rx.exec(path), fn]).find(([m]) => m);
  const holder = h("div");
  const view = hit ? hit[1] : async () => h("div", null, h("h2", { tabindex: "-1" }, "Not found"), empty("There is no such page.", "Use the navigation above."));
  const node = await guarded(() => view(...(hit ? hit[0].slice(1).map(decodeURIComponent) : [])), holder);
  if (token !== renderToken || !ctx.user) return;
  clear(mainEl);
  if (node) holder.prepend(node);
  mainEl.append(...holder.childNodes);
  const heading = mainEl.querySelector("h2"); if (heading && document.activeElement === document.body) heading.focus({ preventScroll: true });
  document.title = `${(heading && heading.textContent) || "Edge Sentinel"} · Edge Sentinel`;
}

function poll() {
  clearTimeout(ctx.timer);
  ctx.timer = setTimeout(async () => {
    if (!ctx.user) return;
    if (document.visibilityState === "visible") {
      try {
        const r = await get(`/api/v1/changes?after=${ctx.head}&limit=200`);
        if (r.items.length) {
          ctx.head = r.head; announce(`${r.items.length} new change(s) recorded.`);
          const busy = document.querySelector("dialog[open]") || (document.activeElement && ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName));
          if (!busy) route();
        }
      } catch (e) { if (e instanceof ApiError && e.status === 401) { await showLogin("Your session ended. Sign in again."); return; } }
    }
    poll();
  }, document.visibilityState === "visible" ? 5000 : 30000);
}

(async () => {
  try { const s = await get("/api/v1/session"); setCsrf(s.csrf); await start(s.user); }
  catch (e) { await showLogin(""); }
})();
