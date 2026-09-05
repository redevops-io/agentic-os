"""RealPairRunner — STRUCTURAL wiring tests only (fake subprocess). NOT hardware validation:
these prove the runner issues the documented commands; they do NOT prove it works against a real
NVIDIA PAIR install (no PAIR host in CI)."""
import pytest

from agentic_os.mission import pair_real
from agentic_os.mission.pair import PairStatus
from agentic_os.mission.pair_real import RealPairRunner


class FakeRun:
    """Records commands; returns a queued (rc, out, err) per call, default (0, '', '')."""
    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])
    def __call__(self, cmd):
        self.calls.append(cmd)
        return self.results.pop(0) if self.results else (0, "", "")
    @property
    def flat(self):
        return [" ".join(map(str, c)) for c in self.calls]


def _up():   return lambda: PairStatus(available=True, base_url="http://127.0.0.1:1234/v1", models=("qwen",))
def _down(): return lambda: PairStatus(available=False)


def test_install_pair_requires_an_installer_path():
    r = RealPairRunner(run=FakeRun(), detect_fn=_down())
    with pytest.raises(RuntimeError, match="installer not provided"):
        r.install_pair()


def test_install_pair_uses_apt_on_linux():
    run = FakeRun()
    RealPairRunner(installer_path="/tmp/NVPAIR.deb", os_name="linux", run=run, detect_fn=_down()).install_pair()
    assert any("apt install -y /tmp/NVPAIR.deb" in c for c in run.flat)


def test_install_pair_uses_msiexec_on_windows():
    run = FakeRun()
    RealPairRunner(installer_path="C:/NVPAIR.msi", os_name="windows", run=run, detect_fn=_down()).install_pair()
    assert any(c.startswith("msiexec /i C:/NVPAIR.msi") for c in run.flat)


def test_install_ollama_skips_when_present(monkeypatch):
    monkeypatch.setattr(pair_real.shutil, "which", lambda b: "/usr/bin/ollama")
    run = FakeRun()
    RealPairRunner(os_name="linux", run=run, detect_fn=_down()).install_ollama()
    assert run.calls == []                                  # adopts the existing engine, no install


def test_install_ollama_installs_when_absent(monkeypatch):
    monkeypatch.setattr(pair_real.shutil, "which", lambda b: None)
    run = FakeRun()
    RealPairRunner(os_name="linux", run=run, detect_fn=_down()).install_ollama()
    assert any("ollama.com/install.sh" in c for c in run.flat)


def test_ensure_model_pulls_only_when_absent():
    run = FakeRun(results=[(0, "llama3.3:70b\n", "")])     # `ollama list` lacks our model
    RealPairRunner(run=run, detect_fn=_down()).ensure_model("qwen2.5:7b")
    assert any("ollama pull qwen2.5:7b" in c for c in run.flat)

    run2 = FakeRun(results=[(0, "qwen2.5:7b  4.7GB\n", "")])  # already present
    RealPairRunner(run=run2, detect_fn=_down()).ensure_model("qwen2.5:7b")
    assert not any("pull" in c for c in run2.flat)


def test_health_and_detect_use_the_probe():
    r = RealPairRunner(run=FakeRun(), detect_fn=_up())
    assert r.health().available and r.detect().available


def test_start_services_noop_when_already_serving():
    run = FakeRun()
    RealPairRunner(run=run, detect_fn=_up()).start_services()
    assert run.calls == []                                  # already healthy → nothing to start
