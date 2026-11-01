# Native portability matrix: rAIls Core on Windows x86-64 and macOS Apple Silicon (NAT-00)

Date 2026-11-01. Status: audit complete; no implementation has started. This is the first gate of the native Windows and macOS plan: a repository and dependency audit of the actual code, before any estimate is relied on.

**Method and limits.** Four read-only audits ran in parallel against fresh clones of each repository's `main`. They read the code, resolved every dependency against PyPI for both targets, and ran each repository's own offline test suite on macOS arm64 (Python 3.12.16). **No Windows host was used: every Windows statement below is reasoned from the code and from Python and Windows documentation, and is labelled INFERRED in the detailed reports.** Only Python 3.12 was checked. Windows ARM64, macOS Intel and Python 3.13 or later were not assessed. Effort figures in the detailed reports are guesses, not estimates.

## 1. Audited commits and verdicts

| area | commit audited | verdict | one-line reason |
|---|---|---|---|
| [context-runtime](native/context-runtime.md) | `eb27120` (main, 2026-10-31) | go with changes | Pure Python with no POSIX-only modules; work is file handling (encoding, ordering, locations, atomic replace, locking) and the MCP stdio client's process lifecycle. |
| [agent-harness](native/agent-harness.md) | `f3c87b7` (main); tag v0.2.0 = `a58ef9e` | go with changes for the portable parts; **blocked as it stands** for command execution and sandboxing | No process-tree control or cancellation; command policy and path confinement use POSIX string logic; the full environment reaches child processes; no native isolation backend. |
| [agentic-os](native/agentic-os.md) | `40d1729` (main, v0.4.1+3) | go with changes | The governance core is small (74 of 527 modules) and portable apart from POSIX file locking and permission checks; the default sidecar has no caller authentication. |
| [redevops-rag, JVM, QA orchestration](native/qa-rag-jvm.md) | redevops-rag `3a6c253`; redevops-qa `7029820` | rag: portable with small work; JVM: nothing in the audited public repositories; QA: not ready for Windows, partly ready for macOS arm64 | See the gap analysis against the target test contract. |

## 2. Dependencies and wheels

- Every shipped dependency of the three Python repositories has a `win_amd64` and a `macosx_arm64` wheel for CPython 3.12. None needs a compiler or is source-only. The exceptions are optional: the `hipporag` extra of context-runtime cannot be installed as pinned (`vllm==0.6.6.post1` has no distribution for either target), and CUDA-only packages are never installed on Windows or macOS.
- context-runtime: 212 distinct package versions across all extras, 62 with native code; all non-GPU extras resolve together (about 784 MB of wheels on Windows, 773 MB on macOS). The core install is far smaller: 28 packages, about 52 MB (Windows) and 44 MB (macOS) with the test tools. Full matrix: [context-runtime-dependency-matrix.md](native/context-runtime-dependency-matrix.md).
- agentic-os: the sidecar needs five native wheels (cffi, cryptography, duckdb, pydantic-core, PyYAML). All 39 registry packages were downloaded for both platforms and all 138 locked packages were checked against PyPI.
- agent-harness: no runtime dependencies and no native binaries.
- macOS floor for the full optional set is about macOS 14 (current torch, onnxruntime, av and scipy wheels); the core needs far less.
- Distribution blockers common to all three: internal packages are referenced by git URL (PyPI rejects direct references in a published wheel); the PyPI name `agent-harness` belongs to an unrelated project; the other distribution names are unregistered; lock files are stale or unhashed; there is no SBOM, provenance or vulnerability scan in CI, no third-party notices file, and no code signing configuration for the PyInstaller sidecar.

## 3. Test evidence on macOS arm64 (Python 3.12.16)

