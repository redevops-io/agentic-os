"""Static AST conformance lints over apps/*/*.py (non-test source). See docs/CONFORMANCE.md.

Each lint passes TODAY via an explicit, shrinking ALLOWLIST ``{(rule, "apps/<app>/<file>.py"): "reason (audit id)"}``
of known offenders, so it blocks NEW regressions without failing on existing debt. An allowlist entry that no
longer matches (the offender was fixed) only emits a ``StaleAllowlist`` warning -- delete the entry.
"""
from __future__ import annotations

import inspect
import warnings
from pathlib import Path

import pytest

import _conf_lints as L
from _conf_apps import OPERATOR_APPS, build_operator

ROOT = Path(__file__).resolve().parents[2]
APP_SOURCES = sorted(p for p in (ROOT / "apps").rglob("*.py")
                     if not p.name.startswith("test_") and p.name != "conftest.py"
                     and "__pycache__" not in p.parts)


class StaleAllowlist(UserWarning):
    pass


def _rel(p: Path) -> str:
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:  # synthetic files in unit tests
        return p.as_posix()


# ── allowlists (shrink only; every entry: short reason + audit id) ─────────────────────────────────────────────
_N7 = "direct model client in app (should route via the governed model gateway) -- round-4 audit N7"
ALLOW_N7 = {("N7", f"apps/{a}"): _N7 for a in (
    "agentic-privacy/app.py", "billing/app.py", "books/app.py", "compliance/app.py",
    "edge-sentinel/app.py", "growth-assistant/app.py", "growth-engine/app.py", "guide/app.py",
    "market-radar/app.py")}
_N9 = "hard-coded 'lift' benchmark banner in _CR_BANNER (must come from measured data) -- round-4 audit N9"
ALLOW_N9 = {("N9", f"apps/{a}"): f"{_N9} [{v}]" for a, v in (
    ("billing/app.py", "4.12"), ("books/app.py", "3.63"),
    ("compliance/app.py", "3.56"), ("control-tower/app.py", "5.33"), ("market-radar/app.py", "3.61"),
    ("growth-engine/app.py", "7.85"), ("social-autopilot/app.py", "3.88"))}
ALLOW_N5: dict[tuple[str, str], str] = {}          # no offenders today
ALLOW_D1 = {("D1", "apps/guide/app.py"): "guide route reads request.json() without _strip_approval -- round-4 audit approval-forgery"}
ALLOW_D2: dict[tuple[str, str], str] = {}
_F = "default credential literal in source (fail closed / require env) -- round-4 audit secrets"
ALLOW_F = {("F", f"apps/{a}"): _F for a in (
    "growth-engine/core.py", "growth-engine/seed.py", "social-autopilot/core.py", "social-autopilot/seed.py")}
ALLOW_N8 = {  # (app, capability) -> reason
    ("billing", "billing.refund"): "delegates to core.stage_refund (stage-only); gate lives in core -- round-4 audit N8",
    ("books", "books.close"): "delegates to core.close; gate lives in core -- round-4 audit N8",
    ("compliance", "compliance.remediate"): "delegates to core.remediate; gate lives in core -- round-4 audit N8",
    ("growth-engine", "growth.reallocate_budget"): "delegates to core.reallocate_budget; gate lives in core -- round-4 audit N8",
    ("social-autopilot", "social.publish"): "delegates to core.publish; gate lives in core -- round-4 audit N8",
}


def _check(lint, allow: dict, rule: str, files=APP_SOURCES) -> None:
    bad = {_rel(p): v for p in files if (v := lint(p.read_text()))}
    new = {f: v for f, v in bad.items() if (rule, f) not in allow}
    for (r, path), why in allow.items():
        if r == rule and path not in bad:
            warnings.warn(f"stale allowlist entry ({rule}, {path}): no longer offends -- remove it [{why}]",
                          StaleAllowlist, stacklevel=2)
    assert not new, f"{rule}: NEW violations (fix them, do not allowlist):\n" + "\n".join(
        f"  {f}: " + "; ".join(str(x) for x in v[:3]) for f, v in new.items())


def test_n7_no_direct_model_clients():
    _check(L.lint_n7_direct_model_clients, ALLOW_N7, "N7")


def test_n5_no_literal_reward_in_record_outcome():
    _check(L.lint_n5_literal_reward, ALLOW_N5, "N5")


def test_n9_no_hardcoded_benchmark_banners():
    _check(L.lint_n9_hardcoded_benchmark, ALLOW_N9, "N9")


def test_approval_forgery_raw_request_json_is_stripped():
    _check(L.lint_d_raw_request_json, ALLOW_D1, "D1")


def test_approval_forgery_routes_never_read_approval_marker():
    _check(L.lint_d_route_reads_approval, ALLOW_D2, "D2")


def test_secrets_no_default_credential_literals():
    _check(L.lint_f_default_credentials, ALLOW_F, "F")


