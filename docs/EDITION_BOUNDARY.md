# Edition boundary

What this repository's licence family (source-available: AGPL-3.0-or-later with the Commons Clause, not open source) contains, and what it
leaves to the enterprise edition. The rules below are the owner's decisions; `tests/test_edition_boundary.py` and
`docs/edition_boundary.json` check what can be checked mechanically. Nothing here is a legal statement about the licence.

## The rule

**Governance and single-host safety controls stay in this repository. Distributed identity and tenant authorization do not.**

The basic permissions plane authorises the *application instance*, not callers: grants say which app, role or user may read, write or delete
which tool, table or corpus, and may restrict rows and columns inside the documented scope. It does not know who the caller is beyond the
local profile, and it does not manage or authenticate tenants. It can compare a tenant label carried on a grant with a tenant label carried on the
identity and refuse a mismatch (decision B3 below); that comparison is the whole of what this edition does about tenants.

### In this edition (community)

| Capability | Where it lives |
|---|---|
| Governance: approval gates, policy rules, mission authority envelopes, audit and event chains | `agentic_os/mission/*` |
| Tool execution controls: operator `/invoke` authority checks, execution authorization, route authorization | `mission/invoke_gate.py`, `app_kit/route_authz.py` |
| Basic grants and resource permissions over tools, tables and corpora; basic row and column restrictions within their documented scope | `permissions*.py`, `app_gate.py` |
| Single-host capability revocation epochs (per store, per process or host): revoking an authority takes effect at the next decision | `mission/authority.py` (`revoke`, `revoke_all`, `epoch`) |
| An optional tenant label and revocation epoch on a permissions grant, compared fail-closed against the identity's tenant label at the next decision, on one host (B3) | `permissions.py` (`Grant`, `Identity`, `PermissionsPlane`, `GrantStore.revoke_epoch`) |
| Local single-user identity from the local profile; no identity taken from a request header; no implicit admin bypass | `local_capability.py`, `permissions.py` |
| Extension seams with working standalone defaults (`overlays.set_*`, `execution_context.set_*`) | `overlays.py`, `execution_context.py` |
| A request for an enterprise capability without the enterprise package fails closed with a named missing-capability error | `app_gate.py`, `tool_actions.py`, `trusted_import.py` |

### Enterprise only (must not be in this artifact)

| Capability | Boundary check |
|---|---|
| Identity provider integration: SSO, OIDC, SAML, SCIM, JWKS-verified or federated principals | vocabulary check |
| RBAC and ABAC administration, break-glass access, enterprise policy administration, compliance reporting | vocabulary check |
| Tenant identity and tenant-scoped tokens and visibility (B1); multi-tenant authorization and isolation as a managed capability: tenant provisioning, cross-tenant delegation, per-tenant secret namespaces | vocabulary check, pinned legacy surface |
| Revocation across conversations, caches and hosts; stale-cache invalidation | pinned (no cross-host field) |
| Permission-aware retrieval; end-to-end document, row, column and graph authorization | pinned (no enforcement claim) |
| Cross-host tenant authorization; tenant membership resolution | pinned (`authorize` takes no resolver or membership argument) |
| Any dependency of this runtime on an enterprise identity or permission service | import check |

## Decisions recorded for borderline cases

