"""Replicate draws: the same respondent, text and question asked more than once, averaged per respondent.

Why this exists (measured on 2026-11-24 with a local model at temperature 0.7 on a server that ignores ``seed``): with one draw the control
that repeats the base text word for word differed from its own base by +0.22 and -0.31 on a five-point scale. A study that cannot agree with
itself on identical text cannot rank two different texts. These tests pin the mechanics; the fakes are deterministic given the seed.
"""
from __future__ import annotations

import pytest

from agentic_os.market import synthetic_research as sr
from agentic_os.market.synthetic_research import contracts as C
from agentic_os.market.synthetic_research import elicitation as E
from agentic_os.market.synthetic_research import personas as P
from agentic_os.market.synthetic_research import stats as ST
from agentic_os.market.synthetic_research import study as SV
from tests._synthetic_fakes import FakeRespondent, HashEmbedder, make_variants

SME = P.load_frame("sme_niches")


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setenv(sr.FLAG_ENV, "1")


def run(replicates, **kw):
    people = P.sample_personas(SME, 4, 11, segments=["professional_services"], level="segment")
    cfg = SV.StudyConfig(study_id="rep", seed=5, constructs=("purchase_intent", "trust"), model_id="fake", n_boot=200, sweep=False,
                         control_personas_per_cell=4, params=E.GenerationParams(max_tokens=60), replicates=replicates, **kw)
    t = FakeRespondent()
    return SV.run_study(cfg, personas=people, variants=make_variants(2), snapshots=[], transport=t, embedder=HashEmbedder()), t, people


def test_one_draw_is_what_it_always_was(on):
    res, t, people = run(1)
    ids = [r.response_id for r in res.responses if r.arm == "elicitation"]
    assert all("#r" not in i for i in ids) and len(ids) == len(set(ids))
    assert len(t.calls) == len(people) * (2 + 6) * 2            # 2 variants + 6 controls, 2 constructs, one draw


def test_three_draws_ask_three_times_with_distinct_ids_and_seeds(on):
    res, t, people = run(3)
    el = [r for r in res.responses if r.arm == "elicitation"]
    assert len(el) == len(people) * (2 + 6) * 2 * 3 and len({r.response_id for r in el}) == len(el)
    one = [r for r in el if r.persona_id == people[0].persona_id and r.variant_id == "A" and r.construct == "purchase_intent"]
    assert sorted(r.response_id.rpartition("#")[2] if "#" in r.response_id else "r0" for r in one) == ["r0", "r1", "r2"]
    assert len({r.seed for r in one}) == 3                       # each draw has its own seed; draw 0 keeps the single-draw seed
    single = next(r for r in run(1)[0].responses if r.arm == "elicitation" and r.persona_id == people[0].persona_id
                  and r.variant_id == "A" and r.construct == "purchase_intent")
    assert single.seed == next(r.seed for r in one if "#" not in r.response_id)


def test_a_respondents_value_is_the_mean_of_its_draws(on):
    res, _, people = run(3)
    dist = {d.response_id: d.mean for d in res.distributions if d.arm == "ssr_paper"}
    cmp_ = next(C.to_plain(c) for c in res.comparisons if C.to_plain(c).get("kind") == "variants" and C.to_plain(c)["construct"] == "purchase_intent")
    per_persona = []
    for p in people:
        draws = [dist[r.response_id] for r in res.responses if r.arm == "elicitation" and r.included and r.persona_id == p.persona_id
                 and r.variant_id == "A" and r.construct == "purchase_intent"]
        assert len(draws) == 3
        per_persona.append(ST.mean(draws))
    a = next(v for v in cmp_["variants"] if v["variant_id"] == "A")
    assert cmp_["n_paired"] == len(people)                       # more draws are not more respondents
    assert a["mean"] == pytest.approx(ST.mean(per_persona), abs=1e-9)


def test_replicates_are_bounded(on):
    for bad in (0, 11):
        with pytest.raises(ValueError, match="replicates"):
            run(bad)


def test_the_control_base_can_be_named_and_controls_are_built_from_it(on):
    res, _, _ = run(1, control_base_id="B")
    b = make_variants(2)[1]
    identical = next(v for v in res.variants if v.variant_id == "ctrl_identical_relabelled")
    assert (identical.headline, identical.body, identical.cta) == (b.headline, b.body, b.cta)
    first = next(v for v in run(1)[0].variants if v.variant_id == "ctrl_identical_relabelled")
    assert first.headline == make_variants(2)[0].headline       # unset: the first variant, as before
