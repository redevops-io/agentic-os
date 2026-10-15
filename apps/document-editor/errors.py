"""Typed errors for the document-editor connector.

Every refusal is a ``DocEditorError`` with a stable ``code``. ``to_result()`` renders it as a *failure-shaped*
result (``status`` is in ``agentic_os.mission.verify.FAILED_STATUSES``) so the Mission verifier can never read a
refusal as success, and so a side-effecting handler cannot accidentally "prove" execution.
"""
from __future__ import annotations

from typing import Any


class DocEditorError(Exception):
    code = "error"
    status = "error"          # a member of mission.verify.FAILED_STATUSES
    http_status = 400

    def __init__(self, detail: str = "", **extra: Any):
        super().__init__(detail or self.code)
        self.detail = detail or self.code
        self.extra = extra

    def to_result(self) -> dict:
        out = {"status": self.status, "error": self.code, "detail": self.detail, "executed": False}
        out.update(self.extra)
        return out


class ConfigError(DocEditorError):
    code, status, http_status = "config_error", "not_configured", 503


class UnsupportedCapability(DocEditorError):
    code, status, http_status = "unsupported_capability", "denied", 501


class BackendUnavailable(DocEditorError):
    code, status, http_status = "backend_unavailable", "unavailable", 503


class BackendError(DocEditorError):
    code, status, http_status = "backend_error", "error", 502


class OutcomeUnknown(DocEditorError):
    """A non-idempotent write may or may not have happened (network failure after send). NEVER retried blindly."""
    code, status, http_status = "outcome_unknown", "unverifiable", 502


class PolicyDenied(DocEditorError):
    code, status, http_status = "policy_denied", "denied", 403


class ApprovalRequired(DocEditorError):
    code, status, http_status = "approval_required", "pending_approval", 202


class StaleVersion(DocEditorError):
    code, status, http_status = "stale_version", "conflict", 409


class Conflict(DocEditorError):
    code, status, http_status = "conflict", "conflict", 409


class InvalidPath(DocEditorError):
    code, status, http_status = "invalid_path", "invalid", 400


class NotFound(DocEditorError):
    code, status, http_status = "not_found", "not_found", 404


class TenantMismatch(DocEditorError):
    code, status, http_status = "tenant_mismatch", "forbidden", 403


class SSRFBlocked(DocEditorError):
    code, status, http_status = "ssrf_blocked", "blocked", 403


class InvalidInput(DocEditorError):
    code, status, http_status = "invalid_input", "invalid", 400


class VerificationFailed(DocEditorError):
    code, status, http_status = "verification_failed", "unverifiable", 502
