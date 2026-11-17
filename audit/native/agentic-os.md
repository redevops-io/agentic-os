# NAT-00 audit: `agentic-os` (public repository)

Auditor scope: the governance plane, the AGPL permissions plane, local services and CLI, config/secrets, module system, packaging, apps, tests.
Out of scope: context-runtime and agent-harness internals (only the APIs agentic-os imports are recorded), and edition-specific features.

Conventions: `file:line` refs are to the audited commit. "INFERRED" marks a conclusion drawn from code or documented platform behaviour that was not executed on that platform. No Windows host was available: nothing below is a Windows runtime result. Effort figures are guesses.

## 1. Summary

**Verdict: GO-WITH-CHANGES.** A pure-Python governance core exists that needs only `cryptography`, `PyYAML` and `runtime-contracts` (stdlib otherwise), and every shipped dependency has `win_amd64` and macOS arm64 wheels. Its state stores are hard-wired to POSIX (`fcntl`, mode bits, `uid`, `resource`), and the existing local service is unauthenticated by default. Those are fixable in NAT-01/02/05/06, but three of them are release blockers as the code stands.

Top five risks:

1. **Governance state stores need POSIX file locking.**
   - `GrantStore` (the AGPL permissions plane) raises on any platform without `fcntl`, by design and documented (`agentic_os/permissions.py:46-48,633-636`, `docs/PERMISSIONS_MIGRATION.md:35,263`).
   - The hash-chained mission event log and the outcome store silently run with no inter-process lock when `fcntl` is missing (`agentic_os/mission/store.py:176-181`, `outcome_store.py:97-121`); the fork/join lock raises `ImportError` instead (`fork_join.py:459-470`).
   - Result: the permissions plane is unavailable and the audit chain can fork across processes on Windows.
2. **Local service is unauthenticated by default.**
   - The frozen sidecar serves the Projects API on fixed `127.0.0.1:8787` (`agentic_os/projects_api.py:1960-1966`, `deploy/launcher/src-tauri/src/main.rs:29`).
   - Route authorization is enforced only under `AGENTIC_OS_PROFILE=production` (`projects_route_policy.py` docstring, `app_kit/route_authz.py:265-281`, `mission/profile.py:28-43`).
   - There is no Host or Origin validation. Verified here with a test client: a request with a foreign `Host` header returns 200.
   - This is a release blocker for the native sidecar (unauthenticated sidecar access) unless changed; detail is held privately.
3. **Security checks are written in POSIX mode bits and uids.**
   - Key-file, secret-file and local-profile checks use `st_mode & 0o177`, `!= 0o600` and `os.geteuid()` (`permissions.py:488-499`, `permissions_setup.py:60-71` with an unguarded `geteuid`, `secrets/stores.py:67-72`, and the same pattern in `runtime_contracts/secrets_local/store.py:84`).
   - On Windows these would refuse every legitimate file (INFERRED: `os.stat` reports synthetic modes).
   - The secrets bootstrap edits `~/.zshrc`/`~/.bashrc` on macOS/Linux (`secrets/bootstrap.py:344-351`) and writes managed secrets into the user environment on Windows (`:335-339,414-418`).
   - The OS credential-store class exists (`secrets/stores.py:123`) but `keyring` is not a declared dependency and is absent from the frozen sidecar.
4. **The containment and posture layer assumes Linux and Docker.**
   - `mission/membrane.py:33` does `import resource` at module top, so it is not importable on Windows. Simulated import: 3 of 527 modules fail, namely `membrane`, `execution_backend` and `control_plane`.
   - macOS refuses `RLIMIT_AS`, so the membrane fails closed there (10 tests skipped here).
   - `device_posture.derive` requires a container runtime (`docker`/`podman`) for any non-BLOCKED verdict (`mission/device_posture.py:221-236`), so a native, Docker-free host is BLOCKED by construction.
   - `fork_join._pid_alive` calls `os.kill(pid, 0)` (`fork_join.py:441-449`); on Windows a signal other than the Ctrl events terminates the process (documented Python behaviour, INFERRED here).
   - 11 of the 12 kernel `subprocess` calls inherit the full environment, and there is no process-tree handling.
5. **Distribution is not yet publishable.**
   - The wheel declares git direct references (`pyproject.toml:12-27`) that PyPI refuses.
   - `modules.yaml` and `deploy/assets` live outside the wheel (`registry.py:10`, `control_plane.py:163`).
   - The sidecar is a PyInstaller one-file binary with no signing or entitlements configured (`packaging/redevops-sidecar.spec:160-166`).
   - `packaging/requirements.lock` is stale (agentic-os 0.2.4, runtime-contracts 0.3.4, no discovery-runtime or knowledge-frontier), diverges from `uv.lock`, and has no hashes.
   - CI has no SBOM, provenance or vulnerability scan, and there is no third-party notices file.
   - There is no `.gitattributes`, so Windows checkouts risk CRLF in vectors and scripts.

Counts: 26 items in the risk-ranked list (section 11: 3 blocker, 9 high, 10 medium, 4 low); 12 entries in the unsupported-features register (section 12); kernel suite 6,679 passed / 1 failed / 94 skipped / 4 xfailed after correcting one run-setup omission (first run 6,675 / 5 / 94 / 4); 19 app suites 2,257 passed / 11 skipped.

## 2. Repo facts

| Item | Value |
|---|---|
| Repository / ref | `github.com/redevops-io/agentic-os`, `origin/main` |
| Commit audited | `40d1729b1eda73ea4ef7b0859e3314cae3ee42df` (2026-10-29, `v0.4.1-3-g40d1729`) |
| Package | `agentic-os` 0.4.1, `requires-python >=3.11`; CI and packaging use CPython 3.12 (`.github/workflows/tests.yml`, `packaging/build-sidecar.sh:177`); tested here on 3.12.16 |
| Size | 1,782 tracked files; 1,418 `.py`; `agentic_os/` 536 py / ~91,000 lines (527 importable modules); `apps/` 346 py; `tests/` 481 py; `deploy/` 172 files; Rust launcher 7 `.rs` files |
| JVM/Kotlin | None in this repository (no `.kt`, `build.gradle*`, `pom.xml`). No JVM runtime requirement. |
| Licence | Source-available: AGPL-3.0-or-later with Commons Clause (`LICENSE.md`, `pyproject.toml:8`) |
| Console scripts | `agentic-os` -> `agentic_os.cli:main`; `agentic-os-projects` -> `agentic_os.projects_api:main` (`pyproject.toml:69-74`) |
| Other entry points | `uvicorn agentic_os.control_plane:app` (Docker CMD); `python -m agentic_os.mission.unified_server`; `python -m agentic_os.mission.bootstrap`; `python -m agentic_os.intelligence.demo_server`; `app_kit/suite_server.py`; `packaging/sidecar_main.py` (`serve` default, `bootstrap`); `deploy/module_service.py`; each `apps/*/app.py` |
| Extras | `dev`, `duckdb`, `gsc`, `postgres`, `rag`, `connectors`, `projects`, `google`, `microsoft`, `slack`, `office`, `mcp` (`pyproject.toml:36-67`) |
| Module catalog | `modules.yaml`: 21 entries (15 `compose`, 5 `operator`, 1 `tool`); generated from app manifests. `apps/`: 21 directories |
| Core dependencies | pyyaml, fastapi, uvicorn, pydantic, httpx, cryptography, plus three git-pinned: runtime-contracts v0.3.14, discovery-runtime v0.1.19, knowledge-frontier v0.2.0 (`pyproject.toml:12-28`) |
| Lock | `uv.lock` revision 3: 144 packages (5 git, 138 registry, 1 editable); `uv sync --frozen` succeeded on macOS arm64 |
| Wheel built here | `py3-none-any`, 1.7 MB, 550 files, zero native binaries; `modules.yaml` is NOT in it |
| Existing native work | Rust/Tauri launcher `deploy/launcher/src-tauri` (Tauri 2.12.5; keyring 3.6.3 with apple-native/windows-native; 496 crates in `Cargo.lock`); `packaging/` wheelhouse + PyInstaller sidecar; workflows `native-packaging.yml`, `desktop-shell.yml` |
| Existing Windows CI evidence | `document-editor.yml` runs one app's unit tests on windows-latest: 600 passed, 1 skipped (run of 2026-10-30; macOS 599/2, Ubuntu 601). `desktop-shell.yml` compiles the launcher and runs its 31 Rust tests on all three OS (green 2026-10-21). `native-packaging.yml` last ran 2026-09-18: bundles built unsigned on all three OS (Windows artifact 73 MB, macOS-14 39 MB, Ubuntu 169 MB); only Ubuntu got an install/serve acceptance. |
| Docker/Linux reliance | Compose/Docker only for the app fleet (`fleet.py:78-98`, `mission/installer.py:270-275`, `deploy/`), the control-plane `Dockerfile`, k8s manifests. No Redis, MinIO, Doris, Kafka or boto3 import anywhere in Python. Postgres is optional (`psycopg`, `MISSION_EVENT_BACKEND=postgres`, `sources_postgres.py`); Dagster is remote (`mission/dagster_exec.py:52`). In-process stores: JSONL, SQLite (WAL), optional DuckDB file. |

