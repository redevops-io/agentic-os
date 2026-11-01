"""Every append-only writer keeps every record when writers run at the same instant (threads and processes).

A writer that appends without a lock relies on POSIX ``O_APPEND`` being atomic. On Windows the C runtime emulates it as "seek to the end, then
write", so two writers with their own descriptors can overwrite each other: a record that was "written" is missing (the cause of a Windows-only
failure of a fork/join test, where three branches finished within milliseconds). These writers now append through
``agentic_os._platform.files.append_line``: one write while holding the file's inter-process lock, binary mode, a bare LF.

Each test releases N threads (and, separately, 2 processes) from a barrier / a common start time; each appends K records; exactly N*K intact lines
must come out. On Linux and macOS the old lock-free code passed these too: the proof that they are Windows-meaningful is the loop job in the pull
request that ran the old and the new writers on windows-latest."""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from agentic_os._platform import files
from tests._proc_support import spawn

N, K = 8, 25          # threads (and records per thread)
P, PK = 2, 60         # processes (and records per process)

# One source per writer: `make(path)` returns `append(i, j)`; the parent and the child processes exec the same text.
WRITERS = {
    "policy_store": '''
def make(path):
    from agentic_os.policy_promotion import FilePolicyStore
    s = FilePolicyStore(path)
    return lambda i, j: s.append({"i": i, "j": j, "pad": "x" * 40})
''',
    "intervention_store": '''
def make(path):
    from agentic_os.intervention_record import FileInterventionStore, OutcomeLink
    s = FileInterventionStore(path)
    return lambda i, j: s.link(OutcomeLink(f"iv-{i}", f"o-{j}", 1.0, 0.0))
''',
    "evidence_value_store": '''
def make(path):
    from agentic_os.intelligence.value_store import EvidenceValueStore
    class Rec:
        def __init__(self, i, j): self.i, self.j = i, j
        def canonical_form(self): return {"i": self.i, "j": self.j}
    s = EvidenceValueStore(path)
    return lambda i, j: s.append(Rec(i, j))
''',
    "email_suppression_list": '''
def make(path):
    from agentic_os.intelligence.email.bridge import SuppressionList
    s = SuppressionList(path)
    return lambda i, j: s.add("t", f"r{i}-{j}@x.com", "bounce")
''',
    "email_receipt_store": '''
def make(path):
    from agentic_os.intelligence.email.bridge import ReceiptStore
    class Rcpt:
        def __init__(self, i, j): self.i, self.j = i, j
        def canonical_form(self): return {"i": self.i, "j": self.j}
    s = ReceiptStore(path)
    return lambda i, j: s.append(Rcpt(i, j))
''',
    "outreach_suppression_ledger": '''
def make(path):
    from agentic_os.world.outreach import SuppressionLedger
    s = SuppressionLedger(path)
    return lambda i, j: s.suppress(f"u{i}-{j}@x.com")
''',
}


def _parse(name: str, line: str):
    if name == "outreach_suppression_ledger":
        a, b = line[1:-len("@x.com")].split("-")
        return int(a), int(b)
    d = json.loads(line)
    if name == "intervention_store":
        return int(d["intervention_id"].split("-")[1]), int(d["outcome_ref"].split("-")[1])
    if name == "email_suppression_list":
        a, b = d["recipient"][1:-len("@x.com")].split("-")
        return int(a), int(b)
    return d["i"], d["j"]


def _make(name: str, path: str):
    ns: dict = {}
    exec(WRITERS[name], ns)             # noqa: S102 - the writer's own test source
    return ns["make"](path)


def _hammer(append, n=N, k=K):
    gate = threading.Barrier(n)
    errs = []

    def work(i):
        try:
            gate.wait(20)
            for j in range(k):
                append(i, j)
        except BaseException as e:      # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join(120) for t in ts]
    assert not errs, errs


def _lines(path: str) -> list[str]:
    raw = open(path, "rb").read()
    assert b"\r" not in raw, "a CRLF was written"
    assert raw.endswith(b"\n")
    return raw.decode("utf-8").split("\n")[:-1]


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_n_threads_each_append_k_records_and_none_is_lost_or_torn(tmp_path, name):
    path = str(tmp_path / "sub" / f"{name}.jsonl")
    _hammer(_make(name, path))
    got = [_parse(name, l) for l in _lines(path)]
    assert sorted(got) == sorted((i, j) for i in range(N) for j in range(K))


_CHILD = """
import sys, time
path, who, k, start = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])
ns = {}
exec(sys.argv[5], ns)
append = ns["make"](path)
while time.time() < start:
    time.sleep(0.001)
for j in range(k):
    append(who, j)
"""


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_two_processes_each_append_records_and_none_is_lost_or_torn(tmp_path, name):
    path = str(tmp_path / f"{name}.jsonl")
    start = time.time() + 2.0
    procs = [spawn(_CHILD, path, str(w), str(PK), str(start), WRITERS[name]) for w in range(P)]
    for p in procs:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err[-800:]
    got = [_parse(name, l) for l in _lines(path)]
    assert sorted(got) == sorted((i, j) for i in range(P) for j in range(PK))


