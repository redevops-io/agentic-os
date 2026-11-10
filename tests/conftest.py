"""Test-suite fixtures.

Keep the suite deterministic + offline: the World Adapter registry prefers a real OSS core whenever its
env creds are present + reachable, and those creds may be exported in the developer's environment (to wire a
live demo). Clear them before every test so projections always use the in-memory demo store — tests that
exercise a real adapter opt back in with an explicit ``monkeypatch.setenv``.
"""
import os
import re
from pathlib import Path

import pytest

# --- the session must run against THIS worktree's code (tests/_env_guard.py, tests/test_env_guard.py) --------------------------------------------------
# An environment with agentic_os installed from ANOTHER worktree passes in-process tests (pytest puts this repository on sys.path) and fails the ones that start a
# subprocess. Resolve agentic_os the way such a subprocess does, and stop with both paths and the fix when it is not inside this repository.
# Only applies to the repository's own tests/ (tests/test_live_guard.py runs a copy of this file in a temp directory).
_ROOT = Path(__file__).resolve().parent.parent
_IS_REPO_TESTS = (_ROOT / "pyproject.toml").is_file() and (_ROOT / "agentic_os").is_dir() and Path(__file__).resolve().parent.name == "tests"

# --- live tests are opt-in (R0-prime): an ambient credential alone never triggers a live call ------------------------
# Tests marked ``live`` (real provider/bot/infrastructure calls) run only with REDEVOPS_ALLOW_LIVE_TESTS=1. Unless that
# variable is set, every credential-looking environment variable (API keys, tokens, secrets, passwords, SSH agent socket)
# is also removed from the process for the whole session, so no test or subprocess it starts can pick one up by accident.
# Tests that need a value set it themselves (monkeypatch.setenv after this runs).
ALLOW_LIVE = os.environ.get("REDEVOPS_ALLOW_LIVE_TESTS") == "1"
_CRED_NAME = re.compile(r"(API_KEY|API_TOKEN|_TOKEN|TOKEN|_SECRET|SECRET_KEY|SECRET_ACCESS_KEY|ACCESS_KEY|PASSWORD|CREDENTIALS?|PRIVATE_KEY|API_KEY_ID)$"
                        r"|^(SSH_AUTH_SOCK|SSH_AGENT_PID|APPROVAL_SECRET)$", re.I)
_HIDDEN: dict = {}


def pytest_configure(config):
    if _IS_REPO_TESTS:
        from tests import _env_guard
        try:
            _env_guard.check_session(_ROOT)
        except AssertionError as e:
            raise pytest.UsageError(str(e)) from None
    config.addinivalue_line("markers", "live: calls a real external service or infrastructure; runs only with REDEVOPS_ALLOW_LIVE_TESTS=1")
    if not ALLOW_LIVE:
        for name in [n for n in os.environ if _CRED_NAME.search(n)]:
            _HIDDEN[name] = os.environ.pop(name)


def pytest_unconfigure(config):
    os.environ.update(_HIDDEN)
    _HIDDEN.clear()


def pytest_report_header(config):
    return ("live tests: " + ("ENABLED (REDEVOPS_ALLOW_LIVE_TESTS=1)" if ALLOW_LIVE else
            f"disabled (set REDEVOPS_ALLOW_LIVE_TESTS=1); {len(_HIDDEN)} credential-like environment variable(s) hidden from the session (names not shown)"))


