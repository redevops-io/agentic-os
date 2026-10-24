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


@pytest.fixture(autouse=True)
def _no_hosted_oauth(monkeypatch):
    """The Projects API builds its hosted-OAuth connector ONCE from the environment and caches it in module globals. A developer who has exported real OAuth client credentials (to wire a live
    demo) would then see the simulated Connect flow refused, which is the right behaviour but not what these tests are about. Clear the credentials and reset the cache before every test; a test
    that exercises hosted OAuth sets them itself."""
    import sys
    try:
        from agentic_os.integrations.hosted_oauth import KNOWN_OAUTH
        for spec in KNOWN_OAUTH.values():
            monkeypatch.delenv(spec["env_id"], raising=False)
            monkeypatch.delenv(spec["env_secret"], raising=False)
    except Exception:  # the connector plugin may not be installed: nothing to clear
        pass
    mod = sys.modules.get("agentic_os.projects_api")
    if mod is not None:
        monkeypatch.setattr(mod, "_HOSTED", None, raising=False)
        monkeypatch.setattr(mod, "_HOSTED_TRIED", False, raising=False)
        monkeypatch.setattr(mod, "_HOSTED_BROKEN", False, raising=False)
    yield


# ── hygiene guard: the suite must never rewrite the vendored UI bundle ─────────────────────────────────────────
# `agentic_os/projects_ui/` is a git-ignored build artefact fetched from the pinned redevops-projects release
# (scripts/fetch_projects_ui.py; scripts/build_projects_ui.sh builds one from a local clone). Tests only read it. If any test/fixture starts building into it, fail the run loudly instead of silently
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
        print("\nHYGIENE FAILURE: the test run modified agentic_os/projects_ui/ (fetched UI bundle). "
              "Tests must write to tmp dirs; refresh the UI only via scripts/fetch_projects_ui.py.")
        session.exitstatus = 1


@pytest.fixture(autouse=True)
def _isolate_platform_secrets(monkeypatch, tmp_path):
    """Never read/write the developer's real secrets file, rc files or keyring (docs/SECRETS.md)."""
    monkeypatch.setenv("REDEVOPS_SECRETS_AUTO", "0")
    monkeypatch.setenv("REDEVOPS_SECRETS_FILE", str(tmp_path / "no-such-dir" / "secrets.env"))
    monkeypatch.setenv("REDEVOPS_SECRETS_NO_KEYRING", "1")
    for e in ("VAULT_ADDR", "VAULT_TOKEN", "AGENTIC_OS_APPROVAL_SECRET"):
        monkeypatch.delenv(e, raising=False)
    yield


@pytest.fixture(autouse=True)
def _operator_invoke_open_for_legacy_tests(monkeypatch):
    """Operator /invoke fails closed without a credential; pre-existing handler tests exercise it directly, so
    they opt into the documented dev flag. Auth tests clear it explicitly."""
    monkeypatch.setenv("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", "1")
    # F21: `approval_required` is enforced on every /invoke path in every profile. These handler-level tests drive the
    # handlers directly (including their own staging), so they opt into the explicit, non-production testing override.
    # The default behaviour has its own tests (tests/test_invoke_approval_required.py) which clear it.
    monkeypatch.setenv("AGENTIC_OS_ALLOW_UNAPPROVED_INVOKE", "1")
    yield
