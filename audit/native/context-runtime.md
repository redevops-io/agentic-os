# context-runtime: native portability audit (Windows x86-64, macOS Apple Silicon)

Audit date 2026-11-01. Scope: public `context-runtime`, plus the parts of `runtime-contracts` and `agent-harness` it imports. Method: read-only static review, PyPI wheel resolution for both targets, and the repository's own offline test suite on macOS arm64. No Windows host was available: every Windows statement is reasoned from code and documentation and labelled INFERRED. Nothing here is a measured Windows result.

## 1. Summary

**Verdict: go-with-changes** for the core library and a loopback control-plane sidecar. **Blocked as shipped** for four optional features: the `hipporag` extra, GPU acceleration, the DAST/scanner stack, and the Docker-based tests.

- The default install is pure Python plus small native chains (PyYAML; pydantic-core for the control plane).
- All non-GPU extras resolve together to binary wheels for both targets.
- The package has no `fork`, `multiprocessing`, `fcntl`, `resource`, `signal`, `/proc` or `shell=True` use.
- The work is concentrated in file handling (encoding, ordering, locations, atomic replace, locking) and in one process-lifecycle component (the MCP stdio client).

Top five risks:

1. Cross-OS divergence of deterministic context output (high): F-ENC-1, F-ORD-1.
2. No platform abstraction for state locations (high): F-PATH-1..4.
3. Unsynchronised rename-based persistence (high): F-ATOM-1.
4. MCP stdio client lifecycle (high): F-PROC-1..5.
5. Distribution blockers (high): D-1..D-5 (see sections 3, 7 and 9).

## 2. Repository facts

| item | value | evidence |
|---|---|---|
| repository | `github.com/redevops-io/context-runtime` (public) | `gh api` visibility |
| commit audited | `eb2712020c8ddb47ddf0cf0b0989e9bc1df4aa53` (origin/main, 2026-10-31) | `git rev-parse HEAD` in a fresh clone |
| version | 0.4.1 | `pyproject.toml:3` |
| Python | `>=3.12` only; CI and container use 3.12 | `pyproject.toml:6`, `.github/workflows/tests.yml`, `Dockerfile:8` |
| CI | four workflows, all `ubuntu-latest`; no Windows or macOS job | `.github/workflows/*.yml` |
| build | hatchling; the built wheel is `py3-none-any`, 195 files, no native code | `pip wheel --no-deps` |
| licence | AGPL-3.0-or-later with the Commons Clause (family licence) | `pyproject.toml:7`, `LICENSE`, `LICENSE-COMMONS-CLAUSE`, `LICENSE.md` |
| size | 184 modules / 25,791 lines in `context_runtime/`, 27 sub-packages; 103 test modules / 10,955 lines; 42,071 Python lines repo-wide | `wc -l` |
| tracked native binaries | none; two `.mojo` benchmark sources and 7 executable scripts under `deploy/` | `git ls-files`, mode 100755 |
| case collisions, reserved names, long paths, symlinks in tree | none | `git ls-tree` scan |

**Sub-packages:** accel, adapters, compression, constraints, control_plane, costmodel, execution, geospatial, ingest, integrations, learning, observability, optimizer, plancache, planner, plugins, policy, providers, reasoner, registry, retrieval, runtime, scheduler, scripts, sources, tools, verification.

**Entry points**

- `context-runtime` console script, `context_runtime.runtime.cli:main`, with `run`, `explain` and `simulate` (no `serve`).
- `context_runtime.control_plane.app:app`: FastAPI ASGI app, 34 routes, started only through the `uvicorn` CLI. `Dockerfile:28` binds `0.0.0.0:8080`. The module builds the fleet and starts a gateway thread at import time (`control_plane/app.py:196-197`).
- `python -m context_runtime.tools.mcp_servers.web_search` and `.deploy_scan`: reference MCP servers over stdio; `deploy_scan` also has an opt-in `--http` mode.
- `context_runtime/scripts/fit_calibration.py`, which reads `$CONTEXT_RUNTIME_HOME` (`:24-25`).

**Declared dependencies.** Base: `pyyaml>=6.0` and `agent-harness` pinned by git commit (`pyproject.toml:11-12`). Extras are at `pyproject.toml:18-93`: test, mcp, litellm, context-state (git), rag (git), hipporag (git fork), graphiti (git fork), embeddings, polars, duckdb, analytical, postgres, parquet, multimodal, turbovec, dagster, ingest, ingest-multimodal, connectors, ocr, pdf-tables, aws, otel, langfuse, control-plane, dev.

