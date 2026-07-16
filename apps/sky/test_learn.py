"""Tests for the placement reward loop — sky.learn + the operator's optimize/launch wiring."""
from __future__ import annotations

from sky import core
from sky.learn import PlacementLedger, reward_from_outcome
from sky.operator import build_sky_operator

_DRY = (
    "Considered resources (1 node):\n"
    "-------------------------------------------------------------------------------\n"
    " CLOUD   INSTANCE   vCPUs   Mem(GB)   ACCELERATORS   REGION/ZONE    COST ($)   CHOSEN\n"
    "-------------------------------------------------------------------------------\n"
    " GCP     g2         4       16        L4:1           us-central1    0.85          ✔\n"
    " AWS     g5         4       16        A10G:1         us-east-1      1.01\n"
    "-------------------------------------------------------------------------------\n"
)


def _run(cases):
    def run(argv, cwd=None):
        j = " ".join(argv)
        for k, v in cases.items():
            if k in j:
                return v
        return 0, "", ""
    return run


def test_reward_from_outcome():
    assert reward_from_outcome({"launched": False}) == 0.0
    assert reward_from_outcome({"launched": True}) == 1.0
    assert reward_from_outcome({"launched": True, "had_capacity": False}) == 0.5
    assert reward_from_outcome({"launched": True, "preemption_rate": 0.4}) == 0.6
    assert reward_from_outcome({"launched": True, "time_to_ready_s": 900}) == 0.8


def test_ledger_ema_and_value():
    led = PlacementLedger(path="", beta=0.5)
    spec = {"gpus": "L4:1", "spot": True}
    cand = {"cloud": "GCP", "region": "us-central1", "instance": "g2"}
    assert led.value(spec, cand) is None
    assert led.record(spec, cand, {"launched": True}) == 1.0 and led.value(spec, cand) == 1.0
    led.record(spec, cand, {"launched": False})  # 0.0 → EMA (0.5·1 + 0.5·0)
    assert led.value(spec, cand) == 0.5


def test_rerank_prefers_learned_good_over_cheaper():
    led = PlacementLedger(path="")
    spec = {"gpus": "L4:1"}
    cheap = {"cloud": "AWS", "region": "us-east-1", "instance": "g5", "hourly_usd": 0.85}
    good = {"cloud": "GCP", "region": "us-central1", "instance": "g2", "hourly_usd": 1.01}
    led.record(spec, good, {"launched": True})                             # 1.0
    led.record(spec, cheap, {"launched": True, "preemption_rate": 0.7})    # 0.3
    ranked = led.rerank(spec, [cheap, good])
    assert ranked[0]["cloud"] == "GCP" and ranked[0]["chosen"] is True     # learned-good beats cheaper
    assert ranked[0]["learned_reward"] == 1.0 and ranked[1]["chosen"] is False


def test_optimize_reranks_by_learning_and_launch_records(tmp_path):
    led = PlacementLedger(path=str(tmp_path / "ledger.json"))
    op = build_sky_operator(run=_run({"--dryrun": (0, _DRY, ""), "sky launch": (0, "Launching on GCP\n", "")}), ledger=led)
    spec = {"gpus": "L4:1"}

    # cold start: no learning ⇒ cheapest (GCP 0.85) chosen, learned flag off
    r0 = op.invoke("sky.optimize", {"spec": spec})
    assert r0["chosen"]["cloud"] == "GCP" and r0["learned"] is False

    # teach that AWS gets preempted hard (low reward) by launching it a few times
    aws = {"cloud": "AWS", "region": "us-east-1", "instance": "g5"}
    for _ in range(3):
        led.record(spec, aws, {"launched": True, "preemption_rate": 0.8})  # reward 0.2
    # and that GCP delivers
    led.record(spec, {"cloud": "GCP", "region": "us-central1", "instance": "g2"}, {"launched": True})

    r1 = op.invoke("sky.optimize", {"spec": spec})
    assert r1["learned"] is True
    assert r1["chosen"]["cloud"] == "GCP"                # learned reward keeps GCP on top
    aws_row = next(c for c in r1["candidates"] if c["cloud"] == "AWS")
    assert aws_row["learned_reward"] and aws_row["learned_reward"] < 0.5   # AWS demoted by preemption

    # launch records the measured outcome against the CHOSEN candidate (keys match the optimizer)
    lr = op.invoke("sky.launch", {"spec": spec, "chosen": r1["chosen"]})
    assert lr["reward"] == 1.0 and lr["outcome"]["launched"] is True
    assert led.value(spec, r1["chosen"]) is not None
