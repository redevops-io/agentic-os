"""Static guards for the launcher shell (R0-prime defect c). No GUI, no Rust toolchain needed.

(c) `ui/index.html` calls `window.__TAURI__.core.invoke`. Tauri 2 only injects `window.__TAURI__` when
`app.withGlobalTauri` is true; v0.3.2 did not set it, so every button was dead (the shipped binary had 0
`__TAURI_IIFE__` markers). The compiled-in check lives in the Rust test
`the_global_tauri_api_is_compiled_in` (cargo test) and the binary check in the R0-prime report; this
file is the cheap CI guard that needs neither.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[1] / "deploy" / "launcher"
CONF = LAUNCHER / "src-tauri" / "tauri.conf.json"
UI = LAUNCHER / "ui"
MAIN_RS = LAUNCHER / "src-tauri" / "src" / "main.rs"


def _conf() -> dict:
    return json.loads(CONF.read_text(encoding="utf-8"))


def _ui_pages():
    pages = sorted(UI.glob("*.html"))
    assert pages, "the launcher UI is expected under deploy/launcher/ui"
    return pages


def _ui_sources():
    """The page and its scripts: since RC3 the page logic is the external ui/app.js (no inline script, CSP script-src 'self')."""
    return _ui_pages() + sorted(UI.glob("*.js"))


def test_ui_uses_the_global_tauri_object():
    # If this ever stops being true (UI moves to bundled @tauri-apps/api imports) the next test
    # becomes optional; until then it is the thing that needs the flag.
    assert any("window.__TAURI__" in p.read_text(encoding="utf-8") for p in _ui_sources())


def test_with_global_tauri_is_enabled_whenever_the_ui_uses_window_tauri():
    uses_global = any("window.__TAURI__" in p.read_text(encoding="utf-8") for p in _ui_sources())
    app = _conf()["app"]
    assert (not uses_global) or app.get("withGlobalTauri") is True, (
        "ui/*.html uses window.__TAURI__ but app.withGlobalTauri is not true: every button would be dead"
    )


def test_with_global_tauri_key_is_the_documented_camelcase_app_property():
    conf = _conf()
    assert conf["app"]["withGlobalTauri"] is True
    assert "withGlobalTauri" not in conf  # a top-level key would be silently ignored/rejected
    assert not any("with_global_tauri" in k or "with-global-tauri" in k for k in conf["app"])


def test_every_command_the_ui_invokes_is_registered_in_the_rust_handler():
    html = "\n".join(p.read_text(encoding="utf-8") for p in _ui_sources())
    used = set(re.findall(r'(?:invoke|run)\(\s*"([a-z_]+)"', html))
    assert {"device_report", "start_native", "stop_native", "docker_available", "about_info", "about_text"} <= used
    rs = MAIN_RS.read_text(encoding="utf-8")
    handler = re.search(r"generate_handler!\[(.*?)\]", rs, re.S).group(1)
    registered = set(re.findall(r"[a-z_]+", handler))
    assert used <= registered, f"UI invokes unregistered commands: {used - registered}"
    rs_all = rs + "\n" + (LAUNCHER / "src-tauri" / "src" / "about.rs").read_text(encoding="utf-8")
    for name in used:
        assert re.search(rf"#\[tauri::command\]\s*(?:pub\s+)?(?:async\s+)?fn {name}\b", rs_all), name


# --------------------------------------------------------------------------- RC3: window name, CSP, capability, page structure
SRC = LAUNCHER / "src-tauri"
EXPECTED_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src data:; connect-src ipc: http://ipc.localhost; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'"
)


def _handler_commands() -> list[str]:
    rs = MAIN_RS.read_text(encoding="utf-8")
    body = re.search(r"generate_handler!\[(.*?)\]", rs, re.S).group(1)
    return [c.strip().split("::")[-1] for c in body.split(",") if c.strip()]


def test_the_csp_is_the_exact_restrictive_policy():
    sec = _conf()["app"]["security"]
    assert sec["csp"] == EXPECTED_CSP
    directives = {d.split()[0]: d.split()[1:] for d in sec["csp"].split(";") if d.strip()}
    assert directives["default-src"] == ["'none'"]
    assert directives["script-src"] == ["'self'"] and directives["style-src"] == ["'self'"]
    assert directives["connect-src"] == ["ipc:", "http://ipc.localhost"]
    assert directives["base-uri"] == ["'none'"] and directives["form-action"] == ["'none'"]
    assert directives["frame-ancestors"] == ["'none'"] and directives["object-src"] == ["'none'"]
    for sources in directives.values():
        for banned in ("'unsafe-inline'", "'unsafe-eval'", "*", "https:", "http:", "blob:", "filesystem:", "ws:", "wss:"):
            assert banned not in sources, (banned, sources)
    assert "devCsp" not in sec and "dev-csp" not in sec, "a dev-only policy would let a looser one run in development builds"
    assert "dangerousDisableAssetCspModification" not in sec
    assert not sec.get("assetProtocol", {}).get("enable", False)
    assert sec["headers"]["X-Content-Type-Options"] == "nosniff"


def test_the_page_is_external_script_and_style_only():
    """No inline script/style/handler anywhere in ui/: the CSP has no 'unsafe-inline' to allow them."""
    for page in _ui_pages():
        html = page.read_text(encoding="utf-8")
        assert re.findall(r"<script\b[^>]*>", html) == ['<script src="app.js" defer>'], page
        assert re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S) == [""], "inline script body"
        assert "<style" not in html and not re.search(r"\sstyle\s*=", html), "inline style"
        assert not re.search(r"<[a-z][^>]*\son[a-z]+\s*=", html, re.I), "inline event handler"
        assert "javascript:" not in html and "<iframe" not in html and "<form" not in html and "<object" not in html
        assert not re.search(r"""(?:src|href|action)\s*=\s*["']\s*(?:https?:)?//""", html), "external reference"
        assert 'rel="stylesheet" href="app.css"' in html
    js = (UI / "app.js").read_text(encoding="utf-8")
    for banned in ("eval(", "new Function", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "javascript:", "http://", "https://", "fetch(", "XMLHttpRequest", "WebSocket", "window.open", "confirm(", "alert(", "prompt("):
        assert banned not in js, banned      # native dialogs are silent no-ops in the macOS launcher webview
    assert sorted(p.name for p in UI.iterdir()) == ["app.css", "app.js", "index.html"], "only these files may be served to the webview"


