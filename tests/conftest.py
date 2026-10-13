"""Test-suite fixtures.

Keep the suite deterministic + offline: the World Adapter registry prefers a real OSS core whenever its
env creds are present + reachable, and those creds may be exported in the developer's environment (to wire a
live demo). Clear them before every test so projections always use the in-memory demo store — tests that
exercise a real adapter opt back in with an explicit ``monkeypatch.setenv``.
"""
import pytest

_CORE_ENV = ("TWENTY_BASE_URL", "TWENTY_API_KEY", "LAGO_API_URL", "LAGO_API_KEY",
             "CHATWOOT_BASE_URL", "CHATWOOT_API_TOKEN", "CHATWOOT_ACCOUNT_ID")


@pytest.fixture(autouse=True)
def _no_live_cores(monkeypatch):
    for e in _CORE_ENV:
        monkeypatch.delenv(e, raising=False)
    yield


# ── hygiene guard: the suite must never rewrite the vendored UI bundle ─────────────────────────────────────────
# `agentic_os/projects_ui/` is a TRACKED, vendored build artefact (scripts/build_projects_ui.sh regenerates it).
# Tests only read it. If any test/fixture starts building into it, fail the run loudly instead of silently
# dirtying `git status` (CI also runs `git diff --exit-code` + an untracked-files check as a second guard).
import hashlib  # noqa: E402
from pathlib import Path  # noqa: E402

_UI_DIR = Path(__file__).resolve().parents[1] / "agentic_os" / "projects_ui"


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    if root.is_dir():
        for p in sorted(root.rglob("*")):
            if p.is_file():
                h.update(p.relative_to(root).as_posix().encode())
                h.update(p.read_bytes())
    return h.hexdigest()


def pytest_sessionstart(session):
    session.config._ui_digest_before = _tree_digest(_UI_DIR)


def pytest_sessionfinish(session, exitstatus):
    before = getattr(session.config, "_ui_digest_before", None)
    if before is not None and _tree_digest(_UI_DIR) != before:
        print("\nHYGIENE FAILURE: the test run modified agentic_os/projects_ui/ (tracked vendored UI). "
              "Tests must write to tmp dirs; rebuild the UI only via scripts/build_projects_ui.sh.")
        session.exitstatus = 1
