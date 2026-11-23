"""Ledger compatibility probe (SR-04 round 4). Not a test module.

`run()` feeds a fixed table of inputs to `FirstPartyLedger.record` and returns {case id: 'ok' | exception class name}.
It uses only names that exist on BOTH the base commit d9210fb and the current code, so the same file was run against
each tree to derive `tests/golden/ledger_compat_base.json` and `tests/golden/ledger_compat_now.json`:

    PYTHONPATH=<tree> python tests/ledger_compat_probe.py <out.json>

For inputs that are "minted by a registered adapter" the current tree mints with `repo_adapter(source).make_metric`;
base has no adapters, so the equivalent input there is the hand-built metric (the only way a metric existed).
"""
from __future__ import annotations

import dataclasses
import json
import sys
from types import SimpleNamespace

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.market.outcomes_join import FirstPartyLedger, FirstPartyMetric

try:                                                    # current tree only
    from agentic_os.market.ingestion import repo_adapter
except ImportError:                                     # base: no adapters
    repo_adapter = None

SOURCES = ["umami", "gsc", "marketplace", "crm", "billing", "erp", "analytics", "UMAMI", " gsc ", "", "   ",
           "stripe", "ga4", "internal", "analytics_v2", "umami:site", "synthetic", "ssr:likert", "study:abc"]
PROVIDERS = ["umami", "buyer-panel", "simulation", "ssrs-reports"]
LEGIT = SOURCES[:7]


def hand(source, provider="umami", **kw):
    return FirstPartyMetric(prov=Provenance(provider=provider, **kw.pop("prov", {})), source=source, metric="signup",
                            value=0.5, segment="treatment", **kw)


def minted(source, **kw):
    if repo_adapter is None:
        return hand(source, **({"prov": {"evidence_refs": kw["evidence_refs"]}} if "evidence_refs" in kw else {}))
    return repo_adapter(source).make_metric(metric="signup", value=0.5, segment="treatment", **kw)


def cases():
    out = {}
    for s in SOURCES:
        for p in PROVIDERS:
            out[f"hand|source={s!r}|provider={p}"] = lambda s=s, p=p: hand(s, p)
    for s in LEGIT:
        out[f"minted|source={s}"] = lambda s=s: minted(s)
    out["minted|umami|study ref in evidence_refs"] = lambda: minted("umami", evidence_refs=("Synthetic-Study:v1:{}",))
    out["minted|umami|study ref with whitespace"] = lambda: minted("umami", evidence_refs=(" synthetic-study:v1:x",))
    out["hand|umami|study ref in evidence_refs"] = lambda: hand("umami", prov={"evidence_refs": ("synthetic-study:v1:x",)})

    def mutated():
        m = minted("umami")
        object.__setattr__(m, "value", 9.9)
        return m
    out["minted|umami|value mutated after minting"] = mutated

    def copied_attestation():
        m = minted("umami")
        other = dataclasses.replace(m, metric="other", value=0.9)       # base: plain copy; now: stale attestation
        return other
    out["minted|umami|copied onto a different metric"] = copied_attestation
    out["duck|SimpleNamespace metric"] = lambda: SimpleNamespace(**{**{f.name: getattr(hand("umami"), f.name) for f in
                                                                       dataclasses.fields(hand("umami"))}})
    out["subclass|FirstPartyMetric subclass"] = lambda: type("Sub", (FirstPartyMetric,), {})(
        **{f.name: getattr(minted("umami"), f.name) for f in dataclasses.fields(minted("umami"))})
    out["type|dict"] = lambda: {"source": "umami", "metric": "signup"}
    out["type|None"] = lambda: None
    out["type|object"] = lambda: object()
    out["type|str"] = lambda: "umami"

    def synthetic_attr():
        m = SimpleNamespace(**{f.name: getattr(hand("umami"), f.name) for f in dataclasses.fields(hand("umami"))})
        m.synthetic = True
        return m
    out["duck|synthetic=True attribute"] = synthetic_attr
    return out


def run() -> dict:
    res = {}
    for name, build in cases().items():
        try:
            FirstPartyLedger().record(build())
            res[name] = "ok"
        except Exception as e:  # noqa: BLE001
            res[name] = type(e).__name__
    return res


if __name__ == "__main__":
    json.dump(run(), open(sys.argv[1], "w"), indent=1, sort_keys=True)
    print("ok")
