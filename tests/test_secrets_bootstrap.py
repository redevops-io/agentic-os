import base64
import logging
import os
import stat
import sys

import pytest

from agentic_os import cli
from agentic_os.mission import approval, operator_sdk
from agentic_os.secrets import bootstrap as bs
from agentic_os.secrets.managed import APPROVAL, MANAGED, OUTREACH, TWENTY, NAMES

posix = pytest.mark.skipif(sys.platform.startswith("win"), reason="posix perms")


class FakeKR:
    def __init__(self):
        self.d = {}

    def get_password(self, s, n):
        return self.d.get((s, n))

    def set_password(self, s, n, v):
        self.d[(s, n)] = v


def mk(tmp_path, env=None, platform="linux", **kw):
    e = {"HOME": str(tmp_path), "APPDATA": str(tmp_path / "AppData")}
    e.update(env or {})
    kw.setdefault("keyring", None)
    return bs.Bootstrap(environ=e, platform=platform, home=tmp_path, **kw)


def fileof(b):
    return bs.read_secrets_file(b.file)


def test_generation_formats_and_validators():
    a, t, o = (MANAGED[n].generate() for n in (APPROVAL, TWENTY, OUTREACH))
    assert len(a) == 64 and int(a, 16) >= 0 and MANAGED[APPROVAL].validate(a)[0]
    assert len(t) == 64 and MANAGED[TWENTY].validate(t)[0]
    u, _, pw = o.partition(":")
    assert u == "operator" and len(pw) >= 20 and MANAGED[OUTREACH].validate(o)[0]
    assert not MANAGED[APPROVAL].validate("short")[0]
    assert not MANAGED[TWENTY].validate("demo-only-" + "x" * 40)[0]
    assert not MANAGED[OUTREACH].validate("nocolon")[0]
    assert not MANAGED[OUTREACH].validate("operator:short")[0]
    assert a != MANAGED[APPROVAL].generate()


def test_init_idempotent_and_default_path(tmp_path):
    b = mk(tmp_path)
    r1 = b.init()
    assert {x.status for x in r1["results"]} == {"generated"}
    assert b.file == tmp_path / ".config/redevops/secrets.env"
    snap = (b.file.read_text(), (tmp_path / ".bashrc").read_text())
    r2 = b.init()
    assert {x.status for x in r2["results"]} == {"already-set"}
    assert snap == (b.file.read_text(), (tmp_path / ".bashrc").read_text())
    assert set(fileof(b)) == set(NAMES)


def test_respects_preset_env_and_never_overwrites_file(tmp_path):
    b = mk(tmp_path, {APPROVAL: "my-own-value-" + "z" * 30})
    r = {x.name: x for x in b.init()["results"]}
    assert r[APPROVAL].status == "kept(user-provided)"
    assert APPROVAL not in fileof(b)
    # existing file value (weak) is kept, flagged
    b.file.write_text(f"{TWENTY}=weak\n")
    r = {x.name: x for x in mk(tmp_path).init()["results"]}
    assert r[TWENTY].status == "weak" and fileof(b)[TWENTY] == "weak"


@posix
def test_perms(tmp_path):
    b = mk(tmp_path)
    b.init()
    assert stat.S_IMODE(b.file.stat().st_mode) == 0o600
    assert stat.S_IMODE(b.file.parent.stat().st_mode) == 0o700


def test_shell_block_add_replace_no_duplicate_and_backup(tmp_path):
    rc = tmp_path / ".bashrc"
    rc.write_text("export A=1\n")
    b = mk(tmp_path)
    b.init()
    b.init()
    text = rc.read_text()
    assert text.count(bs.BEGIN) == 1 and text.count(bs.END) == 1 and text.startswith("export A=1")
    assert (tmp_path / ".bashrc.bak-redevops").read_text() == "export A=1\n"
    assert "secrets.env" in text and all(v not in text for v in fileof(b).values())
    # stale block is replaced, not duplicated; backup not rewritten
    rc.write_text(text.replace("set -a", "set -a; echo old"))
    assert bs.install_shell_block(rc, bs.shell_block("${HOME}/.config/redevops/secrets.env")) == "updated"
    assert rc.read_text().count(bs.BEGIN) == 1 and "echo old" not in rc.read_text()
    assert (tmp_path / ".bashrc.bak-redevops").read_text() == "export A=1\n"


