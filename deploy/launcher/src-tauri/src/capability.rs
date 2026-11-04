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
// Secrecy rules enforced here: the secret, the launch code and the cookie are never written to a log,
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
    #[allow(dead_code)] // read by the integration tests (Set-Cookie)
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
            Ok(0) => break,
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
    /// Only from `mint_launch_code`, i.e. AFTER a verified handshake: the sidecar refused the mint in a way
    /// that is worth another attempt (429, or 401 `invalid_proof`: expired/evicted challenge). Section 4.7.
    /// Neither evidence of an attack nor of trust. `fresh_launch_code` turns it into a bounded retry.
    Retry(MintRetry),
    /// Every bounded attempt of 4.7 failed: the local service is busy. The browser is not opened and the
    /// sidecar is NOT stopped or declared untrusted.
    Busy,
}

/// Why a mint was refused with a retryable answer (docs/LOCAL_API_SECURITY.md 4.7).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MintRetry {
    /// `429`; the `Retry-After` seconds, if the header was a plain integer.
    RateLimited(Option<u64>),
    /// `401` with `error.code == "invalid_proof"` (challenge unknown/expired/evicted).
    InvalidProof,
}

impl fmt::Display for CapabilityError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CapabilityError::Unreachable(e) => write!(f, "the control plane did not answer ({e})"),
            CapabilityError::Untrusted(why) => write!(f, "the listener did not behave as this launch's control plane ({why})"),
            CapabilityError::ProofMismatch => f.write_str("the listener could not prove it is this launch's control plane"),
            CapabilityError::Retry(_) => f.write_str("the control plane asked for a retry"),
            CapabilityError::Busy => f.write_str("the local service is busy"),
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

