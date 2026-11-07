"""Folder policy for sources a desktop user registers (``agentic_os/source_folder_policy.py``).

Every rule is tested with synthetic directories (``tmp_path``) and a synthetic home; no real user folder is read or written. The
Windows forms (drive letters, forward slashes, UNC, device paths, 8.3 short names) are tested through the module's ``windows`` switch with
an injected ``realpath``/``stat``, because the lexical rules are the same text rules on every host; the filesystem rules (exists, is a
directory, symlinks resolved) run for real on the host.
"""
from __future__ import annotations

import os
import socket
import stat
import types
import unicodedata

import pytest

from agentic_os import source_folder_policy as sfp
from agentic_os.source_folder_policy import REFUSALS, check_folder, lexical_key, protected_locations


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home" / "alex"
    h.mkdir(parents=True)
    return h


@pytest.fixture
def real_home(home):
    return os.path.realpath(home)


def ok(raw, home, **kw):
    d = check_folder(raw, home=str(home), env={}, **kw)
    assert d.ok, (raw, d.code)
    return d


def refused(raw, home, code, **kw):
    d = check_folder(raw, home=str(home), env={}, **kw)
    assert not d.ok and d.code == code, (raw, d.ok, d.code)
    return d


# ----------------------------------------------------------------------------------------------------------------------------------
# what is accepted: the exact folder, normalised, with both paths recorded
# ----------------------------------------------------------------------------------------------------------------------------------
def test_an_ordinary_folder_is_accepted_and_both_paths_are_recorded(tmp_path, home):
    d = tmp_path / "docs"
    d.mkdir()
    r = ok(str(d), home)
    assert r.normalised == str(d) and r.real == os.path.realpath(d)


def test_trailing_separators_dots_and_dotdot_are_normalised_to_the_exact_folder(tmp_path, home):
    d = tmp_path / "docs"
    (tmp_path / "other").mkdir()
    d.mkdir()
    for raw in (f"{d}{os.sep}", f"{d}{os.sep}{os.sep}", f"{tmp_path}{os.sep}other{os.sep}..{os.sep}docs", f"{tmp_path}{os.sep}.{os.sep}docs"):
        r = ok(raw, home)
        assert r.normalised == str(d) and r.real == os.path.realpath(d), raw


def test_a_symlink_is_resolved_and_both_the_link_and_the_real_path_are_recorded(tmp_path, home):
    real = tmp_path / "real-docs"
    real.mkdir()
    link = tmp_path / "link-docs"
    os.symlink(real, link)
    r = ok(str(link), home)
    assert r.normalised == str(link) and r.real == os.path.realpath(real) and r.normalised != r.real


def test_tilde_expands_to_the_given_home_only_at_the_start(home):
    sub = home / "Documents"
    sub.mkdir()
    assert ok("~/Documents", home).real == os.path.realpath(sub)
    refused("~", home, "home_directory")
    refused("~/", home, "home_directory")
    refused("~root/x", home, "invalid_path")           # another user's home is not expanded


def test_a_path_registered_twice_gives_one_lexical_key(tmp_path):
    assert lexical_key(f"{tmp_path}{os.sep}x{os.sep}", windows=False) == lexical_key(str(tmp_path / "x" / ".." / "x"), windows=False)
    assert lexical_key("C:/Users/A/Docs", windows=True) == lexical_key("c:\\users\\a\\DOCS\\", windows=True)


# ----------------------------------------------------------------------------------------------------------------------------------
# what is refused: not a usable folder
# ----------------------------------------------------------------------------------------------------------------------------------
def test_a_missing_path_a_file_a_fifo_and_a_socket_are_not_folders(tmp_path, home):
    refused(str(tmp_path / "nope"), home, "not_found")
    f = tmp_path / "file.txt"
    f.write_text("x")
    refused(str(f), home, "not_a_directory")
    link_to_file = tmp_path / "linkfile"
    os.symlink(f, link_to_file)
    refused(str(link_to_file), home, "not_a_directory")
    if hasattr(os, "mkfifo"):
        os.mkfifo(tmp_path / "pipe")
        refused(str(tmp_path / "pipe"), home, "not_a_directory")
    if hasattr(socket, "AF_UNIX"):
        short = tmp_path / "s.sock"
        s = socket.socket(socket.AF_UNIX)
        try:
            s.bind(str(short))
        except OSError:                                    # path too long for AF_UNIX on this host: the fifo/file rows cover the rule
            pass
        else:
            refused(str(short), home, "not_a_directory")
        finally:
            s.close()


