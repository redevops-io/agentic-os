"""PostgresEventStore against a REAL Postgres container on the proxmox docker host.

Gated on RDO_PROXMOX_IT=1 (offline suite skips it). Proves the shared-substrate payoff with
no Windows/macOS/iOS hardware: a Postgres container is stood up on proxmox, one store instance
appends a mission, and a *second* store instance (a stand-in for another device, same DSN)
rehydrates it by fold — the A→B resume the multi-instance program is built on.

Run:  RDO_PROXMOX_IT=1 pytest tests/test_event_store_postgres.py -q
Env:  RDO_PROXMOX_SSH (default "proxmox"), RDO_PROXMOX_HOST (default "192.168.40.105")
"""
import os
import secrets
import subprocess
import time

import pytest

from agentic_os.mission.store import MissionRepository
from agentic_os.mission.event_backends import PostgresEventStore

pytestmark = pytest.mark.skipif(
    os.environ.get("RDO_PROXMOX_IT") != "1",
    reason="proxmox integration test (set RDO_PROXMOX_IT=1 to run)")

SSH = os.environ.get("RDO_PROXMOX_SSH", "proxmox")
HOST = os.environ.get("RDO_PROXMOX_HOST", "192.168.40.105")
NAME = "rdo-pg-test"
IMAGE = "postgres:16-alpine"


def _docker(*args, check=True):
    return subprocess.run(["ssh", SSH, "docker", *args],
                          capture_output=True, text=True, timeout=120, check=check)


@pytest.fixture(scope="module")
def dsn():
    pw = secrets.token_hex(16)                          # ephemeral; never logged
    port = 35000 + secrets.randbelow(900)
    _docker("rm", "-f", NAME, check=False)
    # This host's docker (nested under LXC) denies AF_UNIX under the default apparmor/seccomp
    # profiles, so Postgres cannot create its socket during initdb; unconfine both — the same
    # config the host's existing *-db containers run with.
    _docker("run", "-d", "--rm", "--name", NAME,
            "--security-opt", "seccomp=unconfined", "--security-opt", "apparmor=unconfined",
            "-e", f"POSTGRES_PASSWORD={pw}", "-p", f"{port}:5432", IMAGE)
    url = f"host={HOST} port={port} dbname=postgres user=postgres password={pw}"
    try:
        _wait_ready(url)
        yield url
    finally:
        _docker("rm", "-f", NAME, check=False)


def _wait_ready(dsn: str, timeout: float = 40.0):
    import psycopg
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            psycopg.connect(dsn, connect_timeout=3).close()
            return
        except Exception as e:                          # noqa: BLE001 — poll until up
            last = str(e)
            time.sleep(1.0)
    raise RuntimeError(f"postgres not ready: {last}")


def test_postgres_backend_conformance(dsn):
    store = PostgresEventStore(dsn)
    seen = []
    store.subscribe(seen.append)
    a = store.append("A", "m1", {"n": 1})
    b = store.append("B", "m1", {"n": 2})
    store.append("C", "m2", {"n": 3})
    assert a.seq < b.seq                                # DB-assigned sequence
    assert [e.type for e in store.for_mission("m1")] == ["A", "B"]
    assert store.mission_ids() == ["m1", "m2"]
    assert [e.type for e in seen] == ["A", "B", "C"]
    store.close()


def test_two_instances_share_the_ledger_A_creates_B_resumes(dsn):
    # instance A (one "device") creates a mission
    a = PostgresEventStore(dsn)
    a.append("MissionOpened", "shared-mission", {"goal": "review repo"})
    a.append("StepDone", "shared-mission", {"step": 1})
    a.close()
    # instance B (another "device", same shared DSN) rehydrates it by fold — never saw A's memory
    b = PostgresEventStore(dsn)
    timeline = MissionRepository(b).timeline("shared-mission")
    assert [t["type"] for t in timeline] == ["MissionOpened", "StepDone"]
    b.close()
