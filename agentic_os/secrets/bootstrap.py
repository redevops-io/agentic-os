"""Auto-generate and store the platform's managed secrets (see docs/SECRETS.md).

Precedence when reading: process env > secrets file > OS keyring > Vault (if configured). ``init`` skips any
secret present in any of them and NEVER overwrites an existing value. Values are never logged or printed here.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .managed import MANAGED, OUTREACH, ManagedSecret

log = logging.getLogger("agentic_os.secrets")

BEGIN = "# >>> redevops secrets (managed) >>>"
END = "# <<< redevops secrets (managed) <<<"
KEYRING_SERVICE = "redevops"
# Live bundle: KV v2 mount `vibexgen`, path `agentic-os/stack` (API path vibexgen/data/agentic-os/stack).
DEFAULT_VAULT_MOUNT = "vibexgen"
DEFAULT_VAULT_PATH = "agentic-os/stack"


class SecretsError(RuntimeError):
    pass


class VaultCasConflict(SecretsError):
    pass


# ── locations ──────────────────────────────────────────────────────────────────────────────────────────

def _platform(platform: Optional[str] = None) -> str:
    p = platform or sys.platform
    return "windows" if p.startswith(("win", "cygwin")) else ("macos" if p == "darwin" else "linux")


def secrets_file_path(environ: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get("REDEVOPS_SECRETS_FILE"):
        return Path(env["REDEVOPS_SECRETS_FILE"])
    if _platform(platform) == "windows":
        base = env.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "redevops" / "secrets.env"
    home = env.get("HOME") or str(Path.home())
    return Path(home) / ".config" / "redevops" / "secrets.env"


def parse_dotenv(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
    return out


def read_secrets_file(path: Optional[Path] = None) -> Dict[str, str]:
    """Read-only; never raises (used on the approval hot path)."""
    try:
        p = path or secrets_file_path()
        return parse_dotenv(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_file_values(path: Path, updates: Mapping[str, str], windows: bool = False,
                       runner: Optional[Callable] = None) -> None:
    created = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if created and not windows:
        os.chmod(path.parent, 0o700)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    pending = dict(updates)
    out: List[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip().removeprefix("export ").strip() if "=" in line else None
        if key in pending and not line.lstrip().startswith("#"):
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(line)
    if not out:
        out.append("# redevops managed secrets - KEY=value, compose --env-file compatible. Keep private.")
    out.extend(f"{k}={v}" for k, v in pending.items())
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, ("\n".join(out) + "\n").encode())
    finally:
        os.close(fd)
    if not windows:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    if windows and runner:
        user = os.environ.get("USERNAME", "")
        runner(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"], None)


# ── shell profile ──────────────────────────────────────────────────────────────────────────────────────

def shell_block(file_expr: str) -> str:
    return "\n".join([
        BEGIN,
        "# Loads the private (0600) redevops secrets file. Values are NOT stored in this file.",
        f'{{ f="{file_expr}"; [ -f "$f" ] && {{ set -a; . "$f"; set +a; }}; unset f; }}',
        END, ""])


def rc_files(home: Path, platform: str) -> List[Path]:
    if platform == "linux":
        return [home / ".bashrc"]
    if platform == "macos":
        rcs = [home / ".zshrc"]
        if (home / ".bash_profile").exists():
            rcs.append(home / ".bash_profile")
        return rcs
    return []


def install_shell_block(rc: Path, block: str) -> str:
    """Add or replace the single managed block. Returns 'added'|'updated'|'unchanged'."""
    text = rc.read_text(encoding="utf-8") if rc.exists() else ""
    pat = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)
    if pat.search(text):
        new = pat.sub(lambda _m: block, text, count=1)
        status = "updated"
    else:
        new = text + ("" if not text or text.endswith("\n") else "\n") + ("\n" if text else "") + block
        status = "added"
    if new == text:
        return "unchanged"
    bak = rc.with_name(rc.name + ".bak-redevops")
    if rc.exists() and not bak.exists():
        bak.write_text(text, encoding="utf-8")
    rc.write_text(new, encoding="utf-8")
    return status


# ── vault ──────────────────────────────────────────────────────────────────────────────────────────────

class VaultBundle:
    """Read-merge-write of ONE KV v2 secret holding all keys (KV2 write replaces the whole secret)."""

    def __init__(self, addr: str, token: str, mount: str, path: str, transport: Optional[Callable] = None):
        from .stores import _urllib_transport
        self.addr, self.token, self.mount, self.path = addr.rstrip("/"), token, mount, path.strip("/")
        self._t = transport or _urllib_transport

    @property
    def url(self) -> str:
        return f"{self.addr}/v1/{self.mount}/data/{self.path}"

    def _h(self) -> Dict[str, str]:
        return {"X-Vault-Token": self.token, "Content-Type": "application/json"}

    def read(self) -> Dict[str, str]:
        try:
            resp = self._t("GET", self.url, self._h(), None)
        except Exception as e:  # noqa: BLE001
            if getattr(e, "code", None) == 404:
                return {}
            raise SecretsError(f"Vault is configured ({self.addr}) but unreachable or denied read "
                               f"({type(e).__name__}); not falling back to local storage.") from None
        return {str(k): str(v) for k, v in (((resp or {}).get("data") or {}).get("data") or {}).items()}

    def read_versioned(self) -> Tuple[Dict[str, str], int]:
        """(data, current KV v2 version); version 0 means the secret does not exist yet (CAS create-only)."""
        try:
            resp = self._t("GET", self.url, self._h(), None)
        except Exception as e:  # noqa: BLE001
            if getattr(e, "code", None) == 404:
                return {}, 0
            raise SecretsError(f"Vault is configured ({self.addr}) but unreachable or denied read "
                               f"({type(e).__name__}); not falling back to local storage.") from None
        d = (resp or {}).get("data") or {}
        try:
            ver = int((d.get("metadata") or {}).get("version") or 0)
        except (TypeError, ValueError):
            ver = 0
        return {str(k): str(v) for k, v in (d.get("data") or {}).items()}, ver

    def write(self, merged: Mapping[str, str], cas: Optional[int] = None) -> None:
        import json
        body: Dict[str, Any] = {"data": dict(merged)}
        if cas is not None:
            body["options"] = {"cas": cas}
        try:
            self._t("POST", self.url, self._h(), json.dumps(body).encode())
        except VaultCasConflict:
            raise
        except Exception as e:  # noqa: BLE001
            if cas is not None and getattr(e, "code", None) == 400:
                raise VaultCasConflict("Vault bundle changed since it was read (check-and-set mismatch)") from None
        except Exception as e:  # noqa: BLE001
            raise SecretsError(f"Vault write failed ({type(e).__name__}); nothing was stored locally.") from None


# ── runner / keyring ───────────────────────────────────────────────────────────────────────────────────

def _default_runner(argv: List[str], env: Optional[Mapping[str, str]]) -> int:
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(argv, env=e, capture_output=True, timeout=30).returncode


def _default_keyring() -> Any:
    if os.environ.get("REDEVOPS_SECRETS_NO_KEYRING") == "1":
        return None
    try:
        import keyring  # noqa: PLC0415
        return keyring
    except Exception:  # noqa: BLE001
        return None


@dataclass
class SecretResult:
    name: str
    status: str            # generated | already-set | kept(user-provided) | weak
    where: List[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Bootstrap:
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    platform: Optional[str] = None
    keyring: Any = "default"
    vault_transport: Optional[Callable] = None
    runner: Callable = _default_runner
    home: Optional[Path] = None

    def __post_init__(self) -> None:
        self.plat = _platform(self.platform)
        if self.keyring == "default":
            self.keyring = _default_keyring()
        self.home = self.home or Path(self.environ.get("HOME") or Path.home())

    # -- config
    @property
    def file(self) -> Path:
        return secrets_file_path(self.environ, self.platform)

    def vault_configured(self) -> bool:
        return bool(self.environ.get("VAULT_ADDR") and self.environ.get("VAULT_TOKEN"))

    def vault(self) -> VaultBundle:
        return VaultBundle(self.environ["VAULT_ADDR"], self.environ["VAULT_TOKEN"],
                           self.environ.get("VAULT_MOUNT", DEFAULT_VAULT_MOUNT),
                           self.environ.get("REDEVOPS_VAULT_PATH", DEFAULT_VAULT_PATH), self.vault_transport)

    def vault_location(self) -> str:
        v = self.vault()
        return f"vault:{v.mount}/{v.path}"

    # -- reading
    def _kr_get(self, name: str) -> Optional[str]:
        if not self.keyring:
            return None
        try:
            return self.keyring.get_password(KEYRING_SERVICE, name) or None
        except Exception:  # noqa: BLE001
            return None

    def locate(self, names, vault_data: Optional[Dict[str, str]] = None) -> Dict[str, List[Tuple[str, str]]]:
        """name -> [(source, value)] in precedence order env, file, keyring, vault."""
        fdata = read_secrets_file(self.file)
        out: Dict[str, List[Tuple[str, str]]] = {}
        for n in names:
            hits: List[Tuple[str, str]] = []
            if self.environ.get(n):
                hits.append(("env", self.environ[n]))
            if fdata.get(n):
                hits.append(("file", fdata[n]))
            kv = self._kr_get(n)
            if kv:
                hits.append(("keyring", kv))
            if vault_data and vault_data.get(n):
                hits.append(("vault", vault_data[n]))
            out[n] = hits
        return out

    def resolve(self, name: str) -> Tuple[Optional[str], List[str]]:
        vd = self.vault().read() if self.vault_configured() else None
        hits = self.locate([name], vd)[name]
        return (hits[0][1] if hits else None), [h[0] for h in hits]

    # -- init
    def init(self, backend: str = "auto", dry_run: bool = False, shell_profile: bool = True) -> Dict[str, Any]:
        use_vault = backend == "vault" or (backend == "auto" and self.vault_configured())
        if use_vault and not self.vault_configured():
            raise SecretsError("--backend vault needs VAULT_ADDR and VAULT_TOKEN to be set.")
        vdata: Optional[Dict[str, str]] = None
        if self.vault_configured() and backend != "file" or use_vault:
            vdata = self.vault().read()
        found = self.locate(MANAGED, vdata)
        results: List[SecretResult] = []
        fresh: Dict[str, str] = {}
        for name, m in MANAGED.items():
            hits = found[name]
            if hits:
                srcs = [h[0] for h in hits]
                ok, why = m.validate(hits[0][1])
                st = "already-set" if any(s != "env" for s in srcs) else "kept(user-provided)"
                if not ok:
                    results.append(SecretResult(name, "weak", srcs, f"existing value kept but weak: {why}"))
                else:
                    results.append(SecretResult(name, st, srcs))
            else:
                fresh[name] = m.generate()
                results.append(SecretResult(name, "generated", []))
        actions: List[str] = []
        loc = self.vault_location() if use_vault else str(self.file)
        if fresh and not dry_run:
            if use_vault:
                merged = dict(vdata or {})
                for k, v in fresh.items():
                    merged.setdefault(k, v)
                self.vault().write(merged)
            else:
                _write_file_values(self.file, fresh, self.plat == "windows", self.runner)
                if self.plat == "windows":
                    for k, v in fresh.items():
                        self.runner(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                     "[Environment]::SetEnvironmentVariable($env:RDV_N,$env:RDV_V,'User')"],
                                    {"RDV_N": k, "RDV_V": v})
                self._mirror_keyring(fresh)
        for r in results:
            if r.status == "generated":
                r.where = [loc]
        if shell_profile and not use_vault and self.plat != "windows":
            expr = str(self.file) if self.environ.get("REDEVOPS_SECRETS_FILE") else \
                f"${{HOME}}/.config/redevops/secrets.env"
            for rc in rc_files(self.home, self.plat):
                if dry_run:
                    actions.append(f"would ensure managed block in {rc}")
                else:
                    actions.append(f"{rc}: {install_shell_block(rc, shell_block(expr))}")
        return {"results": results, "location": loc, "backend": "vault" if use_vault else "file",
                "actions": actions, "dry_run": dry_run}

    def _mirror_keyring(self, vals: Mapping[str, str]) -> None:
        if not self.keyring:
            return
        for k, v in vals.items():
            try:
                self.keyring.set_password(KEYRING_SERVICE, k, v)
            except Exception:  # noqa: BLE001
                log.debug("keyring mirror skipped for %s", k)

    # -- push (local -> Vault, add-only)
    def push(self, only=None, dry_run: bool = False, attempts: int = 3) -> Dict[str, Any]:
        """Copy managed secrets that exist locally (env/file/keyring) into Vault, adding ONLY keys missing
        there. Read-merge-write with check-and-set on the version read; never overwrites or drops keys."""
        if not self.vault_configured():
            raise SecretsError("push needs Vault: set VAULT_ADDR and VAULT_TOKEN (no local fallback).")
        names = list(only) if only else list(MANAGED)
        unknown = [n for n in names if n not in MANAGED]
        if unknown:
            raise SecretsError(f"unknown managed secret(s): {', '.join(unknown)}; known: {', '.join(MANAGED)}")
        b = self.vault()
        for attempt in range(attempts):
            vdata, ver = b.read_versioned()
            local = self.locate(names)
            added: List[str] = []
            skipped_present: List[str] = []
            missing_local: List[str] = []
            merged = dict(vdata)
            for n in names:
                if vdata.get(n):
                    skipped_present.append(n)
                elif local[n]:
                    merged[n] = local[n][0][1]
                    added.append(n)
                else:
                    missing_local.append(n)
            if not added or dry_run:
                break
            try:
                b.write(merged, cas=ver)
                break
            except VaultCasConflict:
                if attempt == attempts - 1:
                    raise SecretsError("Vault bundle kept changing (check-and-set conflict); nothing was "
                                       "overwritten. Re-run push.") from None
        return {"location": self.vault_location(), "added": added, "skipped_present": skipped_present,
                "missing_local": missing_local, "dry_run": dry_run}

    # -- rotate
    def rotate(self, name: str) -> str:
        m = MANAGED.get(name)
        if not m:
            raise SecretsError(f"unknown managed secret '{name}'")
        new = m.generate()
        if self.vault_configured() and not read_secrets_file(self.file).get(name):
            b = self.vault()
            merged = b.read()
            merged[name] = new
            b.write(merged)
        else:
            _write_file_values(self.file, {name: new}, self.plat == "windows", self.runner)
            if self.plat == "windows":
                self.runner(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                             "[Environment]::SetEnvironmentVariable($env:RDV_N,$env:RDV_V,'User')"],
                            {"RDV_N": name, "RDV_V": new})
        if self._kr_get(name):
            self._mirror_keyring({name: new})
        return new

    def status_rows(self) -> List[Dict[str, Any]]:
        vd = None
        if self.vault_configured():
            try:
                vd = self.vault().read()
            except SecretsError:
                vd = None
        rows = []
        for name, hits in self.locate(MANAGED, vd).items():
            if hits:
                ok, why = MANAGED[name].validate(hits[0][1])
                rows.append({"name": name, "present": True, "where": [h[0] for h in hits],
                             "valid": ok, "reason": why})
            else:
                rows.append({"name": name, "present": False, "where": [], "valid": False, "reason": "missing"})
        return rows


def basic_header(value: str) -> str:
    return "Authorization: Basic " + base64.b64encode(value.encode()).decode()


def ensure_secrets(**kw: Any) -> Optional[Dict[str, Any]]:
    """Entrypoint hook: init once, non-fatally. Exports file/vault values into this process's env (never
    overriding existing env). Disabled with REDEVOPS_SECRETS_AUTO=0."""
    if os.environ.get("REDEVOPS_SECRETS_AUTO", "1") == "0":
        return None
    try:
        b = Bootstrap(**kw)
        res = b.init()
        for name in MANAGED:
            if not os.environ.get(name):
                val, _ = b.resolve(name)
                if val:
                    os.environ[name] = val
        log.info("redevops secrets ready (%s)", res["location"])
        return res
    except Exception as e:  # noqa: BLE001
        log.warning("redevops secrets bootstrap failed: %s. Run `agentic-os secrets init`.", e)
        return None
