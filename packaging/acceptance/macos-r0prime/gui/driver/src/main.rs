// Headless driver for the launcher's real start / open / stop code paths (acceptance harness, not shipped).
//
// The four launcher modules are INCLUDED FROM THE REPOSITORY SOURCE (same files the shipped launcher was
// built from; no copy, no edit). Only the thin glue of `main.rs` (`start_native`, `open_ui`, `stop_native`,
// `legacy_service_warning`, `device_report`, the exit hook) is re-written here without Tauri, line for line.
// So: launcher LOGIC = same source, built by the same toolchain; the shipped launcher BINARY and its
// webview/click path are NOT exercised by this driver.
//
// Protocol: one command per stdin line, one JSON object per stdout line. Secrets and launch codes are
// never printed, except by `mintcode`, which exists for the negative tests (single use, disposable env).
#![allow(dead_code, unused_imports)]

#[path = "../../../../../../deploy/launcher/src-tauri/src/capability.rs"]
mod capability;
#[path = "../../../../../../deploy/launcher/src-tauri/src/launch.rs"]
mod launch;
#[path = "../../../../../../deploy/launcher/src-tauri/src/lifecycle.rs"]
mod lifecycle;
#[path = "../../../../../../deploy/launcher/src-tauri/src/opener.rs"]
mod opener;

use launch::Running;
use lifecycle::{ManagedChild, StopOutcome, GRACE};
use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

extern "C" {
    fn proc_listchildpids(ppid: i32, buffer: *mut std::ffi::c_void, buffersize: i32) -> i32;
}

fn child_count(pid: u32) -> usize {
    let mut buf = [0i32; 64];
    let n = unsafe { proc_listchildpids(pid as i32, buf.as_mut_ptr() as *mut _, (buf.len() * 4) as i32) };
    if n <= 0 { 0 } else { n as usize / 4 }
}

fn json_str(s: &str) -> String {
    let mut o = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            '\n' => o.push_str("\\n"),
            '\r' => o.push_str("\\r"),
            c if (c as u32) < 0x20 => o.push_str(&format!("\\u{:04x}", c as u32)),
            c => o.push(c),
        }
    }
    o.push('"');
    o
}

fn emit(line: String) {
    println!("{line}");
    let _ = std::io::stdout().flush();
}

/// Same lookup as main.rs `sidecar()` but the "beside the launcher" case is the copied .app.
fn sidecar() -> String {
    std::env::var("RDO_SIDECAR").expect("RDO_SIDECAR must name the sidecar inside the copied .app")
}

/// Same argv/env as main.rs `start_native`.
fn sidecar_cmd() -> Command {
    let mut cmd = Command::new(sidecar());
    cmd.args(["serve", "--exit-on-stdin-eof"]);
    cmd
}

fn cache_launch_dir() -> PathBuf {
    // Tauri's app_cache_dir() on macOS = $HOME/Library/Caches/<identifier> (dirs::cache_dir()).
    let home = std::env::var("HOME").expect("HOME");
    PathBuf::from(home).join("Library/Caches/io.redevops.agentic-apps/launch")
}

fn ms(d: Duration) -> f64 {
    (d.as_secs_f64() * 1000.0 * 10.0).round() / 10.0
}

struct State {
    running: Option<Running>,
    flood: Option<(Arc<AtomicBool>, Arc<AtomicU64>, Arc<AtomicU64>, Vec<std::thread::JoinHandle<()>>)>,
}

fn start_flood(port: u16, threads: usize, ok: Arc<AtomicU64>, err: Arc<AtomicU64>, stop: Arc<AtomicBool>) -> Vec<std::thread::JoinHandle<()>> {
    // Handshake requests with fresh nonces from `threads` threads; the proofs are not even checked.
    (0..threads)
        .map(|_| {
            let (ok, err, stop) = (ok.clone(), err.clone(), stop.clone());
            std::thread::spawn(move || {
                while !stop.load(Ordering::Relaxed) {
                    let nonce = capability::fresh_nonce().unwrap_or_else(|_| "AAAAAAAAAAAAAAAAAAAAAA".into());
                    let body = format!("{{\"client_nonce\":\"{nonce}\"}}");
                    match capability::http_request(port, "POST", "/api/local/handshake", &[], Some(&body), Duration::from_secs(5)) {
                        Ok(r) if r.status == 200 => { ok.fetch_add(1, Ordering::Relaxed); }
                        _ => { err.fetch_add(1, Ordering::Relaxed); }
                    }
                }
            })
        })
        .collect()
}

