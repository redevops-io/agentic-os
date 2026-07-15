"""Fleet orchestrator — deploy modules and run their agents.

The Fleet ties the pieces together: it brings modules up (via their own compose files),
gives each module its declared agents, and dispatches agent work through the cost-aware
`Router`. Any action a module marks `approval_required` is routed to `Context` as a pending
approval instead of being executed directly.

Deployment here shells out to the module's own `docker compose` (each redevops.io module
ships one). It is intentionally thin: agentic-os orchestrates modules, it does not
re-implement them.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .context import Approval, Context
from .registry import Module, Registry
from .router import Router, Task


@dataclass
class ModuleStatus:
    name: str
    deployed: bool
    agents: tuple[str, ...]
    detail: str = ""


class Fleet:
    def __init__(self, registry: Registry, router: Router, context: Context,
                 workdir: str | Path = ".agentic-os/modules"):
        self.registry = registry
        self.router = router
        self.context = context
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        # Hermes 0.17 background subagents: job_id -> {status,module,action,result}.
        self.jobs: dict[str, dict] = {}

    # --- lifecycle -----------------------------------------------------------
    def up(self, *names: str) -> list[ModuleStatus]:
        return [self._deploy(self.registry.get(n)) for n in (names or self.registry.names)]

    def down(self, *names: str) -> list[ModuleStatus]:
        out = []
        for n in (names or self.registry.names):
            m = self.registry.get(n)
            if m.deploy == "compose":
                self._compose(m, "down")
            out.append(ModuleStatus(m.name, deployed=False, agents=m.agents, detail="stopped"))
        return out

    def status(self) -> list[ModuleStatus]:
        out = []
        for m in self.registry:
            deployed = (self.workdir / m.name).exists() if m.deploy == "compose" else True
            out.append(ModuleStatus(m.name, deployed=deployed, agents=m.agents))
        return out

    def _deploy(self, m: Module) -> ModuleStatus:
        if m.deploy == "tool":
            return ModuleStatus(m.name, deployed=True, agents=m.agents, detail="tool (no service)")
        if not shutil.which("docker"):
            return ModuleStatus(m.name, deployed=False, agents=m.agents, detail="docker not available")
        path = self._ensure_checkout(m)
        self._compose(m, "up", "-d", cwd=path)
        return ModuleStatus(m.name, deployed=True, agents=m.agents, detail=f"compose up @ {path}")

    def _ensure_checkout(self, m: Module) -> Path:
        path = self.workdir / m.name
        if not path.exists() and shutil.which("git"):
            subprocess.run(["git", "clone", "--depth", "1", m.url, str(path)], check=False,
                           capture_output=True)
        return path

    def _compose(self, m: Module, *args: str, cwd: Path | None = None) -> None:
        if shutil.which("docker"):
            subprocess.run(["docker", "compose", *args], cwd=str(cwd or self.workdir / m.name),
                           check=False, capture_output=True)

    # --- agent work ----------------------------------------------------------
    def dispatch(self, module_name: str, agent: str, action: str, prompt: str,
                 capability: str = "reason", background: bool = False) -> Approval | str | dict:
        """Run one agent action. If the action needs approval, record it and stop short of
        executing; otherwise route the work to the cheapest capable model and return its output.

        With ``background=True`` (Hermes 0.17 background subagents), a non-approval action is
        launched as a detached job that returns immediately with ``{"job_id","status":"running"}``;
        the result lands in ``self.jobs[job_id]`` and a chat notification fires on completion."""
        m = self.registry.get(module_name)
        if agent not in m.agents:
            raise ValueError(f"module {module_name} has no agent {agent!r} (agents: {m.agents})")
        if m.needs_approval(action):
            return self.context.request_approval(
                module=module_name, action=action,
                summary=f"{agent} wants to {action}", payload={"prompt": prompt})
        task = Task(prompt=prompt, capability=capability, system=self._system_prompt(m, agent))
        if background:
            return self._dispatch_background(m, action, task)
        return self.router.run(task).text

    def _dispatch_background(self, m: Module, action: str, task: Task) -> dict:
        job_id = f"job-{len(self.jobs) + 1:05d}"
        self.jobs[job_id] = {"status": "running", "module": m.name, "action": action, "result": None}
        notifier = getattr(self.context, "notifier", None)

        def _worker() -> None:
            try:
                text = self.router.run(task).text
                self.jobs[job_id].update(status="done", result=text)
                note = f"✅ background done · {m.name}: {action}"
            except Exception as e:  # noqa: BLE001
                self.jobs[job_id].update(status="error", result=str(e))
                note = f"⚠ background failed · {m.name}: {action}: {e}"
            if notifier is not None:
                try:
                    notifier.send(note)
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=_worker, name=f"bg-{job_id}", daemon=True).start()
        return {"job_id": job_id, "status": "running", "module": m.name, "action": action}

    def job(self, job_id: str) -> dict | None:
        return self.jobs.get(job_id)

    def _system_prompt(self, m: Module, agent: str) -> str:
        biz = self.context.profile().get("name", "the business")
        return (f"You are the {agent} agent for the '{m.name}' module of {biz}'s Agentic Business OS. "
                f"You handle: {m.pain}. Act safely; never take irreversible action without approval.")
