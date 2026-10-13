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
