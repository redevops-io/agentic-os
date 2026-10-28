"""`agentic-os` CLI — drive the OS from the terminal.

    agentic-os modules            list the catalog
    agentic-os up [names...]      bring modules up + their agents
    agentic-os down [names...]    stop modules
    agentic-os status             fleet health
    agentic-os approvals          list pending approvals
    agentic-os approve <id>       approve a pending action
    agentic-os reject <id>        reject a pending action
    agentic-os secrets init|list|get|rotate|push|where   platform secrets (docs/SECRETS.md)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from .context import Context
from .fleet import Fleet
from .registry import Registry
from .router import Router


def _fleet() -> Fleet:
    registry = Registry.load()
    cfg = {}
    cfg_path = Path(os.environ.get("AGENTIC_OS_CONFIG", "config.yaml"))
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    router = Router.from_config(cfg.get("router", {"tiers": []}))
    return Fleet(registry, router, Context(os.environ.get("AGENTIC_OS_HOME", ".agentic-os")))


def _secrets_cmd(args, b=None, stdin_tty=None) -> int:
    from .secrets.bootstrap import Bootstrap, SecretsError, basic_header
    from .secrets.managed import MANAGED, OUTREACH
    import base64
    b = b or Bootstrap()
    try:
        if args.scmd == "init":
            r = b.init(args.backend, args.dry_run, not args.no_shell_profile)
            print(f"  backend: {r['backend']}  location: {r['location']}" + ("  (dry-run, nothing written)" if r["dry_run"] else ""))
            for x in r["results"]:
                st = x.status if not r["dry_run"] or x.status != "generated" else "would-generate"
                print(f"  {x.name:26} {st:22} {','.join(x.where)} {x.note}".rstrip())
            for a in r["actions"]:
                print(f"  shell profile: {a}")
            print("  next: open a new shell (or `source` your rc file), then `agentic-os secrets list`.")
            print("        compose: docker compose --env-file " + str(b.file) + " up -d")
            print("        override: put your own KEY=value in that file (existing values are never overwritten).")
        elif args.scmd == "list":
            for row in b.status_rows():
                v = "ok" if row["valid"] else (row["reason"] if row["present"] else "missing")
                print(f"  {row['name']:26} present={'yes' if row['present'] else 'no':3} "
                      f"where={','.join(row['where']) or '-':20} valid={v}")
        elif args.scmd == "get":
            val, _ = b.resolve(args.name)
            if val is None:
                print(f"  {args.name} is not set; run `agentic-os secrets init`", file=sys.stderr)
                return 1
            if args.as_header:
                if args.name != OUTREACH:
                    print("  --as-header only applies to OUTREACH_BASIC_AUTH", file=sys.stderr)
                    return 2
                print(basic_header(val))
            elif args.decode and args.name == OUTREACH:
                u, _, pw = val.partition(":")
                print(f"user: {u}\npassword: {pw}")
            elif args.decode and args.name.endswith("_B64"):
                print(base64.b64decode(val).decode("utf-8", "replace"))
            else:
                print(val)
        elif args.scmd == "rotate":
            m = MANAGED.get(args.name)
            if not m:
                print(f"  unknown managed secret {args.name}; known: {', '.join(MANAGED)}", file=sys.stderr)
                return 2
            print(f"  WARNING: {m.rotate_warning}")
            tty = sys.stdin.isatty() if stdin_tty is None else stdin_tty
            if not args.yes:
                if not tty or input("  Rotate now? [y/N] ").strip().lower() not in ("y", "yes"):
                    print("  not rotated (pass --yes to confirm)", file=sys.stderr)
                    return 2
            b.rotate(args.name)
            print(f"  {args.name} rotated. Read it with: agentic-os secrets get {args.name}")
        elif args.scmd == "push":
            if not b.vault_configured():
                print("  error: push needs Vault (VAULT_ADDR + VAULT_TOKEN); no local fallback.", file=sys.stderr)
                return 1
            plan = b.push(args.only, dry_run=True)
            print(f"  target: {plan['location']}")
            for n in plan["added"]:
                print(f"  {n:26} {'would-add' if args.dry_run else 'will-add'}")
            for n in plan["skipped_present"]:
                print(f"  {n:26} skipped (already in Vault, not overwritten)")
            for n in plan["missing_local"]:
                print(f"  {n:26} skipped (not set locally)")
            if args.dry_run or not plan["added"]:
                print("  (dry-run, nothing written)" if args.dry_run else "  nothing to push")
                return 0
            tty = sys.stdin.isatty() if stdin_tty is None else stdin_tty
            if not args.yes:
                if not tty or input(f"  Add {len(plan['added'])} key(s) to Vault? [y/N] ").strip().lower() not in ("y", "yes"):
                    print("  not pushed (pass --yes to confirm)", file=sys.stderr)
                    return 2
            r = b.push(args.only)
            print(f"  pushed {len(r['added'])} key(s) to {r['location']}: {', '.join(r['added']) or '-'}")
        elif args.scmd == "where":
            f = b.file
            print(f"  secrets file : {f}   (compose: --env-file {f})")
            print(f"  read it      : grep '^NAME=' {f}" if b.plat != "windows" else
                  f"  read it      : Select-String '^NAME=' {f}  |  [Environment]::GetEnvironmentVariable('NAME','User')")
            print("  vault        : vault kv get -field=NAME <mount>/<REDEVOPS_VAULT_PATH>  (default vibexgen/agentic-os/stack; VAULT_MOUNT=vibexgen)")
            if b.plat == "macos":
                print("  keychain     : security find-generic-password -s redevops -a NAME -w")
            if b.plat == "windows":
                print("  credential mgr: service 'redevops', user NAME (cmdkey /list)")
            print("  see docs/SECRETS.md")
    except SecretsError as e:
        print(f"  error: {e}", file=sys.stderr)
        return 1
    return 0


def _permissions_cmd(args) -> int:
    from . import permissions_setup as ps
    from .permissions import GrantStorageError, GrantStore
    try:
        if args.pcmd == "key":
            path = ps.write_key_env_file(args.env_file, from_key_file=args.from_key_file)
            print(f"  wrote PERMISSIONS_KEY to {path} (mode 0600; the key is not printed, keep the file out of git and images)")
            print(f"  Docker Compose : docker compose --env-file {path} up   (the compose files require PERMISSIONS_KEY; no default)")
            if args.k8s_secret:
                ns = f" --namespace {args.namespace}" if args.namespace else ""
                print(f"  Kubernetes     : kubectl create secret generic {args.k8s_secret} --from-env-file={path}{ns}")
                print(f"                   then set permissions.keySecret={args.k8s_secret} in the chart values; delete {path} afterwards")
            return 0
        if args.pcmd == "init":
            r = ps.init_local_profile(args.dir, user=args.user, include_write=args.include_write, key_only=args.key_only)
            env_key = r["key_source"] == "environment"
            if args.key_only:
                print(f"  permissions directory : {r['directory']}")
                if env_key:
                    print("  key                   : PERMISSIONS_KEY is set in the environment; nothing to provision, no key file is created")
                    return 0
                print(f"  key file              : {r['key_file']} ({'created' if r['key_created'] else 'kept'}, mode 0600)")
                print(f"  next                  : export PERMISSIONS_DIR={r['directory']} AGENTIC_OS_PERMISSIONS=1   (the key file and the local profile are found there; the apps then enforce these grants)")
                return 0
            print(f"  permissions directory : {r['directory']}")
            if env_key:
                print("  key                   : PERMISSIONS_KEY from the environment seals the grants; no key file was created (keep the key in a secret store, never beside the data)")
            else:
                print(f"  key file              : {r['key_file']} ({'created' if r['key_created'] else 'kept'}, mode 0600)")
            print(f"  user                  : {r['user']}")
            print(f"  grants                : {len(r['granted'])} for bundled tools"
                  + (f", {len(r['withheld'])} write-class tools withheld (re-run with --include-write)" if r["withheld"] else ""))
            print(f"  next                  : export PERMISSIONS_DIR={r['directory']} AGENTIC_OS_PERMISSIONS=1   ("
                  + ("the same PERMISSIONS_KEY must be in the environment of every app; " if env_key else "the key file ")
                  + "and the local profile are found there; the apps then enforce these grants)")
            print(f"  note                  : {ps.INSTANCE_NOT_CALLERS}. Put authentication in front of an instance that is reachable from a network" + (", or re-run without --include-write." if args.include_write else "."))
            return 0
        if args.pcmd == "audit-identifiers":                  # READ-ONLY: never migrates, generates a key, writes or deletes anything
            directory = args.dir or ps.default_dir()
            try:
                report = GrantStore(directory, migrate_plaintext=False, allow_plaintext_dev=False, allow_generated_key_dev=False,
                                  read_only=True).audit_identifiers()
            except GrantStorageError as e:
                print(f"  error: {e}", file=sys.stderr)
                return 2
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                c = report["contract"]
                print(f"  identifier contract: Unicode {c['unicode_version']} table {c['table_sha256'][:12]}...  grants checked: {report['total']}  "
                      f"violating: {report['violations']}")
                for f in report["findings"]:
                    print(f"    {f['id'] or '(id is not a plain token)'}: " + "; ".join(f"{field}: {why}" for field, why in f["problems"]))
                if report["violations"]:
                    print("  nothing was changed. These grants authorise nothing under the current contract (they count in status().malformed). "
                          "Remedy, your choice: `agentic-os permissions revoke <id>`, then add the grant again with a corrected name.")
            return 1 if report["violations"] else 0
        store = GrantStore(args.dir or ps.default_dir())
        if args.pcmd == "migrate":
            if not args.purge_backup:
                print("  error: nothing to do — pass --purge-backup", file=sys.stderr)
                return 2
            n = store.purge_migration_backups()
            print(f"  purged {n} plaintext backup file(s) (verified the encrypted store opens; overwritten, then removed)")
            return 0
        if args.pcmd == "accept-rollback":
            n = store.accept_rollback()
            print(f"  accepted: the store at counter {n} is the new floor")
            return 0
        if args.pcmd == "recover-counter":
            if not args.confirm:
                print("  error: this re-establishes the rollback counter from the store as it is NOW: an older store restored by "
                      "mistake would be accepted. Check the store is the one you mean, then re-run with --confirm", file=sys.stderr)
                return 2
            n = store.recover_counter()
            print(f"  recorded: the rollback counter is re-established at {n}")
            return 0
        if args.pcmd == "list":
            for g in sorted(store.list(), key=lambda g: (g.resource_name, g.subject)):
                print(f"  {g.id}  {g.subject_kind}:{g.subject}  {g.resource_kind}:{g.resource_name}  {','.join(g.actions)}  by {g.created_by}")
            return 0
        if args.pcmd == "revoke":
            ok = store.remove(args.id)
            print(f"  {args.id}: {'revoked' if ok else 'no such grant'}")
            return 0 if ok else 1
    except GrantStorageError as e:
        print(f"  error: {e}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agentic-os", description="The redevops.io Agentic Business OS control plane.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("modules", help="list the module catalog")
    for name in ("up", "down"):
        sp = sub.add_parser(name, help=f"{name} modules")
        sp.add_argument("names", nargs="*", help="module names (default: all)")
    sub.add_parser("status", help="fleet health")
    sub.add_parser("approvals", help="list pending approvals")
    for name in ("approve", "reject"):
        sp = sub.add_parser(name, help=f"{name} a pending action")
        sp.add_argument("id", help="approval id")
    sp = sub.add_parser("permissions", help="local single-user permissions profile (key + explicit grants for the bundled tools)")
    sperm = sp.add_subparsers(dest="pcmd", required=True)
    pi = sperm.add_parser("init", help="create the persistent key and per-tool grants (idempotent; never replaces a key)")
    pi.add_argument("--dir", default=None, help="permissions directory (default: $PERMISSIONS_DIR or ~/.config/redevops/permissions)")
    pi.add_argument("--user", default="local", help="the local user the grants are for")
    pi.add_argument("--include-write", action="store_true", help="also grant the write-class bundled tools (state changes, scanner launchers)")
    pi.add_argument("--key-only", action="store_true", help="create only the persistent key file (no grants)")
    pm = sperm.add_parser("migrate", help="after a plaintext -> encrypted migration: purge the plaintext backup (verified, overwritten, removed)")
    pm.add_argument("--purge-backup", action="store_true", help="verify the encrypted store opens, then overwrite and remove grants.jsonl.migrated-*")
    pm.add_argument("--dir", default=None)
    pa = sperm.add_parser("accept-rollback", help="explicitly accept an OLDER grants store (after a restore) as the new floor")
    pa.add_argument("--dir", default=None)
    pc = sperm.add_parser("recover-counter", help="re-establish a MISSING rollback sidecar from the store (needs --confirm)")
    pc.add_argument("--dir", default=None)
    pc.add_argument("--confirm", action="store_true", help="I checked that the store in this directory is the one I mean")
    pai = sperm.add_parser("audit-identifiers", help="read-only: list stored grants whose names break the current identifier contract (nothing is changed)")
    pai.add_argument("--dir", default=None)
    pai.add_argument("--json", action="store_true", help="machine-readable report")
    pk = sperm.add_parser("key", help="create a PERMISSIONS_KEY env file for Docker Compose / Kubernetes (the key is never printed)")
    pk.add_argument("--env-file", required=True, help="the new 0600 file to write PERMISSIONS_KEY=... into (never overwritten)")
    pk.add_argument("--from-key-file", default=None, help="reuse the key of an existing key file (continuity for a running deployment)")
    pk.add_argument("--k8s-secret", default=None, metavar="NAME", help="also print the kubectl command that creates this Secret from the file")
    pk.add_argument("--namespace", default=None, help="namespace for the kubectl command")
    pl = sperm.add_parser("list", help="the grants in the store (no secrets)")
    pl.add_argument("--dir", default=None)
    pr = sperm.add_parser("revoke", help="revoke one grant by id")
    pr.add_argument("id")
    pr.add_argument("--dir", default=None)
    sp = sub.add_parser("secrets", help="auto-generate / inspect platform secrets")
    ss = sp.add_subparsers(dest="scmd", required=True)
    si = ss.add_parser("init", help="generate missing secrets (idempotent, never overwrites)")
    si.add_argument("--backend", choices=["auto", "file", "vault"], default="auto")
    si.add_argument("--dry-run", action="store_true")
    si.add_argument("--no-shell-profile", action="store_true")
    si.add_argument("--yes", action="store_true", help="accepted for scripts; init never prompts")
    ss.add_parser("list", help="which secrets are present/valid (no values)")
    sg = ss.add_parser("get", help="print a secret value (admin)")
    sg.add_argument("name")
    sg.add_argument("--decode", action="store_true")
    sg.add_argument("--as-header", action="store_true")
    sr = ss.add_parser("rotate", help="generate a new value")
    sr.add_argument("name")
    sr.add_argument("--yes", action="store_true")
    sp_ = ss.add_parser("push", help="copy local secrets into Vault, adding only missing keys")
    sp_.add_argument("--to", choices=["vault"], default="vault")
    sp_.add_argument("--only", nargs="+", metavar="NAME")
    sp_.add_argument("--dry-run", action="store_true")
    sp_.add_argument("--yes", action="store_true")
    ss.add_parser("where", help="platform locations and fetch commands")
    args = p.parse_args(argv)

    if args.cmd == "permissions":
        return _permissions_cmd(args)
    if args.cmd == "secrets":
        return _secrets_cmd(args)
    if args.cmd == "up":
        from .secrets.bootstrap import ensure_secrets
        ensure_secrets()

    fleet = _fleet()
    if args.cmd == "modules":
        for m in fleet.registry:
            print(f"  {m.name:18} {m.pain:32} {m.url}")
    elif args.cmd in ("up", "down"):
        for s in (fleet.up(*args.names) if args.cmd == "up" else fleet.down(*args.names)):
            print(f"  {'✓' if s.deployed else '·'} {s.name:18} {s.detail}")
    elif args.cmd == "status":
        for s in fleet.status():
            print(f"  {'running' if s.deployed else 'stopped':8} {s.name:18} agents={','.join(s.agents)}")
    elif args.cmd == "approvals":
        pend = fleet.context.pending()
        if not pend:
            print("  (nothing waiting on you)")
        for a in pend:
            print(f"  {a.id}  {a.module}/{a.action}: {a.summary}")
    elif args.cmd in ("approve", "reject"):
        ap = fleet.context.resolve(args.id, approved=args.cmd == "approve")
        print(f"  {args.id}: {ap.status}" if ap else f"  no pending approval {args.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
