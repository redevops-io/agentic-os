// Navigation guard for the launcher webview (RC3). The window shows only the bundled page; it never loads the
// control plane's origin (docs/LOCAL_API_SECURITY.md 4.3), the user's browser does. The CSP already blocks
// forms, frames, objects and subresource loads from elsewhere (`default-src 'none'`, `form-action 'none'`), but
// a CSP does not stop a top-level navigation. This hook does: any navigation to a URL outside the app's own
// origin is cancelled. It is installed as a tiny plugin (no window configuration change), so every webview
// of the app goes through it.

use tauri::plugin::{Builder, TauriPlugin};
use tauri::{Runtime, Url};

/// The origin Tauri serves the bundled frontend from, per OS (tauri 2.11 `tauri_protocol_url`): `tauri://localhost`
/// on macOS and Linux, `http(s)://tauri.localhost` on Windows only. The `tauri.localhost` hosts are therefore NOT
/// an app origin on macOS/Linux and are refused there; `tauri://localhost` stays allowed everywhere (harmless on
/// Windows, where no webview serves it). `about:blank` is refused: the webview does not need it for the first
/// load (OBSERVED natively on macOS: the launcher loads with it refused; a script that navigates to it would
/// blank the launcher page). Windows initial load without `about:blank`: UNVERIFIED at run time.
pub fn navigation_allowed_on(url: &Url, windows: bool) -> bool {
    if !url.username().is_empty() || url.password().is_some() || url.port().is_some() {
        return false;
    }
    match url.scheme() {
        "tauri" => url.host_str() == Some("localhost"),
        "http" | "https" => windows && url.host_str() == Some("tauri.localhost"),
        _ => false,
    }
}

/// The decision for the OS this launcher was compiled for.
pub fn navigation_allowed(url: &Url) -> bool {
    navigation_allowed_on(url, cfg!(windows))
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

    fn on(u: &str, windows: bool) -> bool {
        navigation_allowed_on(&Url::parse(u).unwrap(), windows)
    }

    #[test]
    fn the_apps_own_origin_is_allowed_per_os() {
        for u in ["tauri://localhost", "tauri://localhost/", "tauri://localhost/index.html", "tauri://localhost/app.js"] {
            assert!(on(u, false) && on(u, true), "{u}"); // allowed on every OS
        }
        for u in ["http://tauri.localhost/", "https://tauri.localhost/index.html", "http://tauri.localhost"] {
            assert!(on(u, true), "{u} is the Windows app origin");
            assert!(!on(u, false), "{u} is not an app origin on macOS/Linux");
        }
    }

    #[test]
    fn about_blank_is_refused_on_every_os() {
        for w in [false, true] {
            assert!(!on("about:blank", w) && !on("about:blank#x", w) && !on("about:srcdoc", w), "windows={w}");
        }
    }

    #[test]
    fn the_compiled_decision_follows_the_target_os() {
        assert!(ok("tauri://localhost/"));
        assert_eq!(ok("http://tauri.localhost/"), cfg!(windows));
        assert!(!ok("about:blank"));
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
            assert!(!on(u, false) && !on(u, true), "{u}");
        }
    }
}
