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
use std::time::Duration;

/// Time allowed for the sidecar to extract (PyInstaller onefile), import and bind. Generous: a first
/// start on Windows with antivirus scanning can take tens of seconds.
pub const READY_TIMEOUT: Duration = Duration::from_secs(90);

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

/// Why a start failed. `Display` text is user-facing and contains no secret, code or cookie.
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
            StartError::Untrusted(e) => write!(
                f,
                "The process on the reported port could not prove it is the control plane this app started \
                 ({e}). It was stopped and the browser was not opened."
            ),
        }
    }
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

    // Nothing is trusted, and no browser is opened, before this passes.
    if let Err(e) = capability::handshake(handoff.port, &handoff.secret) {
        child.stop(GRACE);
        return Err(StartError::Untrusted(e));
    }
    Ok(Running { child, port: handoff.port, base_path: handoff.base_path, secret: handoff.secret })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::capability::{fresh_launch_code, handshake, http_request, launch_url, HttpResponse};
    use std::net::{TcpListener, TcpStream};
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

    fn cookie_of(r: &HttpResponse) -> String {
        let sc = r.headers.iter().find(|(k, _)| k.eq_ignore_ascii_case("set-cookie")).expect("Set-Cookie").1.clone();
        sc.split(';').next().unwrap().to_string()
    }

    fn get(port: u16, path: &str, cookie: Option<&str>) -> HttpResponse {
        let h: Vec<(&str, &str)> = cookie.map(|c| vec![("Cookie", c)]).unwrap_or_default();
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

        // what the page's script does with the fragment: exchange it for a session
        let body = format!("{{\"code\":\"{}\"}}", code.expose());
        let ex = http_request(port, "POST", "/api/local/session", &[], Some(&body), Duration::from_secs(5)).unwrap();
        assert_eq!(ex.status, 200, "exchanging the launch code must yield a session");
        let cookie = cookie_of(&ex);
        assert!(cookie.starts_with("agentic_os_session="));
        let projects = get(port, "/api/projects", Some(&cookie));
        assert_eq!(projects.status, 200, "the session works");
        assert!(String::from_utf8_lossy(&projects.body).contains("customer-ops"));

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