def pytest_collection_modifyitems(config, items):
    if ALLOW_LIVE:
        return
    skip = pytest.mark.skip(reason="live test: set REDEVOPS_ALLOW_LIVE_TESTS=1 (and its own credentials) to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


# ── descriptor guard: a test must not close or replace a file descriptor it does not own ─────────────────────────────────────────────
# pytest's faulthandler plugin saves a duplicate of stderr in a descriptor of its own and closes it in pytest_unconfigure, after the last test. A test that
# dup2-ed over that number and closed it (tests/test_signing_procedure_r0prime.py did, with fixed numbers 9, 12, 20, ...) made the run end with
# "OSError: [Errno 9] Bad file descriptor" and exit status 1 although every test passed, with nothing naming the test. This fixture compares, per test, the identity
# of that descriptor and of fd 2 before the test's setup and after its last teardown (it is the first autouse fixture, so it is finalized last), and fails THAT test.
# It restores nothing, hides nothing and does not change how the session ends (pytest's own unconfigure still reports a lost descriptor). Cost: a few fstat calls per test.
def _fd_identity(fd: int):
    try:
        st = os.fstat(fd)
    except OSError as e:
        return ("closed", e.errno)
    return (st.st_dev, st.st_ino, st.st_mode, st.st_rdev)


_FD_ALREADY_REPORTED: set = set()          # a descriptor whose loss has been reported is not watched again: later tests may legitimately reuse its number


@pytest.fixture(autouse=True)
def _fd_integrity_guard(request):
    watched = [2]
    try:
        from _pytest.faulthandler import fault_handler_stderr_fd_key
        saved = request.config.stash.get(fault_handler_stderr_fd_key, None)      # absent under ``-p no:faulthandler``
        if isinstance(saved, int):
            watched.append(saved)
    except ImportError:                                                         # a pytest without that plugin layout: fd 2 only
        pass
    watched = [fd for fd in watched if fd not in _FD_ALREADY_REPORTED]
    before = {fd: _fd_identity(fd) for fd in watched}
    yield
    after = {fd: _fd_identity(fd) for fd in watched}
    changed = {fd: (before[fd], after[fd]) for fd in watched if after[fd] != before[fd]}
    if changed:
        _FD_ALREADY_REPORTED.update(changed)
        pytest.fail("this test closed or replaced a file descriptor it does not own: " + "; ".join(
            f"fd {fd}{' (stderr)' if fd == 2 else ' (the copy of stderr that pytest saved for faulthandler)'}: {b} -> {a}" for fd, (b, a) in changed.items())
            + ". Use descriptors you created yourself (os.dup, fcntl F_DUPFD) or do it in a child process; see tests/test_fd_guard.py", pytrace=False)


_CORE_ENV = ("TWENTY_BASE_URL", "TWENTY_API_KEY", "LAGO_API_URL", "LAGO_API_KEY",
             "CHATWOOT_BASE_URL", "CHATWOOT_API_TOKEN", "CHATWOOT_ACCOUNT_ID")


@pytest.fixture(autouse=True)
def _no_live_cores(monkeypatch):
    for e in _CORE_ENV:
        monkeypatch.delenv(e, raising=False)
    yield


@pytest.fixture(autouse=True)
def _explicit_test_authority_policy(monkeypatch):
    """Side effects are refused unless the owner's envelope covers them or the runtime was given an explicit, named test policy
    (agentic_os/mission/authority_policy.py); there is no silent "development allows" default. The suite builds thousands of
    runtimes without an envelope, so it declares ONE named policy here, recorded in every mission log it touches
    (``TestPolicyActive``). Tests of the fail-closed behaviour use the ``no_test_policy`` fixture."""
    from agentic_os.mission.authority_policy import AuthorityTestPolicy
    from agentic_os.mission.runtime import MissionRuntime
    monkeypatch.setattr(MissionRuntime, "test_policy", AuthorityTestPolicy.allow_all(
        "pytest-suite-default", "the test suite builds runtimes without owner envelopes; this named policy is its declaration"))
    yield


@pytest.fixture
def no_test_policy(monkeypatch):
    """Remove the suite's default test policy: the runtime then behaves as an unconfigured deployment (fail closed)."""
    from agentic_os.mission.runtime import MissionRuntime
    monkeypatch.setattr(MissionRuntime, "test_policy", None)
    yield


@pytest.fixture(autouse=True)
def _permissions_dev_key(monkeypatch):
    """The grant store never generates a key implicitly: it needs PERMISSIONS_KEY or an existing key file. The suite
    builds many throw-away stores in temp directories, so the TEST ENVIRONMENT opts into the explicit development-only
    key generation. Tests of the fail-closed default (tests/test_permissions_grant_storage.py) clear this."""
    monkeypatch.setenv("PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "1")
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
    """An operator's /invoke runs a side-effecting capability only for an authenticated identity with current authority and the applicable
    approval (agentic_os/mission/invoke_gate.py); there is no "development allows" default. The suite's handler-level tests call /invoke
    without envelopes, so they declare ONE explicit, named test policy here (with the credential-less caller accepted as its named test
    identity, and the mandatory approval waived because those tests drive the handlers' own staging). It is recorded in every audit
    record they produce. Tests of the real behaviour use the ``no_operator_test_policy`` fixture."""
    from agentic_os.mission.authority_policy import AuthorityTestPolicy
    from agentic_os.mission.operator_sdk import Operator
    monkeypatch.setenv("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", "1")
    monkeypatch.setattr(Operator, "test_policy", AuthorityTestPolicy.allow_all(
        "pytest-operator-default", "the suite's handler-level tests call /invoke without owner envelopes; this named policy is its declaration",
        anonymous_operators=True, unapproved=True))
    yield


@pytest.fixture
def no_operator_test_policy(monkeypatch):
    """Remove the suite's default operator test policy: /invoke then behaves as an unconfigured deployment (fail closed)."""
    from agentic_os.mission.operator_sdk import Operator
    monkeypatch.setattr(Operator, "test_policy", None)
    monkeypatch.delenv("AGENTIC_OS_OPERATOR_ALLOW_ANONYMOUS", raising=False)
    yield


@pytest.fixture(autouse=True)
def _app_gate_state_is_per_test(monkeypatch):
    """The gate registries are process-global (an app registers its gate at import). A test that installs one must not leak it into the next test: every test starts and ends clean."""
    from agentic_os import app_gate
    app_gate.reset_state()
    yield
    app_gate.reset_state()


@pytest.fixture(autouse=True)
def _isolate_source_registration_audit(monkeypatch, tmp_path_factory):
    """The desktop sidecar audits every folder registration under the per-user application-data directory (agentic_os/desktop_sources.py). No test may
    write there: every test gets its own audit location, outside its ``tmp_path``. A test of the default location passes explicit arguments."""
    d = tmp_path_factory.mktemp("srcaudit")
    monkeypatch.setenv("AGENTIC_OS_SOURCES_AUDIT_PATH", str(d / "audit" / "source-registrations.jsonl"))
    yield
