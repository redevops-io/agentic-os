"""E-mail and internal-messenger messages as evidence: communications that never entered the CRM, the ERP or the support system.

Both adapters are READ-ONLY and produce the same :class:`~agentic_os.economics.evidence.Communication` the Chatwoot and Twenty collectors do, so correlation, reasoning and findings need
no change. They reuse what the platform already has: the IMAP read pattern of ``world.connectors.GmailIMAPConnector`` (an app password, ``readonly`` select, a quoting guard against
injection into a search term; extended here from "find replies" to "read bodies and headers") and ``collaboration.slack.SlackCollaborationProvider`` (extended with ``read_history``).

What the adapters will and will not say:

* The author's role comes from the addresses: a message from an own domain to an external one is ``company``; from an external address ``customer``; between own-domain addresses only
  ``internal_note``. A messenger channel is internal unless it is named as shared with customers.
* The customer is resolved only by an explicit identifier: the external participant's address or domain, else exactly one customer's address or domain written in the text. A name alone is
  never a resolution (a message that only says "Granite" has no customer here).
* Threads come from ``Message-ID``/``In-Reply-To``/``References`` (e-mail) and the thread timestamp (messenger). Subjects are not used to join threads.
* The same message in two mailboxes is one message (``Message-ID``). Quoted replies (``>`` lines and the "On ... wrote:" tail) are cut so that a reply does not repeat the statement it quotes.
* Every read is strict: a failed login, search, fetch or page is a recorded gap, and a cut history is not shown as a complete one. The access scope of every message is its mailbox or its
  channel, and ``evidence.visible_to`` applies it before anything is retrieved or ranked.
"""
from __future__ import annotations

import datetime as _dt
import email
import imaplib
import re
from email import policy
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from agentic_os.integrations.business.contracts import Provenance

from .contracts import Extraction
from .evidence import ROLE_COMPANY, ROLE_CUSTOMER, ROLE_INTERNAL, Communication, EvidenceRef, IdentityMap, _host

ADDR = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
WROTE = re.compile(r"^\s*(?:On .{5,120}wrote:|-{2,}\s*Original Message\s*-{2,}|From:\s.+)\s*$", re.I)


