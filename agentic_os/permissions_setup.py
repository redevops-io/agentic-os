"""Quickstart for an individual user: a local, single-user permissions profile.

``agentic-os permissions init`` provisions, in one directory (default ``~/.config/redevops/permissions``):

* the persistent key, as configuration: ``.permissions.key`` (0600), created once by this command and kept on every
  later run (``GrantStore`` then finds it as an existing key file, so no environment is needed);
* explicit, visible, revocable grants for the BUNDLED tools only (``agentic_os.tool_actions``): one grant per tool,
  for the local user, on the resource ``<app>.<tool>``, at the action the tool is classified for. Read-class tools get
  ``read`` grants; write-class tools (the ones that change state or start scanners) get ``write`` grants only with
  ``--include-write``. Nothing else is granted: a third-party tool is not mapped and is therefore denied.

``load_gate`` returns the authorizer for that profile. Revoke a grant by id with ``agentic-os permissions revoke``
(or ``DELETE /api/permissions/grants/{id}``). For anything beyond one machine, provide ``PERMISSIONS_KEY`` from a
secret store and set ``PERMISSIONS_REQUIRE_KEY=1``.
"""
from __future__ import annotations

import base64
import os
import json
import secrets
from pathlib import Path

from .permissions import GrantStorageError, GrantStore, _ensure_private_dir, provision_key_file
from .permissions_profiles import GateProfile, build_gate
from .tool_actions import TOOL_ACTIONS

CREATED_BY = "quickstart"
DEFAULT_USER = "local"
# the sentence a deployer must not miss (printed by `permissions init`, repeated in the READMEs and the quickstart docs; a test keeps them equal)
INSTANCE_NOT_CALLERS = "basic permissions authorise the app INSTANCE, not callers: an instance with write grants lets every caller who can reach it write"


PROFILE_FILE = "profile.json"


def write_profile(directory: str, user: str) -> None:
    """Record WHO the local single-user profile is (non-secret): the principal of an app running with basic permissions comes from this file,
    never from a request header. Atomic, 0600."""
    from . import identifiers
    from .permissions import _atomic_write, _ensure_private_dir
    if not identifiers.valid_identifier(user):
        raise GrantStorageError("the local user must be a valid identifier (see docs/PERMISSIONS_MIGRATION.md, the identifier contract)")
    _ensure_private_dir(Path(directory))
    _atomic_write(Path(directory) / PROFILE_FILE, json.dumps({"version": 1, "user": user}).encode("utf-8"))


def local_user(directory: str) -> str:
    """The local profile's user: ``profile.json`` in the permissions directory, else ``DEFAULT_USER`` (a store provisioned before profiles
    existed). A profile that cannot be read or whose user is not a valid identifier is an error, never a silent fallback to another user."""
    from . import identifiers
    import logging
    import stat as _stat
    path = Path(directory) / PROFILE_FILE
    if not os.path.lexists(path):
        logging.getLogger(__name__).warning("no %s in %s: using the default local user %r (a store provisioned before profiles existed); run `agentic-os permissions init` "
                                            "to write it", PROFILE_FILE, directory, DEFAULT_USER)
        return DEFAULT_USER
    # read it like a secret (FR-08): never through a symlink, a regular file, owned by the running user, not accessible to anybody else
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        raise GrantStorageError(f"the local profile {path} is not readable as a regular file (a symlink is refused) ({type(e).__name__})") from e
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            raise GrantStorageError(f"the local profile {path} is not a regular file")
        if st.st_uid != os.geteuid():
            raise GrantStorageError(f"the local profile {path} is not owned by the user running this process")
        if _stat.S_IMODE(st.st_mode) != 0o600:
            raise GrantStorageError(f"the local profile {path} must be mode 0600 (it is {_stat.S_IMODE(st.st_mode):04o}): chmod 600 it, or re-run `agentic-os permissions init`")
        with os.fdopen(fd, "rb") as f:
            fd = -1
            raw = f.read(65536)
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        user = json.loads(raw.decode("utf-8"))["user"]
    except Exception as e:  # noqa: BLE001
        raise GrantStorageError(f"the local profile {path} cannot be read ({type(e).__name__})") from e
    if not identifiers.valid_identifier(user):
        raise GrantStorageError(f"the local profile {path} names a user that is not a valid identifier")
    return user


def default_dir() -> str:
    return os.getenv("PERMISSIONS_DIR") or str(Path.home() / ".config" / "redevops" / "permissions")


def bundled_tools() -> dict[str, tuple[str, str]]:
    """{tool: (app, action)} for every bundled tool."""
    return {t: (app, a) for app, tools in TOOL_ACTIONS.items() for t, a in tools.items()}


