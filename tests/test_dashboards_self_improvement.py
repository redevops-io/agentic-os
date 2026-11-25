"""The Self-improvement dashboard shows what optimizer runs recorded - every number traceable to a record - and nothing
else. The fixtures are written by agent-harness's real optimizer driven by scripted parts (see fixtures/sim_runs/generate.py);
they are synthetic."""
import json
import shutil
from pathlib import Path

import pytest

from agentic_os.dashboards.config import parse_config
from agentic_os.dashboards.service import DashboardService, parse_filters
from agentic_os.dashboards.store import DashboardStore

FIXTURES = Path(__file__).parent / "fixtures" / "sim_runs"
MARKER = "NEVER-SHOW-THIS"


class Provider:
    serves_fixtures = False

    def projects(self): return [{"id": "main", "name": "Main"}]
    def attention(self, pid): return []
    def missions(self, pid): return []
    def sources(self, pid): return []
    def runtime(self, pid): return {"runtimes": [], "models": []}


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_RUNS_DIR", raising=False)
    out = tmp_path / "runs"
    shutil.copytree(FIXTURES, out, ignore=shutil.ignore_patterns("*.py", "__pycache__"))
    return out


def service(directory=None, *, enabled=True, access=None):
    doc = {"schema": "aos.dashboards/v1", "projects": [{"id": "main", "name": "Main"}]}
    if enabled:
        doc["sources"] = {"sim_runs": {"project": "main", **({"dir": str(directory)} if directory else {})}}
    if access:
        doc["access"] = access
    cfg = parse_config(doc)
    assert not cfg.errors, cfg.errors
    return DashboardService(Provider(), config=cfg, store=DashboardStore(""))


def build(svc, principal="local-owner"):
    return svc.build("self_improvement", parse_filters({}, now=svc.clock()), svc.access(principal))


def metrics(d):
    return {m.id: m for p in d.panels for m in p.metrics}


def drill(svc, key):
    return svc.drill("self_improvement", key, parse_filters({}, now=svc.clock()), svc.access("local-owner"))


def offered(svc, principal="local-owner"):
    return "self_improvement" in [d["id"] for d in svc.catalog(svc.access(principal))["dashboards"]]


def test_every_figure_is_the_count_of_the_records_it_drills_into(runs):
    svc = service(runs)
    assert offered(svc)
    d = build(svc)
    m = metrics(d)
    # the script: source run = 2 + 3 candidates (2 accepted, 1 rejected, 1 screened out, 1 refused), target run = 1 rejected
    assert (m["runs"].value, m["evaluated"].value, m["not_adopted"].value, m["rollbacks"].value, m["regressing"].value) == (2, 4, 4, 1, 2)
    assert (m["accepted"].numerator, m["accepted"].denominator, m["accepted"].value) == (2, 4, 50.0)
    assert (m["prediction_hits"].numerator, m["prediction_hits"].denominator) == (4, 6)
    for key, want in (("sim:runs", 2), ("sim:evaluated", 4), ("sim:accepted", 2), ("sim:rejected", 4), ("sim:regressions", 2), ("sim:rollbacks", 1)):
        assert drill(svc, key)["total"] == want, key
    decisions = sorted(r["status"] for r in drill(svc, "sim:rejected")["records"])
    assert decisions == ["refused", "rejected", "rejected", "screened out"]
    assert sum(r["fields"]["fixed"] for r in drill(svc, "sim:evaluated")["records"]) == 4
    assert [r["fields"]["regressed_tasks"] for r in drill(svc, "sim:regressions")["records"]] == ["e4", "e4"]


def test_causes_transfer_rollback_and_predictions_are_the_recorded_ones(runs):
    svc = service(runs)
    d = build(svc)
    panels = {p.id: p for p in d.panels}
    rows = [r["cells"] for r in panels["interventions"].rows]
    second = next(c for c in rows if c["change"] == "add the second marker")
    assert second["cause"] == "markers are missing" and second["decision"] == "accepted"      # cited as "P1" in the record
    unseen = next(c for c in rows if c["change"] == "screen me out")
    assert unseen["observed"] == "not evaluated" and unseen["score"] == "" and unseen["broke"] == ""
    carried = {c["cells"]["run"]: c["cells"] for c in panels["runs"].rows}
    assert carried["target"]["carried"] == "source (synthetic-2)" and carried["target"]["policy"] == "model-b@1"
    assert carried["source"]["carried"] == "not carried" and carried["source"]["policy"] == "model-a@1"
    assert [r["cells"]["reason"] for r in panels["rollbacks"].rows] == ["synthetic rollback for the fixtures"]
    chart = panels["predicted_observed"]
    assert chart.x == ["source", "target"] and [s["values"] for s in chart.series] == [[3, 1], [1, 1]]
    assert d.summary == ""                                                                    # something was accepted


