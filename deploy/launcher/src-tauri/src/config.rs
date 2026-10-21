//! The saved connection (non-secret) and the URL policy.
//!
//! Policy, because this URL decides where a credential may be sent and what the window may navigate to:
//! http(s) only; no userinfo in the URL (credentials live in the OS keychain, never in a string that gets logged);
//! plain http only to this machine (or to hosts the user explicitly allowlisted) so a bearer token is never sent
//! in clear text across a network; query and fragment dropped.

use serde::{Deserialize, Serialize};
use std::fs;
use std::io::Write;
use std::path::Path;
use url::{Host, Url};

pub const MAX_URL_LEN: usize = 2048;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Connection {
    pub url: String,
    #[serde(default)]
    pub label: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UrlError {
    Empty,
    TooLong,
    Malformed,
    Scheme,
    Userinfo,
    NoHost,
    InsecureRemote(String),
}

impl std::fmt::Display for UrlError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            UrlError::Empty => write!(f, "Enter the address of your ReDevOps deployment."),
            UrlError::TooLong => write!(f, "That address is too long."),
            UrlError::Malformed => write!(f, "That doesn't look like a web address."),
            UrlError::Scheme => write!(f, "Use an http:// or https:// address."),
            UrlError::Userinfo => write!(f, "Don't put a username or password in the address — the app stores credentials securely instead."),
            UrlError::NoHost => write!(f, "The address has no host name."),
            UrlError::InsecureRemote(h) => write!(f, "{h} must use https://. Credentials are never sent unencrypted to another machine."),
        }
    }
}

fn is_loopback(host: &Host<&str>) -> bool {
    match host {
        Host::Domain(d) => d.eq_ignore_ascii_case("localhost"),
        Host::Ipv4(ip) => ip.is_loopback(),
        Host::Ipv6(ip) => ip.is_loopback(),
    }
}

/// `insecure_allow` is an explicit, user-supplied list of hosts that may be reached over plain http
/// (e.g. a LAN box). Empty by default.
pub fn normalize_url(input: &str, insecure_allow: &[String]) -> Result<Url, UrlError> {
    let raw = input.trim();
    if raw.is_empty() {
        return Err(UrlError::Empty);
    }
    if raw.len() > MAX_URL_LEN {
        return Err(UrlError::TooLong);
    }
    // a bare host: loopback gets http (that is how the local runtime listens), everything else https
    let with_scheme = if raw.contains("://") {
        raw.to_string()
    } else {
        let host = raw.split(['/', ':']).next().unwrap_or("");
        let local = host.eq_ignore_ascii_case("localhost") || host.starts_with("127.") || host == "[::1]";
        format!("{}://{}", if local { "http" } else { "https" }, raw)
    };
    let mut url = Url::parse(&with_scheme).map_err(|_| UrlError::Malformed)?;
    if url.scheme() != "http" && url.scheme() != "https" {
        return Err(UrlError::Scheme);
    }
    if !url.username().is_empty() || url.password().is_some() {
        return Err(UrlError::Userinfo);
    }
    let host = url.host().ok_or(UrlError::NoHost)?;
    if url.scheme() == "http" && !is_loopback(&host) {
        let h = url.host_str().unwrap_or("").to_ascii_lowercase();
        if !insecure_allow.iter().any(|a| a.eq_ignore_ascii_case(&h)) {
            return Err(UrlError::InsecureRemote(h));
        }
    }
    url.set_query(None);
    url.set_fragment(None);
    let trimmed = url.path().trim_end_matches('/').to_string();
    url.set_path(if trimmed.is_empty() { "/" } else { &trimmed });
    Ok(url)
}

/// `https://host/projects` → `https://host/projects/` (the form joined and navigated to).
pub fn base_with_slash(url: &Url) -> String {
    let s = url.as_str();
    if s.ends_with('/') { s.to_string() } else { format!("{s}/") }
}

/// The stable key a credential is stored under: scheme + host + port (+ path prefix).
pub fn account_for(url: &Url) -> String {
    format!("conn:{}", base_with_slash(url))
}

pub fn same_origin(a: &Url, b: &Url) -> bool {
    a.scheme() == b.scheme() && a.host_str() == b.host_str() && a.port_or_known_default() == b.port_or_known_default()
}

/// Hosts allowed over plain http, from `RDO_ALLOW_INSECURE_HOSTS=host1,host2` (explicit opt-in, empty by default).
pub fn insecure_allowlist() -> Vec<String> {
    std::env::var("RDO_ALLOW_INSECURE_HOSTS")
        .map(|v| v.split(',').map(|s| s.trim().to_ascii_lowercase()).filter(|s| !s.is_empty()).collect())
        .unwrap_or_default()
}

// ── persistence: NON-secret connection details only ─────────────────────────────────────────────────────────────

pub fn load(path: &Path) -> Option<Connection> {
    let bytes = fs::read(path).ok()?;
    let c: Connection = serde_json::from_slice(&bytes).ok()?;
    // re-validate on load: a hand-edited file must not smuggle in a URL the policy would refuse
    normalize_url(&c.url, &insecure_allowlist()).ok().map(|u| Connection { url: u.to_string(), label: c.label })
}

