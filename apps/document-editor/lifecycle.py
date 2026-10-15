"""Uninstall support: remove ONLY this connector's own state.

Never deletes documents, document versions, receipts (``<root>/<tenant>/.docedit``), published copies, or anything on
the external editor. The state directory (proposals only) is removed solely when it carries this connector's marker
file, so a mis-configured path can never delete an unrelated directory.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .errors import InvalidInput

MARKER = ".docedit-state"


def uninstall_connector_state(state_dir: "str | Path") -> dict:
    p = Path(state_dir)
    if not p.is_dir():
        return {"removed": False, "reason": "no state directory"}
    if not (p / MARKER).is_file():
        raise InvalidInput(f"{p} does not carry the connector's state marker; refusing to delete it")
    if p.is_symlink():
        raise InvalidInput("state directory is a symlink; refusing")
    shutil.rmtree(p)
    return {"removed": True, "kept": "documents, versions, receipts, published copies and the external editor"}
