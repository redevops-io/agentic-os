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
SCANNED = ("agentic_os", "apps", "deploy", "scripts")
# "file::function" -> (append-shaped sites in that function, why that is safe). Anything else must use files.append_line / files.locked_append.
ALLOWED_APPENDERS = {
    "agentic_os/_platform/files.py::locked_append": (1, "the helper itself"),
    "agentic_os/mission/store.py::EventStore.append": (1, "the hash-chained event log: opened append and locked for the whole append (locks.locked)"),
    "agentic_os/outcome_store.py::FileOutcomeStore.append": (1, "O_APPEND inside the store's own lock file (check-then-append under _flock)"),
    "agentic_os/agent_gateway/audit_store.py::EventStoreAuditSink.from_path": (1, "touch only: proves the path is writable; the events go through EventStore"),
    "agentic_os/context.py::Context._append": (1, "not part of this change"),
    "apps/document-editor/ledger.py::Ledger.append": (1, "hash-chained ledger append under the connector's own portable lock"),
    "apps/compliance/core.py::_ledger_lock.__enter__": (1, "the lock file itself"),
    "apps/compliance/core.py::_append_chained": (1, "the ledger append runs under _ledger_lock"),
    "apps/agentic-privacy/core.py::_audit": (1, "audit append under the app's own store lock (separate change)"),
    "apps/books/seed_erpnext.py::<module>": (1, "a dev seed script's own log file, not a data store"),
    "deploy/desktop-shell-main/selftest/run.py::launch": (1, "a Linux/macOS harness redirecting a child process's output to a log"),
}

_FILE_HANDLERS = ("FileHandler", "RotatingFileHandler", "TimedRotatingFileHandler", "WatchedFileHandler")


def _scan(tree) -> "dict[str, int]":
    """Append-shaped sites per enclosing function: an append-mode ``open``/``fdopen`` (literal mode containing "a"), a builtin ``open``/``os.fdopen``/
    ``io.open`` whose mode is not a literal, a log file handler that is not explicitly ``mode="w"``, ``os.O_APPEND`` (attribute) and
    ``from os import O_APPEND``."""
    import ast
    from collections import Counter
    hits: "Counter[str]" = Counter()
    stack: list[str] = []

    def key() -> str:
        return ".".join(stack) or "<module>"

    def is_append(c) -> bool:
        return isinstance(c, str) and bool(c) and set(c) <= set("rwxabt+") and "a" in c

    class V(ast.NodeVisitor):
        def _scope(self, node):
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()
        visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope

        def visit_ImportFrom(self, node):
            if node.module == "os" and any(a.name == "O_APPEND" for a in node.names):
                hits[key()] += 1

        def visit_Attribute(self, node):
            if node.attr == "O_APPEND":
                hits[key()] += 1
            self.generic_visit(node)

        def visit_Call(self, node):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            owner = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            if name in ("open", "fdopen") and not (name == "open" and owner == "os"):          # os.open takes flags, not a mode string
                builtin_like = isinstance(f, ast.Name) or owner in ("os", "io")
                mode = kw.get("mode") or (node.args[1] if builtin_like and len(node.args) > 1 else node.args[0] if not builtin_like and node.args else None)
                if mode is not None:
                    if isinstance(mode, ast.Constant):
                        if is_append(mode.value):
                            hits[key()] += 1
                    elif builtin_like:
                        hits[key()] += 1                     # a mode this scan cannot read: it must be reviewed (`obj.open(x)` is not judged: zipfile.open(name))
            elif name in _FILE_HANDLERS:
                mode = kw.get("mode") or (node.args[1] if len(node.args) > 1 else None)
                if not (isinstance(mode, ast.Constant) and mode.value == "w"):
                    hits[key()] += 1                         # the logging handlers append unless told otherwise
            self.generic_visit(node)
    V().visit(tree)
    return dict(hits)