def test_the_episode_store_keeps_every_run_line(tmp_path):
    from agentic_os.mission.apps_e2e_runner import JsonlEpisodeStore, run_scenario
    from agentic_os.mission.decision_provider import StaticDecisionProvider
    from tests.test_apps_e2e_runner import _outcome, _request, _scenario
    run = run_scenario(_scenario(), _request(), StaticDecisionProvider("static", "static@1"), lambda s, r: _outcome(), tenant_id="tenant-a",
                       behavior_policy_id="benchmark-router", behavior_policy_pin="router@sha256:abc", propensity=1.0,
                       decision_time="2026-01-01T00:00:01Z")
    path = str(tmp_path / "ep" / "runs.jsonl")
    store = JsonlEpisodeStore(path, store_id="bench")
    _hammer(lambda i, j: store.append(run))
    lines = _lines(path)
    assert len(lines) == N * K and all(json.loads(l) == run.canonical_payload() for l in lines)
    assert len(JsonlEpisodeStore(path, store_id="bench").runs()) == N * K          # and the file replays


def test_the_discovery_registry_index_keeps_every_line(tmp_path):
    from agentic_os.revenue import MissionRegistry, QualifiedOpportunity, RecordingChannel
    from tests.test_revenue_restart import HANDOFF
    reg = MissionRegistry(owner="Alex", channel_factory=lambda: RecordingChannel(name="telegram"),
                          persist_path=str(tmp_path / "idx" / "registry.jsonl"), mission_store_dir=str(tmp_path / "missions"))
    reg.ingest(QualifiedOpportunity.from_dict(HANDOFF))
    rec = next(iter(reg._by_id.values()))
    before = len(_lines(reg.persist_path))
    _hammer(lambda i, j: reg._persist(rec))
    lines = _lines(reg.persist_path)
    assert len(lines) == before + N * K and all(json.loads(l)["opportunity_id"] == HANDOFF["opportunity_id"] for l in lines)


# ── the helper itself ──

def test_the_helper_writes_exact_bytes_creates_directories_and_leaves_no_sidecar(tmp_path):
    p = tmp_path / "a" / "b" / "log.jsonl"
    files.append_line(p, '{"k": "café"}')
    files.locked_append(p, b'{"raw": 1}\n', fsync=True)
    files.locked_append(p, '{"s": 2}\n')
    assert p.read_bytes() == '{"k": "café"}\n'.encode() + b'{"raw": 1}\n{"s": 2}\n'          # UTF-8, LF only, in order, on every OS
    assert sorted(x.name for x in p.parent.iterdir()) == ["log.jsonl"]


def test_a_line_record_with_a_line_break_is_refused_and_writes_nothing(tmp_path):
    p = tmp_path / "log"
    for bad in ("a\nb", "a\rb", "a\r\nb"):
        with pytest.raises(ValueError):
            files.append_line(p, bad)
    assert not p.exists() or p.read_bytes() == b""


def test_the_helper_does_not_lose_records_between_threads(tmp_path):
    p = str(tmp_path / "h.log")
    _hammer(lambda i, j: files.append_line(p, f"{i}:{j}:" + "y" * 200))
    got = sorted(tuple(map(int, l.split(":")[:2])) for l in _lines(p))
    assert got == sorted((i, j) for i in range(N) for j in range(K))


# ── the guard: no new unlocked append ──

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# file -> (how many append-mode opens it may contain, why that is safe). Anything else must use files.append_line / files.locked_append.
ALLOWED_APPENDERS = {
    "agentic_os/_platform/files.py": (1, "the helper itself"),
    "agentic_os/mission/store.py": (1, "the hash-chained event log: opened append and locked for the whole append (locks.locked)"),
    "agentic_os/outcome_store.py": (1, "O_APPEND inside the store's own lock file (check-then-append under _flock)"),
    "agentic_os/agent_gateway/audit_store.py": (1, "touch only: proves the path is writable; the events go through EventStore"),
    "agentic_os/context.py": (1, "the approvals queue: handled by its own change"),
    "apps/document-editor/ledger.py": (1, "hash-chained ledger append under the connector's own portable lock"),
    "apps/compliance/core.py": (2, "the ledger append runs under _ledger_lock; the other open is the lock file itself"),
    "apps/agentic-privacy/core.py": (1, "audit append under _store_mutation, whose cross-process lock is POSIX-only: to implement"),
    "apps/books/seed_erpnext.py": (1, "a dev seed script's own log file, not a data store"),
}


def _is_append_open(node) -> bool:
    import ast
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
    if name not in ("open", "fdopen"):
        return False
    cands = [a.value for a in node.args[:2] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    cands += [k.value.value for k in node.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str)]
    return any(c and set(c) <= set("rwxabt+") and "a" in c for c in cands)


def test_no_new_unlocked_append_appears_outside_the_helper():
    import ast
    import pathlib
    found: dict[str, int] = {}
    for base in ("agentic_os", "apps"):
        for f in (pathlib.Path(ROOT) / base).rglob("*.py"):
            if f.name.startswith("test_") or "tests" in f.parts:
                continue
            tree = ast.parse(f.read_text(encoding="utf-8"))
            n = sum(1 for node in ast.walk(tree) if _is_append_open(node))
            n += sum(1 for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "O_APPEND")
            if n:
                found[f.relative_to(ROOT).as_posix()] = n
    new = {k: v for k, v in found.items() if k not in ALLOWED_APPENDERS or v > ALLOWED_APPENDERS[k][0]}
    assert not new, (f"append-mode open(s) outside the helper: {new}. Use agentic_os._platform.files.append_line / locked_append "
                     f"(a lock-free append can lose a record on Windows), or add the file to ALLOWED_APPENDERS with the reason it is safe.")
    stale = sorted(k for k in ALLOWED_APPENDERS if k not in found)
    assert not stale, f"ALLOWED_APPENDERS entries with no append left (remove them): {stale}"
