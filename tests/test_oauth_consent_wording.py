"""The consent page must not call a connection read-only: ticked grants can make files, keep records and start work."""
from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")

from agentic_os.agent_gateway.oauth_http import GRANT_TEXT, consent_page  # noqa: E402


def page(scopes):
    return consent_page(action="/oauth/authorize", client_name="Agent", client_id="cid_x", redirect_uri="https://client.example/cb",
                        person="approver:acme", tenant="acme", scopes=scopes, ticket="t")


@pytest.mark.parametrize("scopes", [["projects.read"], ["media.edit", "apps.use", "social.propose"]])
def test_the_page_never_says_the_connection_only_reads(scopes):
    words = re.sub(r"<[^>]+>", " ", page(scopes).split("<form")[0])
    assert not re.search(r"read-only|asks to read|only read", words, re.I), words
    assert "only what you tick" in words and "Not all of it is reading" in re.sub(r"\s+", " ", words)


def test_every_grant_that_acts_without_a_proposal_says_so_on_its_line():
    """What the page's own words rely on: a grant that changes something at once does not describe itself as reading."""
    for grant in ("media.edit", "apps.use", "market.research"):
        text = GRANT_TEXT[grant]
        assert re.search(r"new files|keep their records|Run market research", text), (grant, text)
