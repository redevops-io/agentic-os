"""Uninstall — the reverse of the governed install, for Windows and macOS (and Linux).

Cleanly removes what the install stood up, in reverse: tear down the deployed module containers,
optionally remove the on-device credentials the installer provisioned, and optionally uninstall the
local AI (PAIR + engine). It is **destructive**, so it refuses without an explicit ``confirmed`` and
**keeps the user's data by default** — a full wipe (secrets + local AI) is opt-in. Everything is
recorded on the mission ledger, so an uninstall is as auditable/replayable as an install.

The physical actions run through the same injected seams as install — the ``Deployer`` (its
``teardown``) and the ``PairRunner`` (its ``uninstall``) — so tests drive it with stubs, and the OS
specifics live in those runners. The native launcher's Uninstall button calls
``docker compose down`` for the stack and then this flow for creds/local-AI; the OS-native app
uninstall (winget / Applications) is documented in deploy/launcher/README.md.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from runtime_contracts.canonical import content_hash

UNINSTALL_SCOPE = "__uninstall__"
MODULE_EVENT = "ModuleUninstalled"
SECRETS_EVENT = "SecretsRemoved"
LOCAL_AI_EVENT = "LocalAIUninstalled"
DONE_EVENT = "UninstallCompleted"


class UninstallNotConfirmed(RuntimeError):
    """Uninstall is destructive and was called without confirmed=True."""


@dataclass(frozen=True)
class UninstallReceipt:
    torn_down: Tuple[str, ...] = ()
    failed: Tuple[str, ...] = ()
    secrets_removed: Tuple[str, ...] = ()
    local_ai_removed: bool = False
    kept_data: bool = True
    contract_version: str = "uninstall-receipt/v1"

    def canonical_form(self) -> dict:
        return {
            "contract_version": self.contract_version,
            "torn_down": sorted(self.torn_down),
            "failed": sorted(self.failed),
            "secrets_removed": sorted(self.secrets_removed),
            "local_ai_removed": self.local_ai_removed,
            "kept_data": self.kept_data,
        }

    @property
    def receipt_id(self) -> str:
        return content_hash(self.canonical_form())

    @property
    def ok(self) -> bool:
        return not self.failed


def installed_modules(store) -> List[str]:
    """Fold the ledger for everything a governed install stood up (union of InstallCompleted
    module sets), so the launcher can uninstall without being handed a list."""
    mods: set[str] = set()
    for e in store.all():
        if e.type == "InstallCompleted":
            mods.update(e.payload.get("modules", []) or [])
    return sorted(mods)


def _remove_provisioned_secrets(secret_dir: str, namespace: str = "default") -> Tuple[str, ...]:
    """Remove the on-device secret FILES the installer provisioned (only within secret_dir).
    Returns the names removed. Never touches anything outside the given directory."""
    root = os.path.realpath(os.path.join(secret_dir, namespace))
    base = os.path.realpath(secret_dir)
    removed: List[str] = []
    if not root.startswith(base) or not os.path.isdir(root):
        return ()
    for name in sorted(os.listdir(root)):
        target = os.path.join(root, name)
        if os.path.isfile(target) and not os.path.islink(target):
            try:
                os.remove(target)
                removed.append(name)
            except OSError:
                pass
    return tuple(removed)


def uninstall(store, *, deployer, modules: Optional[List[str]] = None,
              secret_dir: Optional[str] = None, pair_runner=None,
              remove_data: bool = False, remove_local_ai: bool = False,
              confirmed: bool = False, scope: str = UNINSTALL_SCOPE) -> UninstallReceipt:
    """Reverse the install. Destructive: requires ``confirmed=True``. Keeps data unless
    ``remove_data`` (on-device credentials) / ``remove_local_ai`` (PAIR + engine) are set.

    ``modules`` defaults to what the ledger says was installed. Each is torn down via the
    deployer; failures are recorded (not raised) so one bad teardown doesn't strand the rest.
    """
    if not confirmed:
        raise UninstallNotConfirmed("uninstall is destructive — pass confirmed=True")

    mods = modules if modules is not None else installed_modules(store)
    torn: List[str] = []
    failed: List[str] = []
    for m in mods:
        try:
            deployer.teardown(m)
            torn.append(m)
            store.append(MODULE_EVENT, scope, {"module": m})
        except Exception as e:      # noqa: BLE001 — record + continue
            failed.append(m)
            store.append(MODULE_EVENT, scope, {"module": m, "error": type(e).__name__})

    secrets_removed: Tuple[str, ...] = ()
    if remove_data and secret_dir:
        secrets_removed = _remove_provisioned_secrets(secret_dir)
        if secrets_removed:
            store.append(SECRETS_EVENT, scope, {"names": list(secrets_removed)})

    local_ai_removed = False
    if remove_local_ai and pair_runner is not None:
        try:
            pair_runner.uninstall()
            local_ai_removed = True
            store.append(LOCAL_AI_EVENT, scope, {})
        except Exception:  # noqa: BLE001
            failed.append("local-ai")

    receipt = UninstallReceipt(
        torn_down=tuple(torn), failed=tuple(failed), secrets_removed=secrets_removed,
        local_ai_removed=local_ai_removed, kept_data=not remove_data)
    store.append(DONE_EVENT, scope, receipt.canonical_form())
    return receipt
