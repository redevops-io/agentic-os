#!/usr/bin/env python3
"""Optional, NOT part of the test suite: does the query-to-page relevance hook separate related from unrelated
pairs with a real embedder?

Illustrative/synthetic: six invented pages and twelve invented queries written for this script (no real site, no
Search Console data). n is tiny; this checks plumbing and the cosine scale, nothing else. It is not validation
against rankings, CTR or conversion.

Run (needs the optional sentence-transformers extra and a locally cached BAAI/bge-small-en-v1.5; no network is
used by the hook itself):  HF_HUB_OFFLINE=1 python scripts/search_relevance_sanity.py [--json]
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentic_os.content.relevance import PageText, make_relevance_fn  # noqa: E402
from agentic_os.content.search_signals import SearchObservation  # noqa: E402

PAGES = {
    "invoicing": PageText("Invoicing software for freelancers", "Send invoices and get paid faster",
                          "Create invoices, track payments and send reminders for freelancers and small studios.",
                          "Create professional invoices in minutes. Track which invoices are paid, overdue or draft. "
                          "Automatic payment reminders and recurring invoices for retainer clients."),
    "memory": PageText("Long-term memory for AI agents", "Give your agent persistent memory",
                       "How agent memory stores facts across sessions so assistants stay consistent.",
                       "Agents forget everything between sessions. A memory layer stores facts and summaries and "
                       "retrieves the relevant ones when the agent starts a new conversation."),
    "backup": PageText("Kubernetes backup and restore", "Back up your cluster state",
                       "Scheduled snapshots of cluster resources and volumes with one-command restore.",
                       "Protect namespaces, persistent volumes and manifests. Schedule snapshots, replicate them to "
                       "object storage and restore a whole cluster or a single namespace."),
    "email": PageText("Email deliverability guide", "Stop landing in spam",
                      "SPF, DKIM and DMARC explained, with a checklist to improve inbox placement.",
                      "Authenticate your sending domain with SPF, DKIM and DMARC, warm up new addresses, keep "
                      "bounce rates low and monitor complaint rates to protect your sender reputation."),
    "a11y": PageText("Accessible web design checklist", "Design for everyone",
                     "Colour contrast, keyboard navigation and screen reader support for accessible websites.",
                     "Check colour contrast ratios, make every control reachable by keyboard, label form fields "
                     "and provide text alternatives so people using screen readers can use your site."),
    "pricing": PageText("Pricing plans", "Simple, transparent pricing",
                        "Compare the monthly and annual plans and what each one includes.",
                        "Starter, Team and Business plans. Pay monthly or save with annual billing. All plans "
                        "include support; invoices are emailed to the billing contact."),
}
QUERIES = {                       # query -> the page it is genuinely about
    "invoice software for freelancers": "invoicing", "send payment reminders to clients": "invoicing",
    "ai agent long term memory": "memory", "how do agents remember past conversations": "memory",
    "kubernetes cluster backup": "backup", "restore a single namespace snapshot": "backup",
    "spf dkim dmarc setup": "email", "why do my emails go to spam": "email",
    "website colour contrast checklist": "a11y", "screen reader friendly forms": "a11y",
    "annual plan discount": "pricing", "monthly subscription cost comparison": "pricing",
}


class _Provider:
    def page_text(self, page_url):
        return PAGES.get(page_url)


def stuffing(emb) -> dict:
    """Keyword stuffing residual (illustrative): one honest page (the 'backup' page) against all 12 queries, and the
    same page whose body is stuffed with every query (twice, then once), with the repeat-collapse mitigation on
    and off. A page that literally contains each query once is 'relevant' to it by this measure; the mitigation
    only removes repetition."""
    import agentic_os.content.relevance as rel
    qs = list(QUERIES)
    honest = PAGES["backup"]
    base = ("Kubernetes backup and restore", "Back up your cluster state", "Scheduled snapshots")
    variants = {"honest_backup_page": honest,
                "stuffed_body_twice": PageText(*base, " ".join(qs) + " " + " ".join(qs)),
                "stuffed_body_once": PageText(*base, " ".join(qs))}
    out = {}
    original = rel.collapse_repeats
    for label, fn in (("mitigation_on", original), ("mitigation_off", lambda t: t)):
        rel.collapse_repeats = fn
        out[label] = {}
        for name, page in variants.items():
            f = make_relevance_fn(emb, type("P", (), {"page_text": lambda self, u, pg=page: pg})())
            vals = [f(SearchObservation(query=q, page_url="u")).value for q in qs]
            out[label][name] = {"mean": round(statistics.mean(vals), 3), "queries_at_or_above_0.6":
                                f"{sum(v >= 0.6 for v in vals)}/{len(vals)}"}
    rel.collapse_repeats = original
    return out


def main(as_json: bool = False) -> int:
    from agentic_os.market.synthetic_research.embedder import SentenceTransformerEmbedder
    emb = SentenceTransformerEmbedder("BAAI/bge-small-en-v1.5")
    out = {"embedder": emb.name, "n_queries": len(QUERIES), "n_pages": len(PAGES), "maps": {}}
    for mapname in ("clamp01", "halfcosine"):
        fn = make_relevance_fn(emb, _Provider(), similarity_map=mapname)
        rel, unrel = [], []
        for q, topic in QUERIES.items():
            for page in PAGES:
                r = fn(SearchObservation(query=q, page_url=page))
                (rel if page == topic else unrel).append(r.value)
        wins = sum(1 for a in rel for b in unrel if a > b) + 0.5 * sum(1 for a in rel for b in unrel if a == b)
        per_q = sum(1 for q, topic in QUERIES.items()
                    if max(PAGES, key=lambda p: fn(SearchObservation(query=q, page_url=p)).value) == topic)
        out["maps"][mapname] = {
            "related": {"n": len(rel), "mean": round(statistics.mean(rel), 3), "min": round(min(rel), 3),
                        "max": round(max(rel), 3)},
            "unrelated": {"n": len(unrel), "mean": round(statistics.mean(unrel), 3), "min": round(min(unrel), 3),
                          "max": round(max(unrel), 3)},
            "pairwise_auc_related_over_unrelated": round(wins / (len(rel) * len(unrel)), 3),
            "queries_whose_top_page_is_the_related_one": f"{per_q}/{len(QUERIES)}",
            "unrelated_pairs_at_or_above_0.6": sum(1 for v in unrel if v >= 0.6),
            "related_pairs_at_or_above_0.6": sum(1 for v in rel if v >= 0.6),
        }
        out["embedder_revision_recorded"] = fn(SearchObservation(query="invoice software", page_url="invoicing")
                                               ).embedder_revision
    out["keyword_stuffing"] = stuffing(emb)
    print(json.dumps(out, indent=2) if as_json else json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--json" in sys.argv))
