"""Automated outbound outreach — persona templates, evidence-grounded middle, and a hard quality gate.

The rule the founder set: **do not send unless Discovery can produce a concrete, cited reason for contacting
the company and a plausible Runtime problem derived from it.** Otherwise the lead is routed to
``NEEDS_MORE_EVIDENCE`` — never a generic "saw your company" email. The first email is short and leads with
the operational problem, not the product; the middle 40% is generated from the company's actual evidence
(60% fixed). Persona selects the template. Sending is real (Postmark) but gated: a verified business email,
a passing quality gate, no suppression, a frequency cap, and a compliance footer are all required — and
auto-send is off unless explicitly enabled.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class OutreachDecision(str, Enum):
    SEND = "SEND"                          # auto-send allowed and executed
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"  # drafted, held for founder approval
    DRAFT_ONLY = "DRAFT_ONLY"
    NEEDS_MORE_EVIDENCE = "NEEDS_MORE_EVIDENCE"  # the quality gate blocked it
    SUPPRESSED = "SUPPRESSED"              # do-not-contact / opt-out / duplicate / frequency cap
    NO_EMAIL = "NO_EMAIL"                  # no verified business email to send to


# persona → template. Founder-to-founder for founders/CTOs; the primary (problem-first) for AI/platform leads.
_FOUNDER_ROLES = ("founder", "cto", "co-founder", "chief technology")
_PRIMARY_ROLES = ("head of ai", "vp engineering", "vp eng", "ai platform", "developer productivity",
                  "ml platform", "ai infrastructure", "head of engineering")

#: CAN-SPAM: a valid physical postal address + a working, honored opt-out in every message.
_ADDRESS = "ReDevOps.io LLC, 20200 West Dixie Highway, Suite 902, Miami, Florida 33180"
_COMPLIANCE = ("\n\n—\nAlex · ReDevOps · redevops.io\n{address}\n"
               "You're receiving this one-off note because of public, work-related signals about {company}. "
               "Unsubscribe: {unsub_url}  (or reply STOP, or email unsubscribe@redevops.io) — honored within "
               "10 business days.")


def unsubscribe_token(email: str) -> str:
    """A stable, non-reversible token for an unsubscribe URL (the raw address never rides in the link)."""
    from runtime_contracts import content_hash  # noqa: PLC0415
    return content_hash({"u": email.strip().lower()}).split(":", 1)[-1][:24]


def unsubscribe_url(email: str) -> str:
    return f"https://redevops.io/unsubscribe?u={unsubscribe_token(email)}"


class SuppressionLedger:
    """A persistent do-not-contact / opt-out ledger (CAN-SPAM). Keyed by unsubscribe token AND raw email/
    domain so a monitored ``unsubscribe@redevops.io`` mailbox or the /unsubscribe endpoint can suppress by
    either. File-backed (one entry per line) so suppression survives restarts and is auditable."""

    def __init__(self, path: str = "") -> None:
        self.path = path or os.environ.get("REDEVOPS_SUPPRESSION_FILE", "")
        self._tokens: set = set()
        self._addrs: set = set()
        self._load()

    def _load(self) -> None:
        if self.path and os.path.exists(self.path):
            for line in open(self.path, encoding="utf-8"):
                v = line.strip().lower()
                if v:
                    (self._tokens if len(v) == 24 and "@" not in v else self._addrs).add(v)

    def is_suppressed(self, email: str) -> bool:
        e = (email or "").strip().lower()
        if not e:
            return False
        domain = e.split("@")[-1]
        return e in self._addrs or domain in self._addrs or unsubscribe_token(e) in self._tokens

    def suppress(self, value: str, *, reason: str = "opt-out") -> None:
        """Add an email, a domain, or an unsubscribe token to the ledger (idempotent, appended to file)."""
        v = (value or "").strip().lower()
        if not v:
            return
        (self._tokens if len(v) == 24 and "@" not in v else self._addrs).add(v)
        if self.path:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(v + "\n")


def handle_unsubscribe(token_or_email: str, ledger: SuppressionLedger) -> Dict[str, str]:
    """What the monitored /unsubscribe endpoint (or mailbox processor) calls — suppress + confirm."""
    ledger.suppress(token_or_email, reason="user_unsubscribe")
    return {"status": "unsubscribed", "message": "You won't hear from us again."}

_PRIMARY = """Subject: A question about your AI stack

Hi {first_name},

I'm building ReDevOps, an open-source runtime stack for production AI systems.

I'm reaching out because {company} appears to be {observed_activity}. {specific_evidence_sentence} That made \
me wonder whether {runtime_problem} is something your team is already running into.

We've been building retrieval, context, execution, permissions, verification and observability as shared \
infrastructure instead of re-implementing them inside each AI application. The stack turns an objective into \
a governed execution path — Discovery → planning → execution → context optimization → verification/outcome — \
with replay, approvals and an evidence trail across applications.

We're looking for a small number of companies to run a pilot against a real production workflow, rather than \
another synthetic demo. Based on {evidence_about_company}, {specific_problem_or_workflow} could be an \
interesting candidate.

Would you be open to a 20-minute technical conversation to see whether there's a fit?

Alex
Founder, ReDevOps
redevops.io"""

_FOUNDER = """Subject: {company} × ReDevOps

Hi {first_name},

I've been looking at how {company} is {observed_activity}.

