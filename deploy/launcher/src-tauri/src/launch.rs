// Start the sidecar and prove it is ours, then (separately) obtain a fresh launch code to open the UI.
//
//   spawn  PROJECTS_PORT=0 AGENTIC_OS_HANDOFF=stdout, own process group / Job Object, stdin tether
//   read   the single JSON hand-off line (timeout, size cap, bind_failed handling)
//   prove  HMAC handshake on the reported port; anything that fails it is stopped and refused
//
// A listener that this launcher did not start is never adopted: the port is chosen by the OS at the
// moment the sidecar binds, and only the process that holds this launch's secret can pass the
// handshake (docs/LOCAL_API_SECURITY.md sections 4.1 and 4.2).

use crate::capability::{self, CapabilityError, HandoffError, Secret};
use crate::lifecycle::{ManagedChild, StopOutcome, GRACE};
use std::io::Read;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// Time allowed for the sidecar to extract (PyInstaller onefile), import and bind. Generous: a first
/// start on Windows with antivirus scanning can take tens of seconds.
pub const READY_TIMEOUT: Duration = Duration::from_secs(90);

/// Total time allowed for the start path's identity check (handshake with retries).
pub const START_VERIFY_DEADLINE: Duration = Duration::from_secs(30);

/// A sidecar this launcher started and authenticated.
pub struct Running {
    pub child: ManagedChild,
    pub port: u16,
    pub base_path: String,
    secret: Secret,
}

impl Running {
    #[allow(dead_code)]
    pub fn secret(&self) -> &Secret {
        &self.secret
    }

    /// Stop the whole tree (graceful, then forced); the secret is wiped when `self` drops.
    pub fn stop(self) -> StopOutcome {
        self.child.stop(GRACE)
    }

    /// Non-secret view for an open: what is needed to ask for a fresh launch code.
    pub fn open_params(&self) -> (u16, String, Secret) {
        (self.port, self.base_path.clone(), self.secret.clone())
    }
}

/// Why a start failed. `Display` text is user-facing and contains no secret, code or session token.
#[derive(Debug)]
pub enum StartError {
    Spawn(String),
    BindFailed(String),
    Rejected(String),
    ExitedEarly(String),
    Timeout,
    BadHandoff(&'static str),
    HandoffTooLong,
    /// The listener did not pass the handshake: it was stopped and not opened.
    Untrusted(CapabilityError),
    /// The identity check could not be completed within the deadline because the sidecar stayed busy or
    /// unreachable (flood, overload). Not evidence of a foreign process; what WE spawned was stopped.
    Busy,
}

impl std::fmt::Display for StartError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StartError::Spawn(e) => write!(f, "failed to start the control plane: {e}"),
            StartError::BindFailed(d) => write!(
                f,
                "The control plane could not open a local port ({d}). Nothing was started; \
                 whatever else is listening was not touched or reused."
            ),
            StartError::Rejected(d) => write!(f, "The control plane refused to start: {d}"),
            StartError::ExitedEarly(d) => write!(f, "The control plane exited before it was ready{d}."),
            StartError::Timeout => write!(
                f,
                "The control plane did not report ready within {} s and was stopped.",
                READY_TIMEOUT.as_secs()
            ),
            StartError::BadHandoff(w) => write!(
                f,
                "The control plane sent an unexpected start-up message ({w}); it was stopped and not trusted."
            ),
            StartError::HandoffTooLong => f.write_str(
                "The control plane sent an oversized start-up message; it was stopped and not trusted.",
            ),
            StartError::Busy => write!(
                f,
                "The local service is busy: the control plane this app just started did not answer its \
                 identity check within {} s (something may be flooding the local port, or the machine is \
                 overloaded). It was stopped, nothing was opened and nothing else was used. Try Start again.",
                START_VERIFY_DEADLINE.as_secs()
            ),
            StartError::Untrusted(e) => write!(
                f,
                "The process on the reported port could not prove it is the control plane this app started \
                 ({e}). It was stopped and the browser was not opened."
            ),
        }
    }
}

/// The port the frozen v0.3.2 sidecar listened on (fixed). Current sidecars use an OS-chosen port.
pub const LEGACY_PORT: u16 = 8787;
/// Budget for the legacy probe: start is never slowed by more than this.
const LEGACY_PROBE_BUDGET: Duration = Duration::from_millis(300);

