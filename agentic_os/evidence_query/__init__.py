"""Authorized, sandboxed, auditable evidence queries and replay authorization (D-1). See docs/EVIDENCE_REPLAY_BOUNDARY.md."""
from . import worker
from .artifacts import ArtifactFile, ArtifactRoot
from .audit import JsonlAuditSink, MemoryAuditSink, statement_sha256
from .authz import ACTIONS, Authenticator, Ceiling, GrantsCeiling, IdentityCeiling, SingleOwnerTokenAuthenticator, StaticCeiling, perm
from .catalog import DatasetSpec, ScopeCatalog, ScopeData, ViewSpec, event_store_dataset
from .errors import ArtifactError, IntegrityError, QueryRefused
from .limits import Limits
from .ratelimit import RateLimiter
from .replay import ReplayController, ReplayRefused
from .service import EvidenceQueryService, QueryResult

__all__ = [n for n in dir() if not n.startswith("_")]
