"""Onboarding provisioning — create the credentials a non-technical user shouldn't have to,
store them on THIS device, and tell them plainly where they live.

Two ideas:

* :func:`provision_local_secret` writes a secret to the on-device file secret store (0600, via
  ``FileSecretStore``) and returns a :class:`ProvisionedCredential` carrying the **location** and a
  human :class:`Notice` — never the value. So the installer can set an app's key up automatically
  and hand the user a "your key is saved at …" message instead of a setup wizard.

* A :class:`CredentialProvisioner` is a per-secret hook that *obtains* a value (e.g. mints/reads an
  app's API key) and provisions it. :class:`TwentyKeyProvisioner` is the first: it takes the Twenty
  CRM key (from a supplied source — an API-mint callback, or the environment) and stores it locally,
  then notifies the user of the file path and the Twenty URL, reassuring them nothing else is needed.

The notices are the point: a person who is "not technical enough to set up each app" gets a clear,
location-bearing message, not a config file to edit.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

DEFAULT_SECRET_DIRNAME = ".redevops/secrets"


@dataclass(frozen=True)
class Notice:
    """A human-facing bootstrap message. ``location`` is a path or URL the user may need to find."""

    title: str
    detail: str
    location: str = ""

    @property
    def human_text(self) -> str:
        loc = f"\n  Location: {self.location}" if self.location else ""
        return f"{self.title}\n  {self.detail}{loc}"


@dataclass(frozen=True)
class ProvisionedCredential:
    """A credential created/stored on this device. Carries a reference + location, never the value."""

    name: str
    provider: str
    location: str            # where it is stored on this device
    notice: Notice
    ref: object = None       # a SecretRef the runtime resolves later (broker/store)


def default_secret_dir() -> str:
    """The on-device secret directory: ``$REDEVOPS_SECRET_DIR`` or ``~/.redevops/secrets``."""
    return os.environ.get("REDEVOPS_SECRET_DIR") or os.path.join(
        os.path.expanduser("~"), DEFAULT_SECRET_DIRNAME)


def provision_local_secret(name: str, value: str, *, secret_dir: Optional[str] = None,
                           namespace: str = "default", app: str = "",
                           app_url: str = "") -> ProvisionedCredential:
    """Store ``value`` on this device (0600) and return where it lives + a human notice. No value logged."""
    from runtime_contracts.secrets_local.store import FileSecretStore   # noqa: PLC0415

    root = secret_dir or default_secret_dir()
    os.makedirs(root, exist_ok=True)
    ref = FileSecretStore(root).put(namespace=namespace, path=name,
                                    value=value.encode(), classifications=("credential",))
    location = os.path.join(root, namespace, name)
    label = app or name
    detail = (f"An API key for {label} was created and saved securely on this device, "
              f"so you don't have to set it up yourself.")
    if app_url:
        detail += f" {label} is available at {app_url}."
    notice = Notice(title=f"{label}: key ready", detail=detail, location=location)
    return ProvisionedCredential(name=name, provider="file", location=location, notice=notice, ref=ref)


class CredentialProvisioner(Protocol):
    """Obtains and provisions one named secret during install."""

    name: str
    def provision(self, *, secret_dir: Optional[str] = None) -> Optional[ProvisionedCredential]: ...


@dataclass
class TwentyKeyProvisioner:
    """Provision the Twenty CRM API key onto the device and notify the user of its location.

    ``key_source`` supplies the key value — an API-mint callback in production, or (default) the
    ``TWENTY_API_KEY`` environment variable a deploy already set. Returns None (no crash) if no key
    can be obtained, so the installer records it as still-missing rather than failing.
    """

    name: str = "twenty_api_key"
    app_url: str = "http://localhost:3010"
    key_source: Optional[Callable[[], str]] = None

    def _obtain(self) -> str:
        if self.key_source is not None:
            return self.key_source() or ""
        return os.environ.get("TWENTY_API_KEY", "")

    def provision(self, *, secret_dir: Optional[str] = None) -> Optional[ProvisionedCredential]:
        value = self._obtain()
        if not value:
            return None
        return provision_local_secret(self.name, value, secret_dir=secret_dir,
                                      app="Twenty CRM", app_url=self.app_url)
