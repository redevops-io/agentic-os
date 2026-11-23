"""SSR scoring core: formula fidelity, invariants, degenerate cases, anchor packs, embedder safety, feature flag.
Deterministic (seeded) property loops; no network; no optional dependencies."""
from __future__ import annotations

import ast
import json
import math
import random
from pathlib import Path

import pytest

from agentic_os.market import synthetic_research as sr
from agentic_os.market.synthetic_research import anchors as A
from agentic_os.market.synthetic_research import embedder as E
from agentic_os.market.synthetic_research import pipeline as P
from agentic_os.market.synthetic_research import ssr as S

RNG = random.Random(20261122)
PKG = Path(S.__file__).parent


def rvec(dim=8, rng=RNG):
    return [rng.uniform(-1, 1) for _ in range(dim)]


def rsets(m=3, dim=8, rng=RNG):
    return [[rvec(dim, rng) for _ in range(5)] for _ in range(m)]


# ── formula: equation 8 by hand ────────────────────────────────────────────────────────────────────────────
def test_equation_8_worked_example():
    pmf, flat = S.pmf_from_similarities([0.1, 0.2, 0.3, 0.5, 0.9])
    assert not flat
    # subtract min (0.1): [0, .1, .2, .4, .8]; sum 1.5
    assert pmf == pytest.approx([0, 0.1 / 1.5, 0.2 / 1.5, 0.4 / 1.5, 0.8 / 1.5])
    assert pmf[0] == 0.0                                  # the paper's stated bias at epsilon = 0
    assert sum(pmf) == pytest.approx(1.0)


def test_epsilon_puts_mass_on_the_argmin_only():
    pmf, _ = S.pmf_from_similarities([0.1, 0.2, 0.3, 0.5, 0.9], epsilon=0.3)
    assert pmf == pytest.approx([0.3 / 1.8, 0.1 / 1.8, 0.2 / 1.8, 0.4 / 1.8, 0.8 / 1.8])


def test_degenerate_equal_similarities_are_flagged_and_uniform():
    pmf, flat = S.pmf_from_similarities([0.5] * 5)
    assert flat and pmf == [0.2] * 5
    pmf2, flat2 = S.pmf_from_similarities([0.5] * 5, epsilon=0.1)
    assert not flat2 and pmf2[0] == pytest.approx(1.0)    # all mass at argmin (first index on ties)


def test_negative_similarities_are_fine_because_the_minimum_is_subtracted():
    pmf, flat = S.pmf_from_similarities([-0.9, -0.5, -0.2, 0.0, 0.4])
    assert not flat and all(p >= 0 for p in pmf) and sum(pmf) == pytest.approx(1.0)


def test_bad_parameters_fail_loudly():
    for bad in ([0.1] * 4, [0.1] * 6, [float("nan")] * 5, [float("inf"), 0, 0, 0, 0]):
        with pytest.raises(S.SSRError):
            S.pmf_from_similarities(bad)
    for eps in (-0.1, float("nan"), float("inf")):
        with pytest.raises(S.SSRError):
            S.pmf_from_similarities([0.1, 0.2, 0.3, 0.4, 0.5], eps)


# ── cosine / variants ──────────────────────────────────────────────────────────────────────────────────────
def test_cosine_basics_and_errors():
    assert S.cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert S.cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert S.cosine([1, 0], [-1, 0]) == pytest.approx(-1.0)
    for a, b in (([1, 2], [1]), ([], []), ([0, 0], [1, 1]), ([float("nan"), 1], [1, 1]), ([True, 1], [1, 1])):
        with pytest.raises(S.SSRError):
            S.cosine(a, b)