def _scan_repo() -> "dict[str, int]":
    import ast
    import pathlib
    found: dict[str, int] = {}
    for base in SCANNED:
        for f in (pathlib.Path(ROOT) / base).rglob("*.py"):
            if f.name.startswith("test_") or "tests" in f.parts or "node_modules" in f.parts:
                continue
            for fn, n in _scan(ast.parse(f.read_text(encoding="utf-8"))).items():
                found[f"{f.relative_to(ROOT).as_posix()}::{fn}"] = n
    return found


def test_no_new_unlocked_append_appears_outside_the_helper():
    found = _scan_repo()
    new = {k: v for k, v in found.items() if k not in ALLOWED_APPENDERS or v > ALLOWED_APPENDERS[k][0]}
    assert not new, (f"append-shaped site(s) outside the helper: {new}. Use agentic_os._platform.files.append_line / locked_append "
                     f"(a lock-free append can lose a record on Windows), or add the function to ALLOWED_APPENDERS with the reason it is safe.")
    stale = sorted(k for k in ALLOWED_APPENDERS if k not in found)
    assert not stale, f"ALLOWED_APPENDERS entries with no append left (remove them): {stale}"


@pytest.mark.parametrize("src,expected", [
    ("def f(p):\n    open(p, 'a')\n", 1),
    ("def f(p):\n    open(p, mode='ab')\n", 1),
    ("def f(p):\n    from pathlib import Path\n    Path(p).open('a')\n", 1),
    ("from os import O_APPEND\n", 1),
    ("import os\ndef f(p):\n    os.open(p, os.O_WRONLY | os.O_APPEND)\n", 1),
    ("def f(p, m):\n    open(p, m)\n", 1),                                   # a mode the scan cannot read
    ("def f(p, x):\n    open(p, 'a' if x else 'w')\n", 1),
    ("import os\ndef f(fd, m):\n    os.fdopen(fd, m)\n", 1),
    ("import logging\ndef f(p):\n    logging.FileHandler(p)\n", 1),
    ("import logging.handlers as h\ndef f(p):\n    h.RotatingFileHandler(p, maxBytes=1)\n", 1),
    ("import logging\ndef f(p):\n    logging.FileHandler(p, mode='w')\n", 0),
    ("def f(p):\n    open(p)\n    open(p, 'r')\n    open(p, 'w')\n    open(p, 'rb')\n", 0),
])
def test_the_guard_recognises_every_append_shape(src, expected):
    import ast
    assert sum(_scan(ast.parse(src)).values()) == expected


def test_the_guard_counts_per_function_so_a_second_append_in_an_allowed_file_is_caught():
    import ast
    src = "class S:\n    def a(self, p):\n        open(p, 'ab')\n    def b(self, p):\n        open(p, 'a')\n"
    assert _scan(ast.parse(src)) == {"S.a": 1, "S.b": 1}


def test_locked_append_takes_and_releases_the_lock_around_the_write(tmp_path, monkeypatch):
    """Platform independent: the concurrency tests above cannot tell on Linux/macOS whether the lock is taken (POSIX append is atomic anyway), so
    a fake lock records the order of events: the lock is taken before the record is written and released after, once per record."""
    events = []

    class FakeLocks:
        LockTimeout = files.locks.LockTimeout

        @staticmethod
        def locked(fh, timeout=None):
            import contextlib

            @contextlib.contextmanager
            def cm():
                events.append(("acquire", timeout, fh.tell()))
                try:
                    yield
                finally:
                    events.append(("release", os.path.getsize(fh.name)))
            return cm()
    monkeypatch.setattr(files, "locks", FakeLocks)
    p = tmp_path / "log"
    files.append_line(p, "one")
    files.append_line(p, "two", timeout=3.0)
    assert [e[0] for e in events] == ["acquire", "release", "acquire", "release"]
    assert events[0][1] == files.APPEND_LOCK_TIMEOUT_S and events[2][1] == 3.0
    assert events[1][1] == 4 and events[3][1] == 8                  # the bytes were written between the acquire and the release
    assert p.read_bytes() == b"one\ntwo\n"
