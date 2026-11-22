# NAT-00 audit: agent-harness

Auditor scope: `github.com/redevops-io/agent-harness` (public). Read-only audit of a fresh clone; no repository or checkout was modified. File:line references are to the audited commit. Statements marked INFERRED are reasoning from code or documentation, not observed behaviour. No Windows host was available: nothing below is a Windows runtime result.

## 1. Summary

**Verdict: GO-WITH-CHANGES** for the portable parts; **BLOCKED as-is** for the tool-execution and sandbox layer.

- The package has zero runtime dependencies (`pyproject.toml:8`), no native binaries and no compiled extensions. Its core (tool registry, approval gate, loop, supervisor, spend ledger, `agent_harness.qa` records, receipt chains and verifier) is portable Python, and the full offline suite passes on macOS Apple Silicon (3558 passed, 21 skipped, 0 failed; Section 7).
- The part that executes commands, confines paths and cleans up processes (`sandbox.py`, `command_policy.py`, `agent.py`) is POSIX-only by construction. On Windows it would not malfunction silently: INFERRED, every command resolves to "not found" and is denied (fail closed), so it is non-functional rather than unsafe. On macOS it works but has a case-sensitivity gap.
- There is no MCP client or server code in this repository (Section 6). NAT-05's MCP environment-isolation work is greenfield, not a port.

**Top five risks**

1. **No process-tree control or cancellation (blocker for NAT-04).** `sandbox.run_shell` uses `subprocess.run(..., timeout=)` (`agent_harness/sandbox.py:40-55`), which kills only the direct child. There is no process group, session, Job Object or cancel path for a running tool; `max_seconds` is only checked between turns (`loop.py:139`). On Windows, CPython additionally calls `communicate()` after the kill (`subprocess.py:552-559` in the 3.12.16 stdlib), which can block while a surviving grandchild holds the pipes (INFERRED).
2. **Command policy and file confinement are written in POSIX string semantics (high).** Fixed `TRUSTED_PATH`, `"/" in name` tests, POSIX `shlex.split`, and `startswith(root + os.sep)` containment on `realpath` strings (`command_policy.py:33,63,105,160`; `agent.py:30`; `sandbox.py:30,33`). These need a platform path layer; macOS's default case-insensitive volume is already affected (details withheld; see pointer in Section 12).
3. **Full environment inheritance into children, no allowlist (high, NAT-05).** `dict(os.environ)` is passed to the sandboxed command (`sandbox.py:38`) and `{**os.environ, ...}` to judge and harness children (`bench/evalkit.py:132,178`, `bench/harnesses/opencode.py:86`, `bench/harnesses/harness2.py:170`). Credentials in the parent environment (`OPENAI_API_KEY`, `NIM_API_KEY`, `JUDGE_API_KEY`) reach every child.
4. **Only listeners are unauthenticated loopback HTTP proxies, one injecting a real provider key (high, design).** `spend_proxy.py:183-186,205,211` binds `127.0.0.1` and accepts any caller ("any dummy API key", `spend_proxy.py:3`); there is no local IPC contract or caller authentication anywhere in the repo. This does not meet the plan's "authenticate every caller" rule if reused natively.
5. **No native isolation backend, and the only strong one needs Docker (high).** `LocalSandbox` is documented soft confinement (`sandbox.py:6-10`, `openshell.py:124-128`); it checks declared capabilities before running but restricts nothing at run time. The only real isolation is `bench/sandbox.py` (Docker, Linux container). Natively, network, filesystem-escape and process-spawn actions must fail closed (Section 5).

**Counts:** 86 Python modules (17,808 LOC package; 25,055 LOC tests); 0 runtime dependencies; 0 native binaries; 3,579 tests collected with Pillow installed (3,558 pass / 21 skip / 0 fail on macOS arm64, Python 3.12.16); 29 POSIX-assumption findings; 25 change items (3 blocker, 6 high, 10 medium, 6 low); 14 unsupported-feature entries.

## 2. Repo facts

| Item | Value |
|---|---|
| Audited ref | `origin/main` = `f3c87b7f274aaa2bd2d0841715972081f1319f0d` (2026-10-31, "Merge pull request #29: Record the 0.2.0 release on main (tag v0.2.0)") |
| Release tag, audited separately | `v0.2.0` = `a58ef9efcf2aa61fa92137f6a8dbd589b6e4b183` ("Release 0.2.0"); is an ancestor of main; 12 commits between tag and main |
| Version | `0.2.0` (`pyproject.toml:3`); status "alpha / experimental" (`STATUS.md`) |
| Python | `requires-python >=3.10` (`pyproject.toml:6`); `agent_harness.qa` requires 3.11+, enforced by an import guard (`agent_harness/qa/__init__.py`, `docs/qa.md`) |
| Build backend | hatchling (`pyproject.toml:15-16`); wheel packages `agent_harness` (`pyproject.toml:19`) |
| Entry points | No `[project.scripts]`. Module entry points via `python -m`: `egress_proxy`, `spend_proxy`, `nim`, `openshell`, `guardrails_nemo`, and `bench.*` (`ablation, selfcheck, audit, batch_validation, replay, experiment, jobbench, harnesses.opencode`) |
| Modules | 86 `.py` files: core 4,400 LOC (excluding qa and bench); `qa/` 9,895 LOC (28 files, all SPDX-tagged); `bench/` 3,513 LOC; `h2/` (file-tree spec) |
| LOC | package 17,808; tests 25,055 (82 test files incl. `tests/qa`) |
| CI | `.github/workflows/tests.yml` (ubuntu-latest, Python 3.10 and 3.12), `qa.yml` (ubuntu-latest, 3.11 and 3.12, scrubbed env), `secret-scan.yml`, `docs-links.yml`. No Windows or macOS runner anywhere |
| Lockfile / SBOM | none in repo |
| Docs mentioning Windows | none; macOS appears only for upstream tools (`docs/harness-opencode.md:11`, `docs/harness-google-harness2.md:17`: needs GNU `timeout`) |

