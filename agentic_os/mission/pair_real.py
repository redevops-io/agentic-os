"""RealPairRunner — the host implementation of the narrow PairRunner contract.

⚠️  HARDWARE-UNVALIDATED. This shells out to the NVIDIA PAIR installer + Ollama and has **not** been
run against a real PAIR install (no GPU/PAIR host in the build environment). The command wiring
follows PAIR's published docs; validate on a real machine before relying on it. It implements ONLY
the seven-method PairRunner contract — ReDevOps still owns the Mission, policy, approval, rollback
and provider selection; PAIR owns its own inference fabric.

Everything runs through an injectable ``run`` (so the wiring is structurally testable) and detection
uses the ordinary loopback probe (``detect_pair``). Where a required artifact is missing (the PAIR
installer, which Microsoft-style has no stable URL), it raises a clear, actionable error rather than
pretending to succeed.
"""
from __future__ import annotations

import platform as _platform
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from .pair import PAIR_OPENAI_BASE, PairStatus, detect_pair

# (returncode, stdout, stderr)
RunResult = Tuple[int, str, str]
Runner = Callable[[list], RunResult]


def _subprocess_run(cmd: list) -> RunResult:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    return p.returncode, p.stdout, p.stderr


@dataclass
class RealPairRunner:
    """Hardware-unvalidated real runner. ``installer_path`` points at the downloaded PAIR installer
    (required for install_pair; there is no stable download URL). ``run``/``detect_fn`` are injectable."""

    installer_path: str = ""
    model_engine: str = "ollama"
    ollama_bin: str = "ollama"
    os_name: str = ""                                  # "" ⇒ auto-detect
    run: Runner = _subprocess_run
    detect_fn: Callable[[], PairStatus] = lambda: detect_pair()  # noqa: E731

    def _os(self) -> str:
        return (self.os_name or _platform.system()).lower()

    def _check(self, cmd: list, what: str) -> None:
        rc, out, err = self.run(cmd)
        if rc != 0:
            raise RuntimeError(f"{what} failed (rc={rc}): {(err or out).strip()[:200]}")

    # ── contract ──────────────────────────────────────────────────────────────────────────
    def detect(self) -> PairStatus:
        """Already installed/serving here? (loopback health, doubling as a presence check)."""
        return self.detect_fn()

    def install_pair(self) -> None:
        if not self.installer_path:
            raise RuntimeError(
                "PAIR installer not provided. Download it from "
                "https://github.com/NVIDIA/Personal-AI-Router/releases and pass installer_path=…")
        osn = self._os()
        if "windows" in osn:
            self._check(["msiexec", "/i", self.installer_path, "/quiet", "/norestart"], "install PAIR (msi)")
        elif "darwin" in osn:                          # a .pkg; a .dmg must be mounted+copied first
            self._check(["installer", "-pkg", self.installer_path, "-target", "/"], "install PAIR (pkg)")
        else:                                          # debian/ubuntu
            self._check(["sudo", "apt", "install", "-y", self.installer_path], "install PAIR (deb)")

    def install_ollama(self) -> None:
        if shutil.which(self.ollama_bin):
            return                                     # PAIR adopts an existing engine — nothing to do
        osn = self._os()
        if "windows" in osn:
            raise RuntimeError("Install Ollama for Windows from https://ollama.com/download, "
                               "or let the PAIR app install it, then retry.")
        # posix: the official one-liner (documented by Ollama)
        self._check(["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"], "install Ollama")

    def ensure_model(self, model: str) -> None:
        rc, out, _ = self.run([self.ollama_bin, "list"])
        if rc == 0 and model in out:
            return
        self._check([self.ollama_bin, "pull", model], f"pull model {model}")

    def start_services(self) -> None:
        # Best-effort: PAIR's service and Ollama normally autostart; nudge Ollama if it isn't serving.
        if self.detect_fn().available:
            return
        if shutil.which(self.ollama_bin):
            self.run([self.ollama_bin, "serve"])       # best-effort; ignore if already running

    def health(self) -> PairStatus:
        return self.detect_fn()

    def uninstall(self) -> None:
        # Best-effort rollback of what we installed. PAIR/Ollama removal is platform-specific and
        # non-destructive here (we don't purge user models); a real deployment refines this.
        osn = self._os()
        try:
            if "linux" in osn:
                self.run(["sudo", "apt", "remove", "-y", "nvpair"])
        except Exception:
            pass
