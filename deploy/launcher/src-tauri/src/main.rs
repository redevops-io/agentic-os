// ReDevOps Launcher — native shell (Tauri). Holds NO business logic.
//
// NATIVE-FIRST (decision 2026-09-13): the default one-click runs the bundled, frozen Python
// sidecar directly — the whole control plane (missions, connectors/OAuth, proactive intelligence,
// permissions) with NO Docker and NO system Python/Git. Docker is an ADVANCED, opt-in capability
// (the observability combo + rootless membrane), never required to run.
//
// SCAFFOLD — build/test on a machine with the Rust + Tauri toolchain and a GUI (see ../README.md).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod about;
mod capability;
mod guard;
mod launch;
mod lifecycle;
mod opener;

use launch::Running;
use lifecycle::StopOutcome;
use std::process::Command;
use std::sync::{Mutex, MutexGuard};
use tauri::{Manager, State};

/// Tracks the running native control-plane process TREE (bootloader + Python child) plus this launch's
/// secret, so we can open it with a fresh launch code and stop all of it on Stop and on quit. Only a
/// process this launcher spawned and authenticated is ever tracked.
struct Server(Mutex<Option<Running>>);

impl Server {
    /// Poison-tolerant lock (a panicking command must not stop us from cleaning up on exit).
    fn lock(&self) -> MutexGuard<'_, Option<Running>> {
        self.0.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Take the tree out of the mutex and stop it (blocking up to the grace period).
    fn stop(&self) -> Option<StopOutcome> {
        let taken = self.lock().take();
        taken.map(|running| running.stop())
    }
}

/// The frozen sidecar binary. Tauri ships it beside the launcher (externalBin); overridable
/// via RDO_SIDECAR for dev.
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
    lifecycle::hide_console(cmd);
    match cmd.output() {
        Ok(out) => {
            let stdout = String::from_utf8_lossy(&out.stdout).to_string();
            let stderr = String::from_utf8_lossy(&out.stderr).to_string();
            if out.status.success() {
                Ok(stdout)
            } else {
                // bootstrap uses a non-zero exit to signal a BLOCKED device — surface both streams.
                Err(format!("exit {}\n{}\n{}", out.status, stdout, stderr))
            }
        }
        Err(e) => Err(format!("failed to launch: {e}")),
    }
}

// ── Native default (no Docker) ────────────────────────────────────────────────────────

/// One-click: start the control plane natively from the frozen sidecar, authenticate it, and return its
/// plain URL (no launch code). Nothing is opened here; `open_ui` mints a fresh launch code per open.
#[tauri::command]
async fn start_native(server: State<'_, Server>) -> Result<String, String> {
    let mut guard = server.lock();
    if let Some(running) = guard.as_mut() {
        if running.child.is_running() {
            return Ok(capability::plain_url(running.port, &running.base_path)); // OUR authenticated child
        }
        // It exited on its own (crash): drop the dead handle (wipes its secret) and start fresh.
        *guard = None;
    }

    // The capability handshake (docs/LOCAL_API_SECURITY.md section 4) replaces the old
    // fixed-port "is something listening" check: the sidecar binds an OS-chosen port, reports it with a one-run secret,
    // on a pipe only this launcher holds, and must pass an HMAC proof before anything trusts or opens
    // it. A stale or foreign listener cannot answer, and cannot make us adopt it.
    // The stdin pipe from ManagedChild::spawn stays open as the exit-on-EOF tether.
    let mut cmd = Command::new(sidecar());
    cmd.args(["serve", "--exit-on-stdin-eof"]);
    let running = launch::start_sidecar(&mut cmd, launch::READY_TIMEOUT).map_err(|e| e.to_string())?;
    let url = capability::plain_url(running.port, &running.base_path);
    *guard = Some(running);
    Ok(url)
}