| repository | result | notes |
|---|---|---|
| context-runtime | 980 passed, 12 skipped, 0 failed | No macOS-specific failures. The "offline" suite downloads the DuckDB `fts` extension and writes state into the working directory. |
| agent-harness | 3521 passed, 23 skipped, 0 failed (main); 3264 passed, 5 skipped (v0.2.0) | All skips are missing optional packages, Docker or datasets; none is an OS skip. The suite never exercises case-insensitive paths. |
| agentic-os kernel | 6679 passed, 1 failed (after fetching the pinned UI bundle) | The one failure is a Homebrew symlinked-prefix path comparison in a membrane test. App suites: 19 apps, 2257 passed, 11 skipped. |
| redevops-rag | 80 passed, 14 skipped | The skips are the Postgres tests, which need a live database. |
| redevops-qa | 359 + 133 + 10 passed (macOS arm64) | Local install-profile run; also passed on Ubuntu 24.04 apart from one test fixed since. |

CI for these repositories runs on Ubuntu only, apart from the document-editor connector's unit tests in agentic-os, which run on Windows. There is no Windows evidence for the core.

## 4. Cross-cutting blockers and the workstreams that own them

| # | finding | where | severity | workstream |
|---|---|---|---|---|
| 1 | No process-tree control or cancellation: only the direct child is killed on timeout; no process group or Job Object | agent-harness `sandbox.py:40-55`; context-runtime MCP client `tools/mcp.py:97-105,219` | blocker | NAT-02, NAT-04 |
| 2 | Governance and state stores use POSIX file locks or no lock at all: the permissions store raises without `fcntl`; the hash-chained event log and outcome store run unlocked; eight context-runtime stores replace files via a fixed temp name with no inter-process lock | agentic-os `permissions.py:46-48,633-636`, `mission/store.py:176-181`, `outcome_store.py:97-121`; context-runtime F-ATOM-1 | blocker / high | NAT-01, NAT-05 |
| 3 | The default sidecar has no caller authentication and no Host or Origin check, and route authorization is enforced only under the production profile (verified with a test client against sample data) | agentic-os `projects_api.py:1960-1966`, `route_authz.py:265-281` | blocker for a native release | NAT-06 |
| 4 | Permission checks use POSIX mode bits and `geteuid`, which would refuse every key and secret file on Windows; secrets bootstrap edits shell profiles; `keyring` is not a declared dependency | agentic-os `permissions.py:488-499`, `secrets/stores.py:67-72` | high | NAT-02 |
| 5 | The full parent environment is passed to child processes; there is no allowlist | agent-harness `sandbox.py:38`; agentic-os (11 of 12 kernel subprocess calls) | high | NAT-05 |
| 6 | Command policy and path confinement compare POSIX strings; case-insensitive volumes (the macOS default) defeat the comparison, and on Windows every command is denied (fails closed but does nothing) | agent-harness `command_policy.py` | high | NAT-04 |
| 7 | No native isolation backend; the only real isolation is Docker; posture checks return BLOCKED without Docker or Podman; the membrane imports `resource` at module top and fails closed on macOS | agent-harness `sandbox.py`; agentic-os `membrane.py:33`, `device_posture.py:221-236` | high | NAT-02, NAT-05 |
| 8 | Cross-OS divergence of deterministic context output: files read with the locale encoding (24 call sites), enumerated in an OS-dependent order, labelled with OS separators | context-runtime F-ENC-1, F-ORD-1 | high | NAT-01, NAT-03 |
| 9 | State locations: hard-coded `/data/...` defaults and working-directory-relative paths; no platform directory logic | context-runtime F-PATH-1..3 | high | NAT-02 |
| 10 | The MCP stdio client has no cancellation, no timeout, an undrained stderr pipe, locale-encoded pipes and no `.cmd` shim handling. The agent-harness repository has no MCP code, so ownership of the MCP work needs a decision | context-runtime `tools/mcp.py`; agent-harness audit | high | NAT-05 |
| 11 | Local listeners without caller authentication: the harness spend proxy accepts any local caller and injects the provider key upstream; the context-runtime control plane authenticates a shared secret, not a caller | agent-harness `spend_proxy.py`; context-runtime `control_plane/security.py` | high | NAT-02, NAT-06 |
| 12 | Runtime-downloaded native code: DuckDB downloads its `fts` extension at first use, so offline BM25 silently drops out of retrieval and the unsigned extension must be bundled and re-signed for notarization | context-runtime F-DUCK-1; redevops-rag `store.py:62` | high | NAT-03, NAT-08 |
| 13 | Distribution: no CI on Windows or macOS for the core; no signing; stale locks; name collisions; git direct references | all | high | NAT-08, NAT-09 |
| 14 | redevops-qa's supervisor is POSIX-only (`os.killpg`, `signal.SIGKILL`, a fixed `PATH`, POSIX deny prefixes) and its verifier has no subject kind for process exit codes or stdout, so native CLI and sidecar-contract engines need new adapters | redevops-qa `supervisor.py`, `paths.py` | high | NAT-09 |

