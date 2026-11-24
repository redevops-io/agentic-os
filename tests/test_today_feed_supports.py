"""The Today feed tells the UI what this deployment can do, so it offers only that.

Without a governed case source Today is read-only: capture and item detail answer 501. The UI used to learn that by asking (two failed requests
on every load) and still showed the Quick Capture box.
"""
import pytest

from agentic_os.capture import CaptureError
from agentic_os.projects_api import SampleProjectionProvider


def test_the_sample_deployment_supports_capture_and_detail_and_they_work():
    p = SampleProjectionProvider()
    feed = p.today("customer-ops")
    assert feed["supports"] == {"capture": True, "detail": True}
    assert p.today_item("customer-ops", feed["items"][0]["id"])["id"] == feed["items"][0]["id"]


def test_a_read_only_deployment_says_so_and_the_statement_matches_what_the_api_then_does():
    p = SampleProjectionProvider(today_sample_cases=False)
    assert p.today("customer-ops")["supports"] == {"capture": False, "detail": False}
    with pytest.raises(CaptureError) as e:
        p.capture_list("customer-ops")
    assert e.value.status == 501
