"""Which folders a desktop user may register as a source (the policy behind ``agentic_os/desktop_sources.py``).

A ``files`` source makes THIS process walk a folder on the user's disk. On the desktop nobody holds an operator allow-list, so the rule is
the opposite of a list of allowed parents: the only folder that can be registered is the exact folder the user picked, and a set of
places is never registrable whoever picks them. :func:`check_folder` returns one decision per request; it reads nothing but the
metadata of the path (``realpath`` and ``stat``) and never the folder's contents.

Rules, in the order they are applied (the first that fails decides; the code is the audit reason and the refusal's ``code``):

==========================  ==================================================================================================================
code                        rule
==========================  ==================================================================================================================
``invalid_path``            not a string; empty; longer than 4096 characters; a control character (NUL, newline, tab, DEL ...); a glob or
                            wildcard character (``*`` ``?``); ``~user`` (only the current user's ``~`` is expanded); on Windows forms also
                            ``< > " |``, a ``:`` after the drive, a component ending in a dot or a space (Windows strips them, so the name
                            is ambiguous), and a UNC path with no share
``device_path``             ``\\\\?\\`` and ``\\\\.\\`` in either slash direction (so ``//./`` and ``//?/`` too), Windows reserved device
                            names (``NUL``, ``CON``, ``COM1`` ... ``LPT9``) as a component of a Windows-form path. Textual: refused on every host
``unc_admin_share``         a UNC path whose share ends in ``$`` (``C$``, ``ADMIN$``, ``IPC$``)
``not_absolute``            a relative path, a drive-relative path (``C:dir``), a rooted-but-driveless Windows path (``\\dir``)
``drive_root``              ``C:``, ``C:\\``, ``C:/``
``share_root``              ``\\\\server\\share`` with nothing below it
``filesystem_root``         ``/`` (any number of slashes) or a Windows root after normalisation
``home_directory``          the user's home directory itself (a folder INSIDE it is fine)
``protected_location``      a protected location or anything inside it: ``~/.ssh``, ``~/.aws``, ``~/.gnupg``, ``~/.kube``, ``~/.config/gcloud``,
                            ``~/.docker``, ``~/.azure``, ``~/.config/gh``, macOS keychains/cookies/mail/browser profiles, Linux and Windows
                            browser profiles, ``Microsoft\\Credentials|Protect|Vault``, the system trees (``/etc``, ``/dev``, ``/proc``, ``/sys``,
                            ``/System``, ``%SystemRoot%``) and the application's own data, state, grant, audit, key and secrets directories
``contains_protected_location``  a folder that HOLDS a protected location (a parent: ``/``, the directory above home, ``~/.config``,
                            ``~/Library``, ``%APPDATA%``, ``%LOCALAPPDATA%``, the directory that holds the audit log ...). A parent is never
                            registered, because everything under it would be
``not_found``               the path does not exist or cannot be examined
``not_a_directory``         a file, a device node, a socket, a pipe (after symlinks are resolved)
==========================  ==================================================================================================================

The protected list is compared with BOTH the lexical path (``..`` and trailing separators collapsed) and the real path (every symlink,
junction and 8.3 short name resolved by the operating system, ``..`` evaluated AFTER the links in front of it, as the OS does), and the
comparison is case-insensitive and Unicode-normalised (NFC) on every host: it can only refuse more than the file system would, never less.
The real path is the one that is scanned (:func:`check_folder` returns it) and both are recorded in the audit.

The Windows rules are text rules and are tested on every host through the ``windows`` switch with an injected ``realpath``/``stat``;
the filesystem rules run for real on the host. A Windows host expands 8.3 names and junctions in ``os.path.realpath``; that part could
only be exercised with an injected resolver here.
"""
from __future__ import annotations

import ntpath
import os
import posixpath
import re
import stat as _stat
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

MAX_PATH_CHARS = 4096

