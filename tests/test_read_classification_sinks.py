"""FR-04: an automated heuristic over every capability classified ``read``: its handler (and the ``core`` functions it reaches, up to four calls deep) must not contain a SINK: a process
launch (``subprocess``, ``os.system``, an injected runner called ``run`` / ``_run``), a file write (``open`` for writing, ``write_text`` / ``write_bytes``, ``unlink``, ``mkdir`` ...) or a
network mutation (``post`` / ``put`` / ``delete`` / ``patch``, ``urlopen``). Found by it: ``infra.plan`` (``terraform plan -out=redevops-approved.tfplan``, overwriting the approved plan file, with
caller-supplied vars) and ``infra.drift`` (terraform / ansible with caller inputs): both are ``write`` now. What remains is an ALLOWLIST where every entry says why the sink is a read (and the test
fails when an entry no longer hits, so the list cannot rot). A heuristic is not proof: the manual review of the read classifications is in the report.
"""
from __future__ import annotations

import ast
import pathlib

from agentic_os.tool_actions import TOOL_ACTIONS as T

APPS = pathlib.Path(__file__).resolve().parents[1] / "apps"
SINK_ATTR={"system","popen","Popen","check_output","check_call","write_text","write_bytes","unlink","rmtree","copyfile","move","rename","remove","makedirs","mkdir","touch","post","put","delete","patch","urlopen"}
SINK_NAME={"open","_run","run","run_cmd","Popen","system"}
def sinks(fn_node):
    out=set()
    for n in ast.walk(fn_node):
        if isinstance(n,ast.Call):
            f=n.func
            if isinstance(f,ast.Attribute):
                if f.attr in SINK_ATTR: out.add(f.attr)
                if isinstance(f.value,ast.Name) and f.value.id=="subprocess": out.add("subprocess."+f.attr)
            elif isinstance(f,ast.Name):
                if f.id in SINK_NAME:
                    if f.id=="open":
                        mode=None
                        if len(n.args)>1 and isinstance(n.args[1],ast.Constant): mode=n.args[1].value
                        for k in n.keywords:
                            if k.arg=="mode" and isinstance(k.value,ast.Constant): mode=k.value.value
                        if mode and any(c in str(mode) for c in "wax+"): out.add("open(w)")
                    else: out.add(f.id)
    return out
def module_funcs(path):
    t=ast.parse(path.read_text()); d={}
    for n in ast.walk(t):
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)): d.setdefault(n.name,n)
    return d
def reach(funcs,name,seen=None,depth=0):
    seen=seen if seen is not None else set()
    if name in seen or name not in funcs or depth>4: return set()
    seen.add(name); node=funcs[name]; s=set(sinks(node))
    for n in ast.walk(node):
        if isinstance(n,ast.Call):
            f=n.func
            callee=f.id if isinstance(f,ast.Name) else (f.attr if isinstance(f,ast.Attribute) and isinstance(f.value,ast.Name) and f.value.id=="core" else None)
            if callee: s|=reach(funcs,callee,seen,depth+1)
    return s


def scan():
    res = {}
    for app, tools in T.items():
        reads = [t for t, a in tools.items() if a == "read"]
        opf, corep = APPS / app / "operator.py", APPS / app / "core.py"
        if not opf.exists():
            continue
        ofn = module_funcs(opf)
        cfn = module_funcs(corep) if corep.exists() else {}
        tree = ast.parse(opf.read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "capability" and n.args and isinstance(n.args[0], ast.Constant):
                name = n.args[0].value
                if name not in reads or len(n.args) < 2:
                    continue
                h, hit, names = n.args[1], set(), set()
                for m in ast.walk(h):
                    if isinstance(m, ast.Call):
                        f = m.func
                        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "core":
                            names.add(f.attr)
                        elif isinstance(f, ast.Name):
                            names.add(f.id)
                if isinstance(h, ast.Name):
                    names.add(h.id)
                for nm in names:
                    if nm in ofn:
                        hit |= sinks(ofn[nm])
                        for m in ast.walk(ofn[nm]):
                            if isinstance(m, ast.Call):
                                f = m.func
                                if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "core":
                                    hit |= reach(cfn, f.attr)
                    if nm in cfn:
                        hit |= reach(cfn, nm)
                if hit:
                    res[(app, name)] = sorted(hit)
    return res


# (app, capability) -> why the sink is a read. Reviewed by reading the code.
ALLOWED = {
    ("agentic-privacy", "privacy.retention"): "calls retention_scan(confirm=False) with a constant: the purge branch (delete / open-for-write) is unreachable from this capability; it returns counts only",
    ("control-tower", "bi.ask"): "httpx.post to Metabase's /api/session and /api/dataset: authentication and a read-only query of a pre-written SQL template (never model-authored SQL)",
    ("control-tower", "bi.refresh"): "the same Metabase /api/dataset query re-run (a cache bust of a read)",
    ("control-tower", "bi.summary"): "the same Metabase /api/dataset read queries",
    ("edge-sentinel", "sentinel.triage"): "subprocess.run of a FIXED argv: `cscli ... list` (CrowdSec alerts/decisions) through docker exec: a listing, caller input is never part of the argv",
    ("growth-engine", "growth.analyze"): "httpx.post to Umami's /api/auth/login (a token for the read API); the analysis itself only reads",
    ("outreach-engine", "outreach.refresh"): "urlopen GETs of Twenty / GitHub to rebuild an in-memory pipeline; no write to either",
}


def test_infra_plan_and_drift_are_write_by_their_actual_effect():
    assert T["infra"]["infra.plan"] == "write" and T["infra"]["infra.drift"] == "write"
    assert T["infra"]["infra.verify"] == "read"                                        # a GET smoke of a checked address


def test_no_read_capability_reaches_a_sink_unless_it_is_allow_listed_with_a_reason():
    hits = scan()
    new = {k: v for k, v in hits.items() if k not in ALLOWED}
    assert new == {}, f"a capability classified read reaches a sink (reclassify it or allow-list it with the reason): {new}"


def test_every_allowlist_entry_still_hits_and_has_a_reason():
    hits = scan()
    assert set(ALLOWED) <= set(hits), f"stale allowlist entries: {sorted(set(ALLOWED) - set(hits))}"
    assert all(len(why) > 30 for why in ALLOWED.values())


def test_the_scan_catches_the_demonstrated_defect():
    """Positive control: with infra.plan put back to read, the scan reports it (an injected runner, `run`)."""
    saved = T["infra"]["infra.plan"]
    try:
        T["infra"]["infra.plan"] = "read"
        assert ("infra", "infra.plan") in scan()
    finally:
        T["infra"]["infra.plan"] = saved
