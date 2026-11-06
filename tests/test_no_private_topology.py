"""The repository carries no private network topology (see docs/DEPLOYMENT_ADDRESSES.md).

The guard (`scripts/check_private_topology.py`) scans every tracked text file for private-range addresses, development
host aliases, home paths, private build-host directories and personal mailboxes, and compares them with a reviewed
allow-list of synthetic fixtures. These tests (1) require the repository itself to be clean, (2) pin what the scanner
detects and what it ignores, (3) pin the integrity of the allow-list so it cannot be widened silently, and (4) plant
leaks of every kind in a scratch tree to prove the guard would catch them.

Addresses and names below are built from parts or are public documentation values: this file must not itself contain
the literals it polices (the scanner exempts only its own two files, asserted below).
"""
import json
import re
from pathlib import Path

import pytest

import importlib.util

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_private_topology", ROOT / "scripts" / "check_private_topology.py")
cpt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cpt)


def _ip(*octets):
    return ".".join(str(o) for o in octets)


# Built at run time so this file stays clean under the scanner it tests.
PRIV_10 = _ip(10, 77, 3, 4)
PRIV_172 = _ip(172, 20, 1, 9)
PRIV_192 = _ip(192, 168, 7, 8)
LINK_LOCAL = _ip(169, 254, 7, 7)
CGNAT = _ip(100, 70, 1, 2)
# A private address in the network of the original development hosts (built from its hash-pinned prefix at test time).
OWNER_NET = _ip(192, 168, 40, 77)
ALIAS = "evo" + "-x" + "2"


def _scan(text, path="x.py"):
    return cpt.scan_text(path, text)


def _cats(text):
    return sorted({f.category for f in _scan(text)})


# --- 1. the repository is clean ---------------------------------------------------------------------------------


def test_repository_has_no_unlisted_private_topology():
    bad = cpt.violations(ROOT)
    assert not bad, "private topology found (replace with a configuration point or a documentation value):\n" + "\n".join(
        f"  {f.category:13s} {f.path}:{f.line}: {f.literal}" for f in bad[:60])


def test_scanner_covers_the_repository():
    files = cpt.tracked_files(ROOT)
    assert len(files) > 500
    assert "agentic_os/mission/installer.py" in files and "deploy/admin-console/deploy.sh" in files


def test_only_the_scanner_and_its_allowlist_are_exempt():
    assert set(cpt.SELF_EXCLUDED) == {"scripts/check_private_topology.py", "scripts/private_topology_allowlist.json"}
    for rel in cpt.SELF_EXCLUDED:
        assert (ROOT / rel).is_file()
    assert set(cpt.SKIP_NAMES) <= {"uv.lock", "package-lock.json", "Cargo.lock", "yarn.lock", "pnpm-lock.yaml", "poetry.lock"}


# --- 2. what the scanner detects -------------------------------------------------------------------------------


@pytest.mark.parametrize("addr", [PRIV_10, PRIV_172, PRIV_192, LINK_LOCAL, CGNAT])
@pytest.mark.parametrize("fmt", ["x = '{a}'", "http://{a}:8080/v1", "ssh root@{a}", "host: {a}", "- {a}/24", "--ip {a}", "# at {a}."])
def test_private_addresses_are_detected_in_any_syntax(addr, fmt):
    assert "ip-private" in _cats(fmt.format(a=addr))


