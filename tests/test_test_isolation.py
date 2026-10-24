"""The suite must not depend on the developer's exported credentials. A developer who has real OAuth client credentials in their shell used to see the Projects API connect tests fail (the app correctly
refuses the simulated Connect flow once hosted OAuth is configured); conftest now clears them and resets the app's cached connector before every test."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_exported_oauth_credentials_do_not_change_the_projects_api_connect_tests():
    env = {**os.environ, "GOOGLE_OAUTH_CLIENT_ID": "exported-id", "GOOGLE_OAUTH_CLIENT_SECRET": "exported-secret", "SLACK_CLIENT_ID": "x", "SLACK_CLIENT_SECRET": "y",
           "PYTHONPATH": f"{ROOT}{os.pathsep}{ROOT / 'apps'}"}
    env.pop("AGENTIC_OS_APPROVAL_SECRET", None)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(ROOT / "tests" / "test_projects_api.py"), "-k", "connect"],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-1500:]


def test_the_fixture_clears_the_credentials_and_resets_the_cached_connector(monkeypatch):
    import agentic_os.projects_api as pa
    from agentic_os.integrations.hosted_oauth import KNOWN_OAUTH
    assert pa._HOSTED is None and pa._HOSTED_TRIED is False and pa._HOSTED_BROKEN is False
    assert all(not os.environ.get(s["env_id"]) and not os.environ.get(s["env_secret"]) for s in KNOWN_OAUTH.values())


# The next two tests only prove the cache reset when they run in this order (the default); in any other order the second still passes, it just proves less.
def test_a_test_that_builds_the_cached_connector_leaves_it_cached_for_itself(monkeypatch):
    import agentic_os.projects_api as pa
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id-set-by-this-test")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret-set-by-this-test")
    pa._get_hosted()
    assert pa._HOSTED_TRIED is True


def test_and_the_next_test_starts_with_the_connector_cache_reset():
    import agentic_os.projects_api as pa
    assert pa._HOSTED_TRIED is False and pa._HOSTED is None and pa._HOSTED_BROKEN is False