### Imports of context-runtime and agent-harness (APIs and assumptions)

Neither is a declared dependency (`pyproject.toml`); all uses are guarded soft imports.

| API used | Where | Assumption |
|---|---|---|
| `agent_harness.tools`: `ToolRegistry`, `ToolResult`, `function_tool`, `ApprovalPolicy` | `permissions_profiles.py:35,205,222` | Optional; 27 kernel tests skip when absent |
| `context_runtime.tools.base`: `set_default_authorizer`; read-back via `get_default_authorizer` or a module-level `_default_authorizer` (or the module defining the setter) | `app_gate.py:188-250,585` | Process-wide authorizer lives in-process; layout probing is fragile |
| `context_runtime.integrations.agent_console` (`AgentConsole`, `tool`), `.vuln_db`, `.supply_chain`, `.tools.mcp.MCPClient` | `apps/growth-engine/app.py:385`, `apps/market-radar/app.py:382,465`, `apps/edge-sentinel/app.py:629,725,734`, `apps/agentic-privacy/app.py:307` | Linux-hosted app integrations only |
| Trust gate | `trusted_import.py:99-132`, `app_gate.py:169-174` | The package is imported only if a dist-info RECORD in the interpreter's own package dirs lists it. INFERRED: in a PyInstaller bundle the dist-info is usually absent, so the engine hook would be silently disabled; needs a test in NAT-04/06. |

Edition-specific features are out of scope.

## 3. Minimal native core proposal

Method: static import closure (AST) from 14 root modules covering policy, approvals, budgets, audit chain, receipts, replay, permissions and the gateway: `mission.{runtime,approval,authority,store,receipt,verify,executor,control_auth,profile}`, `permissions`, `governance.{policy,receipt}`, `agent_gateway.{gateway,audit_store}`.

- **Top-level imports only: 74 modules, 16,956 lines** (of 527 modules / ~91k lines). Third-party need is `cryptography`, `runtime_contracts`, `yaml` only.
- With lazy in-function imports: 117 modules / 26,201 lines, adding `fastapi`, `uvicorn`, `fastmcp`, `keyring`, `psycopg`, `redevops_connectors` (all optional paths).
- The 74-module closure contains none of the three modules that fail the simulated no-POSIX import (`membrane`, `execution_backend`, `control_plane`). It does include `fleet.py` and `router.py` because `agentic_os/__init__.py` imports them eagerly (`agentic_os/__init__.py:11`).

**Proposed core (Tier 0, no Docker, no WSL, no external service):**

| Capability | Modules (current location) | Native state |
|---|---|---|
| Policy / governance | `governance/{policy,classification,tools,pools,routing,receipt}.py`, `mission/{policy,policy_decision,governance,profile}.py` | Pure logic |
| Approvals | `mission/approval.py` (HMAC tokens, in-memory `ReplayGuard`, `SqliteReplayGuard`), `approval_gate.py`; `context.py` approvals log (to be replaced) | SQLite WAL file |
| Budgets / authority | `mission/authority.py` (envelopes, `SqliteLedger` with `BEGIN IMMEDIATE`, `SqliteEnvelopeStore`, leases); `router.py` budget is in-memory only | SQLite WAL file |
| Audit chain | `mission/store.py` (`EventStore` JSONL, hash chain, `verify_chain`), `agent_gateway/audit_store.py` | Append-only JSONL, needs lock port |
| Receipts / replay | `mission/receipt.py`, `governance/receipt.py`, `historical_replay.py`, `MemoryEventStore`, `conformance/vectors/*.json` | Derived from events |
| Permissions plane (AGPL) | `permissions.py`, `permissions_setup.py`, `permissions_profiles.py`, `app_gate.py` (basic mode needs nothing else) | Encrypted file + lock + rollback counter |
| Gateway | `agent_gateway/{gateway,contracts,auth,idempotency,egress,registry,capabilities,protocols}` (MCP Streamable-HTTP via optional `fastmcp`) | Pure logic |
| Secrets | `secrets/{stores,resolve,factory}.py` (`KeyringSecretStore`, `EncryptedFileSecretStore`) | OS store |
| Contracts | `runtime-contracts` (pure Python, depends on `pyyaml`) | None |

**Not core** (remain integrations or Linux-hosted): `fleet.py` use, `mission/installer.py`, `pair*.py` (docker, `apt`, `sudo`, `curl | sh`), the `device_posture` Docker floor, `membrane.py` (replace through a `sandbox_backend`), `control_plane.py` (Linux/Docker entry), `projects_api.py` demo provider and UI bundle, `sources_*`, `intelligence/`, `world/`, `content/`, `social/`, `revenue/`, all `apps/*` (Dockerfiles, bind `0.0.0.0`), `deploy/*`.

Native footprint of the sidecar extras (`projects,duckdb`): five native wheels (`cffi`, `cryptography`, `duckdb`, `pydantic-core`, `PyYAML`).

## 4. Dependency and wheel matrix

Method: `uv.lock` parsed; closure computed per extra; every registry package in the shipped closure fetched with `pip download --only-binary=:all: --no-deps --python-version 3.12 --implementation cp` for `--platform win_amd64` and for macOS arm64 (`macosx_11_0/12_0/14_0_arm64` and `universal2` tags). All 39 downloads succeeded on both. All 138 registry packages in the lock were additionally checked against the PyPI JSON API for a cp312 `win_amd64` and macOS arm64/universal2 wheel.

Result: **no wheel blocker for any shipped dependency.** The only packages without a macOS arm64 wheel are Linux/CUDA-only and gated by `sys_platform` markers (`nvidia-*`, `cuda-*`, `triton`; `pywin32` is win32-only), all in the optional `rag` extra, which the sidecar excludes (`packaging/redevops-sidecar.spec:156`).

Shipped closure (base + `duckdb` + `mcp` + `gsc` + `postgres`; the sidecar uses base + `projects` + `duckdb`):

