"""Errors. Messages are fixed, short and never echo caller-supplied paths or another scope's names."""
from __future__ import annotations


class QueryRefused(Exception):
    """The request did not run. ``code`` is machine readable; the message is safe to show the caller."""

    MESSAGES = {
        "unauthenticated": "authentication required",
        "not_found": "scope not found",                       # unknown scope AND not authorized look identical
        "statement_rejected": "statement rejected: only a single read-only SELECT over the scope's tables is allowed",
        "timeout": "query exceeded the time limit",
        "limit": "query exceeded a resource limit",
        "error": "query failed",
        "integrity": "an evidence artifact failed its integrity check",
        "audit_unavailable": "the audit log is unavailable; the query was not run",
        "rate_limited": "too many requests",
    }

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or self.MESSAGES.get(code, "refused"))


class ArtifactError(Exception):
    """A path escapes its root, is a symlink/special file, is too large or cannot be read."""


class IntegrityError(ArtifactError):
    """An artifact's bytes do not match the recorded sha256."""
