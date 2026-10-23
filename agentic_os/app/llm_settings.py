"""Model-provider settings an operator (or Sidekick, on the operator's instruction) can change WITHOUT a restart.

Two things are kept apart on purpose:

* the **settings** (provider base URL, model name, which hosts count as inside the trust boundary, and a
  *reference* to the key) live in a small non-secret JSON file named by ``REDEVOPS_LLM_SETTINGS_FILE`` and are
  re-read on every model call;
* the **key itself** lives only in a secret store (Vault, KeePassXC, OS keyring, encrypted/plain file), resolved
  through the credential broker on every call. The settings file holds only the reference (``vault:llm/provider``).

With no settings file the environment is used exactly as before (REDEVOPS_LLM_BASE_URL, REDEVOPS_LLM_MODEL,
REDEVOPS_LLM_IN_BOUNDARY_HOSTS and the key from OPENAI_API_KEY), so existing deployments are unchanged.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from typing import Tuple
from urllib.parse import urlparse

SETTINGS_FILE_ENV = "REDEVOPS_LLM_SETTINGS_FILE"
DEFAULT_MODEL = "DeepSeek-V4-Flash"


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class LLMSettings:
    base_url: str = ""
    model: str = DEFAULT_MODEL
    key_ref: str = ""                      # "backend:namespace/path"; empty = use OPENAI_API_KEY from the environment
    in_boundary_hosts: Tuple[str, ...] = ()
    source: str = "env"                    # "env" | "file"


def _env_settings() -> LLMSettings:
    hosts = tuple(h.strip().lower() for h in os.environ.get("REDEVOPS_LLM_IN_BOUNDARY_HOSTS", "").split(",") if h.strip())
    return LLMSettings(base_url=os.environ.get("REDEVOPS_LLM_BASE_URL", "") or "",
                       model=os.environ.get("REDEVOPS_LLM_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL,
                       key_ref="", in_boundary_hosts=hosts, source="env")


def load_llm_settings(path: "str | None" = None) -> LLMSettings:
    path = (path if path is not None else os.environ.get(SETTINGS_FILE_ENV, "")).strip()
    if not path or not os.path.exists(path):
        return _env_settings()
    mode = os.stat(path).st_mode
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        # a group/world-writable settings file could redirect model traffic: refuse it, use the environment
        return _env_settings()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return _env_settings()
    if not isinstance(d, dict):
        return _env_settings()
    env = _env_settings()
    hosts = d.get("in_boundary_hosts") or []
    return LLMSettings(
        base_url=str(d.get("base_url") or env.base_url), model=str(d.get("model") or env.model),
        key_ref=str(d.get("key_ref") or ""),
        in_boundary_hosts=tuple(sorted({str(h).strip().lower() for h in hosts if str(h).strip()} | set(env.in_boundary_hosts))),
        source="file")


def save_llm_settings(path: str, *, base_url: str, model: str, key_ref: str = "",
                      in_boundary_hosts: Tuple[str, ...] = ()) -> LLMSettings:
    """Validate and atomically write the settings (0600). Holds a key REFERENCE, never a key."""
    u = urlparse(base_url or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise SettingsError("base_url must be an http(s) URL")
    if not (model or "").strip():
        raise SettingsError("model is required")
    if key_ref:
        from agentic_os.secrets.resolve import parse_ref  # noqa: PLC0415
        parse_ref(key_ref)                                  # raises SecretReferenceError on a bad reference
    for h in in_boundary_hosts:
        if not h or "/" in h or " " in h:
            raise SettingsError(f"not a host name: {h!r}")
    data = {"base_url": base_url.rstrip("/"), "model": model.strip(), "key_ref": key_ref,
            "in_boundary_hosts": sorted({h.strip().lower() for h in in_boundary_hosts})}
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".llm-settings-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)                               # atomic: a reader sees the old or the new, never half
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return LLMSettings(base_url=data["base_url"], model=data["model"], key_ref=key_ref,
                       in_boundary_hosts=tuple(data["in_boundary_hosts"]), source="file")


def llm_api_key(settings: LLMSettings) -> str:
    """The key for THIS call: from the secret reference when one is set (resolved through the broker now, so a
    rotation is live), else OPENAI_API_KEY from the environment, else the local-server sentinel ``EMPTY``."""
    if settings.key_ref:
        from agentic_os.secrets.resolve import read_secret  # noqa: PLC0415
        value = read_secret(settings.key_ref)
        if value:
            return value
        return "EMPTY"            # reference set but unreadable: never fall back to a different credential
    return os.environ.get("OPENAI_API_KEY", "EMPTY") or "EMPTY"


__all__ = ["LLMSettings", "SettingsError", "SETTINGS_FILE_ENV", "load_llm_settings", "save_llm_settings", "llm_api_key"]