def test_device_nodes_are_not_folders():
    # /dev/null exists on every POSIX host; the fixed refusal is the protected-location one (the device tree is protected) or not-a-directory.
    if os.path.exists("/dev/null"):
        d = check_folder("/dev/null", home="/nonexistent-home", env={})
        assert not d.ok and d.code in ("protected_location", "not_a_directory")


@pytest.mark.parametrize("raw", ["", "   ", "\x00", "a\x00b", "/tmp/x\ny", "/tmp/x\ty", "/tmp/\x7fx", "/tmp/*", "/tmp/a?b", "x" * 5000])
def test_empty_control_characters_wildcards_and_oversize_are_invalid(raw, home):
    refused(raw, home, "invalid_path")


def test_non_strings_are_invalid(home):
    for bad in (None, 5, b"/tmp", ["/tmp"], {"a": 1}):
        refused(bad, home, "invalid_path")


@pytest.mark.parametrize("raw", ["docs", "./docs", "../docs", "docs/sub"])
def test_relative_paths_are_refused(raw, home):
    refused(raw, home, "not_absolute")


# ----------------------------------------------------------------------------------------------------------------------------------
# what is refused: places that are never a source
# ----------------------------------------------------------------------------------------------------------------------------------
def test_the_filesystem_root_is_refused(home):
    refused(os.sep, home, "filesystem_root")
    refused(os.sep * 3, home, "filesystem_root")
    anchor = os.path.splitdrive(os.path.abspath(os.sep))[0] + os.sep
    refused(anchor, home, "filesystem_root")


def test_the_home_directory_itself_is_refused_and_so_is_a_parent_of_it_but_not_a_folder_inside_it(home, real_home):
    refused(str(home), home, "home_directory")
    refused(f"{home}{os.sep}", home, "home_directory")
    refused(f"{home}{os.sep}docs{os.sep}..", home, "home_directory")
    refused(str(home.parent), home, "contains_protected_location")
    refused(str(home.parent.parent), home, "contains_protected_location")
    inside = home / "projects" / "notes"
    inside.mkdir(parents=True)
    assert ok(str(inside), home).real == os.path.realpath(inside)
    assert ok(str(home / "projects"), home).ok


@pytest.mark.parametrize("rel", [".ssh", ".aws", ".gnupg", ".kube", ".config/gcloud", ".docker", ".azure", ".config/gh", ".config/redevops",
                                 ".agentic-os", "Library/Keychains", "Library/Application Support/Google/Chrome", "Library/Application Support/Firefox",
                                 "Library/Application Support/BraveSoftware", "Library/Application Support/Microsoft Edge",
                                 ".mozilla", ".config/google-chrome", ".config/chromium", ".config/BraveSoftware", "snap/firefox",
                                 "AppData/Roaming/Mozilla", "AppData/Roaming/Microsoft/Credentials", "AppData/Roaming/Microsoft/Protect",
                                 "AppData/Local/Google/Chrome", "AppData/Local/Microsoft/Edge", "AppData/Roaming/redevops"])
def test_credential_and_browser_profile_locations_are_refused_with_everything_under_and_above_them(rel, home):
    target = home.joinpath(*rel.split("/"))
    target.mkdir(parents=True)
    (target / "inner").mkdir()
    refused(str(target), home, "protected_location")                 # the location
    refused(str(target / "inner"), home, "protected_location")       # a folder inside it
    parent = target.parent
    d = check_folder(str(parent), home=str(home), env={})            # a folder above it holds it: never register a parent
    assert not d.ok and d.code in ("contains_protected_location", "home_directory"), (rel, d.code)


def test_the_config_and_library_roots_are_refused_but_a_project_folder_under_them_is_not(home):
    for rel in (".config", "Library", "Library/Application Support", "AppData", "AppData/Roaming", "AppData/Local", ".local/share"):
        (home / rel).mkdir(parents=True, exist_ok=True)
        refused(str(home / rel), home, "contains_protected_location")
    mine = home / ".config" / "my-notes"
    mine.mkdir()
    assert ok(str(mine), home).ok


def test_a_symlink_to_a_protected_folder_is_refused_by_its_real_path(tmp_path, home):
    ssh = home / ".ssh"
    ssh.mkdir()
    link = tmp_path / "innocent-looking"
    os.symlink(ssh, link)
    d = refused(str(link), home, "protected_location")
    assert d.normalised == str(link)                                  # the requested path is what is reported, not the real one