def write_key_env_file(path: str, *, from_key_file: str | None = None) -> str:
    """Write ``PERMISSIONS_KEY=<base64>`` to a NEW env file (0600, created exclusively, never through a symlink, never
    overwriting) for Docker Compose (``--env-file``) or a Kubernetes Secret (``--from-env-file``). The key is generated
    here (32 random bytes) or taken from an existing key file (``from_key_file``: keeps the same key when a running
    deployment moves from a key file to an environment key). **The key is never printed or logged.**"""
    if from_key_file:
        key = Path(from_key_file).read_text().strip()
        try:
            if len(base64.b64decode(key, validate=True)) not in (16, 24, 32):
                raise ValueError("wrong length")
        except Exception as e:  # noqa: BLE001
            raise GrantStorageError(f"the key file {from_key_file} is unusable ({type(e).__name__}); nothing written") from e
    else:
        key = base64.b64encode(secrets.token_bytes(32)).decode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    parent = os.path.dirname(os.path.abspath(path))
    _ensure_private_dir(parent)                              # a missing parent (the default is under ~/.config) is created private
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise GrantStorageError(f"{path} already exists; not overwriting a key (delete it deliberately if you mean to "
                                f"replace it — a different key cannot open grants already encrypted with the old one)") from None
    with os.fdopen(fd, "w") as f:
        f.write(f"PERMISSIONS_KEY={key}\n")
        f.flush()
        os.fsync(f.fileno())
    return path


def init_local_profile(directory: str | None = None, *, user: str = DEFAULT_USER, include_write: bool = False,
                       key_only: bool = False) -> dict:
    """Provision the key and the grants. Idempotent: grants are keyed by (user, tool), the key is never replaced.
    ``key_only`` provisions the key file and nothing else (a deployment that manages grants through the API)."""
    d = directory or default_dir()
    if os.environ.get("PERMISSIONS_KEY", "").strip():
        # the key comes from the environment / a secret store: NO key file is created on the data volume (a second key beside the ciphertext would weaken encryption at rest)
        key_path, key_created, key_source = None, False, "environment"
    else:
        if os.environ.get("PERMISSIONS_REQUIRE_KEY", "").strip().lower() in ("1", "true", "yes", "on"):
            raise GrantStorageError("PERMISSIONS_REQUIRE_KEY is set, so only the environment key is accepted and init will not create a key file: provision PERMISSIONS_KEY first "
                                    "(`agentic-os permissions key --env-file permissions.env`, then load it into the environment)")
        key_path, key_created = provision_key_file(d, os.environ.get("PERMISSIONS_KEY_FILE") or None)
        key_source = "key file"
    if key_only:
        return {"directory": d, "key_file": str(key_path) if key_path else None, "key_created": key_created, "key_source": key_source, "user": None,
                "granted": [], "withheld": []}
    store = GrantStore(d)
    write_profile(d, user)
    have = {g.id for g in store.list()}
    granted, withheld = [], []
    safe = lambda v: "".join(c if c.isalnum() or c in "._:-" else "_" for c in v)   # noqa: E731
    for tool, (app, action) in sorted(bundled_tools().items()):
        if action == "read" or include_write:
            gid = f"qs-{safe(user)}-{safe(app)}.{safe(tool)}"[:128]     # a stable id: re-running init adds nothing twice
            if gid not in have:
                store.add("user", user, "tool", f"{app}.{tool}", actions=[action], created_by=CREATED_BY, stable_id=gid)
            granted.append({"id": gid, "tool": tool, "app": app, "action": action})
        else:
            withheld.append({"tool": tool, "app": app, "action": action})
    return {"directory": d, "key_file": str(key_path) if key_path else None, "key_created": key_created, "key_source": key_source, "user": user,
            "granted": granted, "withheld": withheld}


def load_gate(directory: str | None = None, *, user: str = DEFAULT_USER, human_approver=None) -> tuple[GateProfile, object]:
    """The production-profile gate for the quickstart store, mapped to the bundled tools only, and the principal that
    represents the local user (``app=""``, ``user=<user>``, no roles)."""
    from .permissions_profiles import DemoPrincipal
    store = GrantStore(directory or default_dir())
    tools = bundled_tools()
    gate = build_gate("production", store,
                      tool_resources={t: ("tool", f"{app}.{t}") for t, (app, _) in tools.items()},
                      tool_actions={t: a for t, (_, a) in tools.items()},
                      human_approver=human_approver)
    return gate, DemoPrincipal(app="", user=user, roles=())
