"""Fixtures for the SOC 2 tests: a vault with registered producers, an envelope builder and a fixed clock."""
from __future__ import annotations

import json

from .collectors import build_envelope
from .vault import EvidenceVault, ProducerKeys

NOW = 1_800_000_000.0
DAY = 86_400.0
PERIOD = (NOW - 30 * DAY, NOW)
SECRET_A = b"collector-a-secret-0123456789abcdef"
SECRET_B = b"collector-b-secret-0123456789abcdef"


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t


def keys() -> ProducerKeys:
    k = ProducerKeys()
    k.register("key-a", "collector-a", ["acme"], SECRET_A)
    k.register("key-b", "collector-b", ["acme", "globex"], SECRET_B)
    return k


def make_vault(tmp_path, clock=None) -> EvidenceVault:
    return EvidenceVault(str(tmp_path / "vault.db"), keys(), clock=clock or Clock())


def env(kind, payload, *, tenant="acme", scope="prod", producer="collector-a", key_id="key-a", secret=SECRET_A, cov=PERIOD, nonce=None, observed_at=None):
    return build_envelope(kind, payload, tenant=tenant, scope=scope, source="test", producer=producer, key_id=key_id, secret=secret,
                          observed_at=observed_at or cov[1], coverage_from=cov[0], coverage_to=cov[1], nonce=nonce)


def accounts(*rows, complete=True):
    return {"accounts": list(rows), "population_complete": complete}


def acct(id, *, privileged=True, mfa=True, owner="owner-1", reviewed_by="reviewer-1", reviewed_at=NOW - 5 * DAY, status="active", terminated_at=None,
         access_revoked_at=None):
    return {"id": id, "privileged": privileged, "mfa": mfa, "owner": owner, "reviewed_by": reviewed_by, "reviewed_at": reviewed_at, "status": status,
            "terminated_at": terminated_at, "access_revoked_at": access_revoked_at}


def release(id, *, author="dev-1", approvers=("dev-2",), required=("ci", "scan"), passed=("ci", "scan"), at=NOW - 3 * DAY):
    return {"id": id, "author": author, "approvers": list(approvers), "required_checks": list(required), "passed_checks": list(passed), "deployed_at": at}