def test_dotdot_through_a_symlink_is_judged_by_where_the_os_would_go(tmp_path, home):
    (home / ".ssh").mkdir()
    (home / ".ssh" / "inner").mkdir()
    safe = tmp_path / "safe"
    safe.mkdir()
    os.symlink(home / ".ssh" / "inner", safe / "link")
    # lexically /safe/link/../ is /safe (fine); the OS resolves link first, so it is ~/.ssh
    d = check_folder(f"{safe}{os.sep}link{os.sep}..", home=str(home), env={})
    assert not d.ok and d.code == "protected_location"


def test_the_apps_own_data_state_grant_audit_and_key_directories_are_refused(tmp_path, home):
    dirs = {}
    for name in ("AGENTIC_OS_HOME", "AGENTIC_OS_STATE_DIR", "PERMISSIONS_DIR", "PROJECTS_DRILLDOWN_GRANTS_DIR"):
        d = tmp_path / name.lower()
        (d / "sub").mkdir(parents=True)
        dirs[name] = str(d)
    audit = tmp_path / "audit-dir"
    audit.mkdir()
    dirs["AGENTIC_OS_SOURCES_AUDIT_PATH"] = str(audit / "log.jsonl")
    dirs["PROJECTS_DRILLDOWN_AUDIT_PATH"] = str(audit / "dd.jsonl")
    keys = tmp_path / "keys"
    keys.mkdir()
    dirs["REDEVOPS_SECRETS_FILE"] = str(keys / "secrets.env")
    for name, value in dirs.items():
        d = os.path.dirname(value) if name.endswith(("_PATH", "_FILE")) else value
        r = check_folder(d, home=str(home), env=dirs)
        assert not r.ok and r.code == "protected_location", (name, r.code)
    r = check_folder(str(tmp_path / "permissions_dir" / "sub"), home=str(home), env=dirs)
    assert not r.ok and r.code == "protected_location"
    r = check_folder(str(tmp_path), home=str(home), env=dirs)         # a parent of any of them
    assert not r.ok and r.code == "contains_protected_location"


def test_extra_protected_locations_can_be_passed_by_the_caller(tmp_path, home):
    d = tmp_path / "mine"
    (d / "x").mkdir(parents=True)
    r = check_folder(str(d / "x"), home=str(home), env={}, extra_protected=[str(d)])
    assert not r.ok and r.code == "protected_location"


def test_system_directories_are_refused(home):
    for p in ("/etc", "/dev", "/proc", "/sys", "/System"):
        if os.path.isdir(p):
            r = check_folder(p, home=str(home), env={})
            assert not r.ok and r.code in ("protected_location", "contains_protected_location"), p


def test_case_differences_and_unicode_normalisation_do_not_get_around_the_list(tmp_path):
    h = tmp_path / "ré" / "home"                                       # a home whose name has an accent (composed)
    h.mkdir(parents=True)
    (h / ".ssh").mkdir()
    nfd_home = unicodedata.normalize("NFD", str(h))
    assert nfd_home != str(h)
    # the protected entry is built from the NFC home; the request spells the same folder with a decomposed accent and different case
    d = check_folder(os.path.join(nfd_home, ".SSH"), home=str(h), env={})
    assert not d.ok and d.code in ("protected_location", "not_found") and d.code != "ok"
    d2 = check_folder(unicodedata.normalize("NFD", str(h)).replace(".", "."), home=nfd_home, env={})
    assert not d2.ok and d2.code == "home_directory"
    # mixed case: the policy is case-insensitive even where the filesystem is not (it can only refuse more, never less)
    d3 = check_folder(os.path.join(str(h), ".SsH", "inner"), home=str(h), env={})
    assert not d3.ok and d3.code == "protected_location"


# ----------------------------------------------------------------------------------------------------------------------------------
# Windows forms (lexical rules, run on any host through windows=True and an injected filesystem)
# ----------------------------------------------------------------------------------------------------------------------------------
def fake_fs(dirs, links=None, short=None):
    dirs = {d.casefold() for d in dirs}
    links = {k.casefold(): v for k, v in (links or {}).items()}
    short = {k.casefold(): v for k, v in (short or {}).items()}

    def realpath(p):
        key = p.replace("/", "\\").rstrip("\\").casefold()
        return short.get(key, links.get(key, p.replace("/", "\\").rstrip("\\") or p))

    def stat_fn(p):
        key = p.replace("/", "\\").rstrip("\\").casefold()
        if key in dirs:
            return types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o755)
        raise FileNotFoundError(p)
    return realpath, stat_fn


WHOME = "C:\\Users\\alex"


def win(raw, dirs=("C:\\data\\pdfs",), **kw):
    rp, st = fake_fs(dirs, kw.pop("links", None), kw.pop("short", None))
    return check_folder(raw, home=WHOME, env={}, windows=True, realpath=rp, stat_fn=st, **kw)


