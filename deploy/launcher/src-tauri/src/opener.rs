// Opening the UI without putting a credential on any command line (security review SR-01).
//
// The launch code is single use and lives 60 s, but a URL passed to `open` / `xdg-open` / `cmd /C start`
// sits in that process's argv, which every other local user can read (`ps`, /proc/<pid>/cmdline) while
// the opener (or, on Linux, a freshly started browser) runs. So the opener is given only the PATH of a
// small local HTML file; the file (mode 0600, in a private 0700 directory of the current user) holds the
// URL with the code in its fragment and redirects the browser to it. The file is deleted after the code's
// lifetime (60 s) and on exit, and stale files are swept before each write.
//
// Redirect semantics: `location.replace("http://127.0.0.1:<port>/#lc=<code>")` from a file: page is a
// top-level navigation from file: to http:, which Chromium, WebKit and Gecko allow (a file: page may not
// LOAD subresources from other origins, but navigating away is not restricted); the fragment is part of
// the target URL and survives. See the stage 3 report for what was observed in headless browsers.

use std::ffi::OsString;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, SystemTime};

/// A redirect file is useless after the code expires (60 s); keep it that long and no longer.
pub const FILE_LIFETIME: Duration = Duration::from_secs(60);
/// Files older than this are removed whenever a new one is written (crash leftovers).
const STALE_AFTER: Duration = Duration::from_secs(120);
const PREFIX: &str = "open-";
const SUFFIX: &str = ".html";

/// Files this process wrote and has not removed yet (removed by the timers and on exit).
static PENDING: Mutex<Vec<PathBuf>> = Mutex::new(Vec::new());

fn pending() -> std::sync::MutexGuard<'static, Vec<PathBuf>> {
    PENDING.lock().unwrap_or_else(|e| e.into_inner())
}

/// The redirect page for `url`. The URL is built by this program from a port, a validated base path
/// and a base64url code; it is still refused here unless it only contains characters that are inert in
/// both an HTML attribute and a JavaScript string literal.
pub fn redirect_html(url: &str) -> Result<String, &'static str> {
    if !url.starts_with("http://127.0.0.1:")
        || !url.bytes().all(|c| c.is_ascii_alphanumeric() || b":/._-#=~".contains(&c))
    {
        return Err("refusing to write a redirect page for an unexpected URL");
    }
    Ok(format!(
        "<!doctype html>\n<html><head><meta charset=\"utf-8\">\n\
         <meta name=\"referrer\" content=\"no-referrer\">\n\
         <meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline'\">\n\
         <title>Opening Agentic Apps</title>\n\
         <noscript><meta http-equiv=\"refresh\" content=\"0;url={url}\"></noscript>\n\
         </head><body><p>Opening Agentic Apps&hellip;</p>\n\
         <script>location.replace(\"{url}\");</script>\n\
         </body></html>\n"
    ))
}

/// The program and arguments handed to the OS: only the FILE PATH, never the URL or any code.
pub fn opener_command(path: &Path) -> (&'static str, Vec<OsString>) {
    let p: OsString = path.as_os_str().to_owned();
    if cfg!(target_os = "windows") {
        ("cmd", vec!["/C".into(), "start".into(), "".into(), p])
    } else if cfg!(target_os = "macos") {
        ("open", vec![p])
    } else {
        ("xdg-open", vec![p])
    }
}

/// Create (or tighten) the private directory. Unix: mode 0700, must be a real directory (not a
/// symlink) owned by the current user. Windows: the per-user directory under %LOCALAPPDATA% inherits
/// the default user-only ACL.
pub fn ensure_private_dir(dir: &Path) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
        std::fs::DirBuilder::new().recursive(true).mode(0o700).create(dir)?;
        let md = std::fs::symlink_metadata(dir)?;
        if !md.is_dir() {
            return Err(std::io::Error::other("launch directory is not a plain directory"));
        }
        if md.uid() != unsafe { libc::geteuid() } {
            return Err(std::io::Error::other("launch directory is owned by another user"));
        }
        if md.mode() & 0o077 != 0 {
            std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))?;
        }
        Ok(())
    }
    #[cfg(not(unix))]
    {
        std::fs::create_dir_all(dir)
    }
}

fn random_name() -> std::io::Result<String> {
    let mut b = [0u8; 16];
    getrandom::fill(&mut b).map_err(|_| std::io::Error::other("no system randomness"))?;
    let hex: String = b.iter().map(|x| format!("{x:02x}")).collect();
    Ok(format!("{PREFIX}{hex}{SUFFIX}"))
}

