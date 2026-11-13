"""redevops_infinity: an optional RAP bridge between the Infinity agent runtime and the ReDevOps Agent Gateway.

Not part of the base install. Infinity decides when an agent runs; ReDevOps decides what it may do. See docs/INFINITY_RAP_BRIDGE_ADR.md.
"""
from .callback_auth import (CallbackClient, CallbackOrigin, CallbackVerifier, Peer, PeerRegistry, RateLimiter, sign_request)
from .contracts import CONTRACT_VERSION, ActionStatus, Reason
from .http_server import RapHttpServer
from .lineage import ChildScopePolicy, LineageManager
from .operation_binding import Arg, OperationSpec, derive_action_id
from .rap_gateway import RapGateway
from .store import OutboxStore

__version__ = "0.1.0"
__all__ = ["CONTRACT_VERSION", "ActionStatus", "Reason", "Arg", "OperationSpec", "derive_action_id", "RapGateway", "RapHttpServer", "OutboxStore",
           "Peer", "PeerRegistry", "CallbackOrigin", "CallbackClient", "CallbackVerifier", "RateLimiter", "sign_request", "ChildScopePolicy",
           "LineageManager"]