Items that carry security detail are tracked privately with the maintainers and are not described here.

## 5. Minimal native core (proposal from the audit)

The governance core in agentic-os (74 of 527 modules, 16,956 lines) needs only the standard library, `cryptography`, PyYAML and `runtime-contracts`. context-runtime's default install is pure Python plus PyYAML (and pydantic-core for the control plane). agent-harness has no dependencies. With the POSIX modules blocked, 3 of 527 agentic-os modules fail to import (`membrane`, `execution_backend`, `control_plane`) and none is in the core. A native core of: the governance core, context-runtime base, the harness tool layer, and a loopback sidecar with real caller authentication is supportable with the changes in section 4.

## 6. Unsupported on native, and how each fails

The detailed registers are in each report. In summary: strong process isolation (no Windows or macOS backend exists; high-risk actions must fail closed); the `hipporag` extra; GPU acceleration (falls back to CPU by design); DAST and supply-chain scanners (external binaries, not bundled; scope fails closed, tool absence yields a visible failure or a simulated report); Docker-based tests and posture checks; video and audio ingestion without an external ffmpeg (content is dropped silently, which should become a visible failure); the POSIX-only governance safety scan (advisory only). Postgres, Kafka, Neo4j, OpenSearch and cloud integrations remain remote services, not ports.

## 7. What this means for the plan

- The audit **does not confirm or refute the 7 to 14 engineer-week range**. The per-item effort guesses in the reports cover individual repositories and overlap, so they are not summed.
- Three items look like the critical path: the process runner and platform services layer (blocker 1, 4, 7), authenticating the sidecar (blocker 3), and portable locking and atomic storage for the governance stores (blocker 2). Everything else is parallelisable.
- A Windows CI lane and a macOS CI lane do not exist for any of the three Python repositories and are the first thing to add: GitHub-hosted `windows-latest` and `macos-14` runners are available for public repositories.
- No product claim about Windows or macOS support should be made until those lanes run the core suites.

## 8. Decisions needed from the maintainers

1. Which feature set is the native core (see section 5)? That decides how much of the DuckDB extension and wheel-weight findings matter.
2. How is the sidecar authenticated: a per-launch token plus Host and Origin checks first, or peer-credential identity over a Unix socket and named pipe from the first release?
3. Which repository owns the MCP client and server work?
4. Packaging of `agent-harness` and of the other internal distributions: rename, vendor, or reserve the names on PyPI before any public install instruction.
5. Cross-OS determinism: which outputs must be bit-identical (chunk ids, order, labels, scores), and is a tolerance acceptable for floating-point scores?
6. Are the DAST, scanner, Docker-based and `hipporag` paths declared unsupported on native, or do they get replacements?
7. Licence wording for bundles and stores: how the Commons Clause interacts with AGPL section 7, and whether the plain AGPL on `agent-harness` is intended. Counsel should review store submissions.
