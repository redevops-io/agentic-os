"""Sidekick as the stack's Q&A expert — authoritative, deterministic answers about creds,
data locality, and governance (so nobody has to read a manual)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from agentic_os.projects_api import SampleProjectionProvider, create_app, sidekick_reply
from agentic_os.stack_knowledge import answer_stack_question, help_questions


def test_credentials_answer_is_authoritative_and_ref_only():
    e = answer_stack_question("how are my credentials handled?")
    assert e is not None and e.id == "credentials"
    # the load-bearing claims: a reference (not the token) reaches the Mission, nothing logged
    assert "reference" in e.answer.lower() and "never logged" in e.answer.lower()


def test_data_locality_distinguishes_cloud_vs_local():
    e = answer_stack_question("is my data processed locally or in the cloud?")
    assert e is not None and e.id == "data-locality"
    a = e.answer.lower()
    assert "cloud" in a and ("local" in a or "your own machine" in a)


def test_model_never_sees_secrets():
    e = answer_stack_question("does the AI model ever see my tokens?")
    assert e is not None and e.id == "secrets-to-model"
    assert e.answer.strip().lower().startswith("no")


def test_connect_scope_says_connecting_is_not_ingest_everything():
    e = answer_stack_question("if I connect google will it read my whole drive?")
    assert e is not None and e.id == "connect-scope"
    assert "not" in e.answer.lower() and "drive.file" in e.answer.lower()


def test_revoke_and_governance_entries_match():
    assert answer_stack_question("how do I revoke access to an app?").id == "revoke"
    assert answer_stack_question("is anything auto-executed without asking?").id == "governance"


def test_ordinary_mission_request_does_not_false_match():
    # a normal task, not a stack question → no knowledge entry hijacks it
    assert answer_stack_question("refund Sarah Chen for the duplicate charge") is None
    assert answer_stack_question("draft a cold outreach email to Tasha") is None
    assert answer_stack_question("summarise the Q3 board deck into bullet points") is None
    assert answer_stack_question("create a spreadsheet of last week's signups") is None


def test_expanded_topics_resolve_to_their_entry():
    # each new curated topic answers a natural phrasing of the question
    cases = {
        "how much does this cost, is it open source?": "licensing",
        "are you SOC2 compliant and GDPR ready?": "compliance",
        "how long do you retain my data?": "retention",
        "which apps work offline without internet?": "offline-local-apps",
        "how do I add a connector that isn't listed?": "add-connector",
        "can I bring my own oauth app and use my own keys?": "byo-oauth",
        "do I have to install everything or can I pick which apps I want?": "pick-apps",
        "what is a mission versus a workflow?": "mission-vs-workflow",
        "do you share or sell my data or train on it?": "third-party-sharing",
        "how do I contact support if I'm stuck?": "support-contact",
    }
    for q, expected in cases.items():
        e = answer_stack_question(q)
        assert e is not None and e.id == expected, f"{q!r} → {e.id if e else None}, expected {expected}"


def test_pick_apps_matches_the_users_own_phrasings_without_hijacking_doc_tasks():
    # the feature's own phrasing ("don't have to install the whole thing") must resolve
    for q in ("do I have to install everything or can I pick apps?",
              "I don't want to install the whole thing",
              "can I just pick apps?",
              "do I need the whole suite?"):
        assert answer_stack_question(q).id == "pick-apps", q
    # but an ordinary document task that says "whole" must NOT route to pick-apps
    for q in ("read the whole thing and summarise it",
              "summarise the whole document",
              "email the whole team"):
        assert answer_stack_question(q) is None, q


def test_component_explainers_resolve_and_carry_a_detail_tier():
    # Sidekick can explain each core runtime/component; each has a plain-English overview + a
    # deeper technical `detail` tier, grounded in the public redevops.io pages.
    cases = {
        "how does the whole stack work?": "arch-overview",
        "how does the Context Runtime work?": "context-runtime",
        "what is redevops-rag and how does retrieval work?": "redevops-rag",
        "how does the Mission Runtime work?": "mission-runtime",
        "how does the Discovery Runtime work?": "discovery-runtime",
        "how does the Execution Planner work?": "execution-planner",
        "what is the agent-harness?": "agent-harness",
        "how does the Governance Plane work?": "governance-plane",
        "how do Projects and Sidekick fit into the architecture?": "projects-sidekick-arch",
    }
    for q, expected in cases.items():
        e = answer_stack_question(q)
        assert e is not None and e.id == expected, f"{q!r} -> {e.id if e else None}"
        assert e.topic == "How it works"
        assert e.detail, f"{expected} should carry a technical detail tier"


def test_component_explainers_are_grounded_in_public_pages():
    from agentic_os.stack_knowledge import STACK_KNOWLEDGE
    how = [e for e in STACK_KNOWLEDGE if e.topic == "How it works"]
    assert len(how) >= 9
    for e in how:
        assert "redevops.io" in e.source     # every explainer cites a public page, not an internal path


def test_expanded_answers_stay_truthful_not_overclaiming():
    # licensing must not quote a fabricated price; compliance must not claim a certification it lacks
    lic = answer_stack_question("what does it cost?")
    assert lic.id == "licensing" and "open source" in lic.answer.lower()
    comp = answer_stack_question("is it soc2 certified?").answer.lower()
    assert "self-host" in comp and "contact" in comp        # points to the team, doesn't assert a cert


def test_every_entry_names_where_it_is_enforced():
    for e in help_questions():
        assert e["question"] and e["topic"]
    from agentic_os.stack_knowledge import STACK_KNOWLEDGE
    assert all(e.source for e in STACK_KNOWLEDGE)   # every claim points at its enforcement site


def test_proactive_intelligence_topics_resolve_to_their_entry():
    # Sidekick can guide users through the proactive-intelligence direction (the plan's features).
    cases = {
        "can you tell me what needs my attention today?": "proactive-overview",
        "what is the priority engine and how does it rank things?": "priority-engine",
        "what's the difference between an opportunity and an intervention?": "opportunity-vs-intervention",
        "will the CRM tell me the next best action for a deal?": "crm-next-best-action",
        "can support predict the likely resolution path?": "support-resolution-intelligence",
        "will projects warn me about execution risk before a milestone slips?": "projects-execution-risk-radar",
        "can it find emerging trends before they're saturated?": "growth-trend-intelligence",
        "is there a semrush for content creators?": "creator-intelligence",
        "who is worth contacting now for outreach?": "outreach-intent-radar",
        "can it match me to jobs using a capability graph?": "recruiting-fit-engine",
        "does research plan what to investigate to reduce uncertainty?": "research-info-gain-planner",
        "can it find stale documents and knowledge gaps?": "knowledge-debt-radar",
        "how does the stack decide what to learn next (knowledge frontier)?": "knowledge-frontier",
        "will analytics explain why a metric changed?": "analytics-anomaly-action",
        "would the wealth manager flag when my plan's assumptions drift?": "wealth-assumption-drift",
        "if it acts on its own how is that governed and how do i know it works?": "proactive-governance-learning",
    }
    for q, expected in cases.items():
        e = answer_stack_question(q)
        assert e is not None and e.id == expected, f"{q!r} -> {e.id if e else None}, expected {expected}"
        assert e.topic == "Proactive intelligence (roadmap)"


def test_proactive_answers_are_status_honest_not_overclaiming():
    # the load-bearing honesty guard: roadmap features must NOT read as shipped.
    from agentic_os.stack_knowledge import STACK_KNOWLEDGE
    proactive = {e.id: e for e in STACK_KNOWLEDGE if e.topic == "Proactive intelligence (roadmap)"}
    assert len(proactive) >= 16
    # pure-roadmap entries say so plainly (in the answer, where the user reads it)
    for pid in ("crm-next-best-action", "outreach-intent-radar", "recruiting-fit-engine",
                "knowledge-debt-radar", "analytics-anomaly-action",
                "wealth-assumption-drift", "creator-intelligence"):
        a = proactive[pid].answer.lower()
        assert ("not shipped" in a or "roadmap" in a or "planned" in a or "not yet" in a), pid
    # the Priority Engine spine + the 'what needs me?' surface now SHIP — those two say so, honestly,
    # while still flagging that the detectors feeding them are growing (they must not over- OR under-claim).
    for pid in ("priority-engine", "proactive-overview"):
        a = proactive[pid].answer.lower()
        assert "ship" in a and ("roadmap" in a or "growing" in a or "still" in a), pid
    # the shipped detector KERNELS (Growth, Projects Risk, Research) each state they validate LOGIC on
    # SYNTHETIC data, NOT real-world accuracy — matching the shipped-PR framing and the "abstain" rule.
    for pid in ("growth-trend-intelligence", "projects-execution-risk-radar", "research-info-gain-planner",
                "knowledge-frontier"):
        a = proactive[pid].answer.lower()
        assert "kernel" in a and "ship" in a, pid
        assert "synthetic" in a and "logic" in a, pid
        assert "not real-world" in a or "not real world" in a, pid
    # Support states its primitives ship today AND that the predictor is the extension
    supp = proactive["support-resolution-intelligence"].answer.lower()
    assert "ship" in supp and ("extension" in supp or "roadmap" in supp)


def test_proactive_entries_disclose_status_in_their_source():
    from agentic_os.stack_knowledge import STACK_KNOWLEDGE
    for e in STACK_KNOWLEDGE:
        if e.topic == "Proactive intelligence (roadmap)":
            s = e.source.lower()
            assert ("roadmap" in s or "shipped" in s or "not yet" in s), f"{e.id} source hides status"


def test_proactive_keywords_do_not_hijack_real_mission_requests():
    # the plan's own examples are literally real tasks — asking Sidekick to DO them must NOT route
    # to a roadmap explainer, and neither must ordinary app work.
    for q in ("contact Acme now",
              "send Acme the deployment proposal",
              "draft a cold outreach email to Tasha",
              "refund Sarah Chen for the duplicate charge",
              "apply a macro to the support ticket",       # 'macro' is near resolution-intelligence
              "summarise the Q3 board deck into bullet points",
              "create a spreadsheet of last week's signups",
              "rebalance the portfolio to the target allocation"):
        assert answer_stack_question(q) is None, f"{q!r} was hijacked to {answer_stack_question(q)}"


def test_sidekick_reply_routes_stack_questions_to_the_expert():
    r = sidekick_reply({"section": "Overview"}, "where is my data processed, locally or in the cloud?")
    assert "cloud" in r["text"].lower() and r.get("topic") == "Data handling"
    # and the existing governed-mission behaviour is untouched (tier-4 approval answer still wins)
    assert "tier-4" in sidekick_reply({"section": "Missions"}, "why does this need approval?")["text"].lower()


def test_priorities_endpoint_returns_the_what_needs_me_surface():
    c = TestClient(create_app(SampleProjectionProvider()))
    s = c.get("/api/projects/customer-ops/priorities").json()
    assert "need" in s["summary"].lower() and "you today" in s["summary"].lower()
    assert s["basis"] == "sample"                         # honest: example data, not a live deployment
    assert 1 <= len(s["surfaced"]) <= 4                   # attention budget
    for item in s["surfaced"]:
        assert item["requires_approval"] and item["action"] == "request_approval"
        assert item["source_app"] and item["proposed_action"] and 0 <= item["confidence"] <= 1
    # surfaced are highest-priority first
    prios = [i["priority"] for i in s["surfaced"]]
    assert prios == sorted(prios, reverse=True)


def test_priorities_surface_draws_from_the_real_shipped_detectors():
    # every shipped detector must actually feed the surface: Growth trend, Support follow-up, the
    # Projects Execution Risk Radar, and (auto-handled, low-risk) the Research Information-Gain Planner.
    c = TestClient(create_app(SampleProjectionProvider()))
    s = c.get("/api/projects/customer-ops/priorities").json()
    fed = ({i["source_app"] for i in s["surfaced"]} | {i["source_app"] for i in s["deferred"]}
           | {i["source_app"] for i in s.get("handled", [])})
    assert {"growth", "support", "projects"} <= fed       # these surface for approval
    # the research investigation is READ-tier ⇒ auto-handled (not surfaced); confirm it ran
    assert s["handled_automatically"] >= 1


def test_sidekick_actionable_what_needs_me_returns_live_surface_not_the_roadmap_explainer():
    # with a provider wired, an ACTIONABLE 'what needs me?' returns the live Priority Engine surface…
    c = TestClient(create_app(SampleProjectionProvider()))
    r = c.post("/api/sidekick", json={"ctx": {}, "text": "what needs me today?"}).json()
    assert r.get("topic") == "Priorities" and "you today" in r["text"].lower()
    assert isinstance(r.get("items"), list) and r["items"]
    # …but an EXPLANATORY question still gets the roadmap/how-it-works KB explainer, not the list
    explain = c.post("/api/sidekick", json={"ctx": {}, "text": "how does the priority engine work?"}).json()
    assert explain.get("topic") != "Priorities"


def test_sidekick_reply_without_provider_falls_through_to_kb():
    # the 2-arg call path (no provider) must not attempt the surface — KB roadmap explainer answers
    r = sidekick_reply({"section": "Overview"}, "what needs my attention?")
    assert r.get("topic") != "Priorities"                 # no provider ⇒ no live surface


def test_sidekick_and_help_endpoints():
    c = TestClient(create_app(SampleProjectionProvider()))
    ans = c.post("/api/sidekick", json={"ctx": {}, "text": "how are my credentials stored?"}).json()
    assert "reference" in ans["text"].lower()
    helped = c.get("/api/sidekick/help").json()
    ids = {h["id"] for h in helped}
    assert {"credentials", "data-locality", "connect-scope", "governance"} <= ids