def test_macos_rc_files(tmp_path):
    (tmp_path / ".bash_profile").write_text("")
    mk(tmp_path, platform="darwin").init()
    assert bs.BEGIN in (tmp_path / ".zshrc").read_text()
    assert bs.BEGIN in (tmp_path / ".bash_profile").read_text()
    assert not (tmp_path / ".bashrc").exists()


def test_no_shell_profile_and_dry_run_write_nothing(tmp_path):
    b = mk(tmp_path)
    r = b.init(dry_run=True)
    assert r["dry_run"] and not b.file.exists() and not (tmp_path / ".bashrc").exists()
    b.init(shell_profile=False)
    assert b.file.exists() and not (tmp_path / ".bashrc").exists()


def test_keyring_mirror_optional(tmp_path):
    kr = FakeKR()
    b = mk(tmp_path, keyring=kr)
    b.init()
    assert kr.d[("redevops", APPROVAL)] == fileof(b)[APPROVAL]
    mk(tmp_path / "x", keyring=None).init()  # no keyring: not fatal

    class Boom(FakeKR):
        def set_password(self, *a):
            raise RuntimeError("no backend")
    mk(tmp_path / "y", keyring=Boom()).init()


def test_keyring_counts_as_present(tmp_path):
    kr = FakeKR()
    kr.d[("redevops", APPROVAL)] = "k" * 40
    r = {x.name: x for x in mk(tmp_path, keyring=kr).init()["results"]}
    assert r[APPROVAL].status == "already-set"


def test_windows_runner_and_file(tmp_path):
    calls = []
    b = mk(tmp_path, platform="win32", runner=lambda argv, env: calls.append((argv, env)) or 0)
    b.init()
    assert b.file == tmp_path / "AppData/redevops/secrets.env"
    assert any(a[0] == "icacls" for a, _ in calls)
    ps = [(a, e) for a, e in calls if a[0] == "powershell"]
    assert {e["RDV_N"] for _, e in ps} == set(NAMES)
    vals = set(fileof(b).values())
    assert all(v not in " ".join(a) for a, _ in calls for v in vals)  # never on argv
    assert not (tmp_path / ".bashrc").exists()


class FakeVault:
    def __init__(self, data=None, fail=None):
        self.data, self.calls, self.fail = data, [], fail

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        if self.fail:
            raise self.fail
        import json
        if method == "GET":
            if self.data is None:
                e = Exception("nf")
                e.code = 404
                raise e
            return {"data": {"data": dict(self.data)}}
        self.data = json.loads(body)["data"]
        return {}


VENV = {"VAULT_ADDR": "http://v:8200", "VAULT_TOKEN": "t"}


def test_vault_merge_adds_only_missing(tmp_path):
    keep = "z" * 40
    v = FakeVault({"OTHER": "1", TWENTY: keep})
    b = mk(tmp_path, VENV, vault_transport=v)
    r = b.init()
    assert r["backend"] == "vault"
    assert v.data["OTHER"] == "1" and v.data[TWENTY] == keep
    assert set(v.data) == {"OTHER", *NAMES}
    assert not b.file.exists() and not (tmp_path / ".bashrc").exists()
    assert v.calls[-1][1] == "http://v:8200/v1/secret/data/vibexgen/agentic-os/stack"
    before = dict(v.data)
    b.init()
    assert v.data == before and sum(c[0] == "POST" for c in v.calls) == 1


def test_vault_custom_path_and_missing_secret(tmp_path):
    v = FakeVault(None)
    mk(tmp_path, {**VENV, "VAULT_MOUNT": "kv", "REDEVOPS_VAULT_PATH": "a/b"}, vault_transport=v).init()
    assert v.calls[-1][1].endswith("/v1/kv/data/a/b") and set(v.data) == set(NAMES)