Imports that no extra declares (all lazy): `kafka` (`learning/kafka_bus.py:43,55,75`), `pymysql` (`integrations/vuln_db.py:38`), `opensearchpy` (`providers/aws/opensearch_retriever.py:34`), `pandas`/`cupy`/`cudf`/`cuvs` (`accel/*.py`), `scenedetect` (`adapters/store_video.py:158`), `colpali_engine` (`adapters/store_multivector.py:158`), `sidekick` (`integrations/sidekick.py:225`).

**Imported parts of sibling repositories** (all public, checked out at the pinned ref, pure Python)

| repository | pin | what is imported | notes |
|---|---|---|---|
| agent-harness | `6b8ba3a596a5e2caf0fdf009f2af013517591662`, 0.1.0, AGPL-3.0-or-later, no dependencies | `agent_harness.tools` (`tools/base.py:9`) | Importing it runs `agent_harness/__init__.py`, which eagerly imports agent, command_policy, sandbox, openshell, nim and h2. Its shell sandbox is POSIX-oriented (`command_policy.py:33`, `sandbox.py:33,38`, `egress_proxy.py:88`); context-runtime does not call those parts. Covered by the agent-harness audit. |
| runtime-contracts | tag `v0.3.13` = `3cd04bf0330c9f19adb142736e1e2c821ec68619`, AGPL-3.0-or-later with the Commons Clause, `>=3.11`, depends on `pyyaml>=6` | `models/{context,handle,visibility}`, `protocol/{evidence,geospatial}` (`context_state.py:26-31`, `geospatial/contracts.py:19`) | Standard library only. The tag has no case collisions; **`origin/main` does** (`docs/CANONICALIZATION.md` vs `docs/canonicalization.md`, and two `...0.3.X_CONSOLIDATION.md` files differing in the case of one letter), so cloning main on a default macOS or Windows file system gives a dirty tree. `build/lib/` is committed. |
| redevops-rag (`rag` extra) | `3a6c25379b93d81124ffc80ca459ff324685f18c`, AGPL-3.0-or-later with the Commons Clause | `adapters/store_redevops.py`, `integrations/redevops_rag.py` | Depends on sentence-transformers (torch), duckdb, numpy. `store.py:62` runs `INSTALL fts`; `parquet_index.py:151,192` run `INSTALL httpfs`. |
| HippoRAG fork (`hipporag` extra) | `ea601818728ad51d5841c240a43bd47a5b3f8091`, MIT | `adapters/store_hipporag.py` | `requirements.txt` pins `vllm==0.6.6.post1`, `torch==2.5.1`, `python_igraph==0.11.8`. |
| Graphiti fork (`graphiti` extra) | `053af50b96c5a78ac097decb88fdceb1266d8f0c`, Apache-2.0, graphiti-core 0.29.2 | `adapters/store_temporal.py` | Dependencies resolve. Needs a Neo4j server (`bolt://localhost:7687` default). |

**External executables the code can invoke (none bundled):** `ffmpeg` (`ingest/multimodal.py:94,161`, `adapters/store_video.py:171`); `docker`, `nmap`, `nuclei`, ZAP scripts (`integrations/runtime_scan_engine.py:159,178,187,209,238`); `trivy`, `syft`, `cosign`, `clamscan` (`integrations/supply_chain.py:68`). Benchmark and example scripts call `git`, `go`, `mojo`; `scripts/docs_check.py:75` calls `bash -n`; tests use `caddy` and `docker` when present.

**Privileges and network.** Nothing in the default path needs admin: ports are 8080 and 8770 and state is plain files. Model weights are fetched at first use by fastembed, faster-whisper, sentence-transformers and ColPali (`adapters/store_semantic.py:61`, `store_image.py:52`, `store_video.py:184`, `store_temporal.py:210`, `store_multivector.py:159`). DuckDB fetches the `fts` extension at `adapters/store_duckdb.py:46`.

## 3. Dependency and wheel matrix

**Method.** For each extra, and for all non-git extras together, `pip install --dry-run --only-binary=:all: --platform <tag> --python-version 3.12 --implementation cp --abi cp312 --abi abi3 --abi none --report` was run once for `win_amd64` and once for macOS arm64 (all tags from `macosx_26_0_arm64` down to `macosx_10_9_universal2` allowed). A package with no wheel for the platform fails the resolution. A second pass with only macOS 13 and older tags found the macOS floor. Licences come from PyPI JSON for the exact version; empty or garbled values were read from the wheel or upstream documentation and marked.

**The full 212-row matrix (package, version, extra groups, licence, `win_amd64` wheel, `macosx_arm64` wheel, notes) is in [`context-runtime-dependency-matrix.md`](context-runtime-dependency-matrix.md).**

**Results**

