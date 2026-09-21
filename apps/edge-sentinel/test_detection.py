"""Phase 4 acceptance: no publish without validation, replay evidence and approval; deliberately bad
detections prove the gates reject them.
"""
from __future__ import annotations

import importlib

import pytest

detection = importlib.import_module("edge-sentinel.detection")

DetectionArtifact = detection.DetectionArtifact
DetectionLanguage = detection.DetectionLanguage
ApprovalState = detection.ApprovalState
validate = detection.validate
replay = detection.replay
approve = detection.approve
publish = detection.publish
PublishRejected = detection.PublishRejected
validate_sigma = detection.validate_sigma
validate_yara = detection.validate_yara

GOOD_SIGMA = """
title: SSH brute force
logsource:
  product: linux
  service: sshd
detection:
  selection:
    event: failed_login
    service: sshd
  condition: selection
level: high
"""

BAD_SIGMA = """
title: broken
detection:
  condition: selection_that_does_not_exist
"""

CORPUS = [
    {"event": "failed_login", "service": "sshd", "label": "malicious"},
    {"event": "failed_login", "service": "sshd", "label": "malicious"},
    {"event": "login", "service": "sshd", "label": "benign"},
    {"event": "failed_login", "service": "nginx", "label": "benign"},
]


def _good() -> DetectionArtifact:
    return DetectionArtifact(language=DetectionLanguage.SIGMA, content=GOOD_SIGMA, target_backend="wazuh")


# ── validation ──

def test_valid_sigma_passes_and_bad_sigma_fails():
    assert validate_sigma(GOOD_SIGMA)["valid"] is True
    r = validate_sigma(BAD_SIGMA)
    assert r["valid"] is False and any("undefined selection" in e for e in r["errors"])
    assert validate_sigma("::: not yaml :::")["valid"] is False   # unparseable
    assert validate_sigma("title: x")["valid"] is False           # missing logsource/detection


def test_bad_yara_fails():
    assert validate_yara('rule R { strings: $a="x" condition: $a }')["valid"] is True
    assert validate_yara("not a rule")["valid"] is False
    assert validate_yara("rule R { condition: true ")["valid"] is False   # unbalanced braces


# ── the publish gate: needs validation + replay + approval ──

def test_cannot_publish_without_validation():
    det = _good()
    with pytest.raises(PublishRejected):
        publish(det)                                      # never validated


def test_cannot_publish_without_replay():
    det = validate(_good())
    assert det.approval_state is ApprovalState.VALIDATED
    with pytest.raises(PublishRejected):
        approve(det, "eng")                               # no replay evidence yet


def test_cannot_publish_without_approval():
    det = replay(validate(_good()), CORPUS)
    assert det.replay_result["tp"] == 2 and det.replay_result["fp"] == 0
    with pytest.raises(PublishRejected):
        publish(det)                                      # validated + replayed but not approved


def test_full_gated_lifecycle_publishes():
    det = _good()
    det = validate(det); assert det.approval_state is ApprovalState.VALIDATED
    det = replay(det, CORPUS); assert det.approval_state is ApprovalState.REPLAYED
    assert det.replay_result["precision"] == 1.0 and det.test_corpus_digest
    det = approve(det, "detection-eng")
    receipt = publish(det)
    assert det.approval_state is ApprovalState.PUBLISHED
    assert receipt["validation"]["valid"] and receipt["replay"]["tp"] == 2
    assert receipt["receipt"]["approved_by"] == "detection-eng"


def test_deliberately_bad_detection_is_rejected_by_the_gates():
    """A malformed Sigma is REJECTED at validation and can never publish or be approved."""
    det = validate(DetectionArtifact(language=DetectionLanguage.SIGMA, content=BAD_SIGMA))
    assert det.approval_state is ApprovalState.REJECTED
    with pytest.raises(PublishRejected):
        approve(det, "eng")
    with pytest.raises(PublishRejected):
        publish(det)


def test_replay_flags_a_noisy_detection():
    """A detection that also fires on benign events shows false positives in its replay evidence — the
    reviewer sees precision drop before approving."""
    noisy = DetectionArtifact(language=DetectionLanguage.SIGMA, content="""
title: noisy
logsource:
  product: linux
detection:
  selection:
    service: sshd
  condition: selection
""")
    noisy = replay(validate(noisy), CORPUS)
    assert noisy.replay_result["fp"] == 1        # fires on the benign sshd login
    assert noisy.replay_result["precision"] < 1.0
