// Launcher side of the sidecar's local capability contract (docs/LOCAL_API_SECURITY.md section 4):
//   4.1 read the single JSON hand-off line the sidecar writes on its stdout,
//   4.2 authenticate the sidecar with the HMAC handshake BEFORE trusting or opening anything,
//   4.4 mint a fresh single-use launch code for every "open",
//   4.3 build the URL that carries the code in the fragment only.
//
// Hand-off channel: `AGENTIC_OS_HANDOFF=stdout` (not `fd:N`). A Rust launcher cannot portably pass an
// arbitrary extra inheritable file descriptor on Windows (std::process only wires stdin/out/err; extra
// handles need raw CreateProcess + handle-list plumbing), while a piped stdout works identically on
// every platform and the sidecar already moves its banner and access log off stdout in this mode.
//
// Secrecy rules enforced here: the secret, the launch code and any session token are never written to a log,
// never put in an error message, never given to the UI, and the secret is wiped on drop. Error values
// carry only fixed text and public numbers (status codes, ports).
//
// No new packages: sha2 (HMAC is 15 lines on top of it) and getrandom are already in the tauri tree.
// HTTP is a minimal HTTP/1.1 client over std::net (the handshake is two small loopback POSTs).

use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fmt;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::sync::mpsc;
use std::time::{Duration, Instant};

pub const PROTO: &str = "agentic-os-local-capability/1";
const HANDSHAKE_CONTEXT: &str = "agentic-os-handshake-v1";
const MINT_CONTEXT: &str = "agentic-os-mint-v1";
/// A hand-off line is about 330 bytes; anything past this is refused instead of buffered.
pub const MAX_HANDOFF_LINE: usize = 4096;
/// Handshake/mint responses are tiny JSON objects.
const MAX_RESPONSE: usize = 16 * 1024;
/// Total budget for one HTTP exchange with the sidecar.
const HTTP_TIMEOUT: Duration = Duration::from_secs(5);

// ---------------------------------------------------------------------------------------------
// base64url (no padding), strict
// ---------------------------------------------------------------------------------------------

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

pub fn b64url_encode(data: &[u8]) -> String {
    let mut out = String::with_capacity((data.len() * 4).div_ceil(3));
    for chunk in data.chunks(3) {
        let n = match chunk.len() {
            3 => (chunk[0] as u32) << 16 | (chunk[1] as u32) << 8 | chunk[2] as u32,
            2 => (chunk[0] as u32) << 16 | (chunk[1] as u32) << 8,
            _ => (chunk[0] as u32) << 16,
        };
        let chars = chunk.len() + 1;
        for i in 0..chars {
            out.push(B64[((n >> (18 - 6 * i)) & 63) as usize] as char);
        }
    }
    out
}

/// Strict decode: URL-safe alphabet only, no padding, canonical (unused trailing bits must be zero).
pub fn b64url_decode(s: &str) -> Option<Vec<u8>> {
    let bytes = s.as_bytes();
    if bytes.len() % 4 == 1 {
        return None;
    }
    let mut out = Vec::with_capacity(bytes.len() * 3 / 4);
    let mut acc: u32 = 0;
    let mut bits = 0;
    for &c in bytes {
        let v = B64.iter().position(|&x| x == c)? as u32;
        acc = (acc << 6) | v;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
            acc &= (1 << bits) - 1;
        }
    }
    if acc != 0 {
        return None; // non-canonical trailing bits
    }
    Some(out)
}

fn is_b64url(s: &str, min: usize, max: usize) -> bool {
    (min..=max).contains(&s.len()) && s.bytes().all(|c| B64.contains(&c))
}

// ---------------------------------------------------------------------------------------------
// HMAC-SHA256 (RFC 2104) and constant-time compare
// ---------------------------------------------------------------------------------------------

pub fn hmac_sha256(key: &[u8], msg: &[u8]) -> [u8; 32] {
    let mut k = [0u8; 64];
    if key.len() > 64 {
        k[..32].copy_from_slice(Sha256::digest(key).as_slice());
    } else {
        k[..key.len()].copy_from_slice(key);
    }
    let mut inner = Sha256::new();
    inner.update(k.map(|b| b ^ 0x36));
    inner.update(msg);
    let ih = inner.finalize();
    let mut outer = Sha256::new();
    outer.update(k.map(|b| b ^ 0x5c));
    outer.update(ih);
    let mut out = [0u8; 32];
    out.copy_from_slice(outer.finalize().as_slice());
    out
}

/// Compare without an early exit. Lengths are public (fixed by the protocol), so a mismatch there
/// returns false immediately.
pub fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut d = 0u8;
    for (x, y) in a.iter().zip(b) {
        d |= x ^ y;
    }
    d == 0
}

fn proof(secret: &Secret, context: &str, port: u16, nonce: &str) -> String {
    let msg = format!("{context}\n{port}\n{nonce}");
    b64url_encode(&hmac_sha256(&secret.0, msg.as_bytes()))
}

/// Proof the sidecar must return for `client_nonce` (4.2).
pub fn handshake_proof(secret: &Secret, port: u16, client_nonce: &str) -> String {
    proof(secret, HANDSHAKE_CONTEXT, port, client_nonce)
}

/// Proof the launcher sends to mint a launch code for `server_nonce` (4.4).
pub fn mint_proof(secret: &Secret, port: u16, server_nonce: &str) -> String {
    proof(secret, MINT_CONTEXT, port, server_nonce)
}

// ---------------------------------------------------------------------------------------------
// Secret (memory only, redacted, wiped)
// ---------------------------------------------------------------------------------------------

/// The 32-byte launch secret. Debug never prints it; Drop overwrites it (best effort).
#[derive(Clone)]
pub struct Secret(pub(crate) [u8; 32]);

impl Secret {
    #[allow(dead_code)] // used by tests and by callers constructing a known key
    pub fn from_bytes(b: [u8; 32]) -> Secret {
        Secret(b)
    }
}
impl fmt::Debug for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Secret(<redacted>)")
    }
}
impl Drop for Secret {
    fn drop(&mut self) {
        for b in self.0.iter_mut() {
            unsafe { std::ptr::write_volatile(b, 0) };
        }
        std::sync::atomic::fence(std::sync::atomic::Ordering::SeqCst);
    }
}