/// Stop the native control plane: the whole process tree, gracefully first (also called on quit).
#[tauri::command]
async fn stop_native(server: State<'_, Server>) -> Result<String, String> {
    Ok(match server.stop() {
        Some(StopOutcome::Graceful) => "Stopped.".to_string(),
        Some(StopOutcome::Forced) => "Stopped (it did not exit in time and was terminated).".to_string(),
        None => "The control plane was not started by this app.".to_string(),
    })
}

/// Open the control plane in the user's browser with a FRESH single-use launch code. The launcher
/// window stays the control panel; the sidecar origin is never loaded into this webview. Every click
/// re-proves the sidecar (handshake) and mints a new code (they live 60 s and work once). The code is
/// never put on a command line: the OS opener is given only the path of a private (0600, in a 0700
/// per-user directory) redirect page that carries the URL with the code in its fragment, and that file
/// is deleted after the code's lifetime and on exit (security review SR-01).
#[tauri::command]
async fn open_ui(app: tauri::AppHandle, server: State<'_, Server>) -> Result<(), String> {
    let (port, base_path, secret) = {
        let mut guard = server.lock();
        let alive = guard.as_mut().map(|r| r.child.is_running()).unwrap_or(false);
        match guard.as_ref().filter(|_| alive) {
            Some(r) => r.open_params(),
            None => return Err("The control plane is not running. Press \"Start & open\" first.".to_string()),
        }
    };
    let code = match capability::fresh_launch_code(port, &secret) {
        Ok(c) => c,
        Err(e @ capability::OpenError::Untrusted(_)) => {
            // The handshake proof failed: something that does not hold this launch's secret answers on
            // the port. Final for that process: stop it and do not open.
            server.stop();
            return Err(format!(
                "The control plane could not prove its identity ({e}). It was stopped and the browser was not opened."
            ));
        }
        // Transient or unexpected: never stop the user's sidecar for it (docs/LOCAL_API_SECURITY.md 4.7).
        Err(capability::OpenError::Busy) => {
            return Err("The local service is busy. Nothing was opened; try again in a moment.".to_string())
        }
        Err(e) => return Err(format!("Could not get a launch code: {e}. Try again, or Stop and Start.")),
    };
    let dir = app
        .path()
        .app_cache_dir()
        .map_err(|e| format!("no per-user cache directory: {e}"))?
        .join("launch");
    let file = opener::write_redirect(&dir, &capability::launch_url(port, &base_path, &code))
        .map_err(|e| format!("could not prepare the private launch page: {}", e.kind()))?;
    opener::remove_after(file.clone(), opener::FILE_LIFETIME);
    if let Err(e) = open_in_browser(&file) {
        opener::remove(&file);
        return Err(e);
    }
    Ok(())
}

/// Warn (never act) about a leftover v0.3.2 sidecar that still serves an unauthenticated API on the old
/// fixed port. Best effort, bounded to 300 ms; None when nothing is found.
#[tauri::command]
async fn legacy_service_warning() -> Option<String> {
    launch::legacy_listener_warning(launch::LEGACY_PORT)
}

/// Hand the redirect page's PATH to the OS opener. The argv holds a file path only: no URL, no code, no
/// secret (see opener.rs).
fn open_in_browser(file: &std::path::Path) -> Result<(), String> {
    let (prog, args) = opener::opener_command(file);
    let mut cmd = Command::new(prog);
    cmd.args(args);
    lifecycle::hide_console(&mut cmd);
    cmd.spawn().map(|_| ()).map_err(|e| format!("could not open the browser: {}", e.kind()))
}

/// "Check this device" — device readiness via the FROZEN brain (posture + default LLM). No system Python.
#[tauri::command]
fn device_report() -> Result<String, String> {
    run_wait(Command::new(sidecar()).arg("bootstrap"))
}

// ── Advanced: Docker capability (opt-in; NOT required to run) ───────────────────────────

fn bundle_dir() -> String {
    std::env::var("RDO_BUNDLE_DIR").unwrap_or_else(|_| "../sidekick-devops".to_string())
}