@pytest.mark.parametrize("app", OPERATOR_APPS)
def test_n8_gated_capabilities_consult_the_approval_marker(app):
    try:
        op = build_operator(app)
    except ImportError as e:
        pytest.skip(f"{app}: operator needs an uninstalled package ({e})")
    offenders = []
    for name, spec in op._specs.items():
        if not spec.approval_required:
            continue
        try:
            src = inspect.getsource(op._handlers[name])
        except (OSError, TypeError):
            src = ""
        if L.handler_mentions_approval(src):
            if (app, name) in ALLOW_N8:
                warnings.warn(f"stale allowlist entry N8 ({app}, {name}): handler now mentions approval -- remove it",
                              StaleAllowlist)
        elif (app, name) not in ALLOW_N8:
            offenders.append(name)
    assert not offenders, f"{app}: approval_required capability handlers that never mention _approval/pending_approval: {offenders}"


def test_n8_allowlist_names_exist():
    apps = set(OPERATOR_APPS)
    for app, cap in ALLOW_N8:
        assert app in apps, f"N8 allowlist names unknown app {app}"


# ── prove each lint bites: synthetic offending + clean sources ─────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [
    'URL = "http://x/v1/chat/completions"',
    'u = f"https://api.openai.com/v1/{x}"',
    'requests.post("https://api.anthropic.com/v1/messages")',
    'from context_runtime.adapters.model_openai import OpenAICompatibleModel',
    'm = mod.OpenAICompatibleModel.from_env()',
    'c = AgentConsole(tools=[])',
])
def test_lint_n7_bites(bad):
    assert L.lint_n7_direct_model_clients(bad)


def test_lint_n7_clean():
    assert not L.lint_n7_direct_model_clients('"""docs mention /chat/completions"""\nx = gateway.complete(p)\n')


@pytest.mark.parametrize("bad", ["record_outcome(d, reward=1.0)", "ledger.record_outcome(d, 0.5)",
                                 "record_outcome(d, reward=-1)"])
def test_lint_n5_bites(bad):
    assert L.lint_n5_literal_reward(bad)


def test_lint_n5_clean():
    assert not L.lint_n5_literal_reward("record_outcome(d, reward=measured)\nrecord_outcome(d, accepted=True)")


def test_lint_n9_bites_and_clean():
    assert L.lint_n9_hardcoded_benchmark('B = "<b>lift 4.12 vs 2.44</b>"')
    assert L.lint_n9_hardcoded_benchmark('f"{x} 3.61 vs 0.40"')
    assert not L.lint_n9_hardcoded_benchmark('B = "version 1.2 vs 1.3"\nC = f"{a:.2f} vs {b:.2f}"')


def test_lint_d1_bites_and_clean():
    assert L.lint_d_raw_request_json("async def r(request):\n    b = await request.json()\n")
    assert not L.lint_d_raw_request_json("async def r(request):\n    b = _strip_approval(await request.json())\n")
    assert L.lint_d_raw_request_json("async def r(request):\n    raw = await request.json()\n    b = _strip_approval(raw)\n")


def test_lint_d2_bites_and_clean():
    bad = ("@app.post('/x')\nasync def r(request):\n    b = _strip_approval(await request.json())\n"
           "    return helper(b)\n\ndef helper(b):\n    return b.get('_approval')\n")
    assert L.lint_d_route_reads_approval(bad)
    direct = "@app.post('/x')\ndef r(b: dict):\n    return b['_approval']\n"
    assert L.lint_d_route_reads_approval(direct)
    ok = "def handler(inp):\n    return inp.get('_approval')\n\n@app.get('/x')\ndef r():\n    return 1\n"
    assert not L.lint_d_route_reads_approval(ok)


def test_lint_f_bites_and_clean():
    assert L.lint_f_default_credentials('P = os.environ.get("UMAMI_ADMIN_PASS", "umami")')
    assert L.lint_f_default_credentials('os.getenv("PG_USER", "postiz")')
    assert L.lint_f_default_credentials('S = "demo-only-change-me"')
    assert not L.lint_f_default_credentials('P = os.environ.get("UMAMI_ADMIN_PASS", "")\nx = {"core": "umami"}')


def test_lint_n8_helper():
    assert L.handler_mentions_approval("lambda i: x if i.get('_approval') else y")
    assert L.handler_mentions_approval("return pending_approval('a')")
    assert not L.handler_mentions_approval("lambda i: core.do(i)")


def test_allowlist_check_fails_on_a_new_offender_and_warns_on_stale(tmp_path):
    f = tmp_path / "x.py"
    f.write_text('U = "https://api.openai.com/v1"\n')
    with pytest.raises(AssertionError, match="NEW violations"):
        _check(L.lint_n7_direct_model_clients, {}, "N7", files=[f])
    _check(L.lint_n7_direct_model_clients, {("N7", f.as_posix()): "synthetic"}, "N7", files=[f])  # allowlisted: ok
    f.write_text("U = 1\n")
    with pytest.warns(StaleAllowlist):
        _check(L.lint_n7_direct_model_clients, {("N7", f.as_posix()): "synthetic"}, "N7", files=[f])