/// A single-use launch code. Debug never prints it.
pub struct LaunchCode(String);
impl LaunchCode {
    pub fn expose(&self) -> &str {
        &self.0
    }
}
impl fmt::Debug for LaunchCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("LaunchCode(<redacted>)")
    }
}

// ---------------------------------------------------------------------------------------------
// 4.1 hand-off line
// ---------------------------------------------------------------------------------------------

/// What the launcher keeps from a `ready` record. The record's own launch code is deliberately not
/// kept: every open mints a fresh one (4.4), and the first code simply expires unused.
#[derive(Debug)]
pub struct Handoff {
    pub port: u16,
    pub base_path: String,
    pub secret: Secret,
}

#[derive(Debug, PartialEq, Eq)]
pub enum HandoffError {
    /// No complete line within the timeout.
    Timeout,
    /// The stream ended before a complete line (the sidecar died or closed stdout).
    Eof,
    /// More than MAX_HANDOFF_LINE bytes without a newline.
    TooLong,
    /// Not the documented record (bad JSON, wrong version/proto/host, bad field). Fixed text only:
    /// the line may contain the secret, so nothing of it is echoed.
    Malformed(&'static str),
    /// `{"event":"error","code":"bind_failed"}`: no secret was sent. Carries the sanitised detail.
    BindFailed(String),
    /// Any other `event":"error"` record.
    Rejected(String),
}

fn sanitize(s: &str, max: usize) -> String {
    s.chars().filter(|c| !c.is_control()).take(max).collect()
}

fn valid_base_path(p: &str) -> bool {
    p.is_empty()
        || (p.starts_with('/')
            && !p.ends_with('/')
            && !p.contains("//")
            && p.len() <= 128
            && p.bytes().all(|c| c.is_ascii_alphanumeric() || b"/-._~".contains(&c)))
}

/// Parse one hand-off line (without its terminator; a trailing `\r` from a Windows text-mode stdout is
/// tolerated).
pub fn parse_handoff_line(line: &[u8]) -> Result<Handoff, HandoffError> {
    let line = line.strip_suffix(b"\r").unwrap_or(line);
    if line.len() > MAX_HANDOFF_LINE {
        return Err(HandoffError::TooLong);
    }
    let v: Value = serde_json::from_slice(line).map_err(|_| HandoffError::Malformed("not JSON"))?;
    let obj = v.as_object().ok_or(HandoffError::Malformed("not an object"))?;
    if obj.get("v").and_then(Value::as_u64) != Some(1) {
        return Err(HandoffError::Malformed("unsupported version"));
    }
    match obj.get("event").and_then(Value::as_str) {
        Some("error") => {
            let code = sanitize(obj.get("code").and_then(Value::as_str).unwrap_or("unknown"), 40);
            let detail = sanitize(obj.get("detail").and_then(Value::as_str).unwrap_or(""), 200);
            return Err(if code == "bind_failed" {
                HandoffError::BindFailed(detail)
            } else {
                HandoffError::Rejected(format!("{code}: {detail}"))
            });
        }
        Some("ready") => {}
        _ => return Err(HandoffError::Malformed("unknown event")),
    }
    if obj.get("proto").and_then(Value::as_str) != Some(PROTO) {
        return Err(HandoffError::Malformed("unknown protocol"));
    }
    if obj.get("host").and_then(Value::as_str) != Some("127.0.0.1") {
        return Err(HandoffError::Malformed("host is not 127.0.0.1"));
    }
    let port = obj
        .get("port")
        .and_then(Value::as_u64)
        .filter(|p| (1..=65535).contains(p))
        .ok_or(HandoffError::Malformed("bad port"))? as u16;
    let base_path = obj.get("base_path").and_then(Value::as_str).unwrap_or("");
    if !valid_base_path(base_path) {
        return Err(HandoffError::Malformed("bad base_path"));
    }
    let secret_s = obj.get("secret").and_then(Value::as_str).ok_or(HandoffError::Malformed("no secret"))?;
    let mut key = [0u8; 32];
    match b64url_decode(secret_s) {
        Some(b) if b.len() == 32 => key.copy_from_slice(&b),
        _ => return Err(HandoffError::Malformed("bad secret")),
    }
    Ok(Handoff { port, base_path: base_path.to_string(), secret: Secret(key) })
}

/// Read the single hand-off line from the sidecar's stdout with a timeout, then keep draining (and
/// discarding) the stream on a background thread so the sidecar can never block on a full pipe.
/// The line is held in memory only and overwritten after parsing.
pub fn read_handoff<R: Read + Send + 'static>(mut stream: R, timeout: Duration) -> Result<Handoff, HandoffError> {
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let mut buf: Vec<u8> = Vec::with_capacity(512);
        let mut chunk = [0u8; 512];
        let mut sent = false;
        loop {
            match stream.read(&mut chunk) {
                Ok(0) | Err(_) => {
                    if !sent {
                        let _ = tx.send(Err(HandoffError::Eof));
                    }
                    break;
                }
                Ok(n) => {
                    if sent {
                        continue; // drain and discard
                    }
                    buf.extend_from_slice(&chunk[..n]);
                    if let Some(pos) = buf.iter().position(|&b| b == b'\n') {
                        let res = parse_handoff_line(&buf[..pos]);
                        let _ = tx.send(res);
                        sent = true;
                        buf.iter_mut().for_each(|b| *b = 0); // wipe the line (it held the secret)
                        buf.clear();
                    } else if buf.len() > MAX_HANDOFF_LINE {
                        let _ = tx.send(Err(HandoffError::TooLong));
                        sent = true;
                        buf.iter_mut().for_each(|b| *b = 0);
                        buf.clear();
                    }
                }
            }
        }
    });
    rx.recv_timeout(timeout).unwrap_or(Err(HandoffError::Timeout))
}

// ---------------------------------------------------------------------------------------------
// minimal HTTP/1.1 client (loopback only)
// ---------------------------------------------------------------------------------------------

