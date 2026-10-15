# OS support statement

Initial supported platforms for the connector code: **Windows, macOS and Linux** (no Android, iOS or web).

* Pure Python with `pathlib` / `os.replace`; no shell, `apt`, bash or POSIX-only calls. Document ids are *logical*
  posix-style paths validated identically on every OS (backslashes, drive letters, UNC paths, alternate data streams,
  reserved device names, trailing dots/spaces and 8.3 aliases are rejected everywhere).
* Locking is a portable lock *file* (`O_CREAT|O_EXCL`, stale-lock break by atomic rename) - no `fcntl`, no `msvcrt`. It is
  a local-filesystem lock; do not put the document root on a network filesystem that lacks atomic `O_EXCL`.
* Atomic writes use temp file + `fsync` + `os.replace` (with a short retry for Windows sharing violations).
* Symlinks, and Windows junctions / reparse points, inside a tenant's tree are refused.
* **Tested OS matrix = ubuntu-latest, macos-latest, windows-latest**, enforced by `.github/workflows/document-editor.yml`
  (connector unit tests; the integration suite needs Docker and runs locally).
* The service container image is Linux (`python:3.12-slim`), like every app image. Running the *connector service* natively
  on Windows/macOS is exercised only through the unit tests, not through a packaged installer.
* The external editor is whatever the customer runs; the connector only speaks the documented HTTP protocols to it.