#: code -> (HTTP status, fixed wording). The wording never contains a path; the response carries the normalised requested path next to it.
REFUSALS: Dict[str, Tuple[int, str]] = {
    "invalid_path": (422, "That is not a usable folder path."),
    "not_absolute": (422, "Give the full path of the folder."),
    "device_path": (403, "Device paths cannot be registered as a source."),
    "unc_admin_share": (403, "Administrative network shares cannot be registered as a source."),
    "drive_root": (403, "A whole drive cannot be registered as a source; pick a folder inside it."),
    "share_root": (403, "A whole network share cannot be registered as a source; pick a folder inside it."),
    "filesystem_root": (403, "The root of the file system cannot be registered as a source; pick a folder inside it."),
    "home_directory": (403, "Your whole home folder cannot be registered as a source; pick a folder inside it."),
    "protected_location": (403, "That folder holds credentials, browser data, system files or this app's own data and cannot be registered as a source."),
    "contains_protected_location": (403, "That folder contains credentials, browser data, system files or this app's own data and cannot be registered as a source; pick a more specific folder."),
    "not_found": (422, "That folder does not exist or cannot be read."),
    "not_a_directory": (422, "That path is not a folder."),
    "outside_allowed_roots": (403, "That folder is outside the folders this deployment allows."),
    "not_proposed": (403, "Only a folder you picked in a proposal can be connected. Ask again, then confirm."),
    "audit_unavailable": (503, "The registration could not be recorded, so nothing was registered."),
}

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE = re.compile(r"^[A-Za-z]:")
_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))})
_WIN_BAD = set('<>"|')

#: Home-relative locations that are protected with everything inside them.
HOME_TREES = (
    ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".oci", ".terraform.d", ".password-store", ".agentic-os",
    ".config/gcloud", ".config/gh", ".config/doctl", ".config/redevops", ".mozilla", ".thunderbird",
    ".config/google-chrome", ".config/chromium", ".config/BraveSoftware", ".config/microsoft-edge", ".config/vivaldi", ".config/opera",
    "snap/firefox", "snap/chromium", ".var/app/org.mozilla.firefox", ".var/app/com.google.Chrome",
    "Library/Keychains", "Library/Cookies", "Library/Safari", "Library/Mail", "Library/Messages",
    "Library/Application Support/Google/Chrome", "Library/Application Support/Firefox", "Library/Application Support/BraveSoftware",
    "Library/Application Support/Microsoft Edge", "Library/Application Support/Arc", "Library/Application Support/Vivaldi",
    "Library/Application Support/com.operasoftware.Opera", "Library/Application Support/Chromium", "Library/Application Support/redevops",
    "Library/Application Support/io.redevops.agentic-apps", "Library/Application Support/io.redevops.agentic-apps.r0prime",
    "AppData/Roaming/Mozilla", "AppData/Roaming/Microsoft/Credentials", "AppData/Roaming/Microsoft/Protect", "AppData/Roaming/Microsoft/Vault",
    "AppData/Roaming/redevops", "AppData/Local/Google/Chrome", "AppData/Local/Microsoft/Edge", "AppData/Local/BraveSoftware",
    "AppData/Local/Mozilla", "AppData/Local/Microsoft/Credentials", "AppData/Local/Microsoft/Vault",
    "AppData/Local/io.redevops.agentic-apps", "AppData/Local/io.redevops.agentic-apps.r0prime",
)
#: Home-relative locations that are protected AS THEMSELVES (and as a parent of anything protected) but not for what a user keeps inside them.
HOME_ROOTS = (".config", ".local", ".local/share", ".local/state", "Library", "Library/Application Support", "AppData", "AppData/Roaming",
              "AppData/Local", "AppData/LocalLow")
#: The same two lists under the Windows environment's own roots (which a profile redirection may move off the home directory).
APPDATA_TREES = ("Mozilla", "Microsoft/Credentials", "Microsoft/Protect", "Microsoft/Vault", "redevops", "io.redevops.agentic-apps",
                 "io.redevops.agentic-apps.r0prime")
LOCALAPPDATA_TREES = ("Google/Chrome", "Microsoft/Edge", "BraveSoftware", "Mozilla", "Microsoft/Credentials", "Microsoft/Vault",
                      "io.redevops.agentic-apps", "io.redevops.agentic-apps.r0prime")
SYSTEM_TREES = ("/etc", "/private/etc", "/dev", "/proc", "/sys", "/System", "/var/root", "/private/var/root", "/boot")

#: Environment variables that name a DIRECTORY the application owns, and ones that name a FILE it owns (the file's directory is protected).
APP_DIR_ENVS = ("AGENTIC_OS_HOME", "AGENTIC_OS_STATE_DIR", "PERMISSIONS_DIR", "PROJECTS_DRILLDOWN_GRANTS_DIR", "PERMISSIONS_KEY_DIR")
APP_FILE_ENVS = ("AGENTIC_OS_SOURCES_AUDIT_PATH", "PROJECTS_DRILLDOWN_AUDIT_PATH", "PROJECTS_DRILLDOWN_EVENT_PATH", "PROJECTS_DRILLDOWN_AUTHORITY_DB",
                 "REDEVOPS_SECRETS_FILE", "PERMISSIONS_COUNTER_FILE", "PERMISSIONS_KEY_FILE", "AGENTIC_OS_REPLAY_DB", "AGENTIC_OS_ANCHOR_FILE")


