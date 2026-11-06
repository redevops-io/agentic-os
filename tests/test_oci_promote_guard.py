"""The owner-terminal promotion draft (docs/release/oci-promote-owner.sh.draft): its 'does the destination tag exist' guard must tell the
registry's 404 from EVERY other answer (401, 403, 429, 5xx, a refused connection) so a network or permission problem can never read as
'absent'; the script must parse on the bash 3.2 that macOS ships and use no bash 4 feature; and it must refuse to run against a remote
registry in rehearsal mode. A fake registry on loopback answers the status each repository name asks for. Needs `oras` (skipped without)."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docs" / "release" / "oci-promote-owner.sh.draft"
ORAS = shutil.which("oras")


class _Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _do(self):
        if self.path.rstrip("/") == "/v2":
            self.send_response(200)
            self.end_headers()
            return
        parts = self.path.split("/")
        repo = parts[2] if len(parts) > 4 else ""
        code = {"s401": 401, "s403": 403, "s404": 404, "s429": 429, "s500": 500, "s503": 503, "sok": 200}.get(repo, 404)
        body = json.dumps({"errors": [{"code": "X", "message": "x"}]}).encode()
        if code == 200:
            body = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                               "config": {"mediaType": "application/vnd.oci.empty.v1+json", "digest": "sha256:" + "0" * 64, "size": 0}, "layers": []}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if code == 200:
            self.send_header("Docker-Content-Digest", "sha256:" + "1" * 64)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_HEAD = _do


@pytest.fixture(scope="module")
def fake():
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def _state(ref: str) -> str:
    r = subprocess.run(["bash", "-c", f'source "{SCRIPT}"; ORAS_FLAGS=(--plain-http); tag_state "{ref}"'],
                       env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "PROMOTE_FUNCTIONS_ONLY": "1", "HOME": "/nonexistent"},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.skipif(ORAS is None, reason="oras is not installed")
@pytest.mark.parametrize("repo,expected", [("s404", "absent"), ("sok", "exists"), ("s401", "error"), ("s403", "error"), ("s429", "error"),
                                           ("s500", "error"), ("s503", "error")])
def test_only_a_404_reads_as_absent(fake, repo, expected):
    out = _state(f"127.0.0.1:{fake}/{repo}/x:1.2.3")
    assert out.split(":", 1)[0] == expected, out


@pytest.mark.skipif(ORAS is None, reason="oras is not installed")
def test_a_refused_connection_is_an_error_not_absent():
    out = _state("127.0.0.1:1/x/y:1.2.3")
    assert out.startswith("error:"), out


def test_the_draft_parses_on_bash_3_2_and_uses_no_bash_4_feature():
    text = SCRIPT.read_text()
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    for feature in ("mapfile", "readarray", "declare -A", "${!", "&>>", "|&", "${@,,}", "${@^^}"):
        assert feature not in code, feature
    if Path("/bin/bash").exists():
        r = subprocess.run(["/bin/bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_the_draft_never_builds_and_refuses_what_it_should():
    code = "\n".join(l for l in SCRIPT.read_text().splitlines() if not l.lstrip().startswith("#"))
    assert "docker build" not in code and "buildx build" not in code
    for needle in ("must be a staged-* package", "must not be a staged-* package", "tags are never overwritten", "cannot establish that",
                   "is only accepted when both registries are on this machine", "(foreign or additional material)", "are not sigstore bundles",
                   "AFTER COPY: pre-existing tag", "AFTER COPY: destination index bytes differ"):
        assert needle in code.replace("the source repository ", "").replace("the destination ", "") or needle in code, needle
    assert "EXECUTE=0" in code and 'if [ "$EXECUTE" != 1 ]' in code        # dry run by default


def test_rehearsal_mode_is_refused_for_a_remote_registry():
    r = subprocess.run(["bash", str(SCRIPT), "--src", "ghcr.io/o/staged-x", "--dst", "ghcr.io/o/x", "--index", "sha256:" + "a" * 64,
                        "--tag", "1.0.0", "--pub", str(SCRIPT)],
                       env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "PROMOTE_LOCAL_REHEARSAL": "1", "HOME": "/nonexistent"},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "only accepted when both registries are on this machine" in r.stderr, r.stderr
