"""Apollo reconciliation probe for `outreach.enroll`.

What Apollo documents (docs.apollo.io): `POST /emailer_campaigns/{sequence_id}/add_contact_ids` adds contacts to a sequence; a contact already in it is SKIPPED (reported in
`skipped_contact_ids` as `contacts_already_exists_in_current_campaign`), so enrollment is idempotent state, not a counted event. `GET /contacts/{contact_id}` costs no credits (600 calls/hour
in the documented 429 example) and returns the contact with `emailer_campaign_ids` and `contact_campaign_statuses` (`status`, `added_at`, `inactive_reason`; the view-a-contact page shows
`emailer_campaign_ids` only in its example, so its presence is not a documented guarantee).

The probe reads each contact and CONFIRMS only when the sequence is among its campaigns AND the membership started since the call (`added_at` >= dispatch minus skew):
  * the membership is keyed by (contact, sequence), so a membership that began inside the window is the effect of this request or of an identical enrollment, which Apollo makes the same state;
  * a membership that began BEFORE the call is INCONCLUSIVE: the contact was already there, so this attempt may have been skipped; whether that matters is a person's call;
  * when a contact has several statuses and none says which sequence it belongs to (the per-entry campaign id is not documented), the probe does not guess: INCONCLUSIVE;
  * with several contacts, every one must be confirmed; a partial result names the others.
Not covered: `contact.upsert` (the connector creates without `run_dedupe`, so a retry may create a duplicate contact: UNSUPPORTED), `outreach.sequence.configure` (adds a step on every call) and
`outreach.sequence.activate` (UI only). No Apollo probe can return an authoritative negative. Enrolling in an active sequence causes emails, which is why an unresolved enrollment escalates instead of being resent.
"""
from __future__ import annotations

from ..probes import ProbeContext, ProbeOutcome, ProbeResult, Qualification
from ._common import Http, ProbeBase, SKEW_S, epoch, httpx_get

QUALIFICATION = Qualification("apollo", eventually_consistent=True, visibility_window_s=None, authoritative_negative=False)
NO_LOOKUP = Qualification("apollo", supports_lookup=False)
READ_PATHS = ("/contacts",)
MAX_CONTACTS = 20


def default_http(api_key: str, base_url: str = "https://api.apollo.io/api/v1", timeout: float = 10.0) -> Http:
    """GET-only transport restricted to `/contacts/{id}` (the master key `add_contact_ids` needs is not needed to read a contact)."""
    return httpx_get(base_url, {"X-Api-Key": api_key, "Accept": "application/json", "Cache-Control": "no-cache"}, READ_PATHS, timeout, one_segment=True)


def derive_ref(inputs: dict) -> dict:
    ids = inputs.get("contact_ids")
    if not ids and inputs.get("contact_id"):
        ids = [inputs["contact_id"]]
    joined = ",".join(sorted(str(i) for i in ids)) if isinstance(ids, (list, tuple)) else ""
    out = {}
    if joined and len(joined) <= 200:
        out["contact_ids"] = joined                       # too many to fit: nothing is recorded and the probe reports UNSUPPORTED rather than check a truncated list
    if inputs.get("sequence_id"):
        out["sequence_id"] = str(inputs["sequence_id"])
    return out


class ApolloProbes(ProbeBase):
    def enroll(self, ctx: ProbeContext) -> ProbeResult:
        ref = ctx.effect_ref or {}
        ids, seq = ref.get("contact_ids"), ref.get("sequence_id")
        if not (isinstance(ids, str) and ids and isinstance(seq, str) and seq):
            return ProbeResult(ProbeOutcome.UNSUPPORTED, detail="the reservation does not say which contacts and which sequence: the enrollment cannot be looked up")
        contacts = ids.split(",")
        if len(contacts) > MAX_CONTACTS:
            return ProbeResult(ProbeOutcome.UNSUPPORTED, detail=f"{len(contacts)} contacts is more than one probe checks under the hourly limit")
        since = ctx.dispatched_at - SKEW_S
        confirmed: list = []
        pending: list = []
        for cid in contacts:
            got = self._get(f"/contacts/{cid}", {})
            if isinstance(got, ProbeResult):
                return got
            code, body, headers = got
            bad = self._bad_status(code, headers)
            if bad is not None:
                if bad.outcome is ProbeOutcome.INCONCLUSIVE:
                    pending.append(f"{cid} (contact not found)")
                    continue
                return bad
            c = (body or {}).get("contact") if isinstance(body, dict) else None
            verdict = self._membership(c, seq, since)
            (confirmed if verdict is True else pending).append(cid if verdict is True else f"{cid} ({verdict})")
        if pending:
            return ProbeResult(ProbeOutcome.INCONCLUSIVE, detail=f"not confirmed for {len(pending)} of {len(contacts)} contact(s): {'; '.join(pending)[:300]}. Not proof the enrollment did not happen.")
        return ProbeResult(ProbeOutcome.CONFIRMED_EXECUTED,
                           evidence=self._evidence(f"apollo contact(s) {ids} are in sequence {seq}, membership started since the call", ctx.now),
                           provider_ref=confirmed[0], observed_at=ctx.now,
                           result={"sequence_id": seq, "contact_ids": confirmed, "reconciled": True},
                           detail="every contact is in the sequence with a membership that began since the call")

    @staticmethod
    def _membership(contact, seq: str, since: float):
        """True, or why the membership was not confirmed."""
        if not isinstance(contact, dict):
            return "no contact in the response"
        in_ids = seq in [str(x) for x in (contact.get("emailer_campaign_ids") or [])]
        entries = [e for e in (contact.get("contact_campaign_statuses") or []) if isinstance(e, dict)]
        labelled = [e for e in entries if e.get("emailer_campaign_id")]
        mine = [e for e in labelled if str(e.get("emailer_campaign_id")) == seq] if labelled else (entries if (len(entries) == 1 and in_ids) else [])
        if not in_ids and not mine:
            return "not in the sequence yet"
        if not mine:
            return "in the sequence, but the status entry for it cannot be identified"
        added = epoch(mine[0].get("added_at"))
        if added is None:
            return "in the sequence, start time unknown"
        if added < since:
            return "already in the sequence before the call: this attempt may have been skipped"
        return True


def register_apollo(registry, http: Http, *, prefix: str = "") -> ApolloProbes:
    p = ApolloProbes(http)
    registry.register(f"{prefix}outreach.enroll", p.enroll, QUALIFICATION, ref_keys=(), ref_derive=derive_ref)
    for cap in ("contact.upsert", "outreach.sequence.configure", "outreach.sequence.activate"):
        registry.register(f"{prefix}{cap}", lambda c: ProbeResult(ProbeOutcome.UNSUPPORTED, detail="no read-only lookup identifies this effect at Apollo"), NO_LOOKUP)
    return p