@dataclass(frozen=True)
class FolderDecision:
    ok: bool
    code: str = ""             # "" when ok
    normalised: str = ""       # the requested path, absolute, ``~`` expanded, NFC, ``.``/``..``/trailing separators collapsed ("" when it could not be computed)
    real: str = ""             # the same folder with every link resolved ("" when not reached)


@dataclass(frozen=True)
class Protected:
    path: str
    subtree: bool = True       # True: the location and everything inside it; False: the location itself (and its parents) only
    home: bool = False


def refusal_body(decision: FolderDecision) -> Dict[str, str]:
    """The body of a refusal: a stable code, fixed wording and the normalised requested path. Nothing else (the real path stays in the audit)."""
    _status, message = REFUSALS[decision.code]
    return {"code": decision.code, "message": message, "path": decision.normalised}


def status_of(code: str) -> int:
    return REFUSALS[code][0]


def _pm(windows: bool):
    return ntpath if windows else posixpath


def _platform_windows() -> bool:
    return os.name == "nt"


def _split(path: str, windows: bool) -> Tuple[str, Tuple[str, ...]]:
    p = unicodedata.normalize("NFC", path)
    drive, rest = _pm(windows).splitdrive(p)
    comps = tuple(c.casefold() for c in re.split(r"[\\/]+", rest) if c and c != ".")
    return drive.replace("/", "\\").casefold(), comps


def lexical_key(raw: str, *, windows: Optional[bool] = None, home: Optional[str] = None) -> str:
    """A comparison key for 'the same folder, spelled differently': NFC, ``~`` expanded when ``home`` is given, separators and ``.``/``..``
    collapsed, trailing separators dropped, case-folded. Used to tie a confirmation to the proposal that offered the folder."""
    win = _platform_windows() if windows is None else bool(windows)
    s = unicodedata.normalize("NFC", str(raw))
    if home and (s == "~" or s[:2] in ("~/", "~\\")):
        s = home + s[1:]
    if not win:
        s = re.sub(r"^/{2,}", "/", s)
    return _pm(win).normpath(s).casefold()


def protected_locations(home: str, env: Mapping[str, str], windows: bool, extra: Iterable[str] = (),
                        realpath: Optional[Callable[[str], str]] = None) -> List[Protected]:
    """The protected list for one decision. Every entry also appears in its resolved form (a home under a symlinked prefix, such as ``/var`` on
    macOS, would otherwise never match a resolved candidate)."""
    pm = _pm(windows)
    rp = realpath or os.path.realpath
    out: List[Protected] = []

    def add(path: str, subtree: bool = True, is_home: bool = False) -> None:
        if not path:
            return
        out.append(Protected(path, subtree, is_home))
        try:
            real = rp(path)
        except (OSError, ValueError):
            return
        if real and real != path:
            out.append(Protected(real, subtree, is_home))

    add(home, False, True)
    for rel in HOME_TREES:
        add(pm.join(home, *rel.split("/")))
    for rel in HOME_ROOTS:
        add(pm.join(home, *rel.split("/")), False)
    appdata = env.get("APPDATA") or pm.join(home, "AppData", "Roaming")
    local = env.get("LOCALAPPDATA") or pm.join(home, "AppData", "Local")
    add(appdata, False)
    add(local, False)
    for rel in APPDATA_TREES:
        add(pm.join(appdata, *rel.split("/")))
    for rel in LOCALAPPDATA_TREES:
        add(pm.join(local, *rel.split("/")))
    if env.get("ProgramData"):
        add(env["ProgramData"], False)
    if env.get("SystemRoot"):
        add(env["SystemRoot"])
    for p in SYSTEM_TREES:
        add(p)
    for key in APP_DIR_ENVS:
        if env.get(key):
            add(env[key])
    for key in APP_FILE_ENVS:
        if env.get(key):
            add(pm.dirname(env[key]) or ".")
    for p in extra:
        add(p)
    return out


def _bad_component(c: str) -> bool:
    return c not in (".", "..") and (c.endswith(".") or c.endswith(" ") or any(ch in _WIN_BAD or ch == ":" for ch in c))


