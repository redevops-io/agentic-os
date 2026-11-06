// The connection screen. It talks only to the shell's own commands; it holds no business logic and never sees a token
// after it is saved (the shell returns only `has_credential`). Wording matches the web UI (classifyConnectionError).
const invoke = (name, args) => window.__TAURI__.core.invoke(name, args || {});
const $ = (id) => document.getElementById(id);
const statusEl = $("status");

const MSG = {
  connecting:      { tone: "run",  title: "Connecting…", detail: "" },
  restoring:       { tone: "run",  title: "Reconnecting to your deployment…", detail: "" },
  ok:              { tone: "ok",   title: "Connected.", detail: "" },
  unavailable:     { tone: "warn", title: "The runtime isn't responding", detail: "Your work is safe — everything is saved on the server. This will reconnect by itself." },
  unauthenticated: { tone: "bad",  title: "This deployment didn't accept your sign-in", detail: "Check the address and access token, or ask your administrator for access. Nothing was changed." },
  incompatible:    { tone: "bad",  title: "This deployment is out of date", detail: "It doesn't have the Today feed yet. Update the runtime, then reconnect." },
  invalid:         { tone: "warn", title: "Connect to your deployment", detail: "Enter its address below." },
  connect:         { tone: "run",  title: "Connect to your deployment", detail: "Enter its address below." },
};
function show(key, extra) {
  const m = MSG[key] || MSG.connect;
  statusEl.hidden = false;
  statusEl.className = "status " + m.tone;
  statusEl.setAttribute("role", m.tone === "bad" ? "alert" : "status");
  statusEl.textContent = "";
  const t = document.createElement("strong"); t.textContent = m.title; statusEl.appendChild(t);
  const d = document.createElement("span"); d.className = "detail"; d.textContent = extra || m.detail; statusEl.appendChild(d);
}
function fail(e) { statusEl.hidden = false; statusEl.className = "status bad"; statusEl.setAttribute("role", "alert"); statusEl.textContent = String(e); }

let retryTimer = null, attempt = 0;
function stopRetry() { if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; } attempt = 0; }
// While the runtime is merely down, keep trying (2s, 4s … 30s) and go straight back in when it answers.
function retrySoon() {
  stopRetry();
  const wait = () => { const ms = Math.min(2000 * 2 ** attempt, 30000); attempt += 1; retryTimer = setTimeout(tick, ms); };
  const tick = async () => {
    try {
      const s = await invoke("connection_probe");
      if (s.state === "ok") { stopRetry(); await invoke("connection_open"); return; }
      if (s.state !== "unavailable") { stopRetry(); show(s.state); return; }
    } catch (_) { /* keep waiting */ }
    wait();
  };
  wait();
}

async function load() {
  const state = new URLSearchParams(location.search).get("state") || "";
  let info;
  try { info = await invoke("connection_get"); } catch (e) { return fail(e); }
  if (info.url) $("url").value = info.url;
  $("label").value = info.label || "";
  $("forget").hidden = !info.url;
  $("browser").hidden = !info.url;
  $("token-hint").textContent = info.credential_store_available
    ? (info.has_credential ? "A token is saved in your computer's secure credential store. Enter a new one to replace it." : "Stored in your computer's secure credential store — never in a file, and never shown again.")
    : "This computer has no secure credential store, so a token can't be saved. You can still connect without one.";
  if (!info.credential_store_available) { $("token").disabled = true; }
  invoke("docker_available").then((ok) => { $("docker").hidden = !ok; }).catch(() => {});

  if (state === "unavailable") { show("unavailable"); retrySoon(); return; }
  if (["unauthenticated", "incompatible", "invalid", "connect"].includes(state)) { show(state); return; }
  if (info.url) {                       // a relaunch: restore the workspace without a click
    show("restoring");
    try {
      const s = await invoke("connection_probe");
      if (s.state === "ok") { await invoke("connection_open"); return; }
      show(s.state); if (s.state === "unavailable") retrySoon();
    } catch (e) { fail(e); }
    return;
  }
  show("connect");
}

$("connect").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  stopRetry();
  const btn = $("go"); btn.disabled = true; show("connecting");
  try {
    const s = await invoke("connection_save", { url: $("url").value, token: $("token").value || null, label: $("label").value || null });
    $("token").value = "";                                    // never keep the token in the page once it is saved
    if (s.state === "ok") show("ok"); else { show(s.state); if (s.state === "unavailable") retrySoon(); }
  } catch (e) { fail(e); }
  finally { btn.disabled = false; }
});

$("local").addEventListener("click", async () => {
  stopRetry(); show("connecting", "Starting the runtime on this computer…");
  try {
    const url = await invoke("start_native");
    $("url").value = url; $("token").value = "";
    // the runtime needs a moment to listen; the save probes and, if it is not up yet, the page retries by itself
    const s = await invoke("connection_save", { url, token: null, label: "This computer" });
    if (s.state === "ok") show("ok"); else { show(s.state); if (s.state === "unavailable") retrySoon(); }
  } catch (e) { fail(e); }
});

$("forget").addEventListener("click", async () => {
  if (!confirm("Forget this connection? The saved token is removed from the secure credential store.")) return;
  stopRetry();
  try { await invoke("connection_forget"); $("url").value = ""; $("label").value = ""; $("token").value = ""; $("forget").hidden = true; show("connect"); }
  catch (e) { fail(e); }
});

const out = $("out"), say = (t) => { out.textContent = t; };
const run = (name) => async () => { say("Working…"); try { say((await invoke(name)) || "Done."); } catch (e) { say("Error:\n" + e); } };
$("check").addEventListener("click", run("device_report"));
$("stop").addEventListener("click", run("stop_native"));
$("browser").addEventListener("click", run("open_in_browser"));
$("docker-up").addEventListener("click", run("advanced_docker_up"));
$("docker-down").addEventListener("click", run("advanced_docker_down"));
$("docker-purge").addEventListener("click", async () => {
  if (!confirm("Remove the container tier's data (volumes)? Native data is untouched.")) return;
  await run("advanced_docker_purge")();
});

load();