def test_variants_are_related_by_the_affine_map_and_coincide_at_epsilon_zero():
    for _ in range(200):
        a, b = rvec(), rvec()
        assert S.similarity(a, b, S.VARIANT_REFERENCE) == pytest.approx((1 + S.similarity(a, b, S.VARIANT_PAPER)) / 2)
        r, sets = rvec(), rsets()
        p1 = S.ssr_score(r, sets, variant=S.VARIANT_PAPER).pmf
        p2 = S.ssr_score(r, sets, variant=S.VARIANT_REFERENCE).pmf
        assert p1 == pytest.approx(p2, abs=1e-12)          # identical at epsilon = 0 (affine map cancels)


def test_variants_differ_when_epsilon_is_positive():
    r, sets = rvec(), rsets()
    a = S.ssr_score(r, sets, variant=S.VARIANT_PAPER, epsilon=0.05).pmf
    b = S.ssr_score(r, sets, variant=S.VARIANT_REFERENCE, epsilon=0.05).pmf
    assert max(abs(x - y) for x, y in zip(a, b)) > 1e-6
    with pytest.raises(S.SSRError):
        S.similarity([1, 0], [1, 0], "nope")


# ── properties of the full score ───────────────────────────────────────────────────────────────────────────
def test_pmf_is_a_distribution_for_random_inputs():
    for _ in range(300):
        res = S.ssr_score(rvec(), rsets(m=RNG.randint(1, 6)), epsilon=RNG.choice([0, 0.01, 0.5]),
                          temperature=RNG.choice([0.5, 1.0, 2.0]))
        assert len(res.pmf) == 5 and all(0 <= p <= 1 for p in res.pmf)
        assert sum(res.pmf) == pytest.approx(1.0, abs=1e-12)
        assert 1.0 <= res.mean <= 5.0


def test_scale_invariance_of_embeddings():
    r, sets = rvec(), rsets()
    base = S.ssr_score(r, sets).pmf
    scaled = S.ssr_score([x * 37.5 for x in r], [[[y * 0.01 for y in a] for a in s] for s in sets]).pmf
    assert base == pytest.approx(scaled, abs=1e-12)


def test_permuting_the_anchor_sets_does_not_change_the_result():
    r, sets = rvec(), rsets(m=5)
    p = S.ssr_score(r, sets).pmf
    shuffled = list(sets)
    RNG.shuffle(shuffled)
    assert S.ssr_score(r, shuffled).pmf == pytest.approx(p, abs=1e-12)


def test_deterministic_bit_identical():
    r, sets = rvec(), rsets()
    assert S.ssr_score(r, sets) == S.ssr_score(r, sets)


def test_a_response_equal_to_the_top_anchor_scores_high_and_to_the_bottom_anchor_low():
    sets = rsets(m=4, dim=16)
    hi = S.ssr_score(sets[0][4], sets).mean
    lo = S.ssr_score(sets[0][0], sets).mean
    assert hi > lo


def test_degenerate_response_is_reported():
    same = [1.0] * 4
    res = S.ssr_score(same, [[same] * 5])
    assert res.is_degenerate and res.pmf == pytest.approx([0.2] * 5)


def test_input_errors():
    with pytest.raises(S.SSRError):
        S.ssr_score(rvec(), [])
    with pytest.raises(S.SSRError):
        S.ssr_score(rvec(), [[rvec() for _ in range(4)]])
    with pytest.raises(S.SSRError):
        S.ssr_score(rvec(8), [[rvec(7) for _ in range(5)]])


# ── temperature ────────────────────────────────────────────────────────────────────────────────────────────
def test_temperature_scaling_properties():
    p = [0.0, 0.1, 0.2, 0.3, 0.4]
    assert S.temperature_scale(p, 1.0) == p
    assert S.temperature_scale(p, 0.0) == [0, 0, 0, 0, 1.0]
    assert S.temperature_scale([0.5, 0, 0, 0, 0.5], 0.0) == [0.5, 0, 0, 0, 0.5]
    sharp, smooth = S.temperature_scale(p, 0.25), S.temperature_scale(p, 4.0)
    assert sharp[4] > p[4] > smooth[4]
    assert S.temperature_scale(p, 0.5)[0] == 0.0           # zeros stay zero
    assert sum(S.temperature_scale(p, 1e-6)) == pytest.approx(1.0)      # no overflow at tiny T
    for bad in (-1.0, float("nan"), float("inf")):
        with pytest.raises(S.SSRError):
            S.temperature_scale(p, bad)


