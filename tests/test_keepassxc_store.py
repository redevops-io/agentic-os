"""KeePassXC SecretStore + the reference resolver, against a fake keepassxc-cli (no KeePassXC needed).

The fake models the CLI contract the store relies on (show/add/edit/mkdir/rm, master password on stdin, key file or
--no-password). A real-CLI lane lives in test_keepassxc_real_cli (opt-in: REDEVOPS_TEST_KEEPASSXC_CLI)."""
import os

import pytest

from agentic_os.secrets import KeePassXCSecretStore, build_secret_store, read_secret, store_secret
from agentic_os.secrets.resolve import SecretReferenceError, parse_ref
from runtime_contracts.protocol.secrets import SecretRef
from runtime_contracts.secrets_local.store import SecretAccessError

MASTER = "correct horse battery"


class FakeCli:
    """Just enough keepassxc-cli: entries by 'Group/Title', master password required on stdin."""

    def __init__(self, require_master=True):
        self.entries: dict = {}
        self.groups = {""}
        self.calls = []
        self.require_master = require_master

    def __call__(self, args, stdin):
        self.calls.append((list(args), stdin))
        cmd, rest = args[1], list(args[2:])
        lines = stdin.split("\n")
        if self.require_master and "--no-password" not in rest:
            if lines[0] != MASTER:
                return 1, "", "Error: Failed to open database (invalid credentials)"
            lines = lines[1:]
        entry = args[-1]
        if cmd == "show":
            if entry not in self.entries:
                return 1, "", "Error: Could not find entry with path " + entry
            attr = rest[rest.index("-a") + 1] if "-a" in rest else "Password"
            return 0, self.entries[entry].get(attr, "") + "\n", ""
        if cmd in ("add", "edit"):
            pw = lines[0]
            if cmd == "add" and entry in self.entries:
                return 1, "", "Error: Entry already exists"
            if cmd == "edit" and entry not in self.entries:
                return 1, "", "Error: Could not find entry with path " + entry
            self.entries.setdefault(entry, {"Title": entry.split("/")[-1]})["Password"] = pw
            return 0, "", ""
        if cmd == "mkdir":
            self.groups.add(entry)
            return 0, "", ""
        if cmd == "rm":
            return (0, "", "") if self.entries.pop(entry, None) is not None else (1, "", "Error: not found")
        return 1, "", "unknown"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("KP_MASTER", MASTER)


def _store(fake=None, **kw):
    fake = fake or FakeCli()
    return KeePassXCSecretStore("/db/vault.kdbx", password_env="KP_MASTER", runner=fake, **kw), fake


def test_put_then_read_roundtrip(env):
    store, fake = _store()
    ref = store.put(namespace="Redevops", path="LLM", value=b"sk-live-123")
    assert ref.provider == "keepassxc" and "sk-live" not in repr(ref)
    assert store._read(SecretRef(provider="keepassxc", namespace="Redevops", path="LLM")) == b"sk-live-123"
    assert fake.entries["Redevops/LLM"]["Password"] == "sk-live-123"


def test_put_again_updates_in_place_so_a_rotation_is_a_write(env):
    store, fake = _store()
    store.put(namespace="Redevops", path="LLM", value=b"old")
    store.put(namespace="Redevops", path="LLM", value=b"new")
    assert store._read(SecretRef(provider="keepassxc", namespace="Redevops", path="LLM")) == b"new"
    assert [c[0][1] for c in fake.calls if c[0][1] in ("add", "edit")] == ["add", "edit"]


def test_the_master_password_goes_to_stdin_never_to_argv(env):
    store, fake = _store()
    store.put(namespace="G", path="E", value=b"v")
    for args, stdin in fake.calls:
        assert MASTER not in " ".join(args) and "v" not in args[-1:]
    assert any(stdin.startswith(MASTER + "\n") for _, stdin in fake.calls)


def test_a_missing_master_variable_or_wrong_password_is_a_clean_refusal(monkeypatch):
    monkeypatch.delenv("KP_MASTER", raising=False)
    store, _ = _store()
    with pytest.raises(SecretAccessError, match="not set"):
        store._read(SecretRef(provider="keepassxc", namespace="G", path="E"))
    monkeypatch.setenv("KP_MASTER", "wrong")
    store, _ = _store()
    with pytest.raises(SecretAccessError, match="could not be unlocked"):
        store._read(SecretRef(provider="keepassxc", namespace="G", path="E"))


def test_missing_entry_and_path_traversal(env):
    store, _ = _store()
    with pytest.raises(SecretAccessError, match="not found"):
        store._read(SecretRef(provider="keepassxc", namespace="G", path="Nope"))
    with pytest.raises(SecretAccessError, match="invalid"):
        store._read(SecretRef(provider="keepassxc", namespace="..", path="x"))


def test_key_file_only_database_needs_no_password_env():
    fake = FakeCli(require_master=False)
    store = KeePassXCSecretStore("/db/v.kdbx", key_file="/keys/k.keyx", runner=fake)
    store.put(namespace="G", path="E", value=b"v")
    flags = fake.calls[0][0]
    assert "--no-password" in flags and "-k" in flags and "/keys/k.keyx" in flags


