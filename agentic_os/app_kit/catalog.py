"""Generate modules.yaml from AppManifests (plan §3.2).

The module catalog is derived from the manifests' deploy blocks, so it can't drift from them: the
``source`` is always ``apps/<app-name>`` (fixing the historical path drift where e.g. an
``agentic-support`` entry pointed at a non-existent ``apps/agentic-support``), and the capability /
approval truth lives with the app. Entries with no manifest (an external tool like ``sidekick``) are
passed through as ``extras``.
"""
from __future__ import annotations

from typing import Iterable, List, Mapping

from .manifest import AppManifest, DeploySpec

CATALOG_HEADER = (
    "# Agentic apps module catalog.\n"
    "# GENERATED from the app manifests' deploy blocks (agentic_os.app_kit.catalog). Do not hand-edit\n"
    "# the app entries — change the app's manifest.py deploy=DeploySpec(...) and regenerate.\n"
    "# App source lives in the agentic-os monorepo under apps/<name>.\n"
)


def module_entry(manifest: AppManifest) -> dict:
    """The modules.yaml entry for one app manifest. ``source`` is always apps/<app-name>."""
    d = manifest.deploy or DeploySpec()
    entry: dict = {
        "name": d.catalog_name or manifest.name,
        "source": f"apps/{manifest.name}",
        "pain": d.pain,
        "deploy": d.deploy,
    }
    if d.port is not None:
        entry["port"] = d.port
    if d.tagline:
        entry["tagline"] = d.tagline
    if d.agents:
        entry["agents"] = list(d.agents)
    if d.approval:
        entry["approval_required"] = list(d.approval)
    return entry


def render_modules_yaml(manifests: Iterable[AppManifest], *,
                        extras: Iterable[Mapping] = (), header: str = CATALOG_HEADER) -> str:
    """Render the full modules.yaml document (header + ``modules:`` list) from manifests + extras."""
    import yaml

    entries: List[dict] = [module_entry(m) for m in manifests]
    entries += [dict(e) for e in extras]
    body = yaml.safe_dump({"modules": entries}, sort_keys=False, default_flow_style=False,
                          allow_unicode=True, width=100)
    return f"{header}{body}"


__all__ = ["CATALOG_HEADER", "module_entry", "render_modules_yaml"]
