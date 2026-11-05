// Navigation guard for the launcher webview (RC3). The window shows only the bundled page; it never loads the
// control plane's origin (docs/LOCAL_API_SECURITY.md 4.3), the user's browser does. The CSP already blocks
// forms, frames, objects and subresource loads from elsewhere (`default-src 'none'`, `form-action 'none'`), but
// a CSP does not stop a top-level navigation. This hook does: any navigation to a URL outside the app's own
// origin is cancelled. It is installed as a tiny plugin (no window configuration change), so every webview
// of the app goes through it.

use tauri::plugin::{Builder, TauriPlugin};
use tauri::{Runtime, Url};

/// The origins Tauri itself serves the bundled frontend from: `tauri://localhost` (macOS, Linux) and
/// `http(s)://tauri.localhost` (Windows). `about:blank` is what a webview shows before the first load.
pub fn navigation_allowed(url: &Url) -> bool {
    if !url.username().is_empty() || url.password().is_some() || url.port().is_some() {
        return false;
    }
    match url.scheme() {
        "tauri" => url.host_str() == Some("localhost"),
        "http" | "https" => url.host_str() == Some("tauri.localhost"),
        "about" => url.as_str() == "about:blank",
        _ => false,
    }
}

/// Plugin that cancels every navigation `navigation_allowed` refuses.
pub fn plugin<R: Runtime>() -> TauriPlugin<R> {
    Builder::new("launcher-navigation-guard")
        .on_navigation(|_webview, url| navigation_allowed(url))
        .build()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ok(u: &str) -> bool {
        navigation_allowed(&Url::parse(u).unwrap())
    }

    #[test]
    fn the_apps_own_origins_are_allowed() {
        for u in [
            "tauri://localhost",
            "tauri://localhost/",
            "tauri://localhost/index.html",
            "tauri://localhost/app.js",
            "http://tauri.localhost/",
            "https://tauri.localhost/index.html",
            "about:blank",
        ] {
            assert!(ok(u), "{u}");
        }
    }

    #[test]
    fn everything_else_is_refused_including_the_sidecar_origin_and_lookalikes() {
        for u in [
            "http://127.0.0.1:8787/",
            "http://127.0.0.1:51234/#lc=abc",
            "http://localhost:8787/",
            "http://localhost/",
            "https://example.com/",
            "http://tauri.localhost.evil.example/",
            "http://evil.example/tauri.localhost",
            "http://tauri.localhost:8080/",
            "http://user@tauri.localhost/",
            "http://user:pw@tauri.localhost/",
            "tauri://evil/",
            "tauri://localhost.evil/",
            "ipc://localhost/start_native",
            "asset://localhost/etc/passwd",
            "file:///etc/passwd",
            "data:text/html,<h1>x</h1>",
            "javascript:alert(1)",
            "blob:tauri://localhost/00000000-0000-0000-0000-000000000000",
            "about:srcdoc",
            "about:blank#x",
            "ftp://tauri.localhost/",
            "ws://tauri.localhost/",
        ] {
            assert!(!ok(u), "{u}");
        }
    }
}
