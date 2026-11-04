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


def test_ui_uses_the_global_tauri_object():
    # If this ever stops being true (UI moves to bundled @tauri-apps/api imports) the next test
    # becomes optional; until then it is the thing that needs the flag.
    assert any("window.__TAURI__" in p.read_text(encoding="utf-8") for p in _ui_pages())


def test_with_global_tauri_is_enabled_whenever_the_ui_uses_window_tauri():
    uses_global = any("window.__TAURI__" in p.read_text(encoding="utf-8") for p in _ui_pages())
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
    html = "\n".join(p.read_text(encoding="utf-8") for p in _ui_pages())
    used = set(re.findall(r'(?:invoke|run)\(\s*"([a-z_]+)"', html))
    assert {"device_report", "start_native", "stop_native", "docker_available"} <= used
    rs = MAIN_RS.read_text(encoding="utf-8")
    handler = re.search(r"generate_handler!\[(.*?)\]", rs, re.S).group(1)
    registered = set(re.findall(r"[a-z_]+", handler))
    assert used <= registered, f"UI invokes unregistered commands: {used - registered}"
    for name in used:
        assert re.search(rf"#\[tauri::command\]\s*(?:async\s+)?fn {name}\b", rs), name