# ── aggregation / metrics ──────────────────────────────────────────────────────────────────────────────────
def test_mean_rating_and_mean_pmf_and_ks():
    assert S.mean_rating([1, 0, 0, 0, 0]) == 1.0 and S.mean_rating([0, 0, 0, 0, 1]) == 5.0
    assert S.mean_pmf([[1, 0, 0, 0, 0], [0, 0, 0, 0, 1]]) == [0.5, 0, 0, 0, 0.5]
    assert S.ks_similarity([0.2] * 5, [0.2] * 5) == pytest.approx(1.0)
    assert S.ks_similarity([1, 0, 0, 0, 0], [0, 0, 0, 0, 1]) == pytest.approx(0.0)
    assert S.ks_similarity([0.5, 0.5, 0, 0, 0], [0.5, 0, 0.5, 0, 0]) == pytest.approx(0.5)
    with pytest.raises(S.SSRError):
        S.mean_pmf([])


# ── anchor packs ───────────────────────────────────────────────────────────────────────────────────────────
def test_packs_are_well_formed_and_construct_separated():
    assert set(A.PACKS) == {"purchase_intent", "trial_intent", "relevance", "trust"}
    allt = []
    for p in A.PACKS.values():
        p.validate()
        assert len(p.sets) == 6 and all(len(s) == 5 for s in p.sets)
        allt += list(p.flat_texts())
    assert len(allt) == len(set(allt)) == 120               # no statement is shared between constructs
    assert len({p.content_hash for p in A.PACKS.values()}) == 4
    assert A.get_pack("trust").pack_id.startswith("trust/local-v1/")
    with pytest.raises(KeyError):
        A.get_pack("willingness_to_pay")


def test_pack_hash_changes_with_content_and_validation_rejects_bad_packs():
    p = A.PURCHASE_INTENT
    q = A.AnchorPack(p.construct, p.version, (tuple(reversed(p.sets[0])),) + p.sets[1:])
    assert q.content_hash != p.content_hash
    for bad in (A.AnchorPack("x", "v", ()), A.AnchorPack("x", "v", (("a", "b", "c", "d", ""),)),
                A.AnchorPack("x", "v", (("a", "a", "c", "d", "e"),)),
                A.AnchorPack("x", "v", (("a", "b", "c", "d", "e"), ("a", "g", "h", "i", "j")))):
        with pytest.raises(ValueError):
            bad.validate()


# ── pipeline with a deterministic fake embedder ────────────────────────────────────────────────────────────
class FakeEmbedder:
    """Bag-of-words hashing embedder: deterministic, good enough to exercise the plumbing (NOT a quality claim)."""
    name = "fake:bow-64"

    def embed(self, texts):
        out = []
        for t in texts:
            v = [0.0] * 64
            for w in t.lower().replace(".", " ").replace(",", " ").replace(";", " ").split():
                v[hash_w(w) % 64] += 1.0
            out.append(v)
        return out


def hash_w(w: str) -> int:
    h = 1469598103934665603
    for ch in w.encode():
        h = ((h ^ ch) * 1099511628211) % (1 << 64)
    return h


def test_pipeline_scores_and_caches_anchor_embeddings():
    calls = []

    class Counting(FakeEmbedder):
        def embed(self, texts):
            calls.append(len(texts))
            return super().embed(texts)

    emb = Counting()
    P._ANCHOR_CACHE.clear()
    r1 = P.score_responses(["I would definitely buy this", "No interest in buying it at all"], A.PURCHASE_INTENT, emb)
    r2 = P.score_responses(["I might or might not buy this"], A.PURCHASE_INTENT, emb)
    assert len(r1) == 2 and len(r2) == 1
    assert calls.count(30) == 1                              # 6 sets x 5 anchors embedded once, then cached
    assert all(abs(sum(r.pmf) - 1) < 1e-12 for r in r1 + r2)
    assert r1[0].mean > r1[1].mean                           # plumbing sanity with the fake embedder only