**v0.2.0 tag vs main.** The tag already contains `qa/` (28 files) and the core. `sandbox.py`, `command_policy.py`, `agent.py` and `openshell.py` are byte-identical between tag and main (`git diff --quiet`), so Sections 4 and 5 apply to both. Main adds 33 non-QA modules (supervisor, netpolicy, multimodal, completion, integrity, bench tool parity) and modifies `tools.py`, `loop.py`, `egress_proxy.py`, `spend_proxy.py`, `bench/sandbox.py`. Tag test run (same venv, Pillow present): 3264 passed, 5 skipped, 0 failed.

## 3. Dependency and wheel matrix

Declared runtime dependencies: none (`pyproject.toml:8`). Imports across `agent_harness/` are standard library plus package-relative; optional or lazy third-party imports: `PIL` (`multimodal.py:90`, guarded by `except Exception` at `:99`), `context_runtime` (separate package, imported lazily in tests and `bench/jobbench.py`), `harness2` (upstream project, lazy, `bench/jobbench.py`), `nemoguardrails` (named in docstring only; the module is dependency-free, `guardrails_nemo.py:16`). External programs (not Python): `docker` (bench only), OpenCode and Harness2 CLIs (bench only), GNU `timeout`/`bash`/`pkill` inside the Docker image.

Wheel check method: `pip download --only-binary=:all: --no-deps --platform <p> --python-version 3.12 --implementation cp` with `<p>` = `win_amd64` and `macosx_11_0_arm64`, run in a throwaway venv (pip 26.2.2). Result is the wheel file name returned.

| Package | Role | Version resolved | win_amd64 | macosx_arm64 | Native code | Licence (wheel metadata) |
|---|---|---|---|---|---|---|
| agent-harness | this package | 0.2.0 | pure Python | pure Python | none | AGPL-3.0-or-later |
| pytest | dev extra | 9.2.0 | `py3-none-any` | `py3-none-any` | none | MIT |
| iniconfig | pytest dep | 2.3.1 | `py3-none-any` | `py3-none-any` | none | MIT |
| packaging | pytest, hatchling dep | 26.3 | `py3-none-any` | `py3-none-any` | none | Apache-2.0 OR BSD-2-Clause |
| pluggy | pytest, hatchling dep | 1.7.0 | `py3-none-any` | `py3-none-any` | none | MIT |
| Pygments | pytest dep | 2.21.0 | `py3-none-any` | `py3-none-any` | none | BSD-2-Clause |
| colorama | pytest dep, `sys_platform == "win32"` only | 0.4.6 | `py2.py3-none-any` | `py2.py3-none-any` | none | BSD-3-Clause |
| tomli | `python < 3.11` dep (pytest, hatchling) | 2.5.0 | `cp312-win_amd64` | `cp312-macosx_11_0_arm64` | wheel is binary | MIT |
| exceptiongroup | pytest dep, `python < 3.11` | 1.3.1 | `py3-none-any` | `py3-none-any` | none | MIT |
| typing-extensions | exceptiongroup dep | 4.16.0 | `py3-none-any` | `py3-none-any` | none | not read from the wheel; verify |
| hatchling | build backend | 1.33.0 | `py3-none-any` | `py3-none-any` | none | MIT |
| pathspec | hatchling dep | 1.1.1 | `py3-none-any` | `py3-none-any` | none | MPL-2.0 (file-level copyleft; build-time only, not redistributed) |
| trove-classifiers | hatchling dep | 2026.9.21.13 | `py3-none-any` | `py3-none-any` | none | Apache-2.0 |
| tomlkit | hatchling dep | 0.15.2 | `py3-none-any` | `py3-none-any` | none | MIT |
| Pillow | optional (`multimodal.py`; tests only here) | 13.0.0 | `cp312-win_amd64` | `cp312-macosx_11_0_arm64` | yes (bundles image libraries) | MIT-CMU |

All 15 rows resolved for both targets on CPython 3.12. Other CPython versions (3.10, 3.11, 3.13) were not queried; the 3.10 floor pulls `tomli` and `exceptiongroup`, which the table covers for 3.12 only. Nothing on the runtime path is a required native wheel. If an installer bundles Pillow, its vendored image libraries need an SBOM line (not enumerated here).

Test venv pins (for a lockfile): pytest 9.2.0, iniconfig 2.3.1, packaging 26.3, pluggy 1.7.0, Pygments 2.21.0, Pillow 13.0.0.

## 4. POSIX-assumption findings

Severity is for a native Windows x86-64 and macOS arm64 distribution of rAIls Core. Windows effects are INFERRED from code; macOS effects are marked observed where I ran something.