/// Whether Docker is present — the UI shows the advanced "local containers" option only if so.
#[tauri::command]
fn docker_available() -> bool {
    Command::new("docker")
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

/// Advanced: enable the container-backed tiers (observability + rootless membrane).
#[tauri::command]
fn advanced_docker_up() -> Result<String, String> {
    run_wait(Command::new("docker").current_dir(bundle_dir()).args(["compose", "up", "-d"]))
}

#[tauri::command]
fn advanced_docker_down() -> Result<String, String> {
    run_wait(Command::new("docker").current_dir(bundle_dir()).args(["compose", "down"]))
}

/// Advanced: full wipe of the container tier (KEEPS native data). Destructive; the UI confirms.
#[tauri::command]
fn advanced_docker_purge() -> Result<String, String> {
    run_wait(
        Command::new("docker")
            .current_dir(bundle_dir())
            .args(["compose", "down", "-v", "--remove-orphans"]),
    )
}

/// The compiled Tauri context. A function (not inline in `main`) so the tests can inspect exactly
/// what the build embedded: the configuration and the injected global API script.
fn context() -> tauri::Context {
    tauri::generate_context!()
}

fn main() {
    let app = tauri::Builder::default()
        .manage(Server(Mutex::new(None)))
        .plugin(guard::plugin())
        .invoke_handler(tauri::generate_handler![
            start_native,
            stop_native,
            open_ui,
            legacy_service_warning,
            device_report,
            docker_available,
            advanced_docker_up,
            advanced_docker_down,
            advanced_docker_purge,
            about::about_info,
            about::about_text
        ])
        .build(context())
        .expect("error while building the ReDevOps launcher");

    app.run(|app_handle, event| {
        // Quit (Cmd+Q, last window closed, tray/menu quit): never leave the control plane behind.
        if let tauri::RunEvent::Exit = event {
            app_handle.state::<Server>().stop();
            opener::cleanup_all();
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha256};

    /// R0-prime defect c: the UI calls `window.__TAURI__.core.invoke`, which only exists when the
    /// global API script is compiled in (`app.withGlobalTauri`). v0.3.2 shipped without it (0
    /// `__TAURI_IIFE__` markers in the binary), so every button was dead.
    #[test]
    fn the_global_tauri_api_is_compiled_in() {
        let ctx = context();
        assert!(ctx.config().app.with_global_tauri, "app.withGlobalTauri must be true");
        let dump = format!("{ctx:?}");
        assert!(
            dump.contains("plugin_global_api_scripts: Some("),
            "no global API script embedded in the context"
        );
        assert!(dump.contains("__TAURI_IIFE__"), "the embedded script is not the global API bundle");
    }

    /// The launcher webview CSP (RC3). The exact string is pinned here, in tauri.conf.json (compared by
    /// tests/test_launcher_tauri_config.py) and in the headless-browser test, which serves the page with it.
    const EXPECTED_CSP: &str = "default-src 'none'; script-src 'self'; style-src 'self'; img-src data:; connect-src ipc: http://ipc.localhost; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'";

    fn text_of(assets: &dyn tauri::Assets<tauri::Wry>, name: &str) -> String {
        let key: tauri::utils::assets::AssetKey = name.into();
        String::from_utf8(assets.get(&key).unwrap_or_else(|| panic!("{name} is not embedded")).into_owned()).unwrap()
    }

    #[test]
    fn the_launcher_csp_is_compiled_in_exactly_as_configured() {
        let ctx = context();
        match &ctx.config().app.security.csp {
            Some(tauri::utils::config::Csp::Policy(p)) => assert_eq!(p, EXPECTED_CSP),
            other => panic!("app.security.csp must be the policy string, got {other:?}"),
        }
        // dev builds must not run under a different (looser) policy
        assert!(ctx.config().app.security.dev_csp.is_none(), "no separate devCsp");
        for forbidden in ["'unsafe-inline'", "'unsafe-eval'", "'wasm-unsafe-eval'", "*", "blob:", "filesystem:", "http:", "https:", "ws:", "wss:", "'strict-dynamic'"] {
            assert!(!EXPECTED_CSP.split_whitespace().any(|t| t.trim_end_matches(';') == forbidden), "{forbidden}");
        }
        for must in ["default-src 'none'", "script-src 'self'", "style-src 'self'", "base-uri 'none'", "form-action 'none'", "frame-ancestors 'none'"] {
            assert!(EXPECTED_CSP.contains(must), "{must}");
        }
    }

    #[test]
    fn the_other_security_options_are_left_at_their_strict_defaults() {
        let ctx = context();
        let sec = &ctx.config().app.security;
        assert!(matches!(sec.dangerous_disable_asset_csp_modification, tauri::utils::config::DisabledCspModificationKind::Flag(false)));
        assert!(!sec.asset_protocol.enable, "the asset protocol (file access from the page) stays off");
        assert!(matches!(sec.pattern, tauri::utils::config::PatternKind::Brownfield));
        assert!(sec.headers.as_ref().is_some_and(|h| h.x_content_type_options.is_some()));
    }

    /// The page's scripts are external files served from the app origin (script-src 'self'); there is no
    /// inline script, inline style, inline handler or script URL for Tauri to hash or for a CSP to need.
    #[test]
    fn the_embedded_page_has_only_external_scripts_and_styles_and_nothing_else_ships() {
        let ctx = context();
        let mut names: Vec<String> = ctx.assets.iter().map(|(k, _)| k.to_string()).collect();
        names.sort();
        assert_eq!(names, ["/app.css", "/app.js", "/index.html"], "the webview may only be able to load these files");

        let html = text_of(&*ctx.assets, "/index.html");
        assert!(html.contains("<script src=\"app.js\" defer=\"\"></script>"), "{html}");
        assert!(html.contains("<link rel=\"stylesheet\" href=\"app.css\">"), "{html}");
        assert_eq!(html.matches("<script").count(), 1, "exactly one script element");
        assert!(!html.contains("<style"), "no inline <style>");
        assert!(!html.contains(" style="), "no style attributes");
        assert!(!html.contains("javascript:") && !html.contains("<iframe") && !html.contains("<form") && !html.contains("<object") && !html.contains("<embed"));
        assert!(!html.contains("<a "), "the page has no links: nothing that could navigate the webview");
        let handlers = regex_free_on_attributes(&html);
        assert!(handlers.is_empty(), "inline event handlers: {handlers:?}");
        // Tauri adds a 'sha256-...' source to script-src for every .js file it embeds (the only hash source it
        // can add here, since there is no inline script body); it must be exactly the hash of app.js.
        let hashes: Vec<String> = ctx.assets.csp_hashes(&"/index.html".into()).map(|h| format!("{h:?}")).collect();
        let source = format!("'sha256-{}'", b64(&Sha256::digest(text_of(&*ctx.assets, "/app.js").as_bytes())));
        let want = format!("{:?}", tauri::utils::assets::CspHash::Script(&source));
        assert_eq!(hashes, [want], "only the hash of app.js is expected on the document");

        let js = text_of(&*ctx.assets, "/app.js");
        for banned in ["eval(", "new Function", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "javascript:", "setTimeout(\"", "setInterval(\"", "http://", "https://", "fetch(", "XMLHttpRequest", "WebSocket", "window.open", "location.", "localStorage", "sessionStorage", "import("] {
            assert!(!js.contains(banned), "app.js must not contain {banned}");
        }
        let css = text_of(&*ctx.assets, "/app.css");
        assert!(!css.contains("@import") && !css.contains("url("), "app.css must not load anything");
    }

    fn b64(bytes: &[u8]) -> String {
        const A: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        let mut s = String::new();
        for c in bytes.chunks(3) {
            let n = (u32::from(c[0]) << 16) | (u32::from(*c.get(1).unwrap_or(&0)) << 8) | u32::from(*c.get(2).unwrap_or(&0));
            for i in 0..4 {
                if i <= c.len() { s.push(A[((n >> (18 - 6 * i)) & 63) as usize] as char) } else { s.push('=') }
            }
        }
        s
    }

    /// `on<event>=` attributes in tag text (a CSP would block them; the page must not rely on any).
    fn regex_free_on_attributes(html: &str) -> Vec<String> {
        html.split(|c: char| c.is_whitespace() || c == '<')
            .filter(|w| w.len() > 3 && w.starts_with("on") && w.contains('=') && w[2..].chars().take_while(|c| *c != '=').all(|c| c.is_ascii_lowercase()))
            .map(str::to_string)
            .collect()
    }

    // ---- the compiled default window icon (RC3, D-LIC-11: temporary neutral artwork) -----------------------

    /// Minimal zlib/DEFLATE decoder (stored, fixed and dynamic blocks; after RFC 1951 / zlib's puff.c), so the
    /// test can decode `icons/32x32.png` without adding a dependency to the launcher.
    fn inflate(zlib: &[u8]) -> Vec<u8> {
        struct Bits<'a> { d: &'a [u8], pos: usize, bit: u32, cnt: u32 }
        impl Bits<'_> {
            fn need(&mut self, n: u32) -> u32 {
                while self.cnt < n {
                    self.bit |= (self.d[self.pos] as u32) << self.cnt;
                    self.pos += 1;
                    self.cnt += 8;
                }
                let v = self.bit & ((1u32 << n) - 1);
                self.bit >>= n;
                self.cnt -= n;
                v
            }
        }
        // canonical Huffman table: (count per length, symbols ordered by code)
        fn build(lengths: &[u8]) -> ([u16; 16], Vec<u16>) {
            let mut count = [0u16; 16];
            for &l in lengths { count[l as usize] += 1; }
            let mut offs = [0u16; 16];
            for i in 1..15 { offs[i + 1] = offs[i] + count[i]; }
            let mut syms = vec![0u16; lengths.len()];
            for (sym, &l) in lengths.iter().enumerate() {
                if l != 0 { syms[offs[l as usize] as usize] = sym as u16; offs[l as usize] += 1; }
            }
            (count, syms)
        }
        fn decode(b: &mut Bits, t: &([u16; 16], Vec<u16>)) -> usize {
            let (mut code, mut first, mut index) = (0i32, 0i32, 0i32);
            for len in 1..16 {
                code |= b.need(1) as i32;
                let c = t.0[len] as i32;
                if code - c < first { return t.1[(index + (code - first)) as usize] as usize; }
                index += c;
                first += c;
                first <<= 1;
                code <<= 1;
            }
            panic!("bad huffman code");
        }
        const LBASE: [u16; 29] = [3,4,5,6,7,8,9,10,11,13,15,17,19,23,27,31,35,43,51,59,67,83,99,115,131,163,195,227,258];
        const LEXT: [u8; 29] = [0,0,0,0,0,0,0,0,1,1,1,1,2,2,2,2,3,3,3,3,4,4,4,4,5,5,5,5,0];
        const DBASE: [u16; 30] = [1,2,3,4,5,7,9,13,17,25,33,49,65,97,129,193,257,385,513,769,1025,1537,2049,3073,4097,6145,8193,12289,16385,24577];
        const DEXT: [u8; 30] = [0,0,0,0,1,1,2,2,3,3,4,4,5,5,6,6,7,7,8,8,9,9,10,10,11,11,12,12,13,13];
        assert_eq!(zlib[0] & 0x0f, 8, "zlib header: deflate");
        let mut b = Bits { d: zlib, pos: 2, bit: 0, cnt: 0 };
        let mut out: Vec<u8> = Vec::new();
        loop {
            let last = b.need(1);
            match b.need(2) {
                0 => {
                    b.bit = 0;
                    b.cnt = 0;
                    let len = u16::from_le_bytes([zlib[b.pos], zlib[b.pos + 1]]) as usize;
                    out.extend_from_slice(&zlib[b.pos + 4..b.pos + 4 + len]);
                    b.pos += 4 + len;
                }
                kind @ (1 | 2) => {
                    let (lt, dt) = if kind == 1 {
                        let mut l = [8u8; 288];
                        for x in l.iter_mut().take(256).skip(144) { *x = 9; }
                        for x in l.iter_mut().take(280).skip(256) { *x = 7; }
                        (build(&l), build(&[5u8; 30]))
                    } else {
                        let (nlen, ndist, ncode) = (b.need(5) as usize + 257, b.need(5) as usize + 1, b.need(4) as usize + 4);
                        const ORDER: [usize; 19] = [16,17,18,0,8,7,9,6,10,5,11,4,12,3,13,2,14,1,15];
                        let mut cl = [0u8; 19];
                        for &o in ORDER.iter().take(ncode) { cl[o] = b.need(3) as u8; }
                        let ct = build(&cl);
                        let mut lens = vec![0u8; nlen + ndist];
                        let mut i = 0;
                        while i < nlen + ndist {
                            let sym = decode(&mut b, &ct);
                            if sym < 16 { lens[i] = sym as u8; i += 1; continue; }
                            let (prev, rep) = match sym {
                                16 => (lens[i - 1], 3 + b.need(2)),
                                17 => (0, 3 + b.need(3)),
                                _ => (0, 11 + b.need(7)),
                            };
                            for _ in 0..rep { lens[i] = prev; i += 1; }
                        }
                        (build(&lens[..nlen]), build(&lens[nlen..]))
                    };
                    loop {
                        let sym = decode(&mut b, &lt);
                        if sym < 256 { out.push(sym as u8); continue; }
                        if sym == 256 { break; }
                        let sym = sym - 257;
                        let len = LBASE[sym] as usize + b.need(LEXT[sym] as u32) as usize;
                        let ds = decode(&mut b, &dt);
                        let dist = DBASE[ds] as usize + b.need(DEXT[ds] as u32) as usize;
                        for _ in 0..len { out.push(out[out.len() - dist]); }
                    }
                }
                _ => panic!("bad deflate block type"),
            }
            if last == 1 { break; }
        }
        out
    }

    /// Decode an 8-bit RGBA, non-interlaced PNG into (width, height, rgba) (all five filter types).
    fn decode_rgba_png(png: &[u8]) -> (u32, u32, Vec<u8>) {
        assert_eq!(&png[..8], b"\x89PNG\r\n\x1a\n");
        let (mut pos, mut idat, mut hdr) = (8usize, Vec::new(), None);
        while pos < png.len() {
            let len = u32::from_be_bytes(png[pos..pos + 4].try_into().unwrap()) as usize;
            let tag = &png[pos + 4..pos + 8];
            let data = &png[pos + 8..pos + 8 + len];
            match tag {
                b"IHDR" => hdr = Some((u32::from_be_bytes(data[0..4].try_into().unwrap()), u32::from_be_bytes(data[4..8].try_into().unwrap()), data[8], data[9], data[12])),
                b"IDAT" => idat.extend_from_slice(data),
                _ => {}
            }
            pos += 12 + len;
        }
        let (w, h, depth, colour, interlace) = hdr.expect("IHDR");
        assert_eq!((depth, colour, interlace), (8, 6, 0), "the icon must be 8-bit RGBA, not interlaced");
        let raw = inflate(&idat);
        let stride = w as usize * 4;
        assert_eq!(raw.len(), (stride + 1) * h as usize);
        let mut px = vec![0u8; stride * h as usize];
        for y in 0..h as usize {
            let f = raw[y * (stride + 1)];
            for x in 0..stride {
                let cur = raw[y * (stride + 1) + 1 + x];
                let a = if x >= 4 { px[y * stride + x - 4] } else { 0 };
                let up = if y > 0 { px[(y - 1) * stride + x] } else { 0 };
                let c = if x >= 4 && y > 0 { px[(y - 1) * stride + x - 4] } else { 0 };
                px[y * stride + x] = cur.wrapping_add(match f {
                    0 => 0,
                    1 => a,
                    2 => up,
                    3 => ((a as u16 + up as u16) / 2) as u8,
                    4 => {
                        let p = a as i32 + up as i32 - c as i32;
                        let (pa, pb, pc) = ((p - a as i32).abs(), (p - up as i32).abs(), (p - c as i32).abs());
                        if pa <= pb && pa <= pc { a } else if pb <= pc { up } else { c }
                    }
                    _ => panic!("bad PNG filter"),
                });
            }
        }
        (w, h, px)
    }

    /// RC3 / D-LIC-11: the window icon compiled into the binary (tauri-codegen embeds `bundle.icon`'s first
    /// `.png` as the default window icon on macOS and Linux) is the temporary neutral 32x32 artwork, decoded
    /// RGBA for RGBA. A fingerprint of the previous artwork's pixels (taken from its decoded 32x32 RGBA, kept
    /// as a hash only) must not match.
    #[test]
    fn the_compiled_default_window_icon_is_the_temporary_neutral_32x32() {
        const OLD_RGBA_SHA256: &str = "c5070069b55e5bf69732713095d04e0f9e8079f13d0ffeec3f4d43acccd8b82c";
        let ctx = context();
        let icon = ctx.default_window_icon().expect("a default window icon is compiled in");
        let file = std::fs::read(concat!(env!("CARGO_MANIFEST_DIR"), "/icons/32x32.png")).unwrap();
        let (w, h, rgba) = decode_rgba_png(&file);
        assert_eq!((w, h), (32, 32));
        assert_eq!((icon.width(), icon.height()), (32, 32), "the compiled icon is the 32x32 asset");
        assert_eq!(icon.rgba(), &rgba[..], "compiled RGBA differs from icons/32x32.png");
        let digest: String = Sha256::digest(icon.rgba()).iter().map(|b| format!("{b:02x}")).collect();
        assert_ne!(digest, OLD_RGBA_SHA256, "the compiled icon is still the previous artwork");
        // plain geometry: the corners are transparent, the centre is opaque
        assert_eq!(icon.rgba()[3], 0, "top-left pixel is transparent (rounded corner)");
        let centre = (16 * 32 + 16) * 4;
        assert_eq!(icon.rgba()[centre + 3], 255);
        // development builds on macOS also embed the first `.icns` (the Dock icon of an unbundled run): its size is the new file's
        let icns_len = std::fs::metadata(concat!(env!("CARGO_MANIFEST_DIR"), "/icons/icon.icns")).unwrap().len();
        let dump = format!("{ctx:?}");
        if cfg!(target_os = "macos") && dump.contains("app_icon: Some(") {
            assert!(dump.contains(&format!("app_icon: Some([u8; {icns_len}])")), "the embedded .icns is not icons/icon.icns");
        }
    }

    /// Windows title and page title carry the product name of R0-prime (packaging/identity.json); the old
    /// "ReDevOps" heading is gone and the page shows no logo.
    #[test]
    fn the_window_and_the_page_are_named_agentic_apps_r0_prime_and_carry_no_logo() {
        let ctx = context();
        let w = &ctx.config().app.windows[0];
        assert_eq!(w.title, "Agentic Apps R0-prime");
        assert_eq!(w.label, "main");
        let html = text_of(&*ctx.assets, "/index.html");
        assert!(html.contains("<title>Agentic Apps R0-prime</title>"));
        assert!(html.contains("<h1>Agentic Apps R0-prime</h1>"));
        assert!(!html.contains("ReDevOps"), "the old heading must be gone");
        assert!(html.contains("Private candidate, release line r0-prime."));
        assert!(!html.contains("<img") && !html.contains("<svg") && !html.contains("icon.") && !html.contains(".png") && !html.contains(".icns"));
        assert!(html.contains("<link rel=\"icon\" href=\"data:,\">"), "an empty data: favicon so no icon request is made");
    }
}