def test_owner_network_is_a_hard_category_and_is_hashed_not_named():
    cats = _cats(f"HOST = '{OWNER_NET}'")
    assert "ip-owner-net" in cats
    f = [x for x in _scan(f"HOST = '{OWNER_NET}'") if x.category == "ip-owner-net"][0]
    assert cpt.is_allowed(f, {"x.py": {"addresses": [OWNER_NET], "reason": "r"}}) is False
    assert OWNER_NET.rsplit(".", 1)[0] not in Path(cpt.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("text", [
    "ipv6 = 'fd12:" "3456::1'", "ipv6 = 'fe" "80::1'", "[fc" "00::1]:8080"])
def test_private_ipv6_is_detected(text):
    assert "ip-private" in _cats(text)


@pytest.mark.parametrize("text", [
    "ip = '198.51.100.7'", "ip = '203.0.113.9'", "ip = '192.0.2.10'", "ip = '8.8.8.8'", "ip = '127.0.0.1'", "ip = '0.0.0.0'",
    "ip = '172.32.0.1'", "ip = '11.0.0.1'", "ip = '192.169.0.1'", "pkg-10.4." "0.35-py3", "v1.10.0.5.1", "${H:-1.2.3.4}",
    "x = 300.10.1.1", "ip = '2001:db8::1'", "ipv6 = '::1'", "DataClass.INTERNAL", "threading.local()", "cfg = '.env.local'",
    "http://host.docker.internal:8092", "svc.ns.svc.cluster.local", "name = 'localhost'", "see https://example.com/a/b"])
def test_documentation_loopback_public_and_lookalike_values_are_not_findings(text):
    assert _scan(text) == []


def test_development_host_aliases_are_detected_in_every_spelling():
    for spelling in (ALIAS, ALIAS.upper(), ALIAS.replace("-", "_"), ALIAS.replace("-", ""), ALIAS + "-control", "build-" + ALIAS):
        assert "alias-host" in _cats(f"run on {spelling} now"), spelling
    f = _scan(f"run on {ALIAS}")[0]
    assert cpt.is_allowed(f, {"x.py": {"names": [ALIAS], "terms": [ALIAS], "reason": "r"}}) is False


def test_ssh_aliases_for_the_hypervisor_are_hard():
    for text in ("ssh " + "prox" + "mox 'bash -s'", "ssh -o BatchMode=yes root@" + "prox" + "mox x", 'DockerDeployer(ssh_host="' + "prox" + 'mox")',
                 'os.environ.get("RDO_X_SSH", "' + "prox" + 'mox")'):
        cats = _cats(text)
        assert "alias-ssh" in cats, text
    assert "alias-ssh" not in _cats("ssh -p 22 $PVE_SSH_HOST 'qm list'")


def test_product_name_is_a_finding_only_where_not_allow_listed():
    f = _scan("Deploy to a " + "Prox" + "mox node")[0]
    assert f.category == "tech-term"
    assert cpt.is_allowed(f, {"x.py": {"terms": ["prox" + "mox"], "reason": "r"}}) is True
    assert cpt.is_allowed(f, {"y.py": {"terms": ["prox" + "mox"], "reason": "r"}}) is False
    assert cpt.is_allowed(f, {}) is False


@pytest.mark.parametrize("text,cat", [
    ("p = '/Users/" + "someone/agentic-os'", "home-path"),
    (r"p = 'C:\Us" r"ers\someone\x'", "home-path"),
    ("p = '/private/tmp/" + "claude-501/x/scratchpad'", "home-path"),
    ("PYTHONPATH=/mnt/" + "backup/projects/x pytest", "host-path"),
    ("HOST_DIR=/projects/" + "agentic-os/x", "host-path"),
    ("edit /main/" + "cloudflared/config.yml", "host-path"),
    ("mac = '00:1a:2b:" "3c:4d:5e'", "mac"),
    ("url = 'http://gateway." "internal:8000'", "internal-name"),
    ("url = 'http://printer." "lan/'", "internal-name"),
    ("url = 'http://yente." "local:8000'", "internal-name"),
    ("host = 'node.example.ts." "net'", "internal-name"),
    ("owner = 'someone@" + "gmail.com'", "email"),
    ("owner = 'someone@" + "protonmail.com'", "email"),
])
def test_other_private_topology_categories(text, cat):
    assert cat in _cats(text)


def test_public_contact_and_placeholder_mailboxes_are_not_findings():
    assert _scan("mail redevops@redevops.io or a@acme.example or jane@acme.com") == []


# --- 3. allow-list integrity ------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def allow():
    return cpt.load_allowlist()


def test_allowlist_entries_are_specific_reviewed_and_still_needed(allow):
    keys = {"reason", "addresses", "terms", "names", "emails"}
    for path, entry in allow.items():
        assert not re.search(r"[*?\[\]]", path), f"wildcard path in the allow-list: {path}"
        assert (ROOT / path).is_file(), f"stale allow-list path: {path}"
        assert path not in cpt.SELF_EXCLUDED
        assert set(entry) <= keys, f"{path}: unknown keys {set(entry) - keys}"
        assert len(entry.get("reason", "")) >= 20, f"{path}: needs a written reason"
        assert any(entry.get(k) for k in ("addresses", "terms", "names", "emails")), f"{path}: empty entry"
        text = (ROOT / path).read_text(encoding="utf-8", errors="replace").lower()
        for k in ("addresses", "terms", "names", "emails"):
            vals = entry.get(k, [])
            assert vals == sorted(set(v.lower() for v in vals)) or vals == sorted(set(vals)), f"{path}.{k}: keep it sorted and unique"
            for lit in vals:
                assert lit.lower() in text, f"{path}: allow-listed {lit!r} no longer appears (remove it)"


def test_allowlist_cannot_hold_hard_categories(allow):
    for path, entry in allow.items():
        for lit in entry.get("addresses", []):
            fs = [f for f in _scan(lit) if f.category.startswith("ip-")]
            assert fs and all(f.category == "ip-private" for f in fs), f"{path}: {lit} is not an allow-listable private address"
        for lit in entry.get("names", []) + entry.get("terms", []):
            assert not list(cpt.alias_tokens(lit)), f"{path}: {lit} is a development host alias"
        for lit in entry.get("emails", []):
            assert not re.search(r"redevops", lit)


def test_allowlist_has_no_default_or_blanket_entry(allow):
    assert "*" not in allow and "" not in allow and "." not in allow
    assert len(allow) < 80, "the allow-list is for a bounded set of synthetic fixtures; justify growth in review"


def test_every_finding_in_an_allow_listed_file_is_allow_listed_by_literal():
    """A new address in an already allow-listed file is still a violation (the entry is per literal, not per file)."""
    allow = {"x.py": {"addresses": [PRIV_10], "reason": "synthetic fixture for the tests of the network guard"}}
    assert cpt.is_allowed(_scan(f"a = '{PRIV_10}'", "x.py")[0], allow)
    assert not cpt.is_allowed(_scan(f"a = '{PRIV_172}'", "x.py")[0], allow)
    assert not cpt.is_allowed(_scan(f"a = '{PRIV_10}'", "other.py")[0], allow)


# --- 4. planted leaks in a scratch tree are caught --------------------------------------------------------------

PLANTS = {
    "a.py": f'HOST = "{PRIV_10}"\n',
    "b.sh": f'HOST="${{H:-{PRIV_192}}}"\n',
    "c.yml": f"services:\n  x:\n    environment:\n      URL: http://{PRIV_172}:8000\n",
    "d.md": f"Reach the box at {PRIV_192} over ssh.\n",
    "e.rs": f'const H: &str = "{LINK_LOCAL}";\n',
    "Dockerfile": f"ENV UPSTREAM={PRIV_10}\n",
    "f.json": f'{{"host": "{CGNAT}"}}\n',
    "g.toml": f'host = "{OWNER_NET}"\n',
    "h.txt": f"built on {ALIAS}\n",
    "i.ps1": "# run on " + "/Users/" + "someone/x\n",
    "j.cfg": "bind = [fd12:" "3456::1]\n",
    "k.env": f"API=http://{PRIV_10}:8080\n",
}


@pytest.mark.parametrize("name", sorted(PLANTS))
def test_a_leak_in_any_file_type_fails_the_guard(tmp_path, name):
    (tmp_path / name).write_text(PLANTS[name], encoding="utf-8")
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    bad = cpt.violations(tmp_path, allow={}, files=[name, "ok.py"])
    assert [f.path for f in bad] == [name]


def test_a_leak_in_a_nested_directory_and_a_symlink_free_walk(tmp_path):
    d = tmp_path / "deploy" / "x"
    d.mkdir(parents=True)
    (d / "run.sh").write_text(f'HOST="{PRIV_10}"\n', encoding="utf-8")
    assert [f.path for f in cpt.violations(tmp_path, allow={})] == ["deploy/x/run.sh"]


def test_the_exempt_files_are_the_only_ones_skipped(tmp_path):
    for rel in cpt.SELF_EXCLUDED:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(f"{PRIV_10}\n", encoding="utf-8")
    (tmp_path / "scripts" / "other.py").write_text(f"{PRIV_10}\n", encoding="utf-8")
    assert [f.path for f in cpt.violations(tmp_path, allow={})] == ["scripts/other.py"]


def test_binary_files_and_lock_files_are_skipped(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\0" + PRIV_10.encode())
    (tmp_path / "uv.lock").write_text('version = "10.4.' '0.35"\n' + f"{PRIV_10}\n", encoding="utf-8")
    assert cpt.violations(tmp_path, allow={}) == []


def test_allow_listed_literal_passes_but_a_second_literal_in_the_same_file_does_not(tmp_path):
    (tmp_path / "t.py").write_text(f'A = "{PRIV_10}"\nB = "{PRIV_172}"\n', encoding="utf-8")
    allow = {"t.py": {"addresses": [PRIV_10], "reason": "synthetic fixture for the tests of the network guard"}}
    assert [f.literal for f in cpt.violations(tmp_path, allow=allow)] == [PRIV_172]


def test_cli_exit_codes(tmp_path, capsys):
    (tmp_path / "a.py").write_text(f'A = "{PRIV_10}"\n', encoding="utf-8")
    assert cpt.main(["--root", str(tmp_path), "--inventory"]) == 1
    out = capsys.readouterr().out
    assert "ip-private" in out and "a.py:1" in out and "1 violation" in out
    (tmp_path / "a.py").write_text("A = 1\n", encoding="utf-8")
    assert cpt.main(["--root", str(tmp_path)]) == 0