@pytest.mark.parametrize("raw", ["C:\\data\\pdfs", "C:/data/pdfs", "c:\\DATA\\Pdfs\\", "C:\\data\\x\\..\\pdfs", "C:/data//pdfs/"])
def test_windows_drive_letter_backslash_forward_slash_case_and_trailing_forms_are_one_folder(raw):
    d = win(raw)
    assert d.ok, d.code
    assert lexical_key(raw, windows=True) == lexical_key("C:\\data\\pdfs", windows=True)
    assert d.normalised.lower().replace("/", "\\").rstrip("\\") in ("c:\\data\\pdfs",)


@pytest.mark.parametrize("raw", ["C:\\", "C:/", "c:\\", "C:\\\\", "D:/", "C:"])
def test_windows_drive_roots_are_refused(raw):
    d = win(raw)
    assert not d.ok and d.code in ("drive_root", "not_absolute") and (raw != "C:\\" or d.code == "drive_root")
    assert win("C:\\").code == "drive_root" and win("C:/").code == "drive_root" and win("d:\\").code == "drive_root"


def test_windows_drive_relative_and_rooted_relative_are_not_absolute():
    assert win("C:data\\pdfs").code == "not_absolute"
    assert win("\\data\\pdfs").code == "not_absolute"
    assert win("data\\pdfs").code == "not_absolute"


@pytest.mark.parametrize("raw", ["\\\\?\\C:\\data\\pdfs", "\\\\.\\PhysicalDrive0", "\\\\.\\C:", "//./pipe/x", "//?/C:/data/pdfs",
                                 "//?/UNC/server/share/x", "\\\\?\\UNC\\server\\share\\x", "\\\\.\\pipe\\x"])
def test_windows_device_paths_are_refused(raw):
    assert win(raw).code == "device_path"
    # the same text is refused on a POSIX host too: the rule is not platform-conditional
    d = check_folder(raw, home="/home/alex", env={}, windows=False)
    assert not d.ok and d.code == "device_path"


@pytest.mark.parametrize("raw", ["C:\\data\\NUL", "C:\\data\\con", "C:\\data\\COM1", "C:\\data\\lpt9.txt", "C:\\data\\AUX"])
def test_windows_reserved_device_names_are_device_paths(raw):
    assert win(raw).code == "device_path"


@pytest.mark.parametrize("raw", ["\\\\server\\C$\\Users", "\\\\server\\ADMIN$\\x", "\\\\server\\IPC$", "//server/C$/x", "\\\\10.0.0.5\\d$\\data", "\\\\server\\c$"])
def test_unc_administrative_shares_are_refused(raw):
    assert win(raw).code == "unc_admin_share", raw


def test_unc_share_roots_are_refused_and_a_folder_in_a_normal_share_is_accepted():
    assert win("\\\\server\\share").code == "share_root"
    assert win("//server/share/").code == "share_root"
    assert win("\\\\server").code == "invalid_path"
    d = win("\\\\server\\share\\docs", dirs=("\\\\server\\share\\docs",))
    assert d.ok, d.code
    d = win("//server/share/docs/", dirs=("\\\\server\\share\\docs",))
    assert d.ok, d.code


def test_backslash_unc_is_recognised_on_a_posix_host_too(home):
    d = check_folder("\\\\server\\C$\\x", home=str(home), env={}, windows=False)
    assert not d.ok and d.code == "unc_admin_share"


def test_windows_profile_credential_roots_are_refused_in_every_spelling():
    for raw in (WHOME, WHOME + "\\", "C:/Users/alex", "c:\\USERS\\ALEX"):
        assert win(raw).code == "home_directory", raw
    for raw in ("C:\\Users", "C:\\Users\\", "C:/users"):
        assert win(raw).code == "contains_protected_location", raw
    for raw in ("C:\\Users\\alex\\.ssh", "C:/Users/alex/.SSH/keys", "c:\\users\\alex\\.aws", "C:\\Users\\alex\\AppData\\Roaming",
                "C:\\Users\\alex\\AppData\\Local", "C:\\Users\\alex\\AppData\\Roaming\\Microsoft\\Credentials\\x",
                "C:\\Users\\alex\\AppData\\Local\\Google\\Chrome\\User Data"):
        d = win(raw)
        assert d.code in ("protected_location", "contains_protected_location"), (raw, d.code)


