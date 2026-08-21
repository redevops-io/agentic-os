"""agentic-os — the control plane for the redevops.io Agentic Business OS.

Public surface:
    Registry   — load + validate the module catalog (modules.yaml)
    Router     — cost-aware LLM routing across local/cheap/premium tiers
    Context    — shared business context + an append-only approvals/audit log
    Fleet      — orchestrator: deploy modules and run their agents
"""

from .registry import Module, Registry
from .router import Router, Tier, Task
from .context import Context, Approval
from .fleet import Fleet

__all__ = ["Module", "Registry", "Router", "Tier", "Task", "Context", "Approval", "Fleet"]
__version__ = "0.2.3"