/// Best-effort, non-invasive detection of an OLD v0.3.2 sidecar left running on `port` (the old launcher
/// orphaned it, and it keeps serving an unauthenticated API with wildcard CORS). Only a
/// `GET /api/projects` without credentials: a 200 with the projects list means it is open to anyone. It is
/// neither adopted nor stopped; the caller shows the returned text. Anything else (nothing listening,
/// 401/403/404, slow, not JSON) returns None. Bounded by LEGACY_PROBE_BUDGET.
pub fn legacy_listener_warning(port: u16) -> Option<String> {
    let r = capability::http_request(port, "GET", "/api/projects", &[], None, LEGACY_PROBE_BUDGET).ok()?;
    if r.status != 200 {
        return None;
    }
    let v: serde_json::Value = serde_json::from_slice(&r.body).ok()?;
    // The old sidecar's `/api/projects` is a list of projects with string `id` and `name`
    // ([{"id":"customer-ops","name":"Customer Operations",...}]). Require that shape so an unrelated JSON
    // service on the port does not trigger a false warning (review N-05).
    let looks_like_projects = v.as_array().is_some_and(|a| {
        !a.is_empty() && a.iter().all(|p| p.get("id").is_some_and(|x| x.is_string()) && p.get("name").is_some_and(|x| x.is_string()))
    });
    if !looks_like_projects {
        return None;
    }
    Some(format!(
        "An old, unauthenticated Agentic Apps service is running on port {port} (it answers without any \
         credential). It was not started by this version and is not used. Quit it: close the old app, or \
         end the process listening on 127.0.0.1:{port}."
    ))
}

/// Collect up to 2 KiB of the sidecar's stderr tail for failure messages. The contract keeps secrets out
/// of stderr; nothing is shown unless the start fails.
fn drain_stderr(mut r: impl Read + Send + 'static) -> Arc<Mutex<Vec<u8>>> {
    let tail = Arc::new(Mutex::new(Vec::new()));
    let t = Arc::clone(&tail);
    std::thread::spawn(move || {
        let mut chunk = [0u8; 512];
        while let Ok(n) = r.read(&mut chunk) {
            if n == 0 {
                break;
            }
            let mut g = t.lock().unwrap_or_else(|e| e.into_inner());
            g.extend_from_slice(&chunk[..n]);
            let len = g.len();
            if len > 2048 {
                g.drain(..len - 2048);
            }
        }
    });
    tail
}

fn stderr_hint(tail: &Arc<Mutex<Vec<u8>>>) -> String {
    // give the drain thread a moment to read what the dying process wrote
    std::thread::sleep(Duration::from_millis(150));
    let g = tail.lock().unwrap_or_else(|e| e.into_inner());
    let text = String::from_utf8_lossy(&g);
    let last = text.lines().rev().find(|l| !l.trim().is_empty()).unwrap_or("");
    let clean: String = last.chars().filter(|c| !c.is_control()).take(200).collect();
    if clean.is_empty() { String::new() } else { format!(" ({clean})") }
}

/// Run the HMAC handshake against `port` until it verifies, retrying transient conditions until `deadline`.
/// - `Ok`: the peer holds this launch's secret.
/// - `Untrusted`: final at once for a wrong proof or a peer that is not the sidecar (404/405/other
///   refusals, wrong protocol, malformed answer): a stale v0.3.2 sidecar or a foreign listener can never
///   answer, so it is never reported as merely busy.
/// - `Busy`: only transient conditions (429, 5xx, `401 invalid_proof`, timeouts, resets, refused
///   connection) until the deadline.
pub fn prove_sidecar(port: u16, secret: &Secret, deadline: Duration, sleep: &dyn Fn(Duration)) -> Result<(), StartError> {
    let start = Instant::now();
    let mut attempt = 0u32;
    loop {
        let left = deadline.saturating_sub(start.elapsed());
        if left.is_zero() {
            return Err(StartError::Busy);
        }
        let nonce = capability::fresh_nonce().map_err(StartError::Untrusted)?;
        // each exchange is bounded by what is left of the deadline (at most the normal 5 s)
        match capability::handshake_budgeted(port, secret, &nonce, left.min(Duration::from_secs(5))) {
            Ok(_) => return Ok(()),
            Err(CapabilityError::Unreachable(_)) | Err(CapabilityError::Transient(_)) => {
                attempt += 1;
                let wait = Duration::from_millis(250u64 << attempt.min(2)).min(deadline.saturating_sub(start.elapsed()));
                sleep(wait);
            }
            Err(e) => return Err(StartError::Untrusted(e)),
        }
    }
}

