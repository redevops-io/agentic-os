// ReDevOps desktop shell — a DELIVERY SURFACE, not a control plane. See ../DESKTOP.md.
//
// What this process does: remember which deployment to use (URL on disk; credential in the OS keychain), show that
// deployment's own UI in a native window, notice when the runtime is away and say so, and add native affordances (tray,
// deep links, window restore). What it deliberately does NOT do: approvals, scheduling, case or capture storage,
// notification state, navigation rules, or agent execution. Those live in the one authoritative backend and the one web
// UI, so the browser and this window can never disagree.
//
// The local runtime (the frozen sidecar) and the optional Docker tier are unchanged from the original launcher.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod config;
mod deeplink;
mod health;
mod probe;
mod secret;

use config::Connection;
use health::{Action, Monitor};
use probe::{Http, State as Health, Status, UreqHttp};
use secret::{KeyringStore, Secret, SecretStore, TokenCache};
use serde::Serialize;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex, RwLock};
use tauri::{AppHandle, Manager, State, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use tauri_plugin_deep_link::DeepLinkExt;

const LOCAL_URL: &str = "http://127.0.0.1:8787";
const WINDOW: &str = "main";

struct Shell {
    conn_path: PathBuf,
    /// the app's own page (connection screen); the target of "leave the deployment"
    home: RwLock<Option<Url>>,
    /// the configured deployment — the only http(s) origin the window may show
    allowed: RwLock<Option<Url>>,
    store: Arc<dyn SecretStore>,
    /// the token is read from the OS keychain once and held here — see `secret::TokenCache`
    tokens: TokenCache,
    http: Arc<dyn Http>,
    monitor: Mutex<Monitor>,
    last: Mutex<Status>,
    server: Mutex<Option<Child>>,
}

type Sh<'a> = State<'a, Arc<Shell>>;

// Test-only observability (cargo feature `selftest`; never in a shipped build). It lets a harness seed a connection,
// then read what the real webview rendered, so "same Today in Chromium and the shell" is measured, not assumed.
#[cfg(feature = "selftest")]
macro_rules! st {
    ($($a:tt)*) => { eprintln!("[selftest] {}", format!($($a)*)); };
}
#[cfg(not(feature = "selftest"))]
macro_rules! st {
    ($($a:tt)*) => {};
}

/// Wrap a journey script so its result reaches the harness. The deployment's page (a remote origin, no IPC) posts to the
/// harness' local listener (fire-and-forget, no-cors); the shell's OWN pages report through IPC, because their CSP
/// deliberately forbids talking to anything else.
#[cfg(feature = "selftest")]
fn selftest_wrap(script: &str, port: &str, tag: &str, ipc: bool) -> String {
    let send = if ipc {
        "window.__TAURI__.core.invoke('selftest_report',{payload:JSON.stringify(__m)})".to_string()
    } else {
        format!("fetch('http://127.0.0.1:{port}/report',{{method:'POST',mode:'no-cors',headers:{{'Content-Type':'text/plain'}},body:JSON.stringify(__m)}})")
    };
    format!(
        "(async()=>{{let __m;try{{const __r=await ({script});__m={{tag:{tag},at:Date.now(),result:__r}};}}catch(e){{__m={{tag:{tag},error:String(e)}};}}{send};}})();",
        script = script, tag = serde_json::to_string(tag).unwrap(), send = send
    )
}

#[cfg(feature = "selftest")]
#[tauri::command]
fn selftest_report(payload: String) {
    eprintln!("[selftest-report] {}", payload.replace('\n', " "));
}

fn unknown() -> Status {
    Status { state: Health::Unavailable, detail: "Not connected yet.".into(), project: None }
}

// ── helpers ────────────────────────────────────────────────────────────────────────────────────────────────────

fn state_name(s: Health) -> &'static str {
    match s {
        Health::Ok => "ok",
        Health::Unavailable => "unavailable",
        Health::Unauthenticated => "unauthenticated",
        Health::Incompatible => "incompatible",
        Health::Invalid => "invalid",
    }
}

/// The keychain account for a deployment. (Selftest builds add a per-run suffix so a test run only ever touches keychain
/// items created by the very binary under test — a rebuilt, unsigned binary is a different program to the keychain.)
fn account(u: &Url) -> String {
    let base = config::account_for(u);
    #[cfg(feature = "selftest")]
    if let Ok(s) = std::env::var("RDO_SELFTEST_ACCOUNT_SUFFIX") { return format!("{base}#{s}"); }
    base
}

