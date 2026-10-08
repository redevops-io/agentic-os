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


def _scalar(value) -> str:
    """Render a YAML scalar. Quote strings with characters the catalog's lightweight parsers choke on
    (':', '#', leading/trailing space, or a leading list/flow marker); everything else stays bare."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    s = str(value)
    if s == "" or s != s.strip() or any(c in s for c in (":", "#", "[", "]", "{", "}", "\"", "'")) \
            or s[0] in "-?&*!|>%@`":
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _entry_lines(entry: Mapping) -> List[str]:
    """One module as the catalog's canonical text: 2-space indented block scalars + INLINE arrays
    (``agents: [a, b]``), matching the format the website's sync/check-modules parsers expect."""
    lines = [f"  - name: {_scalar(entry['name'])}"]
    # source may be explicitly null (an external tool like sidekick)
    src = entry.get("source", "")
    lines.append(f"    source: {_scalar(src)}" if src else "    source:")
    lines.append(f"    pain: {_scalar(entry.get('pain', ''))}")
    lines.append(f"    deploy: {_scalar(entry.get('deploy', 'compose'))}")
    if entry.get("port") is not None:
        lines.append(f"    port: {entry['port']}")
    if entry.get("tagline"):
        lines.append(f"    tagline: {_scalar(entry['tagline'])}")
    if entry.get("agents"):
        lines.append(f"    agents: [{', '.join(entry['agents'])}]")
    if entry.get("approval_required"):
        lines.append(f"    approval_required: [{', '.join(entry['approval_required'])}]")
    return lines


def render_modules_yaml(manifests: Iterable[AppManifest], *,
                        extras: Iterable[Mapping] = (), header: str = CATALOG_HEADER) -> str:
    """Render the full modules.yaml document (header + ``modules:`` list) from manifests + extras, in
    the catalog's canonical format (inline agents/approval arrays) so the website parsers read it."""
    entries: List[dict] = [module_entry(m) for m in manifests]
    entries += [dict(e) for e in extras]
    out = [header.rstrip("\n"), "", "modules:"]
    for entry in entries:
        out.extend(_entry_lines(entry))
    return "\n".join(out) + "\n"


__all__ = ["CATALOG_HEADER", "module_entry", "render_modules_yaml"]