#[derive(Debug)]
pub struct HttpResponse {
    pub status: u16,
    #[allow(dead_code)] // read by the integration tests
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl HttpResponse {
    #[allow(dead_code)]
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(k, _)| k.eq_ignore_ascii_case(name)).map(|(_, v)| v.as_str())
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum HttpError {
    /// Could not connect, or the connection failed/timed out mid-exchange.
    Io(String),
    /// The peer's answer is not a well-formed HTTP response, or exceeds the size cap.
    BadResponse(&'static str),
}

fn find(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

/// One request, `Connection: close`, answer capped at MAX_RESPONSE, whole exchange bounded by `budget`.
pub fn http_request(
    port: u16,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: Option<&str>,
    budget: Duration,
) -> Result<HttpResponse, HttpError> {
    let deadline = Instant::now() + budget;
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let mut s = TcpStream::connect_timeout(&addr, budget).map_err(|e| HttpError::Io(e.kind().to_string()))?;
    let left = |d: Instant| d.saturating_duration_since(Instant::now()).max(Duration::from_millis(1));
    s.set_write_timeout(Some(left(deadline))).ok();
    let mut req = format!("{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n");
    for (k, v) in headers {
        req.push_str(&format!("{k}: {v}\r\n"));
    }
    if let Some(b) = body {
        req.push_str(&format!("Content-Type: application/json\r\nContent-Length: {}\r\n", b.len()));
    }
    req.push_str("\r\n");
    if let Some(b) = body {
        req.push_str(b);
    }
    s.write_all(req.as_bytes()).map_err(|e| HttpError::Io(e.kind().to_string()))?;

    let mut buf: Vec<u8> = Vec::new();
    let mut chunk = [0u8; 2048];
    loop {
        if Instant::now() >= deadline {
            return Err(HttpError::Io("timed out".into()));
        }
        s.set_read_timeout(Some(left(deadline))).ok();
        match s.read(&mut chunk) {
            Ok(0) => {
                if buf.is_empty() {
                    return Err(HttpError::Io("connection closed without an answer".into()));
                }
                break;
            }
            Ok(n) => {
                buf.extend_from_slice(&chunk[..n]);
                if buf.len() > MAX_RESPONSE {
                    return Err(HttpError::BadResponse("response too large"));
                }
                if let Some(he) = find(&buf, b"\r\n\r\n") {
                    if let Some(cl) = content_length(&buf[..he]) {
                        if buf.len() >= he + 4 + cl {
                            break;
                        }
                    }
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock || e.kind() == std::io::ErrorKind::TimedOut => {
                return Err(HttpError::Io("timed out".into()));
            }
            Err(e) => return Err(HttpError::Io(e.kind().to_string())),
        }
    }
    parse_response(&buf)
}

fn content_length(head: &[u8]) -> Option<usize> {
    let text = std::str::from_utf8(head).ok()?;
    text.lines().skip(1).find_map(|l| {
        let (k, v) = l.split_once(':')?;
        if k.trim().eq_ignore_ascii_case("content-length") { v.trim().parse().ok() } else { None }
    })
}

fn parse_response(buf: &[u8]) -> Result<HttpResponse, HttpError> {
    let he = find(buf, b"\r\n\r\n").ok_or(HttpError::BadResponse("no header end"))?;
    let head = std::str::from_utf8(&buf[..he]).map_err(|_| HttpError::BadResponse("non-text header"))?;
    let mut lines = head.split("\r\n");
    let status_line = lines.next().unwrap_or("");
    let mut parts = status_line.split_whitespace();
    let version = parts.next().unwrap_or("");
    if !version.starts_with("HTTP/1.") {
        return Err(HttpError::BadResponse("not HTTP"));
    }
    let status: u16 = parts
        .next()
        .and_then(|s| s.parse().ok())
        .filter(|s| (100..=599).contains(s))
        .ok_or(HttpError::BadResponse("bad status"))?;
    let headers: Vec<(String, String)> = lines
        .filter_map(|l| l.split_once(':').map(|(k, v)| (k.trim().to_string(), v.trim().to_string())))
        .collect();
    let mut body = buf[he + 4..].to_vec();
    if let Some(cl) = headers.iter().find(|(k, _)| k.eq_ignore_ascii_case("content-length")) {
        if let Ok(n) = cl.1.parse::<usize>() {
            body.truncate(n);
        }
    }
    Ok(HttpResponse { status, headers, body })
}

// ---------------------------------------------------------------------------------------------
// 4.2 handshake and 4.4 mint
// ---------------------------------------------------------------------------------------------

#[derive(Debug, PartialEq, Eq)]
pub enum CapabilityError {
    /// Transport problem (could not connect, timed out). The peer is not proven to be foreign.
    Unreachable(String),
    /// The peer answered, but not as the sidecar of this launch would: wrong status (a stale or
    /// foreign server has no handshake endpoint), malformed body, or a wrong proof. Untrusted.
    Untrusted(&'static str),
    /// The proof did not verify: whatever listens on the port does not hold this launch's secret.
    ProofMismatch,
    /// The peer answered with a transient condition (429, 5xx, `401 invalid_proof`): a busy or flooded
    /// sidecar, not evidence of a foreign one. Retryable; never a reason to distrust.
    Transient(u16),
}

impl fmt::Display for CapabilityError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CapabilityError::Unreachable(e) => write!(f, "the control plane did not answer ({e})"),
            CapabilityError::Untrusted(why) => write!(f, "the listener did not behave as this launch's control plane ({why})"),
            CapabilityError::ProofMismatch => f.write_str("the listener could not prove it is this launch's control plane"),
            CapabilityError::Transient(st) => write!(f, "the control plane is busy (HTTP {st})"),
        }
    }
}

impl From<HttpError> for CapabilityError {
    fn from(e: HttpError) -> Self {
        match e {
            HttpError::Io(s) => CapabilityError::Unreachable(s),
            HttpError::BadResponse(w) => CapabilityError::Untrusted(w),
        }
    }
}

pub fn fresh_nonce() -> Result<String, CapabilityError> {
    let mut n = [0u8; 16];
    getrandom::fill(&mut n).map_err(|_| CapabilityError::Unreachable("no system randomness".into()))?;
    Ok(b64url_encode(&n)) // 22 chars
}

fn json_body(r: &HttpResponse) -> Result<serde_json::Map<String, Value>, CapabilityError> {
    match serde_json::from_slice::<Value>(&r.body) {
        Ok(Value::Object(m)) => Ok(m),
        _ => Err(CapabilityError::Untrusted("answer is not a JSON object")),
    }
}

/// POST /api/local/handshake with a fresh nonce; verify the proof in constant time. On success returns
/// the sidecar's single-use `server_nonce` (the challenge for a mint). Nothing else may be done with
/// the sidecar before this returns Ok.
pub fn handshake(port: u16, secret: &Secret) -> Result<String, CapabilityError> {
    handshake_with(port, secret, &fresh_nonce()?)
}

/// `handshake` with a caller-chosen nonce (tests).
pub fn handshake_with(port: u16, secret: &Secret, client_nonce: &str) -> Result<String, CapabilityError> {
    handshake_budgeted(port, secret, client_nonce, HTTP_TIMEOUT)
}

/// Is this non-200 answer to the handshake a TRANSIENT condition of a busy/flooded sidecar (retry) rather
/// than proof of a peer that is not ours? 429 and 5xx, and the gate's own busy codes. Everything else
/// (404/405 of a stale v0.3.2 sidecar, 401 `auth_required` or 403 of another gate, 3xx, other 4xx) means
/// the handshake endpoint is absent or refused: Untrusted at once.
fn handshake_status_is_transient(r: &HttpResponse) -> bool {
    match r.status {
        429 | 500..=599 => true,
        401 => matches!(error_code(r).as_deref(), Some("invalid_proof") | Some("rate_limited")),
        _ => false,
    }
}

/// `handshake_with` with an explicit time budget for the HTTP exchange (the start path bounds the whole
/// verification with a deadline).
pub fn handshake_budgeted(
    port: u16,
    secret: &Secret,
    client_nonce: &str,
    budget: Duration,
) -> Result<String, CapabilityError> {
    let body = format!("{{\"client_nonce\":\"{client_nonce}\"}}");
    let r = http_request(port, "POST", "/api/local/handshake", &[], Some(&body), budget)?;
    if r.status != 200 {
        if handshake_status_is_transient(&r) {
            return Err(CapabilityError::Transient(r.status));
        }
        return Err(CapabilityError::Untrusted("handshake endpoint refused or absent"));
    }
    let m = json_body(&r)?;
    if m.get("proto").and_then(Value::as_str) != Some(PROTO) {
        return Err(CapabilityError::Untrusted("wrong protocol"));
    }
    let got = m.get("proof").and_then(Value::as_str).ok_or(CapabilityError::Untrusted("no proof"))?;
    let server_nonce = m
        .get("server_nonce")
        .and_then(Value::as_str)
        .filter(|s| is_b64url(s, 22, 86))
        .ok_or(CapabilityError::Untrusted("bad server_nonce"))?;
    let want = handshake_proof(secret, port, client_nonce);
    if !ct_eq(want.as_bytes(), got.as_bytes()) {
        return Err(CapabilityError::ProofMismatch);
    }
    Ok(server_nonce.to_string())
}

/// Why one mint attempt did not give a code.
#[derive(Debug, PartialEq, Eq)]
pub enum MintError {
    /// 429 (`Retry-After` seconds when present) or 401 `invalid_proof` (`None`): the challenge expired or
    /// was evicted, or the budget is spent. Transient (docs/LOCAL_API_SECURITY.md 4.7): never evidence of
    /// an attack and never a reason to stop the sidecar.
    Busy(Option<u64>, u16),
    /// Could not reach the sidecar / the exchange broke off. Retryable.
    Unreachable(String),
    /// Any other answer after a verified handshake (not transient, not proof of a foreign peer).
    Refused(&'static str),
}

/// POST /api/local/launch-code with the proof for `server_nonce` (from a verified handshake).
pub fn mint_launch_code(port: u16, secret: &Secret, server_nonce: &str) -> Result<LaunchCode, MintError> {
    let p = mint_proof(secret, port, server_nonce);
    let body = format!("{{\"server_nonce\":\"{server_nonce}\"}}");
    let r = match http_request(port, "POST", "/api/local/launch-code", &[("X-Launcher-Proof", &p)], Some(&body), HTTP_TIMEOUT) {
        Ok(r) => r,
        Err(HttpError::Io(e)) => return Err(MintError::Unreachable(e)),
        Err(HttpError::BadResponse(w)) => return Err(MintError::Refused(w)),
    };
    match r.status {
        200 => {}
        429 => {
            let ra = r.header("Retry-After").and_then(|v| v.trim().parse::<u64>().ok());
            return Err(MintError::Busy(ra, 429));
        }
        401 if error_code(&r).as_deref() == Some("invalid_proof") => return Err(MintError::Busy(None, 401)),
        _ => return Err(MintError::Refused("launch code refused")),
    }
    let m = serde_json::from_slice::<Value>(&r.body)
        .ok()
        .and_then(|v| v.as_object().cloned())
        .ok_or(MintError::Refused("answer is not a JSON object"))?;
    let code = m
        .get("launch_code")
        .and_then(Value::as_str)
        .filter(|c| is_b64url(c, 22, 86))
        .ok_or(MintError::Refused("bad launch_code"))?;
    Ok(LaunchCode(code.to_string()))
}

fn error_code(r: &HttpResponse) -> Option<String> {
    let v: Value = serde_json::from_slice(&r.body).ok()?;
    v.get("error")?.get("code")?.as_str().map(str::to_string)
}

/// Outcome of "get me a fresh launch code" (an Open click).
#[derive(Debug, PartialEq, Eq)]
pub enum OpenError {
    /// The handshake proof did not verify (or the peer is not the sidecar): final for that process. The
    /// only case in which the caller may stop what it spawned.
    Untrusted(CapabilityError),
    /// Three attempts hit the transient conditions of 4.7 ("the local service is busy").
    Busy,
    /// The sidecar could not be reached on the last attempt.
    Unreachable(String),
    /// Verified peer, unexpected answer to the mint.
    Refused(&'static str),
}

impl fmt::Display for OpenError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            OpenError::Untrusted(e) => write!(f, "{e}"),
            OpenError::Busy => f.write_str("the local service is busy"),
            OpenError::Unreachable(e) => write!(f, "the control plane did not answer ({e})"),
            OpenError::Refused(w) => write!(f, "the control plane refused the request ({w})"),
        }
    }
}

/// At most this many (handshake + mint) attempts per user action (4.7).
pub const MAX_OPEN_ATTEMPTS: usize = 3;

/// Verified handshake, then a fresh launch code: what every "open" does (the 60 s lifetime means a code
/// is never kept). Follows 4.7: after a VERIFIED handshake, a 429 or `401 invalid_proof` on the mint (and a
/// network error) is retried with a whole new handshake, waiting min(Retry-After, 5) s (429) or
/// 0.25/0.5/1 s (plain 401, network error), at most 3 attempts; then `Busy`/`Unreachable`. A handshake
/// whose proof does not verify is final (`Untrusted`).
pub fn fresh_launch_code(port: u16, secret: &Secret) -> Result<LaunchCode, OpenError> {
    fresh_launch_code_with(port, secret, MAX_OPEN_ATTEMPTS, &std::thread::sleep)
}

pub fn fresh_launch_code_with(
    port: u16,
    secret: &Secret,
    attempts: usize,
    sleep: &dyn Fn(Duration),
) -> Result<LaunchCode, OpenError> {
    let backoff = |i: usize| Duration::from_millis(250u64 << i.min(2));
    let mut last = OpenError::Busy;
    for i in 0..attempts {
        let server_nonce = match handshake(port, secret) {
            Ok(n) => n,
            Err(CapabilityError::Unreachable(e)) => {
                last = OpenError::Unreachable(e);
                if i + 1 < attempts {
                    sleep(backoff(i));
                }
                continue;
            }
            Err(CapabilityError::Transient(_)) => {
                // 429/5xx on the handshake: a busy sidecar, not a foreign one (same rule as the mint)
                last = OpenError::Busy;
                if i + 1 < attempts {
                    sleep(backoff(i));
                }
                continue;
            }
            Err(e) => return Err(OpenError::Untrusted(e)),
        };
        match mint_launch_code(port, secret, &server_nonce) {
            Ok(code) => return Ok(code),
            Err(MintError::Busy(ra, _status)) => {
                last = OpenError::Busy;
                if i + 1 < attempts {
                    sleep(match ra {
                        Some(s) => Duration::from_secs(s.min(5)),
                        None => backoff(i),
                    });
                }
            }
            Err(MintError::Unreachable(e)) => {
                last = OpenError::Unreachable(e);
                if i + 1 < attempts {
                    sleep(backoff(i));
                }
            }
            Err(MintError::Refused(w)) => return Err(OpenError::Refused(w)),
        }
    }
    Err(last)
}

/// 4.3: the code travels only in the fragment, which the browser never sends to a server.
pub fn launch_url(port: u16, base_path: &str, code: &LaunchCode) -> String {
    format!("http://127.0.0.1:{port}{base_path}/#lc={}", code.expose())
}

/// The URL without any code (what the UI may show).
pub fn plain_url(port: u16, base_path: &str) -> String {
    format!("http://127.0.0.1:{port}{base_path}")
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::io::Cursor;
    use std::net::TcpListener;
    use std::thread;

    pub fn vector_key() -> Secret {
        let mut k = [0u8; 32];
        for (i, b) in k.iter_mut().enumerate() {
            *b = i as u8;
        }
        Secret::from_bytes(k)
    }

    // --- pinned vectors from docs/LOCAL_API_SECURITY.md section 4.4 ---------------------------

    #[test]
    fn pinned_handshake_vector() {
        assert_eq!(
            handshake_proof(&vector_key(), 8787, "AAAAAAAAAAAAAAAAAAAAAA"),
            "XrELUoD7WHe1e8IgKZZYoltOOKdPcPN3PR-qWdhqCeY"
        );
    }

    #[test]
    fn pinned_mint_vector() {
        assert_eq!(
            mint_proof(&vector_key(), 8787, "BBBBBBBBBBBBBBBBBBBBBB"),
            "JYxsI49qlI_AzpnZPgZwDLjyQLNRgueAarKpIxB19VQ"
        );
    }

    #[test]
    fn proofs_depend_on_context_port_nonce_and_key() {
        let k = vector_key();
        let base = handshake_proof(&k, 8787, "AAAAAAAAAAAAAAAAAAAAAA");
        assert_ne!(base, mint_proof(&k, 8787, "AAAAAAAAAAAAAAAAAAAAAA"), "context is bound");
        assert_ne!(base, handshake_proof(&k, 8788, "AAAAAAAAAAAAAAAAAAAAAA"), "port is bound");
        assert_ne!(base, handshake_proof(&k, 8787, "AAAAAAAAAAAAAAAAAAAAAB"), "nonce is bound");
        assert_ne!(base, handshake_proof(&Secret::from_bytes([7; 32]), 8787, "AAAAAAAAAAAAAAAAAAAAAA"));
    }

    // --- HMAC and base64url primitives (RFC 4231 cases 1 and 6) --------------------------------

    fn hex(b: &[u8]) -> String {
        b.iter().map(|x| format!("{x:02x}")).collect()
    }

    #[test]
    fn hmac_rfc4231_case_1() {
        assert_eq!(
            hex(&hmac_sha256(&[0x0b; 20], b"Hi There")),
            "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"
        );
    }

    #[test]
    fn hmac_rfc4231_case_6_key_longer_than_block() {
        assert_eq!(
            hex(&hmac_sha256(&[0xaa; 131], b"Test Using Larger Than Block-Size Key - Hash Key First")),
            "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54"
        );
    }

    #[test]
    fn b64url_roundtrip_and_strictness() {
        for n in 0..70usize {
            let data: Vec<u8> = (0..n).map(|i| (i * 7 + 3) as u8).collect();
            let e = b64url_encode(&data);
            assert!(!e.contains('=') && !e.contains('+') && !e.contains('/'));
            assert_eq!(b64url_decode(&e).as_deref(), Some(&data[..]));
        }
        assert_eq!(b64url_decode("AAAA="), None, "padding refused");
        assert_eq!(b64url_decode("AA+A"), None, "standard alphabet refused");
        assert_eq!(b64url_decode("AAAAA"), None, "impossible length");
        assert_eq!(b64url_decode("AB"), None, "non-canonical trailing bits");
        assert_eq!(b64url_encode(&[0xfb, 0xff]), "-_8");
    }

    #[test]
    fn ct_eq_basics() {
        assert!(ct_eq(b"abc", b"abc"));
        assert!(!ct_eq(b"abc", b"abd"));
        assert!(!ct_eq(b"abc", b"abcd"));
    }

    #[test]
    fn secret_and_code_never_print() {
        let s = format!("{:?}", vector_key());
        assert!(s.contains("redacted") && !s.contains("0, 1, 2"));
        assert!(format!("{:?}", LaunchCode("secretcode".into())).contains("redacted"));
        let h = parse_handoff_line(ready_line("127.0.0.1", 5000, "").as_bytes()).unwrap();
        assert!(!format!("{h:?}").contains(&ok_secret()));
    }

    // --- hand-off parser ----------------------------------------------------------------------

    fn ok_secret() -> String {
        b64url_encode(&vector_key().0)
    }

    fn ready_line(host: &str, port: u64, base: &str) -> String {
        format!(
            "{{\"v\":1,\"proto\":\"{PROTO}\",\"event\":\"ready\",\"host\":\"{host}\",\"port\":{port},\"pid\":4242,\
             \"base_path\":\"{base}\",\"secret\":\"{}\",\"launch_code\":\"{}\",\"launch_code_ttl\":60}}",
            ok_secret(),
            "L".repeat(43)
        )
    }

    #[test]
    fn handoff_ready_line_parses() {
        let h = parse_handoff_line(ready_line("127.0.0.1", 50123, "").as_bytes()).unwrap();
        assert_eq!((h.port, h.base_path.as_str()), (50123, ""));
        assert_eq!(h.secret.0, vector_key().0);
        let h = parse_handoff_line(ready_line("127.0.0.1", 1, "/app/x").as_bytes()).unwrap();
        assert_eq!(h.base_path, "/app/x");
    }

    #[test]
    fn handoff_tolerates_a_windows_carriage_return() {
        let mut l = ready_line("127.0.0.1", 50123, "").into_bytes();
        l.push(b'\r');
        assert!(parse_handoff_line(&l).is_ok());
    }

    #[test]
    fn handoff_bind_failed_carries_no_secret_and_is_reported() {
        let l = br#"{"v":1,"event":"error","code":"bind_failed","detail":"[Errno 48] Address already in use"}"#;
        assert_eq!(
            parse_handoff_line(l).unwrap_err(),
            HandoffError::BindFailed("[Errno 48] Address already in use".into())
        );
        let l = "{\"v\":1,\"event\":\"error\",\"code\":\"x\",\"detail\":\"a\\u0007b\"}";
        assert_eq!(parse_handoff_line(l.as_bytes()).unwrap_err(), HandoffError::Rejected("x: ab".into()), "control chars dropped");
    }

    #[test]
    fn handoff_rejects_malformed_and_foreign_records_without_echoing_them() {
        let cases: Vec<(String, &str)> = vec![
            ("not json".into(), "not JSON"),
            ("[1,2]".into(), "not an object"),
            ("{}".into(), "unsupported version"),
            (ready_line("127.0.0.1", 5000, "").replace("\"v\":1", "\"v\":2"), "unsupported version"),
            (ready_line("127.0.0.1", 5000, "").replace(PROTO, "other/1"), "unknown protocol"),
            (ready_line("0.0.0.0", 5000, ""), "host is not 127.0.0.1"),
            (ready_line("127.0.0.1", 0, ""), "bad port"),
            (ready_line("127.0.0.1", 70000, ""), "bad port"),
            (ready_line("127.0.0.1", 5000, "/a b"), "bad base_path"),
            (ready_line("127.0.0.1", 5000, "/a//b"), "bad base_path"),
            (ready_line("127.0.0.1", 5000, "").replace("\"event\":\"ready\"", "\"event\":\"hello\""), "unknown event"),
            (ready_line("127.0.0.1", 5000, "").replace(&ok_secret(), "short"), "bad secret"),
            (ready_line("127.0.0.1", 5000, "").replace(&ok_secret(), &"A".repeat(44)), "bad secret"),
        ];
        for (line, why) in cases {
            match parse_handoff_line(line.as_bytes()) {
                Err(HandoffError::Malformed(w)) => {
                    assert_eq!(w, why, "{why}");
                    assert!(!w.contains(&ok_secret()));
                }
                other => panic!("expected Malformed({why}), got {other:?}"),
            }
        }
        let mut big = ready_line("127.0.0.1", 5000, "");
        big.push_str(&" ".repeat(MAX_HANDOFF_LINE));
        assert_eq!(parse_handoff_line(big.as_bytes()).unwrap_err(), HandoffError::TooLong);
    }

    #[test]
    fn read_handoff_returns_the_first_line_then_drains() {
        let mut data = ready_line("127.0.0.1", 50123, "");
        data.push('\n');
        data.push_str("later output that must be ignored\n");
        let h = read_handoff(Cursor::new(data.into_bytes()), Duration::from_secs(2)).unwrap();
        assert_eq!(h.port, 50123);
    }

    #[test]
    fn read_handoff_reports_eof_oversize_and_timeout() {
        assert_eq!(read_handoff(Cursor::new(Vec::new()), Duration::from_secs(2)).unwrap_err(), HandoffError::Eof);
        assert_eq!(
            read_handoff(Cursor::new(b"no newline yet".to_vec()), Duration::from_secs(2)).unwrap_err(),
            HandoffError::Eof
        );
        let big = vec![b'x'; MAX_HANDOFF_LINE * 3];
        assert_eq!(read_handoff(Cursor::new(big), Duration::from_secs(2)).unwrap_err(), HandoffError::TooLong);
        // A reader that never produces anything.
        struct Never;
        impl Read for Never {
            fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
                thread::sleep(Duration::from_millis(400));
                Ok(0)
            }
        }
        assert_eq!(read_handoff(Never, Duration::from_millis(100)).unwrap_err(), HandoffError::Timeout);
    }

    // --- handshake against mock servers (client side behaviour) --------------------------------

    /// Serve one connection with a fixed raw response; returns the port.
    fn one_shot_server(response: Vec<u8>) -> u16 {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        thread::spawn(move || {
            if let Ok((mut s, _)) = l.accept() {
                let mut buf = [0u8; 4096];
                let _ = s.read(&mut buf);
                let _ = s.write_all(&response);
            }
        });
        port
    }

    fn json_200(body: &str) -> Vec<u8> {
        format!("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len())
            .into_bytes()
    }

    /// A faithful sidecar side of 4.2 for one connection, to prove the client accepts a correct answer.
    fn honest_server(secret: Secret) -> u16 {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        thread::spawn(move || {
            if let Ok((mut s, _)) = l.accept() {
                let mut buf = [0u8; 4096];
                let n = s.read(&mut buf).unwrap_or(0);
                let req = String::from_utf8_lossy(&buf[..n]).to_string();
                assert!(req.starts_with("POST /api/local/handshake HTTP/1.1\r\n"));
                assert!(req.contains(&format!("Host: 127.0.0.1:{port}\r\n")));
                assert!(!req.to_ascii_lowercase().contains("origin:"));
                let nonce = req.split("\"client_nonce\":\"").nth(1).unwrap().split('"').next().unwrap().to_string();
                assert_eq!(nonce.len(), 22, "fresh 16-byte nonce, 22 base64url chars");
                let body = format!(
                    "{{\"v\":1,\"proto\":\"{PROTO}\",\"proof\":\"{}\",\"server_nonce\":\"{}\",\"expires_in\":30}}",
                    handshake_proof(&secret, port, &nonce),
                    "S".repeat(22)
                );
                let _ = s.write_all(&json_200(&body));
            }
        });
        port
    }

    #[test]
    fn handshake_accepts_a_correct_proof() {
        let port = honest_server(vector_key());
        assert_eq!(handshake(port, &vector_key()).unwrap(), "S".repeat(22));
    }

    #[test]
    fn handshake_refuses_a_proof_made_with_another_secret() {
        let port = honest_server(Secret::from_bytes([9; 32]));
        assert_eq!(handshake(port, &vector_key()), Err(CapabilityError::ProofMismatch));
    }

    #[test]
    fn handshake_refuses_a_stale_sidecar_without_the_endpoint() {
        // v0.3.2 answered unknown POSTs with 404/405 (or 401 behind the gate without a session).
        for status in ["404 Not Found", "405 Method Not Allowed", "401 Unauthorized", "403 Forbidden", "301 Moved Permanently"] {
            let body = "{\"detail\":\"Not Found\"}";
            let resp = format!("HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
            let port = one_shot_server(resp.into_bytes());
            assert_eq!(
                handshake(port, &vector_key()),
                Err(CapabilityError::Untrusted("handshake endpoint refused or absent")),
                "{status}"
            );
        }
    }

    #[test]
    fn handshake_refuses_foreign_listeners() {
        let secret = vector_key();
        // a 200 that is not the protocol
        let port = one_shot_server(json_200("{\"hello\":\"world\"}"));
        assert!(matches!(handshake(port, &secret), Err(CapabilityError::Untrusted(_))));
        // right protocol, no proof
        let port = one_shot_server(json_200(&format!("{{\"proto\":\"{PROTO}\",\"server_nonce\":\"{}\"}}", "S".repeat(22))));
        assert_eq!(handshake(port, &secret), Err(CapabilityError::Untrusted("no proof")));
        // right protocol, a constant proof
        let port = one_shot_server(json_200(&format!(
            "{{\"proto\":\"{PROTO}\",\"proof\":\"{}\",\"server_nonce\":\"{}\"}}",
            "A".repeat(43),
            "S".repeat(22)
        )));
        assert_eq!(handshake(port, &secret), Err(CapabilityError::ProofMismatch));
        // not JSON / not HTTP / closes immediately
        assert!(matches!(handshake(one_shot_server(json_200("<html>")), &secret), Err(CapabilityError::Untrusted(_))));
        assert!(handshake(one_shot_server(b"SSH-2.0-OpenSSH_9\r\n".to_vec()), &secret).is_err());
        assert!(handshake(one_shot_server(Vec::new()), &secret).is_err());
        // oversized answer
        let big = format!("HTTP/1.1 200 OK\r\nContent-Length: 99999\r\n\r\n{}", "x".repeat(40_000));
        assert!(handshake(one_shot_server(big.into_bytes()), &secret).is_err());
    }

    #[test]
    fn handshake_reports_nothing_listening_and_a_silent_peer() {
        let port = {
            let l = TcpListener::bind("127.0.0.1:0").unwrap();
            l.local_addr().unwrap().port()
        };
        assert!(matches!(handshake(port, &vector_key()), Err(CapabilityError::Unreachable(_))));
        // accepts but never answers: bounded by the budget, not hanging
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        let t = thread::spawn(move || {
            let _c = l.accept();
            thread::sleep(Duration::from_secs(7));
        });
        let t0 = Instant::now();
        assert!(matches!(handshake(port, &vector_key()), Err(CapabilityError::Unreachable(_))));
        assert!(t0.elapsed() < Duration::from_secs(7));
        drop(t);
    }

    // --- SR-03: bounded retry on the mint (docs/LOCAL_API_SECURITY.md 4.7) ----------------------

    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    #[derive(Clone)]
    enum Mint {
        Code,
        Status(u16, &'static str, Option<u64>), // status, error code, Retry-After
        Drop,                                    // close without answering
    }

    struct Scripted {
        port: u16,
        handshakes: Arc<AtomicUsize>,
        mints: Arc<AtomicUsize>,
        stop: Arc<AtomicBool>,
    }
    impl Drop for Scripted {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::SeqCst);
        }
    }

    /// A sidecar double: honest handshake (HMAC with `secret`), scripted answers to the mint.
    fn scripted_sidecar(secret: Secret, handshake_ok: bool, script: Vec<Mint>) -> Scripted {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        l.set_nonblocking(true).unwrap();
        let port = l.local_addr().unwrap().port();
        let (hs, mi, stop) = (Arc::new(AtomicUsize::new(0)), Arc::new(AtomicUsize::new(0)), Arc::new(AtomicBool::new(false)));
        let (hs2, mi2, stop2) = (hs.clone(), mi.clone(), stop.clone());
        let script = Arc::new(Mutex::new(script.into_iter().collect::<std::collections::VecDeque<_>>()));
        thread::spawn(move || {
            while !stop2.load(Ordering::SeqCst) {
                let Ok((mut s, _)) = l.accept() else {
                    thread::sleep(Duration::from_millis(5));
                    continue;
                };
                s.set_nonblocking(false).ok();
                let mut buf = [0u8; 4096];
                let n = s.read(&mut buf).unwrap_or(0);
                let req = String::from_utf8_lossy(&buf[..n]).to_string();
                if req.starts_with("POST /api/local/handshake") {
                    hs2.fetch_add(1, Ordering::SeqCst);
                    let nonce = req.split("\"client_nonce\":\"").nth(1).unwrap().split('"').next().unwrap().to_string();
                    let proof = if handshake_ok {
                        handshake_proof(&secret, port, &nonce)
                    } else {
                        "A".repeat(43)
                    };
                    let body = format!(
                        "{{\"v\":1,\"proto\":\"{PROTO}\",\"proof\":\"{proof}\",\"server_nonce\":\"{}\",\"expires_in\":30}}",
                        "S".repeat(22)
                    );
                    let _ = s.write_all(&json_200(&body));
                } else if req.starts_with("POST /api/local/launch-code") {
                    mi2.fetch_add(1, Ordering::SeqCst);
                    let step = script.lock().unwrap().pop_front().unwrap_or(Mint::Code);
                    match step {
                        Mint::Code => {
                            let body = format!("{{\"v\":1,\"launch_code\":\"{}\",\"expires_in\":60}}", "C".repeat(43));
                            let _ = s.write_all(&json_200(&body));
                        }
                        Mint::Status(st, code, ra) => {
                            let body = format!("{{\"error\":{{\"code\":\"{code}\",\"message\":\"x\"}}}}");
                            let ra = ra.map(|r| format!("Retry-After: {r}\r\n")).unwrap_or_default();
                            let _ = s.write_all(
                                format!("HTTP/1.1 {st} X\r\n{ra}Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len())
                                    .as_bytes(),
                            );
                        }
                        Mint::Drop => {}
                    }
                }
            }
        });
        Scripted { port, handshakes: hs, mints: mi, stop }
    }

    fn run_open(sc: &Scripted) -> (Result<LaunchCode, OpenError>, Vec<Duration>) {
        let sleeps = Mutex::new(Vec::new());
        let r = fresh_launch_code_with(sc.port, &vector_key(), MAX_OPEN_ATTEMPTS, &|d| sleeps.lock().unwrap().push(d));
        (r, sleeps.into_inner().unwrap())
    }

    const BUSY401: Mint = Mint::Status(401, "invalid_proof", None);

    #[test]
    fn flood_scenario_evicted_challenge_then_success_never_kills_anything() {
        // the review's flood: the mint answers 401 invalid_proof (challenge evicted) twice, then works
        let sc = scripted_sidecar(vector_key(), true, vec![BUSY401, BUSY401, Mint::Code]);
        let (r, sleeps) = run_open(&sc);
        assert!(r.is_ok(), "{r:?}");
        assert_eq!(sc.handshakes.load(Ordering::SeqCst), 3, "a whole fresh handshake per attempt");
        assert_eq!(sc.mints.load(Ordering::SeqCst), 3);
        assert_eq!(sleeps, vec![Duration::from_millis(250), Duration::from_millis(500)]);
    }

    #[test]
    fn rate_limited_then_ok_waits_retry_after_capped_at_5s() {
        let sc = scripted_sidecar(vector_key(), true, vec![Mint::Status(429, "rate_limited", Some(2)), Mint::Status(429, "rate_limited", Some(60)), Mint::Code]);
        let (r, sleeps) = run_open(&sc);
        assert!(r.is_ok(), "{r:?}");
        assert_eq!(sleeps, vec![Duration::from_secs(2), Duration::from_secs(5)], "min(Retry-After, 5)");
    }

    #[test]
    fn three_busy_answers_report_busy_and_make_no_fourth_attempt() {
        for m in [BUSY401, Mint::Status(429, "rate_limited", Some(1))] {
            let sc = scripted_sidecar(vector_key(), true, vec![m.clone(), m.clone(), m.clone(), Mint::Code]);
            let (r, sleeps) = run_open(&sc);
            assert_eq!(r.unwrap_err(), OpenError::Busy, "reported as busy, not as an untrusted sidecar");
            assert_eq!(sc.mints.load(Ordering::SeqCst), 3, "at most 3 attempts");
            assert_eq!(sleeps.len(), 2, "no wait after the last attempt");
        }
    }

    #[test]
    fn network_errors_during_the_retry_are_retryable() {
        let sc = scripted_sidecar(vector_key(), true, vec![Mint::Drop, BUSY401, Mint::Code]);
        let (r, _) = run_open(&sc);
        assert!(r.is_ok(), "{r:?}");
        // and a peer that keeps dropping ends as Unreachable, never Untrusted
        let sc = scripted_sidecar(vector_key(), true, vec![Mint::Drop, Mint::Drop, Mint::Drop]);
        let (r, _) = run_open(&sc);
        assert!(matches!(r, Err(OpenError::Unreachable(_))), "{r:?}");
        // nothing listening at all: retried, then Unreachable
        let port = { TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port() };
        let slept = Mutex::new(0usize);
        let r = fresh_launch_code_with(port, &vector_key(), 3, &|_| *slept.lock().unwrap() += 1);
        assert!(matches!(r, Err(OpenError::Unreachable(_))), "{r:?}");
        assert_eq!(*slept.lock().unwrap(), 2);
    }

    #[test]
    fn a_failed_handshake_proof_stays_final_and_never_mints() {
        let sc = scripted_sidecar(vector_key(), false, vec![]);
        let (r, sleeps) = run_open(&sc);
        assert_eq!(r.unwrap_err(), OpenError::Untrusted(CapabilityError::ProofMismatch));
        assert_eq!(sc.handshakes.load(Ordering::SeqCst), 1, "no retry of a failed proof");
        assert_eq!(sc.mints.load(Ordering::SeqCst), 0, "no mint call to an unverified peer");
        assert!(sleeps.is_empty());
    }

    #[test]
    fn other_mint_answers_after_a_verified_handshake_are_refused_without_retry_or_distrust() {
        for st in [403u16, 404, 500, 415] {
            let sc = scripted_sidecar(vector_key(), true, vec![Mint::Status(st, "whatever", None), Mint::Code]);
            let (r, _) = run_open(&sc);
            assert_eq!(r.unwrap_err(), OpenError::Refused("launch code refused"), "{st}");
            assert_eq!(sc.mints.load(Ordering::SeqCst), 1);
        }
        // 401 with another error code is not "busy" either
        let sc = scripted_sidecar(vector_key(), true, vec![Mint::Status(401, "auth_required", None), Mint::Code]);
        assert_eq!(run_open(&sc).0.unwrap_err(), OpenError::Refused("launch code refused"));
    }

    #[test]
    fn launch_url_puts_the_code_in_the_fragment_only() {
        let code = LaunchCode("c".repeat(43));
        assert_eq!(launch_url(50123, "", &code), format!("http://127.0.0.1:50123/#lc={}", "c".repeat(43)));
        assert_eq!(launch_url(50123, "/app", &code), format!("http://127.0.0.1:50123/app/#lc={}", "c".repeat(43)));
        assert_eq!(plain_url(50123, "/app"), "http://127.0.0.1:50123/app");
        let u = launch_url(1, "", &code);
        assert_eq!(u.matches('#').count(), 1);
        assert!(!u.split('#').next().unwrap().contains("lc="), "nothing code-like before the fragment");
    }
}
