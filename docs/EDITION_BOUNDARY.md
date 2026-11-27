# Edition boundary

What this repository's licence family (source-available: AGPL-3.0-or-later with the Commons Clause, not open source) contains, and what it
leaves to the enterprise edition. The rules below are the owner's decisions; `tests/test_edition_boundary.py` and
`docs/edition_boundary.json` check what can be checked mechanically. Nothing here is a legal statement about the licence.

## The rule

**Governance, single-host safety controls and tenant isolation stay in this repository. Distributed identity and deciding who belongs to which tenant do not.**

The basic permissions plane authorises the *application instance*, not callers: grants say which app, role or user may read, write or delete
which tool, table or corpus, and may restrict rows and columns inside the documented scope. It does not know who the caller is beyond the
local profile. A grant may name a tenant: it then matches only an identity that the caller's authentication has placed in that tenant, and
fails closed on a missing or different one. Nothing here decides which tenant a person or system belongs to.

### In this edition (community)

| Capability | Where it lives |
|---|---|
| Governance: approval gates, policy rules, mission authority envelopes, audit and event chains | `agentic_os/mission/*` |
| Tool execution controls: operator `/invoke` authority checks, execution authorization, route authorization | `mission/invoke_gate.py`, `app_kit/route_authz.py` |
| Basic grants and resource permissions over tools, tables and corpora; basic row and column restrictions within their documented scope | `permissions*.py`, `app_gate.py` |
| Single-host capability revocation epochs (per store, per process or host): revoking an authority takes effect at the next decision | `mission/authority.py` (`revoke`, `revoke_all`, `epoch`) |
| Tenant isolation: tenant-labelled tokens and visibility, tenant-scoped grants and revocation epochs, enforced fail-closed | `permissions.py`, `agent_gateway/scope.py`, `mission/control_auth.py` |
| Local single-user identity from the local profile; no identity taken from a request header; no implicit admin bypass | `local_capability.py`, `permissions.py` |
| Extension seams with working standalone defaults (`overlays.set_*`, `execution_context.set_*`) | `overlays.py`, `execution_context.py` |
| A request for an enterprise capability without the enterprise package fails closed with a named missing-capability error | `app_gate.py`, `tool_actions.py`, `trusted_import.py` |

### Enterprise only (must not be in this artifact)

| Capability | Boundary check |
|---|---|
| Identity provider integration: SSO, OIDC, SAML, SCIM, JWKS-verified or federated principals | vocabulary check |
| RBAC and ABAC administration, break-glass access, enterprise policy administration, compliance reporting | vocabulary check |
| Multi-tenant authorization as a managed capability: tenant provisioning and onboarding, resolving which tenant an external identity belongs to, cross-tenant delegation, per-tenant secret namespaces | vocabulary check |
| Revocation across conversations, caches and hosts; stale-cache invalidation | pinned (no cross-host field) |
| Permission-aware retrieval; end-to-end document, row, column and graph authorization | pinned (no enforcement claim) |
| Cross-host tenant authorization | pinned (no tenant argument on `authorize`: the tenant is the authenticated identity's) |
| Any dependency of this runtime on an enterprise identity or permission service | import check |

## Decisions recorded for borderline cases

| ID | Case | Decision |
|---|---|---|
| B1 | Tenant-scoped tokens and tenant visibility | **Community (owner decision 2026-11-27: preserve tenant isolation and explicit permission scope).** `AGENTIC_OS_TENANT_TOKENS`, `AGENTIC_OS_TENANT_LIMITS`, a 404 for another tenant's mission, tenant-aware drill-down, tenant-scoped agent tokens, grants, governed writes and application state. The modules that read the tenant settings are catalogued, so a new one is a reviewed change. Replaces the 2026 decision that this was enterprise and not an isolation guarantee. |
| B2 | Single-host capability revocation epochs | **Community.** |
| B3 | Tenant and epoch fields on grants | **Enforced.** A permissions `Grant` carries `tenant` and `revocation_epoch`. A tenant-scoped grant matches only an identity of that tenant, including for privileged roles unless a role is named in `global_privileged_roles`; a missing or different tenant is refused, and an epoch bump ends older grants at the next decision. `authorize()` takes no tenant argument: it is the authenticated identity's. `tests/test_edition_boundary.py` checks the enforcement itself, so the fields can never become decoration. |

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
* a tenant or epoch on a permissions `Grant` stops being enforced, or `authorize` gains a tenant argument (B3);
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
