"""FR-03: an unrecognised value of a permission switch must never start an app ungated. Accepted spellings (case-insensitive, surrounding white space ignored):
on = 1 / true / yes / on; off = 0 / false / no / off / empty (or unset). ANY other non-empty value refuses to start with a clear message naming the variable and the
accepted spellings. Applies to AGENTIC_OS_PERMISSIONS and the permission-aware retrieval / cache switches; CR_PERMISSIONS keeps its own rule (any non-off value is a
request)."""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import _cr_enterprise_stub as stub  # noqa: E402

from agentic_os import app_gate  # noqa: E402
from agentic_os.app_gate import GateStartupError, install_gate  # noqa: E402

APP = "growth-assistant"
SWITCHES = ["AGENTIC_OS_PERMISSIONS", app_gate.RETRIEVAL_SWITCH, app_gate.CACHE_SWITCH]
ON = ["1", "true", "TRUE", "True", "yes", "Yes", "on", "ON", " 1 ", "true\t"]
OFF = ["0", "false", "False", "no", "NO", "off", "Off", "", "   "]
BAD = ["enabled", "enable", "y", "Y", "t", "2", "-1", "10", "truee", "ye", "of", "none", "null", "maybe", "True ,", "1;", "on!", "١", "tru​e"]


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in (*SWITCHES, "CR_PERMISSIONS", "CR_IDENTITY_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
    monkeypatch.setattr(app_gate, "_GATES", {})
    monkeypatch.setattr(app_gate, "_PROCESS_AUTHORIZER", None)
    stub.absent(monkeypatch)


@pytest.mark.parametrize("name", SWITCHES)
@pytest.mark.parametrize("value", BAD)
def test_an_unrecognised_value_refuses_to_start_and_names_the_variable_and_the_accepted_spellings(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(GateStartupError) as e:
        install_gate(APP)
    msg = str(e.value)
    assert name in msg and "not a recognised value" in msg and "1, true, yes, on" in msg and "0, false, no, off" in msg
    assert "refuses to run ungated" in msg or "refuses to start" in msg


@pytest.mark.parametrize("value", ON)
def test_every_accepted_truthy_spelling_turns_basic_permissions_on(monkeypatch, value):
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", value)
    pf = install_gate(APP)
    assert pf.mode == "basic" and app_gate.gate_for(APP) is not None


@pytest.mark.parametrize("value", OFF)
def test_every_accepted_falsy_spelling_is_the_basic_default(monkeypatch, value):
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", value)
    install_gate(APP)
    assert app_gate.gate_for(APP) is None and app_gate.basic_enabled() is False


@pytest.mark.parametrize("value", ON)
def test_the_enterprise_switches_accept_the_same_spellings(monkeypatch, value):
    monkeypatch.setenv(app_gate.CACHE_SWITCH, value)
    with pytest.raises(app_gate.EnterpriseCapabilityMissing):
        install_gate(APP)


@pytest.mark.parametrize("value", BAD)
def test_at_call_time_an_unrecognised_value_counts_as_requested_so_a_console_denies_never_opens(monkeypatch, value):
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", value)
    assert app_gate.permissions_requested() is True


def test_the_accepted_spellings_are_documented():
    t = (pathlib.Path(__file__).resolve().parents[1] / "docs" / "PERMISSIONS_MIGRATION.md").read_text()
    assert "1, true, yes, on" in t and "0, false, no, off" in t and "not a recognised value" in t.lower()
