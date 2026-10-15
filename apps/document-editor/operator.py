"""document-editor as a Mission Runtime operator.

Capabilities (namespace ``document.*``):

  document.connect_check   read    backend reachability + the truthful capability report
  document.inspect         read    local structure facts (+ editor open check when the transmit gate allows)
  document.open_session    read    short-lived, view-only, signed editor session config (expiry, tenant/doc scoped)
  document.convert         write*  docx->pdf etc.; output validated and stored as a NEW versioned artifact
  document.export          write*  same, to the export namespace            (*non-destructive: PRESERVE, no undo)
  document.propose_patch   read    NO mutation: diff/preview + validation bound to base version/hash/backend
  document.apply_patch     write   approval-gated, expected-version precondition, NEW version, read-back verified
  document.publish         write   approval-gated, allowlisted destinations only, irreversible

Compensation model (0.3.33, explicit-only): nothing here is ever automatically undone. apply_patch / convert /
export declare ``disposition=PRESERVE`` (their new versions and drafts are KEPT on failure; reversing a user's
document is a separate, human decision). publish is ``irreversible`` (a published copy cannot be recalled).
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from agentic_os.governance.classification import DataClassification
from agentic_os.mission.operator_sdk import Operator, capability, pending_approval

from .errors import ConfigError, DocEditorError, InvalidInput, TenantMismatch
from .secure_router import GatedOperator

_log = logging.getLogger("document_editor")

OPERATOR_NAME = "document-editor"
_CLS = [DataClassification.CUSTOMER_CONFIDENTIAL.value]

#: capabilities that may be auto-approved in TEST/DEMO drivers: reads only, never a write or a publish.
DEMO_AUTO_APPROVE_SAFE = frozenset({"document.connect_check", "document.inspect", "document.propose_patch"})

ServiceProvider = Callable[[], "object"]


def _tenant_of(inp: dict) -> str:
    """The tenant is the AUTHENTICATED tenant when the gated router injected one; a conflicting ``tenant`` input
    is refused. In-process (trusted runtime) callers pass ``tenant``."""
    auth, claimed = inp.get("_auth_tenant"), inp.get("tenant")
    if auth:
        if claimed and claimed != auth:
            raise TenantMismatch("tenant in the request does not match the authenticated tenant")
        return str(auth)
    if not claimed or not isinstance(claimed, str):
        raise InvalidInput("tenant is required")
    return claimed


def _actor_of(inp: dict) -> str:
    return str(inp.get("_auth_actor") or inp.get("actor") or "")[:128]


def build_document_editor_operator(provider: Optional[ServiceProvider] = None) -> Operator:
    """``provider()`` returns the DocumentService (or raises ConfigError -> every call fails closed, typed). The
    default provider builds the service lazily from the environment/config (``wiring.default_service``)."""
    if provider is None:
        from .wiring import default_service  # noqa: PLC0415
        provider = default_service

    def guarded(op: str, fn):
        def run(inp: dict) -> dict:
            tenant = ""
            try:
                tenant = _tenant_of(inp)
                return fn(provider(), tenant, inp)
            except DocEditorError as e:
                try:
                    if tenant and not isinstance(e, (ConfigError, TenantMismatch)):
                        provider().refusal(tenant, op, e, _actor_of(inp))
                except Exception:  # noqa: BLE001 - receipting a refusal must never mask the refusal
                    pass
                return e.to_result()
            except Exception:  # noqa: BLE001 - controlled failure, never a 500, never a stack trace to the caller
                _log.exception("document-editor %s failed", op)
                return {"status": "error", "error": "internal_error", "detail": "unexpected failure", "executed": False}
        return run

    def s_connect(svc, tenant, inp):
        return svc.connect_check(tenant, actor=_actor_of(inp))

    def s_inspect(svc, tenant, inp):
        return svc.inspect(tenant, _doc(inp), actor=_actor_of(inp), local_only=bool(inp.get("local_only")),
                           transmit_approval=inp.get("transmit_approval"))

    def s_open(svc, tenant, inp):
        return svc.open_session(tenant, _doc(inp), user_id=str(inp.get("user_id") or _actor_of(inp)),
                                actor=_actor_of(inp), ttl_s=int(inp.get("ttl_s") or 300),
                                transmit_approval=inp.get("transmit_approval"))

    def s_convert(svc, tenant, inp):
        return svc.convert(tenant, _doc(inp), _fmt(inp), actor=_actor_of(inp),
                           transmit_approval=inp.get("transmit_approval"))

    def s_export(svc, tenant, inp):
        return svc.export(tenant, _doc(inp), _fmt(inp), actor=_actor_of(inp),
                          transmit_approval=inp.get("transmit_approval"))

    def s_propose(svc, tenant, inp):
        return svc.propose_patch(tenant, _doc(inp), inp.get("ops"), track_changes=bool(inp.get("track_changes")),
                                 base_version=inp.get("base_version"), actor=_actor_of(inp))

    def apply_handler(inp: dict) -> dict:
        """Stages (zero backend calls, zero writes) unless the runtime delivered a VERIFIED ``_approval``."""
        approval = inp.get("_approval")
        if not approval:
            # Uniform staging: no backend call, no write, whatever else is wrong with the inputs. The preview is
            # best effort (an unknown proposal / tenant just yields the plain staged shape).
            preview: dict = {}
            try:
                preview = provider().preview_apply(_tenant_of(inp), str(inp.get("proposal_id", "")))
            except Exception:  # noqa: BLE001
                preview = {}
            return pending_approval("document.apply_patch", document_version_written=False, **preview)

        def go(svc, tenant, i):
            return svc.apply_patch(
                tenant, proposal_id=str(i.get("proposal_id", "")), base_hash=str(i.get("base_hash", "")),
                backend=str(i.get("backend", "")), patch_digest=str(i.get("patch_digest", "")),
                expected_version=_int(i.get("expected_version")),
                idempotency_key=str(i.get("idempotency_key") or i.get("_auth_idem") or ""),
                approval=approval if isinstance(approval, dict) else {"approved": True}, actor=_actor_of(i),
                mission_ref={"mission_id": i.get("_mission"), "node_id": i.get("_node")})
        return guarded("document.apply_patch", go)(inp)

    def publish_handler(inp: dict) -> dict:
        approval = inp.get("_approval")
        if not approval:
            return pending_approval("document.publish", published=False, destination=str(inp.get("destination", "")),
                                    doc_id=str(inp.get("doc_id", "")))

        def go(svc, tenant, i):
            return svc.publish(tenant, doc_id=_doc(i), version=_int(i.get("version")),
                               sha256=str(i.get("sha256", "")), destination=str(i.get("destination", "")),
                               approval=approval if isinstance(approval, dict) else {"approved": True},
                               actor=_actor_of(i),
                               mission_ref={"mission_id": i.get("_mission")})
        return guarded("document.publish", go)(inp)

    def label_handler(inp: dict) -> dict:
        approval = inp.get("_approval")
        if not approval:
            return pending_approval("document.set_label", label_set=False, doc_id=str(inp.get("doc_id", "")),
                                    label=str(inp.get("label", "")))

        def go(svc, tenant, i):
            return svc.set_label(tenant, _doc(i), str(i.get("label", "")),
                                 approval=approval if isinstance(approval, dict) else {"approved": True},
                                 actor=_actor_of(i), mission_ref={"mission_id": i.get("_mission")})
        return guarded("document.set_label", go)(inp)

    common = dict(network=["document-editor (configured, allowlisted external editor)"], data_classifications=_CLS)
    return GatedOperator(OPERATOR_NAME, [
        capability(
            "document.connect_check", guarded("document.connect_check", s_connect),
            provides=["document_backend_status"],
            outputs={"backend_reachable": "bool: the editor answered a JWT-verified version call",
                     "capabilities": "truthful per-capability report (declared AND proven)"},
            estimated_value="low", deterministic=False, concurrency_mode="read_only", **common),
        capability(
            "document.inspect", guarded("document.inspect", s_inspect), provides=["document_facts"],
            outputs={"sha256": "content hash of the inspected version", "version": "document version inspected"},
            estimated_value="low", concurrency_mode="read_only", **common),
        capability(
            "document.open_session", guarded("document.open_session", s_open), provides=["editor_session"],
            outputs={"session": "view-only signed editor config with an expiry, scoped to tenant+document+version"},
            estimated_value="low", concurrency_mode="read_only", **common),
        capability(
            "document.convert", guarded("document.convert", s_convert), provides=["converted_document"],
            outputs={"artifact_sha256": "sha256 of the stored, validated converted artifact",
                     "verified": "bool: output validated and read back from the store"},
            side_effecting=True, approval_required=False,
            compensation={"supported": False, "disposition": "PRESERVE", "reversibility": "IRREVERSIBLE",
                          "notes": "creates a NEW versioned artifact beside the source; nothing is overwritten and "
                                   "the artifact is kept on failure (deleting it is a separate human decision)"},
            permissions=["document:convert"], estimated_value="medium", latency_ms=5000,
            concurrency_mode="read_only", **common),
        capability(
            "document.export", guarded("document.export", s_export), provides=["exported_document"],
            outputs={"artifact_sha256": "sha256 of the stored, validated export",
                     "verified": "bool: output validated and read back from the store"},
            side_effecting=True, approval_required=False,
            compensation={"supported": False, "disposition": "PRESERVE", "reversibility": "IRREVERSIBLE",
                          "notes": "creates a NEW versioned export artifact; nothing is overwritten or deleted"},
            permissions=["document:export"], estimated_value="medium", latency_ms=5000,
            concurrency_mode="read_only", **common),
        capability(
            "document.propose_patch", guarded("document.propose_patch", s_propose), provides=["document_patch_proposal"],
            outputs={"proposal_id": "id bound to base version + hash + backend + patch digest",
                     "preview": "unified diff of the simulated result"},
            estimated_value="medium", concurrency_mode="read_only", **common),
        capability(
            "document.apply_patch", apply_handler, provides=["document_patched"],
            outputs={"verified": "bool: new version read back from the store and its hash re-verified",
                     "new_version": "version number written (a NEW version; never an in-place overwrite)",
                     "result_sha256": "sha256 of the written version"},
            side_effecting=True, approval_required=True,
            compensation={"supported": False, "disposition": "PRESERVE", "reversibility": "IRREVERSIBLE",
                          "notes": "writes a NEW immutable version; the prior version is untouched and every draft "
                                   "is preserved on failure. Reverting is a new, separately approved patch."},
            permissions=["document:write"], required_authority=["document:approve_write"],
            isolation_class="external-editor", estimated_value="high", latency_ms=20000,
            concurrency_mode="exclusive", concurrency_key="document:{doc_id}", **common),
        capability(
            "document.set_label", label_handler, provides=["document_labelled"],
            outputs={"verified": "bool: the label was read back from the sidecar manifest",
                     "label": "classification label now in force"},
            side_effecting=True, approval_required=True,
            compensation={"supported": False, "disposition": "PRESERVE", "reversibility": "IRREVERSIBLE",
                          "notes": "metadata only; the previous label is kept in the receipt ledger and a change back "
                                   "is a new, separately approved call. Downgrades are flagged in the receipt."},
            permissions=["document:classify"], required_authority=["document:approve_label"],
            estimated_value="medium", concurrency_mode="exclusive", concurrency_key="document:{doc_id}", **common),
        capability(
            "document.publish", publish_handler, provides=["document_published"],
            outputs={"verified": "bool: the published copy was read back and matches the approved hash",
                     "published": "bool: copy exists at an allowlisted destination"},
            side_effecting=True, approval_required=True,
            irreversible="a published copy leaves the connector's control and cannot be recalled",
            permissions=["document:publish"], required_authority=["document:approve_publish"],
            estimated_value="high", latency_ms=2000,
            concurrency_mode="exclusive", concurrency_key="document:{doc_id}", **common),
    ])


def _doc(inp: dict) -> str:
    d = inp.get("doc_id")
    if not isinstance(d, str) or not d:
        raise InvalidInput("doc_id is required")
    return d


def _fmt(inp: dict) -> str:
    f = inp.get("format")
    if not isinstance(f, str) or not f:
        raise InvalidInput("format is required")
    return f.lower()


def _int(v) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, str)) or (isinstance(v, str) and not v.isdigit()):
        raise InvalidInput("integer expected")
    return int(v)