def test_windows_appdata_from_the_environment_is_protected_even_off_the_profile():
    rp, st = fake_fs(["D:\\redirected\\Roaming\\Thing"])
    env = {"APPDATA": "D:\\redirected\\Roaming", "LOCALAPPDATA": "D:\\redirected\\Local"}
    d = check_folder("D:\\redirected\\Roaming", home=WHOME, env=env, windows=True, realpath=rp, stat_fn=st)
    assert d.code == "contains_protected_location"
    d = check_folder("D:\\redirected\\Roaming\\Microsoft\\Credentials", home=WHOME, env=env, windows=True, realpath=rp, stat_fn=st)
    assert d.code == "protected_location"


def test_windows_8_3_short_names_are_judged_by_their_long_form():
    long_home = "C:\\Users\\alexander"
    short = {"C:\\USERS\\ALEXAN~1\\.SSH": long_home + "\\.ssh", "C:\\USERS\\ALEXAN~1": long_home}
    rp, st = fake_fs([long_home + "\\.ssh", long_home], short=short)
    d = check_folder("C:\\USERS\\ALEXAN~1\\.SSH", home=long_home, env={}, windows=True, realpath=rp, stat_fn=st)
    assert d.code == "protected_location"
    d = check_folder("C:\\USERS\\ALEXAN~1", home=long_home, env={}, windows=True, realpath=rp, stat_fn=st)
    assert d.code == "home_directory"
    # control: with no expansion the short spelling is NOT recognisable, which is why the real path is what the list is compared with
    rp2, st2 = fake_fs([long_home + "\\.ssh", "C:\\USERS\\ALEXAN~1\\.SSH"])
    d = check_folder("C:\\USERS\\ALEXAN~1\\.SSH", home=long_home, env={}, windows=True, realpath=rp2, stat_fn=st2)
    assert d.ok


@pytest.mark.parametrize("raw", ["C:\\data\\pdfs.", "C:\\data\\pdfs ", "C:\\data\\pdfs:stream", "C:\\data\\p<df", "C:\\data\\p|df", "C:\\data\\\"pdfs"])
def test_windows_trailing_dots_spaces_streams_and_reserved_characters_are_invalid(raw):
    assert win(raw).code == "invalid_path", raw


def test_windows_symlink_or_junction_to_a_protected_folder_is_refused_by_its_real_path():
    d = win("C:\\data\\pdfs", links={"C:\\data\\pdfs": WHOME + "\\.ssh"}, dirs=("C:\\data\\pdfs", WHOME + "\\.ssh"))
    assert d.code == "protected_location" and d.normalised.lower() == "c:\\data\\pdfs"


# ----------------------------------------------------------------------------------------------------------------------------------
# the refusals themselves: fixed wording, 4xx, no path echo beyond the normalised requested path
# ----------------------------------------------------------------------------------------------------------------------------------
def test_every_refusal_is_a_4xx_with_fixed_wording():
    assert REFUSALS
    for code, (status, message) in REFUSALS.items():
        if code == "audit_unavailable":
            assert status == 503
            continue
        assert 400 <= status < 500, code
        assert message and "{" not in message and "%" not in message and message.endswith("."), code
    # every code the policy can return is in the table
    for fn_code in ("invalid_path", "not_absolute", "device_path", "unc_admin_share", "drive_root", "share_root", "filesystem_root", "home_directory",
                    "protected_location", "contains_protected_location", "not_found", "not_a_directory"):
        assert fn_code in REFUSALS


def test_a_refusal_never_contains_the_real_path_or_the_raw_text_beyond_the_normalised_path(tmp_path, home):
    ssh = home / ".ssh"
    ssh.mkdir()
    link = tmp_path / "x" / ".." / "innocent"
    (tmp_path / "x").mkdir()
    os.symlink(ssh, tmp_path / "innocent")
    d = check_folder(str(link), home=str(home), env={})
    assert not d.ok
    body = sfp.refusal_body(d)
    assert body["code"] == d.code and body["path"] == str(tmp_path / "innocent")
    assert set(body) == {"code", "message", "path"}
    assert str(ssh) not in repr(body) and ".." not in body["path"]


def test_a_refusal_for_unusable_text_echoes_no_path_at_all():
    d = check_folder("a\x00b", home="/h", env={})
    body = sfp.refusal_body(d)
    assert body["path"] == "" and "\x00" not in repr(body)


def test_a_path_the_policy_cannot_examine_is_refused_not_crashed(home, monkeypatch):
    def boom(_p):
        raise OSError(13, "denied")
    d = check_folder("/tmp", home=str(home), env={}, realpath=boom)
    assert not d.ok and d.code in ("not_found", "invalid_path")