| Package | Version | win_amd64 wheel | macOS arm64 wheel | Native libs (win/mac) | Licence |
|---|---|---|---|---|---|
| cffi | 2.1.1 | cp312 win_amd64 | cp312 macosx_11_0_arm64 | 1/1 | MIT-0 |
| charset-normalizer | 3.5.1 | cp312 win_amd64 | cp312 macosx_10_13_universal2 | 2/2 | MIT |
| cryptography | 50.0.1 | cp311-abi3 win_amd64 | cp311-abi3 macosx_11_0_arm64 | 1/1 (static OpenSSL) | Apache-2.0 OR BSD-3-Clause |
| duckdb | 1.5.5 | cp312 win_amd64 | cp312 macosx_11_0_arm64 | 1/1 | MIT |
| pydantic-core | 2.46.4 | cp312 win_amd64 | cp312 macosx_11_0_arm64 | 1/1 | MIT |
| pyyaml | 6.0.3 | cp312 win_amd64 | cp312 macosx_11_0_arm64 | 1/1 | MIT |
| psycopg-binary (postgres extra, bundles libpq) | 3.3.5 | cp312 win_amd64 | cp312 macosx_11_0_arm64 | not unpacked (PyPI JSON check only) | LGPL-3.0-only |
| psycopg (pure) | 3.3.5 | none-any | none-any | 0 | LGPL-3.0-only |
| certifi | 2026.10.19 | none-any | none-any | 0 | MPL-2.0 |
| fastapi 0.138.0, starlette 1.3.1, uvicorn 0.49.0, httpx 0.28.1, httpcore 1.0.9, h11 0.16.0, anyio 4.14.2, click 8.5.0, idna 3.18, typing-extensions 4.15.0, typing-inspection 0.4.2, annotated-types 0.7.0, annotated-doc 0.0.4, pycparser 3.0, pydantic 2.13.4 | as listed | none-any | none-any | 0 | MIT / BSD-3-Clause / PSF-2.0 |
| fastmcp / fastmcp-slim 4.0.5, mcp-types 2.2.0, pydantic-settings 2.15.0, python-dotenv 1.2.3, platformdirs 4.11.11, rich 15.0.0, markdown-it-py 4.2.0, mdurl 0.1.2, pygments 2.20.0 | as listed | none-any | none-any | 0 | Apache-2.0 / MIT / BSD (`mcp` extra) |
| google-auth 2.59.0, requests 2.34.2, urllib3 2.8.0, pyasn1 0.6.4, pyasn1-modules 0.4.2, tzdata 2026.3 | as listed | none-any | none-any | 0 | Apache-2.0 / MIT / BSD (`gsc`/`postgres` extras) |
| pywin32 (only via `mcp`, marker `sys_platform == 'win32'`) | 312 | cp312 win_amd64 | n/a | DLLs | PSF |

Git-pinned dependencies (built from source; pure Python; none on PyPI, confirmed by PyPI query):

| Package | Tag / SHA | Own deps | Licence |
|---|---|---|---|
| runtime-contracts | v0.3.14 / `80e727d8` | pyyaml | AGPL-3.0-or-later WITH Commons-Clause |
| discovery-runtime | v0.1.19 / `b49f66c6` | runtime-contracts (git direct URL) | AGPL-3.0-or-later WITH Commons-Clause |
| knowledge-frontier | v0.2.0 / `057258ae` | none | AGPL-3.0-or-later |
| redevops-connectors (`projects` extra) | v0.1.0 / `7fbb9ec4` | none (stdlib) | AGPL-3.0-or-later |
| redevops-rag (`rag` extra) | v0.4.0 / `33ac617f` | duckdb, numpy, sentence-transformers (torch) | AGPL-3.0-or-later |

`rag` extra only (not shipped): `torch` 2.14.0, `numpy` 2.5.3, `scipy` 1.18.1, `scikit-learn` 1.9.1, `tokenizers` 0.23.2, `safetensors` 0.8.0, `hf-xet` 1.6.0, `regex`, `markupsafe`: all have win_amd64 and macOS arm64 wheels (PyPI JSON). The NVIDIA wheels are `LicenseRef-NVIDIA-Proprietary`; they are Linux-only by marker and must never enter a bundle.

Rust launcher: 496 crates (`Cargo.lock`), `rustls` + `ring` (no OpenSSL), `windows-sys`, `webview2-com`, `security-framework`. Crate licences were not audited (no `cargo` toolchain here); run `cargo deny` in NAT-08.

Notes:
- `psycopg` (LGPL) and `certifi` (MPL) need notice handling in any redistributed bundle.
- The Windows build needs `git` on the build host (git-pinned deps, `packaging/build-windows.ps1`).
- A diverging second lock exists: `packaging/requirements.lock` (a `pip freeze`, no hashes) differs from `uv.lock` (for example uvicorn 0.52.4 vs 0.49.0, starlette 1.6.0 vs 1.3.1).

## 5. POSIX-assumption findings (file:line)

Evidence for row 1's import claim: an import-time simulation (a meta-path blocker for `fcntl`, `resource`, `termios`, `pwd`, `grp`, `pty`, `tty`, plus deletion of `os.geteuid/fork/killpg/setsid/chown`, `signal.SIGKILL/SIGHUP/...`, `O_NOFOLLOW`) imported all 527 `agentic_os` modules. Three failed (`control_plane`: `GrantStorageError`; `membrane` and `execution_backend`: `ImportError: resource`). This models missing APIs only; it is not a Windows run.

