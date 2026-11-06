"""The Vault KV mount is selected explicitly; no code path, document or script defaults to a particular mount.

Vault is reached only when VAULT_ADDR and VAULT_TOKEN are set. The mount then comes from REDEVOPS_VAULT_MOUNT (the older
name VAULT_MOUNT is accepted as an alias). With neither, every Vault lookup stops with an error that names
REDEVOPS_VAULT_MOUNT: nothing falls back to a built-in mount and nothing is silently skipped.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_os import cli
from agentic_os.secrets import bootstrap as bs
from agentic_os.secrets.managed import APPROVAL, NAMES

ROOT = Path(__file__).resolve().parent.parent
BASE = {"VAULT_ADDR": "http://v:8200", "VAULT_TOKEN": "t"}
OLD = "vibex" + "gen"       # the mount name that used to be the default (built from parts: this file is scanned too)


class FakeVault:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        return {"data": {"data": {}}}


def mk(tmp_path, env, **kw):
    e = {"HOME": str(tmp_path), "APPDATA": str(tmp_path / "AppData"), **env}
    kw.setdefault("keyring", None)
    return bs.Bootstrap(environ=e, platform="linux", home=tmp_path, **kw)


def test_there_is_no_default_mount_constant():
    assert not hasattr(bs, "DEFAULT_VAULT_MOUNT")


@pytest.mark.parametrize("extra", [{}, {"REDEVOPS_VAULT_MOUNT": ""}, {"REDEVOPS_VAULT_MOUNT": "   "}, {"VAULT_MOUNT": ""}, {"VAULT_MOUNT": "\t"}])
def test_a_vault_lookup_without_a_mount_fails_naming_the_variable(tmp_path, extra):
    t = FakeVault()
    b = mk(tmp_path, {**BASE, **extra}, vault_transport=t)
    with pytest.raises(bs.VaultMountMissing, match="REDEVOPS_VAULT_MOUNT"):
        b.vault()
    assert isinstance(bs.VaultMountMissing("x"), bs.SecretsError)
    assert t.calls == [], "no request may be made to a guessed mount"


def test_every_vault_operation_stops_without_a_mount_and_none_swallows_it(tmp_path):
    t = FakeVault()
    for op in (lambda b: b.init(), lambda b: b.push(), lambda b: b.rotate(APPROVAL), lambda b: b.resolve(APPROVAL),
               lambda b: b.status_rows(), lambda b: b.vault_location()):
        b = mk(tmp_path / "h", BASE, vault_transport=t)
        with pytest.raises(bs.VaultMountMissing, match="REDEVOPS_VAULT_MOUNT"):
            op(b)
    assert t.calls == []


def test_the_startup_hook_reports_the_missing_mount_by_name_and_does_nothing_else(tmp_path, caplog):
    t = FakeVault()
    with caplog.at_level("WARNING"):
        assert bs.ensure_secrets(environ={**BASE, "HOME": str(tmp_path)}, keyring=None, vault_transport=t, home=tmp_path) is None
    assert "REDEVOPS_VAULT_MOUNT" in caplog.text and t.calls == [] and not (tmp_path / ".config").exists()


def test_without_vault_configured_no_mount_is_needed(tmp_path):
    b = mk(tmp_path, {})
    assert b.init()["backend"] == "file"
    assert [r["name"] for r in b.status_rows()] == list(NAMES)


def test_the_selected_mount_is_used_and_the_new_name_wins_over_the_alias(tmp_path):
    t = FakeVault()
    b = mk(tmp_path, {**BASE, "VAULT_MOUNT": "legacy", "REDEVOPS_VAULT_MOUNT": "chosen"}, vault_transport=t)
    assert b.vault().mount == "chosen"
    assert mk(tmp_path, {**BASE, "VAULT_MOUNT": "legacy"}, vault_transport=t).vault().mount == "legacy"
    assert mk(tmp_path, {**BASE, "REDEVOPS_VAULT_MOUNT": " padded "}, vault_transport=t).vault().mount == "padded"


def test_the_cli_reports_a_missing_mount_by_name(tmp_path, monkeypatch, capsys):
    for k, v in {"HOME": str(tmp_path), "REDEVOPS_SECRETS_FILE": str(tmp_path / "s.env"), **BASE}.items():
        monkeypatch.setenv(k, v)
    for n in ("VAULT_MOUNT", "REDEVOPS_VAULT_MOUNT", "REDEVOPS_VAULT_PATH"):
        monkeypatch.delenv(n, raising=False)
    rc = cli.main(["secrets", "init"])
    out = capsys.readouterr()
    assert rc != 0 and "REDEVOPS_VAULT_MOUNT" in out.out + out.err


def test_the_where_hint_names_the_variable_and_no_mount(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("REDEVOPS_SECRETS_FILE", str(tmp_path / "s.env"))
    for n in ("VAULT_ADDR", "VAULT_TOKEN", "VAULT_MOUNT", "REDEVOPS_VAULT_MOUNT"):
        monkeypatch.delenv(n, raising=False)
    assert cli.main(["secrets", "where"]) == 0
    out = capsys.readouterr().out
    assert "REDEVOPS_VAULT_MOUNT" in out and OLD not in out.lower()


def _tracked():
    raw = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"], capture_output=True, check=True).stdout
    return [p for p in raw.decode().split("\0") if p]


def test_no_file_in_the_repository_defaults_to_that_mount():
    """No code, script, document or test names the old mount as a Vault mount or as the first segment of a Vault path.
    (The vibexgen product, its URLs and fixture names are not Vault mounts and are not matched.)"""
    pats = [re.compile(r"mount[= `'\"]+" + OLD, re.I), re.compile(OLD + r"/(?:data/)?agentic-os"), re.compile(OLD + r"/postmark"),
            re.compile(r"Vault " + OLD, re.I), re.compile(r"DEFAULT_VAULT_MOUNT"), re.compile(r"VAULT_MOUNT\s*=\s*" + OLD, re.I),
            re.compile(r"default " + OLD, re.I), re.compile(r"mount " + OLD, re.I)]
    me = Path(__file__).resolve()
    bad = []
    for rel in _tracked():
        p = ROOT / rel
        if p.resolve() == me or not p.is_file() or rel.endswith(".lock") or rel.startswith("docs/legal/"):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if any(pt.search(line) for pt in pats):
                bad.append(f"{rel}:{n}")
    assert not bad, "a Vault mount default is back:\n" + "\n".join(bad)