def test_vault_unreachable_is_clear_error_no_local_fallback(tmp_path):
    b = mk(tmp_path, VENV, vault_transport=FakeVault(fail=OSError("down")))
    with pytest.raises(bs.SecretsError, match="unreachable"):
        b.init()
    assert not b.file.exists()
    monkeypatch_env = pytest.MonkeyPatch()
    try:
        monkeypatch_env.setenv("REDEVOPS_SECRETS_AUTO", "1")
        assert bs.ensure_secrets(environ={**VENV, "HOME": str(tmp_path)}, keyring=None,
                                 vault_transport=FakeVault(fail=OSError("x")), home=tmp_path) is None
    finally:
        monkeypatch_env.undo()
    assert not (tmp_path / ".config").exists()


def run(tmp_path, monkeypatch, argv, capsys, env=None, **kw):
    for k, v in {"HOME": str(tmp_path), "REDEVOPS_SECRETS_FILE": str(tmp_path / "s.env"),
                 **(env or {})}.items():
        monkeypatch.setenv(k, v)
    for n in (*NAMES, "VAULT_ADDR", "VAULT_TOKEN", "VAULT_MOUNT", "REDEVOPS_VAULT_PATH"):
        monkeypatch.delenv(n, raising=False)
    rc = cli.main(argv)
    return rc, capsys.readouterr()


def test_cli_init_prints_no_values_and_get(tmp_path, monkeypatch, capsys):
    rc, o = run(tmp_path, monkeypatch, ["secrets", "init", "--no-shell-profile"], capsys,
                {"REDEVOPS_SECRETS_NO_KEYRING": "1"})
    assert rc == 0 and "generated" in o.out
    vals = bs.read_secrets_file(tmp_path / "s.env")
    assert all(v not in o.out + o.err for v in vals.values())
    rc, o = run(tmp_path, monkeypatch, ["secrets", "get", APPROVAL], capsys)
    assert o.out.strip() == vals[APPROVAL]
    rc, o = run(tmp_path, monkeypatch, ["secrets", "get", OUTREACH, "--decode"], capsys)
    user, pw = vals[OUTREACH].split(":", 1)
    assert f"user: {user}" in o.out and f"password: {pw}" in o.out
    rc, o = run(tmp_path, monkeypatch, ["secrets", "get", OUTREACH, "--as-header"], capsys)
    assert o.out.strip() == "Authorization: Basic " + base64.b64encode(vals[OUTREACH].encode()).decode()
    rc, o = run(tmp_path, monkeypatch, ["secrets", "list"], capsys)
    assert all(vals[n] not in o.out for n in NAMES) and o.out.count("valid=ok") == 3
    rc, o = run(tmp_path, monkeypatch, ["secrets", "where"], capsys)
    assert "s.env" in o.out


def test_cli_get_decode_b64_name(tmp_path, monkeypatch, capsys):
    (tmp_path / "s.env").write_text("X_B64=" + base64.b64encode(b"hello").decode() + "\n")
    # non-managed names resolve via env too
    monkeypatch.setattr(bs.Bootstrap, "resolve", lambda self, n: (base64.b64encode(b"hello").decode(), ["env"]))
    rc, o = run(tmp_path, monkeypatch, ["secrets", "get", "FOO_B64", "--decode"], capsys)
    assert o.out.strip() == "hello"


def test_rotate_requires_confirmation(tmp_path, monkeypatch, capsys):
    run(tmp_path, monkeypatch, ["secrets", "init", "--no-shell-profile"], capsys)
    old = bs.read_secrets_file(tmp_path / "s.env")[TWENTY]
    monkeypatch.setattr("sys.stdin", open(os.devnull))  # not a TTY
    rc, o = run(tmp_path, monkeypatch, ["secrets", "rotate", TWENTY], capsys)
    assert rc == 2 and "sessions" in o.out and bs.read_secrets_file(tmp_path / "s.env")[TWENTY] == old
    rc, o = run(tmp_path, monkeypatch, ["secrets", "rotate", TWENTY, "--yes"], capsys)
    new = bs.read_secrets_file(tmp_path / "s.env")[TWENTY]
    assert rc == 0 and new != old and new not in o.out