/// Remove redirect files of this launcher older than STALE_AFTER (leftovers of a crash).
pub fn sweep_stale(dir: &Path) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let name = e.file_name();
        let name = name.to_string_lossy();
        if !(name.starts_with(PREFIX) && name.ends_with(SUFFIX)) {
            continue;
        }
        let old = e
            .metadata()
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| SystemTime::now().duration_since(t).ok())
            .is_some_and(|age| age > STALE_AFTER);
        if old {
            let _ = std::fs::remove_file(e.path());
        }
    }
}

/// Write the redirect page for `url` into `dir` (created private if needed) and return its path. The
/// file is created exclusively (never follows a pre-planted name or symlink), mode 0600 on Unix.
pub fn write_redirect(dir: &Path, url: &str) -> std::io::Result<PathBuf> {
    let html = redirect_html(url).map_err(std::io::Error::other)?;
    ensure_private_dir(dir)?;
    sweep_stale(dir);
    let path = dir.join(random_name()?);
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let mut f = opts.open(&path)?;
    f.write_all(html.as_bytes())?;
    f.flush()?;
    pending().push(path.clone());
    Ok(path)
}

/// Delete `path` now and forget it.
pub fn remove(path: &Path) {
    let _ = std::fs::remove_file(path);
    pending().retain(|p| p != path);
}

/// Delete `path` after `delay` on a background thread (the code is dead by then anyway).
pub fn remove_after(path: PathBuf, delay: Duration) {
    std::thread::spawn(move || {
        std::thread::sleep(delay);
        remove(&path);
    });
}

