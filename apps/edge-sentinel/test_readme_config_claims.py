"""P10: the README's configuration and model claims must match what the code does.

Audit item P10 found a documented environment variable (`ANTHROPIC_API_KEY`) that no file reads and a
model name (`claude-opus-4-8`) that appears nowhere, on a route that in fact goes through the governed
kernel. Those are not typos: an operator who set the key paid for a capability they never got, and the
sentence implied where security-relevant data went. This file pins three things so the class of bug
cannot return:

1. every variable the README documents is in `config_contract.json` and is actually read by the
   component the contract names (app or kernel) - no documented variable can be fictional or stale;
2. the model-narration claims (route, classification, provider label, default model id) match the
   implementation and the kernel constants, and no vendor model identifier is asserted anywhere;
3. the deployment guidance still matches the P9 secure defaults.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent            # apps/edge-sentinel
ROOT = HERE.parents[1]                            # repository root
README = (HERE / "README.md").read_text(encoding="utf-8")
CONTRACT = json.loads((HERE / "config_contract.json").read_text(encoding="utf-8"))
APP_SOURCES = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                        for p in sorted(HERE.glob("*.py")) if not p.name.startswith("test_"))
KERNEL_SOURCES = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                           for p in (ROOT / "agentic_os").rglob("*.py"))


def _read_src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _readme_documented_vars() -> set[str]:
    """Uppercase tokens documented in the FIRST column of an environment-variable table row."""
    found: set[str] = set()
    for line in README.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells or cells[0] in ("Var", "-----", ""):
            continue
        found |= {t for t in re.findall(r"`([A-Z][A-Z0-9_]*)`", cells[0])}
    return found


def test_every_documented_variable_is_in_the_contract_and_is_actually_read():
    documented = _readme_documented_vars()
    assert documented, "the README variable table was not parsed - fix this test rather than the README"
    contract = {v["name"]: v for v in CONTRACT["variables"]}
    uncontracted = sorted(documented - set(contract))
    assert not uncontracted, f"documented but not in config_contract.json: {uncontracted}"
    for name in sorted(documented):
        owner = contract[name]["owner"]
        hay = APP_SOURCES if owner == "edge-sentinel" else KERNEL_SOURCES
        assert re.search(rf"\b{re.escape(name)}\b", hay), (
            f"{name} is documented (owner {owner}) but no {owner} file reads it - "
            "either wire it up, or remove it from the README and the contract")


def test_no_contract_variable_has_gone_stale():
    for v in CONTRACT["variables"]:
        hay = APP_SOURCES if v["owner"] == "edge-sentinel" else KERNEL_SOURCES
        assert re.search(rf"\b{re.escape(v['name'])}\b", hay), f"stale contract entry: {v['name']}"


def test_the_phantom_variable_and_vendor_model_claims_stay_removed():
    # the real property: a variable no code reads may not be DOCUMENTED as a variable, and no code may
    # read it. The README may still name it in a sentence telling an operator that it does nothing.
    assert "ANTHROPIC_API_KEY" not in _readme_documented_vars(), \
        "a variable no code reads must not appear in the variable table"
    assert not re.search(r"\bANTHROPIC_", APP_SOURCES), "the app must not read a vendor SDK key variable"
    assert not re.search(r"claude[-\w]*", README, re.I), "the README may not assert a vendor model"
    assert "api.anthropic.com" not in APP_SOURCES, "direct vendor egress is forbidden"
    # "Anthropic" may appear only where the README says the app does NOT use it
    for line in README.splitlines():
        if re.search(r"anthropic", line, re.I):
            assert re.search(r"\b(no|not|never|nothing)\b", line, re.I), f"unqualified vendor claim: {line}"


def _kernel_default_model() -> str:
    try:
        from agentic_os.app.llm_settings import DEFAULT_MODEL
        return DEFAULT_MODEL
    except Exception:                                    # kernel absent: read the constant's source
        m = re.search(r'DEFAULT_MODEL\s*=\s*"([^"]+)"', _read_src("agentic_os/app/llm_settings.py"))
        assert m, "cannot resolve the kernel default model"
        return m.group(1)


def _kernel_default_mode() -> str:
    try:
        from agentic_os.governance.policy import DEFAULT_MODE
        return DEFAULT_MODE.value
    except Exception:
        m = re.search(r'DEFAULT_MODE\s*=\s*PrivacyMode\.([A-Z_]+)', _read_src("agentic_os/governance/policy.py"))
        assert m, "cannot resolve the kernel default privacy mode"
        return m.group(1).lower()


def test_the_narration_route_and_claims_match_the_implementation():
    mn = CONTRACT["model_narration"]
    app = _read_src("apps/edge-sentinel/app.py")
    blurb = app[app.index("def _llm_blurb"):app.index("def _llm_blurb") + 900]
    assert "governed_text" in blurb, "the blurb must go through the governed route"
    assert "CUSTOMER_CONFIDENTIAL" in blurb, "the blurb must classify its own data"
    # the contract names a dotted route; check the module exists and really defines that function
    mod, _, fn = mn["route"].rpartition(".")
    mod_src = _read_src(mod.replace(".", "/") + ".py")
    assert f"def {fn}(" in mod_src, f"contract route {mn['route']} is not defined where it claims to be"
    kern = _read_src("agentic_os/app/llm.py")
    ep = kern[kern.index("def default_in_boundary_endpoint"):kern.index("def governed_text")]
    assert f'provider="{mn["provider_label"]}"' in ep, "the provider label must match the kernel endpoint"
    assert "ExecutionBoundary.IN_BOUNDARY" in ep and mn["boundary"] == "IN_BOUNDARY"
    assert "return None" in ep, "no in-boundary endpoint must mean no model, not a silent fallback"

    contract_vars = {v["name"]: v for v in CONTRACT["variables"]}
    assert contract_vars["REDEVOPS_LLM_MODEL"]["default"] == _kernel_default_model(), \
        "the documented default model id drifted from the kernel constant"
    assert contract_vars["REDEVOPS_PRIVACY_MODE"]["default"] == _kernel_default_mode(), \
        "the documented privacy-mode default drifted from the kernel constant"
    # the README names only the kernel default identifier, never a vendor promise
    assert _kernel_default_model() in README
    for other in re.findall(r"`([A-Z][A-Za-z]*-[Vv]\d[\w.+-]*)`", README):
        assert other == _kernel_default_model(), f"unexpected model identifier in README: {other}"


def test_the_quickstart_and_deployment_guidance_match_the_p9_defaults():
    dockerfile = _read_src("apps/edge-sentinel/Dockerfile")
    # the image may *explain* the flag in comments; what P9 forbids is the image turning it on
    assert not re.search(r"^\s*(ENV|ARG)\s+SENTINEL_ALLOW_EXPOSURE", dockerfile, re.M), \
        "the image must not enable external exposure by default (P9)"
    assert '"edge_sentinel.app:app"' in dockerfile, "the container must boot the package-qualified app"
    m = re.search(r"^docker run[^\n]*", README, re.M)
    assert m and "AGENTIC_OS_PERMISSIONS" in m.group(0), \
        "the documented docker run must state the permissions mode on its first line, as every app does"
    assert "SENTINEL_ALLOW_EXPOSURE=1" in README and "SENTINEL_BASIC_AUTH" in README, \
        "a published-port example must show both the exposure authorisation and a credential"
    assert "`127.0.0.1`" in README, "the loopback default must stay documented"
    pyproject = _read_src("apps/edge-sentinel/pyproject.toml")
    assert re.search(r"^edge-sentinel\s*=", pyproject, re.M), "the documented CLI must exist as a console script"
    assert _read_src("apps/other_entrypoints.json").count("_check_declared_bind") == 1, \
        "the P9 startup guard must remain a reviewed entry point"