| # | file:line | Assumption | Effect | Sev |
|---|---|---|---|---|
| 1 | `agent_harness/command_policy.py:33` | `TRUSTED_PATH = ("/usr/local/bin", "/usr/bin", "/bin")` | Windows: `resolve_command` (`:58-69`) finds nothing, so every bare command is denied or "command not found" (fail closed, non-functional). No PATHEXT, no `.exe` | blocker |
| 2 | `command_policy.py:63,105,109`; `sandbox.py:33` | `"/" in name` decides "path vs bare name" | Windows backslash and drive paths pass as "bare names" and then fail resolution; macOS fine | high |
| 3 | `command_policy.py:160,30`; `sandbox.py:30` | `shlex.split` (POSIX mode); metacharacter set `_SHELL_METACHARS` | Windows paths with backslashes are mangled; the set lacks `%`, `^`, `!`, relevant if a `.bat`/`.cmd` is ever resolved (CreateProcess routes those via `cmd.exe`) | high |
| 4 | `command_policy.py:67` | `os.access(cand, os.X_OK)` as the "is executable" test | Windows: true for any existing file (no exec bit); executability must be decided by extension | medium |
| 5 | `command_policy.py:72-73,117,140-143`; `agent.py:30` | containment by `startswith(root + os.sep)` on `os.path.realpath` strings | Case-sensitive on case-insensitive volumes (macOS default APFS: observed that `posixpath.realpath` does not normalise case and the file exists under both spellings; Windows INFERRED). Also fails on a drive root (`agent.py:30`: root `C:\` gives prefix `C:\\`), 8.3 short names, `\\?\` prefixes, alternate data streams, reserved device names, trailing dots and spaces | high |
| 6 | `command_policy.py:89-101,113-123,170` | "written during session" set stores raw `realpath` strings | Same case and alias problem as #5 for the re-approval control | high (detail withheld) |
| 7 | `agent.py:29`; `command_policy.py:140` | `os.path.realpath` for symlink defence | macOS: symlinks resolved (tested in `tests/test_command_policy.py:62-72`, passes). Windows: symlinks need privilege or Developer Mode; junctions and reparse points resolve via `GetFinalPathName` in 3.8+ (INFERRED); no TOCTOU defence between check and use (no handle-based or `O_NOFOLLOW` open) | medium |
| 8 | `agent.py:39,45` | `open(resolved, "r"/"w")` with no `encoding` | Locale encoding: cp1252 on Windows, so non-ASCII file content corrupts or raises; `\n` to `\r\n` translation on write | medium |
| 9 | `sandbox.py:47` | `text=True` without `encoding`/`errors` | Child output decoded with the locale codec; undecodable bytes raise and are swallowed by the broad `except` at `:56` as `returncode 1` with the error text, losing output | medium |
| 10 | `sandbox.py:38-39` | child env = full `os.environ` with `PATH` forced to the POSIX list | Secrets inherited (Section 6); on Windows a `PATH` of POSIX dirs drops the directories Windows binaries need and `SystemRoot`/`PATHEXT`/`COMSPEC` handling is untouched (INFERRED) | high |
| 11 | `sandbox.py:40-55` | `subprocess.run(timeout=)`, no `start_new_session`, no `killpg`, no `preexec_fn`, no Job Object | Tree not killed (Section 5). Exit code 124 is a synthetic copy of the `timeout(1)` convention, not a real status | blocker |
| 12 | `bench/sandbox.py:135-146` | `docker exec ... timeout --kill-after=5 --signal=TERM ... bash -lc`, `pkill -KILL -f` | Linux userland inside the container (GNU timeout, bash, pkill); Docker-only | n/a (out of scope, see register) |
| 13 | `bench/sandbox.py:61,65-67,115-118` | `os.chmod 0o755/0o644`, `/tmp` tmpfs, `-v {stage}:/opt/egress`, `/work` mount | POSIX modes and container paths; Windows host paths with drive colons in `-v` need translation | n/a (Docker-only) |
| 14 | `bench/jobbench.py:169-176`; `bench/tools_parity.py:120,135,232,236`; `bench/outputs.py:42,48,100`; `bench/selfcheck.py:65-67` | `tempfile.mkdtemp` then `chmod 0o777/0o666` so a container uid 1000 can write | No-op for permission bits on Windows (only the read-only flag); world-writable workspaces on shared POSIX hosts | low (bench only) |
| 15 | `bench/edit.py:63-70` | `mkstemp`, `os.fsync`, `os.chmod(st_mode & 0o7777)`, `os.replace` | Atomic replace works on Windows but raises `PermissionError` while another process (antivirus, indexer) holds the destination open; no retry. `chmod` copies only the read-only bit | medium |
| 16 | `bench/tools_parity.py:132-136,221-233` | same temp-file-then-replace pattern for `write_file` and `web_download` | as #15 | medium |
| 17 | `h2/artifact.py:46-55` | build in `.tmp-<id>` then `Path.rename(dest)` for a directory | Windows: directory rename fails if any file inside is open or being scanned; no cross-process lock on a concurrent save | medium |
| 18 | `h2/artifact.py:52,54,72`; `trace.py:69` | `write_text(..., encoding="utf-8")` without `newline=` | Windows writes `\r\n`; blob names are hashes of the in-memory text (`trace.py:26,66`), so a byte hash of the file would differ by platform (INFERRED) | medium |
| 19 | `trace.py:57`; `loop.py:118`; `spend.py:117`; `bench/outputs.py:103`; `bench/tools_parity.py:121,237` | JSONL appended in text mode, `"\n"` | `\r\n` on Windows; readers use `splitlines()` (`spend.py:90,124`) so they tolerate it | low |
| 20 | `spend.py:82-118` | spend ledger append guarded by an in-process `threading.Lock` only; no file lock; no `fcntl` | Two processes appending the same ledger can interleave on any OS; cap enforcement (`:99-105`) then reads stale totals. Needs a cross-process lock or a single owner process | medium |
| 21 | `completion.py:119,123,125`; `bench/jobbench.py:102,218,241` | `read_text(errors=...)` without `encoding` | Locale codec on Windows | low |
| 22 | `multimodal.py:23`; `spend.py:16` | `Path(__file__).resolve().parent / "data"` for packaged data | Works in a normal wheel; breaks in a zip-imported or frozen single-file layout unless data is bundled (INFERRED) | low |
| 23 | `egress_proxy.py:92` | `select.select(sockets...)` | Valid on Windows for sockets only; fine here (sockets) | low |
| 24 | `spend_proxy.py:183-186,194`; `egress_proxy.py:154-157,165` | `ThreadingHTTPServer` bound to `127.0.0.1`, port 0 or 8765/8766 | See Section 6. `egress_proxy` help text allows `0.0.0.0` "inside the egress sidecar container" | high |
| 25 | `llm.py:15-17`; `nim.py:137-139,169`; `spend_proxy.py:205` | credentials read from environment variables | No native credential store; keys live in process env (Section 6) | high |
| 26 | `llm.py:67`; `nim.py:212` | `urllib.request.urlopen` | Honours OS proxy settings (Windows registry, macOS SystemConfiguration) and the OS/OpenSSL trust store; behaviour differs per platform and a bundled interpreter may lack a CA bundle on macOS (INFERRED) | medium |
| 27 | `bench/capabilities.py:23` | `command -v X >/dev/null 2>&1 && echo yes` run in the container | POSIX shell syntax and `/dev/null`; container-only | n/a (bench only) |
| 28 | `h2/validate.py:13,49`; `h2/artifact.py:72` | `PurePosixPath`, `as_posix()` for logical tree paths | Correctly OS-neutral (positive finding) | none |
| 29 | `tests/test_command_policy.py:37-72,80-87`; `tests/test_tool_parity.py:329-331`; `tests/test_spend.py:286,289` | tests assume `/tmp/...`, `/bin/sh` shebang, `chmod 0o755`, symlinks, mode bits `0o640` | Would fail or need skips on Windows (INFERRED) | medium |

Absent (grep over `agent_harness/` and `tests/`): `fcntl`, `flock`, `resource`/`setrlimit`/`RLIMIT_*`, `os.fork`, `multiprocessing`, `set_start_method`, `signal.*`, `SIGKILL`/`SIGTERM`, `killpg`, `setsid`, `start_new_session`, `preexec_fn`, `setpgid`, `/proc`, `shell=True`, `os.system`/`popen`, `getuid`/`geteuid`, `umask`, `XDG_*`, `os.getlogin`, `ctypes`, `atexit`, `AF_UNIX`, `sys.platform`/`platform.system` branches, `seccomp`/`bwrap`/`unshare`/`landlock`. The package is therefore not fork- or resource-limit-dependent; its POSIX dependence is confined to the items above.

## 5. Process, cancellation and sandbox analysis

**What exists today**

- Tool execution path: `Agent.call_tool` (`agent.py:143`) goes through `ToolRegistry.run` (`tools.py:138-162`): authorizer, then `ApprovalPolicy.decide`, then the tool. `run_bash` (`agent.py:50-63`) asks `CommandPolicy.decide`, resolves argv, then `sandbox.run` (`sandbox.py:59-63`), which calls `subprocess.run([...], shell=False, cwd=, env=, timeout=, capture_output=True, text=True)` (`sandbox.py:40-48`). No shell is used (`shell=False`), which is good for portability.
- `CommandPolicy` is a content check: allowlist of executables, metacharacter rejection (`:30,157`), path containment (`:125-145`), interpreter inline-code flags (`:46-55`), and a "written file may not be executed" set (`:92-123`). Default allowlist is `echo ls cat pwd true false` (`:80-82`). It states that it is not isolation (`:20-21`).
- `LocalSandbox` (`openshell.py:124-179`) enforces a `SandboxPolicy` only at the contract boundary: it refuses a shell action when `allow_process_spawn` is false, or an action declaring `requires_network` while `allow_network` is false (`:152-160`), then runs the same soft runner. It does not restrict a spawned process's file or network access. `SandboxResult.sandbox_impl` is `"local-soft"` (`:130`). The `SandboxCapability` Protocol (`:114-121`) is backend-agnostic and is the natural seam for a native backend.
- Cancellation: `LocalSandbox.run` takes a `cancel` callable but only polls it before start and after the process returns (`openshell.py:148,165`); a running process is never interrupted. `run_loop` checks `max_seconds` between turns only (`loop.py:139`); there is no cancel token in the loop. `agent_harness.qa` has a cooperative `CancelToken` (`qa/hooks.py:132-146`) and deadline accounting through an injected clock (`qa/limits.py`); both rely on the adapter to stop work.
- Isolation that actually exists: only `bench/sandbox.py`, a Docker container with `--network none` or an `--internal` network plus an egress-filter container, `--cap-drop ALL`, `no-new-privileges`, `--memory`, `--cpus`, `--pids-limit`, a non-root user and a workspace mount (`:64-67,115-118`). It requires a Docker daemon and a Linux image (`agent-harness-office:1`, `bench/Dockerfile.office`). Its in-container timeout uses GNU `timeout` and the docstring describes the signal as going to the whole group (`:131-132`).
- Host-side network controls: `EgressProxy` host denylist and `NetPolicy`-based address checks (`egress_proxy.py`, `netpolicy.py`) and `safe_fetch`. These are "soft": a process that ignores proxy variables bypasses them (`egress_proxy.py:7-9`).

**Process-tree cleanup, native requirements**

| Platform | What the port needs | Notes |
|---|---|---|
| Windows | Spawn each tool with `CREATE_NEW_PROCESS_GROUP` and assign it to a Job Object created with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`; on timeout or cancel, `TerminateJobObject`, then close the handle; read stdout and stderr in threads with a deadline so a surviving grandchild cannot block the reader | `subprocess.run` cannot do this; needs a `ctypes` wrapper (stdlib) or an equivalent. Job Objects also give memory and process-count limits but are not a security boundary |
| macOS | `start_new_session=True`, then `os.killpg(pgid, SIGTERM)`, grace period, `SIGKILL`; reap with `wait` | A child that calls `setsid`/`setpgid` itself escapes; there is no cgroup equivalent. launchd does not sandbox. No reliable per-user fork cap (INFERRED) |
| Both | A `ProcessRunner` abstraction with `run(argv, cwd, env, timeout, cancel, limits) -> Result` where cancel is observed while the process runs; the tool call should record "tree terminated: yes/no" in the receipt so a leak is visible | Matches the plan's `process_runner` service |

**Capability detection**: none. There is no probe for bwrap, namespaces, seccomp, containers or a Docker daemon in the core (only `bench/selfcheck.py:37-49` checks `docker`). A native `host_capabilities` service is needed so that policy can ask "is there an isolation backend of strength X".

**Native fallback and fail-closed set.** Neither Job Objects nor macOS process groups confine file, network or syscalls, so the following must refuse natively unless a real backend reports the capability:
1. executing any file written during the session, or any interpreter given inline code (`-c`, `-e`, `--eval`);
2. any command outside a minimal allowlist, and any argument resolving outside the workspace root;
3. network access by a child process (the harness cannot enforce this; only declare and deny);
4. spawning child processes when `allow_process_spawn` is false (currently enforced only by refusing to start the first process);
5. reading environment-held credentials (children must start with an explicit allowlist, Section 6);
6. any `bypass` mode of `ApprovalPolicy` or `CommandPolicy` (`tools.py:78`, `command_policy.py:150-151`) when the host reports no isolation.

`SandboxResult` already has a typed `DENIED` terminal with `Failure.*` reasons (`openshell.py:39-56`); a native backend should reuse it so denials are evidence, not exceptions. `STATUS.md` and `SECURITY.md` already state that the sandbox is not an isolation boundary; any native installer text must keep that wording.

## 6. MCP, environment and transport analysis

- **MCP code in this repository: none.** `grep -rniE "\bmcp\b"` finds one hit, a sentence in a benchmark prompt string (`agent_harness/bench/jobbench.py:46`). There is no stdio transport, no JSON-RPC framing, no server lifecycle and no MCP child-process spawning. Consequences: no binary/text-mode or CRLF handling to review, no MCP env isolation to port; NAT-05 must build the MCP adapter (with an env allowlist) elsewhere or here. The tool contract (`ToolSpec`, `ToolPlugin`, `ToolResult`, `ToolRegistry`, `tools.py`) is the likely seam; its header comment says it is shared with an external package.
- **Environment inheritance into children: all of it.**
  - `sandbox.py:38`: `env = dict(os.environ)` then only `PATH` replaced.
  - `bench/evalkit.py:132,178`: `{**os.environ, "JUDGE_API_KEY": ..., "JUDGE_REASONING_EFFORT": ...}` to the judge subprocess.
  - `bench/harnesses/opencode.py:86` and `bench/harnesses/harness2.py:170`: `{**os.environ, **...}` to third-party CLIs. The design intent is that real keys live in the separate spend-proxy process and children get placeholders (`bench/harnesses/_common.py:26`: `"OPENAI_API_KEY": "dummy"`; `harness2.py:128`: `"unused-in-generation"`), but the inherited parent environment is not scrubbed, so a key exported in the launching shell still reaches them.
  - There is no env allowlist function anywhere in the package. The QA CI job runs under `env -i` (`.github/workflows/qa.yml`), which tests the suite's hermeticity, not the product.
- **Secret handling.** `guardrails.redact` is applied to sandbox output before it is stored (`openshell.py:170`); `SandboxResult` stores secret reference names only (`:99`); the spend proxy never logs bodies, headers or keys (`spend_proxy.py:10`, `log_message` override). `ToolRegistry.audit` keeps raw call arguments in memory (`tools.py:151,155`) with no redaction. Credentials are read from environment variables (`llm.py:15-17`, `nim.py:137-139`, `spend_proxy.py:205`); there is no credential-store abstraction and no Keychain or Credential Manager use.
- **Local listeners and IPC.**
  - `spend_proxy`: `ThreadingHTTPServer` on `127.0.0.1`, default port 8765 (`:183-186,194,211`), no caller authentication, injects `Authorization: Bearer <real key>` upstream (`:120`). Any local process of any user can spend up to the cap. It is documented as loopback only and not a general proxy (`:10`).
  - `egress_proxy`: loopback by default; `--host 0.0.0.0` is documented for use inside a container (`:165`), and `bench/sandbox.py:67` does exactly that on an internal Docker network.
  - No named pipe, Unix socket, token or session concept exists. The plan's authenticated local IPC contract is not present in this repo.
- **Stdio, CRLF and encodings.** Child output is captured as text with the locale encoding (`sandbox.py:47`); in-repo JSON writers set `encoding="utf-8"` in most places, with the gaps listed in Section 4 rows 8, 9, 18, 19, 21. QA canonicalisation and hashing use explicit UTF-8 and refuse floats (`qa/canon.py`); the QA determinism tests run subprocesses and pass on macOS (`tests/qa/test_determinism.py`, `test_evidence_determinism.py`).
- **Receipts and event chains.** `qa/receipts.py` and `qa/integrity.py` build and verify an in-memory hash chain; `MemoryBlobSink` and `MemoryEvidenceStream` are the shipped sinks (`docs/qa.md:73`). `docs/qa.md:239` states durable storage and retention deletion belong to the deployment. So atomic append, fsync, file locking and cross-platform durable evidence are not implemented here and are a NAT-02 platform-service concern. The only path-addressed store is `DirectorySpecStore`, which opens `<root>/<64 hex>.json` read-only in binary mode (`qa/specstore.py:80-112`) and is Windows-safe (no `:` in file names).

## 7. Test results on macOS

Environment: macOS (Darwin 27.0.0), Apple Silicon, APFS (case-insensitive default), Python 3.12.16 (Homebrew) in a throwaway venv with only pytest (and later Pillow); `APPROVAL_SECRET` unset; no network or Docker required. Commands run from a fresh clone of `main` at `f3c87b7`.

| Run | Result |
|---|---|
| `pytest -q` on main, pytest only | **3521 passed, 23 skipped, 0 failed** (91.6 s) |
| `pytest -q` on main, pytest + Pillow 13.0.0 | **3558 passed, 21 skipped, 0 failed** (85.1 s); 3,579 tests |
| QA gate as CI runs it: `env -i HOME=<tmp> PATH=/usr/bin:/bin PYTHONPATH=<repo> QA_STRICT_ENV=1 python tests/qa/ci_gate.py` | `collected=3075 skipped=0 allow-listed-patterns=0`; 3075 passed (29.6 s) |
| `pytest -q` on tag `v0.2.0` (archive export), pytest + Pillow | 3264 passed, 5 skipped, 0 failed (87.5 s) |

Skip causes (main, 21 with Pillow): missing optional packages `context_runtime` (4) and `harness2` (1); no real Harness2 run (1); no benchmark dataset or judge script (2); no Docker daemon or office image (2 in `test_selfcheck.py`, 7 gated by `AGENT_HARNESS_DOCKER_TESTS`); 3 gated by `AGENT_HARNESS_NET_TESTS` (need internet). None is an OS skip: there is no `sys.platform` or `platform` conditional anywhere in `tests/`. **No macOS-specific failure was found.** That is partly because the suite never exercises the macOS-specific hazards: no test uses a different-case path, a non-ASCII file name, or an interpreter on the allowlist beyond the cases in `tests/test_command_policy.py`.

Platform-sensitive tests (would need work on Windows, INFERRED): `tests/test_command_policy.py:37-72,80-87` (`/tmp/echo`, `#!/bin/sh`, `chmod 0o755`, `symlink_to`, `cat --file=/etc/hostname`), `tests/test_tool_parity.py:329-331` (mode `0o640` preserved), `tests/test_spend.py:286,289` (`/tmp/_unused_ledger.jsonl`), and QA subprocess tests that default `PATH` to `/usr/bin:/bin` (`tests/qa/test_determinism.py:37`, `test_contract_alignment.py:606`, `test_evidence_determinism.py:46`).

CI covers ubuntu-latest only. Documentation drift: `README.md` and `STATUS.md` still quote 159 passing tests; the suite now has about 3,500.

## 8. Risk-ranked change list (NAT-01 to NAT-05)

Effort figures are my guesses, in engineer-days, not measured.

| # | Sev | NAT | Location | Change and approach | Effort (guess) |
|---|---|---|---|---|---|
| 1 | blocker | 04 | `sandbox.py:40-55` | Replace `subprocess.run` with a `ProcessRunner` that owns the process tree: Job Object (Windows) and session plus `killpg` (macOS/Linux); reader threads with deadline; cancel observed while running; result records tree-kill outcome | 6-10 d |
| 2 | blocker | 04 | `command_policy.py:33,58-69` | Platform-aware resolver: Windows allowlist of absolute executables (no bare-name search), executability by extension, refuse `.bat`/`.cmd` unless explicitly pinned; keep the Linux/macOS list | 3-5 d |
| 3 | blocker | 04, 05 | whole repo | Decide where MCP lives: no MCP code exists here. Define the transport adapter (stdio framing in binary mode, UTF-8, no text translation) and server lifecycle in the tool layer, with the env allowlist built in from day one | 8-15 d (greenfield) |
| 4 | high | 04 | `command_policy.py:72-73,89-123,140-170`; `agent.py:22-32` | Single `PathGuard` service: canonicalise with case-fold on case-insensitive volumes (detect per volume), `normcase`, strip `\\?\`, reject alternate data streams, reserved names and trailing dot or space, handle drive roots; compare components, not string prefixes; use for both containment and the "written file" set. Add tests with different-case paths | 4-6 d |
| 5 | high | 05 | `sandbox.py:38`; `bench/evalkit.py:132,178`; `bench/harnesses/*.py` | `child_env(allow=[...])` helper: start from an explicit allowlist (`PATH`, `TEMP`, `SystemRoot`, `HOME`/`USERPROFILE` as needed), never `os.environ`; deny-by-default for names matching `*KEY*`, `*TOKEN*`, `*SECRET*`; test that a planted secret does not reach a child | 2-3 d |
| 6 | high | 02, 05 | `spend_proxy.py:183-221`; `egress_proxy.py:154-179` | If the proxies stay in the product: per-launch random bearer token required on every request (constant-time compare), loopback only, token handed to children over a protected channel. If bench-only: exclude from native builds | 3-4 d |
| 7 | high | 02, 04 | `llm.py:15-17`; `nim.py:137-139`; `spend_proxy.py:205` | Introduce `credential_store` injection (Keychain, Credential Manager/DPAPI, env fallback only when explicitly enabled); keys must not appear in child env, trace or `ToolRegistry.audit` | 4-6 d |
| 8 | high | 04 | `openshell.py:124-179` | Add a native `SandboxCapability` implementation that reports its guarantees honestly, plus a `host_capabilities` probe; make `bypass` and interpreter-inline modes refuse when no isolation backend is reported | 4-6 d |
| 9 | high | 04 | `command_policy.py:157-160`; `sandbox.py:30` | Windows argv handling: take argv lists only (no command strings) or use a Windows-aware splitter; drop `shlex` POSIX mode on win32; extend the metacharacter set | 2-3 d |
| 10 | medium | 01 | `agent.py:39,45`; `sandbox.py:47`; `completion.py:119-125`; `bench/jobbench.py:102,218,241` | Explicit `encoding="utf-8"` (and `errors="replace"` for child output), `newline=""` where bytes matter | 1-2 d |
| 11 | medium | 01 | `bench/edit.py:63-70`; `bench/tools_parity.py:132-136,221-233`; `h2/artifact.py:46-55` | `atomic_write` / `atomic_replace_dir` helper with bounded retry on `PermissionError`, fsync of file and directory (POSIX), no `chmod 0o777` | 2-3 d |
| 12 | medium | 01, 02 | `spend.py:82-118` | `file_lock` service for the ledger (and any evidence store), or a single-writer process; test with two processes | 2-3 d |
| 13 | medium | 02 | `qa/hooks.py`, `qa/blobs.py` sinks | Provide durable `BlobSink` and `EvidenceStream` using `data_dir`, with append-only fsync'd segments and hash-chain verification on open; map `sha256:<hex>` digests to `sha256-<hex>` file names | 5-8 d |
| 14 | medium | 04 | `tools.py:138-162` | Per-tool timeout and cancel hook in `ToolRegistry.run`; today a hung tool hangs the loop (`loop.py:139` checks time only between turns) | 2-3 d |
| 15 | medium | 04 | `tools.py:151,155` | Redact `args` before storing in `audit`, or store a digest | 1 d |
| 16 | medium | 01 | `llm.py:67`; `nim.py:212` | Document and test proxy and trust-store behaviour per OS; ship `certifi` or use the OS store explicitly for a bundled interpreter | 2-3 d |
| 17 | medium | 09 | `.github/workflows/*.yml` | Add windows-latest and macos-14 (arm64) jobs for the portable suites; mark Docker, network and POSIX-path tests with explicit markers | 2-3 d |
| 18 | medium | 01 | tests listed in Section 7 | Replace hard-coded `/tmp`, `/bin/sh`, mode-bit assertions with `tmp_path` and capability markers | 2-3 d |
| 19 | medium | 08 | `agent_harness/data/*` | Verify the wheel includes `data/` (hatchling default, INFERRED) and that a frozen layout still finds it (`multimodal.py:23`, `spend.py:16`) | 1 d |
| 20 | low | 01 | `trace.py:57,69`; `loop.py:118`; `spend.py:117` | `newline="\n"` for JSONL writers so on-disk bytes are identical across platforms | 0.5 d |
| 21 | low | 04 | `agent.py:30` | Use a component-wise `commonpath` check (drive-root bug) | 0.5 d |
| 22 | low | 04 | `agent.py:57`; `command_policy.py:88-90` | `AGENT_HARNESS_ROOT`/cwd as default root is ambient state; require an explicit root in native mode | 1 d |
| 23 | low | 11 | `README.md:3-7`; `STATUS.md` | Stale test counts (159 vs about 3,500); NVIDIA Inception badge and text in README are third-party marks, not needed in installers or app resources | 0.5 d |
| 24 | low | 08 | repo | Add a lockfile and SBOM generation; pin the build backend | 1-2 d |
| 25 | low | 01 | `bench/*` | Mark `bench` as non-native (excluded from wheels used by the native distribution, or behind an extra) | 1 d |

## 9. Unsupported-features register

| # | Feature | Where | Native status | Fail-closed behaviour required |
|---|---|---|---|---|
| 1 | Docker sandbox and egress gateway | `bench/sandbox.py`, `bench/selfcheck.py` | Unsupported natively; requires Docker daemon and Linux image | Return "isolation unavailable"; never fall back to unsandboxed execution for untrusted code |
| 2 | Linux office image (`agent-harness-office:1`) | `bench/Dockerfile.office` | Not shippable natively | n/a |
| 3 | OpenCode and Harness2 benchmark adapters | `bench/harnesses/opencode.py`, `harness2.py` | Upstream docs say it assumes Linux (macOS needs GNU `timeout`); third-party code, not audited | Exclude from native builds |
| 4 | Benchmark runners and judge flows | `bench/*.py` | Docker- and dataset-bound; skipped on macOS | Exclude |
| 5 | Run-time resource limits (CPU, memory, pids) | none in core (container flags only, `bench/sandbox.py:115-118`) | No native equivalent in core | Job Object limits on Windows are available; on macOS only soft per-process `RLIMIT_*`; report as unsupported |
| 6 | Syscall or filesystem confinement of a child | none | Not provided on any platform here | Deny actions that need it |
| 7 | Network confinement of a child | `egress_proxy.py` is advisory | Cannot be enforced natively without OS firewall rights | Deny tools that require network unless the user opts in; never claim enforcement |
| 8 | MCP client and server | none | Not present | Not applicable until built |
| 9 | Authenticated local IPC | none | Not present | Sidecar must not start without caller authentication |
| 10 | Native credential store | none | Not present | Refuse to read credentials from child-visible env |
| 11 | Durable evidence store and retention deletion | `docs/qa.md:239` | Not implemented (memory sinks) | Run records must say evidence is not durable |
| 12 | Cross-process file locking | none | Not present | Single-writer rule until a lock service exists |
| 13 | Process-tree termination and live cancellation | `sandbox.py`, `openshell.py:148,165` | Not present | A tool that cannot be killed as a tree must not be launched in native mode |
| 14 | Windows-style path handling, long paths, ADS, reserved names | `command_policy.py`, `agent.py` | Not handled | Deny paths with unsupported forms |

## 10. Licence notes

- `LICENSE` is the unmodified GNU AGPL v3 text; `pyproject.toml:7` declares `AGPL-3.0-or-later`. There is no Commons Clause text in this repository: the README table lists agent-harness as "AGPL-3.0 text (`LICENSE`)" and another repository as "AGPL-3.0-or-later with Commons Clause" (`README.md:93,95`), and `docs/qa.md:302-306` says the QA module imports no Commons-Clause code. So this repository would ship under plain AGPL-3.0-or-later, not the family licence, unless relicensed (a decision outside this audit).
- Only 28 of 86 `.py` files carry an `SPDX-License-Identifier` header (all in `qa/`); the other 58 rely on the repository licence. There is no `NOTICE` file and no per-file copyright headers.
- Nothing found blocks signed installers: zero runtime dependencies means no third-party licence obligation on the runtime path. Build-time only: `pathspec` is MPL-2.0 (not redistributed). If Pillow is bundled, include the MIT-CMU notice and a list of its vendored libraries.
- The README carries NVIDIA Inception Program wording and badge (`README.md:3-7`) with a non-endorsement sentence; not appropriate to ship inside an installer or app resources.
- `SECURITY.md` points to an email address for reports; the policy says the sandbox is not an isolation boundary and that guardrail or approval bypasses are in scope.
- Signing considerations (INFERRED): pure Python with no native binaries, so there is nothing of this package to sign or notarise individually; a bundled interpreter is what needs Developer ID signing and hardened runtime on macOS, and Authenticode on Windows. Writing a script at runtime and then executing it (`write_file` then run) is the behaviour antivirus heuristics flag; the "written file may not be run" rule is a policy, not a mitigation for quarantine.

## 11. Open questions

1. Where will MCP client and server code live? It is absent here, so NAT-05's scope depends on which repository owns it.
2. Is the shell-running agent tool (`run_bash`) in scope for the native core, or only the tool contract and approval gate? If only the latter, blockers 1 and 2 shrink to "disable `run_bash` natively".
3. Are the spend and egress proxies part of the shipped product or benchmark-only? This decides change item 6.
4. Which isolation backend, if any, may the native distribution rely on (user-installed container runtime, restricted tokens, none)? This decides the fail-closed set in Section 5.
5. Should `bench/` be excluded from the native wheel (extra or separate distribution)?
6. Case-insensitive path policy: treat all macOS and Windows volumes as case-insensitive, or detect per volume?
7. Is agent-harness meant to be relicensed under the family licence before it is bundled, given `docs/qa.md:305-306` leaves that open?
8. Windows runtime behaviour of everything marked INFERRED needs a Windows host; none was available.
9. Wheel availability was checked for CPython 3.12 only.

## 12. Pointer

One finding with policy-bypass character is recorded privately with the maintainers, not here.