- Every extra resolves on its own. **All non-git extras resolve together:** 193 packages, about 784 MB of wheels on Windows and 773 MB on macOS. Largest: torch (127 / 139 MB), paddlepaddle (105 MB), polars-runtime-32 (54 MB).
- Minimal runtime plus test tools (base, control-plane, duckdb, analytical): 28 packages, about 52 MB (Windows) / 44 MB (macOS).
- 212 distinct package versions appear across all extras; 62 contain native code. All have wheels for both targets; none needs a compiler or is sdist-only. One extra is unsatisfiable:
- **Not resolvable:** `hipporag` as pinned. `vllm==0.6.6.post1` gives "No matching distribution found (from versions: none)" on both platforms.
- **macOS floor.** duckdb, pydantic-core, PyYAML and numpy 2.5.4 work on macOS 11. The latest torch 2.15.0, onnxruntime 1.31.1, av 19.0.2 and scipy 1.18.1 are tagged `macosx_14_0`; orjson 3.13.0 prefers `macosx_15_0`. On macOS 13 or older pip falls back to older versions. A conservative documented floor for the full optional set is macOS 14; the core needs far less.
- **Declared lower bounds.** `pyyaml>=6.0` and `numpy>=1.24` have no cp312 wheels at the bound; `duckdb==1.0.0` has no macOS arm64 cp312 wheel. Use a hash-pinned lock rather than the bounds.
- **Weight.** `sentence-transformers` pulls torch (209-220 MB of wheels). fastembed/ONNX is about 42-45 MB.

Git-pinned and non-PyPI rows:

| package | version | licence | win_amd64 | macosx_arm64 | notes |
|---|---|---|---|---|---|
| agent-harness | 0.1.0 @ `6b8ba3a` | AGPL-3.0-or-later | pure Python (from git) | pure Python | required; the PyPI name belongs to an unrelated project (0.1.10) |
| runtime-contracts | 0.3.13 | AGPL-3.0-or-later with the Commons Clause | pure | pure | `context-state` extra; needs `pyyaml` |
| redevops-rag | @ `3a6c253` | AGPL-3.0-or-later with the Commons Clause | pure | pure | `rag` extra; pulls torch |
| graphiti-core (fork) | 0.29.2 @ `053af50` | Apache-2.0 | pure | pure | `graphiti` extra |
| hipporag (fork) | 2.0.0-alpha.4 @ `ea60181` | MIT | pure | pure | extra unsatisfiable |
| vllm | 0.6.6.post1 | Apache-2.0 | none | none | blocks `hipporag` |

## 4. Licences

- **Repository.** AGPL-3.0-or-later plus the Commons Clause v1.0; `LICENSE.md` describes it as "source-available, not open source". Wheel metadata carries `License: AGPL-3.0-or-later WITH Commons-Clause`, which is not a valid SPDX expression, whereas `LICENSE.md` itself uses `LicenseRef-AGPL-3.0-or-later-with-Commons-Clause`. `LICENSE-COMMONS-CLAUSE` has a stale header: `Software:` names a different project.
- **Dependencies.** No dependency is outright incompatible with distributing under the family licence (INFERRED, not legal advice). Permissive licences (MIT, BSD, Apache-2.0, PSF, ISC, 0BSD, MIT-CMU) are compatible inbound with AGPL-3.0-or-later. Flags for a signed installer or bundle:
  - MPL-2.0 (`certifi`, `tqdm`, `orjson`): the source of the MPL-covered files must be offered and notices kept.
  - LGPL (psycopg 3.x family, `crc32c`, `python-bidi`): a single-file frozen bundle makes replacement or relinking hard. Keep these extras out of the default bundle, or ship them as separately replaceable wheels.
  - FFmpeg-bundling wheels (`opencv-python`, `opencv-contrib-python`, `av`): the GPL/LGPL configuration of the bundled FFmpeg is unverified. Verify before redistributing.
  - `torch`, `paddlepaddle`, `scipy`, `numpy`, `pypdfium2`, `psycopg-binary`: bundled third-party components. Ship a full notices file, including Apache NOTICE files and BSD attributions.
  - Interpreter, if bundled: PSF-2.0.
- **Questions for counsel.** AGPL-3.0 section 7 allows recipients to remove a "further restriction" from a covered work: how does that interact with the Commons Clause on the family licence? `agent-harness` is plain AGPL-3.0-or-later while the other family repositories carry the Commons Clause: the inbound direction is fine, but confirm that is intended. AGPL section 13 (network source offer) applies to a modified sidecar.

## 5. POSIX-assumption findings

Scope: `context_runtime/`, `scripts/`, `eval/`, `benchmarks/`, `deploy/`, `examples/`, plus the imported parts of the sibling repositories.

