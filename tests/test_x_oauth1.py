"""OAuth 1.0a signing for the X publisher — validated offline before any live post."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
from urllib.parse import quote

from agentic_os.content.clients import XPublisher, _oauth1_header
from agentic_os.content.contracts import Channel


def _ref_signature(ck, cs, at, ats, nonce, ts):
    """Independent re-derivation of the HMAC-SHA1 signature (oauth params only; JSON body not signed)."""
    oauth = {"oauth_consumer_key": ck, "oauth_nonce": nonce, "oauth_signature_method": "HMAC-SHA1",
             "oauth_timestamp": ts, "oauth_token": at, "oauth_version": "1.0"}
    enc = lambda s: quote(str(s), safe="~")
    param_str = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(oauth.items()))
    base = "&".join(["POST", enc("https://api.x.com/2/tweets"), enc(param_str)])
    return base64.b64encode(
        hmac.new(f"{enc(cs)}&{enc(ats)}".encode(), base.encode(), hashlib.sha1).digest()).decode()


def test_oauth1_header_signature_matches_independent_derivation():
    h = _oauth1_header("POST", "https://api.x.com/2/tweets", "CK", "CS", "AT", "ATS",
                       nonce="fixednonce", ts="1700000000")
    assert h.startswith("OAuth ") and 'oauth_signature_method="HMAC-SHA1"' in h
    sig = re.search(r'oauth_signature="([^"]+)"', h).group(1)
    from urllib.parse import unquote
    assert unquote(sig) == _ref_signature("CK", "CS", "AT", "ATS", "fixednonce", "1700000000")


def test_oauth1_is_deterministic_per_nonce_and_body_independent():
    a = _oauth1_header("POST", "https://api.x.com/2/tweets", "ck", "cs", "at", "ats", nonce="N", ts="1")
    b = _oauth1_header("POST", "https://api.x.com/2/tweets", "ck", "cs", "at", "ats", nonce="N", ts="1")
    c = _oauth1_header("POST", "https://api.x.com/2/tweets", "ck", "cs", "at", "ats", nonce="M", ts="1")
    assert a == b and a != c                                  # stable per nonce; changes with nonce


def test_from_env_detects_access_token_vs_secret(monkeypatch):
    monkeypatch.setenv("X_CONSUMER_KEY", "ck")
    monkeypatch.setenv("X_CONSUMER_SECRET", "cs")
    # real access token starts with "<digits>-"; the secret does not — mapping detected either way
    monkeypatch.setenv("X_ACCESS_KEY", "1526228120-AbCdEfGh")
    monkeypatch.setenv("X_ACCESS_TOKEN", "plain45charsecretstring")
    p = XPublisher.from_env()
    assert p._at == "1526228120-AbCdEfGh" and p._ats == "plain45charsecretstring"
    assert p.can_publish(Channel.X)

    monkeypatch.setenv("X_ACCESS_KEY", "plain45charsecretstring")
    monkeypatch.setenv("X_ACCESS_TOKEN", "1526228120-AbCdEfGh")
    p2 = XPublisher.from_env()
    assert p2._at == "1526228120-AbCdEfGh" and p2._ats == "plain45charsecretstring"


def test_cannot_publish_without_full_quartet(monkeypatch):
    for v in ("X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_KEY", "X_ACCESS_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    assert not XPublisher.from_env().can_publish(Channel.X)
