"""Stock ONLYOFFICE Document Server adapter (externally managed; never bundled).

Routes used, all of them present in the Document Server source and exercised by ``tests/integration`` against a real
server (see ``docs/document-editor-connector/EDIT_API.md`` for file:line evidence):

    GET  /healthcheck                       liveness
    POST /command        {c: version}       CommandService (JWT)
    POST /converter      {filetype, outputtype, key, url, async}        Conversion API (JWT, async + polling)
    POST /docbuilder     {url: <script>, argument: {...}, async}        Document Builder service (JWT)

Every request is signed (HS256 JWT in the ``Authorization: Bearer`` header; the signed payload is authoritative),
goes through ``safe_fetch`` (allowlisted editor host only), and every result URL is fetched ONLY after its origin is
proven identical to the configured editor origin. Output bytes are hashed and validated before being returned.

Credentials come ONLY from the environment (Vault-provided at runtime): ``DOCEDITOR_ONLYOFFICE_URL`` and
``DOCEDITOR_ONLYOFFICE_JWT_SECRET``. Nothing is read from files or defaults; ``repr`` never shows the secret.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlsplit

from . import jwtlib, ooxml
from .backend import BackendDecl, BackendFile, DocRef, DocumentBackend
from .content import ContentRegistry
from .errors import (BackendError, BackendUnavailable, ConfigError, InvalidInput, OutcomeUnknown,
                     UnsupportedCapability)
from .safe_fetch import (FetchPolicy, FetchUnavailable, HostRule, ZONE_LOCAL, ZONE_PRIVATE, ZONE_PUBLIC, default_resolver,
                         normalize_host, safe_fetch)

ENV_URL = "DOCEDITOR_ONLYOFFICE_URL"
ENV_SECRET = "DOCEDITOR_ONLYOFFICE_JWT_SECRET"

INPUT_FORMATS = frozenset({"docx", "doc", "odt", "rtf", "txt", "xlsx", "xls", "ods", "csv", "pptx", "ppt", "odp"})
OUTPUT_FORMATS = frozenset({"pdf", "docx", "xlsx", "pptx", "odt", "ods", "odp", "rtf", "txt", "csv"})
DOC_TYPE = {"docx": "word", "doc": "word", "odt": "word", "rtf": "word", "txt": "word",
            "xlsx": "cell", "xls": "cell", "ods": "cell", "csv": "cell",
            "pptx": "slide", "ppt": "slide", "odp": "slide"}
CONVERT_ERRORS = {-1: "unknown error", -2: "conversion timeout", -3: "conversion error", -4: "could not download the source",
                  -5: "incorrect password", -6: "result database error", -7: "input error", -8: "invalid token",
                  -9: "cannot determine output format", -10: "size limit exceeded"}
BUILDER_ERRORS = {-1: "unknown error", -2: "generation timeout", -3: "generation error",
                  -4: "could not download a file", -6: "result database error", -8: "invalid token"}


@dataclass(frozen=True)
class OnlyOfficeConfig:
    url: str
    jwt_secret: str = field(repr=False)
    zone: str = "PRIVATE_NETWORK"            # LOCAL | PRIVATE_NETWORK | EXTERNAL
    request_timeout_s: float = 30.0
    poll_interval_s: float = 1.0
    operation_timeout_s: float = 120.0
    max_result_bytes: int = 64 * 1024 * 1024
    jwt_ttl_s: int = 60

    def __repr__(self) -> str:       # never leak the secret, even via a stray log line
        return f"OnlyOfficeConfig(url={self.url!r}, zone={self.zone!r}, jwt_secret=<redacted>)"

    @staticmethod
    def from_env(zone: str = "PRIVATE_NETWORK", env: Optional[dict] = None, **kw) -> "OnlyOfficeConfig":
        e = os.environ if env is None else env
        url, secret = e.get(ENV_URL, "").strip(), e.get(ENV_SECRET, "")
        if not url or not secret:
            raise ConfigError(f"{ENV_URL} and {ENV_SECRET} must be provided in the environment (Vault)")
        return OnlyOfficeConfig(url=url.rstrip("/"), jwt_secret=secret, zone=zone, **kw)

    def origin(self) -> "tuple[str, str, int]":
        sp = urlsplit(self.url)
        if sp.username or sp.password or sp.query or sp.fragment or (sp.path not in ("", "/")):
            raise ConfigError("editor URL must be a bare origin: no credentials, path, query or fragment")
        scheme = sp.scheme.lower()
        if scheme not in ("http", "https"):
            raise ConfigError("editor URL must be http(s)")
        host = normalize_host(sp.hostname if (sp.hostname and ":" not in sp.hostname) else f"[{sp.hostname}]")
        return scheme, host, sp.port or (443 if scheme == "https" else 80)

    def fetch_policy(self) -> FetchPolicy:
        _, host, port = self.origin()
        zone = {"EXTERNAL": ZONE_PUBLIC, "LOCAL": ZONE_LOCAL}.get(self.zone, ZONE_PRIVATE)
        return FetchPolicy.of([HostRule(host, zone, frozenset({port}))], max_bytes=self.max_result_bytes,
                              timeout_s=self.request_timeout_s)


class OnlyOfficeBackend(DocumentBackend):
    provider = "onlyoffice"
    #: Every entry is backed by a named test in tests/integration (real stock Document Server, digest-pinned).
    #: tests/test_proven_capabilities.py asserts each name below exists in the integration suite.
    PROVEN = frozenset({"open", "convert", "inspect", "export"})
    PROOF_TESTS = {
        "open": "test_open_session_config_is_accepted_by_server",
        "convert": "test_convert_docx_to_pdf_verified",
        "export": "test_convert_docx_to_pdf_verified",
        "inspect": "test_inspect_reports_version_and_text",
    }

    def __init__(self, decl: BackendDecl, cfg: OnlyOfficeConfig, content: ContentRegistry, *,
                 resolver=default_resolver, ssl_context=None, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic):
        super().__init__(decl)
        self.cfg, self.content = cfg, content
        self._policy = cfg.fetch_policy()
        self._origin = cfg.origin()
        self._resolver, self._ssl = resolver, ssl_context
        self._sleep, self._mono = sleep, monotonic

    # -- transport ----------------------------------------------------------------------------------------------
    def _sign(self, payload: dict) -> str:
        return jwtlib.sign(payload, self.cfg.jwt_secret, ttl_s=self.cfg.jwt_ttl_s)

    def _post(self, path: str, payload: dict, *, write: bool = False) -> dict:
        body = json.dumps(payload).encode("utf-8")        # signed payload is authoritative; the body mirrors it
        try:
            res = safe_fetch(self.cfg.url + path, policy=self._policy, method="POST", body=body,
                             headers={"Content-Type": "application/json", "Accept": "application/json",
                                      "Authorization": "Bearer " + self._sign(payload)},
                             resolver=self._resolver, ssl_context=self._ssl)
        except FetchUnavailable as e:
            if write and e.request_sent:
                raise OutcomeUnknown(f"editor did not answer after the request was sent: {e.detail}") from None
            raise BackendUnavailable(f"document editor unreachable: {e.detail}") from None
        if res.status in (401, 403):
            raise BackendError(f"editor rejected the request (HTTP {res.status}); check the JWT secret")
        if res.status >= 500:
            if write:
                raise OutcomeUnknown(f"editor returned HTTP {res.status} to a write")
            raise BackendUnavailable(f"editor returned HTTP {res.status}")
        if res.status != 200:
            raise BackendError(f"unexpected HTTP {res.status} from the editor")
        try:
            data = json.loads(res.body.decode("utf-8"))
        except ValueError:
            raise BackendError("editor returned a non-JSON response") from None
        if not isinstance(data, dict):
            raise BackendError("editor returned an unexpected response shape")
        return data

    def _same_origin(self, url: str) -> None:
        sp = urlsplit(url)
        try:
            host = normalize_host(sp.hostname if (sp.hostname and ":" not in sp.hostname) else f"[{sp.hostname}]")
        except Exception:  # noqa: BLE001
            raise BackendError("editor returned an unparseable result URL") from None
        scheme = sp.scheme.lower()
        port = sp.port or (443 if scheme == "https" else 80)
        if (scheme, host, port) != self._origin:
            raise BackendError(f"editor result URL points at {scheme}://{host}:{port}, not the configured editor "
                               "origin; refusing to fetch it")

    def _download(self, url: str) -> bytes:
        self._same_origin(url)
        try:
            res = safe_fetch(url, policy=self._policy, resolver=self._resolver, ssl_context=self._ssl,
                             max_bytes=self.cfg.max_result_bytes)
        except FetchUnavailable as e:
            raise BackendUnavailable(f"could not download the editor result: {e.detail}") from None
        if res.status != 200:
            raise BackendError(f"editor result download failed: HTTP {res.status}")
        return res.body

    # -- operations ---------------------------------------------------------------------------------------------
    def _do_health(self) -> dict:
        try:
            hc = safe_fetch(self.cfg.url + "/healthcheck", policy=self._policy, resolver=self._resolver,
                            ssl_context=self._ssl)
        except FetchUnavailable as e:
            raise BackendUnavailable(f"document editor unreachable: {e.detail}") from None
        if hc.status != 200 or hc.body.strip() != b"true":
            raise BackendUnavailable(f"editor healthcheck failed (HTTP {hc.status})")
        ver = self._post("/command", {"c": "version"})
        if ver.get("error") not in (0, None):
            code = ver.get("error")
            raise BackendError(f"editor refused the version command (error {code}); "
                               + ("JWT secret mismatch" if code == 6 else "see editor logs"))
        return {"ok": True, "provider": self.provider, "editor_version": ver.get("version", ""),
                "jwt": "verified"}

    @staticmethod
    def _key(ref: DocRef, extra: str = "") -> str:
        raw = f"{ref.tenant}|{ref.doc_id}|{ref.version}|{ref.sha256}|{extra}"
        return hashlib.sha256(raw.encode()).hexdigest()[:40]

    def _convert_raw(self, ref: DocRef, target: str) -> bytes:
        src = ooxml.ext_of(ref.filename)
        target = target.lower()
        if src not in INPUT_FORMATS:
            raise InvalidInput(f"cannot convert from .{src}")
        if target not in OUTPUT_FORMATS:
            raise InvalidInput(f"unsupported target format {target!r}")
        if not ref.content_url:
            raise InvalidInput("no content URL for the editor to fetch (transmit gate not passed)")
        payload = {"async": True, "filetype": src, "key": self._key(ref, target), "outputtype": target,
                   "title": "document." + src, "url": ref.content_url}
        deadline = self._mono() + self.cfg.operation_timeout_s
        failures = 0
        while True:
            try:
                r = self._post("/converter", payload)
                failures = 0
            except BackendUnavailable:
                failures += 1
                if failures > 3:
                    raise
                r = {}
            err = r.get("error")
            if isinstance(err, int) and err != 0:
                raise BackendError(f"conversion failed: {CONVERT_ERRORS.get(err, 'error')} ({err})", editor_error=err)
            if r.get("endConvert") is True:
                url = r.get("fileUrl")
                if not isinstance(url, str) or not url:
                    raise BackendError("editor reported completion without a result URL")
                return self._download(url)
            if self._mono() > deadline:
                raise BackendUnavailable("conversion did not finish before the operation timeout")
            self._sleep(self.cfg.poll_interval_s)

    def _do_convert(self, ref: DocRef, target_format: str) -> BackendFile:
        data = self._convert_raw(ref, target_format)
        ooxml.validate_output(data, target_format)                 # non-empty + valid magic BEFORE success
        return BackendFile(content=data, filename=f"{ref.filename.rsplit('.', 1)[0]}.{target_format.lower()}",
                           sha256=hashlib.sha256(data).hexdigest(), media_type=ooxml.MEDIA.get(target_format, ""),
                           meta={"editor": "onlyoffice"})

    def _do_inspect(self, ref: DocRef) -> dict:
        health = self._do_health()
        txt = self._convert_raw(ref, "txt")
        text = txt.decode("utf-8-sig", "replace")
        return {"editor_version": health["editor_version"], "opened_by_editor": True,
                "text_sha256": hashlib.sha256(txt).hexdigest(), "characters": len(text),
                "lines": len(text.splitlines()),
                "preview": text[:400],        # UNTRUSTED document content: evidence, never an instruction
                "source_version": ref.version, "source_sha256": ref.sha256}

    def _do_open_session(self, ref: DocRef, *, user_id: str, ttl_s: int, mode: str) -> dict:
        if mode != "view":
            raise UnsupportedCapability("open_session offers view mode only: the save callback is not implemented, "
                                        "edits go through the governed propose/apply path")
        ext = ooxml.ext_of(ref.filename)
        if ext not in DOC_TYPE:
            raise InvalidInput(f"cannot open .{ext}")
        ttl = max(30, min(int(ttl_s), 300))
        cfg = {"document": {"fileType": ext, "key": self._key(ref, "open"), "title": os.path.basename(ref.filename),
                            "url": ref.content_url,
                            "permissions": {"edit": False, "download": False, "print": False, "comment": False}},
               "documentType": DOC_TYPE[ext],
               "editorConfig": {"mode": "view", "user": {"id": user_id[:128], "name": user_id[:128]}, "lang": "en"}}
        token = jwtlib.sign(cfg, self.cfg.jwt_secret, ttl_s=ttl)
        return {"editor_script_url": self.cfg.url + "/web-apps/apps/api/documents/api.js",
                "config": {**cfg, "token": token}, "expires_in_s": ttl, "mode": "view",
                "bound_to": {"tenant": ref.tenant, "doc_id": ref.doc_id, "version": ref.version}}