pub fn save(path: &Path, c: &Connection) -> std::io::Result<()> {
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir)?;
    }
    let tmp = path.with_extension("json.tmp");
    {
        let mut f = fs::File::create(&tmp)?;
        f.write_all(serde_json::to_string_pretty(c).unwrap_or_default().as_bytes())?;
        f.sync_all()?;
    }
    fs::rename(tmp, path)
}

pub fn forget(path: &Path) {
    let _ = fs::remove_file(path);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn n(s: &str) -> Result<String, UrlError> {
        normalize_url(s, &[]).map(|u| u.to_string())
    }

    #[test]
    fn accepts_https_and_loopback_http() {
        assert_eq!(n("https://demo.redevops.io").unwrap(), "https://demo.redevops.io/");
        assert_eq!(n("https://demo.redevops.io/projects/").unwrap(), "https://demo.redevops.io/projects");
        assert_eq!(n("http://127.0.0.1:8787").unwrap(), "http://127.0.0.1:8787/");
        assert_eq!(n("http://localhost:8787/").unwrap(), "http://localhost:8787/");
        assert_eq!(n("http://[::1]:8787").unwrap(), "http://[::1]:8787/");
    }

    #[test]
    fn bare_hosts_get_a_safe_default_scheme() {
        assert_eq!(n("demo.redevops.io").unwrap(), "https://demo.redevops.io/");
        assert_eq!(n("localhost:8787").unwrap(), "http://localhost:8787/");
        assert_eq!(n("127.0.0.1:8787").unwrap(), "http://127.0.0.1:8787/");
    }

    #[test]
    fn plain_http_to_another_machine_is_refused_unless_allowlisted() {
        assert_eq!(n("http://192.168.1.20:8787"), Err(UrlError::InsecureRemote("192.168.1.20".into())));
        assert_eq!(n("http://demo.redevops.io"), Err(UrlError::InsecureRemote("demo.redevops.io".into())));
        let ok = normalize_url("http://192.168.1.20:8787", &["192.168.1.20".to_string()]).unwrap();
        assert_eq!(ok.as_str(), "http://192.168.1.20:8787/");
    }

    #[test]
    fn refuses_credentials_in_the_url_and_non_web_schemes() {
        assert_eq!(n("https://user:pw@demo.redevops.io"), Err(UrlError::Userinfo));
        assert_eq!(n("https://token@demo.redevops.io"), Err(UrlError::Userinfo));
        for bad in ["file:///etc/passwd", "javascript:alert(1)", "ftp://x.example", "data:text/html,hi", "tauri://localhost", "ssh://h"] {
            assert!(matches!(n(bad), Err(UrlError::Scheme) | Err(UrlError::Malformed)), "{bad}");
        }
    }

    #[test]
    fn refuses_empty_oversized_and_hostless() {
        assert_eq!(n("   "), Err(UrlError::Empty));
        assert_eq!(n(&format!("https://x.example/{}", "a".repeat(2100))), Err(UrlError::TooLong));
        assert!(n("https://").is_err());
        assert!(n("not a url at all ???").is_err());
    }

    #[test]
    fn drops_query_and_fragment_so_nothing_rides_along() {
        assert_eq!(n("https://demo.redevops.io/projects?token=abc#frag").unwrap(), "https://demo.redevops.io/projects");
    }

    #[test]
    fn origin_comparison_ignores_path_and_default_port() {
        let a = Url::parse("https://demo.redevops.io/projects").unwrap();
        assert!(same_origin(&a, &Url::parse("https://demo.redevops.io:443/other").unwrap()));
        assert!(!same_origin(&a, &Url::parse("https://evil.example/projects").unwrap()));
        assert!(!same_origin(&a, &Url::parse("http://demo.redevops.io/projects").unwrap()));
        // a look-alike host must not match
        assert!(!same_origin(&a, &Url::parse("https://demo.redevops.io.evil.example/").unwrap()));
    }

    #[test]
    fn account_key_is_stable_and_has_no_secret_material() {
        let u = normalize_url("https://demo.redevops.io/projects/", &[]).unwrap();
        assert_eq!(account_for(&u), "conn:https://demo.redevops.io/projects/");
    }

    #[test]
    fn saves_atomically_and_revalidates_on_load() {
        let dir = std::env::temp_dir().join(format!("rdo-cfg-{}", std::process::id()));
        let p = dir.join("connection.json");
        save(&p, &Connection { url: "https://demo.redevops.io/".into(), label: "Demo".into() }).unwrap();
        assert_eq!(load(&p).unwrap().url, "https://demo.redevops.io/");
        assert!(!dir.join("connection.json.tmp").exists());
        // a hand-edited file with a URL the policy refuses is ignored
        fs::write(&p, br#"{"url":"https://user:pw@demo.redevops.io/","label":""}"#).unwrap();
        assert!(load(&p).is_none());
        fs::write(&p, br#"{"url":"file:///etc/passwd","label":""}"#).unwrap();
        assert!(load(&p).is_none());
        fs::write(&p, b"not json").unwrap();
        assert!(load(&p).is_none());
        forget(&p);
        assert!(load(&p).is_none());
        let _ = fs::remove_dir_all(dir);
    }
}