/// Step-wise start for timing: the same calls `launch::start_sidecar` makes, with timestamps, plus a
/// sampler of the bootloader (first `_MEI*` dir in the sidecar's TMPDIR, first child process = the
/// extraction finished and the Python process was spawned).
fn timed_start(tmpdir: Option<&str>, flood_threads: usize) -> String {
    let mut cmd = sidecar_cmd();
    cmd.env("PROJECTS_PORT", "0")
        .env("PROJECTS_HOST", "127.0.0.1")
        .env("PROJECTS_BASE_PATH", "")
        .env("AGENTIC_OS_HANDOFF", "stdout")
        .env_remove("AGENTIC_OS_LOCAL_CAPABILITY")
        .env_remove("PROJECTS_ALLOWED_HOSTS")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let sidecar_tmp: PathBuf = match tmpdir {
        Some(t) => { cmd.env("TMPDIR", t); PathBuf::from(t) }
        None => PathBuf::from(std::env::var("TMPDIR").unwrap_or_else(|_| "/tmp".into())),
    };
    let t0 = Instant::now();
    let mut child = match ManagedChild::spawn(&mut cmd) {
        Ok(c) => c,
        Err(e) => return format!("{{\"ok\":false,\"stage\":\"spawn\",\"error\":{}}}", json_str(&e.to_string())),
    };
    let pid = child.id();
    let t_spawn = t0.elapsed();
    let stdout = child.take_stdout().unwrap();
    let mut stderr = child.take_stderr().unwrap();
    let errbuf = Arc::new(std::sync::Mutex::new(Vec::<u8>::new()));
    {
        let eb = errbuf.clone();
        std::thread::spawn(move || {
            use std::io::Read;
            let mut c = [0u8; 1024];
            while let Ok(n) = stderr.read(&mut c) { if n == 0 { break; } eb.lock().unwrap().extend_from_slice(&c[..n]); }
        });
    }
    // sampler
    let t_mei = Arc::new(AtomicU64::new(0));
    let t_child = Arc::new(AtomicU64::new(0));
    let done = Arc::new(AtomicBool::new(false));
    let sampler = {
        let (t_mei, t_child, done) = (t_mei.clone(), t_child.clone(), done.clone());
        let tmp = sidecar_tmp.clone();
        std::thread::spawn(move || {
            while !done.load(Ordering::Relaxed) {
                let now = t0.elapsed().as_micros() as u64 + 1;
                if t_mei.load(Ordering::Relaxed) == 0 {
                    if let Ok(rd) = std::fs::read_dir(&tmp) {
                        if rd.flatten().any(|e| e.file_name().to_string_lossy().starts_with("_MEI")) {
                            t_mei.store(now, Ordering::Relaxed);
                        }
                    }
                }
                if t_child.load(Ordering::Relaxed) == 0 && child_count(pid) > 0 {
                    t_child.store(now, Ordering::Relaxed);
                }
                std::thread::sleep(Duration::from_millis(2));
            }
        })
    };
    let handoff = capability::read_handoff(stdout, launch::READY_TIMEOUT);
    let t_handoff = t0.elapsed();
    done.store(true, Ordering::Relaxed);
    let _ = sampler.join();
    let h = match handoff {
        Ok(h) => h,
        Err(e) => {
            std::thread::sleep(Duration::from_millis(150));
            let err = String::from_utf8_lossy(&errbuf.lock().unwrap()).to_string();
            child.stop(GRACE);
            return format!("{{\"ok\":false,\"stage\":\"handoff\",\"error\":{},\"stderr_tail\":{}}}", json_str(&format!("{e:?}")), json_str(&err.chars().rev().take(600).collect::<String>().chars().rev().collect::<String>()));
        }
    };
    let flood = if flood_threads > 0 {
        let stopf = Arc::new(AtomicBool::new(false));
        let (ok, err) = (Arc::new(AtomicU64::new(0)), Arc::new(AtomicU64::new(0)));
        let hs = start_flood(h.port, flood_threads, ok.clone(), err.clone(), stopf.clone());
        Some((stopf, ok, err, hs))
    } else { None };
    let tv = Instant::now();
    let proved = launch::prove_sidecar(h.port, &h.secret, launch::START_VERIFY_DEADLINE, &std::thread::sleep);
    let t_verified = t0.elapsed();
    let verify_ms = ms(tv.elapsed());
    // first 200 API response: mint a code, exchange it for a session, GET /api/projects with the header
    let mut api_ms = -1.0f64;
    let mut api_status = 0u16;
    if proved.is_ok() {
        let ta = Instant::now();
        if let Ok(code) = capability::fresh_launch_code(h.port, &h.secret) {
            let body = format!("{{\"code\":\"{}\"}}", code.expose());
            if let Ok(r) = capability::http_request(h.port, "POST", "/api/local/session", &[("Content-Type", "application/json")], Some(&body), Duration::from_secs(10)) {
                if r.status == 200 {
                    if let Ok(v) = serde_json::from_slice::<serde_json::Value>(&r.body) {
                        if let Some(tok) = v.get("session_token").and_then(|x| x.as_str()) {
                            if let Ok(r2) = capability::http_request(h.port, "GET", "/api/projects", &[("X-Agentic-Session", tok)], None, Duration::from_secs(10)) {
                                api_status = r2.status;
                                if r2.status == 200 { api_ms = ms(t0.elapsed()); }
                            }
                        }
                    }
                }
            }
        }
        let _ = ta;
    }
    let first_ok_total = api_ms;
    if let Some((stopf, ok, err, hs)) = flood {
        stopf.store(true, Ordering::Relaxed);
        for t in hs { let _ = t.join(); }
        let line = format!("{{\"flood_ok\":{},\"flood_err\":{}}}", ok.load(Ordering::Relaxed), err.load(Ordering::Relaxed));
        let _ = line;
    }
    let st = child.stop(GRACE);
    let verdict = match &proved { Ok(_) => "verified".to_string(), Err(e) => e.to_string() };
    let us = |a: &AtomicU64| { let v = a.load(Ordering::Relaxed); if v == 0 { -1.0 } else { (v as f64) / 1000.0 } };
    format!(
        "{{\"ok\":{},\"verdict\":{},\"pid\":{},\"port\":{},\"t_spawn_ms\":{},\"t_mei_ms\":{},\"t_child_ms\":{},\"t_handoff_ms\":{},\"t_verified_ms\":{},\"verify_phase_ms\":{},\"t_first200_ms\":{},\"api_status\":{},\"stop\":{}}}",
        proved.is_ok(), json_str(&verdict), pid, h.port, ms(t_spawn), us(&t_mei), us(&t_child), ms(t_handoff), ms(t_verified), verify_ms, first_ok_total, api_status,
        json_str(&format!("{st:?}"))
    )
}

