"""`agentic-os` CLI — drive the OS from the terminal.

    agentic-os modules            list the catalog
    agentic-os up [names...]      bring modules up + their agents
    agentic-os down [names...]    stop modules
    agentic-os status             fleet health
    agentic-os approvals          list pending approvals
    agentic-os approve <id>       approve a pending action
    agentic-os reject <id>        reject a pending action
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
    args = p.parse_args(argv)

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
