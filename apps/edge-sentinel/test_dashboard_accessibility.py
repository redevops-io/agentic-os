"""UI audit 2026-11-11: the four dashboard findings, pinned so they cannot silently return.

The audit measured these in a real browser (`scripts/ui_audit_edge_sentinel.py`); these tests are the cheap
half of that contract. They assert on the markup and CSS the dashboard actually emits, because a CSS rule
deleted in an unrelated refactor is how a fixed layout finding comes back — and because the browser harness
is not part of the unit suite. What they cannot do is prove a pixel never overflows at a given width: that
is what re-running the harness with `UI_AUDIT_EXTRA_VIEWPORTS=reflow-320=320x568` is for.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def page(load_app) -> str:
    return TestClient(load_app("edge-sentinel").app).get("/").text


# ── finding: no main landmark (axe `region`; h1=1, main=0) ───────────────────────────────────────────

def test_the_dashboard_exposes_exactly_one_main_landmark(page: str) -> None:
    assert page.count("<main") == 1, "a screen reader needs exactly one place to land"
    assert page.count("</main>") == 1, "the landmark must be balanced, not merely opened"


def test_the_main_landmark_wraps_the_content_and_not_the_footer(page: str) -> None:
    assert page.index("<main") < page.index("</main>") < page.index("<footer"), \
        "everything above the footer is main content; the footer is not"


# ── finding: two tables with no caption or accessible name (SC 1.3.1) ───────────────────────────────

def test_every_data_table_is_named(page: str) -> None:
    unnamed = [t for t in re.findall(r"<table\b[^>]*>", page)
               if "aria-label" not in t and "aria-labelledby" not in t and "<caption" not in t]
    assert not unnamed, f"tables a screen reader would announce as unnamed: {unnamed}"


def test_the_two_audit_named_tables_are_among_the_named_ones(page: str) -> None:
    named = re.findall(r"<table\b[^>]*aria-label='([^']+)'", page)
    assert len(named) >= 2, f"expected the attack-sources and cases tables to be named, got {named}"
    assert any("Attack sources" in n for n in named) and any("case" in n.lower() for n in named)


# ── findings: horizontal overflow at 834px (cards) and 390px (six-column table) ─────────────────────

def _css(page: str) -> str:
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", page, re.S)) or page


def test_the_table_layout_rule_that_removed_the_floor_is_present(page: str) -> None:
    css = _css(page)
    assert re.search(r"\.table\{[^}]*table-layout:fixed", css), \
        "automatic table layout imposes its min-content width on every ancestor; that was the 947px floor"
    assert re.search(r"\.table (?:th|td)[^{]*\{[^}]*overflow-wrap:anywhere", css), \
        "fixed layout without wrapping clips an unbreakable token instead of wrapping it"


def test_the_scenario_bars_can_shrink_below_their_fixed_column(page: str) -> None:
    css = _css(page)
    assert "@media(max-width:560px)" in css, "the 200px barlist label column needs a narrow-width rule"
    narrow = css.split("@media(max-width:560px)", 1)[1]
    assert "minmax(0,1fr)" in narrow and "grid-column:1/-2" in narrow, \
        "under 561px the label takes the width and the bar moves to its own line"


def test_a_wide_table_gets_a_focusable_scroll_container(page: str) -> None:
    css = _css(page)
    assert re.search(r"\.table-scroll\{[^}]*overflow-x:auto", css), \
        "the six columns keep a readable width by scrolling in their own card, not by widening the page"
    wrappers = re.findall(r"<div class='table-scroll'[^>]*>", page)
    assert wrappers, "the panels are expected to emit scroll wrappers"
    # a scrollable region a keyboard user cannot reach would be a new finding, not a fix
    assert all("tabindex='0'" in w for w in wrappers), f"scrollable regions must be focusable: {wrappers}"


def test_the_plane_panel_tables_are_named_too(load_app) -> None:
    """The audit counted two unnamed tables on a *panel* surface, not on the dashboard cards — which is why
    naming the two card tables did not clear the finding. This test covers the surface that had the defect."""
    client = TestClient(load_app("edge-sentinel").app)
    for route in ("/api/network/ui", "/api/backups/ui"):
        body = client.get(route).text
        unnamed = [t for t in re.findall(r"<table\b[^>]*>", body)
                   if "aria-label" not in t and "aria-labelledby" not in t and "<caption" not in t]
        assert not unnamed, f"{route} renders tables a screen reader cannot name: {unnamed}"


def test_the_shared_table_helper_cannot_be_called_without_a_name() -> None:
    import importlib
    es = importlib.import_module("edge-sentinel.app")
    with pytest.raises(TypeError):
        es._tbl(["One"], [["1"]])                                     # noqa: SLF001 - the point of the test
    assert "aria-label='Named'" in es._tbl(["One"], [["1"]], label="Named")   # noqa: SLF001


def test_the_wrappers_are_balanced(page: str) -> None:
    assert page.count("<div class='table-scroll'") == page.count("</table></div>"), \
        "every wrapper opened before a table has to close after it, or the card layout silently breaks"