def test_constructor_refuses_an_unusable_configuration():
    with pytest.raises(SecretAccessError):
        KeePassXCSecretStore("/db/v.kdbx")
    with pytest.raises(SecretAccessError):
        KeePassXCSecretStore("", password_env="X")


def test_a_secret_with_a_newline_is_refused(env):
    store, _ = _store()
    with pytest.raises(SecretAccessError):
        store.put(namespace="G", path="E", value=b"a\nb")


# ── resolver over the broker ────────────────────────────────────────────────────────────────────
def test_parse_ref():
    assert parse_ref("file:llm/provider") == ("file", "llm", "provider")
    assert parse_ref("keepassxc:Redevops/LLM") == ("keepassxc", "Redevops", "LLM")
    assert parse_ref("env:OPENAI_API_KEY") == ("env", "", "OPENAI_API_KEY")
    for bad in ("", "file", "file:onlyns", "file:../x/y", "x:a/b\nc"):
        with pytest.raises(SecretReferenceError):
            parse_ref(bad)


def test_read_secret_through_the_broker_from_keepassxc(env):
    store, fake = _store()
    store.put(namespace="Redevops", path="LLM", value=b"sk-from-kp")
    got = read_secret("keepassxc:Redevops/LLM", database="/db/vault.kdbx", password_env="KP_MASTER", runner=fake)
    assert got == "sk-from-kp"
    assert read_secret("keepassxc:Redevops/Missing", database="/db/vault.kdbx", password_env="KP_MASTER",
                       runner=fake) is None


def test_read_secret_picks_up_a_rotation_on_the_next_call(tmp_path, monkeypatch):
    monkeypatch.setenv("REDEVOPS_SECRET_DIR", str(tmp_path))
    store_secret("file:llm/provider", "key-1")
    assert read_secret("file:llm/provider") == "key-1"
    store_secret("file:llm/provider", "key-2")
    assert read_secret("file:llm/provider") == "key-2"           # no restart, no re-install


def test_env_refs_are_read_only_for_store_secret(monkeypatch):
    monkeypatch.setenv("MY_KEY", "abc")
    assert read_secret("env:MY_KEY") == "abc"
    with pytest.raises(SecretReferenceError):
        store_secret("env:MY_KEY", "new")


def test_factory_builds_a_keepassxc_store_from_config(monkeypatch):
    monkeypatch.setenv("REDEVOPS_KEEPASSXC_DB", "/db/v.kdbx")
    monkeypatch.setenv("REDEVOPS_KEEPASSXC_PASSWORD_ENV", "KP_MASTER")
    assert isinstance(build_secret_store("keepassxc"), KeePassXCSecretStore)


# ── opt-in lane: the REAL keepassxc-cli (skipped when it is not installed) ─────────────────────────────────────
import shutil
import subprocess

_REAL = shutil.which("keepassxc-cli") or os.environ.get("KEEPASSXC_CLI")


@pytest.mark.skipif(not _REAL, reason="keepassxc-cli not installed")
def test_real_cli_roundtrip_update_missing_wrong_password_and_revoke(tmp_path, monkeypatch):
    db = str(tmp_path / "v.kdbx")
    subprocess.run([_REAL, "db-create", "-p", db], input="master-pass-123\nmaster-pass-123\n", text=True,
                   capture_output=True, check=True, timeout=60)
    monkeypatch.setenv("KP_MASTER", "master-pass-123")
    store = KeePassXCSecretStore(db, password_env="KP_MASTER", cli=_REAL)
    ref = SecretRef(provider="keepassxc", namespace="Redevops", path="LLM")
    store.put(namespace="Redevops", path="LLM", value=b"sk-real-1")
    assert store._read(ref) == b"sk-real-1"
    store.put(namespace="Redevops", path="LLM", value=b"sk-real-2")                   # a rotation is a write
    assert store._read(ref) == b"sk-real-2"
    with pytest.raises(SecretAccessError, match="not found"):
        store._read(SecretRef(provider="keepassxc", namespace="Redevops", path="Nope"))
    monkeypatch.setenv("KP_MASTER", "wrong")
    with pytest.raises(SecretAccessError, match="could not be unlocked"):
        store._read(ref)
    monkeypatch.setenv("KP_MASTER", "master-pass-123")
    store.revoke(ref)
    with pytest.raises(SecretAccessError, match="not found"):
        store._read(ref)


@pytest.mark.skipif(not _REAL, reason="keepassxc-cli not installed")
def test_real_cli_key_file_database_needs_no_password(tmp_path):
    db, key = str(tmp_path / "k.kdbx"), str(tmp_path / "k.keyx")
    subprocess.run([_REAL, "db-create", "--set-key-file", key, db], text=True, capture_output=True, check=True,
                   timeout=60, input="")
    store = KeePassXCSecretStore(db, key_file=key, cli=_REAL)
    store.put(namespace="G", path="E", value=b"via-keyfile")
    assert read_secret("keepassxc:G/E", database=db, key_file=key, cli=_REAL) == "via-keyfile"