def test_window_and_page_are_named_agentic_apps_r0_prime_and_the_re_mark_is_not_used():
    ident = json.loads((LAUNCHER.parents[1] / "packaging" / "identity.json").read_text(encoding="utf-8"))["r0prime"]
    conf = _conf()
    assert conf["productName"] == ident["product_name"] == "Agentic Apps R0-prime"
    assert conf["app"]["windows"][0]["title"] == "Agentic Apps R0-prime"
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert "<title>Agentic Apps R0-prime</title>" in html and "<h1>Agentic Apps R0-prime</h1>" in html
    assert "ReDevOps" not in html and "ReDevOps" not in (UI / "app.js").read_text(encoding="utf-8")
    assert "Private candidate, release line r0-prime." in html
    # D-LIC-11 (unresolved): no logo or icon reference anywhere in the launcher page; only an empty data: favicon
    page_text = html + (UI / "app.css").read_text(encoding="utf-8") + (UI / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"<img|<svg|\.png|\.ico|\.icns|\.svg|url\(", page_text, re.I)
    assert '<link rel="icon" href="data:," />' in html


def test_capability_grants_exactly_the_apps_own_commands_and_nothing_else():
    cap = json.loads((SRC / "capabilities" / "default.json").read_text(encoding="utf-8"))
    assert sorted(p.name for p in (SRC / "capabilities").iterdir()) == ["default.json"], "one capability file"
    assert cap["windows"] == ["main"] and "webviews" not in cap and "remote" not in cap and "platforms" not in cap
    perms = cap["permissions"]
    assert all(isinstance(p, str) for p in perms), "no scoped (object) permission"
    assert not [p for p in perms if ":" in p or not p.startswith("allow-")], perms
    commands = _handler_commands()
    assert sorted(perms) == sorted("allow-" + c.replace("_", "-") for c in commands)
    # the generated per-command permissions come from build.rs; that list must equal the handler list
    build_rs = (SRC / "build.rs").read_text(encoding="utf-8")
    listed = re.findall(r'"([a-z_]+)",', re.search(r"APP_COMMANDS: &\[&str\] = &\[(.*?)\];", build_rs, re.S).group(1))
    assert sorted(listed) == sorted(commands)
    assert "app_manifest(" in build_rs and ".commands(APP_COMMANDS)" in build_rs


def test_no_privileged_plugin_is_a_dependency_or_is_registered():
    cargo = (SRC / "Cargo.toml").read_text(encoding="utf-8")
    assert not re.search(r"^tauri-plugin-[a-z-]+\s*=", cargo, re.M), "no tauri plugin crate (shell, fs, http, opener, ...)"
    assert 'tauri = { version = "2", features = [] }' in cargo, "no extra tauri features (protocol-asset, devtools, ...)"
    main = MAIN_RS.read_text(encoding="utf-8")
    plugins = re.findall(r"\.plugin\(([\w:]+\(\))\)", main.split("#[cfg(test)]")[0])
    assert plugins == ["guard::plugin()"], plugins
    perms = " ".join(json.loads((SRC / "capabilities" / "default.json").read_text(encoding="utf-8"))["permissions"])
    for word in ("shell", "fs:", "http:", "opener", "dialog", "core:", "process", "updater", "global-shortcut", "clipboard"):
        assert word not in perms, word


def test_the_navigation_guard_is_installed_and_the_page_has_no_links():
    guard = (SRC / "src" / "guard.rs").read_text(encoding="utf-8")
    assert ".on_navigation(" in guard and '"tauri" => url.host_str() == Some("localhost")' in guard
    assert "<a " not in (UI / "index.html").read_text(encoding="utf-8")


def test_the_guard_decides_per_os_and_does_not_allow_about_blank():
    guard = (SRC / "src" / "guard.rs").read_text(encoding="utf-8").split("#[cfg(test)]")[0]
    assert "pub fn navigation_allowed_on(url: &Url, windows: bool)" in guard
    assert '"http" | "https" => windows && url.host_str() == Some("tauri.localhost")' in guard
    assert '"about"' not in guard, "about:blank is refused"
    assert "cfg!(windows)" in guard


def test_posix_signals_are_turned_into_a_graceful_exit_and_the_handler_only_writes():
    main = MAIN_RS.read_text(encoding="utf-8").split("#[cfg(test)]")[0]
    assert "#[cfg(unix)]\nmod signals;" in main and "signals::start(" in main and "handle.exit(0)" in main
    sig = (SRC / "src" / "signals.rs").read_text(encoding="utf-8")
    assert "[libc::SIGTERM, libc::SIGINT, libc::SIGHUP]" in sig and "SIGKILL" not in sig.split("#[cfg(test)]")[0].replace("SIGKILL cannot be handled", "").replace("SIGKILL (and", "")
    cargo = (SRC / "Cargo.toml").read_text(encoding="utf-8")
    assert '[target.\'cfg(unix)\'.dependencies]\nlibc = "0.2"' in cargo       # already there: no new dependency
