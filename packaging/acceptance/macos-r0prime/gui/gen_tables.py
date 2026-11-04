#!/usr/bin/env python3
"""Render the scenario result JSON files as markdown tables (for the acceptance report). Usage: gen_tables.py <reports-data> [maxdetail]"""
import json, sys, os
d = sys.argv[1]; maxd = int(sys.argv[2]) if len(sys.argv) > 2 else 170
def esc(s): return str(s).replace("|", "/").replace("\n", " ").replace(os.environ.get("S", "@@"), "$S")
for name in ("lifecycle", "negative", "browser", "failures", "extra"):
    p = f"{d}/{name}/{name}.json"
    if not os.path.exists(p): continue
    rows = json.load(open(p))
    print(f"\n#### {name} ({len(rows)} rows, {sum(1 for r in rows if r['result']=='FAIL')} FAIL)\n")
    print("| ID | result | evidence | test | detail |\n|---|---|---|---|---|")
    for r in rows:
        det = esc(r.get("detail", ""))
        print(f"| {r['id']} | {r['result']} | {r['level']} | {esc(r['desc'])} | {det[:maxd]}{'...' if len(det) > maxd else ''} |")
