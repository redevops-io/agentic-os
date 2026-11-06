"""RC3 UI accessibility (U-01, U-02, U-04, U-05, U-06, U-14): checks that need NO browser.

They read the vendored Projects UI (``agentic_os/projects_ui``) and the served page as text, so the default
``pytest`` run keeps the logic covered; ``test_ui_a11y_browser.py`` (opt-in, ``RDO_BROWSER_TESTS=1``) proves
the same things in a real Chromium with axe-core. Contrast is computed here with the WCAG formula from the
design tokens, for the light and the dark palette.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentic_os.local_capability import LocalCapability
from agentic_os.projects_api import SampleProjectionProvider, mounted_app

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "agentic_os" / "projects_ui"


def _asset(suffix: str) -> str:
    files = sorted((UI / "assets").glob(f"*.{suffix}"))
    assert len(files) == 1, files                                   # one hashed bundle per kind
    return files[0].read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js() -> str:
    return _asset("js")


@pytest.fixture(scope="module")
def css() -> str:
    return _asset("css")


# ----------------------------------------------------------------------------- contrast (U-06)
def _lum(h: str) -> float:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    ch = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(ch[0]) + 0.7152 * f(ch[1]) + 0.0722 * f(ch[2])


def ratio(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _tokens(block: str) -> dict:
    return dict(re.findall(r"--([a-z0-9-]+):(#[0-9a-fA-F]{3,6})", block))


def _palettes(css: str) -> dict:
    light = re.match(r":root\{([^}]*)\}", css).group(1)
    dark_media = re.search(r"prefers-color-scheme:\s*dark\)\{:root:not\(\[data-theme=light\]\)\{([^}]*)\}", css).group(1)
    dark_attr = re.search(r":root\[data-theme=dark\]\{([^}]*)\}", css).group(1)
    return {"light": _tokens(light), "dark (media)": _tokens(dark_media), "dark (attribute)": _tokens(dark_attr)}


# (foreground token, background token): every text pair the audit's 16 failures and the rest of the UI use
PAIRS = [
    ("muted", "bg"), ("muted", "panel"), ("muted", "panel-2"),                 # eyebrows, hints, .s, demoflag, tabs
    ("ok", "ok-wash"), ("warn", "warn-wash"), ("run", "run-wash"), ("bad", "bad-wash"),   # status pills
    ("accent-ink", "accent-wash"), ("accent-ink", "panel"), ("accent-ink", "bg"),
    ("on-accent", "accent-btn"),                                              # primary buttons, own chat bubbles
    ("on-warn", "warn"),                                                      # rail badges
    ("rail-muted", "rail"), ("rail-ink", "rail"),                             # rail captions, footer, About link
    ("ink", "warn-wash"),                                                     # the Sample data chip text
]


@pytest.mark.parametrize("theme", ["light", "dark (media)", "dark (attribute)"])
def test_design_tokens_meet_aa_for_every_text_pair(css, theme):
    pal = _palettes(css)[theme]
    bad = {f"{f} on {b}": round(ratio(pal[f], pal[b]), 2) for f, b in PAIRS if ratio(pal[f], pal[b]) < 4.5}
    assert not bad, bad


def test_the_two_dark_palettes_are_identical(css):
    p = _palettes(css)
    assert p["dark (media)"] == p["dark (attribute)"]


def test_no_hard_coded_low_contrast_colours_remain(css):
    assert "#5f7375" not in css and "opacity:.85" not in css          # the old rail caption/footer and the faded .s text


# ----------------------------------------------------------------------------- ARIA semantics (U-05)
def test_aria_selected_exists_only_on_role_tab_buttons(js):
    sites = [m.start() for m in re.finditer(r'"aria-selected"', js)]
    assert len(sites) == 1, "one shared Tabs component, not one site per view"
    window = js[max(0, sites[0] - 400): sites[0] + 200]
    assert 'role:"tab"' in window
    assert 'role:"tablist"' in js and 'role:"tabpanel"' in js
    assert js.count('"aria-pressed"') >= 2                             # filters and the add-source choices
    css = _asset("css")
    assert '.tabsrow button[aria-selected=true]' in css and 'aria-pressed=true' in css


# ----------------------------------------------------------------------------- keyboard (U-01)
def test_mission_rows_and_the_other_click_targets_are_native_buttons(js):
    assert js.count('className:"row rowbtn"') >= 3                      # Missions, Overview, Sidekick Recent
    assert js.count('"data-mission-id"') >= 1
    assert not re.search(r'"(div|span)",\{[^{}]*(className:"row"|className:"chiplink")[^{}]*onClick', js)   # no div/span with a click handler
    assert not re.search(r'"span",\{[^{}]*className:"chiplink"', js)     # chiplinks are buttons now
    assert re.search(r'"button",\{type:"button",className:"chiplink"', js)
    assert not re.search(r'"(div|span)",\{[^{}]*style:\{[^{}]*cursor:"pointer"', js)   # no pointer cursor on a div/span (labels stay)


# ----------------------------------------------------------------------------- Sidekick dialog (U-14)
def test_sidekick_is_a_dialog_with_a_name_and_the_closed_panel_is_hidden(js, css):
    assert 'role:"dialog"' in js and '"aria-modal":"true"' in js and '"aria-labelledby":"sk-title"' in js
    assert 'e.key==="Escape"' in js.replace(" ", "") or "Escape" in js
    assert re.search(r"\.sk\{[^}]*visibility:hidden", css) and re.search(r"\.sk\.open\{[^}]*visibility:visible", css)
    assert "inert" in js


# ----------------------------------------------------------------------------- sample data (U-04)
def test_sample_data_chip_and_no_live_claim(js, css):
    assert "Sample data" in js and ".samplechip" in css and "dashed" in css
    assert "Healthy (sample)" in js and "Secured (sample)" in js
    assert re.search(r'children:\[\w+\.project\.name,\w+\?"":" · live"\]', js), "the eyebrow is the project name"


# ----------------------------------------------------------------------------- About hook (U-03)
def test_about_link_hook_is_a_relative_path_in_the_rail(js):
    assert '"./about/"' in js and "About and licences" in js
    assert "href:" in js and "/about" not in re.sub(r'"\./about/"', "", js)      # never an absolute path


# ----------------------------------------------------------------------------- served page
def test_served_page_carries_the_shim_first_and_nothing_inline():
    cap = LocalCapability(port=8787)
    c = TestClient(mounted_app("", SampleProjectionProvider(), capability=cap), base_url="http://127.0.0.1:8787")
    html = c.get("/").text
    assert re.search(r"<head[^>]*>\s*<script src=\"/local-capability-shim\.js\"></script>", html)
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html) and not re.search(r"\son[a-z]+\s*=", html)
    assert "unsafe-inline" not in c.get("/").headers["content-security-policy"].split("script-src")[1].split(";")[0]
