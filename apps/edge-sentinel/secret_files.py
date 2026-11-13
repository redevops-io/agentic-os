"""Secrets from files: `NAME_FILE=/run/secrets/x` fills `NAME` from that file (Docker/Kubernetes secrets).

An explicitly set `NAME` always wins. A named file that cannot be read stops the start with a clear message, so a
mis-mounted secret is a startup failure and never a silently unauthenticated app. Only the variables listed here are
read this way; the value is never logged. Standard library only, so any module may import it first."""
from __future__ import annotations

import os

SECRET_NAMES = ("SENTINEL_BASIC_AUTH_PASS", "PARTNERS_BASIC_AUTH_PASS", "CROWDSEC_BOUNCER_KEY",
                "CROWDSEC_MACHINE_PASSWORD", "AGENTIC_OS_CONTROL_TOKEN")


class SecretFileError(RuntimeError):
    pass


def load_file_secrets(env=None) -> list:
    """Fill each unset NAME from NAME_FILE. Returns the names that were filled."""
    env = os.environ if env is None else env
    filled = []
    for name in SECRET_NAMES:
        path = str(env.get(name + "_FILE", "")).strip()
        if not path or str(env.get(name, "")).strip():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                value = f.read().strip()
        except OSError as e:
            raise SecretFileError(f"{name}_FILE is set but the file cannot be read ({e.strerror or type(e).__name__})") from None
        if not value:
            raise SecretFileError(f"{name}_FILE is set but the file is empty")
        env[name] = value
        filled.append(name)
    return filled