| # | Location | POSIX assumption | Effect on Windows / macOS (INFERRED unless stated) |
|---|---|---|---|
| 1 | `permissions.py:46-48,633-636`; `control_plane.py:610` | `import fcntl`; constructor raises `GrantStorageError` without it; control plane builds `GrantStore()` at import | Windows: permissions plane and the `control_plane` module unusable (observed under simulation) |
| 2 | `permissions.py:692-722` (`_txn`) | `fcntl.flock(LOCK_EX|LOCK_NB)` on `<dir>/.grants.lock`, 10 s bounded wait | Needs a cross-platform exclusive lock |
| 3 | `mission/store.py:176-181` | `import fcntl` inside append; `except ImportError: pass` (no lock) | Silent: two writer processes can both extend the chain from the same head |
| 4 | `mission/fork_join.py:451-470` | `import fcntl`, `open(path, "a+")` text mode, `flock` | Windows: `ImportError` on any file-backed fork/join |
| 5 | `outcome_store.py:97-121` | `fcntl.flock`; no-op plus one warning if missing; `os.open(..., 0o644)` | Windows: single-writer only |
| 6 | `apps/agentic-privacy/core.py:36,125-133` | `fcntl` optional; lock skipped | Same class. `apps/compliance/core.py:461-471` already has an `msvcrt` branch; `apps/document-editor/locking.py` is a portable `O_EXCL` lock (the template) |
| 7 | `mission/fork_join.py:441-449,555-558` | `os.kill(pid, 0)` as liveness probe | Windows: signal 0 is passed to `TerminateProcess`, so the probe can kill the claimed driver (documented Python behaviour; not run here) |
| 8 | `mission/membrane.py:33,125-153,200-216` | `import resource` at top; `RLIMIT_AS/CPU/NPROC` via `preexec_fn`; `PATH=/usr/bin:/bin` | Windows: import error (simulated). macOS: `RLIMIT_AS` refused, so the code fails closed (`memory_ceiling_enforceable()`; 10 tests skip here) |
| 9 | `mission/device_posture.py:221-236,238-248,282-299` | Verdict floor needs `docker`/`podman` and `containment_supported` (`os.name == "posix"`); `/proc/version` WSL detection | Native Windows: always BLOCKED. Native macOS without Docker: BLOCKED |
| 10 | `permissions.py:488-499` (`_key_file_exposure`) | `st_mode & 0o177` must be 0 | Windows: `os.stat` reports synthetic modes, so a valid key is likely always "too wide" and startup refuses |
| 11 | `permissions_setup.py:60-71` | `os.geteuid()` unguarded; requires exactly `0o600` | Windows: `AttributeError` |
| 12 | `secrets/stores.py:67-72`; `runtime_contracts/secrets_local/store.py:84` | `st_mode & (S_IRWXG|S_IRWXO)` refuses | Windows: likely refuses all secrets |
| 13 | `permissions.py:512-528`, `permissions_setup.py:112-114`, `secrets/bootstrap.py:85`, `mission/gate_notify.py:94-110`, `mission/approval.py:230-233` | `mkdir(0o700)`, `chmod 0o600`, `umask`: effective only on POSIX | Windows: modes are no-ops (inherit parent ACL). `gate_notify` would raise spurious "accessible to other users" warnings |
| 14 | `permissions.py:561-580`, `context.py:94-103`, `secrets/bootstrap.py:96-106`, `app/llm_settings.py:90-95` | `os.replace` over a file that lock-free readers may hold open | Windows: sharing violation (`PermissionError`); no retry. (`document-editor` documents a retry pattern, `docs/document-editor-connector/OS_SUPPORT.md:10`) |
| 15 | `permissions.py:530-540` | `os.open(dir)` + `fsync` | Windows: raises, caught (benign) |
| 16 | `permissions.py:50,543-548`, `sources.py:367`, `permissions_setup.py:61` | `O_NOFOLLOW` via `getattr(os, "O_NOFOLLOW", 0)` | Windows: becomes 0, so symlink refusal degrades to `_refuse_symlink`'s `lstat` check; junctions/reparse points not handled |
| 17 | `secrets/stores.py:60-61`, `mission/uninstall.py:75-78`, `app_kit/route_authz.py:241`, `projects_api.py:1060` | Containment by `startswith` (`uninstall.py:78` has no separator; `route_authz.py:241` hard-codes `/`); `realpath` case behaviour | Windows/macOS case-insensitive volumes: bypass or false denies. Low |
| 18 | `mission/device_posture.py:262,272`, `secrets/keepassxc.py:31`, `mission/pair_real.py:30`, `mission/installer.py:275`, `integrations/libreoffice_app.py:164` | `subprocess.run(..., text=True)` without `encoding` | Windows: locale code page (cp1252) |
| 19 | 152 `open`/`read_text`/`write_text` calls without `encoding` (17 in `agentic_os/`, 117 in `apps/`, 13 in `deploy/`, 5 in `scripts/`). Kernel hits: `permissions.py:398,477`, `permissions_setup.py:102`, `trusted_import.py:30`, `fork_join.py:462`, `gate_notify.py:231`, `revenue/discovery_bridge.py:248,278,414`, `interaction/nemo_eval.py:32,54,77` and others | Locale encoding; text-mode append | Windows: code-page decoding and `\r\n` writes (`context.py:90-92` appends text-mode). The document-editor CI job sets `PYTHONUTF8=1` |
| 20 | Defaults: `permissions.py:633` (`/data/agentic-os/permissions`), `context.py:34`, `cli.py:36`, `control_plane.py:134`, `fleet.py:38` (cwd-relative `.agentic-os`), `permissions_setup.py:88`, `secrets/bootstrap.py:52` (`~/.config/redevops`), `mission/gate_notify.py:61`, `mission/trust.py:23`, `mission/unified_server.py:27` (`tempfile.gettempdir()`) | No config/data/cache split; relative defaults | macOS app launched from Finder has cwd `/`; Windows uses `~/.config` for most paths (only secrets honours `%APPDATA%`, `secrets/bootstrap.py:48-51`) |
| 21 | `registry.py:10`, `control_plane.py:163`, `fleet.py:88`, `mission/runtime_meta.py:48` | `Path(__file__).parent.parent` reaches outside the package | Not present in the wheel or a frozen bundle; the catalog/Fleet fail |
| 22 | `mission/runtime_meta.py:49-51` | `git rev-parse` at runtime | Silent `None` if git is absent |
| 23 | `secrets/bootstrap.py:122-131,344-351` | Writes a managed block into `~/.zshrc` / `~/.bash_profile` / `~/.bashrc` on first `ensure_secrets()` (called from `projects_api.main`, `projects_api.py:1960`) | Unwanted side effect for a user-mode native app |
| 24 | `mission/gate_notify.py:100-111` | `sqlite3.connect` with no `close()` in the class | Windows: lock on db/-wal/-shm blocks deletion |
| 25 | `mission/pair_real.py:70,80,104` | `sudo apt`, `sh -c "curl ... | sh"` | Linux-only installer; never reachable natively |
| 26 | `deploy/egress/proxy.py:311`, `deploy/launcher/selftest/run.py:64-90` | `SIGHUP`, `killpg`, `start_new_session`, `shell=True` | Linux/macOS test harness and deployment tool only |

Negative findings (checked, nothing found in `agentic_os/`): `os.fork`, `signal.SIGKILL/SIGTERM`, `/proc` beyond `device_posture`, `shell=True`, `bash`/coreutils invocations (the `apps/` seed scripts and `apps/compliance/seed.py:47` call `bash`/`ssh`; out of core), `symlink()` creation, `chown`, `pwd`/`grp`, hard-coded `/tmp`. Executable bits: 20 tracked files, all shell scripts plus `scripts/fetch_projects_ui.py` and `apps/compliance/seed.py`; no symlinks tracked; no case-colliding or reserved Windows names; no path over 150 characters; no non-ASCII filenames. There is no `.gitattributes`: a Windows checkout with `autocrlf` rewrites line endings of byte-exact fixtures (`conformance/vectors/*.json`, `agentic_os/identifier_ranges.json`) and `.sh` scripts.

## 6. Local attack surface and permissions

### Listeners and IPC