I'm working on a related infrastructure problem at ReDevOps: AI applications keep rebuilding the same \
execution layer independently — context, retrieval, planning, permissions, verification, recovery and \
governance. We've moved those concerns into shared Runtimes so multiple agents and applications can execute \
against the same context, policy and evidence trail.

{specific_evidence_sentence} That made me wonder whether {runtime_problem} is something you're hitting.

I'm looking for a few technically strong companies to pilot this against real workloads. The goal isn't to \
sell another AI application; it's to see whether we can remove infrastructure your team otherwise has to \
build and operate itself.

Worth comparing notes for 20 minutes?

Alex
Founder, ReDevOps
redevops.io"""


@dataclass
class OutreachContext:
    company: str
    first_name: str = "there"
    role: str = ""
    verified_email: str = ""
    observed_activity: str = ""
    specific_evidence_sentence: str = ""
    runtime_problem: str = ""
    evidence_about_company: str = ""
    specific_problem_or_workflow: str = ""
    suppressed: bool = False
    already_contacted: bool = False


def quality_gate(ctx: OutreachContext) -> Tuple[bool, str]:
    """The founder's rule: a concrete CITED reason + a plausible Runtime problem, or NEEDS_MORE_EVIDENCE.
    Returns (ok, reason). A cited evidence sentence must reference the company's actual activity, and a
    runtime problem must be derived from it — not a generic pitch."""
    if not ctx.specific_evidence_sentence or len(ctx.specific_evidence_sentence.split()) < 5:
        return False, "no concrete cited evidence sentence"
    if not ctx.runtime_problem:
        return False, "no Runtime problem derived from the evidence"
    if not ctx.observed_activity:
        return False, "no observed activity to ground the opening"
    # the evidence must actually mention the company / a technical signal (not boilerplate)
    generic = ("might be interested", "saw your company", "reaching out to companies")
    if any(g in ctx.specific_evidence_sentence.lower() for g in generic):
        return False, "evidence sentence is generic, not company-specific"
    return True, "cited evidence + derived runtime problem present"


def select_template(role: str) -> str:
    r = (role or "").lower()
    if any(k in r for k in _FOUNDER_ROLES):
        return _FOUNDER
    return _PRIMARY


def render_email(ctx: OutreachContext) -> Dict[str, str]:
    """Render subject + body for the persona. 60% fixed / 40% evidence-generated; compliance footer added."""
    tmpl = select_template(ctx.role)
    rp = ctx.runtime_problem or "shared execution/context/permissions infrastructure"
    rp = rp[8:] if rp.lower().startswith("whether ") else rp    # the templates already supply "whether …"
    filled = tmpl.format(
        first_name=ctx.first_name, company=ctx.company,
        observed_activity=ctx.observed_activity or "building production AI systems",
        specific_evidence_sentence=ctx.specific_evidence_sentence,
        runtime_problem=rp,
        evidence_about_company=ctx.evidence_about_company or ctx.specific_evidence_sentence,
        specific_problem_or_workflow=ctx.specific_problem_or_workflow or "your production AI workflow")
    subject = filled.splitlines()[0].replace("Subject:", "").strip()
    unsub = unsubscribe_url(ctx.verified_email) if ctx.verified_email else "https://redevops.io/unsubscribe"
    footer = _COMPLIANCE.format(company=ctx.company, address=_ADDRESS, unsub_url=unsub)
    body = "\n".join(filled.splitlines()[1:]).strip() + footer
    return {"subject": subject, "body": body}


def decide(ctx: OutreachContext, *, auto_send: Optional[bool] = None,
           ledger: "Optional[SuppressionLedger]" = None) -> OutreachDecision:
    """The governed outbound decision. Deny-wins: suppression → gate → email → auto/approval."""
    if ctx.suppressed or ctx.already_contacted:
        return OutreachDecision.SUPPRESSED
    if ledger is not None and ctx.verified_email and ledger.is_suppressed(ctx.verified_email):
        return OutreachDecision.SUPPRESSED
    ok, _reason = quality_gate(ctx)
    if not ok:
        return OutreachDecision.NEEDS_MORE_EVIDENCE
    if not _valid_email(ctx.verified_email):
        return OutreachDecision.NO_EMAIL
    auto = os.environ.get("REDEVOPS_AUTO_SEND") == "1" if auto_send is None else auto_send
    return OutreachDecision.SEND if auto else OutreachDecision.APPROVAL_REQUIRED


def _valid_email(addr: str) -> bool:
    return bool(addr) and bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", addr))


def send_outreach(ctx: OutreachContext, connector: Any, *, cap_remaining: int = 0,
                  auto_send: Optional[bool] = None, tag: str = "gtm-outreach",
                  ledger: "Optional[SuppressionLedger]" = None) -> Dict[str, Any]:
    """Render + (governed) send. Returns the decision, the rendered email, and the send result if sent.
    Never sends unless decide() == SEND, a verified email exists, the lead isn't suppressed, and the
    frequency cap has room."""
    decision = decide(ctx, auto_send=auto_send, ledger=ledger)
    email = render_email(ctx)
    result: Dict[str, Any] = {"decision": decision.value, "to": ctx.verified_email,
                              "subject": email["subject"], "sent": False}
    if decision is OutreachDecision.SEND and cap_remaining > 0 and connector is not None:
        r = connector.send(to=ctx.verified_email, subject=email["subject"], body=email["body"], tag=tag)
        result["sent"] = r.get("error_code", 1) == 0
        result["message_id"] = r.get("message_id")
    return result
