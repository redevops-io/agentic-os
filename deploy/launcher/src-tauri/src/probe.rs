//! Is the deployment reachable, does it accept us, and does it speak the API this shell was built for?
//! Four answers, the same words the web UI uses (see redevops-projects `classifyConnectionError`):
//! ok · unavailable (waiting may fix it) · unauthenticated (it refused us) · incompatible (too old / not ours).
//!
//! Every request has a hard timeout, redirects are never followed (a redirect to a login page is "unauthenticated",
//! and a credential must never be forwarded to a different host), and the bearer token is only ever attached to the
//! one configured origin. No text from the server is copied into a status — only fixed sentences.

use crate::secret::Secret;
use serde::Serialize;
use std::io::Read;
use std::time::Duration;
use url::Url;

pub const PROBE_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_BODY: u64 = 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum State {
    Ok,
    Unavailable,
    Unauthenticated,
    Incompatible,
    Invalid,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Status {
    pub state: State,
    pub detail: String,
    /// the project the Today feed was read from; used by the tray to read the content-free count
    #[serde(skip_serializing_if = "Option::is_none")]
    pub project: Option<String>,
}

impl Status {
    fn of(state: State, detail: &str) -> Self {
        Status { state, detail: detail.to_string(), project: None }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NetError {
    Refused,
    Timeout,
    Tls,
    Other,
}

pub struct Reply {
    pub status: u16,
    pub body: Vec<u8>,
}

pub trait Http: Send + Sync {
    fn get(&self, url: &str, bearer: Option<&Secret>, timeout: Duration) -> Result<Reply, NetError>;
}

pub struct UreqHttp;

impl Http for UreqHttp {
    fn get(&self, url: &str, bearer: Option<&Secret>, timeout: Duration) -> Result<Reply, NetError> {
        let agent = ureq::AgentBuilder::new().redirects(0).timeout(timeout).timeout_connect(timeout).build();
        let mut req = agent.get(url).set("Accept", "application/json");
        if let Some(b) = bearer {
            req = req.set("Authorization", &format!("Bearer {}", b.expose()));
        }
        let resp = match req.call() {
            Ok(r) => r,
            Err(ureq::Error::Status(_, r)) => r,           // 4xx/5xx still carry a status we classify
            Err(ureq::Error::Transport(t)) => return Err(map_transport(&t)),
        };
        let status = resp.status();
        let mut body = Vec::new();
        let _ = resp.into_reader().take(MAX_BODY).read_to_end(&mut body);
        Ok(Reply { status, body })
    }
}

fn map_transport(t: &ureq::Transport) -> NetError {
    use ureq::ErrorKind::*;
    match t.kind() {
        ConnectionFailed | Dns => NetError::Refused,
        Io => {
            let msg = t.to_string().to_ascii_lowercase();
            if msg.contains("timed out") || msg.contains("would block") { NetError::Timeout } else { NetError::Refused }
        }
        InvalidUrl | UnknownScheme | InsecureRequestHttpsOnly => NetError::Other,
        _ => {
            let msg = t.to_string().to_ascii_lowercase();
            if msg.contains("tls") || msg.contains("certificate") { NetError::Tls } else if msg.contains("timed out") { NetError::Timeout } else { NetError::Other }
        }
    }
}

fn project_ok(id: &str) -> bool {
    !id.is_empty() && id.len() <= 64 && id.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
}

fn join(base: &Url, path: &str) -> String {
    format!("{}{}", crate::config::base_with_slash(base), path.trim_start_matches('/'))
}

/// Map an HTTP exchange onto a state. `Ok(Some(reply))` means we got a 2xx to inspect.
fn gate(r: Result<Reply, NetError>) -> Result<Reply, Status> {
    let reply = r.map_err(|e| match e {
        NetError::Timeout => Status::of(State::Unavailable, "The runtime took too long to answer."),
        NetError::Tls => Status::of(State::Unavailable, "A secure connection couldn't be established."),
        NetError::Refused | NetError::Other => Status::of(State::Unavailable, "Can't reach the runtime."),
    })?;
    match reply.status {
        200..=299 => Ok(reply),
        300..=399 | 401 | 403 => Err(Status::of(State::Unauthenticated, "The deployment didn't accept this connection.")),
        404 | 405 | 410 => Err(Status::of(State::Incompatible, "This deployment doesn't provide the Today feed — update the runtime.")),
        500..=599 => Err(Status::of(State::Unavailable, "The runtime reported an error.")),
        _ => Err(Status::of(State::Incompatible, "The deployment answered in a way this app doesn't understand.")),
    }
}

pub fn probe(h: &dyn Http, base: &Url, token: Option<&Secret>) -> Status {
    let projects = match gate(h.get(&join(base, "api/projects"), token, PROBE_TIMEOUT)) {
        Ok(r) => r,
        Err(s) => return s,
    };
    let id = serde_json::from_slice::<serde_json::Value>(&projects.body).ok().and_then(|v| {
        v.as_array()?.iter().find_map(|p| p.get("id")?.as_str().map(str::to_string))
    });
    let Some(id) = id.filter(|i| project_ok(i)) else {
        return Status::of(State::Incompatible, "This doesn't look like a ReDevOps deployment.");
    };
    let today = match gate(h.get(&join(base, &format!("api/projects/{id}/today")), token, PROBE_TIMEOUT)) {
        Ok(r) => r,
        Err(s) => return s,
    };
    let has_items = serde_json::from_slice::<serde_json::Value>(&today.body).ok().map(|v| v.get("items").map_or(false, |i| i.is_array())).unwrap_or(false);
    if !has_items {
        return Status::of(State::Incompatible, "The Today feed isn't in the shape this app expects — update the runtime.");
    }
    Status { state: State::Ok, detail: "Connected.".into(), project: Some(id) }
}

/// What the tray shows. Only the server's CONTENT-FREE count and summary — never an item title, a customer name or
/// message text. The shell reads it; it does not compute, acknowledge or store notification state.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Counts {
    pub count: u32,
    pub summary: String,
}

pub fn counts(h: &dyn Http, base: &Url, project: &str, token: Option<&Secret>) -> Option<Counts> {
    if !project_ok(project) {
        return None;
    }
    let r = h.get(&join(base, &format!("api/projects/{project}/today/notifications")), token, PROBE_TIMEOUT).ok()?;
    if !(200..300).contains(&r.status) {
        return None;
    }
    let v: serde_json::Value = serde_json::from_slice(&r.body).ok()?;
    let count = u32::try_from(v.get("count")?.as_u64()?).ok()?;
    let summary: String = v.get("summary")?.as_str()?.chars().filter(|c| !c.is_control()).take(80).collect();
    Some(Counts { count, summary })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read as _, Write as _};
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};
    use std::thread;

    struct Fake(Mutex<Vec<Result<(u16, String), NetError>>>, Mutex<Vec<(String, Option<String>)>>);
    impl Fake {
        fn new(script: Vec<Result<(u16, &str), NetError>>) -> Self {
            Fake(Mutex::new(script.into_iter().rev().map(|r| r.map(|(s, b)| (s, b.to_string()))).collect()), Mutex::new(vec![]))
        }
    }
    impl Http for Fake {
        fn get(&self, url: &str, bearer: Option<&Secret>, _t: Duration) -> Result<Reply, NetError> {
            self.1.lock().unwrap().push((url.to_string(), bearer.map(|b| b.expose().to_string())));
            self.0.lock().unwrap().pop().expect("unexpected extra request").map(|(status, b)| Reply { status, body: b.into_bytes() })
        }
    }
    fn base() -> Url { Url::parse("https://demo.redevops.io/projects").unwrap() }
    const PROJECTS: &str = r#"[{"id":"customer-ops","name":"Customer Operations"}]"#;
    const TODAY: &str = r#"{"items":[],"headline":"Nothing needs you right now."}"#;

    #[test]
    fn ok_when_both_endpoints_answer_in_shape() {
        let f = Fake::new(vec![Ok((200, PROJECTS)), Ok((200, TODAY))]);
        let s = probe(&f, &base(), None);
        assert_eq!(s.state, State::Ok);
        assert_eq!(s.project.as_deref(), Some("customer-ops"));
        let urls: Vec<String> = f.1.lock().unwrap().iter().map(|(u, _)| u.clone()).collect();
        assert_eq!(urls, ["https://demo.redevops.io/projects/api/projects", "https://demo.redevops.io/projects/api/projects/customer-ops/today"]);
    }

    #[test]
    fn classifies_every_failure_into_the_four_states() {
        let cases: Vec<(Vec<Result<(u16, &str), NetError>>, State)> = vec![
            (vec![Err(NetError::Refused)], State::Unavailable),
            (vec![Err(NetError::Timeout)], State::Unavailable),
            (vec![Err(NetError::Tls)], State::Unavailable),
            (vec![Ok((503, ""))], State::Unavailable),
            (vec![Ok((500, ""))], State::Unavailable),
            (vec![Ok((401, ""))], State::Unauthenticated),
            (vec![Ok((403, ""))], State::Unauthenticated),
            (vec![Ok((302, ""))], State::Unauthenticated),               // a redirect to a login page
            (vec![Ok((404, ""))], State::Incompatible),
            (vec![Ok((200, "<html>not json</html>"))], State::Incompatible),
            (vec![Ok((200, "[]"))], State::Incompatible),
            (vec![Ok((200, PROJECTS)), Ok((404, ""))], State::Incompatible),         // too old: no Today
            (vec![Ok((200, PROJECTS)), Ok((401, ""))], State::Unauthenticated),
            (vec![Ok((200, PROJECTS)), Ok((200, r#"{"nope":1}"#))], State::Incompatible),
            (vec![Ok((200, PROJECTS)), Err(NetError::Timeout)], State::Unavailable),
        ];
        for (script, want) in cases {
            let desc = format!("{script:?}");
            assert_eq!(probe(&Fake::new(script), &base(), None).state, want, "{desc}");
        }
    }

    #[test]
    fn a_hostile_project_id_is_never_put_in_a_url() {
        for evil in ["../../etc", "a/b", "a b", "x?y=1", "", &"p".repeat(65)] {
            let body = format!(r#"[{{"id":"{evil}"}}]"#);
            let f = Fake::new(vec![Ok((200, &body))]);
            assert_eq!(probe(&f, &base(), None).state, State::Incompatible, "{evil}");
            assert_eq!(f.1.lock().unwrap().len(), 1, "must not have made a second request for {evil}");
        }
    }

    #[test]
    fn the_token_is_sent_as_a_bearer_and_never_appears_in_a_status() {
        let tok = Secret::new("TOP-SECRET-TOKEN-123").unwrap();
        let f = Fake::new(vec![Ok((200, PROJECTS)), Ok((401, "TOP-SECRET-TOKEN-123 rejected"))]);
        let s = probe(&f, &base(), Some(&tok));
        assert_eq!(s.state, State::Unauthenticated);
        assert!(f.1.lock().unwrap().iter().all(|(_, b)| b.as_deref() == Some("TOP-SECRET-TOKEN-123")));
        let shown = format!("{s:?} {}", serde_json::to_string(&s).unwrap());
        assert!(!shown.contains("TOP-SECRET"), "status leaked the token or server text: {shown}");
    }

    #[test]
    fn counts_reads_only_the_content_free_summary() {
        let f = Fake::new(vec![Ok((200, r#"{"count":5,"summary":"5 things need you","pending":[{"title":"Taylor Brooks visa"}]}"#))]);
        let c = counts(&f, &base(), "customer-ops", None).unwrap();
        assert_eq!(c, Counts { count: 5, summary: "5 things need you".into() });
        // control characters and over-long text from a hostile server are not passed through
        let f = Fake::new(vec![Ok((200, &format!(r#"{{"count":1,"summary":"{}\u0007x"}}"#, "a".repeat(500))))]);
        let c = counts(&f, &base(), "p", None).unwrap();
        assert_eq!(c.summary.chars().count(), 80);
        assert!(!c.summary.contains('\u{7}'));
        assert!(counts(&Fake::new(vec![Ok((500, ""))]), &base(), "p", None).is_none());
        assert!(counts(&Fake::new(vec![Ok((200, r#"{"count":-1,"summary":"x"}"#))]), &base(), "p", None).is_none());
        assert!(counts(&Fake::new(vec![]), &base(), "../x", None).is_none());      // bad project id: no request at all
    }

    // ── real sockets: the actual ureq client against a tiny local server ─────────────────────────────────────────
    fn serve(script: Vec<(u16, &'static str, u64)>) -> (u16, Arc<Mutex<Vec<String>>>) {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        let seen = Arc::new(Mutex::new(vec![]));
        let s2 = seen.clone();
        thread::spawn(move || {
            for (status, body, delay_ms) in script {
                let Ok((mut c, _)) = l.accept() else { return };
                let mut buf = [0u8; 4096];
                let n = c.read(&mut buf).unwrap_or(0);
                s2.lock().unwrap().push(String::from_utf8_lossy(&buf[..n]).to_string());
                thread::sleep(Duration::from_millis(delay_ms));
                let reason = if status == 200 { "OK" } else { "X" };
                let loc = if (300..400).contains(&status) { "Location: http://127.0.0.1:1/login\r\n" } else { "" };
                let _ = write!(c, "HTTP/1.1 {status} {reason}\r\n{loc}Content-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
            }
        });
        (port, seen)
    }

    #[test]
    fn real_client_connected_unauthenticated_and_bearer_header() {
        let (port, seen) = serve(vec![(200, PROJECTS, 0), (200, TODAY, 0)]);
        let tok = Secret::new("REAL-SOCKET-TOKEN").unwrap();
        let s = probe(&UreqHttp, &Url::parse(&format!("http://127.0.0.1:{port}")).unwrap(), Some(&tok));
        assert_eq!(s.state, State::Ok);
        assert!(seen.lock().unwrap()[0].to_ascii_lowercase().contains("authorization: bearer real-socket-token"));
        let (port, _) = serve(vec![(401, "{}", 0)]);
        assert_eq!(probe(&UreqHttp, &Url::parse(&format!("http://127.0.0.1:{port}")).unwrap(), None).state, State::Unauthenticated);
    }

    #[test]
    fn real_client_does_not_follow_redirects_so_a_token_cannot_be_forwarded() {
        let (port, _) = serve(vec![(302, "", 0)]);
        let tok = Secret::new("DO-NOT-FORWARD").unwrap();
        let s = probe(&UreqHttp, &Url::parse(&format!("http://127.0.0.1:{port}")).unwrap(), Some(&tok));
        assert_eq!(s.state, State::Unauthenticated);        // would be "unavailable" (nothing on :1) if it had followed
    }

    #[test]
    fn real_client_refused_connection_is_unavailable_and_fast() {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        drop(l);                                              // nothing is listening any more
        let t = std::time::Instant::now();
        let s = probe(&UreqHttp, &Url::parse(&format!("http://127.0.0.1:{port}")).unwrap(), None);
        assert_eq!(s.state, State::Unavailable);
        assert!(t.elapsed() < Duration::from_secs(3));
    }

    #[test]
    fn real_client_a_hung_runtime_cannot_hold_the_caller_past_the_timeout() {
        let (port, _) = serve(vec![(200, PROJECTS, 20_000)]);   // accepts, then never answers
        let t = std::time::Instant::now();
        let r = UreqHttp.get(&format!("http://127.0.0.1:{port}/api/projects"), None, Duration::from_millis(700));
        assert!(matches!(r, Err(NetError::Timeout)), "{:?}", r.err());
        assert!(t.elapsed() < Duration::from_secs(3), "took {:?}", t.elapsed());
    }
}