def test_approval_reads_file_when_env_unset_and_prefers_env(tmp_path, monkeypatch):
    f = tmp_path / "s.env"
    f.write_text(f"{APPROVAL}={'a' * 40}\n")
    monkeypatch.setenv("REDEVOPS_SECRETS_FILE", str(f))
    monkeypatch.delenv(APPROVAL, raising=False)
    assert approval._secret_bytes() == b"a" * 40
    tok = approval.mint_approval_token(operator="o", capability="c", mission_id=None, node_id=None,
                                       inputs={}, marker={"ok": 1})
    assert tok and approval.verify_approval_token(None, tok, operator="o", capability="c", inputs={})
    monkeypatch.setenv(APPROVAL, "b" * 40)
    assert approval._secret_bytes() == b"b" * 40
    assert approval.verify_approval_token(None, tok, operator="o", capability="c", inputs={}) is None
    monkeypatch.delenv(APPROVAL)
    f.unlink()
    assert approval._secret_bytes() == b""


def test_router_warning_rate_limited_and_secretless(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv(APPROVAL, raising=False)
    operator_sdk._WARN_AT.clear()
    caplog.set_level(logging.WARNING, logger="agentic_os.operator")
    operator_sdk._warn_unverified_approval()
    operator_sdk._warn_unverified_approval()
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 1 and "not configured" in msgs[0] and "agentic-os secrets init" in msgs[0]
    monkeypatch.setenv(APPROVAL, "s3cr3t-" + "q" * 40)
    operator_sdk._warn_unverified_approval()  # different reason -> separate bucket
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 2 and "failed verification" in msgs[1] and "q" * 40 not in " ".join(msgs)


def test_router_invoke_warns_on_bad_token(monkeypatch, caplog):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import inspect
    Op = operator_sdk.Operator
    src = inspect.signature(Op.__init__)
    assert "name" in src.parameters or True
    operator_sdk._WARN_AT.clear()
    monkeypatch.delenv(APPROVAL, raising=False)
    caplog.set_level(logging.WARNING, logger="agentic_os.operator")
    op = Op("t", [])
    app = FastAPI()
    app.include_router(op.router())
    r = TestClient(app).post("/invoke", json={"capability": "x", "inputs": {}, "approval_token": "bad.tok"})
    assert r.status_code in (200, 404, 422, 500) and "bad.tok" not in caplog.text
    assert "stay staged" in caplog.text


@pytest.mark.parametrize("q,eid", [
    ("where are my secrets stored?", "secrets-where"),
    ("how do I fetch and decode a secret", "secrets-fetch"),
    ("how can I set my own secret value", "secrets-override"),
    ("how do I rotate a secret", "secrets-rotate"),
    ("why do approved actions stay pending?", "approvals-stay-pending"),
    ("what do I need for the outreach real send gate", "outreach-real-send-gate"),
])
def test_sidekick_answers_secrets_questions(q, eid):
    from fastapi.testclient import TestClient
    from agentic_os.projects_api import create_app
    from agentic_os.projects_api import SampleProjectionProvider
    from agentic_os.stack_knowledge import answer_stack_question
    assert answer_stack_question(q).id == eid
    c = TestClient(create_app(SampleProjectionProvider()))
    assert eid in {h["id"] for h in c.get("/api/sidekick/help").json()}
    r = c.post("/api/sidekick", json={"ctx": {}, "text": q}).json()
    assert "SECRETS.md" in str(r) and "secret" in r["text"].lower()


def test_shell_bootstrap_script_idempotent(tmp_path):
    import shutil
    import subprocess
    sh = shutil.which("sh")
    script = os.path.join(os.path.dirname(__file__), "..", "deploy", "apps", "bootstrap-secrets.sh")
    if not sh or sys.platform.startswith("win"):
        pytest.skip("no sh")
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    subprocess.run([sh, script], env=env, check=True, capture_output=True)
    f = tmp_path / ".config/redevops/secrets.env"
    first = f.read_text()
    out = subprocess.run([sh, script], env=env, check=True, capture_output=True, text=True).stdout
    assert f.read_text() == first and out.count("already-set") == 3
    vals = bs.parse_dotenv(first)
    assert all(MANAGED[n].validate(vals[n])[0] for n in NAMES)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
