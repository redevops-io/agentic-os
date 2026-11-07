"""Navigation helper for the browser tests of the vendored Projects UI.

The UI has five places and Settings (Today, Work, Activity, Automations, Connections, Settings) with a section row inside a place; the tests
were written for one rail button per view. ``go_view`` maps the old view names onto place + section so the same checks drive the current UI.
"""
from __future__ import annotations

import re

# old view name -> (rail button, section button inside that place or None when the place has one section)
PLACES = {
    "Today": ("Today", None),
    "Overview": ("Work", "Summary"),
    "Missions": ("Work", "Missions"),
    "Attention": ("Work", "Attention"),
    "Discovery": ("Work", "Discovery"),
    "Activity": ("Activity", None),
    "Workflows": ("Automations", None),
    "Apps": ("Connections", "Apps"),
    "Sources": ("Connections", "Data sources"),
    "Settings": ("Settings", "General"),
    "Sidekick": ("Settings", "Sidekick"),
}


def go_view(page, name: str) -> None:
    dest, sub = PLACES[name]
    btn = page.locator("button.nav").filter(has=page.locator("span.lbl", has_text=re.compile(f"^{dest}$")))
    btn.scroll_into_view_if_needed()
    btn.click()
    if sub:
        page.get_by_role("navigation", name=f"{dest} sections").get_by_role("button", name=sub, exact=True).click()
    page.wait_for_function("() => !document.body.innerText.includes('Loading…')")


def open_panel(page) -> None:
    """Open the Sidekick panel the way the top bar does: the bar is itself the input, and submitting it empty opens the panel."""
    box = page.locator(".topbar .ask input")
    box.focus()
    box.press("Enter")
