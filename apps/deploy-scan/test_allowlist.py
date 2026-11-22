from __future__ import annotations

import importlib

import pytest

allowlist = importlib.import_module("deploy-scan.allowlist")


def test_parses_comma_and_newline_separated_entries():
    entries = allowlist.load("web-baseline=127.0.0.1:8080,k8s-rbac=my-ctx/my-ns\n# a comment\nweb-baseline=example.com")
    assert entries == [("web-baseline", "127.0.0.1:8080"), ("k8s-rbac", "my-ctx/my-ns"),
                        ("web-baseline", "example.com")]


def test_empty_allowlist_parses_to_nothing():
    assert allowlist.load("") == []


@pytest.mark.parametrize("bad", ["no-equals-sign", "=emptyprofile", "profile="])
def test_malformed_entries_are_rejected(bad):
    with pytest.raises(ValueError):
        allowlist.load(bad)


def test_exact_match_is_allowed_and_nothing_else_is():
    entries = allowlist.load("web-baseline=127.0.0.1:8080")
    assert allowlist.is_allowed("web-baseline", "127.0.0.1:8080", entries)
    assert not allowlist.is_allowed("web-baseline", "127.0.0.1:9999", entries)
    assert not allowlist.is_allowed("k8s-rbac", "127.0.0.1:8080", entries)  # right target, wrong profile


def test_a_wildcard_suffix_allows_only_that_prefix_family():
    entries = allowlist.load("k8s-rbac=kind-deploy-scan-test/*")
    assert allowlist.is_allowed("k8s-rbac", "kind-deploy-scan-test/default", entries)
    assert allowlist.is_allowed("k8s-rbac", "kind-deploy-scan-test/kube-system", entries)
    assert not allowlist.is_allowed("k8s-rbac", "some-other-cluster/default", entries)


def test_an_unlisted_target_is_refused_fail_closed():
    assert not allowlist.is_allowed("web-baseline", "anything", [])
