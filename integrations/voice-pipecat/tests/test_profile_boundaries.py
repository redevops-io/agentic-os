"""What the optional profile does NOT include: provider services, transports that listen, keys, telephony, outbound calling."""
from __future__ import annotations

import ast
import re
import socket
from pathlib import Path

import pytest

from agentic_os_voice.capabilities import default_tools
from agentic_os_voice.demo.run import network_blocked

SRC = Path(__file__).resolve().parent.parent / "src" / "agentic_os_voice"
ALLOWED_PIPECAT_SERVICES = {"pipecat.services.llm_service", "pipecat.services.settings"}


def _imports():
    for p in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                yield p.name, node.module
            elif isinstance(node, ast.Import):
                for a in node.names:
                    yield p.name, a.name


def test_only_the_framework_core_is_imported_no_provider_service_and_no_transport():
    bad = [(f, m) for f, m in _imports() if m.startswith("pipecat.transports") or (m.startswith("pipecat.services") and m not in ALLOWED_PIPECAT_SERVICES)]
    assert bad == []


def test_no_provider_key_or_environment_secret_is_read_by_the_package():
    # mock_crm.py holds one deliberately fake fixture credential (the sentinel that the tests search for); nothing reads the environment
    for p in SRC.rglob("*.py"):
        if p.name == "mock_crm.py":
            continue
        assert not re.search(r"os\.environ|getenv|API_KEY|api_key|load_dotenv|dotenv", p.read_text(encoding="utf-8")), p.name


def test_the_install_profile_asks_for_pipecat_core_with_no_extras():
    import tomllib
    meta = tomllib.loads((SRC.parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    assert [d for d in meta["project"]["dependencies"] if "pipecat" in d] == ["pipecat-ai==1.12.0"]        # no [extra]: no provider SDK, no transport
    assert not meta["project"].get("optional-dependencies", {}).keys() - {"dev"}


def test_there_is_no_tool_that_dials_or_sends_a_message_without_a_person():
    for name, spec in default_tools().items():
        assert not re.search(r"dial|outbound|call_out|sms|phone|telephony", name + spec.capability, re.I)


def test_the_demo_guard_refuses_connect_bind_and_listen():
    with network_blocked():
        s = socket.socket()
        try:
            for call in (lambda: s.connect(("127.0.0.1", 9)), lambda: s.bind(("127.0.0.1", 0)), lambda: s.listen(1),
                         lambda: socket.create_connection(("127.0.0.1", 9)), lambda: socket.getaddrinfo("example.invalid", 80)):
                with pytest.raises(RuntimeError):
                    call()
        finally:
            s.close()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))          # restored afterwards
    s.close()