def _classify(norm: str, protected: List[Protected], windows: bool) -> str:
    cand_prefix, cand = _split(norm, windows)
    for prot in protected:
        pp, pc = _split(prot.path, windows)
        if pp != cand_prefix or not pc:
            continue
        n = len(pc)
        if cand[:n] == pc:                              # the candidate is the location or inside it
            if len(cand) == n:
                return "home_directory" if prot.home else ("protected_location" if prot.subtree else "contains_protected_location")
            if prot.subtree:
                return "protected_location"
        elif len(cand) < n and pc[:len(cand)] == cand:  # the candidate is a parent of the location
            return "contains_protected_location"
    return ""


def check_folder(raw: Any, *, home: Optional[str] = None, env: Optional[Mapping[str, str]] = None, windows: Optional[bool] = None,
                 realpath: Optional[Callable[[str], str]] = None, stat_fn: Optional[Callable[[str], Any]] = None,
                 extra_protected: Iterable[str] = ()) -> FolderDecision:
    """Decide whether the folder named by ``raw`` may be registered as a source. Never raises for a bad path; see the module docstring for the rules."""
    win = _platform_windows() if windows is None else bool(windows)
    env = os.environ if env is None else env
    home = unicodedata.normalize("NFC", home if home is not None else os.path.expanduser("~"))
    rp = realpath or os.path.realpath
    st = stat_fn or os.stat
    pm = _pm(win)

    def no(code: str, norm: str = "", real: str = "") -> FolderDecision:
        return FolderDecision(False, code, norm, real)

    # 1. the text itself
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_PATH_CHARS or _CONTROL.search(raw):
        return no("invalid_path")
    s = unicodedata.normalize("NFC", raw)
    if s == "~" or s[:2] in ("~/", "~\\"):
        s = home + s[1:]
    elif s.startswith("~"):
        return no("invalid_path")
    t = s.replace("\\", "/")
    if t.startswith(("//?/", "//./")) or t in ("//?", "//."):
        return no("device_path")
    winform = win or bool(_DRIVE.match(s)) or s.startswith("\\\\")

    # 2. UNC (a backslash UNC is one on every host; a forward-slash UNC only on a Windows host: POSIX collapses //)
    unc = s.startswith("\\\\") or (win and t.startswith("//"))
    unc_parts: List[str] = []
    if unc:
        parts = [p for p in t.split("/") if p]
        if len(parts) < 2:
            return no("invalid_path")
        if parts[1].endswith("$"):
            return no("unc_admin_share")
        unc_parts = parts
        comps = parts[2:]
    elif _DRIVE.match(s):
        rest = s[2:]
        if rest.strip("/\\") == "":
            return no("drive_root")
        if rest[0] not in "/\\":
            return no("not_absolute")
        comps = [c for c in re.split(r"[\\/]+", rest) if c]
    else:
        comps = [c for c in re.split(r"[\\/]+" if winform else r"/+", s) if c]
        if win or not s.startswith("/"):
            return no("not_absolute")

    # 3. components: reserved device names, wildcards, characters Windows would reinterpret
    if winform:
        for c in comps:
            if c.split(".")[0].rstrip(" ").upper() in _RESERVED:
                return no("device_path")
        if any(_bad_component(c) for c in comps):
            return no("invalid_path")
    if any(("*" in c or "?" in c) for c in comps):
        return no("invalid_path")

    # 4. lexical normalisation, then the roots
    if unc and not win:
        return no("not_absolute")           # a backslash UNC cannot name anything on a POSIX host
    if unc:
        norm = pm.normpath("\\\\" + "\\".join(unc_parts))
    else:
        norm = pm.normpath(re.sub(r"^/{2,}", "/", s) if not win else s)
    drive, tail = pm.splitdrive(norm)
    if tail.strip("/\\") == "":
        return no("drive_root" if _DRIVE.match(drive or "") else ("share_root" if drive else "filesystem_root"), norm)

    prot = protected_locations(home, env, win, extra_protected, rp)
    code = _classify(norm, prot, win)
    if code:
        return no(code, norm)

    # 5. what the operating system resolves it to
    try:
        real = rp(s)
    except (OSError, ValueError, RuntimeError):
        return no("not_found", norm)
    real = unicodedata.normalize("NFC", real)
    rcode = _classify(real, prot, win)
    if rcode:
        return no(rcode, norm, real)
    drive, tail = pm.splitdrive(real)
    if tail.strip("/\\") == "":
        return no("drive_root" if _DRIVE.match(drive or "") else ("share_root" if drive else "filesystem_root"), norm, real)
    try:
        mode = st(real).st_mode
    except (OSError, ValueError):
        return no("not_found", norm, real)
    if not _stat.S_ISDIR(mode):
        return no("not_a_directory", norm, real)
    return FolderDecision(True, "", norm, real)