def strip_quoted(text: str) -> str:
    """The new text of a reply: drops ``>`` lines and everything after an "On ... wrote:" or "Original Message" marker."""
    keep: List[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if WROTE.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        keep.append(line)
    return "\n".join(keep).strip()


def resolve_customer(identity: IdentityMap, participants: Sequence[str], text: str, own_domains: Set[str]) -> Tuple[Optional[str], str, str]:
    """(customer, method, note). External participants first; else exactly one customer's address or domain written in the text. Never by a name."""
    ext = [a for a in participants if _host(a.split("@")[-1]) not in own_domains]
    found = {identity.resolve(email=a)[0] or identity.resolve(domain=a.split("@")[-1])[0] for a in ext} - {None}
    if len(found) == 1:
        c = next(iter(found))
        return c, "email" if identity.resolve(email=ext[0])[0] else "domain", ""
    if len(found) > 1:
        return None, "", "the external participants resolve to more than one customer"
    mentioned = {identity.resolve(email=a)[0] or identity.resolve(domain=a.split("@")[-1])[0] for a in ADDR.findall(text)} - {None}
    if len(mentioned) == 1:
        return next(iter(mentioned)), "mentioned_identifier", "the customer is named by an address in the text, not by a participant"
    if len(mentioned) > 1:
        return None, "", "the text names more than one customer's address"
    return None, "", ""


def _iso(msg) -> str:
    try:
        return parsedate_to_datetime(str(msg.get("Date"))).astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # noqa: BLE001
        return ""


def _ms(iso: str) -> int:
    try:
        return int(_dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return 0


def _body(msg) -> str:
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        return part.get_content()
    html = msg.get_body(preferencelist=("html",))
    return re.sub(r"<[^>]+>", " ", html.get_content()) if html is not None else ""


def parse_message(raw: bytes, *, mailbox: str, uid: str, tenant: str, identity: IdentityMap, own_domains: Set[str], scope: str, now_ms: int = 0) -> Optional[Communication]:
    msg = email.message_from_bytes(raw, policy=policy.default)
    addrs = lambda h: [a.lower() for _, a in getaddresses([str(msg.get(h) or "")]) if a]  # noqa: E731
    frm, to, cc = addrs("From"), addrs("To"), addrs("Cc")
    everyone = frm + to + cc
    if not frm:
        return None
    sender_own = _host(frm[0].split("@")[-1]) in own_domains
    all_own = all(_host(a.split("@")[-1]) in own_domains for a in everyone)
    role = ROLE_INTERNAL if all_own else (ROLE_COMPANY if sender_own else ROLE_CUSTOMER)
    text = strip_quoted(_body(msg))
    mid = str(msg.get("Message-ID") or "").strip() or f"{mailbox}/{uid}"
    refs = re.findall(r"<[^>]+>", str(msg.get("References") or "") + " " + str(msg.get("In-Reply-To") or ""))
    thread = refs[0] if refs else mid
    cust, method, note = resolve_customer(identity, everyone, text + " " + str(msg.get("Subject") or ""), own_domains)
    at = _iso(msg)
    ref = EvidenceRef(tenant, Provenance("email", mid, observed_at=now_ms or 0, known_at=_ms(at)), "message", thread, "", frm[0], role, at, f"mailto-thread:{thread}", scope)
    subj = str(msg.get("Subject") or "").strip()
    return Communication(ref, (subj + ". " + text).strip() if subj else text, cust, method, note, {"mailbox": mailbox, "to": to, "cc": cc})


def collect_imap(host: str, user: str, password: str, ex: Extraction, *, tenant: str, identity: IdentityMap, own_domains: Iterable[str], folder: str = "INBOX", since: str = "",
                 port: int = 993, ssl: bool = True, now_ms: int = 0, imap_factory: Optional[Callable[..., Any]] = None) -> List[Communication]:
    """Every message of one mailbox folder (optionally ``since`` an ISO date), read-only. The scope is ``mailbox:<user>/<folder>``."""
    scope = f"mailbox:{user}/{folder}"
    own = {_host(d) for d in own_domains}
    if '"' in folder or "\\" in folder or '"' in since:
        ex.gap(scope, "invalid folder or date for an IMAP search term", "the mailbox was not read")
        return []
    make = imap_factory or ((lambda: imaplib.IMAP4_SSL(host, port)) if ssl else (lambda: imaplib.IMAP4(host, port)))
    m = None
    out: List[Communication] = []
    try:
        m = make()
        m.login(user, password)
        typ, _ = m.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            ex.gap(scope, f"folder could not be opened (IMAP {typ})", "this mailbox is not available as evidence")
            return []
        crit = "ALL" if not since else f'SINCE "{_dt.date.fromisoformat(since).strftime("%d-%b-%Y")}"'
        typ, data = m.uid("SEARCH", None, crit)
        if typ != "OK":
            ex.gap(scope, f"search failed (IMAP {typ})", "this mailbox is not available as evidence")
            return []
        uids = (data[0].split() if data and data[0] else [])
        ex.counts[f"E-mail message ({user})"] = len(uids)
        for u in uids:
            uid = u.decode() if isinstance(u, bytes) else str(u)
            typ, md = m.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not md or not isinstance(md[0], tuple):
                ex.gap(f"{scope}/uid {uid}", f"message could not be fetched (IMAP {typ})", "this message is missing as evidence; its absence is not evidence of silence")
                continue
            try:
                c = parse_message(md[0][1], mailbox=user, uid=uid, tenant=tenant, identity=identity, own_domains=own, scope=scope, now_ms=now_ms)
            except Exception as e:  # noqa: BLE001
                ex.gap(f"{scope}/uid {uid}", f"message could not be parsed ({type(e).__name__})", "this message is missing as evidence")
                continue
            if c:
                out.append(c)
    except Exception as e:  # noqa: BLE001
        ex.gap(scope, f"could not be read ({type(e).__name__}: {str(e)[:100]})", "this mailbox is not available as evidence")
    finally:
        if m is not None:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass
    return out


def dedupe(comms: Iterable[Communication]) -> Tuple[List[Communication], Dict[str, List[str]]]:
    """One message per Message-ID. The same e-mail read from two mailboxes keeps the first and names the other copies (and their scopes), so neither mailbox's reference is lost."""
    seen: Dict[str, Communication] = {}
    out: List[Communication] = []
    dup: Dict[str, List[str]] = {}
    for c in comms:
        if c.ref.provenance.provider != "email":
            out.append(c)
        elif c.ref.id in seen:
            dup.setdefault(c.ref.id, []).append(c.ref.access_scope)
        else:
            seen[c.ref.id] = c
            out.append(c)
    return out, dup


def collect_messenger(provider: Any, channels: Sequence[str], ex: Extraction, *, tenant: str, identity: IdentityMap, own_domains: Iterable[str] = (), shared_channels: Iterable[str] = (),
                      users: Optional[Dict[str, str]] = None, workspace: str = "", now_ms: int = 0) -> List[Communication]:
    """Messages of the named channels through a ``CollaborationProvider`` that has ``read_history`` (Slack does). Internal unless the channel is in ``shared_channels``.
    ``users`` maps a provider user id to a display name or e-mail; ids are never guessed."""
    out: List[Communication] = []
    own = {_host(d) for d in own_domains}
    shared = set(shared_channels)
    for ch in channels:
        scope = f"messenger:{provider.provider}/{workspace or 'workspace'}/channel:{ch}"
        try:
            events = provider.read_history(ch)
        except Exception as e:  # noqa: BLE001
            ex.gap(scope, f"history could not be read ({type(e).__name__}: {str(e)[:100]})", "this channel is not available as evidence; its absence is not evidence of silence")
            continue
        ex.counts[f"Messenger message ({ch})"] = len(events)
        for ev in events:
            who = (users or {}).get(ev.actor, ev.actor)
            cust, method, note = resolve_customer(identity, [who] if "@" in who else [], ev.text, own)
            role = ROLE_CUSTOMER if ch in shared and "@" in who and _host(who.split("@")[-1]) not in own else (ROLE_COMPANY if ch in shared else ROLE_INTERNAL)
            try:
                known = int(float(ev.ts) * 1000)
                at = _dt.datetime.fromtimestamp(float(ev.ts), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                known, at = 0, ""
            ref = EvidenceRef(tenant, Provenance(provider.provider, f"{ch}/{ev.message_id or ev.ts}", observed_at=now_ms or 0, known_at=known), "message", f"{ch}/{ev.thread or ev.message_id or ev.ts}", "",
                              who, role, at, f"{provider.provider}://{workspace}/{ch}/{ev.message_id or ev.ts}", scope)
            out.append(Communication(ref, ev.text, cust, method, note, {"channel": ch}))
    return out
