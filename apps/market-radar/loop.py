"""The Market Intelligence loop on a company's OWN site: audit, recommend, approve, verify, reassess.

Market Radar's first job is "what changed on the pages we watch" (``radar.brief``). This module adds the other half of the loop, so that
research ends in a change that is checked instead of in a document:

* ``site_audit``    read the site as a search engine and a first-time visitor would: robots.txt, the sitemap, and for each page its status,
                    title, description, canonical, indexability, first heading and whether its buttons are instrumented. Every finding
                    carries a machine-checkable ``check`` so the same finding can be re-tested later.
* ``recommend``     turn the audit, the watch brief and the analytics reading into a short ranked list. Each item names its evidence and the
                    checks that will show it was done. The list has a digest.
* ``approve_plan``  a person's decision, bound to that digest (a list that changed after it was shown is not the list that was approved).
* ``verify_change`` QA of the implementation: run the approved items' checks against the LIVE site. Anything not done fails the step.
* ``reassess``      audit again and compare: what was resolved, what remains, what is new. Its output is the input of the next round.

Nothing here writes to the site, to the watch store or anywhere else. The implementation itself happens in the site's own repository, by a
person or an agent; this module only refuses to call it done until the live pages say so.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from agentic_os.mission.operator_sdk import pending_approval

from . import core

MAX_PAGES = 400            # the most pages one audit reads; a sitemap with more is audited in part, and the result says so
DEFAULT_PAGES = 200
TITLE_RANGE, DESC_RANGE = (20, 70), (50, 170)
UA = "market-radar-site-audit/1 (+read-only)"
Fetch = Callable[[str], "tuple[int, str, dict]"]


def _fetch(url: str) -> "tuple[int, str, dict]":
    with httpx.Client(timeout=15.0, follow_redirects=False, headers={"User-Agent": UA}) as c:
        r = c.get(url)
        return r.status_code, r.text[:600_000], {k.lower(): v for k, v in r.headers.items()}


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title, self.desc, self.canonical, self.robots, self.h1 = "", None, None, "", []
        self.links: List[str] = []
        self.cta_total = self.cta_tagged = 0
        self._in: Optional[str] = None
        self.analytics = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("title", "h1"):
            self._in = tag
            if tag == "h1":
                self.h1.append("")
        elif tag == "meta" and (a.get("name") or "").lower() == "description":
            self.desc = a.get("content") or ""
        elif tag == "meta" and (a.get("name") or "").lower() == "robots":
            self.robots = (a.get("content") or "").lower()
        elif tag == "link" and (a.get("rel") or "").lower() == "canonical":
            self.canonical = a.get("href") or ""
        elif tag == "script" and a.get("data-website-id"):
            self.analytics = True
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
            if "btn" in (a.get("class") or "").split():
                self.cta_total += 1
                self.cta_tagged += 1 if a.get("data-umami-event") else 0

    def handle_endtag(self, tag):
        if tag == self._in:
            self._in = None

    def handle_data(self, data):
        if self._in == "title":
            self.title += data
        elif self._in == "h1" and self.h1:
            self.h1[-1] += data


def _finding(code: str, page: str, detail: str, check: dict, severity: str = "medium") -> dict:
    return {"id": f"{code}:{page}", "code": code, "page": page, "severity": severity, "detail": detail, "check": check}


def _page_facts(site: str, path: str, fetch: Fetch) -> dict:
    status, body, headers = fetch(urljoin(site, path))
    p = _Page()
    if status == 200 and "html" in headers.get("content-type", "text/html"):
        try:
            p.feed(body)
        except Exception:  # noqa: BLE001  a page the parser cannot read is reported by its missing facts
            pass
    return {"path": path, "status": status, "title": re.sub(r"\s+", " ", p.title).strip(), "description": p.desc, "canonical": p.canonical,
            "noindex": "noindex" in p.robots or "noindex" in headers.get("x-robots-tag", "").lower(), "h1": [re.sub(r"\s+", " ", h).strip() for h in p.h1],
            "links": p.links, "cta_total": p.cta_total, "cta_tagged": p.cta_tagged, "analytics": p.analytics}


def _other_form(path: str) -> str:
    """The same address with or without its trailing slash (a file name is left alone)."""
    if path in ("", "/") or "." in path.rsplit("/", 1)[-1]:
        return path
    return path[:-1] if path.endswith("/") else path + "/"


def _parent(path: str, listed: "set[str]") -> str:
    """The nearest page above ``path`` that the sitemap lists: where a link to an orphan belongs. The home page when there is none."""
    parts = path.strip("/").split("/")
    for i in range(len(parts) - 1, 0, -1):
        up = "/" + "/".join(parts[:i]) + "/"
        if up in listed:
            return up
        if up.rstrip("/") in listed:
            return up.rstrip("/")
    return "/"


def run_check(site: str, check: dict, fetch: Fetch) -> "tuple[bool, str]":
    """One machine check against the live site. ``(passed, what was observed)``."""
    kind, path = check.get("kind"), check.get("path", "/")
    if kind == "in_sitemap":
        paths = _sitemap_paths(site, fetch)[0]
        return path in paths, f"{'listed' if path in paths else 'not listed'} in the sitemap ({len(paths)} URLs)"
    if kind in ("listed_page_loads", "links_to"):
        # Two honest ways to resolve "the sitemap offers a page that does not load" or "nothing links to it": fix the page, or stop offering it.
        subject = path if kind == "listed_page_loads" else check.get("target", "")
        if subject not in _sitemap_paths(site, fetch)[0]:
            return True, f"{subject} is no longer in the sitemap"
    f = _page_facts(site, path, fetch)
    if kind in ("status_200", "listed_page_loads"):
        return f["status"] == 200, f"HTTP {f['status']}"
    if f["status"] != 200:
        return False, f"HTTP {f['status']}"
    if kind == "title_length":
        n = len(f["title"]); return TITLE_RANGE[0] <= n <= TITLE_RANGE[1], f"title is {n} characters"
    if kind == "description_length":
        n = len(f["description"] or ""); return DESC_RANGE[0] <= n <= DESC_RANGE[1], f"description is {n} characters"
    if kind == "canonical_self":
        want = urljoin(site, path); return (f["canonical"] or "") == want, f"canonical is {f['canonical'] or 'missing'}"
    if kind == "indexable":
        return not f["noindex"], "noindex" if f["noindex"] else "indexable"
    if kind == "one_h1":
        return len(f["h1"]) == 1, f"{len(f['h1'])} first-level headings"
    if kind == "cta_instrumented":
        return f["cta_total"] == f["cta_tagged"], f"{f['cta_tagged']} of {f['cta_total']} buttons carry an event"
    if kind == "links_to":
        target = check.get("target", ""); ok = any(urlsplit(urljoin(site + path, h)).path in (target, _other_form(target)) for h in f["links"])
        return ok, f"{'links' if ok else 'does not link'} to {target}"
    if kind == "links_in_listed_form":
        listed = set(_sitemap_paths(site, fetch)[0])
        bad = sorted({u.path for u in (urlsplit(urljoin(site + path, h)) for h in f["links"]) if u.netloc in ("", urlsplit(site).netloc)
                      and u.path not in listed and _other_form(u.path) in listed})
        return not bad, (f"{len(bad)} internal link(s) are not in the form the sitemap lists: " + ", ".join(bad[:5])) if bad else "every internal link uses the listed form"
    return False, f"unknown check {kind!r}"


def _sitemap_paths(site: str, fetch: Fetch) -> "tuple[List[str], List[str]]":
    notes, urls, queue, seen = [], [], ["/sitemap-index.xml", "/sitemap.xml"], set()
    status, robots, _ = fetch(urljoin(site, "/robots.txt"))
    if status == 200:
        queue = [urlsplit(m).path for m in re.findall(r"(?im)^sitemap:\s*(\S+)", robots)] + queue
        if re.search(r"(?im)^disallow:\s*/\s*$", robots):
            notes.append("robots.txt disallows the whole site")
    else:
        notes.append(f"robots.txt answered {status}")
    while queue and len(seen) < 12:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        st, body, _ = fetch(urljoin(site, sm))
        if st != 200:
            continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            notes.append(f"{sm} is not well-formed XML"); continue
        locs = [e.text.strip() for e in root.iter() if e.tag.endswith("loc") and e.text]
        if root.tag.endswith("sitemapindex"):
            queue += [urlsplit(u).path for u in locs]
        else:
            urls += [urlsplit(u).path for u in locs if urlsplit(u).netloc == urlsplit(site).netloc]
    if not urls:
        notes.append("no sitemap with URLs of this site was found")
    return sorted(set(urls)), notes


def site_audit(inp: dict, *, fetch: Fetch = _fetch) -> dict:
    # In a mission the site comes from the goal ("... for https://example.com"); a direct call names it; a deployment may set a default.
    in_goal = re.search(r"https?://[^\s\"'<>)]+", str(inp.get("_goal") or ""))
    site = str(inp.get("site") or (in_goal.group(0) if in_goal else "") or os.environ.get("MARKET_RADAR_OWN_SITE", "")).rstrip("/.,;")
    site = "{0.scheme}://{0.netloc}".format(urlsplit(site)) if site else ""
    err = core._watch_url_error(site) if site else "give the site to audit, for example https://example.com"
    if err:
        return {"status": "error", "action": "site_audit", "error": err}
    limit = max(1, min(int(inp.get("max_pages") or DEFAULT_PAGES), MAX_PAGES))
    paths, notes = _sitemap_paths(site, fetch)
    findings: List[dict] = [_finding("SITE", "/", n, {"kind": "in_sitemap", "path": "/"}, "high") for n in notes]
    pages, linked = [], set()
    listed = set(paths)
    for path in paths[:limit]:
        f = _page_facts(site, path, fetch)
        pages.append({k: f[k] for k in ("path", "status", "title", "noindex", "cta_total", "cta_tagged")})
        off_form = set()
        for h in f["links"]:
            u = urlsplit(urljoin(site + path, h))
            if u.netloc in ("", urlsplit(site).netloc):
                linked.add(u.path if u.path.endswith("/") or "." in u.path.rsplit("/", 1)[-1] else u.path + "/")
                other = _other_form(u.path)
                if u.path not in listed and other in listed:          # the sitemap offers the page under the other form of this address
                    off_form.add(u.path)
        if f["status"] == 200 and off_form:
            findings.append(_finding("LINK_FORM", path, f"{len(off_form)} internal link(s) use an address the sitemap lists in another form (each one is answered with a redirect): "
                                     + ", ".join(sorted(off_form)[:5]), {"kind": "links_in_listed_form", "path": path}, "low"))
        if f["status"] != 200:
            findings.append(_finding("STATUS", path, f"listed in the sitemap but answers HTTP {f['status']}", {"kind": "listed_page_loads", "path": path}, "high")); continue
        if f["noindex"]:
            findings.append(_finding("NOINDEX", path, "listed in the sitemap but marked noindex", {"kind": "indexable", "path": path}, "high"))
        if not TITLE_RANGE[0] <= len(f["title"]) <= TITLE_RANGE[1]:
            findings.append(_finding("TITLE", path, f"title is {len(f['title'])} characters (want {TITLE_RANGE[0]} to {TITLE_RANGE[1]})", {"kind": "title_length", "path": path}))
        if not DESC_RANGE[0] <= len(f["description"] or "") <= DESC_RANGE[1]:
            findings.append(_finding("DESCRIPTION", path, f"description is {len(f['description'] or '')} characters (want {DESC_RANGE[0]} to {DESC_RANGE[1]})", {"kind": "description_length", "path": path}))
        if (f["canonical"] or "") != urljoin(site, path):
            findings.append(_finding("CANONICAL", path, f"canonical is {f['canonical'] or 'missing'}", {"kind": "canonical_self", "path": path}))
        if len(f["h1"]) != 1:
            findings.append(_finding("H1", path, f"{len(f['h1'])} first-level headings", {"kind": "one_h1", "path": path}, "low"))
        if f["cta_total"] != f["cta_tagged"]:
            findings.append(_finding("CTA_EVENT", path, f"{f['cta_total'] - f['cta_tagged']} of {f['cta_total']} buttons carry no analytics event", {"kind": "cta_instrumented", "path": path}, "low"))
    truncated = len(paths) > limit
    # A page that was not read may hold the only link to another page: for a partial audit "nothing links to it" cannot be said, so it is not said.
    if not truncated:
        for path in paths:
            if path != "/" and path not in linked:
                findings.append(_finding("ORPHAN", path, "in the sitemap but no page of the sitemap links to it", {"kind": "links_to", "path": _parent(path, listed), "target": path}, "low"))
    by_code: Dict[str, int] = {}
    for f in findings:
        by_code[f["code"]] = by_code.get(f["code"], 0) + 1
    audit = {"site": site, "pages_in_sitemap": len(paths), "pages_audited": len(pages), "findings": findings, "by_code": by_code,
             "pages": pages, "truncated": truncated, "not_read": max(0, len(paths) - limit)}
    return {"status": "done", "action": "site_audit", "site_audit": audit,
            "summary": f"{len(pages)} pages audited on {site}: {len(findings)} findings ({', '.join(f'{k} {v}' for k, v in sorted(by_code.items())) or 'none'})"
                       + (f"; {len(paths) - limit} page(s) of the sitemap were not read, so pages without inbound links are not reported" if truncated else "")}


def _fact(inp: dict, key: str) -> Any:
    """An upstream outcome as the runtime hands it over: the WHOLE result of the step that produced it (so the payload sits under its own
    key inside), or the payload itself in a direct call."""
    v = inp.get(key)
    return v[key] if isinstance(v, dict) and isinstance(v.get(key), dict) else v


_RANK = {"high": 0, "medium": 1, "low": 2}
_WHY = {"STATUS": "a page the sitemap offers to search engines does not load", "NOINDEX": "a page is offered and withheld at the same time",
        "TITLE": "the title is what a search result shows first", "DESCRIPTION": "the description is the rest of the search result",
        "CANONICAL": "without a self-canonical, duplicates split what the page earns", "H1": "one first-level heading tells a reader and a crawler what the page is",
        "CTA_EVENT": "a button with no event is a step of the funnel that cannot be measured", "ORPHAN": "a page nothing links to is found only through the sitemap",
        "SITE": "the site-level files decide whether anything else is read",
        "LINK_FORM": "an internal link in another form than the listed one costs a redirect on every click and every crawl"}


def plan_digest(items: List[dict]) -> str:
    return hashlib.sha256(json.dumps([{"id": i["id"], "checks": i["checks"]} for i in items], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def recommend(inp: dict) -> dict:
    audit = _fact(inp, "site_audit")
    if not isinstance(audit, dict) or "findings" not in audit:
        return {"status": "error", "action": "recommend", "error": "a site_audit is required"}
    limit = max(1, min(int(inp.get("max_items") or 12), 30))
    groups: Dict[str, List[dict]] = {}
    for f in audit["findings"]:
        groups.setdefault(f["code"], []).append(f)
    items = []
    for code, fs in sorted(groups.items(), key=lambda kv: (min(_RANK.get(f["severity"], 3) for f in kv[1]), -len(kv[1]), kv[0])):
        pages = sorted({f["page"] for f in fs})
        items.append({"id": "REC-" + code, "title": f"{code.replace('_', ' ').title()}: {len(fs)} page(s)", "why": _WHY.get(code, ""),
                      "severity": min((f["severity"] for f in fs), key=lambda s: _RANK.get(s, 3)), "pages": pages[:20],
                      "evidence_refs": [f["id"] for f in fs][:40], "checks": [f["check"] for f in fs][:40]})
    brief = _fact(inp, "market_brief")
    changed = int(brief.get("changed") or 0) if isinstance(brief, dict) else 0
    context = {"watched_pages_changed": changed if isinstance(brief, dict) else None,
               "analytics_read": isinstance(inp.get("growth_attribution"), dict) or bool(inp.get("growth_attribution"))}
    if changed:
        items.insert(0, {"id": "REC-WATCH", "title": f"{changed} watched page(s) changed: re-read every claim that quotes them", "severity": "high",
                         "why": "a statement about another product is only as current as the page it quotes", "pages": [], "evidence_refs": ["market_brief"], "checks": []})
    items = items[:limit]
    # `plan_digest` at the top level is what the cockpit shows next to the approval and sends back with it (mission/api.py, _approval_binding).
    return {"status": "done", "action": "recommend", "plan_digest": plan_digest(items),
            "recommendations": {"site": audit.get("site"), "items": items, "digest": plan_digest(items), "context": context},
            "summary": f"{len(items)} recommendation(s) for {audit.get('site')}; digest {plan_digest(items)[:12]}"}


def approve_plan(inp: dict) -> dict:
    recs = _fact(inp, "recommendations")
    usable = isinstance(recs, dict) and isinstance(recs.get("items"), list)            # an EMPTY list is a valid finding: nothing to change
    approval = inp.get("_approval")
    if not approval:                                                                   # without a genuine approval nothing is decided, whatever was sent
        return pending_approval("radar.approve_plan", change_plan_approved=False, plan_digest=recs["digest"] if usable else "",
                                summary=(f"{len(recs['items'])} recommendation(s) for {recs.get('site')} wait for a decision" if usable
                                         else "no recommendations were given; nothing is approved"))
    if not usable:
        return {"status": "error", "action": "approve_plan", "error": "recommendations are required"}
    if approval.get("plan_digest") != plan_digest(recs["items"]):
        return {"status": "error", "action": "approve_plan", "error": "plan_digest_mismatch" if approval.get("plan_digest") else "plan_digest_required",
                "summary": "the approval does not carry the digest of this list of recommendations"}
    return {"status": "done", "action": "approve_plan", "change_plan_approved": {"site": recs.get("site"), "items": recs["items"], "digest": recs["digest"],
                                                                                 "approved_by": approval.get("by", "")},
            "summary": f"{len(recs['items'])} recommendation(s) approved for implementation"}


def verify_change(inp: dict, *, fetch: Fetch = _fetch) -> dict:
    plan = _fact(inp, "change_plan_approved")
    if not isinstance(plan, dict) or not plan.get("site"):
        return {"status": "error", "action": "verify_change", "error": "an approved plan is required"}
    site, passed, failed = plan["site"], [], []
    for item in plan.get("items", []):
        results = [(c, *run_check(site, c, fetch)) for c in item.get("checks", [])]
        bad = [{"check": c, "observed": obs} for c, ok, obs in results if not ok]
        (failed if bad else passed).append({"id": item["id"], "checks": len(results), "not_done": bad[:10]} if bad else {"id": item["id"], "checks": len(results)})
    out = {"site": site, "passed": passed, "failed": failed}
    if failed:
        return {"status": "error", "action": "verify_change", "error": "not_implemented", "verification": out,
                "summary": f"{len(failed)} of {len(passed) + len(failed)} approved item(s) are not live yet: " + ", ".join(f["id"] for f in failed)}
    return {"status": "done", "action": "verify_change", "change_verified": out, "summary": f"all {len(passed)} approved item(s) are live on {site}"}


def reassess(inp: dict, *, fetch: Fetch = _fetch) -> dict:
    before = _fact(inp, "site_audit")
    if not isinstance(before, dict) or not before.get("site"):
        return {"status": "error", "action": "reassess", "error": "the first site_audit is required"}
    again = site_audit({"site": before["site"]}, fetch=fetch)
    if again.get("status") != "done":
        return {"status": "error", "action": "reassess", "error": again.get("error", "the second audit failed")}
    old, new = {f["id"] for f in before["findings"]}, {f["id"] for f in again["site_audit"]["findings"]}
    out = {"site": before["site"], "resolved": sorted(old - new), "remaining": sorted(old & new), "new": sorted(new - old),
           "findings_before": len(old), "findings_after": len(new), "analytics_before": bool(inp.get("growth_attribution")),
           "analytics_after": bool(inp.get("growth_remeasured")), "next_round_input": again["site_audit"]["by_code"]}
    return {"status": "done", "action": "reassess", "reassessment": out,
            "summary": f"{len(out['resolved'])} resolved, {len(out['remaining'])} remaining, {len(out['new'])} new on {before['site']}"}


__all__ = ["site_audit", "recommend", "approve_plan", "verify_change", "reassess", "run_check", "plan_digest"]