def test_task_replies_and_traces_are_never_read_into_the_dashboard(runs):
    assert MARKER in (runs / "source" / "rounds" / "round-001.outcome.json").read_text()      # the premise
    svc = service(runs)
    d = build(svc)
    shown = json.dumps(d.to_dict()) + "".join(json.dumps(drill(svc, key)) for key in d.drills)
    assert MARKER not in shown


def test_runs_that_accepted_nothing_say_so_instead_of_looking_empty(runs):
    shutil.rmtree(runs / "source")
    d = build(service(runs))
    assert d.summary == "No edit was accepted in 1 recorded run(s), after 1 evaluated candidate(s)."
    m = metrics(d)
    assert (m["accepted"].numerator, m["accepted"].denominator) == (0, 1) and m["rollbacks"].value == 0


def test_without_a_directory_nothing_is_offered_and_nothing_is_invented(runs, monkeypatch):
    svc = service(None)
    assert not offered(svc)
    entry = next(e for e in svc.catalog(svc.access("local-owner"))["not_connected"] if e["id"] == "self_improvement")
    assert "SIM_RUNS_DIR" in entry["needs"][0]["remedy"]
    d = build(svc)
    assert d.empty and all(m.value is None for m in metrics(d).values())                     # unknown, never zero
    monkeypatch.setenv("SIM_RUNS_DIR", str(runs))                                            # following the remedy works
    assert offered(service(None))


def test_the_remedy_for_a_switched_off_source_names_the_setting_and_the_file(runs):
    cfg = parse_config({"schema": "aos.dashboards/v1", "projects": [{"id": "main", "name": "Main"}], "sources": {}})
    cfg.source = "/etc/x/dashboards.yaml"
    svc = DashboardService(Provider(), config=cfg, store=DashboardStore(""))
    entry = next(e for e in svc.catalog(svc.access("local-owner"))["not_connected"] if e["id"] == "self_improvement")
    assert "`sim_runs`" in entry["needs"][0]["remedy"] and "/etc/x/dashboards.yaml" in entry["needs"][0]["remedy"]


def test_the_source_stays_off_until_the_operator_lists_it(runs, monkeypatch):
    monkeypatch.setenv("SIM_RUNS_DIR", str(runs))
    assert not offered(service(enabled=False))


def test_a_caller_without_the_source_grant_sees_none_of_it(runs):
    svc = service(runs, access={"anonymous": {"projects": "*", "sources": ["missions"]}, "principals": {"local-owner": {"projects": "*", "sources": "*"}}})
    assert not offered(svc, "") and offered(svc)
    with pytest.raises(PermissionError):
        build(svc, "")


def test_an_unreadable_run_is_named_and_the_counts_become_a_lower_bound(runs):
    (runs / "source" / "sim.json").write_text("{not json")
    d = build(service(runs))
    status = next(s for s in d.sources if s.id == "sim_runs")
    assert status.state == "stale" and "source" in status.detail and "lower bound" in status.detail
    assert metrics(d)["runs"].value == 1


@pytest.mark.parametrize("damage", ["not json", '{"schema": "something-else/9"}'])
def test_when_no_run_can_be_read_the_figures_are_unknown(runs, damage):
    for state in runs.glob("*/state.json"):
        state.write_text(damage)
    svc = service(runs)
    d = build(svc)
    assert d.empty and all(m.value is None for m in metrics(d).values())
    assert next(s for s in d.sources if s.id == "sim_runs").state == "unavailable"


def test_a_link_out_of_the_runs_directory_is_not_followed(runs, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text((runs / "source" / "sim.json").read_text().replace("markers are missing", "OUTSIDE-THE-DIRECTORY"))
    (runs / "source" / "sim.json").unlink()
    (runs / "source" / "sim.json").symlink_to(outside)
    svc = service(runs)
    d = build(svc)
    assert "OUTSIDE-THE-DIRECTORY" not in json.dumps(d.to_dict())
    assert next(s for s in d.sources if s.id == "sim_runs").state == "stale"
