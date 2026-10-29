"""The plain sentence a deployer must not miss: basic permissions authorise the app INSTANCE, not callers.

It is printed by ``agentic-os permissions init`` (always, and in particular with ``--include-write``, where it matters), and repeated in the README of every gated app, in apps/README.md and in the quickstart
documentation.
"""
from __future__ import annotations

import pathlib

import pytest

from agentic_os import cli, permissions_setup
from agentic_os.tool_actions import TOOL_ACTIONS

SENTENCE = "basic permissions authorise the app INSTANCE, not callers: an instance with write grants lets every caller who can reach it write"
ROOT = pathlib.Path(__file__).resolve().parents[1]


def run_init(capsys, monkeypatch, tmp_path, *extra):
    monkeypatch.delenv("PERMISSIONS_KEY", raising=False)
    monkeypatch.delenv("PERMISSIONS_KEY_FILE", raising=False)
    rc = cli.main(["permissions", "init", "--dir", str(tmp_path / "perm"), *extra])
    assert rc == 0
    return capsys.readouterr().out


def test_the_constant_is_the_sentence():
    assert permissions_setup.INSTANCE_NOT_CALLERS == SENTENCE


def test_init_include_write_prints_it(capsys, monkeypatch, tmp_path):
    out = run_init(capsys, monkeypatch, tmp_path, "--include-write")
    assert SENTENCE in out


def test_init_without_write_prints_it_too(capsys, monkeypatch, tmp_path):
    assert SENTENCE in run_init(capsys, monkeypatch, tmp_path)


def test_key_only_init_does_not_need_it(capsys, monkeypatch, tmp_path):
    out = run_init(capsys, monkeypatch, tmp_path, "--key-only")
    assert "grants   " not in out and "granted" not in out and SENTENCE not in out         # no grants were created, so there is no instance grant to warn about


@pytest.mark.parametrize("app", sorted(TOOL_ACTIONS))
def test_every_gated_apps_readme_says_it(app):
    text = (ROOT / "apps" / app / "README.md").read_text()
    assert SENTENCE in text, app


@pytest.mark.parametrize("path", ["apps/README.md", "docs/PERMISSIONS_MIGRATION.md", "docs/SECURITY_MODEL.md"])
def test_the_quickstart_documentation_says_it(path):
    assert SENTENCE in (ROOT / path).read_text(), path
