"""Regression: `/openapi.json` must be generated (R0-prime defect d).

On v0.3.2 the hosted-OAuth callback route was annotated `-> "HTMLResponse"` with the class imported
inside the function body, so FastAPI kept a pydantic ForwardRef and `/openapi.json` answered HTTP 500
(`/docs` and `/redoc` loaded but their schema fetch failed). The route is now declared with
`response_class=HTMLResponse`.
"""
from __future__ import annotations

import warnings

from fastapi.testclient import TestClient

from agentic_os.projects_api import SampleProjectionProvider, create_app, mounted_app

CALLBACK = "/api/apps/connect/callback"


def _client(app) -> TestClient:
    # raise_server_exceptions=False: a 500 must surface as a status code assertion, not an exception.
    return TestClient(app, raise_server_exceptions=False)


def test_openapi_json_is_generated_for_plain_and_mounted_app():
    for app in (create_app(SampleProjectionProvider()), mounted_app("")):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = _client(app).get("/openapi.json")
        assert r.status_code == 200, r.text[:300]
        doc = r.json()
        assert "/api/projects" in doc["paths"]
        assert CALLBACK in doc["paths"]
        # the callback is documented as an HTML page, not a JSON body
        assert "text/html" in doc["paths"][CALLBACK]["get"]["responses"]["200"]["content"]


def test_openapi_json_is_generated_under_a_base_path():
    r = _client(mounted_app("/projects")).get("/projects/openapi.json")
    assert r.status_code == 200, r.text[:300]
    assert CALLBACK in r.json()["paths"]


def test_docs_and_redoc_pages_load():
    # Record: these pages are served by FastAPI itself; they must stay behind the capability model
    # that agent `security` is designing (not implemented here).
    c = _client(create_app(SampleProjectionProvider()))
    assert c.get("/docs").status_code == 200
    assert c.get("/redoc").status_code == 200


def test_callback_still_answers_html_400_without_hosted_oauth():
    r = _client(create_app(SampleProjectionProvider())).get(f"{CALLBACK}?code=x&state=y")
    assert r.status_code == 400
    assert "text/html" in r.headers["content-type"]
