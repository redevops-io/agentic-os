# origin/main's thin desktop shell, preserved (convergence stage 2b)

This directory is the desktop shell exactly as `origin/main` 7b4b11c1 carried it in `deploy/launcher/` (connection screen, keychain,
tray, deep links, `apps-v*` release flow; version 0.2.0). The converged trunk ships the RC line's launcher in `deploy/launcher/`
(bundled sidecar, capability handshake, About/Legal view, R0-prime identity) because the two cannot share one directory: the RC's
tests treat every file in `ui/` as served bytes and the two shells register different commands and capabilities.

Which shell is the product is an OPEN owner decision (convergence plan F2). Nothing in the release packaging reads this
directory; `.github/workflows/desktop-shell.yml` still compiles and tests it (paths retargeted, otherwise unchanged).

Two adaptations only: the icons are the RC line's neutral artwork (the previous artwork may appear in no file of the repository,
tests/test_launcher_icons.py), and the one allow-listed appender in `selftest/run.py` is listed under its new path in
tests/test_append_writers.py. Everything else is byte-identical to `origin/main` 7b4b11c1 `deploy/launcher/`.
