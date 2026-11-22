"""SR-04 round 5 (RA-1, RA-2): the adapter registry is atomic, built-in ids cannot be squatted or spoofed, an id
belongs to one source, registrations are capped and cannot be removed."""
from __future__ import annotations

import subprocess
import sys
import threading

import pytest

from agentic_os.market import ingestion as ING
from agentic_os.market.ingestion import IngestionRefused, register_source_adapter, repo_adapter
from agentic_os.market.outcomes_join import FirstPartyLedger


def _fresh(code: str) -> subprocess.CompletedProcess:
    root = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=root,
                          env={**__import__("os").environ, "PYTHONPATH": root})


# ── RA-1: no first-use race ──────────────────────────────────────────────────────────────────────────────
def test_ra1_repo_adapter_is_safe_under_16_threads_at_a_microsecond_switch_interval():
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(60):
            errs, seen = [], []
            barrier = threading.Barrier(16)

            def w():
                barrier.wait(5)
                try:
                    seen.append(repo_adapter("crm"))
                except Exception as e:  # noqa: BLE001
                    errs.append(type(e).__name__)
            ts = [threading.Thread(target=w) for _ in range(16)]
            [t.start() for t in ts]
            [t.join(5) for t in ts]
            assert errs == [] and len({id(a) for a in seen}) == 1
    finally:
        sys.setswitchinterval(old)


def test_ra1_a_fresh_process_has_every_builtin_adapter_before_anything_else_runs():
    r = _fresh("from agentic_os.market import ingestion as I\n"
               "print(sorted(I.registered_adapters()))\n"
               "print(all(I.repo_adapter(s).adapter_id == 'repo:'+s for s in I.METRIC_SOURCES))")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert all(f"('{s}', 'repo:{s}')" in lines[0] for s in ING.METRIC_SOURCES) and lines[1] == "True"


def test_ra1_concurrent_registration_of_one_id_has_exactly_one_winner():
    wins, losses = [], []
    barrier = threading.Barrier(16)

    def w():
        barrier.wait(5)
        try:
            wins.append(register_source_adapter("billing", "r5-race-winner"))
        except IngestionRefused:
            losses.append(1)
    ts = [threading.Thread(target=w) for _ in range(16)]
    [t.start() for t in ts]
    [t.join(5) for t in ts]
    assert len(wins) == 1 and len(losses) == 15


# ── RA-2: squatting and spoofing ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("adapter_id", ["repo:gsc", "repo:umami", "repo:custom", "repo:billing", "repo:x"])
def test_ra2_the_repo_prefix_is_reserved_and_the_builtins_cannot_be_squatted_or_spoofed(adapter_id):
    for source in ING.METRIC_SOURCES:
        with pytest.raises(IngestionRefused):
            register_source_adapter(source, adapter_id)
    assert repo_adapter("gsc").adapter_id == "repo:gsc"
    m = repo_adapter("gsc").make_metric(metric="m", value=1.0, segment="control")
    assert FirstPartyLedger().record(m).prov.provider == "repo:gsc"


def test_ra2_an_adapter_id_belongs_to_one_source_forever():
    register_source_adapter("erp", "r5-one-source")
    for other in ("billing", "crm", "analytics", "erp"):
        with pytest.raises(IngestionRefused, match="already registered"):
            register_source_adapter(other, "r5-one-source")
    assert ING._IDS["r5-one-source"] == "erp"


def test_ra2_repo_adapter_refuses_unknown_sources():
    for bad in ("ga4", "stripe", "UMAMI", "", None, 5, ["umami"]):
        with pytest.raises(IngestionRefused):
            repo_adapter(bad)                                              # type: ignore[arg-type]


def test_ra2_registrations_are_capped_and_there_is_no_unregister():
    r = _fresh(
        "from agentic_os.market import ingestion as I\n"
        "ok = 0\n"
        "for i in range(200):\n"
        "    try:\n"
        "        I.register_source_adapter('analytics', f'flood-{i:04d}'); ok += 1\n"
        "    except I.IngestionRefused as e:\n"
        "        last = str(e)\n"
        "n = len(I.registered_adapters())\n"
        "print(ok, n, I.MAX_REGISTRATIONS, 'limit' in last)\n"
        "print([n for n in dir(I) if 'unreg' in n.lower() or 'remove' in n.lower() or 'delete' in n.lower()])\n"
        "# the failed registrations left no partial state, and a built-in still works\n"
        "print(I.repo_adapter('umami').adapter_id)")
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip().splitlines()
    ok, n, cap, limited = out[0].split()
    assert int(ok) == int(cap) == ING.MAX_REGISTRATIONS and int(n) == int(cap) + len(ING.METRIC_SOURCES)
    assert limited == "True" and out[1] == "[]" and out[2] == "repo:umami"


def test_ra2_a_refused_registration_leaves_the_registry_unchanged():
    before = (dict(ING._IDS), len(ING.registered_adapters()))
    for args in (("ga4", "r5-never-1"), ("billing", "repo:billing"), ("billing", "ssr-import"), ("billing", "BAD")):
        with pytest.raises(IngestionRefused):
            register_source_adapter(*args)
    assert (dict(ING._IDS), len(ING.registered_adapters())) == before