fn main() {
    let mut st = State { running: None, flood: None };
    let stdin = std::io::stdin();
    emit("{\"event\":\"driver-ready\"}".to_string());
    for line in stdin.lock().lines() {
        let line = match line { Ok(l) => l, Err(_) => break };
        let mut it = line.split_whitespace();
        let cmd = match it.next() { Some(c) => c, None => continue };
        let args: Vec<&str> = it.collect();
        match cmd {
            // ---- main.rs start_native, verbatim logic ----
            "start" => {
                let t = Instant::now();
                if let Some(r) = st.running.as_mut() {
                    if r.child.is_running() {
                        emit(format!("{{\"cmd\":\"start\",\"ok\":true,\"reused\":true,\"url\":{}}}", json_str(&capability::plain_url(r.port, &r.base_path))));
                        continue;
                    }
                    st.running = None; // exited on its own: drop and start fresh
                }
                let mut c = sidecar_cmd();
                if let Some(t) = args.first() { if *t != "-" { c.env("TMPDIR", t); } }
                match launch::start_sidecar(&mut c, launch::READY_TIMEOUT) {
                    Ok(r) => {
                        let url = capability::plain_url(r.port, &r.base_path);
                        emit(format!("{{\"cmd\":\"start\",\"ok\":true,\"pid\":{},\"port\":{},\"url\":{},\"ms\":{}}}", r.child.id(), r.port, json_str(&url), ms(t.elapsed())));
                        st.running = Some(r);
                    }
                    Err(e) => emit(format!("{{\"cmd\":\"start\",\"ok\":false,\"error\":{},\"ms\":{}}}", json_str(&e.to_string()), ms(t.elapsed()))),
                }
            }
            // ---- main.rs open_ui, verbatim logic ----
            "open" => {
                let alive = st.running.as_mut().map(|r| r.child.is_running()).unwrap_or(false);
                let (port, base_path, secret) = match st.running.as_ref().filter(|_| alive) {
                    Some(r) => r.open_params(),
                    None => { emit("{\"cmd\":\"open\",\"ok\":false,\"error\":\"The control plane is not running. Press \\\"Start & open\\\" first.\"}".into()); continue; }
                };
                let t = Instant::now();
                let code = match capability::fresh_launch_code(port, &secret) {
                    Ok(c) => c,
                    Err(e @ capability::OpenError::Untrusted(_)) => {
                        if let Some(r) = st.running.take() { r.stop(); }
                        emit(format!("{{\"cmd\":\"open\",\"ok\":false,\"error\":{}}}", json_str(&format!("The control plane could not prove its identity ({e}). It was stopped and the browser was not opened."))));
                        continue;
                    }
                    Err(capability::OpenError::Busy) => { emit("{\"cmd\":\"open\",\"ok\":false,\"error\":\"The local service is busy. Nothing was opened; try again in a moment.\"}".into()); continue; }
                    Err(e) => { emit(format!("{{\"cmd\":\"open\",\"ok\":false,\"error\":{}}}", json_str(&format!("Could not get a launch code: {e}. Try again, or Stop and Start.")))); continue; }
                };
                // Harness safety: the `open` the launcher code runs MUST be the PATH shim, never /usr/bin/open.
                if !std::env::var("PATH").unwrap_or_default().split(':').next().map(|p| p.ends_with("/gui/shim")).unwrap_or(false) {
                    emit("{\"cmd\":\"open\",\"ok\":false,\"error\":\"HARNESS GUARD: PATH does not start with the open shim; refusing to run the real open\"}".into());
                    continue;
                }
                let dir = cache_launch_dir();
                let file = match opener::write_redirect(&dir, &capability::launch_url(port, &base_path, &code)) {
                    Ok(f) => f,
                    Err(e) => { emit(format!("{{\"cmd\":\"open\",\"ok\":false,\"error\":{},\"kind\":{}}}", json_str(&format!("could not prepare the private launch page: {}", e.kind())), json_str(&format!("{:?}", e.kind())))); continue; }
                };
                opener::remove_after(file.clone(), opener::FILE_LIFETIME);
                let (prog, a) = opener::opener_command(&file);
                let mut oc = Command::new(prog);
                oc.args(a);
                match oc.spawn() {
                    Ok(_) => emit(format!("{{\"cmd\":\"open\",\"ok\":true,\"file\":{},\"ms\":{}}}", json_str(&file.to_string_lossy()), ms(t.elapsed()))),
                    Err(e) => { opener::remove(&file); emit(format!("{{\"cmd\":\"open\",\"ok\":false,\"error\":{}}}", json_str(&format!("could not open the browser: {}", e.kind())))); }
                }
            }
            // ---- main.rs stop_native ----
            "stop" => {
                let t = Instant::now();
                let msg = match st.running.take().map(|r| r.stop()) {
                    Some(StopOutcome::Graceful) => "Stopped.".to_string(),
                    Some(StopOutcome::Forced) => "Stopped (it did not exit in time and was terminated).".to_string(),
                    None => "The control plane was not started by this app.".to_string(),
                };
                emit(format!("{{\"cmd\":\"stop\",\"ok\":true,\"message\":{},\"ms\":{}}}", json_str(&msg), ms(t.elapsed())));
            }
            "legacy" => {
                let w = launch::legacy_listener_warning(launch::LEGACY_PORT);
                emit(format!("{{\"cmd\":\"legacy\",\"warning\":{}}}", match w { Some(s) => json_str(&s), None => "null".into() }));
            }
            // main.rs device_report: `sidecar bootstrap`, stdout+stderr, non-zero exit is an Err
            "check" => {
                let out = Command::new(sidecar()).arg("bootstrap").output();
                match out {
                    Ok(o) => emit(format!("{{\"cmd\":\"check\",\"status\":{},\"stdout\":{},\"stderr\":{}}}", o.status.code().unwrap_or(-1), json_str(&String::from_utf8_lossy(&o.stdout)), json_str(&String::from_utf8_lossy(&o.stderr)))),
                    Err(e) => emit(format!("{{\"cmd\":\"check\",\"error\":{}}}", json_str(&e.to_string()))),
                }
            }
            // main.rs docker_available / advanced_docker_*: same Command, same PATH lookup
            "docker_available" => {
                let ok = Command::new("docker").arg("--version").output().map(|o| o.status.success()).unwrap_or(false);
                emit(format!("{{\"cmd\":\"docker_available\",\"result\":{ok}}}"));
            }
            "docker_up" | "docker_down" | "docker_purge" => {
                let bundle = std::env::var("RDO_BUNDLE_DIR").unwrap_or_else(|_| "../sidekick-devops".to_string());
                let a: &[&str] = match cmd { "docker_up" => &["compose", "up", "-d"], "docker_down" => &["compose", "down"], _ => &["compose", "down", "-v", "--remove-orphans"] };
                match Command::new("docker").current_dir(&bundle).args(a).output() {
                    Ok(o) => emit(format!("{{\"cmd\":\"{cmd}\",\"status\":{},\"stderr\":{}}}", o.status.code().unwrap_or(-1), json_str(&String::from_utf8_lossy(&o.stderr)))),
                    Err(e) => emit(format!("{{\"cmd\":\"{cmd}\",\"error\":{}}}", json_str(&format!("failed to launch: {e}")))),
                }
            }
            "status" => match st.running.as_mut() {
                Some(r) => { let alive = r.child.is_running(); emit(format!("{{\"cmd\":\"status\",\"tracked\":true,\"alive\":{alive},\"pid\":{},\"port\":{}}}", r.child.id(), r.port)); }
                None => emit("{\"cmd\":\"status\",\"tracked\":false}".into()),
            },
            // Test-only: a fresh single-use launch code (printed!). Never used outside the disposable env.
            "mintcode" => {
                match st.running.as_ref().map(|r| r.open_params()) {
                    Some((port, _bp, secret)) => match capability::fresh_launch_code(port, &secret) {
                        Ok(c) => emit(format!("{{\"cmd\":\"mintcode\",\"ok\":true,\"port\":{port},\"code\":{}}}", json_str(c.expose()))),
                        Err(e) => emit(format!("{{\"cmd\":\"mintcode\",\"ok\":false,\"error\":{}}}", json_str(&e.to_string()))),
                    },
                    None => emit("{\"cmd\":\"mintcode\",\"ok\":false,\"error\":\"not running\"}".into()),
                }
            }
            // timed_start [tmpdir|-] [flood_threads]
            "timed_start" => {
                let tmp = args.first().filter(|t| **t != "-").copied();
                let fl: usize = args.get(1).and_then(|x| x.parse().ok()).unwrap_or(0);
                emit(timed_start(tmp, fl));
            }
            // flood N: handshake flood from N threads against the running sidecar until `floodstop`
            "flood" => {
                if let (Some(r), Some(n)) = (st.running.as_ref(), args.first().and_then(|x| x.parse::<usize>().ok())) {
                    let (stopf, ok, err) = (Arc::new(AtomicBool::new(false)), Arc::new(AtomicU64::new(0)), Arc::new(AtomicU64::new(0)));
                    let hs = start_flood(r.port, n, ok.clone(), err.clone(), stopf.clone());
                    st.flood = Some((stopf, ok, err, hs));
                    emit(format!("{{\"cmd\":\"flood\",\"threads\":{n}}}"));
                } else { emit("{\"cmd\":\"flood\",\"error\":\"not running or bad arg\"}".into()); }
            }
            "floodstop" => {
                if let Some((stopf, ok, err, hs)) = st.flood.take() {
                    stopf.store(true, Ordering::Relaxed);
                    for t in hs { let _ = t.join(); }
                    emit(format!("{{\"cmd\":\"floodstop\",\"ok\":{},\"err\":{}}}", ok.load(Ordering::Relaxed), err.load(Ordering::Relaxed)));
                }
            }
            "sleep" => { std::thread::sleep(Duration::from_millis(args.first().and_then(|x| x.parse().ok()).unwrap_or(0))); emit("{\"cmd\":\"sleep\"}".into()); }
            // RunEvent::Exit: stop the tree, delete the redirect files
            "quit" => {
                if let Some((stopf, _, _, hs)) = st.flood.take() { stopf.store(true, Ordering::Relaxed); for t in hs { let _ = t.join(); } }
                let o = st.running.take().map(|r| r.stop());
                opener::cleanup_all();
                emit(format!("{{\"cmd\":\"quit\",\"stop\":{}}}", json_str(&format!("{o:?}"))));
                return;
            }
            other => emit(format!("{{\"error\":{}}}", json_str(&format!("unknown command {other}")))),
        }
    }
    // stdin closed: behave like a window that is gone: nothing to do (no Exit hook runs on a crash either)
}
