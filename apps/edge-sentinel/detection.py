"""Detection engineering — deterministic validate → replay → approve → publish gates (Phase 4).

The plan's rule: a detection is not rule *generation*, it is a *lifecycle*, and **nothing publishes without
validation, replay evidence and approval.** A model may draft a Sigma/YARA/query, but the gates are pure
and deterministic — they reject a malformed detection, a detection with no replay evidence, and a
detection that no human approved. Deliberately-bad detections must fail the gates; that is a test, below.

This implements the plan's ``DetectionArtifact`` contract and the three gates. Sigma is parsed with PyYAML
and evaluated against a *labelled* test corpus to produce replay evidence (tp/fp/fn + precision/recall);
YARA is structurally validated (a real compile via yara-python slots in behind the same seam when present).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import yaml

from .evidence import _now, canonical_json, sha256_hex


class DetectionLanguage(str, Enum):
    SIGMA = "SIGMA"; YARA = "YARA"; QUERY = "QUERY"


class ApprovalState(str, Enum):
    DRAFT = "DRAFT"; VALIDATED = "VALIDATED"; REPLAYED = "REPLAYED"; APPROVED = "APPROVED"
    PUBLISHED = "PUBLISHED"; REJECTED = "REJECTED"; RETIRED = "RETIRED"


@dataclass
class DetectionArtifact:
    """A detection through its lifecycle. Accumulates validation + replay evidence + approval; only a fully
    gated artifact may publish (see :func:`publish`)."""
    language: DetectionLanguage
    content: str
    target_backend: str = ""
    source_hypothesis: str = ""
    source_evidence_refs: tuple[str, ...] = ()
    validator: str = "es-detection-0.1.0"
    validation_result: dict | None = None       # {valid: bool, errors: [...]}
    test_corpus_digest: str = ""
    replay_result: dict | None = None           # {tp,fp,fn,tn,precision,recall}
    false_positive_notes: str = ""
    approval_state: ApprovalState = ApprovalState.DRAFT
    deployment_receipt: dict | None = None
    version: int = 1

    @property
    def detection_id(self) -> str:
        return f"det-{sha256_hex(self.language.value + '|' + self.content)[:16]}"

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["detection_id"] = self.detection_id
        d["language"] = self.language.value
        d["approval_state"] = self.approval_state.value
        d["source_evidence_refs"] = list(self.source_evidence_refs)
        return d


# ──────────────────────────── validators (pure) ────────────────────────────

def validate_sigma(content: str) -> dict:
    """Structurally validate a Sigma rule: parseable YAML with title, logsource, detection and a condition
    that references defined selections. Deterministic; returns {valid, errors}."""
    errors: list[str] = []
    try:
        doc = yaml.safe_load(content)
    except Exception as e:
        return {"valid": False, "errors": [f"unparseable YAML: {e}"]}
    if not isinstance(doc, dict):
        return {"valid": False, "errors": ["Sigma rule must be a mapping"]}
    for req in ("title", "logsource", "detection"):
        if req not in doc:
            errors.append(f"missing required field: {req}")
    det = doc.get("detection")
    if isinstance(det, dict):
        if "condition" not in det:
            errors.append("detection.condition is required")
        else:
            selections = [k for k in det if k != "condition"]
            if not selections:
                errors.append("detection defines no selection")
            cond = str(det.get("condition", ""))
            # every bare identifier in the condition must be a defined selection (or 'all'/'them'/'1 of')
            import re as _re
            for ident in _re.findall(r"[A-Za-z_][A-Za-z0-9_]*", cond):
                if ident in ("and", "or", "not", "of", "all", "them", "1", "any"):
                    continue
                if ident not in selections:
                    errors.append(f"condition references undefined selection: {ident}")
    elif det is not None:
        errors.append("detection must be a mapping")
    return {"valid": not errors, "errors": errors}


def validate_yara(content: str) -> dict:
    """Structural YARA validation (a real compile via yara-python slots in behind this seam). Checks the
    rule has a name, a condition section, and balanced braces."""
    errors: list[str] = []
    import re as _re
    if not _re.search(r"\brule\s+[A-Za-z_][A-Za-z0-9_]*\s*\{", content):
        errors.append("no 'rule <name> {' declaration")
    if "condition:" not in content:
        errors.append("missing condition: section")
    if content.count("{") != content.count("}"):
        errors.append("unbalanced braces")
    return {"valid": not errors, "errors": errors}


def validate(det: DetectionArtifact) -> DetectionArtifact:
    if det.language is DetectionLanguage.SIGMA:
        res = validate_sigma(det.content)
    elif det.language is DetectionLanguage.YARA:
        res = validate_yara(det.content)
    else:
        from .hunt import Query, validate_query
        ok, reason = validate_query(Query(backend=det.target_backend or "sql", text=det.content))
        res = {"valid": ok, "errors": [] if ok else [reason]}
    det.validation_result = res
    det.approval_state = ApprovalState.VALIDATED if res["valid"] else ApprovalState.REJECTED
    return det


# ──────────────────────────── replay against a labelled corpus (Sigma) ────────────────────────────

def _sigma_matches(doc: dict, event: dict) -> bool:
    """Evaluate a (validated) Sigma rule against one event. Supports equality and the |contains modifier
    over AND-ed fields within a selection, with an OR across selections named in the condition."""
    det = doc.get("detection", {})
    selections = {k: v for k, v in det.items() if k != "condition"}

    def sel_matches(sel) -> bool:
        if not isinstance(sel, dict):
            return False
        for key, want in sel.items():
            field_name, _, mod = key.partition("|")
            got = event.get(field_name)
            wants = want if isinstance(want, list) else [want]
            if mod == "contains":
                if not any(str(w).lower() in str(got).lower() for w in wants if got is not None):
                    return False
            else:
                if str(got) not in [str(w) for w in wants]:
                    return False
        return True

    cond = str(det.get("condition", "")).lower()
    named = [s for s in selections if s.lower() in cond]
    if "1 of" in cond or " or " in cond:
        return any(sel_matches(selections[s]) for s in (named or selections))
    return all(sel_matches(selections[s]) for s in (named or selections))


def replay(det: DetectionArtifact, corpus: list[dict]) -> DetectionArtifact:
    """Run a validated Sigma detection over a labelled corpus (each event has label 'malicious'/'benign')
    and record tp/fp/fn/tn + precision/recall as replay evidence. Deterministic."""
    if det.language is not DetectionLanguage.SIGMA or not (det.validation_result or {}).get("valid"):
        det.replay_result = {"error": "replay requires a validated Sigma detection"}
        return det
    doc = yaml.safe_load(det.content)
    tp = fp = fn = tn = 0
    for ev in corpus:
        malicious = ev.get("label") == "malicious"
        hit = _sigma_matches(doc, ev)
        tp += hit and malicious
        fp += hit and not malicious
        fn += (not hit) and malicious
        tn += (not hit) and not malicious
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    det.replay_result = {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
                         "precision": round(precision, 3), "recall": round(recall, 3)}
    det.test_corpus_digest = sha256_hex(canonical_json(corpus))
    if det.approval_state is ApprovalState.VALIDATED:
        det.approval_state = ApprovalState.REPLAYED
    return det


# ──────────────────────────── approval + publish gate ────────────────────────────

class PublishRejected(Exception):
    pass


def approve(det: DetectionArtifact, reviewer: str) -> DetectionArtifact:
    """A human approves — allowed ONLY after validation + replay evidence exist. No auto-approval."""
    if not (det.validation_result or {}).get("valid"):
        raise PublishRejected("cannot approve an unvalidated/invalid detection")
    if not det.replay_result or "error" in det.replay_result:
        raise PublishRejected("cannot approve without replay evidence")
    det.approval_state = ApprovalState.APPROVED
    det.deployment_receipt = {"approved_by": reviewer, "approved_at": _now()}
    return det


def publish(det: DetectionArtifact) -> dict:
    """The gate. Refuses unless validation passed, replay evidence exists, AND a human approved. Returns a
    deployment receipt; a deliberately-bad detection cannot reach here."""
    if not (det.validation_result or {}).get("valid"):
        raise PublishRejected("no publish: detection did not pass validation")
    if not det.replay_result or "error" in det.replay_result:
        raise PublishRejected("no publish: no replay evidence")
    if det.approval_state is not ApprovalState.APPROVED:
        raise PublishRejected("no publish: not approved")
    det.approval_state = ApprovalState.PUBLISHED
    return {"detection_id": det.detection_id, "published_at": _now(),
            "validation": det.validation_result, "replay": det.replay_result,
            "receipt": det.deployment_receipt}