/// Spawn `cmd` (program and fixed arguments already set), read the hand-off, run the handshake.
/// On any failure the spawned tree is stopped before returning.
pub fn start_sidecar(cmd: &mut Command, ready_timeout: Duration) -> Result<Running, StartError> {
    cmd.env("PROJECTS_PORT", "0")
        .env("PROJECTS_HOST", "127.0.0.1")
        .env("PROJECTS_BASE_PATH", "")
        .env("AGENTIC_OS_HANDOFF", "stdout")
        .env_remove("AGENTIC_OS_LOCAL_CAPABILITY") // `off` is refused together with a hand-off
        .env_remove("PROJECTS_ALLOWED_HOSTS") // never widen the Host allowlist for a desktop launch
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = ManagedChild::spawn(cmd).map_err(|e| StartError::Spawn(e.to_string()))?;
    let stdout = match child.take_stdout() {
        Some(s) => s,
        None => {
            child.stop(GRACE);
            return Err(StartError::Spawn("no stdout pipe".into()));
        }
    };
    let tail = child.take_stderr().map(drain_stderr);

    let handoff = match capability::read_handoff(stdout, ready_timeout) {
        Ok(h) => h,
        Err(e) => {
            let hint = tail.as_ref().map(stderr_hint).unwrap_or_default();
            let exited = child.try_exit_status().is_some();
            child.stop(GRACE);
            return Err(match e {
                HandoffError::BindFailed(d) => StartError::BindFailed(d),
                HandoffError::Rejected(d) => StartError::Rejected(d),
                HandoffError::Timeout => StartError::Timeout,
                HandoffError::TooLong => StartError::HandoffTooLong,
                HandoffError::Malformed(w) => StartError::BadHandoff(w),
                HandoffError::Eof => StartError::ExitedEarly(if exited || !hint.is_empty() { hint } else { String::new() }),
            });
        }
    };

    // Nothing is trusted, and no browser is opened, before this passes. Only a VERIFIED failure is final
    // (wrong proof, or a peer whose handshake endpoint is absent/refused: a stale or foreign process);
    // transient conditions get bounded retries with a total deadline, and then "busy".
    if let Err(e) = prove_sidecar(handoff.port, &handoff.secret, START_VERIFY_DEADLINE, &std::thread::sleep) {
        child.stop(GRACE); // only the process tree WE spawned
        return Err(e);
    }
    Ok(Running { child, port: handoff.port, base_path: handoff.base_path, secret: handoff.secret })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::capability::{fresh_launch_code, handshake, http_request, launch_url, HttpResponse};
    use std::io::{Read, Write};
    use std::net::{TcpListener, TcpStream};
    use std::sync::Arc;
    use std::time::Instant;

    /// Real sidecar for the integration tests. Either the frozen binary (`RDO_SIDECAR`) or the source
    /// entry under a CPython (`RDO_TEST_PYTHON`, run from the repository root).
    fn real_sidecar_cmd() -> Command {
        if let Ok(bin) = std::env::var("RDO_SIDECAR") {
            let mut c = Command::new(bin);
            c.args(["serve", "--exit-on-stdin-eof"]);
            return c;
        }
        let py = std::env::var("RDO_TEST_PYTHON").expect("set RDO_SIDECAR or RDO_TEST_PYTHON");
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..").canonicalize().unwrap();
        let mut c = Command::new(py);
        c.arg(root.join("packaging/sidecar_main.py")).args(["serve", "--exit-on-stdin-eof"]);
        c.env("PYTHONPATH", &root).current_dir(&root);
        c
    }

    /// The session the page's script gets from the exchange: since the cookie was removed
    /// (docs/LOCAL_API_SECURITY.md 4.5) it is `session_token` + `csrf_token` in the body.
    fn session_of(r: &HttpResponse) -> (String, String) {
        assert!(
            !r.headers.iter().any(|(k, _)| k.eq_ignore_ascii_case("set-cookie")),
            "the sidecar must not set a cookie any more"
        );
        let v: serde_json::Value = serde_json::from_slice(&r.body).expect("session JSON");
        assert_eq!(v["session_header"], "X-Agentic-Session");
        (v["session_token"].as_str().unwrap().to_string(), v["csrf_token"].as_str().unwrap().to_string())
    }

    fn get(port: u16, path: &str, session: Option<&str>) -> HttpResponse {
        let h: Vec<(&str, &str)> = session.map(|t| vec![("X-Agentic-Session", t)]).unwrap_or_default();
        http_request(port, "GET", path, &h, None, Duration::from_secs(5)).unwrap()
    }

    fn gone(port: u16, pid: u32) -> bool {
        let end = Instant::now() + Duration::from_secs(5);
        while Instant::now() < end {
            #[cfg(unix)]
            let procs_gone = unsafe { libc::kill(-(pid as i32), 0) } == -1;
            #[cfg(not(unix))]
            let procs_gone = {
                let _ = pid;
                true
            };
            if procs_gone && TcpStream::connect(("127.0.0.1", port)).is_err() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        false
    }

    /// One-connection fake peer: answers the first request with `raw` after `delay`.
    fn fake_peer(raw: String, delay: Duration) -> u16 {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        std::thread::spawn(move || {
            if let Ok((mut s, _)) = l.accept() {
                let mut b = [0u8; 4096];
                let _ = s.read(&mut b);
                std::thread::sleep(delay);
                let _ = s.write_all(raw.as_bytes());
            }
        });
        port
    }

    fn http(status: &str, body: &str) -> String {
        format!("HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len())
    }

    #[test]
    fn legacy_listener_with_an_open_api_is_reported_not_touched() {
        let port = fake_peer(http("200 OK", "[{\"id\":\"customer-ops\",\"name\":\"Customer Operations\"}]"), Duration::ZERO);
        let w = legacy_listener_warning(port).expect("warning");
        assert!(w.contains("old, unauthenticated Agentic Apps service") && w.contains(&port.to_string()) && w.contains("Quit it"));
    }

    #[test]
    fn legacy_probe_ignores_gated_missing_and_unrelated_listeners() {
        // the current sidecar (gated): 401
        assert!(legacy_listener_warning(fake_peer(http("401 Unauthorized", "{\"error\":{\"code\":\"auth_required\"}}"), Duration::ZERO)).is_none());
        assert!(legacy_listener_warning(fake_peer(http("404 Not Found", "{}"), Duration::ZERO)).is_none());
        // 200 JSON that is not the projects list (some other service on 8787): no false warning (N-05)
        assert!(legacy_listener_warning(fake_peer(http("200 OK", "{\"status\":\"ok\"}"), Duration::ZERO)).is_none());
        assert!(legacy_listener_warning(fake_peer(http("200 OK", "[1,2,3]"), Duration::ZERO)).is_none());
        assert!(legacy_listener_warning(fake_peer(http("200 OK", "[]"), Duration::ZERO)).is_none());
        // 200 but not JSON (some other web server)
        assert!(legacy_listener_warning(fake_peer(http("200 OK", "<html>hi</html>"), Duration::ZERO)).is_none());
        // nothing listening
        let free = { TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port() };
        assert!(legacy_listener_warning(free).is_none());
    }

    #[test]
    fn legacy_probe_never_slows_start_by_more_than_its_budget() {
        let port = fake_peer(http("200 OK", "[{\"id\":\"a\",\"name\":\"b\"}]"), Duration::from_secs(3)); // slow peer
        let t0 = Instant::now();
        assert!(legacy_listener_warning(port).is_none());
        assert!(t0.elapsed() < Duration::from_millis(600), "took {:?}", t0.elapsed());
    }

    // --- N-03: the START path's identity check (flood => retry/busy, stale/foreign => untrusted) ----

    use capability::{handshake_proof, PROTO};
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use std::sync::Mutex;

    #[derive(Clone)]
    enum Hs {
        Honest,
        WrongProof,
        Status(u16, &'static str), // status, error.code ("" for none)
        Drop,                      // close without answering
        Hang,                      // read the request and never answer
    }

    struct HsServer {
        port: u16,
        calls: Arc<AtomicUsize>,
        stop: Arc<AtomicBool>,
    }
    impl Drop for HsServer {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::SeqCst);
        }
    }

    /// Handshake double. `script` is consumed one answer per connection; when exhausted: `then`.
    fn hs_server(secret: Secret, script: Vec<Hs>, then: Hs) -> HsServer {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        l.set_nonblocking(true).unwrap();
        let port = l.local_addr().unwrap().port();
        let (calls, stop) = (Arc::new(AtomicUsize::new(0)), Arc::new(AtomicBool::new(false)));
        let (c2, s2) = (calls.clone(), stop.clone());
        let script = Arc::new(Mutex::new(script.into_iter().collect::<std::collections::VecDeque<_>>()));
        std::thread::spawn(move || {
            let mut held = Vec::new();
            while !s2.load(Ordering::SeqCst) {
                let Ok((mut s, _)) = l.accept() else {
                    std::thread::sleep(Duration::from_millis(5));
                    continue;
                };
                s.set_nonblocking(false).ok();
                let mut buf = [0u8; 4096];
                let n = s.read(&mut buf).unwrap_or(0);
                let req = String::from_utf8_lossy(&buf[..n]).to_string();
                c2.fetch_add(1, Ordering::SeqCst);
                let step = script.lock().unwrap().pop_front().unwrap_or(then.clone());
                let nonce = req
                    .split("\"client_nonce\":\"")
                    .nth(1)
                    .and_then(|x| x.split('"').next())
                    .unwrap_or("")
                    .to_string();
                let ok_body = |proof: String| {
                    format!(
                        "{{\"v\":1,\"proto\":\"{PROTO}\",\"proof\":\"{proof}\",\"server_nonce\":\"{}\",\"expires_in\":30}}",
                        "S".repeat(22)
                    )
                };
                let reply = |status: &str, body: &str| {
                    format!("HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len())
                };
                match step {
                    Hs::Honest => {
                        let _ = s.write_all(reply("200 OK", &ok_body(handshake_proof(&secret, port, &nonce))).as_bytes());
                    }
                    Hs::WrongProof => {
                        let _ = s.write_all(reply("200 OK", &ok_body("A".repeat(43))).as_bytes());
                    }
                    Hs::Status(st, code) => {
                        let body = if code.is_empty() {
                            "{\"detail\":\"x\"}".to_string()
                        } else {
                            format!("{{\"error\":{{\"code\":\"{code}\",\"message\":\"x\"}}}}")
                        };
                        let _ = s.write_all(reply(&format!("{st} X"), &body).as_bytes());
                    }
                    Hs::Drop => {}
                    Hs::Hang => held.push(s), // keep the connection open, silent
                }
            }
        });
        HsServer { port, calls, stop }
    }

    fn key() -> Secret {
        Secret::from_bytes([42; 32])
    }

    #[test]
    fn start_path_flood_answers_are_retried_until_the_handshake_verifies() {
        for transient in [Hs::Status(429, "rate_limited"), Hs::Status(503, ""), Hs::Status(401, "invalid_proof"), Hs::Drop] {
            let srv = hs_server(key(), vec![transient.clone(), transient.clone(), transient], Hs::Honest);
            let slept = Mutex::new(0usize);
            let r = prove_sidecar(srv.port, &key(), Duration::from_secs(30), &|_| *slept.lock().unwrap() += 1);
            assert!(r.is_ok(), "{r:?}");
            assert_eq!(srv.calls.load(Ordering::SeqCst), 4, "3 transient answers, then a verified handshake");
            assert_eq!(*slept.lock().unwrap(), 3);
        }
    }

    #[test]
    fn start_path_that_stays_busy_ends_busy_at_the_deadline_not_untrusted() {
        for transient in [Hs::Status(429, "rate_limited"), Hs::Status(500, ""), Hs::Drop, Hs::Hang] {
            let srv = hs_server(key(), vec![], transient);
            let t0 = Instant::now();
            let r = prove_sidecar(srv.port, &key(), Duration::from_millis(1500), &std::thread::sleep);
            assert!(matches!(r, Err(StartError::Busy)), "{r:?}");
            assert!(t0.elapsed() < Duration::from_millis(2500), "bounded by the deadline: {:?}", t0.elapsed());
            let msg = r.unwrap_err().to_string();
            assert!(msg.contains("busy") && msg.contains("stopped") && msg.contains("nothing was opened"), "{msg}");
        }
        // nothing listening at all: also only busy after the deadline
        let port = { TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port() };
        assert!(matches!(prove_sidecar(port, &key(), Duration::from_millis(900), &std::thread::sleep), Err(StartError::Busy)));
    }

    #[test]
    fn start_path_stale_or_foreign_peers_are_untrusted_at_once_never_busy() {
        // a stale v0.3.2 sidecar (404/405), another gate (401 auth_required, 403), a redirect, a 200 that is
        // not the protocol or carries a wrong proof: all final on the FIRST answer
        for peer in [
            Hs::Status(404, ""),
            Hs::Status(405, ""),
            Hs::Status(401, "auth_required"),
            Hs::Status(403, "origin_not_allowed"),
            Hs::Status(301, ""),
            Hs::WrongProof,
        ] {
            let srv = hs_server(key(), vec![], peer);
            let t0 = Instant::now();
            let r = prove_sidecar(srv.port, &key(), Duration::from_secs(30), &|_| panic!("must not retry or wait"));
            assert!(matches!(r, Err(StartError::Untrusted(_))), "{r:?}");
            assert_eq!(srv.calls.load(Ordering::SeqCst), 1);
            assert!(t0.elapsed() < Duration::from_secs(1), "promptly: {:?}", t0.elapsed());
        }
    }

    #[test]
    fn start_path_a_failed_proof_after_transient_answers_is_still_final() {
        let srv = hs_server(key(), vec![Hs::Status(429, "rate_limited")], Hs::WrongProof);
        let r = prove_sidecar(srv.port, &key(), Duration::from_secs(30), &|_| ());
        assert!(matches!(r, Err(StartError::Untrusted(capability::CapabilityError::ProofMismatch))), "{r:?}");
        assert_eq!(srv.calls.load(Ordering::SeqCst), 2);
    }

    #[test]
    fn open_path_treats_a_handshake_429_as_busy_not_untrusted() {
        let srv = hs_server(key(), vec![], Hs::Status(429, "rate_limited"));
        let r = capability::fresh_launch_code_with(srv.port, &key(), 3, &|_| ());
        assert_eq!(r.unwrap_err(), capability::OpenError::Busy);
        let srv = hs_server(key(), vec![], Hs::Status(404, ""));
        assert!(matches!(capability::fresh_launch_code_with(srv.port, &key(), 3, &|_| ()), Err(capability::OpenError::Untrusted(_))));
    }

    /// Normal start -> handshake OK -> launch URL has #lc= -> the code yields a working session ->
    /// the code is single use -> another open mints another code -> Stop leaves nothing.
    /// `RDO_SIDECAR=<frozen> | RDO_TEST_PYTHON=<python>  cargo test real_sidecar -- --ignored`
    #[test]
    #[ignore]
    fn real_sidecar_start_handshake_open_exchange_stop() {
        let mut cmd = real_sidecar_cmd();
        let running = start_sidecar(&mut cmd, READY_TIMEOUT).expect("start + handshake");
        let (port, pid) = (running.port, running.child.id());
        assert!(port > 1023 && port != 8787, "OS-chosen port, not the legacy fixed one");

        // before a session nothing is served
        assert_eq!(get(port, "/api/projects", None).status, 401);

        // open: verified handshake + fresh code; the URL carries it in the fragment only
        let code = fresh_launch_code(port, running.secret()).expect("fresh launch code");
        let url = launch_url(port, &running.base_path, &code);
        assert!(url.starts_with(&format!("http://127.0.0.1:{port}/#lc=")));

        // SR-01: the opener only ever gets a file PATH. The redirect page carries the URL; the code the
        // browser would read from its fragment is what gets exchanged below.
        let dir = std::env::temp_dir().join(format!("rdo-launch-it-{}", std::process::id()));
        let file = crate::opener::write_redirect(&dir, &url).expect("redirect page");
        let (_prog, args) = crate::opener::opener_command(&file);
        assert!(args.iter().all(|a| !a.to_string_lossy().contains("lc=")), "no code in the opener argv");
        let page = std::fs::read_to_string(&file).unwrap();
        let frag_code = page.split("#lc=").nth(1).unwrap().split('"').next().unwrap().to_string();
        assert_eq!(frag_code, code.expose(), "the fragment in the redirect page is the minted code");
        crate::opener::remove(&file);
        std::fs::remove_dir(&dir).unwrap();

        // what the page's script does with the fragment: exchange it for a session
        let body = format!("{{\"code\":\"{frag_code}\"}}");
        let ex = http_request(port, "POST", "/api/local/session", &[], Some(&body), Duration::from_secs(5)).unwrap();
        assert_eq!(ex.status, 200, "exchanging the launch code must yield a session");
        let (token, _csrf) = session_of(&ex);
        let projects = get(port, "/api/projects", Some(&token));
        assert_eq!(projects.status, 200, "the session works");
        assert!(String::from_utf8_lossy(&projects.body).contains("customer-ops"));
        // the old cookie credential is gone: presenting it as a cookie authenticates nothing
        let as_cookie = http_request(
            port,
            "GET",
            "/api/projects",
            &[("Cookie", &format!("agentic_os_session={token}"))],
            None,
            Duration::from_secs(5),
        )
        .unwrap();
        assert_eq!(as_cookie.status, 401);

        // single use
        let again = http_request(port, "POST", "/api/local/session", &[], Some(&body), Duration::from_secs(5)).unwrap();
        assert_eq!(again.status, 401);

        // every open mints a different code, which also works
        let code2 = fresh_launch_code(port, running.secret()).unwrap();
        assert_ne!(code.expose(), code2.expose());
        let body2 = format!("{{\"code\":\"{}\"}}", code2.expose());
        let ex2 = http_request(port, "POST", "/api/local/session", &[], Some(&body2), Duration::from_secs(5)).unwrap();
        assert_eq!(ex2.status, 200);

        // a launcher that does not hold this launch's secret cannot mint, and cannot even handshake
        let other = capability::Secret::from_bytes([1; 32]);
        assert_eq!(handshake(port, &other), Err(capability::CapabilityError::ProofMismatch));

        // Stop: whole tree gone, port closed
        assert_eq!(running.stop(), StopOutcome::Graceful);
        assert!(gone(port, pid), "no process left in the group and the port is closed");
    }

    /// The review's flood (SR-03): local threads hammer the unauthenticated handshake endpoint while the
    /// launcher opens repeatedly. Hard guarantees at any intensity: no open is ever reported as an
    /// untrusted sidecar, the user's sidecar keeps running and works again after the flood. With a
    /// moderate flood (enough to evict challenges, which made 0 of 200 opens succeed before the fix)
    /// opens must also still succeed.
    #[test]
    #[ignore]
    fn real_sidecar_survives_a_handshake_flood() {
        use std::sync::atomic::{AtomicBool, Ordering};
        let mut cmd = real_sidecar_cmd();
        let mut running = start_sidecar(&mut cmd, READY_TIMEOUT).expect("start");
        let port = running.port;
        for (threads, opens, must_succeed) in [(8usize, 40usize, true), (32, 8, false), (64, 4, false)] {
            let stop = Arc::new(AtomicBool::new(false));
            let flooders: Vec<_> = (0..threads)
                .map(|_| {
                    let stop = stop.clone();
                    std::thread::spawn(move || {
                        while !stop.load(Ordering::SeqCst) {
                            let _ = http_request(port, "POST", "/api/local/handshake", &[], Some("{\"client_nonce\":\"AAAAAAAAAAAAAAAAAAAAAA\"}"), Duration::from_secs(2));
                        }
                    })
                })
                .collect();
            let (mut ok, mut busy, mut untrusted, mut other) = (0, 0, 0, 0);
            for _ in 0..opens {
                match fresh_launch_code(port, running.secret()) {
                    Ok(_) => ok += 1,
                    Err(capability::OpenError::Busy) => busy += 1,
                    Err(capability::OpenError::Untrusted(_)) => untrusted += 1,
                    Err(_) => other += 1,
                }
            }
            // N-03: the START path's identity check under the same flood. Verified, or busy after its
            // deadline; never untrusted (that would have stopped the sidecar we just spawned).
            let proved = prove_sidecar(port, running.secret(), Duration::from_secs(10), &std::thread::sleep);
            println!("RESULT {threads} flood threads: start-path identity check => {}", match &proved {
                Ok(()) => "verified".to_string(),
                Err(StartError::Busy) => "busy".to_string(),
                Err(_) => "UNTRUSTED".to_string(),
            });
            assert!(matches!(proved, Ok(()) | Err(StartError::Busy)), "a flood must never make Start untrusted: {proved:?}");
            stop.store(true, Ordering::SeqCst);
            for f in flooders {
                let _ = f.join();
            }
            println!("RESULT {threads} flood threads: ok={ok} busy={busy} untrusted={untrusted} other={other} of {opens}");
            assert_eq!(untrusted, 0, "a flood must never look like an untrusted sidecar (review: 116 of 200 before)");
            if must_succeed {
                assert!(ok > 0, "opens still succeed under a moderate flood");
            }
            assert!(running.child.is_running(), "the user's sidecar is still running");
            std::thread::sleep(Duration::from_millis(500));
            assert!(fresh_launch_code(port, running.secret()).is_ok(), "and works after the flood");
        }
        assert_eq!(running.stop(), StopOutcome::Graceful);
    }

    /// End to end through a real (headless) browser, for SR-01: mint a code with the real sidecar, write the
    /// private redirect page, let a headless browser open THE FILE (as the OS opener would), and require
    /// that it lands on the sidecar origin, signs in from the fragment (the UI script strips it) and gets
    /// data from the API. `RDO_BROWSER_CMD` is a command line `<program> <args...>`; it receives
    /// `<redirect file> <port>` as the last two arguments and prints one JSON line.
    #[test]
    #[ignore]
    fn real_sidecar_redirect_page_signs_in_through_a_headless_browser() {
        let cmdline = std::env::var("RDO_BROWSER_CMD").expect("set RDO_BROWSER_CMD");
        let mut cmd = real_sidecar_cmd();
        let running = start_sidecar(&mut cmd, READY_TIMEOUT).expect("start");
        let code = fresh_launch_code(running.port, running.secret()).unwrap();
        let dir = std::env::temp_dir().join(format!("rdo-launch-browser-{}", std::process::id()));
        let file = crate::opener::write_redirect(&dir, &launch_url(running.port, &running.base_path, &code)).unwrap();
        let mut parts = cmdline.split_whitespace();
        let out = Command::new(parts.next().unwrap())
            .args(parts)
            .arg(&file)
            .arg(running.port.to_string())
            .output()
            .expect("run browser driver");
        let text = String::from_utf8_lossy(&out.stdout).to_string();
        println!("browser driver output: {}", text.replace(code.expose(), "<code>"));
        crate::opener::remove(&file);
        let _ = std::fs::remove_dir(&dir);
        let v: serde_json::Value = serde_json::from_str(text.lines().last().unwrap_or("{}")).expect("driver JSON");
        assert_eq!(v["landedOnSidecar"], true);
        assert_eq!(v["fragmentGone"], true, "the UI script removed #lc= from the address bar");
        assert_eq!(v["apiStatus"], 200, "the page is signed in and gets data");
        // the code was used by the browser: it is dead now
        let body = format!("{{\"code\":\"{}\"}}", code.expose());
        let again = http_request(running.port, "POST", "/api/local/session", &[], Some(&body), Duration::from_secs(5)).unwrap();
        assert_eq!(again.status, 401);
        running.stop();
    }

    /// A stale v0.3.2 sidecar (no handshake, no gate) and a foreign listener are refused; and the new
    /// sidecar neither collides with them nor touches them.
    /// `RDO_STALE_SIDECAR=<v0.3.2 frozen sidecar> + the variables above  cargo test stale -- --ignored`
    #[test]
    #[ignore]
    fn stale_v032_sidecar_and_foreign_listener_are_refused() {
        let stale_bin = std::env::var("RDO_STALE_SIDECAR").expect("set RDO_STALE_SIDECAR");
        let free = {
            let l = TcpListener::bind("127.0.0.1:0").unwrap();
            l.local_addr().unwrap().port()
        };
        let mut stale = Command::new(stale_bin);
        stale.arg("serve").env("PROJECTS_PORT", free.to_string()).stdout(Stdio::null()).stderr(Stdio::null());
        let stale = ManagedChild::spawn(&mut stale).expect("spawn stale");
        let end = Instant::now() + Duration::from_secs(90);
        while TcpStream::connect(("127.0.0.1", free)).is_err() {
            assert!(Instant::now() < end, "stale sidecar never listened");
            std::thread::sleep(Duration::from_millis(200));
        }
        // sanity: the stale one really is unauthenticated (this is the v0.3.2 exposure)
        assert_eq!(get(free, "/api/projects", None).status, 200);
        // ... and the launcher's best-effort legacy probe recognises its real answer shape (SR-07/N-05)
        assert!(legacy_listener_warning(free).is_some(), "the real v0.3.2 projects list must trigger the warning");

        // a new launch is unaffected (different OS-chosen port) ...
        let mut cmd = real_sidecar_cmd();
        let running = start_sidecar(&mut cmd, READY_TIMEOUT).expect("new sidecar next to a stale one");
        assert_ne!(running.port, free);
        // ... and the secret of this launch is useless against the stale process: it is refused
        let err = handshake(free, running.secret()).unwrap_err();
        assert!(matches!(err, capability::CapabilityError::Untrusted(_)), "stale sidecar must be untrusted: {err:?}");
        assert!(fresh_launch_code(free, running.secret()).is_err(), "no launch code from a stale sidecar");

        // a foreign listener that answers 200 with a made-up proof
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let fport = l.local_addr().unwrap().port();
        std::thread::spawn(move || {
            if let Ok((mut s, _)) = l.accept() {
                use std::io::{Read, Write};
                let mut b = [0u8; 2048];
                let _ = s.read(&mut b);
                let body = format!(
                    "{{\"v\":1,\"proto\":\"{}\",\"proof\":\"{}\",\"server_nonce\":\"{}\"}}",
                    capability::PROTO,
                    "A".repeat(43),
                    "B".repeat(22)
                );
                let _ = write!(s, "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
            }
        });
        assert_eq!(handshake(fport, running.secret()), Err(capability::CapabilityError::ProofMismatch));

        let (rp, rpid) = (running.port, running.child.id());
        running.stop();
        assert!(gone(rp, rpid));
        // the stale process was not touched by any of this
        assert_eq!(get(free, "/api/projects", None).status, 200);
        stale.stop(GRACE);
    }
}
