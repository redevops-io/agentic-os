# Security

Report suspected vulnerabilities privately; do not open a public issue.

- Email **redevops@redevops.io** (the ReDevOps organisation contact), stating that it is a security report.

Include the version or tag and a minimal reproduction. Design-level security documentation:
[docs/security-model.md](docs/security-model.md#control-plane-auth-idempotency-cache-and-multi-replica-limits), [docs/SECRETS.md](docs/SECRETS.md),
[docs/security-model.md](docs/security-model.md#signed-approval-tokens-http-operators), [docs/security-model.md](docs/security-model.md#permissions-plane--access-control-admin).
Never commit credentials; CI runs a gitleaks secret scan.