fn fresh_nonce() -> Result<String, CapabilityError> {
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
    let body = format!("{{\"client_nonce\":\"{client_nonce}\"}}");
    let r = http_request(port, "POST", "/api/local/handshake", &[], Some(&body), HTTP_TIMEOUT)?;
    if r.status != 200 {
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

/// POST /api/local/launch-code with the proof for `server_nonce` (from a verified handshake).
pub fn mint_launch_code(port: u16, secret: &Secret, server_nonce: &str) -> Result<LaunchCode, CapabilityError> {
    let p = mint_proof(secret, port, server_nonce);
    let body = format!("{{\"server_nonce\":\"{server_nonce}\"}}");
    let r = http_request(port, "POST", "/api/local/launch-code", &[("X-Launcher-Proof", &p)], Some(&body), HTTP_TIMEOUT)?;
    if r.status != 200 {
        return Err(match mint_refusal(&r) {
            Some(retry) => CapabilityError::Retry(retry),
            None => CapabilityError::Untrusted("launch code refused"),
        });
    }
    let m = json_body(&r)?;
    let code = m
        .get("launch_code")
        .and_then(Value::as_str)
        .filter(|c| is_b64url(c, 22, 86))
        .ok_or(CapabilityError::Untrusted("bad launch_code"))?;
    Ok(LaunchCode(code.to_string()))
}

/// Classify a non-200 mint answer (the handshake already verified the peer): `429`, or `401` whose JSON
/// `error.code` is `invalid_proof`, are retryable (4.7); anything else is a refusal.
fn mint_refusal(r: &HttpResponse) -> Option<MintRetry> {
    match r.status {
        429 => Some(MintRetry::RateLimited(r.header("retry-after").and_then(|v| v.trim().parse::<u64>().ok()))),
        401 => {
            let v: Value = serde_json::from_slice(&r.body).ok()?;
            (v.get("error")?.get("code")?.as_str()? == "invalid_proof").then_some(MintRetry::InvalidProof)
        }
        _ => None,
    }
}

/// 4.7: at most this many attempts (each = a fresh verified handshake + one mint) per user action.
pub const MINT_ATTEMPTS: u32 = 3;
/// 4.7: never wait longer than this for a `Retry-After`.
pub const MAX_RETRY_WAIT: Duration = Duration::from_secs(5);
/// 4.7: wait before the next attempt (`attempt` counts from 1 = the attempt that just failed).
fn retry_wait(why: MintRetry, attempt: u32) -> Duration {
    match why {
        MintRetry::RateLimited(Some(s)) => Duration::from_secs(s).min(MAX_RETRY_WAIT),
        MintRetry::RateLimited(None) => Duration::from_secs(1),
        MintRetry::InvalidProof => Duration::from_millis(250u64 << (attempt - 1).min(2)), // 0.25 s, 0.5 s, 1 s
    }
}

/// Verified handshake, then a fresh launch code: what every "open" does (the 60 s lifetime means a
/// code is never kept). Bounded retry per docs/LOCAL_API_SECURITY.md 4.7:
///  * a handshake that fails in ANY way is final (a failed proof is the stale/foreign-process case; no retry);
///  * after a VERIFIED handshake, a 429 or `401 invalid_proof` on the mint waits min(Retry-After, 5) s (or
///    0.25/0.5/1 s) and redoes handshake + mint, up to `MINT_ATTEMPTS` tries, then `Busy`;
///  * every other mint answer is refused at once.
pub fn fresh_launch_code(port: u16, secret: &Secret) -> Result<LaunchCode, CapabilityError> {
    fresh_launch_code_with(port, secret, &mut |d| std::thread::sleep(d))
}

/// `fresh_launch_code` with an injectable sleeper (tests).
pub fn fresh_launch_code_with(
    port: u16,
    secret: &Secret,
    sleep: &mut dyn FnMut(Duration),
) -> Result<LaunchCode, CapabilityError> {
    let mut attempt = 1;
    loop {
        let server_nonce = handshake(port, secret)?;
        match mint_launch_code(port, secret, &server_nonce) {
            Err(CapabilityError::Retry(why)) => {
                if attempt >= MINT_ATTEMPTS {
                    return Err(CapabilityError::Busy);
                }
                sleep(retry_wait(why, attempt));
                attempt += 1;
            }
            other => return other,
        }
    }
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
        for status in ["404 Not Found", "405 Method Not Allowed", "401 Unauthorized", "500 Internal Server Error"] {
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

    // --- 4.7: bounded retry of the mint after a VERIFIED handshake -----------------------------------

    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    #[derive(Default)]
    struct Seen {
        handshakes: AtomicUsize,
        mints: AtomicUsize,
        client_nonces: Mutex<Vec<String>>,
    }

    fn mint_answer(status: &str, extra_header: &str, body: &str) -> Vec<u8> {
        format!(
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\n{extra_header}Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
        .into_bytes()
    }

    fn err_body(code: &str) -> String {
        format!("{{\"error\":{{\"code\":\"{code}\",\"message\":\"x\"}}}}")
    }

    fn ok_mint() -> Vec<u8> {
        json_200(&format!("{{\"v\":1,\"launch_code\":\"{}\",\"expires_in\":60}}", "L".repeat(43)))
    }

    /// A scripted sidecar (holding the pinned vector key): serves exactly `connections` sequential connections.
    /// Handshakes are honest unless `other_secret_from_handshake` = Some(n) makes the n-th and later ones use
    /// another secret; mints pop `mints` in
    /// order after checking that the proof is correct for the challenge of the latest handshake.
    fn scripted_sidecar(
        mut mints: Vec<Vec<u8>>,
        connections: usize,
        other_secret_from_handshake: Option<usize>,
    ) -> (u16, Arc<Seen>) {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        let seen = Arc::new(Seen::default());
        let seen2 = seen.clone();
        mints.reverse();
        thread::spawn(move || {
            let secret = vector_key();
            let mut last_server_nonce = String::new();
            for _ in 0..connections {
                let Ok((mut s, _)) = l.accept() else { return };
                let mut buf = [0u8; 4096];
                let n = s.read(&mut buf).unwrap_or(0);
                let req = String::from_utf8_lossy(&buf[..n]).to_string();
                if req.starts_with("POST /api/local/handshake ") {
                    let k = seen2.handshakes.fetch_add(1, Ordering::SeqCst) + 1;
                    let nonce = req.split("\"client_nonce\":\"").nth(1).unwrap().split('"').next().unwrap().to_string();
                    seen2.client_nonces.lock().unwrap().push(nonce.clone());
                    last_server_nonce = format!("{k:0>22}");
                    let key = match other_secret_from_handshake {
                        Some(from) if k >= from => Secret::from_bytes([9; 32]),
                        _ => vector_key(),
                    };
                    let body = format!(
                        "{{\"v\":1,\"proto\":\"{PROTO}\",\"proof\":\"{}\",\"server_nonce\":\"{last_server_nonce}\",\"expires_in\":30}}",
                        handshake_proof(&key, port, &nonce)
                    );
                    let _ = s.write_all(&json_200(&body));
                } else if req.starts_with("POST /api/local/launch-code ") {
                    seen2.mints.fetch_add(1, Ordering::SeqCst);
                    // the mint must answer the challenge of the handshake just before it, with a correct proof
                    assert!(req.contains(&format!("\"server_nonce\":\"{last_server_nonce}\"")), "mint without a fresh handshake");
                    let want = mint_proof(&secret, port, &last_server_nonce);
                    assert!(req.contains(&format!("X-Launcher-Proof: {want}\r\n")), "wrong mint proof");
                    let _ = s.write_all(&mints.pop().expect("script exhausted"));
                } else {
                    let _ = s.write_all(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
                }
            }
        });
        (port, seen)
    }

    fn run_fresh(port: u16) -> (Result<LaunchCode, CapabilityError>, Vec<Duration>) {
        let mut slept = Vec::new();
        let r = fresh_launch_code_with(port, &vector_key(), &mut |d| slept.push(d));
        (r, slept)
    }

    #[test]
    fn a_429_after_a_verified_handshake_waits_retry_after_then_redoes_handshake_and_mint() {
        let mints = vec![
            mint_answer("429 Too Many Requests", "Retry-After: 2\r\n", &err_body("rate_limited")),
            ok_mint(),
        ];
        let (port, seen) = scripted_sidecar(mints, 4, None);
        let (r, slept) = run_fresh(port);
        assert_eq!(r.unwrap().expose(), "L".repeat(43));
        assert_eq!(slept, vec![Duration::from_secs(2)]);
        assert_eq!(seen.handshakes.load(Ordering::SeqCst), 2, "a fresh handshake before every mint");
        assert_eq!(seen.mints.load(Ordering::SeqCst), 2);
        let n = seen.client_nonces.lock().unwrap().clone();
        assert_ne!(n[0], n[1], "a new client_nonce for every handshake");
    }

    #[test]
    fn retry_after_is_capped_at_five_seconds_and_defaults_to_one() {
        let mints = vec![
            mint_answer("429 Too Many Requests", "Retry-After: 60\r\n", &err_body("rate_limited")),
            mint_answer("429 Too Many Requests", "", &err_body("rate_limited")),
            ok_mint(),
        ];
        let (port, _) = scripted_sidecar(mints, 6, None);
        let (r, slept) = run_fresh(port);
        assert!(r.is_ok());
        assert_eq!(slept, vec![Duration::from_secs(5), Duration::from_secs(1)]);
        // an unparsable Retry-After is treated as absent
        let mints = vec![mint_answer("429 Too Many Requests", "Retry-After: soon\r\n", "{}"), ok_mint()];
        let (port, _) = scripted_sidecar(mints, 4, None);
        assert_eq!(run_fresh(port).1, vec![Duration::from_secs(1)]);
    }

    #[test]
    fn invalid_proof_is_retried_with_short_backoff_then_the_launcher_reports_busy() {
        let inv = || mint_answer("401 Unauthorized", "", &err_body("invalid_proof"));
        let (port, seen) = scripted_sidecar(vec![inv(), inv(), inv()], 6, None);
        let (r, slept) = run_fresh(port);
        assert_eq!(r.unwrap_err(), CapabilityError::Busy);
        assert_eq!(slept, vec![Duration::from_millis(250), Duration::from_millis(500)], "no sleep after the last try");
        assert_eq!(seen.handshakes.load(Ordering::SeqCst), 3, "at most 3 attempts");
        assert_eq!(seen.mints.load(Ordering::SeqCst), 3);
        // Busy is not an identity verdict: main.rs stops the sidecar only for ProofMismatch / Untrusted
        assert!(!matches!(CapabilityError::Busy, CapabilityError::ProofMismatch | CapabilityError::Untrusted(_)));
        assert!(CapabilityError::Busy.to_string().contains("busy"));
    }

    #[test]
    fn three_429s_also_end_in_busy_after_two_bounded_waits() {
        let r429 = || mint_answer("429 Too Many Requests", "Retry-After: 9\r\n", &err_body("rate_limited"));
        let (port, seen) = scripted_sidecar(vec![r429(), r429(), r429()], 6, None);
        let (r, slept) = run_fresh(port);
        assert_eq!(r.unwrap_err(), CapabilityError::Busy);
        assert_eq!(slept, vec![MAX_RETRY_WAIT, MAX_RETRY_WAIT]);
        assert_eq!(seen.mints.load(Ordering::SeqCst), 3);
    }

    #[test]
    fn other_mint_refusals_are_not_retried_and_stay_untrusted() {
        for (status, body) in [
            ("401 Unauthorized", err_body("unauthorized")),   // 401 with another code
            ("401 Unauthorized", "not json".to_string()),
            ("403 Forbidden", err_body("forbidden")),
            ("400 Bad Request", err_body("bad_request")),
            ("500 Internal Server Error", err_body("invalid_proof")), // right code, wrong status
        ] {
            let (port, seen) = scripted_sidecar(vec![mint_answer(status, "", &body)], 2, None);
            let (r, slept) = run_fresh(port);
            assert_eq!(r.unwrap_err(), CapabilityError::Untrusted("launch code refused"), "{status} {body}");
            assert!(slept.is_empty());
            assert_eq!(seen.handshakes.load(Ordering::SeqCst), 1);
            assert_eq!(seen.mints.load(Ordering::SeqCst), 1, "no retry for {status}");
        }
    }

    #[test]
    fn a_failed_handshake_proof_is_final_even_in_the_middle_of_retries() {
        // attempt 1: honest handshake, mint answers invalid_proof; attempt 2: the "sidecar" now holds
        // another secret (a foreign process took the port): ProofMismatch, no further tries, no mint.
        let inv = mint_answer("401 Unauthorized", "", &err_body("invalid_proof"));
        let (port, seen) = scripted_sidecar(vec![inv], 3, Some(2));
        let (r, slept) = run_fresh(port);
        assert_eq!(r.unwrap_err(), CapabilityError::ProofMismatch);
        assert_eq!(slept, vec![Duration::from_millis(250)]);
        assert_eq!(seen.handshakes.load(Ordering::SeqCst), 2);
        assert_eq!(seen.mints.load(Ordering::SeqCst), 1, "never mint from a peer that failed the handshake");
    }

    #[test]
    fn a_handshake_that_never_verifies_is_never_retried() {
        let (port, seen) = scripted_sidecar(vec![], 2, Some(1));
        let (r, slept) = run_fresh(port);
        assert_eq!(r.unwrap_err(), CapabilityError::ProofMismatch);
        assert!(slept.is_empty());
        assert_eq!(seen.handshakes.load(Ordering::SeqCst), 1);
        assert_eq!(seen.mints.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn retry_wait_schedule() {
        assert_eq!(retry_wait(MintRetry::InvalidProof, 1), Duration::from_millis(250));
        assert_eq!(retry_wait(MintRetry::InvalidProof, 2), Duration::from_millis(500));
        assert_eq!(retry_wait(MintRetry::InvalidProof, 3), Duration::from_millis(1000));
        assert_eq!(retry_wait(MintRetry::InvalidProof, 9), Duration::from_millis(1000));
        assert_eq!(retry_wait(MintRetry::RateLimited(Some(0)), 1), Duration::ZERO);
        assert_eq!(retry_wait(MintRetry::RateLimited(Some(3)), 1), Duration::from_secs(3));
        assert_eq!(retry_wait(MintRetry::RateLimited(Some(u64::MAX)), 1), MAX_RETRY_WAIT);
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
