"""Sidekick capability ``operations.model_provider``: change the model provider or its key while the stack runs.

The user says "use my new Anthropic key" or "switch to the local model"; Sidekick turns that into structured inputs
and calls this capability. It never takes a key as an input:

* the key is named by an **environment variable NAME** (``key_env``) or supplied through a secure prompt the shell
  injects (``prompt_secret``), and is read only at the moment of use;
* where it persists is an explicit choice (``persist``): ``store`` writes it to Vault / KeePassXC / the OS keyring /
  a file store behind a reference (``key_ref``), ``env_ref`` records only a reference to the variable you already
  export (``env:NAME``) and writes nothing. There is no "session only" here: this changes the RUNNING stack.

Changes are approval-gated: without a valid approval the capability returns a PREVIEW (what would change, whether
model traffic would leave the trust boundary) and a ``payload_digest``. A person approves that preview; the control
plane mints a token bound to those exact inputs (``mint_settings_approval``); the re-invocation applies it, tests the
provider with a minimal call, and rolls back to exactly the previous state if the test fails. ``show`` and ``test``
need no approval and change nothing.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

from agentic_os.mission.approval import (
    default_replay_guard, inputs_hash, mint_approval_token, verify_approval_token)
from agentic_os.sidekick.capability import CapabilityDomain as D, CapabilityRegistry, Maturity as M, SidekickCapability, default_registry

CAPABILITY_ID = "operations.model_provider"
OPERATOR = "sidekick"
_FORBIDDEN_INPUTS = ("key", "api_key", "apikey", "key_value", "secret", "token", "password", "passphrase")
_ACTIONS = ("show", "test", "set", "rotate_key")

DESCRIPTOR = SidekickCapability(
    capability_id=CAPABILITY_ID, domain=D.OPERATIONS,
    summary="Show, test, switch or rotate the model provider and its API key for the running stack, with the key held "
            "in Vault or KeePassXC (or your own exported variable) and every change approved, tested and reversible.",
    intents=("change my api key", "use my new key", "rotate the model key", "switch the model provider",
             "use the local model", "use claude", "use openai", "change model", "which model is the stack using"),
    required_inputs=("action",),
    optional_inputs=("base_url", "model", "key_env", "key_ref", "persist", "allow_external_host", "_approval"),
    produced_artifacts=("ProviderChangeReceipt",),
    candidate_actions=("show", "test", "set", "rotate_key"),
    authority_requirements=("settings.provider.write.approved",),
    verification_contract="a minimal test call after the change; rollback to the previous settings and key on failure",
    maturity=M.L2_GENERALIZED, status="PARTIAL", code_ref="agentic_os/sidekick/settings_skill.py + agentic_os/app/llm_admin.py")


def _settings_path(explicit: Optional[str]) -> str:
    path = explicit or os.environ.get("REDEVOPS_LLM_SETTINGS_FILE", "").strip()
    if not path:
        raise ValueError("no settings file: set REDEVOPS_LLM_SETTINGS_FILE (for example ~/.config/redevops/llm.json)")
    return path


def _mask(ref: str) -> str:
    return ref  # a reference is not a secret (it names where the secret is)


def mint_settings_approval(inputs: Dict[str, Any], *, ttl_s: int = 300, secret: "bytes | str | None" = None) -> Optional[str]:
    """Called by the control plane AFTER a person approved the preview: a token bound to exactly these inputs."""
    clean = {k: v for k, v in inputs.items() if not k.startswith("_approval")}
    return mint_approval_token(secret, operator=OPERATOR, capability=CAPABILITY_ID, mission_id=None, node_id=None,
                               inputs=clean, marker={"approved": True}, ttl_s=ttl_s)


def make_handler(*, prompt_secret: Optional[Callable[[str], str]] = None, settings_path: Optional[str] = None,
                 secret: "bytes | str | None" = None, replay_guard=None, **store_cfg: Any) -> Callable[..., Dict[str, Any]]:
    """Build the bound handler. ``prompt_secret(label)`` is the shell's secure prompt (never echoed or logged)."""
    guard = replay_guard if replay_guard is not None else default_replay_guard()

    def handler(action: str = "show", **inputs: Any) -> Dict[str, Any]:
        from agentic_os.app import llm_admin, llm_settings  # noqa: PLC0415
        if action not in _ACTIONS:
            return {"status": "refused", "reason": f"action must be one of {list(_ACTIONS)}"}
        bad = [k for k in inputs if k.lower() in _FORBIDDEN_INPUTS]
        if bad:
            return {"status": "refused",
                    "reason": "a key must never be passed as an input (it would land in transcripts and logs): name the "
                              "environment variable with key_env, or use the secure prompt",
                    "rejected_inputs": bad}
        approval = inputs.pop("_approval", None)
        path = _settings_path(settings_path)
        cur = llm_settings.load_llm_settings(path)

        if action == "show":
            from agentic_os.app.llm import _is_in_boundary_host  # noqa: PLC0415
            inside = _is_in_boundary_host(cur.base_url) if cur.base_url else None
            return {"status": "ok", "action": "show", "provider_base_url": cur.base_url, "model": cur.model,
                    "key_ref": _mask(cur.key_ref) or "(environment OPENAI_API_KEY)", "source": cur.source,
                    "boundary": ("inside" if inside else "OUTSIDE" if inside is False else "unset"),
                    "in_boundary_hosts": list(cur.in_boundary_hosts)}
        if action == "test":
            return {"status": "ok", "action": "test", **{"test": llm_admin.test_provider(cur)}}

        # ── a change: validate, preview, require approval ─────────────────────────────────────────
        persist = inputs.get("persist") or ("store" if inputs.get("key_ref") else "env_ref" if inputs.get("key_env") else "")
        key_env = inputs.get("key_env") or ""
        key_ref = inputs.get("key_ref") or (f"env:{key_env}" if key_env and persist == "env_ref" else "")
        if action == "rotate_key":
            if cur.source != "file" or not cur.key_ref:
                return {"status": "refused", "reason": "no stored key reference to rotate: set the provider with a key "
                                                       "reference first"}
            key_ref = key_ref or cur.key_ref
            base_url, model = cur.base_url, cur.model
        else:
            base_url, model = inputs.get("base_url") or cur.base_url, inputs.get("model") or cur.model
        if persist == "store" and key_ref.startswith("env:"):
            return {"status": "refused", "reason": "persist=store needs a store reference (file/vault/keyring/keepassxc), "
                                                   "not an env reference"}
        if persist not in ("store", "env_ref", ""):
            return {"status": "refused", "reason": "persist must be 'store' or 'env_ref'"}
        allow_external = bool(inputs.get("allow_external_host"))
        from agentic_os.app.llm import _is_in_boundary_host  # noqa: PLC0415
        inside = _is_in_boundary_host(base_url) or base_url in ("",)
        change = {"action": action, "base_url": base_url, "model": model, "key_ref": key_ref, "persist": persist,
                  "key_env": key_env, "allow_external_host": allow_external}
        digest = inputs_hash(change)
        preview = {"provider_host": base_url, "model": model, "key_reference": key_ref or "(unchanged)",
                   "persist": persist or "(settings only)",
                   "boundary": "inside your trust boundary" if inside else
                               "OUTSIDE your trust boundary: model traffic would leave it for this host",
                   "needs_explicit_external_approval": not inside}
        if not inside and not allow_external:
            return {"status": "refused", "reason": "that host is outside your trust boundary; say so explicitly "
                                                   "(allow_external_host) and approve the preview", "preview": preview}
        marker = verify_approval_token(secret, approval, operator=OPERATOR, capability=CAPABILITY_ID, inputs=change,
                                       replay_guard=guard) if approval else None
        if marker is None:
            return {"status": "pending_approval", "action": action, "preview": preview, "payload_digest": digest,
                    "payload": change,            # the exact, key-free payload a person approves and a token binds to
                    "approval_hint": "approve this preview, then call again with the approval token from "
                                     "mint_settings_approval(inputs)", "nothing_changed": True}

        # ── approved: obtain the key at the last moment, apply, test, keep or roll back ───────────
        key_value: Optional[str] = None
        if key_env:
            key_value = os.environ.get(key_env)
            if key_value is None:
                return {"status": "refused", "reason": f"environment variable {key_env} is not set"}
        elif persist == "store" and key_ref and (action == "rotate_key" or not key_env):
            if prompt_secret is None:
                return {"status": "refused", "reason": "no key source: pass key_env (the NAME of a variable) or run "
                                                       "this from a shell with a secure prompt"}
            key_value = prompt_secret(f"API key to store at {key_ref}")
        if persist == "store" and not key_value:
            return {"status": "refused", "reason": "no key was provided"}
        store_value = key_value if persist == "store" else None
        res = llm_admin.set_provider(path, base_url=base_url, model=model, key_ref=key_ref, key_value=store_value,
                                     allow_external_host=allow_external, test=True, **store_cfg)
        # for env_ref nothing is stored, but the provider is still tested with the exported key
        return {"status": "applied" if res.get("changed") else "rolled_back", "action": action, "receipt": res,
                "approved_digest": digest}

    return handler


def bind_settings_capability(registry: Optional[CapabilityRegistry] = None, **handler_kwargs: Any) -> str:
    """Register the descriptor (if absent) and bind the handler. Idempotent. Returns the capability id."""
    reg = registry or default_registry
    if reg.get(CAPABILITY_ID) is None:
        reg.register(DESCRIPTOR)
    reg.bind(CAPABILITY_ID, make_handler(**handler_kwargs))
    return CAPABILITY_ID


__all__ = ["CAPABILITY_ID", "DESCRIPTOR", "make_handler", "mint_settings_approval", "bind_settings_capability"]
