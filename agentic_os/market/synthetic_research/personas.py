"""Deterministic, seeded, stratified persona sampling from a YAML-configurable ``PersonaSamplingFrame``.

A frame lists segments (industry), roles, firm sizes, stacks, pains, purchase criteria, objections and
constraints. It is a *design of archetypes*, not a statistical sample of any market: every stratification weight
is labelled hypothetical, nothing is derived from real people or real customers, and sampling is stratified so
that EVERY cell (segment x role [x scenario]) is covered with the same number of personas and every listed
pain/criterion/objection appears at least once as soon as the cell is large enough to rotate through the list.

Reproducibility: the same frame (content hash), the same ``seed`` and the same arguments give the identical
persona list on every machine (``random.Random(str)`` is hashed with sha512, independent of PYTHONHASHSEED).
"""
from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .contracts import PersonaSpec, stable_hash

SCENARIO_DIR = Path(__file__).parent / "scenarios"
HYPOTHETICAL = "hypothetical (not a market-sampling weight)"


class FrameError(ValueError):
    """The frame file is malformed. Fail loudly: a half-valid frame would silently bias a study."""


@dataclass(frozen=True)
class PersonaSamplingFrame:
    frame_id: str
    version: str
    kind: str                                  # sme | enterprise
    evidence_basis: str
    segments: Tuple[Mapping[str, Any], ...]
    scenarios: Tuple[Mapping[str, Any], ...] = ()
    control_segments: Tuple[Mapping[str, Any], ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return stable_hash(self.raw)

    def segment(self, seg_id: str) -> Mapping[str, Any]:
        for s in tuple(self.segments) + tuple(self.control_segments):
            if s["id"] == seg_id:
                return s
        raise KeyError(seg_id)

    def scenario(self, sid: str) -> Mapping[str, Any]:
        for s in self.scenarios:
            if s["id"] == sid:
                return s
        raise KeyError(sid)

    def cells(self, *, segments: Optional[Sequence[str]] = None, roles: Optional[Sequence[str]] = None,
              scenarios: Optional[Sequence[str]] = None, include_control: bool = False) -> List[str]:
        """Every stratification cell id: ``segment/role`` or ``segment/role/scenario`` (enterprise)."""
        out: List[str] = []
        segs = list(self.segments) + (list(self.control_segments) if include_control else [])
        for seg in segs:
            if segments and seg["id"] not in segments:
                continue
            for role in seg["roles"]:
                if roles and role["id"] not in roles:
                    continue
                if self.scenarios:
                    for sc in self.scenarios:
                        if scenarios and sc["id"] not in scenarios:
                            continue
                        out.append(f"{seg['id']}/{role['id']}/{sc['id']}")
                else:
                    out.append(f"{seg['id']}/{role['id']}")
        return out


_REQ_SEG = ("id", "industry", "roles", "firm_sizes")
_LISTS = ("pains", "purchase_criteria", "objections")


def _validate(raw: Mapping[str, Any]) -> None:
    for k in ("frame_id", "version", "kind", "evidence_basis", "segments"):
        if k not in raw:
            raise FrameError(f"frame is missing {k!r}")
    if raw["kind"] not in ("sme", "enterprise"):
        raise FrameError("kind must be sme or enterprise")
    if raw.get("weights_are_hypothetical") is not True:
        raise FrameError("weights_are_hypothetical must be true: frame weights are never market-sampling weights")
    ids = set()
    for seg in list(raw["segments"]) + list(raw.get("control_segments") or []):
        for k in _REQ_SEG:
            if not seg.get(k):
                raise FrameError(f"segment {seg.get('id')!r} is missing {k!r}")
        if seg["id"] in ids:
            raise FrameError(f"duplicate segment id {seg['id']!r}")
        ids.add(seg["id"])
        for r in seg["roles"]:
            for k in ("id", "label", "decision_role", "budget_authority"):
                if not r.get(k):
                    raise FrameError(f"role in {seg['id']!r} is missing {k!r}")
            for k in _LISTS:           # a role list overrides the segment list; one of them must be non-empty
                if not (r.get(k) or seg.get(k)):
                    raise FrameError(f"role {r['id']!r} in {seg['id']!r} has no {k!r}")
        for fs in seg["firm_sizes"]:
            if "label" not in fs or float(fs.get("weight", 0)) <= 0:
                raise FrameError(f"firm size in {seg['id']!r} needs a label and a positive (hypothetical) weight")
    for sc in raw.get("scenarios") or []:
        for k in ("id", "title", "context"):
            if not sc.get(k):
                raise FrameError(f"scenario is missing {k!r}")


def frame_from_dict(raw: Mapping[str, Any]) -> PersonaSamplingFrame:
    _validate(raw)
    return PersonaSamplingFrame(
        frame_id=str(raw["frame_id"]), version=str(raw["version"]), kind=str(raw["kind"]),
        evidence_basis=str(raw["evidence_basis"]), segments=tuple(raw["segments"]),
        scenarios=tuple(raw.get("scenarios") or ()), control_segments=tuple(raw.get("control_segments") or ()),
        raw=dict(raw))


def load_frame(path_or_name: "str | Path") -> PersonaSamplingFrame:
    """Load a frame from a YAML path, or by bare name from the packaged ``scenarios/`` directory."""
    import yaml                                  # pyyaml is a base dependency of agentic_os; imported lazily
    p = Path(path_or_name)
    if not p.suffix:
        p = SCENARIO_DIR / f"{path_or_name}.yaml"
    with open(p, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise FrameError(f"{p}: expected a mapping at the top level")
    return frame_from_dict(raw)


# ── sampling ───────────────────────────────────────────────────────────────────────────────────────────────
def largest_remainder(weights: Sequence[float], n: int) -> List[int]:
    """Allocate ``n`` units over ``weights`` (Hamilton method): sums to n, deterministic, ties by index."""
    tot = math.fsum(weights)
    if tot <= 0 or n < 0:
        raise ValueError("weights must be positive and n >= 0")
    quotas = [w / tot * n for w in weights]
    base = [int(math.floor(q)) for q in quotas]
    rest = n - sum(base)
    order = sorted(range(len(weights)), key=lambda i: (-(quotas[i] - base[i]), i))
    for i in order[:rest]:
        base[i] += 1
    return base


def _rotating(items: Sequence[str], rng: random.Random) -> List[str]:
    out = list(items)
    rng.shuffle(out)
    return out


def _window(order: Sequence[str], i: int, k: int) -> Tuple[str, ...]:
    if not order:
        return ()
    k = min(k, len(order))
    return tuple(order[(i * k + j) % len(order)] for j in range(k))


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def firm_size_weight_schemes(segment: Mapping[str, Any]) -> Dict[str, Dict[str, float]]:
    """Alternative HYPOTHETICAL weightings of a segment's firm sizes, for sensitivity checks: ``base`` (as
    configured), ``uniform`` and ``inverse`` (flips the emphasis). Each sums to 1."""
    labels = [f["label"] for f in segment["firm_sizes"]]
    w = [float(f["weight"]) for f in segment["firm_sizes"]]

    def norm(xs):
        s = math.fsum(xs)
        return {labels[i]: xs[i] / s for i in range(len(xs))}
    return {"base": norm(w), "uniform": norm([1.0] * len(w)), "inverse": norm([1.0 / x for x in w])}


def sample_personas(frame: PersonaSamplingFrame, per_cell: int, seed: int, *,
                    segments: Optional[Sequence[str]] = None, roles: Optional[Sequence[str]] = None,
                    scenarios: Optional[Sequence[str]] = None, include_control: bool = False,
                    level: str = "cell", k_pains: int = 2, k_criteria: int = 3,
                    k_objections: int = 2) -> List[PersonaSpec]:
    """Stratified, reproducible personas.

    ``level="cell"`` (default): ``per_cell`` personas for EVERY segment/role[/scenario] cell.
    ``level="segment"``: ``per_cell`` personas per SEGMENT, split over the segment's roles by the (hypothetical)
    role weights with the largest-remainder rule (so every role is covered once ``per_cell >= n_roles``); the
    persona's ``cell`` is then the segment id.
    """
    if per_cell < 1:
        raise ValueError("per_cell must be >= 1")
    if level not in ("cell", "segment"):
        raise ValueError("level must be 'cell' or 'segment'")
    out: List[PersonaSpec] = []
    fh = frame.content_hash
    segs = list(frame.segments) + (list(frame.control_segments) if include_control else [])
    for seg in segs:
        if segments and seg["id"] not in segments:
            continue
        sel_roles = [r for r in seg["roles"] if not roles or r["id"] in roles]
        if not sel_roles:
            continue
        scens = [None] if not frame.scenarios else [sc for sc in frame.scenarios if not scenarios or sc["id"] in scenarios]
        if level == "segment":
            alloc = largest_remainder([float(r.get("weight", 1.0)) for r in sel_roles], per_cell)
            plan = [(seg["id"], r, None, a) for r, a in zip(sel_roles, alloc) if a > 0]
        else:
            plan = []
            for r in sel_roles:
                for sc in scens:
                    plan.append((f"{seg['id']}/{r['id']}" + (f"/{sc['id']}" if sc else ""), r, sc, per_cell))
        for cell, role, scen, n in plan:
            if scen is None and frame.scenarios and level == "segment":
                raise FrameError("level='segment' is not defined for frames with scenarios")
            rkey = f"{seg['id']}/{role['id']}" + (f"/{scen['id']}" if scen else "")
            out.extend(_sample_cell(frame, fh, seg, role, scen, cell, rkey, n, seed, k_pains, k_criteria, k_objections))
    return out


def _sample_cell(frame, fh, seg, role, scen, cell, rkey, n, seed, k_pains, k_criteria, k_objections):
    rng = random.Random(f"{seed}|{fh}|{rkey}")
    sizes = [f["label"] for f in seg["firm_sizes"]]
    size_w = [float(f["weight"]) for f in seg["firm_sizes"]]
    alloc = largest_remainder(size_w, n)
    size_seq = [s for s, c in zip(sizes, alloc) for _ in range(c)]
    rng.shuffle(size_seq)
    size_weight = {f["label"]: float(f["weight"]) / math.fsum(size_w) for f in seg["firm_sizes"]}
    stacks = _rotating([", ".join(s) if isinstance(s, (list, tuple)) else str(s)
                        for s in (role.get("stacks") or seg.get("stacks") or [[]])], rng)
    pains = _rotating(list(role.get("pains") or seg["pains"]), rng)
    crit = _rotating(list(role.get("purchase_criteria") or seg["purchase_criteria"]), rng)
    obj = _rotating(list(role.get("objections") or seg["objections"]), rng)
    cons_src = list(role.get("constraints") or seg.get("constraints") or [])
    if scen:
        cons_src += list(scen.get("constraints") or [])
    cons = _rotating(cons_src, rng)
    role_w = float(role.get("weight", 1.0))
    out = []
    for i in range(n):
        size = size_seq[i]
        stack = stacks[i % len(stacks)]
        out.append(PersonaSpec(
            persona_id=f"{_slug(rkey)}.{i + 1:02d}", frame_id=frame.frame_id, frame_version=frame.version,
            cell=cell, segment=seg["id"], industry=seg["industry"], role=role["label"], firm_size=size,
            decision_role=role["decision_role"], budget_authority=role["budget_authority"],
            stack=tuple(x.strip() for x in stack.split(",") if x.strip()),
            pains=_window(pains, i, k_pains), purchase_criteria=_window(crit, i, k_criteria),
            objections_to_probe=_window(obj, i, k_objections), constraints=_window(cons, i, 1 if not scen else 2),
            scenario_id=scen["id"] if scen else "", scenario_context=scen["context"] if scen else "",
            hypothetical_weight=round(role_w * size_weight[size], 6), weight_label=HYPOTHETICAL, seed=seed))
    return out


def coverage(personas: Sequence[PersonaSpec], frame: PersonaSamplingFrame) -> Dict[str, Any]:
    """Which cells and which listed pains/criteria/objections the sample covers (for the manifest and tests)."""
    cells = sorted({p.cell for p in personas})
    seen: Dict[str, set] = {"pains": set(), "criteria": set(), "objections": set()}
    for p in personas:
        seen["pains"].update(p.pains)
        seen["criteria"].update(p.purchase_criteria)
        seen["objections"].update(p.objections_to_probe)
    return {"cells": cells, "n_cells": len(cells), "per_cell": {c: sum(1 for p in personas if p.cell == c) for c in cells},
            "distinct_pains": len(seen["pains"]), "distinct_criteria": len(seen["criteria"]),
            "distinct_objections": len(seen["objections"])}


def post_stratified_mean(values: Mapping[str, float], personas: Sequence[PersonaSpec],
                         weights_by_size: Mapping[str, float]) -> float:
    """Re-weight per-persona values by firm-size group under an ALTERNATIVE hypothetical weighting.
    Groups with no persona are dropped and the remaining weights renormalised (reported by the caller)."""
    groups: Dict[str, List[float]] = {}
    for p in personas:
        if p.persona_id in values:
            groups.setdefault(p.firm_size, []).append(values[p.persona_id])
    if not groups:
        raise ValueError("no values to weight")
    tot = math.fsum(weights_by_size.get(g, 0.0) for g in groups)
    if tot <= 0:
        raise ValueError("no weight mass on the observed firm sizes")
    return math.fsum(weights_by_size.get(g, 0.0) / tot * (math.fsum(v) / len(v)) for g, v in groups.items())


__all__ = ["PersonaSamplingFrame", "FrameError", "SCENARIO_DIR", "HYPOTHETICAL", "load_frame", "frame_from_dict",
           "sample_personas", "largest_remainder", "firm_size_weight_schemes", "coverage", "post_stratified_mean"]
