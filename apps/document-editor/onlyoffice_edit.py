"""ONLYOFFICE automated edit through the documented Document Builder service (``POST /docbuilder``).

Proven against a real stock Document Server (see EDIT_API.md): a FIXED script template (``patch.builder_script``) is
fetched by the editor from a short-lived signed connector URL, receives the validated operations as JSON in the
documented ``argument`` request parameter, opens the source by URL, applies ``SearchAndReplace`` / paragraph appends /
cell writes (with ``SetTrackRevisions`` when requested) and saves ``result.<ext>``. No free-form script ever runs.
"""
from __future__ import annotations

import hashlib

from . import ooxml, patch as patchmod
from .backend import BackendFile, DocRef
from .errors import BackendError, BackendUnavailable, OutcomeUnknown
from .onlyoffice import BUILDER_ERRORS, OnlyOfficeBackend


class OnlyOfficeEditBackend(OnlyOfficeBackend):
    PROVEN = OnlyOfficeBackend.PROVEN | frozenset({"automated_edit", "tracked_changes"})
    PROOF_TESTS = {**OnlyOfficeBackend.PROOF_TESTS,
                   "automated_edit": "test_automated_edit_docx_and_xlsx",
                   "tracked_changes": "test_tracked_changes_produce_revisions"}

    def _do_apply_edit(self, ref: DocRef, ops, *, track_changes: bool, idem_key: str) -> BackendFile:
        fmt = ooxml.ext_of(ref.filename)
        script = patchmod.builder_script(fmt)                       # fixed template; ops travel as JSON data
        sha = hashlib.sha256(script).hexdigest()
        script_url = self.content.issue(kind="script", tenant=ref.tenant, sha256=sha, backend=self.provider,
                                        inline=script, media_type="application/javascript")
        payload = {"async": True, "url": script_url,
                   "argument": {"src": ref.content_url, "ops": ops, "track": bool(track_changes)}}
        first = self._post("/docbuilder", payload, write=True)       # THE write: exactly one attempt, never retried
        key = first.get("key")
        err = first.get("error")
        if isinstance(err, int) and err != 0:
            raise BackendError(f"document builder failed: {BUILDER_ERRORS.get(err, 'error')} ({err})", editor_error=err)
        if not isinstance(key, str) or not key:
            raise BackendError("document builder returned no task key")
        deadline = self._mono() + self.cfg.operation_timeout_s
        r, failures = first, 0
        while not r.get("end"):
            if self._mono() > deadline:
                raise OutcomeUnknown("document builder did not finish before the operation timeout", task_key=key)
            self._sleep(self.cfg.poll_interval_s)
            try:
                r = self._post("/docbuilder", {"async": True, "key": key})     # status polling is idempotent
                failures = 0
            except BackendUnavailable:
                failures += 1
                if failures > 3:
                    raise OutcomeUnknown("lost the editor while the builder task was running", task_key=key) from None
                continue
            err = r.get("error")
            if isinstance(err, int) and err != 0:
                raise BackendError(f"document builder failed: {BUILDER_ERRORS.get(err, 'error')} ({err})",
                                   editor_error=err)
        urls = r.get("urls") or {}
        name = f"result.{fmt}"
        if name not in urls:
            raise BackendError("document builder produced no result file")
        data = self._download(urls[name])
        ooxml.validate_output(data, fmt)
        return BackendFile(content=data, filename=name, sha256=hashlib.sha256(data).hexdigest(),
                           media_type=ooxml.MEDIA.get(fmt, ""), meta={"builder_key": key})