**Negative results** (a grep for these patterns found nothing in the package): `os.fork`, `multiprocessing`, process pools; `fcntl`, `flock`, `lockf`, `msvcrt`; `import resource`, `pwd`, `grp`; `signal.*`, `SIGKILL`, `SIGTERM`, `SIGALRM`, `killpg`, `setsid`, `start_new_session`, `preexec_fn`, `os.kill`; `/proc`, `os.getuid`; `shell=True`, `os.system`, `os.popen`; `os.link`, hard links, Unix-domain sockets, `sqlite3`/WAL; XDG variables, `Path.home`, `os.name`/`sys.platform` (apart from tests); `fsync` outside the approvals log. Concurrency is `threading` and `ThreadPoolExecutor` only. By inspection, importing the package on Windows should not fail on missing POSIX modules (INFERRED).

| id | finding | file:line | platform effect |
|---|---|---|---|
| F-ENC-1 | 24 `open()`/`read_text()`/`write_text()` calls without `encoding=`. Corpus adapters call `read_text(errors="ignore")`. | `adapters/store_inmemory.py:49`, `store_duckdb.py:89`, `store_postgres.py:100`, `store_community.py:251`, `store_hipporag.py:52,142`, `store_semantic.py:105`, `store_turbovec.py:84`, `integrations/agent_harness.py:34`; also `costmodel/statistics.py:108,111`, `runtime/config.py:34`, `observability/traces.py:65`, `reasoner/strategies.py:386,409,446`, `integrations/runtime_scan_engine.py:302`, `integrations/evidence_store.py:69,85` | Windows default is the ANSI code page (cp1252). UTF-8 corpus text is mis-decoded or silently dropped, so chunks differ from macOS/Linux. INFERRED from CPython 3.12 behaviour. `subprocess.run(text=True)` at `supply_chain.py:72` and `runtime_scan_engine.py:169` decodes tool JSON with the locale as well. |
| F-ORD-1 | Corpus walk is `sorted(Path.rglob("*"))`; labels use `str(p.relative_to(base))`, which has OS separators; `glob.glob` results are unsorted. | `adapters/store_inmemory.py:45`, `store_duckdb.py:86`, `store_postgres.py:97`, `store_community.py:248`, `store_image.py:131`, `store_video.py:86`, `store_hipporag.py:49,142`, `store_multivector.py:106`, `store_semantic.py:102`, `store_turbovec.py:81`, `integrations/agent_harness.py:32`, `sources/local.py:31,35`, `integrations/sidekick.py:155`, `reasoner/strategies.py:407,444` | `WindowsPath` orders case-insensitively, `PosixPath` case-sensitively, so ids and order differ across operating systems, and labels carry `\` on Windows. INFERRED. |
| F-PATH-1 | Hard-coded Linux-container defaults: `/data/context-runtime/policy`; `/data/market-radar/{profiles,pitch_template.txt,outreach_ledger.jsonl,outbox}` | `policy/store.py:55`; `integrations/job_leads.py:122,239,306,446` | macOS: the root volume is read-only, so a normal user cannot create `/data` (documentation-based). Windows: resolves to the current drive root. |
| F-PATH-2 | Working-directory-relative homes and config: `.context-runtime`, `.agentic-os`, `config.yaml`. The test run created `.context-runtime/` (with `approvals.jsonl`) in the CWD. | `control_plane/app.py:38,170`; `control_plane/fleet.py:61`; `control_plane/context.py:34`; `scripts/fit_calibration.py:24-25` | State location depends on where the process starts. A launchd job or Windows service starts in an unpredictable directory. |
| F-PATH-3 | No platform directory logic exists (no `LOCALAPPDATA`, `Application Support`, XDG). Files are created with the process umask and `mkdir` without a mode. | repo-wide | State files are 0644/0755 on POSIX. |
| F-PATH-4 | `_CORPUS_SEGMENT` allows Windows reserved device names (`con`, `nul`, `aux`, `com1`...). `_safe()` in the policy store does not cap length. | `control_plane/security.py:15,194`; `policy/store.py:147-149` | INFERRED. Later checks make this fail closed (404) rather than escape. |
| F-ATOM-1 | Eight stores write `<name>.tmp` (fixed name, not unique) then `os.replace`. Only `threading.Lock` guards them, no inter-process lock. Further writes are non-atomic or append-only. | `os.replace` at `quality.py:130`, `costmodel/profile.py:88`, `integrations/job_leads.py:149,255`, `integrations/bandit.py:116`, `integrations/calibration.py:101`, `control_plane/context.py:103`, `policy/store.py:85`. Non-atomic: `costmodel/statistics.py:108`, `integrations/sidekick.py:149,168`, `control_plane/context.py:64,70`. Appends: `calibration.py:143`, `evidence_store.py:85`, `context.py:69` | Windows: `os.replace` raises `PermissionError` if the target is open elsewhere (reader or antivirus scan). INFERRED. DuckDB files take an exclusive single-writer lock (`adapters/store_duckdb.py:45`), so a second process cannot open them. |
| F-TMP-1 | `NamedTemporaryFile(delete=True)` is reopened by name while open (`ingest/extractors.py:52-55`), which fails on Windows with the default `delete`. `delete=False` files need a cleanup audit (`ingest/pdf_tables.py:42`, `ingest/paddle_ocr.py:109`). `mkdtemp` is never removed (`compression/transcript.py:49`, `runtime_scan_engine.py:249`). | as listed | INFERRED. Python 3.12 has `delete_on_close=False`. |
| F-HANDLE-1 | `json.load(open(path))` leaks the handle. | `reasoner/strategies.py:386,409,446`; `integrations/bandit.py:94` | A leaked handle blocks replace or delete on Windows until garbage collection. |
| F-LINK-1 | The symlink-escape check relies on `is_symlink()` with `os.walk(followlinks=False)`; `sources/local.py:33` follows symlinks. | `control_plane/security.py:251-261`; `sources/local.py:31-35` | Windows junctions (no admin needed) are not symlinks, so they are neither caught nor refused. INFERRED from CPython documentation (`Path.is_junction` is new in 3.12). This is the ingestion-confinement boundary. |
| F-PROC-1 | `MCPClient.stdio` has no process group, session or Job Object. `close()` calls `terminate()` + `wait(0.5)` on the direct child only. | `tools/mcp.py:97-105,219` | Grandchildren (for example `node` launched by `npx`) survive on every OS. On Windows `terminate()` is a hard kill. |
| F-PROC-2 | Blocking `readline()` with no timeout and no cancel. | `tools/mcp.py:130` | A hung server hangs the caller. |
| F-PROC-3 | `stderr=PIPE` is never drained (read once at EOF). | `tools/mcp.py:101,133` | A chatty server can fill the pipe and deadlock, on every OS. |
| F-PROC-4 | `text=True`, `bufsize=1`, no `encoding=`. | `tools/mcp.py:102-103` | Windows decodes server UTF-8 as cp1252. INFERRED. |
| F-PROC-5 | The command is passed as a list with no `shutil.which`. | `tools/mcp.py:97` | On Windows, `.cmd`/`.bat` shims (`npx`, `uvx`) are not found by `CreateProcess`. INFERRED from documented behaviour. |
| F-ENV-1 | The environment allowlist (good: secrets do not leak to MCP children) has no `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `PROGRAMDATA`, `HOMEDRIVE`, `HOMEPATH`, `ProgramFiles`; Windows has no `HOME`. | `tools/mcp.py:26-34` | Windows children may fail to find a profile directory. INFERRED. |
| F-ENV-2 | Scanner and ffmpeg subprocesses inherit the full parent environment. Documented as by design. | `tests/test_mcp_env_guard.py:36-42`; `supply_chain.py:72`; `runtime_scan_engine.py:169` | Not MCP; keep out of the native core. |
| F-SAFE-1 | The approval safety scan is advisory and matches only POSIX/Linux patterns (`rm -rf`, `sudo`, `curl\|sh`, `dd of=/dev/sd*`, `chmod 777`, `iptables -F`). | `control_plane/safety.py:12-24`, used at `control_plane/context.py:56` | No Windows or macOS patterns (`del /s`, `Remove-Item -Recurse -Force`, `format`, `reg delete`, `iwr\|iex`, `diskutil erase`, `osascript`). Treat the scan as a badge, not a control. |
| F-DUCK-1 | `INSTALL fts; LOAD fts;` downloads a native extension to `~/.duckdb/extensions/<ver>/<platform>` at first use. Confirmed on macOS: the test run wrote `v2.0.0/osx_arm64/fts.duckdb_extension`. The repository for 2.0.0 lists `windows_amd64` and `osx_arm64` (HTTP 200). | `adapters/store_duckdb.py:46`; the redevops-rag store at `store.py:62` | Not offline, despite the CI comment. Needs network and a native extension, not signed by us, at runtime. |
| F-NET-1 | No in-code bind address for the control plane. The uvicorn CLI default is loopback; the Dockerfile uses `0.0.0.0`. CORS defaults include a public origin plus `localhost:5173/8080/3080`. | `Dockerfile:28`; `control_plane/app.py:189-190` | The native launcher must pin loopback. |
| F-AUTH-1 | The control plane fails closed (503 with no key, 401 with a wrong key; constant-time compare). It uses one static shared key read from the environment, via `X-API-Key` or Bearer. No per-caller identity, no native credential store, no Unix socket or named pipe. | `control_plane/security.py:114-170`; `app.py:1078,1189` | Meets "authenticate every caller" only as a shared secret. Needs the platform services layer. |
| F-DOCKER-1 | ZAP-in-Docker uses `--network host` and `-v <host-path>:/zap/wrk`. After a timeout, `subprocess.run` kills only the docker CLI, not the container. | `integrations/runtime_scan_engine.py:176-260`; `os.chmod(workdir, 0o777)` at `:250` | Linux-oriented. Windows host paths in `-v` are a hazard (INFERRED). |

