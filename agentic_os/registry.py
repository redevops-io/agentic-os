"""Module registry — load and validate the declarative catalog (modules.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CATALOG = Path(__file__).resolve().parent.parent / "modules.yaml"

# Must match DeploySpec.deploy in app_kit.manifest (compose | operator | tool), the source the catalog
# generates modules.yaml from — the loader can't be stricter than the generator or it rejects valid catalogs.
_VALID_DEPLOY = {"compose", "operator", "tool"}


@dataclass(frozen=True)
class Module:
    name: str
    pain: str
    # Apps live in the agentic-os monorepo under ``source`` (apps/<name>), which is what the catalog
    # (app_kit.catalog) emits. ``repo`` is the legacy GitHub-slug form, kept as an alias for external
    # tools that really are separate repositories; one of the two must be present.
    source: str = ""
    repo: str = ""
    deploy: str = "compose"
    port: int = 0
    tagline: str = ""
    agents: tuple[str, ...] = ()
    approval_required: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        """GitHub URL for a separate-repo module; empty for an in-monorepo app (use ``path``)."""
        return f"https://github.com/{self.repo}" if self.repo else ""

    @property
    def path(self) -> str:
        """In-monorepo source path (apps/<name>), or empty for a separate-repo module."""
        return self.source

    @property
    def service(self) -> str:
        """The docker-compose service name for this module."""
        return self.name

    def needs_approval(self, action: str) -> bool:
        return action in self.approval_required


class Registry:
    """The catalog of modules the OS can run."""

    def __init__(self, modules: list[Module]):
        self._by_name = {m.name: m for m in modules}

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CATALOG) -> "Registry":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        modules = [cls._parse(m) for m in data.get("modules", [])]
        cls._check_unique(modules)
        return cls(modules)

    @staticmethod
    def _parse(raw: dict) -> Module:
        missing = [k for k in ("name", "pain") if not raw.get(k)]
        # source (monorepo path) / repo (github slug) are both optional: an external tool like sidekick
        # has neither (deploy: tool). The catalog identifies a module by name + pain.
        source, repo = raw.get("source") or "", raw.get("repo") or ""
        if missing:
            raise ValueError(f"module {raw.get('name', '?')} missing required keys: {missing}")
        deploy = raw.get("deploy", "compose")
        if deploy not in _VALID_DEPLOY:
            raise ValueError(f"module {raw['name']}: deploy must be one of {_VALID_DEPLOY}, got {deploy!r}")
        return Module(
            name=raw["name"], pain=raw["pain"], source=source, repo=repo, deploy=deploy,
            port=int(raw.get("port", 0) or 0),
            tagline=str(raw.get("tagline", "") or ""),
            agents=tuple(raw.get("agents", []) or []),
            approval_required=tuple(raw.get("approval_required", []) or []),
        )

    @staticmethod
    def _check_unique(modules: list[Module]) -> None:
        seen: set[str] = set()
        for m in modules:
            if m.name in seen:
                raise ValueError(f"duplicate module name: {m.name}")
            seen.add(m.name)

    def __iter__(self):
        return iter(self._by_name.values())

    def __len__(self) -> int:
        return len(self._by_name)

    def get(self, name: str) -> Module:
        if name not in self._by_name:
            raise KeyError(f"unknown module: {name} (have: {sorted(self._by_name)})")
        return self._by_name[name]

    @property
    def names(self) -> list[str]:
        return sorted(self._by_name)
