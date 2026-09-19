"""The Projects Workflows tab, served (Workflow-Teaching plan §11).

The tab lists learned/authored workflows and renders one with its rules' provenance labels and its
unresolved-question queue — so the operator can answer §25.18: *what did ReDevOps learn, why does it
believe that, what will it do, and where will it ask me?* These tests drive the API the served page reads;
the end-to-end path (demonstration → candidate → definition) is registered exactly as the UI would show it.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from agentic_os.projects.app import ProjectsServer, create_projects_app
from agentic_os.projects.contracts import (
    DemonstrationObservation,
    DemonstrationSession,
    ObservationKind,
)
from agentic_os.projects.workflow_discovery import candidate_to_definition, discover_workflow

K = ObservationKind
CAPS = {"resolve company": ("twenty.crm.search_company", ("ui_agent",)),
        "send email": ("mail.send", ("ui_agent",))}


def _client_with_workflow():
    server = ProjectsServer(project_id="proj_1", project_name="Studio")
    session = DemonstrationSession(project_id="proj_1", title="Inbound lead follow-up", observations=(
        DemonstrationObservation(session_id="d", seq=1, kind=K.INPUT, intent="resolve company", target="Acme"),
        DemonstrationObservation(session_id="d", seq=2, kind=K.SELECTION, intent="pick contact",
                                 target="on_active_opp", alternatives=("other",)),
        DemonstrationObservation(session_id="d", seq=3, kind=K.SUBMIT, intent="send email"),
    ))
    wf = candidate_to_definition(discover_workflow(session, capabilities=CAPS))
    server.add_workflow(wf)
    return TestClient(create_projects_app(server)), wf


def test_workflows_list_endpoint():
    client, wf = _client_with_workflow()
    rows = client.get("/api/workflows").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == wf.workflow_id and row["title"] == "Inbound lead follow-up"
    assert row["status"] == wf.status.value and row["rules"] >= 1
    assert row["unresolved"] >= 1                      # it has open questions from discovery


def test_workflow_detail_exposes_provenance_and_questions():
    client, wf = _client_with_workflow()
    detail = client.get(f"/api/workflows/{wf.workflow_id}").json()
    provs = {r["provenance"] for r in detail["rules"]}
    assert provs & {"OBSERVED", "INFERRED"}            # §14 — rules are labelled by how they're known
    assert detail["unresolved_questions"]              # the queue the UI renders
    assert detail["learned_from"]["recording_refs"]    # traceable to the demonstration


def test_unknown_workflow_is_404():
    client, _ = _client_with_workflow()
    assert client.get("/api/workflows/nope").status_code == 404


def test_page_serves_and_reads_workflows_api():
    client, _ = _client_with_workflow()
    html = client.get("/").text
    assert "Workflows" in html and "/api/workflows" in html and "prov" in html


def test_missions_api_still_works():
    """The Workflows tab did not disturb the existing Missions surface."""
    client, _ = _client_with_workflow()
    assert client.get("/api/missions").status_code == 200
    assert client.get("/api/projects").json()[0]["name"] == "Studio"
