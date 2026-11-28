"""A refused /invoke must never become a capability's result.

Found with redevops-demo against this kernel: every /invoke answered 503 ("operator /invoke is disabled: ...") through
an in-process transport that returns the response body, and the deploy mission still ended SUCCEEDED, with the
refusal text stored as the fact `image_scanned`."""
from __future__ import annotations

import pytest

from agentic_os.mission.operators import HTTPOperatorClient
from agentic_os.mission.util import HTTPError
from agentic_os.mission.verify import result_failed

REFUSAL = {"detail": "operator /invoke is disabled: AGENTIC_OS_CONTROL_TOKEN (or AGENTIC_OS_TENANT_TOKENS) is not set."}


def test_the_shared_classifier_still_treats_free_text_detail_as_a_legitimate_result():
    """`{"detail": "Invoice created ..."}` is a result some operators return (tests/test_kernel_residuals.py pins it), so
    the refusal is caught where its meaning is unambiguous: at the HTTP boundary, below."""
    assert result_failed({"detail": "Invoice created for customer Acme"}, side_effecting=True, declares_outputs=False) == (False, "")


@pytest.mark.parametrize("body", [REFUSAL, {"detail": [{"loc": ["body", "capability"], "msg": "field required"}]}])
def test_the_http_client_raises_for_any_error_envelope(body):
    client = HTTPOperatorClient({"infra": "http://infra"}, transport=lambda url, b, headers, timeout: dict(body))
    with pytest.raises(HTTPError, match="refused infra.plan"):
        client.invoke("infra", "infra.plan", {}, "k1")


def test_the_http_client_raises_when_a_transport_hands_back_a_refusal_body():
    client = HTTPOperatorClient({"infra": "http://infra"}, transport=lambda url, body, headers, timeout: dict(REFUSAL))
    with pytest.raises(HTTPError, match="refused infra.plan: operator /invoke is disabled"):
        client.invoke("infra", "infra.plan", {}, "k1")


def test_the_http_client_still_accepts_a_wrapped_or_a_bare_result():
    ok = HTTPOperatorClient({"o": "http://o"}, transport=lambda *a: {"result": {"done": True}})
    assert ok.invoke("o", "c", {}, "k") == {"done": True}
    bare = HTTPOperatorClient({"o": "http://o"}, transport=lambda *a: {"done": True, "detail": "with other keys"})
    assert bare.invoke("o", "c", {}, "k") == {"done": True, "detail": "with other keys"}
