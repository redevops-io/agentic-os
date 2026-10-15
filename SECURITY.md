# Security

Report suspected vulnerabilities privately; do not open a public issue.

- **Security contact: to be confirmed.** No dedicated security address is published for this repository.
- GitHub private vulnerability reporting was **not enabled** here when checked on 2026-10-15. Until a contact
  is confirmed, use the general ReDevOps contact on the GitHub organization profile (redevops@redevops.io),
  stating that it is a security report. That address is a general contact, not a confirmed security inbox.

Include the version or tag and a minimal reproduction. Design-level security documentation:
[docs/CONTROL_PLANE_SECURITY.md](docs/CONTROL_PLANE_SECURITY.md), [docs/SECRETS.md](docs/SECRETS.md),
[docs/APPROVAL_TOKENS.md](docs/APPROVAL_TOKENS.md), [docs/permissions.md](docs/permissions.md).
Never commit credentials; CI runs a gitleaks secret scan.