| Listener | Default bind | Authentication | Evidence |
|---|---|---|---|
| Projects API + UI (the frozen sidecar's `serve`) | `127.0.0.1:8787` (`PROJECTS_HOST/PORT`) | None in the default `development` profile. Write routes carry policies (`projects_route_policy.py`), but `route_authz` enforces them only when `is_production()` (`route_authz.py:265-281`). Anonymous callers are keyed by socket peer (`projects_api.py:129-143`). No Host/Origin validation, no CSRF token; CORS same-origin unless `PROJECTS_CORS_ORIGINS` (`projects_api.py:1515-1521`). Data is the sample provider (`basis: sample`, `simulated_actions: true`) unless live producers are registered (`select_provider`, `:1488-1496`). | `projects_api.py:1954-1966`; verified with a test client: foreign `Host` -> 200 on reads |
| Optional agent-gateway mount | same app | Static bearer `AGENT_GATEWAY_TOKEN`, constant-time compare, read-only principal; audit in memory unless `AGENT_GATEWAY_AUDIT_PATH` | `projects_api.py:1366-1403` |
| Control plane | `0.0.0.0:8080` in Docker (`Dockerfile`, `docker-compose.yml`); `127.0.0.1:8080` via `make run` | Mutating routes fail closed (503) without `AGENTIC_OS_CONTROL_TOKEN` or API key; bearer constant-time (`mission/control_auth.py:35-47,162-190`); read-only dashboard token; reads gated by default (`:148-150`) | `control_plane.py`; token is an environment variable |
| Mission unified server | `127.0.0.1:8090` (`UNIFIED_HOST/PORT`) | POST routes need control token; `POST /api/sidekick` is in the pinned unauthenticated baseline (`audit/kernel_unauthenticated_routes.json`) | `mission/unified_server.py:10,205-210` |
| Intelligence demo server | `127.0.0.1:8814`, refuses non-loopback unless flagged | Explicitly unauthenticated demo | `intelligence/demo_server.py:9-12,347-378` |
| Suite server | `0.0.0.0:8300` | App operator tokens | `app_kit/suite_server.py:87,108` |
| Apps and `deploy/module_service.py` | `0.0.0.0:<port>` in containers | Operator `/invoke` gate: control token or tenant token, `X-Control-Token`; approval via signed tokens | `apps/*/app.py` (for example `apps/billing/app.py:525`) |
| Chat-channel gateway thread | outbound polling; no listener | sender allow-list; `AGENTIC_OS_GATEWAY_OPEN=1` disables | `channels.py:291-333` |
| Launcher (Tauri) | no listener; spawns the sidecar | Token held in the OS credential store; bearer attached only to the configured origin; redirects never followed | `deploy/launcher/DESKTOP.md`, `src-tauri/src/secret.rs` |

No named pipes, Unix sockets or other IPC exist in agentic-os. Callers authenticate with bearer tokens over HTTP only. The launcher passes no token to the sidecar and the sidecar requires none, so any local process or user can talk to port 8787; the launcher trusts whatever answers on that port (`main.rs:29`; INFERRED: a local process could squat the port).

### Files, keys and secrets

| Store | Format and protection | Locking | Concerns |
|---|---|---|---|
| Grants (`grants.enc`) | AES-GCM, key from `PERMISSIONS_KEY` or `0600` key file; rollback counter sidecar; fail-closed on tamper; symlink refusal | `flock` transaction; lock-free readers + atomic replace | POSIX-bound (section 5 rows 1, 2, 10, 11, 14) |
| Mission events (JSONL) | sha256 hash chain per mission; per-append `fsync` (`AGENTIC_OS_EVENT_FSYNC`, default on) | `flock` on the log itself; torn-write trim | no lock without `fcntl` (row 3). No file permission set at creation |
| Approvals log (`approvals.jsonl`) | plaintext JSONL; not hash-chained | **none**: `_next_id` counts lines (`context.py:87-88`), `resolve` is read-modify-rewrite (`:67-103`) | races on ids and lost updates under any concurrency |
| Budgets / authority / approval replay / reconcile / ownership | SQLite WAL, `BEGIN IMMEDIATE` (`authority.py:419-470`, `approval.py:208-250`, `ownership.py:76-87`, `children.py:106-116`, `reconcile_jobs.py:79-87`) | SQLite file locking | Portable on local disks; avoid synced or network folders. `Router.spent_usd` is memory-only (`router.py:62,96-98`) |
| Approval signing secret | `AGENTIC_OS_APPROVAL_SECRET` env, else the secrets file (`approval.py:38-52`); replay guard is per-process unless `AGENTIC_OS_REPLAY_DB` | n/a | Secrets file is a dotenv at `~/.config/redevops/secrets.env` or `%APPDATA%\redevops\secrets.env` (`bootstrap.py:45-53`); on Windows also persisted to user environment variables (`:335-339,414-418`) |
| `EncryptedFileSecretStore` / `KeyringSecretStore` / Vault | AES-GCM files 0600 (`stores.py:31-120`); keychain store (`:123-176`) | n/a | `put` truncates in place (`:100`, not atomic). `keyring` undeclared and absent from the frozen set, so `_default_keyring()` returns `None` there (`bootstrap.py:219-230`) |
| Gate-notify outbox | SQLite, `umask 077` | SQLite | section 5 rows 13, 24 |

Secrets in logs and receipts: log statements name paths and counts, never values (spot-checked `permissions.py`, `secrets/bootstrap.py`); gateway audit events carry no arguments or outputs (`agent_gateway/audit_store.py:10-12`); egress redaction in `agent_gateway/egress.py`. Child-process environments: only the membrane scrubs (`membrane.py:200-206`, allowlist `env_passthrough`); the other 11 kernel `subprocess` calls inherit everything, and `ensure_secrets()` exports the managed secrets into `os.environ` (`bootstrap.py:453-457`), so they reach children such as `git`, `docker compose`, `keepassxc-cli`. No kernel subprocess call uses `shell=True`; `fleet.py:91,97` have no timeout.

## 7. Audit-chain and replay portability requirements

Three different canonicalisations exist and each must be preserved byte-for-byte:

| Use | Rule | Reference |
|---|---|---|
| Mission event chain | `sha256(prev_ascii + "|" + json.dumps({seq,ts,type,mission_id,payload}, sort_keys=True, separators=(",",":"), ensure_ascii=False, default=str).encode("utf-8"))`; the stored line uses default `ensure_ascii=True`; `ts` is a Python float (`time.time()`); numbers hashed as Python text | `mission/store.py:69-76,171-210`, vectors `conformance/vectors/chain.json` |
| Approval inputs digest | canonical JSON, `ensure_ascii=True`, sorted keys, runtime-meta keys stripped | `mission/approval.py`, `inputs_digest.json`; `agent_gateway/idempotency.py:78` (also NFC-normalises) |
| Cross-runtime content hash | `rcv1`: NFC strings, keys sorted, nulls dropped, floats refused (decimal strings only) | `runtime_contracts/canonical.py` |

Requirements for native ports:

1. **Byte-exact files.** Open logs in binary (`store.py` already does) and force LF: add `.gitattributes` (`eol=lf`) for vectors and fixtures, and set `newline="\n"` for any text-mode writer (`context.py:91`, and `runtime_contracts/store/file.py` `FileEvidenceStore._persist`, which appends in text mode with no lock or fsync).
2. **Number text.** The chain hashes the repr of floats (`1.0`, `1e+16`). Keep CPython; any other reader must preserve number text (the vectors README already states this).
3. **No platform-dependent `default=str`.** `_canonical_entry` stringifies unknown objects, so a `Path` in a payload would hash with `\` on Windows. Payloads pass through `to_jsonable` (`types.py:597-607`) which does not convert `Path`; reject or posix-normalise paths before append.
4. **Unicode.** The chain does not NFC-normalise. Path-bearing payloads recorded on one OS may compare unequal to the "same" path elsewhere. Normalise at the PlatformServices boundary (NFC, forward slashes) before recording.
5. **Timestamps.** Epoch floats (UTC) are portable. Human-readable `time.strftime("%a ...")` in `today.py:341,611` is locale-dependent; it is not part of a hash.
6. **Single-writer discipline.** Chain head and `seq` come from the file under an inter-process lock (`store.py:171-203`). Without a lock, concurrent processes fork the chain. Native design: the sidecar owns the log and is the only writer; additionally enforce a lock and fail closed in the production profile.
7. **Durability.** `fsync` per append (default) and directory fsync after atomic replace (`permissions.py:530-540`); on Windows directory fsync is a no-op (acceptable) but `os.replace` needs sharing-violation retry.
8. **Truncation.** The end of a log cannot be verified from the log alone; anchor `ledger_head` outside the writer's reach (stated in `verify_chain`'s docstring, `store.py:88`). Keep this property when the log moves into `data_dir`.
9. **Replay vectors as the portability gate.** `conformance/gen_vectors.py --check` (run in the kernel suite, passing here on macOS) and the five vector files are the cross-OS acceptance test for NAT-09.

## 8. PlatformServices seam proposal

Existing dependency-injection points to build on: `Bootstrap(environ=, runner=, keyring=, plat=)` (`secrets/bootstrap.py:233-250`; `_platform()` at `:38`), `KeyringSecretStore(backend=)` (`secrets/stores.py:126`), `bootstrap(prober=)` (`mission/bootstrap.py:112-117`), `GrantStore(directory)`, `EventStore(path)`, the `Sandbox` protocol (`mission/executor.py:65`) with `LocalContainmentSandbox`, `EventStoreProtocol` (`store.py:45`).

| Service | Interface (proposed) | Call sites that would call it today | Windows | macOS | Linux |
|---|---|---|---|---|---|
| `config_dir/data_dir/cache_dir/state_dir` | `paths.config()`, `.data()`, `.cache()`, `.state()`, `.runtime()` | `permissions.py:633`; `permissions_setup.py:88`; `secrets/bootstrap.py:45-53`; `context.py:34`; `cli.py:36`; `control_plane.py:134`; `fleet.py:38`; `mission/gate_notify.py:61`; `mission/trust.py:23`; `mission/unified_server.py:27`; `registry.py:10`, `control_plane.py:163` (package data) | `%APPDATA%` (config), `%LOCALAPPDATA%` (data, cache) | `~/Library/Application Support`, `~/Library/Caches` | existing XDG/`~/.config` behaviour preserved |
| `file_store` (private file/dir, atomic replace, no-follow read, dir fsync) | `ensure_private_dir`, `write_atomic(path, data, private=True)`, `read_regular`, `is_private(path) -> Result`, `fsync_dir` | `permissions.py:466-580`; `permissions_setup.py:60-114`; `secrets/stores.py:67-105`; `context.py:94-103`; `secrets/bootstrap.py:80-110`; `app/llm_settings.py:90-95`; `mission/gate_notify.py:94-110`; `mission/approval.py:230-233`; `runtime_contracts/secrets_local/store.py:84-107` | ACL-based `is_private` (owner-only DACL via `GetNamedSecurityInfo`/`icacls`), `os.replace` with sharing retry, junction refusal | POSIX mode checks; keep `O_NOFOLLOW` | existing code, unchanged |
| `file_lock` | `with locks.exclusive(path, timeout) as h:` | `permissions.py:692-722`; `mission/store.py:176-181`; `mission/fork_join.py:451-470`; `outcome_store.py:97-121`; `apps/agentic-privacy/core.py:125-133`; (templates: `apps/compliance/core.py:461-471`, `apps/document-editor/locking.py`) | `LockFileEx`/`msvcrt.locking` on a sidecar file, or the `O_EXCL` lock-file design | `flock` | `flock` |
| `credential_store` | `get/put/delete(service, account)`, `available()` | `secrets/stores.py:123-176`; `secrets/bootstrap.py:219-230,355-362`; `permissions.py:338-440` (`_SecretBox` key source); `mission/approval.py:38-52` (approval secret); `secrets/stores.py:44-56` (`REDEVOPS_SECRET_MASTER_KEY`); launcher `src-tauri/src/secret.rs` | Credential Manager / DPAPI; stop writing user environment variables (`bootstrap.py:335-339,414-418`) | Keychain (the launcher already uses it); stop editing shell rc files (`bootstrap.py:344-351`) | Secret Service or existing files |
| `process_runner` | `run(argv, env_allowlist, timeout, cwd, limits)`, `start(...) -> Handle(tree_kill, alive)`, `alive(pid)` | `mission/membrane.py:133,209`; `fleet.py:91,97`; `mission/installer.py:270-275`; `mission/device_posture.py:262,272`; `secrets/keepassxc.py:31`; `mission/pair_real.py:30`; `mission/runtime_meta.py:49`; `integrations/libreoffice_app.py:164`; `secrets/bootstrap.py:216`; `mission/fork_join.py:441-449`; launcher `src-tauri/src/main.rs:312,322,591` | Job Object (kill-on-close, process/memory limits), `CREATE_NO_WINDOW`, `OpenProcess`-based `alive` | process group + `killpg`, `RLIMIT_*` where honoured | existing `preexec_fn` path |
| `local_transport` | `listen(app) -> Endpoint(address, credential)`, peer authentication | `projects_api.py:1954-1966`; `mission/unified_server.py:205-210`; `intelligence/demo_server.py:365-378`; `control_plane.py` + `control_auth.py:162-190`; `main.rs:29` | Authenticated loopback (random per-launch token, Host allowlist) or named pipe with ACL | Authenticated loopback or Unix socket with peer credentials | loopback / existing |
| `sandbox_backend` | `capabilities() -> honest set`, `run_contained(...)` | `mission/membrane.py:157-260`; `mission/execution_backend.py:24-76`; `agent_gateway/sandbox.py:37`; `runtime_contracts/models/execution_backend.py` | Job Object backend; declare no filesystem/network isolation; deny high-risk classes | timeout + env scrub; memory ceiling declared unenforceable | existing rlimit membrane |
| `host_capabilities` | `probe() -> DeviceFacts` including `native_isolation` | `mission/device_posture.py:175-236,238-318` (`derive`, `_probe_platform` WSL detection, `_probe_containment_supported`), `mission/inference_posture.py`, `mission/bootstrap.py:112-117` | new posture class that does not require a container runtime and denies the classes it cannot enforce | same | unchanged |

Rule: any capability a backend cannot enforce must make the corresponding high-risk action fail closed (the posture/`capabilities()` pair is the place), consistent with the plan's "neither Job Objects nor launchd alone is a security sandbox".

## 9. Test results on macOS

Environment: macOS 27.0.1, arm64, CPython 3.12.16 (Homebrew), throwaway venv from `uv sync --frozen --extra dev --extra duckdb --extra mcp --extra projects` (uv installed in a separate throwaway venv), `HOME` redirected to a scratch directory, `APPROVAL_SECRET`/`PERMISSIONS_KEY`/`AGENTIC_OS_CONTROL_TOKEN` unset. `PYTHONPATH=<repo>:<repo>/apps`. Connector fixtures from `redevops-connectors` (public, shallow clone `eed11f97`). `git status` stayed clean.

Test inventory: `tests/` 472 `test_*.py` files with 4,758 `def test_` (6,778 items collected and run after parametrisation); `apps/` 149 files, 1,286 `def test_`.

| Suite | Passed | Failed | Skipped | xfailed | Time |
|---|---|---|---|---|---|
| Kernel `tests/` (first run) | 6,675 | 5 | 94 | 4 | 171 s |
| Same, after fetching the pinned UI bundle and re-running the 4 affected files | 6,679 | 1 | 94 | 4 | n/a |
| Per-app suites (19 apps with tests, run per app as CI does) | 2,257 | 0 | 11 (+1 app, `video-ad`, fully skipped) | n/a | about 70 s total |

Failure causes:
- 4 failures (`test_hygiene_ui_untouched`, `test_kernel_residuals::test_same_origin_ui_and_api_still_work`, two in `test_projects_api`): 404 for `/` because the Projects UI bundle was absent; this was a run-setup omission (I set `AGENTIC_OS_SKIP_UI_FETCH=1`). After `scripts/fetch_projects_ui.py` (sha256-pinned release asset `projects-ui-root.tgz`) all four pass.
- 1 failure (`test_membrane_import_paths::test_the_childs_path_order_is_standard_library_then_the_parents_paths_then_site_packages`): `ValueError: '/opt/homebrew/opt/python@3.12/...' is not in list`. The test compares `sysconfig`'s stdlib path with the child's `sys.path`; Homebrew exposes the same interpreter through `/opt/homebrew/opt/...` and `/opt/homebrew/Cellar/...`. INFERRED: a symlinked-prefix test artefact, not a product defect. It does show that path equality by string comparison is fragile (section 5 row 17).

Skip causes (94): `agent_harness` not installed 27; `RLIMIT_AS` unenforceable on macOS 10; Docker/compose related 10; app "no gated capability stages via pending_approval" 10; `psycopg` extra not installed 7; Unicode UCD data not provided 5 (+1 Unicode-version); `reel` package for `video-ad` 6; LibreOffice absent 3; Proxmox integration 3; live-service credentials (Twenty, ERPNext, Umami, Lago, Chatwoot, live sentinel) about 10; one "only meaningful on Linux"; one edition-specific package absent. Per-app: document-editor 10 (OnlyOffice integration 8, hardlinks unavailable 1, APFS case-fold 1).

Platform sensitivity: only 2 test files branch on platform (`tests/test_secrets_bootstrap.py:14` posix-only marker; `tests/_platform_facts.py`). 40 files touch `fcntl`/`geteuid`/mode bits/symlinks. The merge-gate CI runs Ubuntu only (`tests.yml`). There is no Windows run of the kernel suite today; the only Windows evidence is the document-editor connector job and the launcher's Rust tests.

## 10. Packaging and signing findings

How `packaging/` works today (`packaging/README.md`, `BUILD-hosts.md`):
1. `build-wheelhouse.sh`: `pip wheel` the git-pinned deps (derived from `pyproject.toml`), rewrite direct URLs to plain names in a copy of the project, build all wheels for extras `projects,duckdb`, prove a `--no-index` install, `pip freeze` into `packaging/requirements.lock`.
2. `build-sidecar.sh`: install the wheelhouse into a venv, install PyInstaller from the index (unpinned `>=6.0`), freeze `sidecar_main.py` with `redevops-sidecar.spec`.
3. `stage-sidecar.sh`: copy as a per-target-triple Tauri `externalBin`.
4. `cargo tauri build`: `.deb`/AppImage, `.msi`/NSIS, `.dmg`/`.app` (`tauri.conf.json` targets).

`packaging/build-windows.ps1` is a PowerShell mirror, "has not been executed here" per its own header, with stale pins (`runtime-contracts@v0.3.4`, `redevops-connectors@main`, `:330-333`).

`hatch_build.py`: a Hatch build hook that runs on wheel and editable builds. It imports `scripts/fetch_projects_ui.py` and downloads the pinned UI asset from a GitHub release (sha256 verified before extraction, member paths validated, `tarfile` `filter="data"`) into the git-ignored `agentic_os/projects_ui/`. `AGENTIC_OS_SKIP_UI_FETCH=1` skips it (the Dockerfile does). Consequences: building needs network access to GitHub; a build with the skip flag produces a wheel without the UI; no native code is built.

Findings:
- **Wheel not publishable.** `Requires-Dist: runtime-contracts @ git+https://...` is in the wheel metadata (verified); PyPI rejects direct references; none of the five internal distributions is on PyPI (queried). `[tool.hatch.metadata] allow-direct-references = true` is required to build at all.
- **Distribution-name check needed.** One name finding is recorded privately with the maintainers.
- **Wheel contents.** `modules.yaml` and `deploy/assets` are not packaged (`packages = ["agentic_os"]`), so `Registry.load()`, `Fleet` and the control-plane assets are broken outside a source checkout. `identifier_ranges.json` and the capability YAMLs are packaged.
- **Reproducibility.** `uv.lock` is the repo's lock; the sidecar is built from an unhashed `pip freeze` that is already stale (24 lines; `agentic-os==0.2.4`, `runtime-contracts==0.3.4`, no discovery-runtime or knowledge-frontier) and from unpinned PyInstaller. No `--require-hashes`.
- **PyInstaller one-file** (`EXE(pyz, a.scripts, a.binaries, a.datas, ...)`, `redevops-sidecar.spec:160-166`): extracts to a temp directory on every start, bundles a bootloader plus embedded native libs. `codesign_identity=None`, `entitlements_file=None`, `console=True`. INFERRED (industry behaviour, not tested here): antivirus/SmartScreen false positives for unsigned one-file bootloaders on Windows; on macOS every embedded `.so`/`.dylib` needs Developer ID signing with hardened-runtime entitlements for the Python runtime (JIT/library validation) before notarization; a one-dir layout inside the app bundle is the safer shape. The launcher's `child.kill()` (`main.rs:322,591`) would stop only the bootloader process (INFERRED), leaving the Python child; a Job Object is needed. Spawning without `CREATE_NO_WINDOW` shows a console window on Windows (INFERRED).
- **Signing status.** No signing step exists; the workflow comments mark it "staged" (`native-packaging.yml`); `DESKTOP.md` states no signed build exists and keychain-prompt behaviour for signed builds is untested. Release notes tell users to bypass Gatekeeper/SmartScreen.
- **Supply chain.** CI has gitleaks only; no SBOM, provenance/attestation, cosign, dependency or vulnerability scanning, or Dependabot. No third-party notices or licence bundle (only the three licence files in the wheel). `cargo deny`/crate licence review not done.
- **Licensing.** Commons Clause forbids selling the software, including hosting or support whose value derives from it (`LICENSE.md`); AGPL section 6 obliges an offer of corresponding source with any installer. Licence strings differ across the tree (the root uses the combined licence; `apps/*/LICENSE` plain AGPL; `knowledge-frontier` and `redevops-connectors` plain AGPL). Needs counsel review before store distribution (open question).
- **Docker is not required by the sidecar**, but `deploy/launcher/README.md` and `ACCEPTANCE.md` still describe a WSL2/Docker path, and the posture check (section 5 row 9) conflicts with a Docker-free claim.
- **CI gaps.** `native-packaging.yml` has not run since 2026-09-18, so current main has never been built into installers by CI; the Windows leg has no launch test; macOS has no signing runner.

## 11. Risk-ranked change list

Severity: blocker / high / medium / low. Effort in engineer-days is a **guess**. NAT column: target workstream.

| # | Sev | NAT | Location | Change and approach | Effort (guess) |
|---|---|---|---|---|---|
| 1 | blocker | 01/02 | `permissions.py:46-48,633-636,692-722`, `control_plane.py:610` | Replace `fcntl` with `file_lock` from PlatformServices; keep fail-closed on lock failure; make `control_plane` construct the store lazily | 3-4 |
| 2 | blocker | 01/06 | `mission/store.py:176-181`, `fork_join.py:451-470`, `outcome_store.py:97-121`, `apps/agentic-privacy/core.py:125` | Same lock service; in production profile refuse to append without a lock; sidecar as single writer; two-process chain test on all OSes | 3-4 |
| 3 | blocker | 06 | `projects_api.py:1954-1966`, `route_authz.py:265-281`, `profile.py:28-43`, `main.rs:29,312` | Authenticate every caller of the sidecar: per-launch random token delivered over a private channel, Host allowlist, Origin/CSRF check, production-profile defaults for the sidecar, ephemeral port plus handshake file in `runtime_dir`; later named pipe / Unix socket | 5-8 |
| 4 | high | 01/02 | `permissions.py:488-499`, `permissions_setup.py:60-71`, `secrets/stores.py:67-72`, `runtime_contracts/secrets_local/store.py:84` | `file_store.is_private()` with ACL check on Windows; fix unguarded `geteuid` | 3-5 |
| 5 | high | 05/02 | `mission/fork_join.py:441-449` | Replace `os.kill(pid, 0)` with `process_runner.alive()` (Windows `OpenProcess`+`GetExitCodeProcess`) | 0.5-1 |
| 6 | high | 05 | `mission/membrane.py:33,125-153`; `execution_backend.py:24` | Lazy `resource` import; `sandbox_backend` seam; Windows Job Object backend; honest capability declaration | 5-8 |
| 7 | high | 02 | `mission/device_posture.py:175-236,291-299` | Add a native posture class not requiring a container runtime; deny unenforceable classes; keep Docker as an optional tier | 2-3 |
| 8 | high | 05 | all kernel `subprocess` calls (section 6) and `bootstrap.py:453-457` | Route through `process_runner` with env allowlist, timeouts, tree kill; stop exporting managed secrets into `os.environ` | 3-4 |
| 9 | high | 02 | `secrets/bootstrap.py:335-339,344-351,414-418,445-462`; `secrets/stores.py:123`; `pyproject.toml` | Declare `keyring`; use the native credential store as source of truth; remove shell-profile edits and user-environment persistence from the native path; wire into the frozen set | 3-5 |
| 10 | high | 08 | `pyproject.toml:12-27`, `packaging/*` | Publish internal distributions to a registry (or vendor) so wheels have no git references; resolve the name finding (recorded privately); one lock (`uv.lock`) with hashes used by the sidecar build | 4-6 |
| 11 | high | 08 | `redevops-sidecar.spec:160-166`, `native-packaging.yml` | One-dir bundle; Developer ID signing + hardened runtime + notarization (macOS); Authenticode signing (Windows); SBOM + provenance + vulnerability scan; pin PyInstaller | 8-12 (plus certificate lead time) |
| 12 | high | 01 | `permissions.py:561-580`, `context.py:94-103`, `secrets/bootstrap.py:96-106` | Replace with `write_atomic` that retries on Windows sharing violations | 1-2 |
| 13 | medium | 01 | 152 text I/O sites (section 5 row 19), `subprocess` `text=True` | Explicit `encoding="utf-8"`, `newline="\n"`; lint rule; set `PYTHONUTF8=1` in the sidecar | 2 |
| 14 | medium | 01 | `context.py:34-103` | Move approvals log to SQLite or the event store; remove id-by-line-count | 2-3 |
| 15 | medium | 01 | missing `.gitattributes` | `* text=auto eol=lf` plus binary markers; CI check on a Windows runner | 0.5 |
| 16 | medium | 02 | defaults listed in section 5 row 20 | `paths` service; no cwd-relative defaults; migrate `~/.config/redevops` on macOS to Application Support (read old location) | 2-3 |
| 17 | medium | 08 | `registry.py:10`, `control_plane.py:163`, `fleet.py:88` | Package data or `importlib.resources`; or drop the catalog from the native core | 1-2 |
| 18 | medium | 05 | `trusted_import.py:99-132`, `app_gate.py:169-250` | Define the trusted-package rule for a frozen bundle; test the engine hook in the sidecar | 2-3 |
| 19 | medium | 06 | `approval.py:181-250` | Default `AGENTIC_OS_REPLAY_DB` into `data_dir` for the sidecar; refuse non-durable replay in production | 1 |
| 20 | medium | 01 | `FileEvidenceStore` (runtime-contracts), `gate_notify.py:100-111` | Locking/fsync/close; coordinate with the contracts repo | 1-2 |
| 21 | medium | 01 | `mission/store.py:69-76` / `types.py:597` | Reject or normalise `Path`/non-JSON objects before append; NFC + posix path normalisation at the seam | 1-2 |
| 22 | medium | 09 | `tests.yml` | Add Windows and macOS runners for core tests, vectors, two-process chain and lock tests (the document-editor job is the template) | 3-4 |
| 23 | low | 01 | path-prefix checks (section 5 row 17) | `commonpath`/`os.path.normcase` containment helper | 0.5 |
| 24 | low | 08 | repo-wide | Third-party notices and corresponding-source offer in installers; `cargo deny` | 2 |
| 25 | low | 06 | `projects_api` static files | Pin MIME types for `.js` (Windows registry-driven `mimetypes`, INFERRED) | 0.5 |
| 26 | low | 01 | `secrets/stores.py:100` | Atomic write for `EncryptedFileSecretStore.put` | 0.5 |

## 12. Unsupported-features register

Features that must be marked unsupported (or replaced) on native Windows/macOS until the item is done. "Fails closed" means the code refuses rather than degrading.

| # | Feature | Windows | macOS arm64 | Behaviour today |
|---|---|---|---|---|
| 1 | Permissions plane (`GrantStore`) | Unsupported | Supported | Windows: raises at construction (documented) |
| 2 | Inter-process chain integrity (audit log, fork/join, outcome store) | Unsupported | Supported | Windows: silent no-lock (store, outcome) or import error (fork/join) |
| 3 | Fork/join driver liveness | Unsupported (and unsafe) | Supported | Windows: `os.kill(pid, 0)` terminates |
| 4 | Memory-ceiling containment (`RLIMIT_AS`) | Unsupported | Unsupported | Fails closed by name on macOS; import error on Windows |
| 5 | CPU/process-count rlimit containment | Unsupported | Partial (`RLIMIT_CPU`; `NPROC` best-effort) | via `preexec_fn` |
| 6 | Device posture verdict without a container runtime | Unsupported | Unsupported | BLOCKED |
| 7 | Docker/Compose app fleet (`Fleet`, installer, module catalog from a wheel) | Requires Docker/WSL | Requires Docker | Out of the native core (apps stay remote integrations) |
| 8 | Local-model installer (`pair_real`: `apt`, `curl \| sh`) | Unsupported | Unsupported | Linux only |
| 9 | Secret files with mode checks (key file, local profile, `EncryptedFileSecretStore`, `FileSecretStore`) | Refuses all (INFERRED) | Supported | POSIX modes |
| 10 | Shell-profile secret loading | Not applicable (writes user environment instead) | Edits `~/.zshrc` | Undesirable side effect |
| 11 | pip-wheel install without git/network | Unsupported | Unsupported | git direct references |
| 12 | Strong isolation claims (network/filesystem/process) | None | None | The membrane documents filesystem containment only (`execution_backend.py:7`) |

## 13. Open questions

1. **Core contents.** Is the core product the 74-module governance closure behind a new authenticated sidecar, or the existing Projects API (`sample` provider) plus launcher? The existing sidecar delivers the latter; the governance modules are libraries without a service.
2. **Which profile is the native default?** Making `production` the sidecar default changes startup requirements (`AGENTIC_OS_AUTHORITY_DB`, durable guard, `profile.py:60-70`).
3. **macOS memory ceiling.** Accept "no memory ceiling on macOS" (fail closed) or add another mechanism?
4. **Windows file permissions.** ACL verification (pywin32/ctypes) versus DPAPI-wrapped keys that make file ACLs secondary.
5. **Commons Clause and store distribution.** Legal reading for signed installers, Microsoft Store, Homebrew casks, and the "may not sell" condition.
6. **Python versions.** Pin the native bundle to 3.12 only, or also test 3.11 (`requires-python`) and 3.13?
7. **Windows ARM64 / macOS Intel.** The lock resolves universal wheels; none were tested. `native-packaging.yml` already dropped Intel macOS.
8. **runtime-contracts local stores** (`secrets_local`, `FileEvidenceStore`) carry the same POSIX assumptions (section 5 rows 12, 19); are they fixed there or shadowed here?
9. **Reproducible UI bundle.** The pinned UI asset predates the Today view (`DESKTOP.md` "Release order"); who owns the release order for a native build?
10. **Private findings.** One local-surface finding and one distribution-name finding are recorded privately with the maintainers.

## Appendix: evidence kept

Evidence under `<scratch>/nat/tmp/`: `lockrows.json` (lock parse), `pypi_res.json` (138 PyPI wheel queries), `ship-req.txt`, `dl-win/` and `dl-mac/` (the 39 downloaded wheels each), `matrix.md`, `pytest-kernel.log`, `junit-kernel.xml`, `pytest-apps.log`, `winsim.py` (import simulation), `impgraph.py` (import closure), `core_top.json`/`core_lazy.json` (the 74 and 117 module lists), `whl/agentic_os-0.4.1-py3-none-any.whl`. The audited clone is `<scratch>/nat/src/agentic-os`; the venv is `<scratch>/nat/venv/aos`.