Not present, for the record: no fsync of data files or directories (only the approvals log, `context.py:102`); no file-locking code at all; executable bits matter only for the seven `deploy/` scripts; `.suffix.lower()` is used consistently and `chunk_id` is built from `fp.name`.

## 6. Test results on macOS

Environment: macOS arm64 (27.0.1), Python 3.12.16, fresh virtual environment. Install: the CI recipe, `pip install -e ".[dev,test,control-plane,duckdb]" numpy pillow` (this pulled `agent-harness` from git). Notable versions: pytest 9.2.0, numpy 2.5.4, duckdb 2.0.0, fastapi 0.162.0, uvicorn 0.54.0. Run with `APPROVAL_SECRET` unset, `HOME` pointed at a throwaway directory, `-p no:cacheprovider`, `python -m pytest tests -q -rs`.

**Result: 980 passed, 12 skipped, 0 failed, 3 warnings, 19.33 s.** There were no macOS-specific failures.

The 12 skips: `runtime_contracts` not installed (`test_context_state.py:13`); `intelligence_gateway` not installed (`test_integration_intelligence_gateway_contract.py:9`); no pandas (`test_accel_backends.py:79`); no CUDA or cupy, two tests (`test_accel_backends.py:108,123`); no docker (`test_container.py:79`); a placeholder for the separate edition-specific permission layer (`test_integration_security.py:60`); `[embeddings]` extra not installed (`test_semantic.py:33`); `dlt` extra not installed (`test_sources.py:85`); `ocr` extra not installed (`test_sources.py:98`); model download gated by `CR_TEST_RAG` (`test_transcript_compaction.py:173`); `turbovec` extra not installed (`test_turbovec.py:32`).