| ID | Case | Decision |
|---|---|---|
| B1 | Tenant-scoped tokens and tenant visibility | **Enterprise.** This tree still contains the single-deployment tenant labelling that has shipped since v0.3.43 (`AGENTIC_OS_TENANT_TOKENS`, `AGENTIC_OS_TENANT_LIMITS`, a 404 for another tenant's mission, tenant-aware drill-down). It is kept so existing callers do not break, it is **not** a tenant-isolation guarantee, and no further tenant-isolation feature is added here. Removing or relocating it is a separate, breaking change that has not been made. |
| B2 | Single-host capability revocation epochs | **Community.** |
| B3 | Tenant and epoch fields on grants | **Amended (hybrid).** The core carries and ENFORCES, on one host, an optional `tenant` label and `revocation_epoch` on a permissions `Grant`, an optional `tenant` label on an `Identity`, and echoes the tenant on an `AccessDecision`. What it enforces, fail closed: a tenant-scoped grant never matches an identity whose tenant is missing, empty or different (comparison is exact: `""`, `*`, padding, case and look-alike characters are not wildcards); a privileged role is not authority across tenants (only a role named in `global_privileged_roles`, empty by default, is); a grant carrying an epoch ends at the next decision once `GrantStore.revoke_epoch` has moved the epoch for its (subject kind, subject, tenant), and a store that cannot report the epoch denies the grant; a malformed tenant or epoch makes the grant or the principal unusable. Absent tenant and epoch is the pre-contract single-operator case, with the same stored bytes and decisions: an untenanted grant keeps its pre-contract meaning and matches whatever tenant label the identity carries. What it does NOT do, and what stays **Enterprise**: authenticate the label (the tenant on an identity is whatever the caller asserted; nothing in this edition resolves which tenant a principal belongs to or checks membership), provision tenants, resolve identities from an identity provider, delegate across tenants, grant fine-grained per-tenant permissions, or revoke across hosts, conversations and caches. B1 stands: this is a single-host label comparison, not a tenant-isolation capability, and it must not be described as one. |

Distributed tenant identity, permission-aware retrieval and cross-host tenant authorization remain enterprise whatever the vocabulary in this
tree says.

## What the checks do

`docs/edition_boundary.json` lists each community capability with a test that proves it runs with the enterprise package absent, and each
enterprise-only capability with the evidence that this tree does not provide it. `tests/test_edition_boundary.py` fails when:

* a module other than the reviewed allow-list names an enterprise package, or does so outside a function (every enterprise import is lazy and
  behind an explicit request);
* a module or top-level definition uses enterprise vocabulary (`sso`, `oidc`, `saml`, `scim`, `jwks`, `abac`, `break_glass`,
  `tenant_provision*`) without an entry in the catalogue;
* the project declares a plug-in entry-point group, or an enterprise package as a dependency, or the kernel reads `importlib.metadata`
  entry points;
* the tenant or epoch on a `Grant` stops being optional and inert-when-absent, or `authorize` gains a membership resolver or tenant argument (B3);
* the catalogue names a test that does not exist.

The checks work from names, hashes and symbols. They cannot read what a real enterprise adapter does; that is verified in the enterprise
repository.

## Hosted default-profile posture

A hosted deployment is open unless it sets the production profile; see "Hosted default-profile posture" in `docs/SECURITY_MODEL.md`.

## Migration note for enterprise overlay authors

The enterprise overlay pins kernel **v0.4.0** and should stay on that pin. The 0.6.0 line is not drop-in for it:

* Operator actions (`approve`, `reconcile`, `compensate`, `suspend`, `resume`) take an `AuthenticatedPrincipal`. A free-text `actor` is no longer an
  identity, and `runtime.approve(mission, node, decision, edit)` without a principal raises `PrincipalRequired`. Issue principals with
  `PrincipalIssuer` from your own authenticated session.
* Authority is mandatory in every profile: a mission with no authority envelope cannot run an unattended side effect, and resuming a
  reconstructed mission needs a current resume authorization.
* Operator `/invoke` needs an authenticated identity before a side effect runs.
* The Projects API has new write routes (`/api/app-directory/record-link`, `/api/app-directory/ref-link`, `/api/sources/remove`, the drill-down
  mount); a pinned route inventory must list them.
* From rc2: a negative or non-finite `amount` is refused (`amount_invalid`) on a grant with an amount limit; capability patterns and
  resource selectors must satisfy the grammar in `docs/SECURITY_MODEL.md` (no control characters, no `..` segment, no `**`, balanced `[...]`),
  a resource value containing `,` or `;` matches only a pattern that contains that character itself, and a forward capability may not start
  with `compensate:`.
* Fixtures generated from kernel behaviour (for example the Go port's golden files) must be regenerated.

Against the offline suites, moving the pin to 0.6.0-rc1 failed 114 of 3,540 enterprise tests, almost all from the principal requirement and
mandatory authority. That is hardening, not leakage; it was not run against a live identity provider, Vault, PostgreSQL or pgvector, and those
paths are unverified.
