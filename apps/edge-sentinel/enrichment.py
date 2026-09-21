"""External enrichment + attack surface — provider-neutral CONTRACTS first (Phase 6).

The plan is explicit: build the enrichment *contracts* before any live provider, so OpenCTI / Shodan /
a DNS or RDAP service never become architectural dependencies. Edge Sentinel depends on these ABCs; a live
provider (or a fixture, in tests) implements them. Every external claim is an :class:`EnrichmentRecord`
that carries its **source and time**, and intel that goes **stale or revoked changes downstream status** —
both are Phase 6 acceptance criteria.

Enrichment extends the Phase 2 CTI graph with provenanced edges; it never mutates evidence, and every edge
records which provider produced it and when.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .evidence import Finding, FindingStatus, _now, sha256_hex
from .stix import Provenance, StixObject, normalize_ioc


@dataclass(frozen=True)
class EnrichmentRecord:
    """One external claim, with provenance (source + time) and a lifecycle. ``valid_until``/``revoked``
    make staleness explicit so a downstream finding can be re-evaluated when intel decays."""
    provider: str
    source: str
    subject: str                    # the IOC/asset this is about
    kind: str                       # dns | certificate | rdap | threat-intel | exposure
    data: dict
    retrieved_at: str
    valid_until: str = ""           # ISO time; "" = no stated expiry
    revoked: bool = False

    @property
    def record_id(self) -> str:
        return f"enr-{sha256_hex(self.provider + '|' + self.subject + '|' + self.kind)[:16]}"

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def is_stale(record: EnrichmentRecord, now: str) -> bool:
    """A record is stale if revoked, or past its stated validity. Deterministic string-time compare (ISO)."""
    if record.revoked:
        return True
    return bool(record.valid_until) and now > record.valid_until


# ──────────────────────────── provider contracts (ABCs) ────────────────────────────

class DnsProvider(ABC):
    @abstractmethod
    def resolve(self, domain: str) -> EnrichmentRecord: ...


class CertificateProvider(ABC):
    @abstractmethod
    def certificates(self, domain: str) -> EnrichmentRecord: ...


class RdapProvider(ABC):
    """Whois / RDAP registration."""
    @abstractmethod
    def registration(self, subject: str) -> EnrichmentRecord: ...


class ThreatIntelProvider(ABC):
    @abstractmethod
    def reputation(self, ioc: str) -> EnrichmentRecord: ...


class AssetExposureProvider(ABC):
    @abstractmethod
    def exposed_services(self, asset: str) -> EnrichmentRecord: ...


# ──────────────────────────── enrichment orchestration ────────────────────────────

def _prov(rec: EnrichmentRecord) -> Provenance:
    return Provenance(source=f"{rec.provider}:{rec.source}", method=f"enrichment:{rec.kind}",
                      created_at=rec.retrieved_at)


def enrich(graph, subject: str, *, now: str = "", dns: DnsProvider | None = None,
           cert: CertificateProvider | None = None, rdap: RdapProvider | None = None,
           ti: ThreatIntelProvider | None = None, exposure: AssetExposureProvider | None = None
           ) -> list[EnrichmentRecord]:
    """Run whatever providers are supplied over ``subject`` and add provenanced edges to the CTI graph.
    Skips stale/revoked records (they do not add live edges). Returns every record (including stale) so a
    caller can reconcile downstream status. Deterministic given deterministic providers."""
    now = now or _now()
    records: list[EnrichmentRecord] = []
    sco, indicator = normalize_ioc(subject)
    graph.add_object(sco); graph.add_object(indicator)

    def _add(rec: EnrichmentRecord):
        records.append(rec)
        if is_stale(rec, now):
            return                                      # stale/revoked intel adds no live edge
        p = _prov(rec)
        if rec.kind == "dns":
            for ip in rec.data.get("addresses", []):
                ip_sco, _ = normalize_ioc(ip)
                graph.add_object(ip_sco)
                graph.relate("resolves-to", indicator.id, ip_sco.id, p, confidence=70)
        elif rec.kind == "certificate":
            for san in rec.data.get("sans", []):
                san_sco, _ = normalize_ioc(san)
                graph.add_object(san_sco)
                graph.relate("shares-certificate", indicator.id, san_sco.id, p, confidence=60)
        elif rec.kind == "rdap":
            asn = rec.data.get("asn")
            if asn:
                asn_obj = StixObject(type="autonomous-system", key=str(asn),
                                     props={"number": asn, "name": rec.data.get("as_name", "")}, provenance=p)
                graph.add_object(asn_obj)
                graph.relate("belongs-to", indicator.id, asn_obj.id, p, confidence=80)
        elif rec.kind == "threat-intel":
            graph.relate("enriched-by", indicator.id, indicator.id, p,
                         confidence=int(rec.data.get("score", 0) * 100))
        elif rec.kind == "exposure":
            for svc in rec.data.get("services", []):
                svc_obj = StixObject(type="x-exposed-service", key=f"{subject}:{svc.get('port')}",
                                     props=svc, provenance=p)
                graph.add_object(svc_obj)
                graph.relate("exposes", indicator.id, svc_obj.id, p, confidence=90)

    for provider, call in ((dns, "resolve"), (cert, "certificates"), (rdap, "registration"),
                           (ti, "reputation"), (exposure, "exposed_services")):
        if provider is not None:
            method = getattr(provider, call)
            _add(method(subject))
    return records


def reconcile_finding(finding: Finding, ti_record: EnrichmentRecord, now: str) -> Finding:
    """When a threat-intel record that BACKED a finding goes stale/revoked, the finding's status changes —
    intel decay must propagate. A malicious verdict that is revoked drops a SUPPORTED finding to
    CONTRADICTED (its external basis no longer holds); a still-valid verdict leaves it unchanged."""
    from dataclasses import replace
    if is_stale(ti_record, now) and finding.status in (FindingStatus.SUPPORTED, FindingStatus.CONFIRMED):
        return replace(finding, status=FindingStatus.CONTRADICTED,
                       contradicting_evidence_refs=finding.contradicting_evidence_refs
                       + (f"stale-intel:{ti_record.record_id}",))
    return finding


def exposure_findings(records: list[EnrichmentRecord], asset: str) -> list[Finding]:
    """Turn exposure records into attack-surface findings (each carries the provider as evidence source)."""
    out: list[Finding] = []
    for rec in records:
        if rec.kind != "exposure":
            continue
        for svc in rec.data.get("services", []):
            out.append(Finding(
                claim=f"{asset} exposes {svc.get('service', 'service')} on port {svc.get('port')} "
                      f"(seen by {rec.provider} @ {rec.retrieved_at})",
                confidence=0.8, evidence_refs=(f"enrichment:{rec.record_id}",),
                affected_assets=(asset,), status=FindingStatus.SUPPORTED))
    return out