Test inventory: 103 test modules, 992 test items, 723 `def test_` functions. Largest: `test_control_plane_access.py` (131 items), `test_control_plane_ingest.py` (54), `test_demo_proxy.py` (43), `test_mcp_env.py` (12), `test_mcp.py` (4).

Platform-sensitive tests:

- **Symlink tests.** `test_control_plane_ingest.py:158` skips them on `win32`. The seven symlink-escape tests (lines 161-206) would not run on Windows, and no junction test exists.
- **Caddy test.** `test_demo_proxy.py:37` skips without `caddy`; it ran here because Caddy is installed on this Mac. CI has no caddy step, so it is probably skipped there (INFERRED). Line 88 builds `PATH` with `:/usr/bin:/bin`, a POSIX separator and path.
- **Hard-coded `/tmp`.** `test_integration_sidekick.py:77` uses `/tmp/context_runtime_test_skills`.
- **Docker test.** `test_container.py:79` skips without docker.
- **Fake MCP servers.** Most MCP tests use `sys.executable` fake servers (`test_mcp.py:95,142`, `test_mcp_env.py:47,120`) and should be portable.

Side effects: the "offline" suite downloads the DuckDB `fts` extension (F-DUCK-1) and writes `.context-runtime/` into the current directory.

Windows predictions (INFERRED, not run): failing or erroring are `test_demo_proxy` (if caddy is present) and any test relying on UTF-8 default reads; skipped are the symlink and docker tests.

## 7. Risk-ranked change list

Effort figures are guesses, in engineer-days.

