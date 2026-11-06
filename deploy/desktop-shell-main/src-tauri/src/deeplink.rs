//! `redevops://open?section=today&item=att_123` → a URL hash for the web UI.
//!
//! The shell does NOT know which sections exist and does not navigate by itself: it only checks that the link is
//! shaped sanely and hands `#/<section>/<item>` to the web UI, which owns routing and allowlists the section.
//! One implementation of navigation (in the web app), used by the browser and the desktop alike. A link can never
//! choose a host: it only ever produces a hash on the user's own configured deployment.

use url::Url;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LinkError {
    Scheme,
    Shape,
    UnknownParam,
}

fn section_ok(s: &str) -> bool {
    (1..=24).contains(&s.len()) && s.chars().all(|c| c.is_ascii_lowercase() || c == '-')
}
fn item_ok(s: &str) -> bool {
    (1..=80).contains(&s.len()) && s.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | ':' | '.' | '-'))
}

/// Returns the hash to set, e.g. `#/today/att_123`. Defaults to Today when no section is given.
pub fn parse(link: &str) -> Result<String, LinkError> {
    let url = Url::parse(link).map_err(|_| LinkError::Shape)?;
    if url.scheme() != "redevops" {
        return Err(LinkError::Scheme);
    }
    if url.host_str() != Some("open") || !(url.path().is_empty() || url.path() == "/") || url.fragment().is_some()
        || !url.username().is_empty() || url.password().is_some() || url.port().is_some() {
        return Err(LinkError::Shape);
    }
    let (mut section, mut item) = (None, None);
    for (k, v) in url.query_pairs() {
        match k.as_ref() {
            "section" if section.is_none() => section = Some(v.into_owned()),
            "item" if item.is_none() => item = Some(v.into_owned()),
            _ => return Err(LinkError::UnknownParam),
        }
    }
    let section = section.unwrap_or_else(|| "today".to_string());
    if !section_ok(&section) {
        return Err(LinkError::Shape);
    }
    match item {
        None => Ok(format!("#/{section}")),
        Some(i) if item_ok(&i) => Ok(format!("#/{section}/{i}")),
        Some(_) => Err(LinkError::Shape),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn well_formed_links_become_hashes() {
        assert_eq!(parse("redevops://open").unwrap(), "#/today");
        assert_eq!(parse("redevops://open?section=today").unwrap(), "#/today");
        assert_eq!(parse("redevops://open?section=connections-apps").unwrap(), "#/connections-apps");
        assert_eq!(parse("redevops://open?section=today&item=att_6afa9a4163a6afa2").unwrap(), "#/today/att_6afa9a4163a6afa2");
        assert_eq!(parse("redevops://open/?item=rem-cap_0001").unwrap(), "#/today/rem-cap_0001");
    }

    #[test]
    fn a_link_can_never_choose_where_to_navigate() {
        for bad in [
            "https://evil.example/", "http://127.0.0.1:8787/", "javascript:alert(1)", "file:///etc/passwd", "tauri://localhost",
            "redevops://evil.example/open", "redevops://open@evil.example", "redevops://user:pw@open", "redevops://open:99",
            "redevops://open/../../x", "redevops://open/x/y", "redevops://open#/today", "redevops:open",
        ] {
            assert!(parse(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn hostile_parameters_are_refused_not_sanitised() {
        for bad in [
            "redevops://open?section=Today", "redevops://open?section=today/evil", "redevops://open?section=",
            "redevops://open?section=%3Cscript%3E", "redevops://open?item=%3Cscript%3E", "redevops://open?item=a%20b",
            "redevops://open?item=../x", "redevops://open?item=a%2Fb", "redevops://open?section=today&item=",
            &format!("redevops://open?item={}", "x".repeat(81)), &format!("redevops://open?section={}", "a".repeat(25)),
        ] {
            assert!(parse(bad).is_err(), "{bad}");
        }
        assert_eq!(parse("redevops://open?section=today&x=1"), Err(LinkError::UnknownParam));
        assert_eq!(parse("redevops://open?section=today&section=work"), Err(LinkError::UnknownParam));
        assert_eq!(parse("redevops://open?url=https://evil.example"), Err(LinkError::UnknownParam));
    }

    #[test]
    fn output_only_contains_safe_characters() {
        let h = parse("redevops://open?section=today&item=att:1.2_x-y").unwrap();
        assert!(h.chars().all(|c| c.is_ascii_alphanumeric() || "#/_:.-".contains(c)), "{h}");
    }
}
