"""Communications as evidence: what was said, by whom, when, where it can be read again, and who it is about.

Chatwoot conversations (customer messages, company replies, private notes) and Twenty notes are read with the strict readers (a failed or cut read is a recorded gap, never
an empty answer) and normalised into :class:`Communication`. Nothing here decides what a message *means*: it keeps the words, the author's role, the time and the source,
resolves the customer by an EXPLICIT identifier (contact e-mail or website domain, held by both sides), and says so when it could not.

``EvidenceRef`` is built on :class:`agentic_os.integrations.business.contracts.Provenance` (provider, the provider's own object id, when it was observed, when it was true) and adds
what a decision needs to cite: the tenant, the object's version, the author and role, an excerpt and a link, and the access scope that governed the read. Tenant and source scope are
applied BEFORE anything is retrieved, ranked or cached: ``collect`` is given the scope and reads only what it names.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from agentic_os.integrations.business.contracts import Provenance

from .contracts import Extraction

ROLE_CUSTOMER, ROLE_COMPANY, ROLE_INTERNAL, ROLE_CRM_NOTE, ROLE_SYSTEM = "customer", "company", "internal_note", "crm_note", "system"
INTERNAL_SUFFIX = "/internal"      # a private note carries its source's scope plus this: granting the source does not grant its private notes


def _ms(iso: str) -> int:
    try:
        return int(_dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return 0


def _iso(epoch: Any) -> str:
    try:
        return _dt.datetime.fromtimestamp(int(epoch), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # noqa: BLE001
        return ""


@dataclass(frozen=True)
class EvidenceRef:
    tenant: str
    provenance: Provenance                 # provider, provider_ref (the source's own id), observed_at (read time, ms), known_at (when it was said, ms)
    kind: str                              # "message" | "note"
    thread: str                            # conversation id, or the record a note is attached to
    version: str                           # a revision marker when the source has one ("" = none known)
    author: str
    role: str
    at: str                                # ISO UTC time the source gives
    uri: str
    access_scope: str                      # what the read was permitted by, e.g. "tenant:acme/source:chatwoot"

    @property
    def id(self) -> str:
        return f"{self.provenance.provider}:{self.provenance.provider_ref}"

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "tenant": self.tenant, "provider": self.provenance.provider, "provider_ref": self.provenance.provider_ref, "kind": self.kind,
                "thread": self.thread, "version": self.version, "author": self.author, "role": self.role, "at": self.at, "uri": self.uri,
                "observed_at": self.provenance.observed_at, "known_at": self.provenance.known_at, "access_scope": self.access_scope}


@dataclass(frozen=True)
class Communication:
    ref: EvidenceRef
    text: str
    customer: Optional[str] = None         # the resolved ERPNext customer, or None
    identity_method: str = ""              # "email" | "domain" | "name_only" | "" (none)
    identity_note: str = ""
    extras: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IdentityMap:
    """ERPNext customers by the identifiers both sides hold. A name alone is never enough: ``by_name`` answers only to say "ambiguous"."""
    by_email: Mapping[str, str]
    by_domain: Mapping[str, str]
    names: Sequence[str] = ()

    @classmethod
    def from_customers(cls, rows: Iterable[Mapping[str, Any]]) -> "IdentityMap":
        e, d, n = {}, {}, []
        for r in rows:
            name = str(r.get("name") or "")
            n.append(name)
            if r.get("email_id"):
                e[str(r["email_id"]).strip().lower()] = name
            if r.get("website"):
                d[_host(str(r["website"]))] = name
        return cls(e, d, tuple(n))

    def resolve(self, *, email: str = "", domain: str = "", name: str = "") -> Tuple[Optional[str], str, str]:
        em, dm = email.strip().lower(), _host(domain)
        if em and em in self.by_email:
            return self.by_email[em], "email", ""
        if dm and dm in self.by_domain:
            return self.by_domain[dm], "domain", ""
        if name and name in self.names:
            return None, "name_only", f"a name ({name!r}) matches a customer but no identifier does: not resolved"
        return None, "", ""


def _host(v: str) -> str:
    v = v.strip().lower()
    v = re.sub(r"^[a-z]+://", "", v)
    return v.split("/")[0].removeprefix("www.")


def collect_chatwoot(client: Any, ex: Extraction, *, tenant: str, identity: IdentityMap, base_uri: str, scope: str, now_ms: int = 0) -> List[Communication]:
    """Every customer message, company reply and private note of every conversation the scope names. Activity lines (reopened, assigned) are counted, not kept as speech."""
    out: List[Communication] = []
    try:
        convs = client.read_conversations(status="all")
    except Exception as e:  # noqa: BLE001
        ex.gap("chatwoot:conversations", f"could not be read ({type(e).__name__}: {str(e)[:100]})", "no conversation is available as evidence")
        return out
    ex.counts["Chatwoot conversation"] = len(convs)
    activity = 0
    for cv in convs:
        sender = ((cv.get("meta") or {}).get("sender")) or {}
        cust, method, note = identity.resolve(email=str(sender.get("email") or ""), name=str(sender.get("name") or ""))
        try:
            msgs = client.read_messages(int(cv["id"]))
        except Exception as e:  # noqa: BLE001
            ex.gap(f"chatwoot:conversation/{cv.get('id')}", f"messages could not be read ({type(e).__name__}: {str(e)[:100]})", "this conversation is missing as evidence; its absence is not evidence of silence")
            continue
        for m in msgs:
            mt = int(m.get("message_type") if m.get("message_type") is not None else -1)
            if mt == 2:
                activity += 1
                continue
            role = ROLE_INTERNAL if m.get("private") else (ROLE_CUSTOMER if mt == 0 else ROLE_COMPANY)
            who = m.get("sender") or {}
            ref = EvidenceRef(tenant, Provenance("chatwoot", f"{cv['id']}/{m['id']}", observed_at=now_ms or 0, known_at=int(m.get("created_at") or 0) * 1000), "message",
                              str(cv["id"]), str(m.get("updated_at") or ""), str(who.get("name") or who.get("email") or role), role, _iso(m.get("created_at")),
                              f"{base_uri}/app/accounts/{getattr(client, 'account_id', '1')}/conversations/{cv['id']}", scope + INTERNAL_SUFFIX if role == ROLE_INTERNAL else scope)
            out.append(Communication(ref, str(m.get("content") or ""), cust, method, note, {"contact_email": sender.get("email")}))
    ex.counts["Chatwoot activity lines (not speech)"] = activity
    return out


def collect_twenty(client: Any, ex: Extraction, *, tenant: str, identity: IdentityMap, base_uri: str, scope: str, now_ms: int = 0) -> List[Communication]:
    """Twenty notes with the company each is attached to. A note attached to no company has no customer."""
    out: List[Communication] = []
    try:
        notes = client.read_all("notes")
        targets = client.read_all("noteTargets")
        companies = {c["id"]: c for c in client.read_all("companies")}
    except Exception as e:  # noqa: BLE001
        ex.gap("twenty:notes", f"could not be read ({type(e).__name__}: {str(e)[:100]})", "no CRM note is available as evidence")
        return out
    ex.counts["Twenty note"] = len(notes)
    by_note: Dict[str, List[str]] = {}
    for t in targets:
        if t.get("deletedAt"):
            continue
        if t.get("targetCompanyId") or t.get("companyId"):
            by_note.setdefault(str(t["noteId"]), []).append(str(t.get("targetCompanyId") or t.get("companyId")))
    for n in notes:
        if n.get("deletedAt"):
            continue
        cids = by_note.get(str(n["id"]), [])
        comp = companies.get(cids[0]) if len(cids) == 1 else None
        dn = (comp or {}).get("domainName") or {}
        cust, method, note = identity.resolve(domain=str(dn.get("primaryLinkUrl") or ""), name=str((comp or {}).get("name") or ""))
        if len(cids) > 1:
            cust, method, note = None, "", "the note is attached to more than one company"
        text = str(((n.get("bodyV2") or {}).get("markdown")) or "")
        ref = EvidenceRef(tenant, Provenance("twenty", str(n["id"]), observed_at=now_ms or 0, known_at=_ms(str(n.get("createdAt") or ""))), "note",
                          cids[0] if cids else "", str(n.get("updatedAt") or ""), str(n.get("createdBy", {}).get("name") if isinstance(n.get("createdBy"), dict) else "") or "crm",
                          ROLE_CRM_NOTE, str(n.get("createdAt") or "")[:19] + "Z", f"{base_uri}/object/note/{n['id']}", scope)
        out.append(Communication(ref, (str(n.get("title") or "") + ". " + text).strip(), cust, method, note))
    return out


def visible_to(comms: Iterable[Communication], granted: Iterable[str]) -> Tuple[List[Communication], int]:
    """What a caller may read: a communication is visible only when its own ``access_scope`` is EXACTLY one the caller holds. A scope never covers a longer one (the source does not
    include its private notes, a mailbox does not include another mailbox, a channel does not include another channel). Returns the visible ones and how many were withheld; the count
    is the whole of what is said about the rest. Apply this BEFORE correlating, explaining or caching, so withheld text is never ranked, shown to a model or cached for someone else."""
    held = set(granted)
    comms = list(comms)
    ok = [c for c in comms if c.ref.access_scope in held]
    return ok, len(comms) - len(ok)