| # | severity | workstream | item | location | approach | effort (guess) |
|---|---|---|---|---|---|---|
| 1 | high | NAT-01 | Explicit UTF-8 everywhere | F-ENC-1 | Add `encoding="utf-8"` at the 24 sites. Add ruff `PLW1514` and `-X warn_default_encoding` to CI. The launcher sets `PYTHONUTF8=1` as a second line of defence. | 1-2 |
| 2 | high | NAT-01/03 | Deterministic enumeration and labels | F-ORD-1 | One helper `iter_corpus_files(root)` returning NFC-normalised POSIX relative paths in codepoint order; replace the 15 call sites; add golden-output tests that must match across Linux, macOS and Windows. | 2-3 |
| 3 | high | NAT-02 | State and config locations | F-PATH-1..3 | One `state_dir()`, `config_dir()` and `cache_dir()` resolver: environment override first, then macOS `~/Library/Application Support` and `Caches`, then Windows `%LOCALAPPDATA%`. Inject into `RuleStore`, `ProfileStore`, `Context`, `Fleet`, calibration. Remove CWD-relative defaults. Create the data directory 0700 on macOS. | 2-3 |
| 4 | high | NAT-01 | Atomic write plus file lock | F-ATOM-1 | `atomic_write()` with a unique temp file in the same directory, file fsync, directory fsync on POSIX, and a bounded retry on `PermissionError` for `os.replace`. A `file_lock` abstraction (`fcntl.flock` / `msvcrt.locking`) for every JSON/JSONL store. | 4-6 |
| 5 | high | NAT-04 | MCP stdio client | F-PROC-1..5, F-ENV-1 | Run through a `process_runner`: new session plus group kill on macOS, a Job Object on Windows (kill-on-close). Reader thread with per-call timeout and cancel. Drain stderr. `encoding="utf-8", errors="replace"`. Resolve the command with `shutil.which`/`PATHEXT`. Extend the environment allowlist with the Windows profile variables. | 4-6 |
| 6 | high | NAT-08 | Packaging of `agent-harness` | `pyproject.toml:12` | Publish under a distinct distribution name or vendor it; PyPI rejects wheels that declare direct references. Provide a hash-pinned lock for each target. | 2-3 |
| 7 | high | NAT-03 | DuckDB `fts` extension | F-DUCK-1 | Bundle the extension and `LOAD` it from a path; fall back to the in-memory BM25 store when it is unavailable; `custom_extension_repository` for offline installs; do not auto-INSTALL in the offline path. | 1-2 |
| 8 | high | NAT-01 | CI matrix | `.github/workflows/tests.yml` | Add `windows-latest` and `macos-14` jobs and fix what they reveal. | 1 + 2-4 |
| 9 | medium | NAT-01 | Junction and reparse-point handling | F-LINK-1 | Detect reparse points through `st_file_attributes` / `Path.is_junction()` and refuse them in the corpus scan; add Windows tests. | 1-2 |
| 10 | medium | NAT-01 | Temp-file hygiene | F-TMP-1, F-HANDLE-1 | `delete_on_close=False` or `TemporaryDirectory`; `with open(...)`; clean up `mkdtemp`. | 1 |
| 11 | medium | NAT-02/06 | Transport and entry point | F-NET-1, F-AUTH-1, `app.py:196-197` | Add `context-runtime serve` with loopback default, an ephemeral port, and a token read from the native credential store. Turn `app.py` into a factory with no import-time side effects. Plan a Unix-socket or named-pipe option. | 4-6 |
| 12 | medium | NAT-04/05 | Command safety scan | F-SAFE-1 | Add Windows and macOS patterns; document it as advisory; enforce high-risk actions in policy, failing closed. | 1 |
| 13 | medium | NAT-08 | Dependency lock | `pyproject.toml` bounds | A hash-pinned lock per target; raise the lower bounds (`pyyaml>=6.0.1`, `numpy>=1.26`); declare the undeclared optional imports. | 1-2 |
| 14 | medium | NAT-08 | Licence metadata | `pyproject.toml:7`, `LICENSE-COMMONS-CLAUSE` header | Align the licence string with `LICENSE.md` and fix the stale header. | 0.5 |
| 15 | medium | NAT-03 | `rag` extra weight | extras | `sentence-transformers` pulls torch (209-220 MB of wheels). Keep it out of the default native bundle; offer fastembed/ONNX (about 42-45 MB) as the semantic option. | 1 |
| 16 | low | NAT-01 | Reserved names and length | F-PATH-4 | Reject Windows reserved names and cap component length. | 0.5 |
| 17 | low | NAT-01 | Test portability | `test_demo_proxy.py:88`, `test_integration_sidekick.py:77`, tests that create `.context-runtime` in the CWD | Use `os.pathsep`, `tmp_path`, and a per-test home. | 0.5 |
| 18 | low | NAT-02 | File permissions | F-PATH-3 | Directories 0700 on macOS; user-profile ACL as the default on Windows. | 0.5 |

## 8. Code signing, antivirus and quarantine

