"""Change the model provider or its key while the stack runs (the operation a shell such as Sidekick performs when
its user says "use my new key" / "switch to the local model").

Every change is: write the secret to the store -> write the settings (a reference, never a value) -> TEST with a
minimal call -> keep it, or ROLL BACK to exactly what was there before. The running agents pick the change up on
their next call (see llm_settings), so nothing is reinstalled or restarted.

Moving traffic to a host that is not inside the trust boundary is a DATA-BOUNDARY decision: it is refused unless the
caller passes ``allow_external_host=True`` (the shell must obtain the user's explicit approval first), and the result
says which data classes may now leave. The function never returns or logs a key; it reports a masked descriptor."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from .llm_settings import LLMSettings, load_llm_settings, save_llm_settings


class ProviderChangeRefused(PermissionError):
    pass


def _host(base_url: str) -> str:
    return (urlparse(base_url).hostname or "").lower()


def _is_inside(base_url: str, extra_hosts: Tuple[str, ...] = ()) -> bool:
    from .llm import _is_in_boundary_host  # noqa: PLC0415
    host = _host(base_url)
    return host in {h.lower() for h in extra_hosts} or _is_in_boundary_host(base_url)


def test_provider(settings: LLMSettings, key: Optional[str] = None, *, timeout: float = 20.0) -> Dict[str, Any]:
    """One minimal chat completion against ``settings``. Reports ok / HTTP status / error CLASS only (never a body
    or a key)."""
    from .llm_settings import llm_api_key  # noqa: PLC0415
    api_key = key if key is not None else llm_api_key(settings)
    url = settings.base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({"model": settings.model, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "ping"}]}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:                      # noqa: S310 - operator-set URL
            return {"ok": 200 <= r.status < 300, "status": r.status}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": "http_error"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": 0, "error": type(e).__name__}


def _masked(value: Optional[str]) -> str:
    return "" if not value else f"set (…{value[-4:]})" if len(value) >= 12 else "set"


def set_provider(settings_path: str, *, base_url: str, model: str, key_ref: str = "", key_value: Optional[str] = None,
                 in_boundary_hosts: Tuple[str, ...] = (), allow_external_host: bool = False,
                 test: bool = True, **store_cfg: Any) -> Dict[str, Any]:
    """Point the stack at ``base_url``/``model`` (and store ``key_value`` at ``key_ref`` when given). Returns a
    receipt. Raises ``ProviderChangeRefused`` for an external host without explicit approval."""
    from agentic_os.secrets.resolve import read_secret, store_secret  # noqa: PLC0415
    inside = _is_inside(base_url, in_boundary_hosts)
    if not inside and not allow_external_host:
        raise ProviderChangeRefused(
            f"{_host(base_url)} is outside your trust boundary: moving model traffic there needs your explicit "
            "approval (allow_external_host) and an allow-listed host")
    before = load_llm_settings(settings_path) if os.path.exists(settings_path) else None
    prev_key = read_secret(key_ref, **store_cfg) if (key_ref and key_value is not None) else None
    hosts = tuple(in_boundary_hosts) + ((_host(base_url),) if (not inside and allow_external_host) else ())
    if key_ref and key_value is not None:
        store_secret(key_ref, key_value, **store_cfg)
    new = save_llm_settings(settings_path, base_url=base_url, model=model, key_ref=key_ref,
                            in_boundary_hosts=hosts)
    outcome: Dict[str, Any] = {"ok": True, "status": None}
    if test:
        outcome = test_provider(new, key_value)
    if test and not outcome.get("ok"):
        # roll back to exactly what was there before
        if before is not None and before.source == "file":
            save_llm_settings(settings_path, base_url=before.base_url, model=before.model, key_ref=before.key_ref,
                              in_boundary_hosts=before.in_boundary_hosts)
        else:
            try:
                os.unlink(settings_path)
            except FileNotFoundError:
                pass
        if key_ref and key_value is not None and prev_key is not None:
            store_secret(key_ref, prev_key, **store_cfg)
        return {"changed": False, "rolled_back": True, "test": outcome, "key": _masked(key_value)}
    return {"changed": True, "rolled_back": False, "test": outcome, "provider_host": _host(base_url),
            "model": model, "key_ref": key_ref, "key": _masked(key_value),
            "boundary": "inside" if inside else "EXTERNAL (explicitly approved)",
            "note": ("" if inside else
                     "Model traffic now leaves your trust boundary for this host; privacy mode and data "
                     "classification still decide which data may be sent.")}


def rotate_key(settings_path: str, *, new_key: str, test: bool = True, **store_cfg: Any) -> Dict[str, Any]:
    """Replace the key behind the CURRENT settings' reference (same provider, same model), with test + rollback."""
    cur = load_llm_settings(settings_path)
    if cur.source != "file" or not cur.key_ref:
        raise ProviderChangeRefused("no stored key reference to rotate: set the provider with a key reference first")
    return set_provider(settings_path, base_url=cur.base_url, model=cur.model, key_ref=cur.key_ref, key_value=new_key,
                        in_boundary_hosts=cur.in_boundary_hosts, allow_external_host=True, test=test, **store_cfg)


__all__ = ["ProviderChangeRefused", "set_provider", "rotate_key", "test_provider"]