def test_pipeline_refuses_blank_responses_and_wrong_counts():
    with pytest.raises(ValueError):
        P.score_responses(["fine", "   "], A.TRUST, FakeEmbedder())
    assert P.score_responses([], A.TRUST, FakeEmbedder()) == []

    class Short(FakeEmbedder):
        def embed(self, texts):
            return super().embed(texts)[:-1]
    P._ANCHOR_CACHE.clear()
    with pytest.raises(ValueError):
        P.score_responses(["fine"], A.TRUST, Short())


# ── embedder safety ────────────────────────────────────────────────────────────────────────────────────────
def test_http_embedder_refuses_external_hosts_and_bad_schemes():
    for url in ("https://api.openai.com", "https://example.com/x", "ftp://127.0.0.1", "file:///etc/passwd"):
        with pytest.raises(E.EmbedderRefused):
            E.HttpEmbedder(url, "m")


def test_http_embedder_in_boundary_roundtrip_with_injected_opener():
    class Resp:
        def __init__(self, body): self._b = body
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    seen = {}

    def opener(req, timeout=None):
        seen["url"], seen["body"] = req.full_url, json.loads(req.data)
        return Resp(json.dumps({"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]}).encode())

    emb = E.HttpEmbedder("http://127.0.0.1:9999", "dedicated-embed", _opener=opener)
    assert emb.embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert seen["url"] == "http://127.0.0.1:9999/v1/embeddings" and seen["body"]["model"] == "dedicated-embed"

    def short(req, timeout=None):
        return Resp(json.dumps({"data": [{"index": 0, "embedding": [1]}]}).encode())
    with pytest.raises(E.EmbedderRefused):
        E.HttpEmbedder("http://127.0.0.1:9999", "m", _opener=short).embed(["a", "b"])
    assert emb.embed([]) == []


# ── feature flag, import hygiene, no I/O in the core ───────────────────────────────────────────────────────
def test_feature_flag_defaults_off(monkeypatch):
    monkeypatch.delenv(sr.FLAG_ENV, raising=False)
    assert sr.enabled() is False
    for v in ("1", "true", "YES", " on "):
        monkeypatch.setenv(sr.FLAG_ENV, v)
        assert sr.enabled() is True
    for v in ("", "0", "false", "off", "maybe"):
        monkeypatch.setenv(sr.FLAG_ENV, v)
        assert sr.enabled() is False


def test_core_module_is_pure_no_io_no_network_no_env():
    banned = {"os", "sys", "socket", "urllib", "http", "requests", "httpx", "subprocess", "pathlib", "open"}
    tree = ast.parse((PKG / "ssr.py").read_text())
    imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)} | \
               {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not (imported & banned), imported & banned
    assert not any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "open" for n in ast.walk(tree))


def test_importing_the_package_does_not_pull_heavy_dependencies():
    import subprocess
    import sys
    code = ("import sys; import agentic_os.market.synthetic_research as p; "
            "bad=[m for m in ('numpy','torch','sentence_transformers','httpx','requests') if m in sys.modules]; "
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(PKG.parents[2]),
                         env={"PYTHONPATH": str(PKG.parents[2]), "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", out.stdout


def test_no_direct_model_clients_in_the_package():
    """N7 spirit: no raw model clients; the only network code is the in-boundary-checked HttpEmbedder."""
    for f in PKG.glob("*.py"):
        src = f.read_text()
        assert "api.openai.com" not in src and "anthropic" not in src.lower().replace("anthropic-style", ""), f.name