fn load_connection(sh: &Shell) -> Option<(Url, Option<Secret>)> {
    let c = config::load(&sh.conn_path)?;
    let url = Url::parse(&c.url).ok()?;
    let token = sh.tokens.get(&account(&url), sh.store.as_ref());
    Some((url, token))
}

/// Probe off the UI thread; the caller (and the window) never waits on the network.
async fn run_probe(sh: Arc<Shell>) -> Status {
    let Some((url, token)) = load_connection(&sh) else {
        return Status { state: Health::Invalid, detail: "No deployment is configured.".into(), project: None };
    };
    let http = sh.http.clone();
    st!("probe credential: {}", if token.is_some() { "present" } else { "absent" });     // a boolean only — never the value
    tauri::async_runtime::spawn_blocking(move || probe::probe(http.as_ref(), &url, token.as_ref()))
        .await
        .unwrap_or_else(|_| Status { state: Health::Unavailable, detail: "The check was interrupted.".into(), project: None })
}

fn window(app: &AppHandle) -> Option<WebviewWindow> {
    app.get_webview_window(WINDOW)
}

fn reveal(app: &AppHandle) {
    if let Some(w) = window(app) {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

/// Show the app's own page, with a fixed, allowlisted state word (never data from the network).
fn show_app_page(app: &AppHandle, sh: &Shell, state: &str) {
    let (Some(w), Some(home)) = (window(app), sh.home.read().unwrap().clone()) else { return };
    let mut u = home;
    u.set_query(Some(&format!("state={state}")));
    let _ = w.navigate(u);
}

/// Show the deployment's own UI. `hash` (already validated by `deeplink::parse`) is optional.
fn show_deployment(app: &AppHandle, sh: &Shell, hash: Option<&str>) {
    let (Some(w), Some(base)) = (window(app), sh.allowed.read().unwrap().clone()) else { return };
    let target = format!("{}{}", config::base_with_slash(&base), hash.unwrap_or(""));
    if let Ok(u) = Url::parse(&target) {
        let on_it = w.url().map(|cur| config::same_origin(&cur, &base)).unwrap_or(false);
        match (on_it, hash) {
            // already showing the deployment: change only the hash — the web UI's router reacts, no reload
            (true, Some(h)) => {
                let _ = w.eval(&format!("window.location.hash = {};", serde_json::to_string(h).unwrap_or_else(|_| "\"\"".into())));
            }
            _ => {
                let _ = w.navigate(u);
            }
        }
    }
}

fn open_external(url: &str) {
    if !(url.starts_with("https://") || url.starts_with("http://")) {
        return;
    }
    let _ = if cfg!(target_os = "windows") {
        Command::new("explorer").arg(url).spawn()          // no shell: a URL with & or | cannot become a command
    } else if cfg!(target_os = "macos") {
        Command::new("open").arg(url).spawn()
    } else {
        Command::new("xdg-open").arg(url).spawn()
    };
}

fn handle_link(app: &AppHandle, sh: &Shell, link: &str) {
    match deeplink::parse(link) {
        Ok(hash) => {
            reveal(app);
            let ok = sh.last.lock().map(|s| s.state == Health::Ok).unwrap_or(false);
            if ok { show_deployment(app, sh, Some(&hash)); } else { show_app_page(app, sh, "connect"); }
        }
        Err(_) => {}                                       // a malformed or hostile link is simply ignored
    }
}

// ── commands (invoked only by the app's own page; remote pages get no IPC) ──────────────────────────────────────

#[derive(Serialize)]
struct ConnectionInfo {
    url: Option<String>,
    label: String,
    has_credential: bool,
    credential_store_available: bool,
    status: Status,
}

#[tauri::command]
fn connection_get(sh: Sh) -> ConnectionInfo {
    let conn = config::load(&sh.conn_path);
    let has_credential = conn.as_ref().and_then(|c| Url::parse(&c.url).ok())
        .map(|u| sh.tokens.get(&account(&u), sh.store.as_ref()).is_some()).unwrap_or(false);
    ConnectionInfo {
        url: conn.as_ref().map(|c| c.url.clone()),
        label: conn.map(|c| c.label).unwrap_or_default(),
        has_credential,
        credential_store_available: sh.store.available(),
        status: sh.last.lock().map(|s| s.clone()).unwrap_or_else(|_| unknown()),
    }
}

/// Validate, store the credential (keychain only), persist the address, probe, and — only if the runtime answers —
/// open it. The token is never returned, logged, or written anywhere but the OS credential store.
#[tauri::command]
async fn connection_save(app: AppHandle, sh: Sh<'_>, url: String, token: Option<String>, label: Option<String>) -> Result<Status, String> {
    let parsed = config::normalize_url(&url, &config::insecure_allowlist()).map_err(|e| e.to_string())?;
    let account = account(&parsed);
    if let Some(t) = token.as_deref().filter(|t| !t.trim().is_empty()) {
        let secret = Secret::new(t.trim()).map_err(|e| e.to_string())?;
        sh.store.set(&account, &secret).map_err(|e| e.to_string())?;     // refuses on a machine with no credential store
        sh.tokens.put(&account, Some(&secret));                           // we just wrote it — no need to read it back
    }
    let conn = Connection { url: parsed.to_string(), label: label.unwrap_or_default().chars().take(60).collect() };
    config::save(&sh.conn_path, &conn).map_err(|_| "Couldn't save the connection.".to_string())?;
    *sh.allowed.write().unwrap() = Some(parsed);
    sh.monitor.lock().unwrap().reset();
    let sh_arc: Arc<Shell> = (*sh).clone();
    let status = run_probe(sh_arc).await;
    *sh.last.lock().unwrap() = status.clone();
    if status.state == Health::Ok {
        sh.monitor.lock().unwrap().observe(&status);
        show_deployment(&app, &sh, None);
    }
    Ok(status)
}

#[tauri::command]
fn connection_forget(app: AppHandle, sh: Sh) -> Result<(), String> {
    if let Some(u) = config::load(&sh.conn_path).and_then(|c| Url::parse(&c.url).ok()) {
        sh.store.delete(&account(&u)).map_err(|e| e.to_string())?;
        sh.tokens.clear();
    }
    config::forget(&sh.conn_path);
    *sh.allowed.write().unwrap() = None;
    sh.monitor.lock().unwrap().reset();
    *sh.last.lock().unwrap() = unknown();
    show_app_page(&app, &sh, "connect");
    Ok(())
}

#[tauri::command]
async fn connection_probe(sh: Sh<'_>) -> Result<Status, String> {
    let sh_arc: Arc<Shell> = (*sh).clone();
    let s = run_probe(sh_arc).await;
    *sh.last.lock().unwrap() = s.clone();
    Ok(s)
}

#[tauri::command]
fn connection_open(app: AppHandle, sh: Sh) -> Result<(), String> {
    if sh.last.lock().map(|s| s.state == Health::Ok).unwrap_or(false) {
        show_deployment(&app, &sh, None);
        Ok(())
    } else {
        Err("The deployment isn't reachable yet.".into())
    }
}

#[tauri::command]
fn open_in_browser(sh: Sh) -> Result<(), String> {
    let base = sh.allowed.read().unwrap().clone().ok_or("No deployment is configured.")?;
    open_external(&config::base_with_slash(&base));
    Ok(())
}

// ── the local runtime (unchanged behaviour: the frozen sidecar, no Docker) ─────────────────────────────────────

fn sidecar() -> String {
    if let Ok(p) = std::env::var("RDO_SIDECAR") {
        return p;
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let name = if cfg!(windows) { "redevops-sidecar.exe" } else { "redevops-sidecar" };
            let cand = dir.join(name);
            if cand.exists() {
                return cand.to_string_lossy().into_owned();
            }
        }
    }
    "redevops-sidecar".to_string()
}

fn run_wait(cmd: &mut Command) -> Result<String, String> {
    match cmd.output() {
        Ok(out) => {
            let stdout = String::from_utf8_lossy(&out.stdout).to_string();
            let stderr = String::from_utf8_lossy(&out.stderr).to_string();
            if out.status.success() { Ok(stdout) } else { Err(format!("exit {}\n{}\n{}", out.status, stdout, stderr)) }
        }
        Err(e) => Err(format!("failed to launch: {e}")),
    }
}

/// Start the local control plane from the frozen sidecar and return its address (the page then saves it as the
/// connection like any other deployment — the local runtime is just a deployment on this machine).
#[tauri::command]
fn start_native(sh: Sh) -> Result<String, String> {
    let mut guard = sh.server.lock().map_err(|e| e.to_string())?;
    if guard.is_none() {
        let child = Command::new(sidecar()).arg("serve").stdout(Stdio::null()).stderr(Stdio::null()).spawn()
            .map_err(|e| format!("failed to start the control plane: {e}"))?;
        *guard = Some(child);
    }
    Ok(LOCAL_URL.to_string())
}

#[tauri::command]
fn stop_native(sh: Sh) -> Result<(), String> {
    if let Some(mut child) = sh.server.lock().map_err(|e| e.to_string())?.take() {
        let _ = child.kill();
    }
    Ok(())
}

#[tauri::command]
fn device_report() -> Result<String, String> {
    run_wait(Command::new(sidecar()).arg("bootstrap"))
}

fn bundle_dir() -> String {
    std::env::var("RDO_BUNDLE_DIR").unwrap_or_else(|_| "../sidekick-devops".to_string())
}

#[tauri::command]
fn docker_available() -> bool {
    Command::new("docker").arg("--version").output().map(|o| o.status.success()).unwrap_or(false)
}
#[tauri::command]
fn advanced_docker_up() -> Result<String, String> {
    run_wait(Command::new("docker").current_dir(bundle_dir()).args(["compose", "up", "-d"]))
}
#[tauri::command]
fn advanced_docker_down() -> Result<String, String> {
    run_wait(Command::new("docker").current_dir(bundle_dir()).args(["compose", "down"]))
}
#[tauri::command]
fn advanced_docker_purge() -> Result<String, String> {
    run_wait(Command::new("docker").current_dir(bundle_dir()).args(["compose", "down", "-v", "--remove-orphans"]))
}

// ── tray: a count and a way back, read from the server's own content-free summary ──────────────────────────────

fn update_tray(app: &AppHandle, count: Option<&probe::Counts>) {
    let Some(tray) = app.tray_by_id("main") else { return };
    let tip = match count {
        Some(c) if c.count > 0 => format!("ReDevOps — {}", c.summary),
        Some(_) => "ReDevOps — nothing needs you".to_string(),
        None => "ReDevOps".to_string(),
    };
    let _ = tray.set_tooltip(Some(tip));
    #[cfg(target_os = "macos")]
    let _ = tray.set_title(count.filter(|c| c.count > 0).map(|c| c.count.to_string()));
}

fn build_tray(app: &tauri::App) -> tauri::Result<()> {
    use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
    use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
    let open = MenuItem::with_id(app, "open", "Open Today", true, None::<&str>)?;
    let settings = MenuItem::with_id(app, "settings", "Connection settings…", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit ReDevOps", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &settings, &PredefinedMenuItem::separator(app)?, &quit])?;
    let mut b = TrayIconBuilder::with_id("main").tooltip("ReDevOps").menu(&menu).show_menu_on_left_click(false)
        .on_menu_event(|app, ev| {
            let sh = app.state::<Arc<Shell>>();
            match ev.id().as_ref() {
                "open" => {
                    reveal(app);
                    if sh.last.lock().map(|s| s.state == Health::Ok).unwrap_or(false) { show_deployment(app, &sh, None); } else { show_app_page(app, &sh, "connect"); }
                }
                "settings" => { reveal(app); show_app_page(app, &sh, "connect"); }
                "quit" => app.exit(0),
                _ => {}
            }
        })
        .on_tray_icon_event(|tray, ev| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = ev {
                reveal(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        b = b.icon(icon.clone());
    }
    b.build(app)?;
    Ok(())
}

// ── the monitor: probe, act on the state machine, refresh the tray ─────────────────────────────────────────────

async fn monitor_loop(app: AppHandle, sh: Arc<Shell>) {
    let mut startup = true;
    loop {
        if !startup {
            let d = sh.monitor.lock().unwrap().next_delay();
            tokio::time::sleep(d).await;
        }
        if config::load(&sh.conn_path).is_none() {
            startup = false;
            tokio::time::sleep(std::time::Duration::from_secs(5)).await;
            continue;
        }
        let status = run_probe(sh.clone()).await;
        *sh.last.lock().unwrap() = status.clone();
        let action = sh.monitor.lock().unwrap().observe(&status);
        st!("probe: {} -> {:?}{}", state_name(status.state), action, if startup { " (startup)" } else { "" });
        if startup {
            // First look after launch: restore the workspace, or say exactly why we can't — don't wait for three failures
            // to show a page, and make sure a later recovery brings the deployment back.
            if status.state == Health::Ok {
                show_deployment(&app, &sh, None);
            } else {
                sh.monitor.lock().unwrap().mark_away();
                show_app_page(&app, &sh, state_name(status.state));
            }
        } else {
            match action {
                Action::GoOffline => show_app_page(&app, &sh, "unavailable"),
                Action::GoOnline => show_deployment(&app, &sh, None),
                Action::NeedsAttention(s) => show_app_page(&app, &sh, state_name(s)),
                Action::None => {}
            }
        }
        startup = false;
        if status.state == Health::Ok {
            if let (Some((url, token)), Some(project)) = (load_connection(&sh), status.project.clone()) {
                let http = sh.http.clone();
                let c = tauri::async_runtime::spawn_blocking(move || probe::counts(http.as_ref(), &url, &project, token.as_ref())).await.ok().flatten();
                update_tray(&app, c.as_ref());
            }
        } else {
            update_tray(&app, None);
        }
    }
}

fn main() {
    let shell_for_nav: Arc<Mutex<Option<Arc<Shell>>>> = Arc::new(Mutex::new(None));
    let nav_slot = shell_for_nav.clone();

    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            // a second launch (or a deep link on Windows/Linux, which arrives as an argument) is routed to this one
            reveal(app);
            if let Some(link) = args.iter().find(|a| a.starts_with("redevops://")) {
                let sh = app.state::<Arc<Shell>>();
                handle_link(app, &sh, link);
            }
        }))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_window_state::Builder::default().build());

    // the one command that exists only in a selftest build reports what a shell-owned page rendered
    #[cfg(feature = "selftest")]
    let builder = builder.invoke_handler(tauri::generate_handler![
        connection_get, connection_save, connection_forget, connection_probe, connection_open, open_in_browser,
        start_native, stop_native, device_report, docker_available, advanced_docker_up, advanced_docker_down,
        advanced_docker_purge, selftest_report
    ]);
    #[cfg(not(feature = "selftest"))]
    let builder = builder.invoke_handler(tauri::generate_handler![
        connection_get, connection_save, connection_forget, connection_probe, connection_open, open_in_browser,
        start_native, stop_native, device_report, docker_available, advanced_docker_up, advanced_docker_down,
        advanced_docker_purge
    ]);

    builder
        .setup(move |app| {
            #[allow(unused_mut)]
            let mut dir = app.path().app_config_dir()?;
            #[cfg(feature = "selftest")]
            if let Ok(d) = std::env::var("RDO_CONFIG_DIR") { dir = PathBuf::from(d); }   // never touch the user's real config
            let sh = Arc::new(Shell {
                conn_path: dir.join("connection.json"),
                home: RwLock::new(None),
                allowed: RwLock::new(None),
                store: Arc::new(KeyringStore),
                tokens: TokenCache::new(),
                http: Arc::new(UreqHttp),
                monitor: Mutex::new(Monitor::new()),
                last: Mutex::new(unknown()),
                server: Mutex::new(None),
            });
            #[cfg(feature = "selftest")]
            if let Ok(u) = std::env::var("RDO_SELFTEST_URL") {          // seed a connection non-interactively
                if let Ok(parsed) = config::normalize_url(&u, &["127.0.0.1".to_string()]) {
                    if std::env::var("RDO_SELFTEST_FORGET").is_ok() {    // cleanup: the shell owns its keychain item, so it removes it
                        let r = sh.store.delete(&account(&parsed));
                        config::forget(&sh.conn_path);
                        st!("forgot the test credential: {}", r.is_ok());
                        std::process::exit(0);
                    }
                    if let Ok(tok) = std::env::var("RDO_SELFTEST_TOKEN") {
                        if let Ok(s) = Secret::new(tok.clone()) {
                            let wrote = sh.store.set(&account(&parsed), &s).is_ok();
                            // verify the keychain round-trip FROM INSIDE the app (a different program reading it would prompt)
                            let back = sh.store.get(&account(&parsed)).ok().flatten().map_or(false, |g| g.expose() == tok);
                            st!("keychain write ok: {wrote}; readback matches: {back}");
                        }
                    }
                    let _ = config::save(&sh.conn_path, &Connection { url: parsed.to_string(), label: "selftest".into() });
                }
            }
            if let Some(c) = config::load(&sh.conn_path) {
                *sh.allowed.write().unwrap() = Url::parse(&c.url).ok();
            }
            *nav_slot.lock().unwrap() = Some(sh.clone());
            app.manage(sh.clone());

            // The window is created here (not from config) so navigation can be restricted: only the app's own pages
            // and the ONE configured deployment may load; any other link opens in the system browser.
            let nav_sh = sh.clone();
            let builder = WebviewWindowBuilder::new(app, WINDOW, WebviewUrl::App("index.html".into()))
                .title("ReDevOps")
                .inner_size(1280.0, 820.0)
                .min_inner_size(820.0, 560.0)
                .on_navigation(move |url| {
                    let allowed = nav_sh.allowed.read().unwrap().clone();
                    let home = nav_sh.home.read().unwrap().clone();
                    let app_page = matches!(url.scheme(), "tauri" | "about") || home.as_ref().map_or(false, |h| config::same_origin(url, h));
                    let deployment = allowed.as_ref().map_or(false, |a| config::same_origin(url, a));
                    if app_page || deployment { return true; }
                    if url.scheme() == "http" || url.scheme() == "https" { open_external(url.as_str()); }
                    false
                });
            #[cfg(feature = "selftest")]
            let builder = {
                let (dep_script, app_script, port) = (
                    std::env::var("RDO_SELFTEST_SCRIPT").ok().and_then(|p| std::fs::read_to_string(p).ok()),
                    std::env::var("RDO_SELFTEST_APP_SCRIPT").ok().and_then(|p| std::fs::read_to_string(p).ok()),
                    std::env::var("RDO_SELFTEST_REPORT").unwrap_or_default(),
                );
                let page_sh = sh.clone();
                builder.on_page_load(move |wv, payload| {
                    if !matches!(payload.event(), tauri::webview::PageLoadEvent::Finished) { return; }
                    let url = payload.url();
                    let on_dep = page_sh.allowed.read().unwrap().as_ref().map_or(false, |a| config::same_origin(url, a));
                    st!("page loaded: {} ({})", url.origin().ascii_serialization(), if on_dep { "deployment" } else { "app" });
                    let script = if on_dep { dep_script.as_ref() } else { app_script.as_ref() };
                    if let Some(s) = script { let _ = wv.eval(&selftest_wrap(s, &port, if on_dep { "deployment" } else { "app" }, !on_dep)); }
                })
            };
            let win = builder.build()?;
            *sh.home.write().unwrap() = win.url().ok().map(|mut u| { u.set_query(None); u.set_fragment(None); u });

            // Closing the window keeps the app alive in the tray on macOS/Windows. On Linux many desktops have no tray,
            // so closing quits — the user can never be left with an invisible app.
            #[cfg(any(target_os = "macos", target_os = "windows"))]
            {
                let w2 = win.clone();
                win.on_window_event(move |ev| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = ev {
                        api.prevent_close();
                        let _ = w2.hide();
                    }
                });
            }

            build_tray(app)?;

            let handle = app.handle().clone();
            app.deep_link().on_open_url({
                let h = handle.clone();
                move |event| {
                    let sh = h.state::<Arc<Shell>>();
                    for u in event.urls() { handle_link(&h, &sh, u.as_str()); }
                }
            });
            #[cfg(any(windows, target_os = "linux"))]
            let _ = app.deep_link().register_all();

            tauri::async_runtime::spawn(monitor_loop(handle, sh));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the ReDevOps desktop shell")
        .run(move |_app, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(sh) = shell_for_nav.lock().unwrap().as_ref() {
                    if let Some(mut child) = sh.server.lock().unwrap().take() {
                        let _ = child.kill();                      // don't leave the local runtime orphaned
                    }
                }
            }
        });
}
