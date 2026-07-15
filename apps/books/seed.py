#!/usr/bin/env python3
"""Repeatable seeder for the Summit Roofing Co. books on self-hosted ERPNext (v15/16).

Bootstrap method (the reliable one for this frappe_docker install): copy
seed_erpnext.py into the running ERPNext backend container and run it with the bench
virtualenv python, which boots a frappe context and creates/updates the company, fiscal
years, customers, suppliers, invoices, payments, journals, and bank transactions — all
idempotently. Then this wrapper (re)generates the Administrator API key/secret so the
agent can read over REST, and writes agents/books/.env.

Why the bench venv python and not `bench --site frontend console`:
  - `bench console` is IPython; piping a multi-line script into it over `docker exec`
    is unreliable (continuation prompts swallow lines and buffer stdout).
  - `env/bin/python seed_erpnext.py` runs the script directly; seed_erpnext.py calls
    frappe.init()/connect() itself when no request context exists.

Usage:
    python3 seed.py
    ERPNEXT_BACKEND=erpnext-backend-1 FRAPPE_SITE=frontend python3 seed.py

Env knobs:
    ERPNEXT_BACKEND   backend container name (default: erpnext-backend-1)
    FRAPPE_SITE       frappe site name        (default: frontend)
    ERPNEXT_URL       REST base for the .env   (default: http://host.docker.internal:8092)
    ERPNEXT_FRONT_URL ERPNext UI link          (default: http://localhost:8092)
    SSH_HOST          ssh target running docker (default: "" = local docker; set an ssh host to run remotely)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED_RB = HERE / "seed_erpnext.py"           # the in-container frappe script
ENV_OUT = HERE / ".env"

BACKEND = os.environ.get("ERPNEXT_BACKEND", "erpnext-backend-1")
SITE = os.environ.get("FRAPPE_SITE", "frontend")
ERPNEXT_URL = os.environ.get("ERPNEXT_URL", "http://host.docker.internal:8092")
ERPNEXT_FRONT_URL = os.environ.get("ERPNEXT_FRONT_URL", "http://localhost:8092")
SSH_HOST = os.environ.get("SSH_HOST", "")  # ssh target running docker; "" = local docker

IN_CONTAINER = "/tmp/seed_erpnext.py"
BENCH_PY = "/home/frappe/frappe-bench/env/bin/python"
SITES_DIR = "/home/frappe/frappe-bench/sites"

# `sudo` is required to talk to the docker socket on this host.
DOCKER = ["sudo", "docker"]


def _wrap(cmd: list[str]) -> list[str]:
    """Wrap a command to run over ssh if SSH_HOST is set, else run locally."""
    if SSH_HOST:
        return ["ssh", SSH_HOST, " ".join(cmd)]
    return cmd


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


# A small frappe snippet to (re)generate Administrator API keys and print them.
GENKEYS = r'''
import frappe, json
from frappe.utils.password import get_decrypted_password
if not getattr(frappe.local, "site", None):
    frappe.init(site="%s"); frappe.connect(); frappe.set_user("Administrator")
api_key = frappe.generate_hash(length=15)
api_secret = frappe.generate_hash(length=15)
u = frappe.get_doc("User", "Administrator")
u.api_key = api_key; u.api_secret = api_secret
u.save(ignore_permissions=True); frappe.db.commit()
dec = get_decrypted_password("User","Administrator","api_secret")
open("/tmp/books_keys.json","w").write(json.dumps({"api_key":api_key,"api_secret":api_secret,"decrypt_ok":dec==api_secret}))
print("KEYS_WRITTEN")
'''


def main() -> int:
    if not SEED_RB.exists():
        print(f"seed_erpnext.py not found at {SEED_RB}", file=sys.stderr)
        return 1

    if SSH_HOST:
        # copy seed_erpnext.py to the ssh host, then into the container
        with open(SEED_RB) as f:
            cp_host = subprocess.run(["ssh", SSH_HOST, f"cat > {IN_CONTAINER}"],
                                     stdin=f, text=True, capture_output=True)
        if cp_host.returncode != 0:
            print("scp to host failed:\n" + cp_host.stderr, file=sys.stderr)
            return 1
    cp = run(_wrap(DOCKER + ["cp", IN_CONTAINER if SSH_HOST else str(SEED_RB),
                             f"{BACKEND}:{IN_CONTAINER}"]))
    if cp.returncode != 0:
        print("docker cp failed:\n" + cp.stderr, file=sys.stderr)
        return 1

    # 1. Run the idempotent seed inside the container.
    seed_cmd = (DOCKER + ["exec", BACKEND, "bash", "-lc",
                f"cd {SITES_DIR} && FRAPPE_SITE={SITE} {BENCH_PY} {IN_CONTAINER}"])
    res = run(_wrap(seed_cmd))
    out = res.stdout + "\n" + res.stderr
    seed_ok = next((l for l in out.splitlines() if l.startswith("SEED_OK")), None)
    if not seed_ok:
        print("Seeding did not report success. Output:\n" + out, file=sys.stderr)
        return 1
    print(seed_ok)

    # 2. (Re)generate Administrator API keys (re-encrypts with the live encryption_key).
    keys_script = GENKEYS % SITE
    if SSH_HOST:
        subprocess.run(["ssh", SSH_HOST, "cat > /tmp/books_genkeys.py"],
                       input=keys_script, text=True, capture_output=True)
    else:
        Path("/tmp/books_genkeys.py").write_text(keys_script)
    run(_wrap(DOCKER + ["cp", "/tmp/books_genkeys.py", f"{BACKEND}:/tmp/books_genkeys.py"]))
    run(_wrap(DOCKER + ["exec", BACKEND, "bash", "-lc",
                        f"cd {SITES_DIR} && FRAPPE_SITE={SITE} {BENCH_PY} /tmp/books_genkeys.py"]))
    keys_raw = run(_wrap(DOCKER + ["exec", BACKEND, "cat", "/tmp/books_keys.json"])).stdout
    try:
        keys = json.loads(keys_raw.strip().splitlines()[-1])
    except Exception:
        print("Could not read generated API keys:\n" + keys_raw, file=sys.stderr)
        return 1
    if not keys.get("decrypt_ok"):
        print("WARNING: api_secret did not round-trip decrypt (site encryption_key issue).",
              file=sys.stderr)

    # 3. Persist .env so app.py picks up the live keys automatically.
    ENV_OUT.write_text(
        f"ERPNEXT_URL={ERPNEXT_URL}\n"
        f"ERPNEXT_API_KEY={keys['api_key']}\n"
        f"ERPNEXT_API_SECRET={keys['api_secret']}\n"
        f"ERPNEXT_FRONT_URL={ERPNEXT_FRONT_URL}\n"
        f"COMPANY=Summit Roofing Co.\n"
    )
    print(f"API_KEY={keys['api_key']}")
    print(f"Wrote {ENV_OUT} (ERPNEXT_URL, ERPNEXT_API_KEY, ERPNEXT_API_SECRET, ERPNEXT_FRONT_URL, COMPANY)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