/// Delete every redirect file this process wrote (called on exit).
pub fn cleanup_all() {
    let files: Vec<PathBuf> = std::mem::take(&mut *pending());
    for f in files {
        let _ = std::fs::remove_file(f);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const URL: &str = "http://127.0.0.1:50123/#lc=AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCdE";

    fn tmp_dir(tag: &str) -> PathBuf {
        let mut r = [0u8; 6];
        getrandom::fill(&mut r).unwrap();
        let n: String = r.iter().map(|b| format!("{b:02x}")).collect();
        std::env::temp_dir().join(format!("rdo-opener-test-{tag}-{n}"))
    }

    #[test]
    fn opener_argv_carries_only_the_file_path_no_code_no_secret() {
        let p = Path::new("/home/u/.cache/app/launch/open-0123.html");
        let (prog, args) = opener_command(p);
        let joined = format!("{prog} {}", args.iter().map(|a| a.to_string_lossy()).collect::<Vec<_>>().join(" "));
        assert!(joined.contains("open-0123.html"));
        assert!(!joined.contains("lc="), "{joined}");
        assert!(!joined.contains("http"), "no URL at all on the command line: {joined}");
        assert!(!joined.contains('#'));
    }

    #[test]
    fn redirect_page_keeps_the_fragment_and_is_inert() {
        let h = redirect_html(URL).unwrap();
        assert!(h.contains(&format!("location.replace(\"{URL}\")")), "JS redirect keeps the full URL with the fragment");
        assert!(h.contains(&format!("<noscript><meta http-equiv=\"refresh\" content=\"0;url={URL}\"></noscript>")));
        assert!(h.contains("no-referrer") && h.contains("default-src 'none'"));
        assert_eq!(h.matches(URL).count(), 2);
    }

    #[test]
    fn redirect_page_refuses_urls_that_could_break_out_of_the_markup() {
        for bad in [
            "http://127.0.0.1:1/#lc=a\"+alert(1)+\"",
            "http://127.0.0.1:1/#lc=a'b",
            "http://127.0.0.1:1/#lc=<script>",
            "http://127.0.0.1:1/#lc=a b",
            "http://127.0.0.1:1/#lc=a\\b",
            "https://evil.example/#lc=abc",
            "http://evil.example/#lc=abc",
            "javascript:alert(1)",
            "file:///etc/passwd",
        ] {
            assert!(redirect_html(bad).is_err(), "{bad}");
        }
    }

    #[cfg(unix)]
    #[test]
    fn file_is_0600_in_a_0700_directory_and_removable() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tmp_dir("modes");
        let p = write_redirect(&dir, URL).unwrap();
        assert_eq!(std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777, 0o700);
        assert_eq!(std::fs::metadata(&p).unwrap().permissions().mode() & 0o777, 0o600);
        assert!(std::fs::read_to_string(&p).unwrap().contains("#lc="));
        remove(&p);
        assert!(!p.exists());
        std::fs::remove_dir(&dir).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn a_too_open_existing_directory_is_tightened_and_a_symlink_is_refused() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tmp_dir("loose");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o755)).unwrap();
        ensure_private_dir(&dir).unwrap();
        assert_eq!(std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777, 0o700);
        std::fs::remove_dir(&dir).unwrap();

        let target = tmp_dir("target");
        std::fs::create_dir_all(&target).unwrap();
        let link = tmp_dir("link");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        assert!(ensure_private_dir(&link).is_err(), "a symlinked launch directory is not trusted");
        assert!(write_redirect(&link, URL).is_err());
        std::fs::remove_file(&link).unwrap();
        std::fs::remove_dir(&target).unwrap();
    }

    #[test]
    fn files_never_collide_and_never_overwrite() {
        let dir = tmp_dir("names");
        let a = write_redirect(&dir, URL).unwrap();
        let b = write_redirect(&dir, URL).unwrap();
        assert_ne!(a, b);
        remove(&a);
        remove(&b);
        std::fs::remove_dir(&dir).unwrap();
    }

    #[test]
    fn cleanup_all_and_timers_remove_files() {
        let dir = tmp_dir("cleanup");
        let a = write_redirect(&dir, URL).unwrap();
        let b = write_redirect(&dir, URL).unwrap();
        remove_after(a.clone(), Duration::from_millis(100));
        let end = std::time::Instant::now() + Duration::from_secs(3);
        while a.exists() && std::time::Instant::now() < end {
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(!a.exists(), "timer removed the file");
        assert!(b.exists());
        cleanup_all(); // exit hook
        assert!(!b.exists(), "exit hook removed the remaining file");
        std::fs::remove_dir(&dir).unwrap();
    }

    #[test]
    fn stale_files_are_swept_but_other_files_are_left() {
        let dir = tmp_dir("sweep");
        ensure_private_dir(&dir).unwrap();
        let old = dir.join(format!("{PREFIX}old{SUFFIX}"));
        let other = dir.join("notes.txt");
        std::fs::write(&old, "x").unwrap();
        std::fs::write(&other, "x").unwrap();
        let t = SystemTime::now() - Duration::from_secs(3600);
        std::fs::File::options().write(true).open(&old).unwrap().set_modified(t).unwrap();
        sweep_stale(&dir);
        assert!(!old.exists() && other.exists());
        std::fs::remove_file(&other).unwrap();
        std::fs::remove_dir(&dir).unwrap();
    }

    /// The review's attack (scripts/40_argv_launch_code.py) against THIS opener: a stand-in process
    /// carries exactly the argv the launcher would give the OS opener and lives long enough for a poller
    /// to read it; the poller looks for a launch URL/code in every process's argv and must find none.
    #[cfg(unix)]
    #[test]
    fn a_ps_poller_finds_no_code_in_the_openers_argv() {
        let dir = tmp_dir("ps");
        let p = write_redirect(&dir, URL).unwrap();
        let (_prog, args) = opener_command(&p);
        // stand-in for `open <path>`: same argv shape, stays alive 600 ms (like the review's stand-in)
        let mut live = std::process::Command::new("sh")
            .arg("-c")
            .arg("sleep 0.6; true")
            .arg("stand-in-opener")
            .args(&args)
            .spawn()
            .unwrap();
        let mut seen = String::new();
        let end = std::time::Instant::now() + Duration::from_millis(500);
        while std::time::Instant::now() < end {
            let out = std::process::Command::new("ps").args(["-axww", "-o", "args="]).output().unwrap();
            seen.push_str(&String::from_utf8_lossy(&out.stdout));
            if seen.contains("stand-in-opener") {
                break;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        let _ = live.wait();
        assert!(seen.contains("stand-in-opener"), "the poller did see the stand-in's argv");
        assert!(seen.contains(p.to_str().unwrap()), "...including the file path");
        assert!(!seen.contains("#lc=AbCdEfGh") && !seen.contains(&URL[URL.find('#').unwrap()..]), "but no code");
        remove(&p);
        std::fs::remove_dir(&dir).unwrap();
    }

    /// Emit a real redirect page for an out-of-process browser check:
    /// `RDO_REDIRECT_OUT=<file> RDO_REDIRECT_URL=<url> cargo test emit_redirect_page -- --ignored`.
    #[test]
    #[ignore]
    fn emit_redirect_page_for_browser_check() {
        let (Ok(out), Ok(url)) = (std::env::var("RDO_REDIRECT_OUT"), std::env::var("RDO_REDIRECT_URL")) else {
            return; // helper only: needs both variables
        };
        std::fs::write(out, redirect_html(&url).unwrap()).unwrap();
    }
}