- **pip wheels.** The context-runtime wheel is pure Python, so there is nothing to sign. Native wheels come from third parties and are signed by nobody on our side.
- **Bundles (zip, installer, pkg).** For macOS notarization every nested Mach-O in the bundle must be signed with the Developer ID and hardened runtime. In the default set that covers pydantic_core, duckdb, numpy, pillow, PyYAML and the interpreter; optional extras add torch, onnxruntime and others.
- **Runtime-loaded native code.** DuckDB's downloaded `fts` extension is a dynamic library signed by DuckDB, not by us (INFERRED). Under hardened runtime with library validation it may be rejected unless the entitlement `com.apple.security.cs.disable-library-validation` is granted or the extension is bundled and re-signed. On Windows, WDAC, AppLocker or Defender can block it. Bundling it is safer.
- **Windows.** Authenticode-sign the launcher and installer. SmartScreen reputation applies to a new publisher. `.pyd` and `.dll` files are not signed by us. A downloaded zip carries Mark-of-the-Web, which can propagate to the extracted files.
- **macOS.** A quarantine attribute is set on downloaded zip or pkg files; `pip install` does not set it.
- **User-mode.** Everything in the default path runs without admin. Break points: the `/data` defaults (F-PATH-1); in the optional DAST path, `nmap` SYN scans need root, Npcap needs admin on Windows (INFERRED) and Docker Desktop needs admin to install; `ffmpeg` can be installed per user.

## 9. Unsupported-features register

| feature | native status | behaviour today | fails closed? |
|---|---|---|---|
| `hipporag` extra | Not installable as pinned (`vllm` has no distribution) | A lazy import fails when used (failure message not verified) | n/a |
| GPU acceleration (cuVS, cuDF, CuPy) | CUDA only | Off by default (`CR_ACCEL`); `gpu_available()` returns False without cupy and falls back to CPU (`accel/selector.py:51-60`) | soft fallback to CPU, by design |
| DAST scanning (nmap, nuclei, ZAP in Docker) | Not bundled; Linux-oriented | Without live mode or tools it returns a simulated report marked `simulated=True` (`runtime_scan_engine.py:265-273`); off-scope targets are refused | scope fails closed; tool absence yields a simulated result |
| Supply-chain scanners (trivy, syft, cosign, clamscan) | External binaries, not bundled | Returns `ScanResult(False, note="trivy not installed")` (`supply_chain.py:111-113`) | visible failure |
| Docker and the container test | Not applicable natively | `test_container.py` skips without docker | n/a |
| Video/audio ingestion | Needs external ffmpeg | Returns empty text silently when ffmpeg is absent (`ingest/multimodal.py:161-162`) | fails soft, content dropped |
| Strong process isolation | No Windows or macOS sandbox backend exists; `agent_harness.sandbox` states it is not an isolation boundary | Commands run as the host user | no; a new `sandbox_backend` must fail closed for high-risk actions |
| Governance safety scan on Windows and macOS | POSIX-only patterns | Advisory badge only; no Windows or macOS patterns | fails open for the badge |
| Postgres/pgvector, Kafka, Neo4j/Graphiti, OpenSearch, AWS Bedrock/Athena, Dagster, Langfuse, CRM and chat integrations | Remote services; client libraries have wheels | Treat as integrations, not ports | n/a |
| `deploy/*` (bash, Caddy, AppArmor, compose) and the Mojo benchmark | Linux/container assets | not part of native | n/a |
| Windows ARM64, macOS Intel, Python 3.13 or 3.14 | Not assessed (resolution was cp312 only) | unknown | unknown |

## 10. Open questions for the maintainers

1. Which feature set is the native core: base plus control-plane only, or also the DuckDB store and ONNX embeddings? That decides how much of F-DUCK-1 and the wheel weight matters.
2. Is `PYTHONUTF8=1` set by the launcher acceptable as the first-line fix for F-ENC-1, or must the code itself be encoding-explicit?
3. What must be bit-identical across operating systems: chunk ids, order, labels, scores? Float results from numpy or BLAS on the vector and semantic paths may differ by CPU and BLAS (INFERRED). Is a tolerance acceptable there?
4. Packaging of `agent-harness`: vendor it, rename the distribution, or publish under a reserved name?
5. Should DuckDB's `fts` extension be bundled and re-signed, or should the native core fall back to the in-memory BM25 store?
6. Is a static shared key plus a loopback bind enough for the native sidecar, or is per-caller identity (peer credentials on a Unix socket or named pipe) required for the first release?
7. Are the DAST/scanner, Docker-based and `hipporag` paths declared unsupported on native, or do they get replacements?
8. How should the Commons Clause and AGPL section 7 interact, and is the plain AGPL on `agent-harness` intended? (Section 4.)
9. `runtime-contracts` `main` has case-colliding file names. Is that to be fixed before native clones of main are expected to work?

## 11. Security-relevant observations

Security-relevant observations from this audit are tracked privately, not in this document.
