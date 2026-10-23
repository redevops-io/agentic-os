"""KeePassXC SecretStore: secrets live in a KDBX database that the user already owns and unlocks.

Implemented over ``keepassxc-cli`` (the maintained, scriptable surface) so no KDBX parsing or crypto lives here.
The unlock material is never stored and never asked for in a form:

* a **key file** (``key_file=``; databases created with ``--set-key-file``), optionally with a master password, or
* the **master password read from an environment variable whose NAME is configured** (``password_env=``), fed to
  the CLI on stdin; the value is not logged or kept after the call.

A secret reference maps ``namespace`` to the KeePassXC group and ``path`` to the entry title
(``SecretRef(provider="keepassxc", namespace="Redevops", path="LLM")`` = entry ``Redevops/LLM``); the secret is the
entry's Password attribute, or another attribute named by ``ref.key`` (custom attribute, e.g. ``api_key``).

The CLI runner is injectable so the store is unit-testable without KeePassXC installed; the real contract was
exercised against ``keepassxc-cli`` in a container (see tests/test_keepassxc_store.py for the opt-in lane).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable, Dict, Optional, Sequence, Tuple

from runtime_contracts.protocol.secrets import SecretDescriptor, SecretRef
from runtime_contracts.secrets_local.store import SecretAccessError

Runner = Callable[[Sequence[str], str], Tuple[int, str, str]]


def _subprocess_runner(args: Sequence[str], stdin: str) -> Tuple[int, str, str]:
    p = subprocess.run(list(args), input=stdin, capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout, p.stderr


class KeePassXCSecretStore:
    provider = "keepassxc"
    is_production_grade = True          # an encrypted KDBX held by the user, unlocked outside our process

    def __init__(self, database: str, *, key_file: Optional[str] = None, password_env: Optional[str] = None,
                 cli: Optional[str] = None, runner: Optional[Runner] = None) -> None:
        if not database:
            raise SecretAccessError("KeePassXC store needs a database path")
        if not key_file and not password_env:
            raise SecretAccessError("KeePassXC store needs a key file or the NAME of an environment variable "
                                    "holding the master password")
        self.database, self.key_file, self.password_env = database, key_file, password_env
        self._cli = cli or os.environ.get("KEEPASSXC_CLI") or shutil.which("keepassxc-cli") or "keepassxc-cli"
        self._run = runner or _subprocess_runner

    # ── plumbing ──────────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _entry(ref: SecretRef) -> str:
        name = "/".join(p for p in (ref.namespace.strip("/"), ref.path.strip("/")) if p)
        if not name or ".." in name.split("/"):
            raise SecretAccessError("invalid KeePassXC entry reference")
        return name

    def _unlock(self) -> Tuple[list, str]:
        """(cli flags, stdin prefix) that unlock the database. The password value is read from the named
        environment variable at the moment of use and goes only to the CLI's stdin."""
        flags: list = []
        stdin = ""
        if self.key_file:
            flags += ["-k", self.key_file]
        if self.password_env:
            pw = os.environ.get(self.password_env)
            if pw is None:
                raise SecretAccessError(f"environment variable {self.password_env} (KeePassXC master password) "
                                        "is not set")
            stdin = pw + "\n"
        else:
            flags += ["--no-password"]
        return flags, stdin

    def _cli_run(self, command: str, extra: Sequence[str], entry: str, stdin_tail: str = "") -> str:
        # NOTE: no -q: it also silences the CLI's error text, which is how a wrong password is told from a missing
        # entry. The CLI prompts on stderr, so stdout carries only the requested value.
        flags, stdin = self._unlock()
        args = [self._cli, command, *flags, *extra, self.database, entry]
        rc, out, err = self._run(args, stdin + stdin_tail)
        if rc != 0:
            # never echo CLI output that could contain material: classify only
            low = (err or "").lower()
            if "could not find entry" in low or "not found" in low:
                raise SecretAccessError("KeePassXC entry not found")
            if "invalid credentials" in low or "failed to open" in low or "wrong" in low:
                raise SecretAccessError("KeePassXC database could not be unlocked")
            raise SecretAccessError(f"keepassxc-cli {command} failed (exit {rc})")
        return out

    # ── SecretStore protocol ──────────────────────────────────────────────────────────────────
    def describe(self, ref: SecretRef) -> SecretDescriptor:
        self._cli_run("show", ["-a", "Title"], self._entry(ref))
        return SecretDescriptor(ref=ref, classifications=("credential",), rotatable=True)

    def _read(self, ref: SecretRef) -> bytes:
        attr = ref.key or "Password"
        out = self._cli_run("show", ["-s", "-a", attr], self._entry(ref))
        value = out.rstrip("\r\n")
        if not value:
            raise SecretAccessError("KeePassXC entry has no value for the requested attribute")
        return value.encode("utf-8")

    def put(self, *, namespace: str, path: str, value: bytes,
            classifications: Tuple[str, ...] = (), metadata: Optional[Dict[str, str]] = None) -> SecretRef:
        ref = SecretRef(provider=self.provider, namespace=namespace, path=path, version="1")
        entry = self._entry(ref)
        secret = value.decode("utf-8")
        if "\n" in secret or "\r" in secret:
            raise SecretAccessError("a KeePassXC secret passed through the CLI cannot contain a newline")
        exists = True
        try:
            self._cli_run("show", ["-a", "Title"], entry)
        except SecretAccessError as e:
            if "not found" not in str(e):
                raise
            exists = False
        if exists:
            self._cli_run("edit", ["-p"], entry, stdin_tail=secret + "\n")
        else:
            group = "/".join(p for p in (namespace.strip("/"),) if p)
            if group:
                try:
                    self._cli_run("mkdir", [], group)          # idempotent: an existing group is fine
                except SecretAccessError:
                    pass
            self._cli_run("add", ["-p"], entry, stdin_tail=secret + "\n")
        return ref

    def rotate(self, ref: SecretRef) -> SecretRef:
        raise NotImplementedError("rotate by put(): KeePassXC holds the value you give it")

    def revoke(self, ref: SecretRef) -> None:
        self._cli_run("rm", [], self._entry(ref))
