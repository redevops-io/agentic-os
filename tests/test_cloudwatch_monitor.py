"""§2.6: Sidekick reads CloudWatch post-deploy and turns an alarm into a governed response mission.

Exercised with fake boto clients + a fake runtime — no boto3, no AWS.
"""
import sys
import pathlib
from types import SimpleNamespace

_SK = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "sidekick-devops"
if str(_SK) not in sys.path:
    sys.path.insert(0, str(_SK))

import mcp_reads  # noqa: E402
from monitor import MonitorLoop  # noqa: E402


# ── mcp_reads.cloudwatch_alarms / cloudwatch_query with injected fakes ───────────────────────────
class FakeCW:
    def describe_alarms(self, StateValue):
        assert StateValue == "ALARM"
        return {"MetricAlarms": [
            {"AlarmName": "HighCPU", "MetricName": "CPUUtilization", "Namespace": "AWS/EKS",
             "StateValue": "ALARM", "StateReason": "cpu > 90% for 5m"}]}


def test_cloudwatch_alarms_parses_live():
    out = mcp_reads.cloudwatch_alarms(client=FakeCW())
    assert out["source"] == "live:cloudwatch" and out["count"] == 1
    assert out["alarms"][0]["name"] == "HighCPU" and "cpu" in out["alarms"][0]["reason"]


def test_cloudwatch_alarms_modeled_without_boto3():
    # no client + no boto3/creds → modeled note, never raises
    out = mcp_reads.cloudwatch_alarms()
    assert out["source"] in ("modeled", "live:cloudwatch")   # modeled in CI; tolerate a real env
    assert "alarms" in out


class FakeLogs:
    def start_query(self, **kw):
        self.kw = kw
        return {"queryId": "q1"}

    def get_query_results(self, queryId):
        return {"status": "Complete", "results": [[{"field": "@message", "value": "OOMKilled"}]]}


def test_cloudwatch_query_runs_and_flattens():
    out = mcp_reads.cloudwatch_query("fields @message", client=FakeLogs(), now=lambda: 1000, window_s=300)
    assert out["source"] == "live:cloudwatch"
    assert out["rows"] == [{"@message": "OOMKilled"}]


# ── the monitor turns an alarm into a governed response mission ──────────────────────────────────
class FakeRuntime:
    def __init__(self):
        self.created = []

    def create_mission(self, goal, policy_refs, template):
        m = SimpleNamespace(id=f"m{len(self.created)}", goal=goal, template=template)
        self.created.append(m)
        return m

    def run(self, mission_id):
        pass


def test_monitor_spawns_mission_on_alarm_then_resolves(monkeypatch):
    rt = FakeRuntime()
    loop = MonitorLoop(rt, ["infra:read"], cw_template="cost_audit")

    # alarm firing → one governed response mission, deduped, tracked as open
    monkeypatch.setattr(mcp_reads, "cloudwatch_alarms",
                        lambda *a, **k: {"alarms": [{"name": "HighCPU", "reason": "cpu>90"}]})
    loop._evaluate_cloudwatch()
    loop._evaluate_cloudwatch()   # second tick: same alarm must NOT spawn a second mission
    assert len(rt.created) == 1
    assert "HighCPU" in rt.created[0].goal and rt.created[0].template == "cost_audit"
    assert "cloudwatch:HighCPU" in loop._open

    # alarm clears → the rule resolves so it can re-trigger later
    monkeypatch.setattr(mcp_reads, "cloudwatch_alarms", lambda *a, **k: {"alarms": []})
    loop._evaluate_cloudwatch()
    assert "cloudwatch:HighCPU" not in loop._open
    assert loop.signals["cloudwatch:HighCPU"]["firing"] is False


def test_monitor_cloudwatch_noop_when_no_alarms(monkeypatch):
    rt = FakeRuntime()
    loop = MonitorLoop(rt, ["infra:read"])
    monkeypatch.setattr(mcp_reads, "cloudwatch_alarms", lambda *a, **k: {"source": "modeled", "alarms": []})
    loop._evaluate_cloudwatch()
    assert rt.created == []
