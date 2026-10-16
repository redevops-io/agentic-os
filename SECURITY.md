# Security

Report suspected vulnerabilities privately; do not open a public issue.

- Email **redevops@redevops.io** (the ReDevOps organisation contact), stating that it is a security report.

Include the version or tag and a minimal reproduction. Design-level security documentation:
[docs/CONTROL_PLANE_SECURITY.md](docs/CONTROL_PLANE_SECURITY.md), [docs/SECRETS.md](docs/SECRETS.md),
[docs/APPROVAL_TOKENS.md](docs/APPROVAL_TOKENS.md), [docs/permissions.md](docs/permissions.md).
Never commit credentials; CI runs a gitleaks secret scan.
