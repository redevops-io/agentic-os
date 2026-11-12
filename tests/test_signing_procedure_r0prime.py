"""Tests of the R0-prime signing PREPARATION in packaging/signing/ (nothing here signs anything).

* No secret, no network, no real identity, no keychain, no Azure, no ssh-agent. Every script runs under an `env -i` allowlist.
* macOS signing script: the codesign / hdiutil / xcrun / spctl / lipo tools are STUBS placed first on PATH (their behaviour is
  driven by a stub.conf next to them); the command sequence, the refusal paths and the no-secret-printing rules are proven
  against the stubs. One test (skipped without clang) runs the WHOLE sequence with the real tools and the ad-hoc identity '-'
  on a tiny arm64 fixture app in a temporary directory.
* SSHSIG script: a THROWAWAY ed25519 key is generated into a temporary directory (comment signproc-TEST-KEY) and never put in an
  agent; the key files are unlinked by name at the end of each test that made them.
* Windows signing script + hand-off helper: stub java / python / az; no Azure, no token.

macOS only (plutil, shasum, ditto, ssh-keygen are used); everything is skipped elsewhere.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import textwrap
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from .about_fixture import Signer, decide, licenses_for, pristine_allowed_text, pristine_gate_text, read_gate, signer, trust  # noqa: F401  (signer is a pytest fixture)

ROOT = Path(__file__).resolve().parents[1]
SIGN = ROOT / "packaging" / "signing"
MAC = SIGN / "sign_and_notarize_r0prime_macos_v3.sh"
WIN = SIGN / "sign_windows_r0prime_v3.sh"
SSH = SIGN / "sign_manifest_sshsig.sh"
HELPER_PROD = SIGN / "r0prime_win_signcommand.sh"
VERIFIER = SIGN / "verify_signature_v2.py"
COMMON = SIGN / "signing-common.sh"
RELEASE_FPR = "SHA256:wUoE8wxKJpmtTH7GqWXMIl9/BW0SH1Isc5/w9HEiwqA"      # as published in SIGNING_REQUEST.md / VERIFY.md (provenance: LITERALS-PROVENANCE.json)
GATE_JSON = ROOT / "packaging" / "distribution-gate.json"
GATE_PY = ROOT / "packaging" / "check-distribution-gate.py"
IDJSON = ROOT / "packaging" / "identity.json"
ENT = ROOT / "packaging" / "macos" / "sidecar.entitlements.plist"
IDENT = json.loads((ROOT / "packaging" / "identity.json").read_text())
R0 = IDENT["r0prime"]
FROZ = IDENT["frozen"]
APP_NAME = R0["product_name"] + ".app"
DMG_NAME = f"{R0['product_name']}_{R0['version']}_aarch64.dmg"
MSI_NAME = f"{R0['product_name']}_{R0['version']}_x64_en-US.msi"
NSIS_NAME = f"{R0['product_name']}_{R0['version']}_x64-setup.exe"
TEST_IDENTITY = "Developer ID Application: Stub Test Identity (TESTTEAM01)"
TEST_PROFILE = "stub-notary-profile-name"
SYSTEM_PATH = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS tools (plutil, shasum, ditto, ssh-keygen) are used")


def sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def sha_file(p: Path) -> str:
    return sha_bytes(Path(p).read_bytes())


def about_pins(tree: Path, lic: Path) -> dict:
    """The RC3 About pins of a scope file (scope_version 3) for the private copy `tree` (a packaging/ directory) and the Licenses folder
    `lic` of the artifact: the sha256 of every file the About check executes or reads as its signer list, and the authorized build commit."""
    return {"check_about_sha256": sha_file(tree / "notices" / "check_about.py"), "generate_about_sha256": sha_file(tree / "notices" / "generate_about.py"),
            "about_record_sha256": sha_file(tree.parent / "agentic_os" / "about_record.py"), "legal_signers_sha256": sha_file(tree / "legal-allowed-signers"),
            "build_commit": json.loads((lic / "about.json").read_text())["build"]["source"]["commit"]}


def clean_env(home: Path, path_dirs=(), **extra) -> dict:
    """An allowlist environment: nothing of the caller's environment is inherited."""
    env = {"PATH": ":".join([str(d) for d in path_dirs] + SYSTEM_PATH), "HOME": str(home), "LANG": "C"}
    env.update({k: str(v) for k, v in extra.items()})
    return env


def run(cmd, env, cwd=None, timeout=300, input=None):
    return subprocess.run([str(c) for c in cmd], env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout, input=input)


def make_tree(dest: Path) -> Path:
    """`dest` is the packaging/ directory of a throw-away repository root (dest.parent): besides the signing scripts it receives the files the
    About check reads (agentic_os/about_record.py, identity, versions, the product licence files, packaging/notices/check_about.py and
    generate_about.py, an EMPTY packaging/legal-allowed-signers)."""
    assert dest.name == "packaging", dest
    _make_tree(dest)
    root = dest.parent
    for rel in ("agentic_os/about_record.py", "agentic_os/__init__.py", "pyproject.toml", "deploy/launcher/src-tauri/tauri.conf.json",
                "deploy/launcher/src-tauri/Cargo.toml", "LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md", "LICENSING.md"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, root / rel)
    (dest / "notices").mkdir()
    for n in ("check_about.py", "generate_about.py"):
        shutil.copy(ROOT / "packaging" / "notices" / n, dest / "notices" / n)
    (dest / "legal-allowed-signers").write_text(pristine_allowed_text(), encoding="utf-8")
    (root / "agentic_os" / "projects_ui").mkdir(exist_ok=True)
    (root / "agentic_os" / "projects_ui" / "index.html").write_text("<!doctype html>\n")
    return dest


def _make_tree(dest: Path) -> Path:
    """A private copy of packaging/ (signing scripts, identity.json, entitlement plist, gate files) for the stub tests of the --real paths. In the
    copy of signing-common.sh the single patch point `r0p_no_test_switch` is replaced by a no-op, because the PRODUCTION library refuses the
    unit-test switch in --real (R5-02); the copy therefore has another sha256, which the test scope files bind."""
    (dest / "signing").mkdir(parents=True)
    (dest / "macos").mkdir()
    for p in SIGN.iterdir():
        if p.is_file():
            shutil.copy(p, dest / "signing" / p.name)
    shutil.copytree(SIGN / "tools", dest / "signing" / "tools")
    for src in (IDJSON, GATE_JSON, GATE_PY):
        if src == GATE_JSON:
            (dest / src.name).write_text(pristine_gate_text(), encoding="utf-8")      # the shipped policy before any owner signature
        else:
            shutil.copy(src, dest / src.name)
    shutil.copy(ENT, dest / "macos" / ENT.name)
    def patch(path, mapping):
        lines = path.read_text().splitlines(keepends=True)
        out = []
        for ln in lines:
            for marker, new in mapping.items():
                if marker in ln:
                    ln = new
            out.append(ln)
        assert out != lines, path
        path.write_text("".join(out))

    patch(dest / "signing" / "signing-common.sh", {"R0P-TEST-PATCH-POINT\n": "r0p_no_test_switch() { :; }   # TEST COPY: the production library refuses the switch in --real\n"})
    for name in (MAC.name, WIN.name, SSH.name):       # the first block of each script refuses the switch in --real and drops the inherited PATH in --real: not in the test copies
        patch(dest / "signing" / name, {"R0P-TEST-PATCH-POINT-PRE": "if false; then   # TEST COPY\n",
                                        "R0P-TEST-PATCH-POINT-PATH": '  if [[ "${R0P_ALLOW_NONSYSTEM_TOOLS:-}" == stub-test ]]; then R0P_PATHV="$R0P_ORIG_PATH"; fi   # TEST COPY\n'})
    patch(dest / "signing" / HELPER_PROD.name, {"R0P-TEST-PATCH-POINT-PS": 'PS_EXE="${R0P_POWERSHELL:-}"   # TEST COPY: the shipped helper has no override\n'})
    return dest


HELPER = make_tree(Path(__import__("tempfile").mkdtemp(prefix="r0p-helper-")) / "packaging") / "signing" / HELPER_PROD.name       # patched copy (see make_tree)


def ledger_text(d: Path) -> str:
    """All event lines of a ledger directory, in order."""
    ev = Path(d) / "events"
    return "".join(f.read_text() for f in sorted(ev.glob("*.txt"))) if ev.is_dir() else ""


def ledger_claims(d: Path) -> list:
    return sorted(p.name for p in (Path(d) / "claims").glob("*")) if (Path(d) / "claims").is_dir() else []


def write_exec(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


# ====================================================================================================================
# static properties of every script
# ====================================================================================================================
def code_of(script: Path) -> str:
    """The code lines of a script: full-line comments and trailing ' # comments' removed."""
    out = []
    for ln in script.read_text().splitlines():
        if ln.lstrip().startswith("#"):
            continue
        out.append(re.split(r"\s#\s", ln, maxsplit=1)[0])
    return "\n".join(out)


SCRIPTS = [MAC, WIN, SSH, HELPER_PROD, COMMON, SIGN / "tools" / "make_adhoc_fixture.sh"]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_syntax_and_static_rules(script):
    assert script.is_file()
    if script != COMMON:
        assert os.access(script, os.X_OK), f"{script} must be executable"
    assert subprocess.run(["/bin/bash", "-n", str(script)], capture_output=True).returncode == 0
    code = code_of(script)
    assert not re.search(r"\brm\s+-[a-zA-Z]*(rf|fr|r)\b", code), "no rm -r / rm -rf"
    assert "sudo " not in code
    assert "set -x" not in code.replace("set +x", "") and "bash -x" not in code
    assert not re.search(r"(^|[;&|(\s])security\s+(find|add|delete|import|unlock|list|dump|cms)", code), "the keychain tool is never called"
    if script != COMMON:
        assert "umask 077" in code
    if script in (MAC, WIN, SSH, HELPER_PROD):
        assert 'case "$-" in *x*)' in code, "xtrace refusal"
    if script in (MAC, WIN, SSH):
        assert "R0P_SCRUBBED" not in code, "no marker variable can skip the environment scrub (round 4, R4-05)"
        assert "bash -c" not in code and "eval \"$" not in code.replace('eval "S_$', ''), "no command strings are built and evaluated (R4-06)"


def test_shellcheck_if_available():
    sc = shutil.which("shellcheck")
    if not sc:
        pytest.skip("shellcheck is not installed (not installed by this suite)")
    r = subprocess.run([sc, "-S", "warning", "-s", "bash", str(MAC), str(WIN), str(SSH), str(HELPER_PROD)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_no_deep_for_signing_and_no_frozen_literals_in_the_macos_script():
    code = code_of(MAC)
    assert "--deep" not in code, "the bundle is verified per binary with --strict; --deep is never used"
    for lit in (FROZ["bundle_identifier"], FROZ["sidecar_signing_identifier"], FROZ["main_binary_name"] + '"', "Agentic.Apps_0.1.0", "q8a92dm4t7"):
        for ln in code.splitlines():
            if lit.lower() in ln.lower():
                raise AssertionError(f"frozen literal {lit!r} in code line: {ln.strip()}")
    # the only frozen product name allowed in a code line is inside an explanatory refusal message
    for ln in code.splitlines():
        if "Agentic Apps" in ln and "frozen" not in ln and not ln.lstrip().startswith("ev "):
            raise AssertionError(f"hard-coded product name: {ln.strip()}")


def test_verifier_is_the_frozen_one_unchanged():
    assert sha_file(VERIFIER) == "e43b6f0f1dbb5587712cce2fe773053aeaece8a60381c37787f2e18bdeeb7de3"


def test_entitlements_file_is_the_expected_single_key():
    d = plistlib.loads(ENT.read_bytes())
    assert d == {"com.apple.security.cs.disable-library-validation": True}


# ====================================================================================================================
# macOS script with stub tools
# ====================================================================================================================
STUB_CODESIGN = r'''#!/bin/bash
D="$(cd "$(dirname "$0")" && pwd)"; . "$D/stub.conf"
echo "codesign $*" >> "$LOG"
env | sed 's/=.*//' | sort >> "$STATE/codesign-env"
ls /dev/fd | tr '\n' ' ' >> "$STATE/codesign-fds"; echo >> "$STATE/codesign-fds"
key() { printf '%s' "$1" | shasum -a 256 | cut -c1-16; }
st() { echo "$STATE/$(key "$1").st"; }
if [ "$1" = "--verify" ]; then
  last="${@: -1}"
  if [ -n "${VERIFY_FAIL_SUBSTR:-}" ]; then case "$last" in *"$VERIFY_FAIL_SUBSTR"*) echo "stub: invalid signature" >&2; exit 1;; esac; fi
  echo "stub: valid on disk" >&2; exit 0
fi
if [ "$1" = "-dvvv" ]; then
  t="$2"; f="$(st "$t")"
  [ -f "$f" ] || { echo "$t: code object is not signed at all" >&2; exit 1; }
  . "$f"
  echo "Executable=$t" >&2
  echo "Identifier=$id" >&2
  if [ "$adhoc" = 1 ]; then fl="0x10002(adhoc,runtime)"; else fl="0x10000(runtime)"; fi
  [ "$rt" = 1 ] || fl="0x0(none)"
  echo "CodeDirectory v=20500 size=100 flags=$fl hashes=3 location=embedded" >&2
  if [ "$kind" = app ]; then echo "Info.plist entries=17" >&2; echo "Sealed Resources version=2 rules=13 files=$sealed" >&2
  elif [ "$kind" = file ]; then echo "Info.plist=not bound" >&2; echo "Sealed Resources=none" >&2; fi
  if [ "$adhoc" = 1 ]; then echo "Signature=adhoc" >&2; echo "TeamIdentifier=not set" >&2
  else
    echo "Authority=Developer ID Application: Stub Test Identity (TESTTEAM01)" >&2; echo "Authority=Developer ID Certification Authority" >&2; echo "Authority=Apple Root CA" >&2
    [ "$ts" = 1 ] && echo "Timestamp=Nov 4, 2026 at 12:00:00" >&2
    echo "TeamIdentifier=${TEAM_OVERRIDE:-TESTTEAM01}" >&2
  fi
  echo "CDHash=0123456789abcdef0123456789abcdef01234567" >&2
  exit 0
fi
if [ "$1" = "-d" ]; then
  t="${@: -1}"; f="$(st "$t")"
  [ -f "$f" ] || { echo "$t: code object is not signed at all" >&2; exit 1; }
  . "$f"
  echo "Executable=$t" >&2
  [ "${SABOTAGE_ENT:-}" = "$(basename "$t")" ] && exit 1
  xml=0; for a in "$@"; do case "$a" in --xml|:-) xml=1;; esac; done
  if [ "$xml" = 1 ]; then
    [ "$ent" = yes ] && echo '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>com.apple.security.cs.disable-library-validation</key><true/></dict></plist>'
    [ -n "${EXTRA_ENT:-}" ] && [ "$ent" = yes ] && echo "" >/dev/null
  else
    [ "$ent" = yes ] && printf '[Dict]\n\t[Key] com.apple.security.cs.disable-library-validation\n\t[Value]\n\t\t[Bool] true\n'
  fi
  exit 0
fi
# signing: -s ID --force TS --options runtime [--identifier X] [--entitlements F] TARGET
id_arg=""; ident=""; entf=""; rt=0; ts=0; adhoc=0
while [ $# -gt 1 ]; do
  case "$1" in
    -s) id_arg="$2"; shift 2;;
    --identifier) ident="$2"; shift 2;;
    --entitlements) entf="$2"; shift 2;;
    --options) [ "$2" = runtime ] && rt=1; shift 2;;
    --timestamp) ts=1; shift;;
    --timestamp=none) ts=0; shift;;
    --force) shift;;
    *) shift;;
  esac
done
target="$1"
[ "$id_arg" = "-" ] && adhoc=1
if [ -d "$target" ]; then   # bundle: seals resources, re-signs the main executable with the bundle id
  bid="$(plutil -extract CFBundleIdentifier raw -o - "$target/Contents/Info.plist")"
  exe="$(plutil -extract CFBundleExecutable raw -o - "$target/Contents/Info.plist")"
  nres="$(find "$target/Contents/Resources" -type f | wc -l | tr -d ' ')"
  printf 'id=%s\nent=no\nrt=%s\nadhoc=%s\nts=%s\nkind=app\nsealed=%s\n' "$bid" "$rt" "$adhoc" "$ts" "$((nres + 1))" > "$(st "$target")"
  printf 'id=%s\nent=no\nrt=%s\nadhoc=%s\nts=%s\nkind=exe\nsealed=0\n' "$bid" "$rt" "$adhoc" "$ts" > "$(st "$target/Contents/MacOS/$exe")"
else
  [ -n "$ident" ] || ident="$(basename "$target")"
  e=no; [ -n "$entf" ] && e=yes
  k=exe; case "$target" in *.dmg) k=file;; esac
  printf 'id=%s\nent=%s\nrt=%s\nadhoc=%s\nts=%s\nkind=%s\nsealed=0\n' "$ident" "$e" "$rt" "$adhoc" "$ts" "$k" > "$(st "$target")"
fi
exit 0
'''

STUB_HDIUTIL = r'''#!/bin/bash
D="$(cd "$(dirname "$0")" && pwd)"; . "$D/stub.conf"
echo "hdiutil $*" >> "$LOG"
tree_of() { mkdir -p "$STATE/trees"; echo "$STATE/trees/$(printf '%s' "$1" | shasum -a 256 | cut -c1-16)"; }
case "$1" in
  info) exit 0;;
  attach)
    mp=""; prev=""; for a in "$@"; do [ "$prev" = -mountpoint ] && mp="$a"; prev="$a"; done
    img="${@: -1}"
    mkdir -p "$mp"
    case "$img" in
      *input-copy.dmg) ditto "$FIXTURE" "$mp";;
      *) tr="$(tree_of "$img")"; [ -d "$tr" ] && ditto "$tr" "$mp";;
    esac
    echo "$mp" >> "$STATE/mounts";;
  detach)
    mp="$2"; echo "$mp" >> "$STATE/detached"
    [ -d "$mp" ] && find "$mp" -mindepth 1 -delete
    exit 0;;
  create)
    src=""; prev=""; for a in "$@"; do [ "$prev" = -srcfolder ] && src="$a"; prev="$a"; done
    out="${@: -1}"
    ditto "$src" "$(tree_of "$out")"; echo "stub rw image" > "$out"; echo "created: $out";;
  convert)
    rw="$2"; out=""; prev=""; for a in "$@"; do [ "$prev" = -o ] && out="$a"; prev="$a"; done
    ditto "$(tree_of "$rw")" "$(tree_of "$out")"; printf 'stub-udzo-image' > "$out";;
esac
exit 0
'''

STUB_XCRUN = r'''#!/bin/bash
D="$(cd "$(dirname "$0")" && pwd)"; . "$D/stub.conf"
echo "xcrun $*" >> "$LOG"
env | grep -E '^(AWS|AZURE|GH_|GITHUB|SSH_|APPROVAL|ANTHROPIC)' >> "$STATE/xcrun-env" || true
case "$1 $2" in
  "notarytool submit")
    case "$NOTARY_MODE" in
      ok) echo '{"id":"11111111-2222-3333-4444-555555555555","status":"Accepted","message":"Processing complete"}';;
      invalid) echo '{"id":"11111111-2222-3333-4444-555555555555","status":"Invalid","message":"The binary is not signed"}';;
      timeout) echo "Error: timed out" >&2; exit 1;;
    esac;;
  "notarytool log") out="${@: -1}"; echo '{"status":"Accepted","ticketContents":[]}' > "$out";;
  "stapler staple") exit "${STAPLE_RC:-0}";;
  "stapler validate")
    last="${@: -1}"
    if [ -d "$last" ]; then echo "$last does not have a ticket stapled to it." >&2; exit 65; fi
    echo "The validate action worked!"; exit 0;;
esac
exit 0
'''

STUB_SIMPLE = {
    "spctl": '#!/bin/bash\nD="$(cd "$(dirname "$0")" && pwd)"; . "$D/stub.conf"\necho "spctl $*" >> "$LOG"\necho "stub spctl" >&2\nexit "${SPCTL_RC:-0}"\n',
    "lipo": '#!/bin/bash\nD="$(cd "$(dirname "$0")" && pwd)"; . "$D/stub.conf"\necho "lipo $*" >> "$LOG"\necho "${LIPO_ARCHS:-arm64}"\n',
    "SetFile": '#!/bin/bash\nD="$(cd "$(dirname "$0")" && pwd)"; . "$D/stub.conf"\necho "SetFile $*" >> "$LOG"\nexit 0\n',
    "GetFileInfo": '#!/bin/bash\necho "attributes: avbstClinmedz"\n',
}


class Mac:
    """A stubbed macOS signing environment in a temp directory."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.stubs = tmp / "stubs"
        self.state = tmp / "stubstate"
        self.home = tmp / "home"
        self.work = tmp / "signing"          # the fresh signing directory that holds the COPY of the input
        for d in (self.stubs, self.state, self.home, self.work):
            d.mkdir()
        self.log = tmp / "stub.log"
        self.log.write_text("")
        self.fixture = tmp / "fixture"
        self.tree = make_tree(tmp / "mroot" / "packaging")
        self.lic = licenses_for(self.tree.parent, tmp / "licwork")      # a REAL generated Licenses folder (about.json included), consistent with the tree
        self.script = self.tree / "signing" / MAC.name
        self.common = self.tree / "signing" / COMMON.name
        self.conf(NOTARY_MODE="ok")
        write_exec(self.stubs / "codesign", STUB_CODESIGN)
        write_exec(self.stubs / "hdiutil", STUB_HDIUTIL)
        write_exec(self.stubs / "xcrun", STUB_XCRUN)
        for n, t in STUB_SIMPLE.items():
            write_exec(self.stubs / n, t)
        self.build_fixture()
        self.dmg = self.work / "input" / DMG_NAME
        self.dmg.parent.mkdir()
        self.dmg.write_bytes(b"stub input dmg for the signing sequence")
        self.out = self.work / "out"

    def conf(self, **kw):
        vals = {"LOG": str(self.log), "STATE": str(self.state), "FIXTURE": str(self.fixture), "NOTARY_MODE": "ok", "SPCTL_RC": "0", "LIPO_ARCHS": "arm64"}
        vals.update(getattr(self, "_conf", {}))
        vals.update({k: str(v) for k, v in kw.items()})
        self._conf = vals
        (self.stubs / "stub.conf").write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in vals.items()))

    def build_fixture(self, product_dir_name=None, bundle_id=None, extra=None):
        fx = self.fixture
        app = fx / (product_dir_name or APP_NAME)
        (app / "Contents" / "MacOS").mkdir(parents=True)
        (app / "Contents" / "Resources" / "Licenses").mkdir(parents=True)
        write_exec(app / "Contents" / "MacOS" / R0["main_binary_name"], "#!stub-launcher\n")
        write_exec(app / "Contents" / "MacOS" / R0["sidecar_binary_name"], "#!stub-sidecar\n")
        info = {"CFBundleExecutable": R0["main_binary_name"], "CFBundleIdentifier": bundle_id or R0["bundle_identifier"],
                "CFBundleShortVersionString": R0["version"], "CFBundleVersion": R0["bundle_version"], "LSMinimumSystemVersion": "11.0",
                "RedevopsReleaseLine": IDENT["release_line"], "CFBundleName": R0["product_name"]}
        (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
        (app / "Contents" / "Resources" / "icon.icns").write_bytes(b"icns")
        for f in self.lic.iterdir():
            shutil.copy(f, app / "Contents" / "Resources" / "Licenses" / f.name)
        (fx / ".VolumeIcon.icns").write_bytes(b"icns")
        os.symlink("/Applications", fx / "Applications")
        for rel, text in (extra or {}).items():
            p = fx / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            write_exec(p, text) if rel.endswith(".sh") else p.write_text(text)

    def reset_fixture(self, **kw):
        for p in sorted(self.fixture.rglob("*"), key=lambda q: -len(q.parts)):
            p.unlink() if (p.is_file() or p.is_symlink()) else p.rmdir()
        self.build_fixture(**kw)

    def env(self, stubs=True, **extra):
        # the stub switch lets the script use the stub tools on PATH; without it PATH is fixed to the system directories
        if stubs:
            extra.setdefault("R0P_ALLOW_NONSYSTEM_TOOLS", "stub-test")
        return clean_env(self.home, [self.stubs], **extra)

    def run(self, *args, mode=None, input_dmg=None, out=None, sha=None, stubs=True, **extra_env):
        a = ["/bin/bash", self.script]
        if mode:
            a.append(mode)
        a += ["--input-dmg", input_dmg or self.dmg, "--expected-sha256", sha or sha_file(Path(input_dmg or self.dmg)), "--out-dir", out or self.out]
        a += list(args)
        return run(a, self.env(stubs=stubs, **extra_env))

    def calls(self, tool=None):
        lines = self.log.read_text().splitlines()
        return [l for l in lines if tool is None or l.startswith(tool + " ")]

    def scope(self, **over) -> Path:
        now = datetime.now(timezone.utc)
        d = {"scope_version": "3", "not_before": iso(now - timedelta(hours=1)), "not_after": iso(now + timedelta(hours=1)),
             "script_sha256": sha_file(self.script), "common_sha256": sha_file(self.common), "entitlements_sha256": sha_file(ENT),
             "identity_json_sha256": sha_file(IDJSON), "input_dmg_sha256": sha_file(self.dmg),
             "identity_sha256": sha_bytes(TEST_IDENTITY.encode()), "notary_profile_sha256": sha_bytes(TEST_PROFILE.encode()),
             "team_id": "TESTTEAM01", "notarize": "yes", "upload_to": "apple-notary", "distribution": "no",
             "gate_sha256": sha_file(self.tree / GATE_JSON.name), "gate_tool_sha256": sha_file(GATE_PY), "evidence_manifest_sha256": "e" * 64,
             "procedure_commit": "c" * 40}
        d.update(about_pins(self.tree, self.lic))
        d.update(over)
        p = self.tmp / f"scope-{len(list(self.tmp.glob('scope-*')))}.txt"
        p.write_text("# test scope\n" + "".join(f"{k}={v}\n" for k, v in d.items() if v is not None))
        return p

    @property
    def ledger(self) -> Path:
        return self.tmp / "ledger"

    def real(self, scope: Path, *extra, notarize=True, env=None, ledger=True, **kw):
        e = {"R0P_CODESIGN_IDENTITY": TEST_IDENTITY}
        if notarize:
            e["R0P_NOTARY_PROFILE"] = TEST_PROFILE
        e.update(env or {})
        args = ["--i-have-owner-authorization", sha_file(scope), "--scope-file", scope] + (["--notarize"] if notarize else [])
        if ledger:
            args += ["--ledger", self.ledger]
        return self.run(*args, *extra, mode="--real", **dict(e, **kw))


@pytest.fixture
def mac(tmp_path):
    return Mac(tmp_path)


def test_macos_names_derive_from_identity_json_and_equal_identity_py(mac):
    r = mac.run()
    assert r.returncode == 0, r.stderr
    py = subprocess.run([sys.executable, "-I", str(ROOT / "packaging" / "tools" / "identity.py"), "get", "dmg_macos_arm64"], capture_output=True, text=True).stdout.strip()
    assert py == DMG_NAME
    for needle in (APP_NAME, R0["main_binary_name"], R0["sidecar_signing_identifier"], R0["bundle_identifier"], DMG_NAME):
        assert needle in r.stdout
    # a changed identity.json changes the names: copy script + identity + plist to a temp tree and edit the product name
    tree = mac.tmp / "tree"
    (tree / "signing").mkdir(parents=True)
    (tree / "macos").mkdir()
    shutil.copy(MAC, tree / "signing" / MAC.name)
    shutil.copy(COMMON, tree / "signing" / COMMON.name)
    shutil.copy(ENT, tree / "macos" / ENT.name)
    ident = json.loads(json.dumps(IDENT))
    ident["r0prime"]["product_name"] = "Zed Test Product"
    (tree / "identity.json").write_text(json.dumps(ident))
    r2 = run(["/bin/bash", tree / "signing" / MAC.name, "--input-dmg", mac.dmg, "--expected-sha256", sha_file(mac.dmg), "--out-dir", mac.work / "o2"], mac.env())
    assert r2.returncode == 0 and "Zed Test Product.app" in r2.stdout and "Zed Test Product_0.1.1_aarch64.dmg" in r2.stdout


def test_dry_run_is_the_default_and_touches_nothing(mac):
    r = mac.run()
    assert r.returncode == 0, r.stderr
    assert "mode: DRY-RUN" in r.stdout and "nothing was created, signed, mounted or uploaded" in r.stdout
    assert not mac.out.exists()
    assert mac.calls() == [], "no tool may be started by a dry run"
    # the plan shows the real sequence, in order
    seq = [l for l in r.stdout.splitlines() if l.startswith("+ ")]
    joined = "\n".join(seq)
    order = [joined.index(x) for x in ("--identifier " + R0["sidecar_signing_identifier"], "agentic-r0prime", "hdiutil create", "notarytool submit", "stapler staple")]
    assert order == sorted(order)
    assert "<identity from R0P_CODESIGN_IDENTITY>" in joined and "--timestamp " in joined and "--options runtime" in joined
    assert "--deep" not in r.stdout


def test_dry_run_explicit_flag_and_hash_mismatch(mac):
    assert mac.run(mode="--dry-run").returncode == 0
    r = mac.run(sha="0" * 64)
    assert r.returncode == 1 and "sha256 mismatch" in r.stderr and not mac.out.exists()


REFUSALS = [
    ("identity env in dry-run", dict(env={"R0P_CODESIGN_IDENTITY": TEST_IDENTITY}), "R0P_CODESIGN_IDENTITY is set but the mode"),
    ("identity env in adhoc", dict(mode="--adhoc-test", env={"R0P_CODESIGN_IDENTITY": "Developer ID Application: X (ABCDEFGHIJ)"}), "R0P_CODESIGN_IDENTITY is set but the mode"),
    ("profile env in dry-run", dict(env={"R0P_NOTARY_PROFILE": "p"}), "R0P_NOTARY_PROFILE is set but the mode"),
    ("notarize in dry-run", dict(extra=["--notarize"]), "--notarize exists only with --real"),
    ("two modes", dict(extra=["--adhoc-test"], mode="--dry-run"), "at most one"),
    ("auth flag in dry run", dict(extra=["--i-have-owner-authorization", "0" * 64]), "used only with --real"),
    ("real without authorization", dict(mode="--real", env={"R0P_CODESIGN_IDENTITY": TEST_IDENTITY}), "--i-have-owner-authorization"),
]


@pytest.mark.parametrize("name,kw,msg", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_macos_mode_refusals(mac, name, kw, msg):
    kw = dict(kw)
    env = kw.pop("env", {})
    extra = kw.pop("extra", [])
    r = mac.run(*extra, **kw, **env)
    assert r.returncode != 0 and msg in r.stderr, (r.stdout, r.stderr)
    assert not mac.out.exists() and mac.calls() == []


def test_path_policy_refusals(mac):
    # an input inside a candidate-like directory
    cand = mac.tmp / "macos-arm64-unsigned-final9"
    cand.mkdir()
    shutil.copy(mac.dmg, cand / DMG_NAME)
    r = mac.run(input_dmg=cand / DMG_NAME)
    assert r.returncode == 1 and "candidate/evidence directory" in r.stderr
    # a directory that holds candidate marker files
    m2 = mac.tmp / "plain"
    m2.mkdir()
    shutil.copy(mac.dmg, m2 / DMG_NAME)
    (m2 / "README-LOCAL-BUILD.md").write_text("x")
    assert "candidate/evidence directory" in mac.run(input_dmg=m2 / DMG_NAME).stderr
    # --protected-dir
    m3 = mac.tmp / "prot"
    m3.mkdir()
    shutil.copy(mac.dmg, m3 / DMG_NAME)
    r = mac.run("--protected-dir", m3, input_dmg=m3 / DMG_NAME)
    assert r.returncode == 1 and "protected directory" in r.stderr
    # symlink input, out-dir non-empty, out-dir containing the input, out-dir = HOME
    link = mac.work / "link.dmg"
    os.symlink(mac.dmg, link)
    assert "regular file" in mac.run(input_dmg=link).stderr
    full = mac.work / "full"
    full.mkdir()
    (full / "x").write_text("x")
    assert "not empty" in mac.run(out=full).stderr
    assert "must not contain the input" in mac.run(out=mac.dmg.parent).stderr
    assert "unsafe --out-dir" in mac.run(out=mac.home).stderr
    assert mac.calls() == []


def test_adhoc_sequence_and_outputs_with_stubs(mac):
    r = mac.run(mode="--adhoc-test")
    assert r.returncode == 0, (r.stdout, r.stderr)
    cs = mac.calls("codesign")
    signs = [c for c in cs if " -s " in c]
    # inside-out order: sidecar (identifier + entitlements + runtime), launcher, bundle, dmg
    assert len(signs) == 4
    assert f"--identifier {R0['sidecar_signing_identifier']}" in signs[0] and "--entitlements" in signs[0] and "--options runtime" in signs[0] and signs[0].endswith(R0["sidecar_binary_name"])
    assert signs[1].endswith(R0["main_binary_name"]) and "--entitlements" not in signs[1] and "--options runtime" in signs[1]
    assert signs[2].endswith(APP_NAME) and "--entitlements" not in signs[2] and "--options runtime" in signs[2]
    assert signs[3].endswith(".dmg") and "--options" not in signs[3]
    assert all(" -s - " in c and "--timestamp=none" in c for c in signs) and not any("--deep" in c for c in cs)
    assert mac.calls("xcrun") == [], "no notarytool / stapler in an ad-hoc test"
    # verification per binary and for the bundle, without --deep
    assert any(c.startswith("codesign --verify --strict") and c.endswith(R0["sidecar_binary_name"]) for c in cs)
    assert any(c.startswith("codesign --verify --strict") and c.endswith(APP_NAME) for c in cs)
    # outputs
    out = mac.out
    assert (out / f"ADHOC-TEST-ONLY_{DMG_NAME}").is_file() and not (out / DMG_NAME).exists()
    assert (out / "ADHOC-TEST-NOT-A-RELEASE.txt").is_file()
    ev = (out / "EVIDENCE.txt").read_text()
    assert "AD-HOC TEST" in ev and "NOTHING (adhoc test)" in ev and "entitlements = {\"com.apple.security.cs.disable-library-validation\":true}" in ev
    assert "launcher: hardened runtime PRESENT" in ev and "entitlements = {} " in ev
    # output hash recorded, transcript hash recorded and correct
    outsum = (out / "OUTPUT.sha256").read_text().rstrip("\n").split("  ", 1)
    assert outsum[0] == sha_file(out / f"ADHOC-TEST-ONLY_{DMG_NAME}") and outsum[1] == f"ADHOC-TEST-ONLY_{DMG_NAME}"
    tsum = (out / "TRANSCRIPT.sha256").read_text().split()
    assert tsum[0] == sha_file(out / "TRANSCRIPT.txt") and f"transcript sha256: {tsum[0]}" in r.stdout
    # tidy: no staged app, no work dir, no leftover mounts
    assert not (out / "work").exists() and not (out / "verify" / "app").exists()
    mounts = (mac.state / "mounts").read_text().split()
    detached = (mac.state / "detached").read_text().split()
    assert set(mounts) == set(detached) and len(set(mounts)) == 3
    # the input is untouched
    assert sha_file(mac.dmg) == sha_bytes(b"stub input dmg for the signing sequence")


@pytest.mark.parametrize("what,conf,fix,msg", [
    ("not arm64", dict(LIPO_ARCHS="x86_64 arm64"), None, "not arm64-only"),
    ("entitlement unreadable on the launcher", dict(SABOTAGE_ENT=R0["main_binary_name"]), None, "cannot read the entitlements"),
    ("bundle verify fails", dict(VERIFY_FAIL_SUBSTR=APP_NAME), None, "bundle"),
    ("wrong bundle id in Info.plist", {}, dict(bundle_id="io.redevops.agentic-apps"), "CFBundleIdentifier is not"),
    ("frozen-named app", {}, dict(product_dir_name="Agentic Apps.app"), "not found in the input dmg"),
    ("extra item at the dmg root", {}, dict(extra={"Licenses.txt": "x"}), "dmg would drop"),
    ("extra executable in the bundle", {}, dict(extra={f"{APP_NAME}/Contents/Resources/helper.sh": "#!/bin/sh\n"}), "unexpected executable"),
])
def test_layout_and_verification_failures_fail_closed(mac, what, conf, fix, msg):
    if conf:
        mac.conf(**conf)
    if fix:
        mac.reset_fixture(**fix)
    r = mac.run(mode="--adhoc-test")
    assert r.returncode != 0, what
    assert msg in (r.stdout + r.stderr), (what, r.stdout, r.stderr)
    assert (mac.out / "FAILED.txt").is_file(), "a failed run leaves a FAILED marker and its work directory"
    assert not any((mac.out / n).exists() for n in (f"ADHOC-TEST-ONLY_{DMG_NAME}.signed",))
    mounts = (mac.state / "mounts").read_text().split() if (mac.state / "mounts").exists() else []
    detached = (mac.state / "detached").read_text().split() if (mac.state / "detached").exists() else []
    assert set(mounts) <= set(detached), "the trap detached every mount it made"


def test_macho_magic_in_an_unexpected_file_is_refused(mac):
    app = mac.fixture / APP_NAME / "Contents" / "Resources" / "extra.dat"
    app.write_bytes(bytes.fromhex("cffaedfe") + b"\0" * 16)
    r = mac.run(mode="--adhoc-test")
    assert r.returncode != 0 and "Mach-O file not in the signing plan" in (r.stdout + r.stderr)


# ---- real mode with stubs: authorization binding, notarization sequence, no secret printing, no retry ---------------
def test_stub_tools_on_path_are_ignored_without_the_test_switch(mac):
    """R4-05: PATH is fixed to the system directories; a stub (or hijacked tool) first in the caller's PATH is never used."""
    sc = mac.scope()
    r = mac.real(sc, stubs=False)
    assert mac.calls() == [], "no tool from the caller's PATH was started"
    assert r.returncode != 0
    r = mac.run(mode="--adhoc-test", stubs=False)
    assert mac.calls() == []


_now = datetime.now(timezone.utc)
SCOPE_REFUSALS = [
    ("wrong script hash", dict(script_sha256="1" * 64), "script_sha256"),
    ("wrong common hash", dict(common_sha256="1" * 64), "common_sha256"),
    ("wrong entitlements hash", dict(entitlements_sha256="1" * 64), "entitlements_sha256"),
    ("wrong identity.json hash", dict(identity_json_sha256="1" * 64), "identity_json_sha256"),
    ("wrong input hash", dict(input_dmg_sha256="1" * 64), "input_dmg_sha256"),
    ("wrong identity", dict(identity_sha256="1" * 64), "identity_sha256"),
    ("wrong notary profile", dict(notary_profile_sha256="1" * 64), "notary_profile_sha256"),
    ("wrong gate hash", dict(gate_sha256="1" * 64), "gate_sha256"),
    ("wrong gate tool hash", dict(gate_tool_sha256="1" * 64), "gate_tool_sha256"),
    ("bad team id", dict(team_id="lower"), "team_id"),
    ("bad commit", dict(build_commit="abc"), "40-hex"),
    ("bad manifest digest", dict(evidence_manifest_sha256="zz"), "evidence_manifest_sha256"),
    ("expired window", dict(not_before=iso(_now - timedelta(hours=3)), not_after=iso(_now - timedelta(hours=2))), "outside the authorized window"),
    ("future window", dict(not_before=iso(_now + timedelta(hours=1)), not_after=iso(_now + timedelta(hours=2))), "outside the authorized window"),
    ("window longer than 24 h", dict(not_before=iso(_now - timedelta(hours=1)), not_after=iso(_now + timedelta(hours=25))), "longer than 24 hours"),
    ("old date-only format", dict(not_before="2026-01-01"), "YYYY-MM-DDTHH:MM:SSZ"),
    ("notarize=no but --notarize given", dict(notarize="no", upload_to="none"), "notarize=no"),
    ("distribution disagrees with the flag", dict(distribution="yes"), "distribution=yes"),
    ("unknown key", dict(surprise="1"), "unknown key"),
    ("missing key", dict(team_id=None), "required key 'team_id'"),
    ("old version", dict(scope_version="1"), "scope_version must be 3"),
    ("inconsistent upload", dict(notarize="yes", upload_to="none"), "notarize/upload_to"),
]


@pytest.mark.parametrize("name,over,msg", SCOPE_REFUSALS, ids=[s[0] for s in SCOPE_REFUSALS])
def test_real_mode_scope_file_refusals(mac, name, over, msg):
    sc = mac.scope(**over)
    r = mac.real(sc)
    assert r.returncode == 1 and msg in r.stderr, (r.stdout, r.stderr)
    assert not mac.out.exists() and mac.calls() == []


def test_real_mode_other_refusals(mac):
    sc = mac.scope()
    # the authorization hash must be the scope file's hash
    r = mac.run("--i-have-owner-authorization", "0" * 64, "--scope-file", sc, "--notarize", mode="--real", R0P_CODESIGN_IDENTITY=TEST_IDENTITY, R0P_NOTARY_PROFILE=TEST_PROFILE, R0P_ALLOW_NONSYSTEM_TOOLS="stub-test")
    assert "does not equal" in r.stderr
    # an identity that is not a Developer ID Application identity, a missing notary profile, a missing identity
    assert "must start with" in mac.real(sc, env={"R0P_CODESIGN_IDENTITY": "Some Other Cert"}).stderr
    assert "R0P_NOTARY_PROFILE" in mac.real(sc, env={"R0P_NOTARY_PROFILE": ""}).stderr
    assert "R0P_CODESIGN_IDENTITY" in mac.real(sc, env={"R0P_CODESIGN_IDENTITY": ""}).stderr
    # a duplicate key in the scope file
    dup = mac.tmp / "dup.txt"
    dup.write_text(sc.read_text() + "team_id=TESTTEAM01\n")
    assert "duplicate key" in mac.real(dup).stderr
    # scope says notarize=yes but the operator forgot --notarize
    assert "authorizes notarization" in mac.real(sc, notarize=False).stderr
    assert not mac.out.exists() and mac.calls() == []


def all_text(root: Path) -> str:
    out = []
    for p in root.rglob("*"):
        if p.is_file():
            try:
                out.append(p.read_text(errors="replace"))
            except OSError:
                pass
    return "\n".join(out)


def test_real_mode_full_sequence_with_stubs(mac):
    sc = mac.scope()
    r = mac.real(sc)
    assert r.returncode == 0, (r.stdout, r.stderr)
    cs = mac.calls("codesign")
    signs = [c for c in cs if " -s " in c]
    assert len(signs) == 4 and all(f" -s {TEST_IDENTITY} " in c for c in signs)
    assert all("--timestamp" in c and "--timestamp=none" not in c for c in signs)
    assert "--options runtime" in signs[0] and "--options runtime" in signs[1] and "--options runtime" in signs[2]
    xc = mac.calls("xcrun")
    sub = [c for c in xc if "notarytool submit" in c]
    assert len(sub) == 1, "exactly one submission, never a retry"
    assert f"--keychain-profile {TEST_PROFILE} --wait --timeout 30m --output-format json" in sub[0] and DMG_NAME in sub[0]
    assert any("notarytool log 11111111" in c for c in xc) and any("stapler staple" in c for c in xc)
    st_valid = [c for c in xc if "stapler validate" in c]
    assert len(st_valid) == 2, "validate on the dmg, then on the app copy (expected: no ticket)"
    # spctl on the dmg and the app copy
    assert len(mac.calls("spctl")) == 2
    # order: all signing before the upload, upload before staple
    full = mac.log.read_text()
    assert full.index("notarytool submit") > full.rindex("codesign -s")
    assert full.index("stapler staple") > full.index("notarytool submit")
    out = mac.out
    assert (out / DMG_NAME).is_file() and not (out / f"ADHOC-TEST-ONLY_{DMG_NAME}").exists()
    ev = (out / "EVIDENCE.txt").read_text()
    assert "ONLY the dmg" in ev and "EXPECTED: the app copied out of the dmg has NO stapled ticket" in ev
    assert f"sha256(identity)={sha_bytes(TEST_IDENTITY.encode())}" in ev and "UPLOADED to the Apple notary service" in ev
    # no secret printing: the profile name is nowhere in the out-dir or on the console; the identity string is not in the
    # transcript, the console or the script-written evidence lines (it appears only inside codesign -dvvv's own Authority line)
    blob = all_text(out) + r.stdout + r.stderr
    assert TEST_PROFILE not in blob
    for name in ("TRANSCRIPT.txt",):
        assert TEST_IDENTITY not in (out / name).read_text()
    assert TEST_IDENTITY not in r.stdout + r.stderr
    ev_lines = [l for l in ev.splitlines() if not l.startswith("Authority=")]
    assert TEST_IDENTITY not in "\n".join(ev_lines)
    assert "<identity from R0P_CODESIGN_IDENTITY>" in (out / "TRANSCRIPT.txt").read_text() and "<profile from R0P_NOTARY_PROFILE>" in (out / "TRANSCRIPT.txt").read_text()
    # the environment the tools saw: nothing of the caller's, only the allowlist
    assert not (mac.state / "xcrun-env").exists() or (mac.state / "xcrun-env").read_text() == ""
    assert "NON-SYSTEM TOOLS" in ev, "stub runs are labelled in the evidence"


def test_real_mode_sign_only_has_no_apple_contact(mac):
    sc = mac.scope(notarize="no", upload_to="none", notary_profile_sha256="none")
    r = mac.real(sc, notarize=False)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert mac.calls("xcrun") == []
    assert "NOTHING (signed only" in (mac.out / "EVIDENCE.txt").read_text()


@pytest.mark.parametrize("mode", ["invalid", "timeout"])
def test_real_mode_failed_notarization_is_never_retried_or_stapled(mac, mode):
    mac.conf(NOTARY_MODE=mode)
    sc = mac.scope()
    r = mac.real(sc)
    assert r.returncode != 0
    xc = mac.calls("xcrun")
    assert len([c for c in xc if "notarytool submit" in c]) == 1
    assert not any("stapler" in c for c in xc)
    failed = (mac.out / "FAILED.txt").read_text()
    assert "never reused" in failed and "re-uploaded without the owner" in failed
    assert (mac.out / "work").exists(), "work/ is left for diagnosis"
    if mode == "timeout":
        assert "xcrun notarytool info" in (r.stdout + r.stderr)


def test_real_mode_team_id_and_missing_timestamp_checks(mac):
    mac.conf(TEAM_OVERRIDE="OTHERTEAM1")
    r = mac.real(mac.scope())
    assert r.returncode != 0 and "Team ID differs" in (r.stdout + r.stderr)
    assert not [c for c in mac.calls("xcrun") if "submit" in c], "nothing is uploaded when a pre-notarization check fails"


# ---- the real tools on a tiny arm64 fixture: ad-hoc only ------------------------------------------------------------
def test_adhoc_sequence_with_the_real_tools_on_a_tiny_arm64_app(tmp_path):
    cc = shutil.which("clang")
    if not cc or not shutil.which("hdiutil") or not shutil.which("codesign"):
        pytest.skip("clang/hdiutil/codesign not available")
    home = tmp_path / "home"
    home.mkdir()
    app = tmp_path / "srcapp" / APP_NAME
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources" / "Licenses").mkdir(parents=True)
    for n, code in ((R0["main_binary_name"], 0), (R0["sidecar_binary_name"], 7)):
        src = tmp_path / f"{n}.c"
        src.write_text(f"int main(void){{return {code};}}\n")
        r = subprocess.run([cc, "-arch", "arm64", "-mmacosx-version-min=11.0", "-o", str(app / "Contents" / "MacOS" / n), str(src)], capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip("cannot compile an arm64 fixture: " + r.stderr[:200])
    info = {"CFBundleExecutable": R0["main_binary_name"], "CFBundleIdentifier": R0["bundle_identifier"], "CFBundleName": R0["product_name"],
            "CFBundleShortVersionString": R0["version"], "CFBundleVersion": R0["bundle_version"], "LSMinimumSystemVersion": "11.0",
            "RedevopsReleaseLine": IDENT["release_line"], "CFBundlePackageType": "APPL"}
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    (app / "Contents" / "Resources" / "icon.icns").write_bytes(b"icns")
    for n in ("LICENSE", "LICENSING.md", "THIRD_PARTY_NOTICES.txt"):
        (app / "Contents" / "Resources" / "Licenses" / n).write_text(n + "\n")
    work = tmp_path / "signing"
    work.mkdir()
    fx = SIGN / "tools" / "make_adhoc_fixture.sh"
    r = run(["/bin/bash", fx, app, work / "fx"], clean_env(home))
    assert r.returncode == 0, r.stderr
    dmg = work / "fx" / "input" / DMG_NAME
    out = work / "out"
    r = run(["/bin/bash", MAC, "--adhoc-test", "--input-dmg", dmg, "--expected-sha256", sha_file(dmg), "--out-dir", out], clean_env(home), timeout=600)
    assert r.returncode == 0, (r.stdout[-3000:], r.stderr)
    ev = (out / "EVIDENCE.txt").read_text()
    assert "sidecar: hardened runtime PRESENT" in ev and "launcher: hardened runtime PRESENT" in ev
    side = (out / "codesign" / "sidecar.entitlements.plist").read_bytes()
    assert plistlib.loads(side) == {"com.apple.security.cs.disable-library-validation": True}
    assert (out / "codesign" / "launcher.entitlements.plist").stat().st_size == 0, "the launcher carries no entitlements"
    for part in ("sidecar", "launcher", "bundle"):
        t = (out / "codesign" / f"{part}.dvvv.txt").read_text()
        assert "runtime" in t and "Signature=adhoc" in t
    assert f"Identifier={R0['sidecar_signing_identifier']}" in (out / "codesign" / "sidecar.dvvv.txt").read_text()
    assert f"Identifier={R0['bundle_identifier']}" in (out / "codesign" / "launcher.dvvv.txt").read_text()
    assert (out / f"ADHOC-TEST-ONLY_{DMG_NAME}").is_file()
    assert subprocess.run(["/usr/bin/hdiutil", "info"], capture_output=True, text=True).stdout.count(str(work)) == 0, "no leaked mount"


# ====================================================================================================================
# SSHSIG script
# ====================================================================================================================
import struct
import threading


def bind_unix_socket(path: Path):
    """AF_UNIX paths are short: bind through a relative name from inside the directory (the absolute path may be long)."""
    s = socket.socket(socket.AF_UNIX)
    cwd = os.getcwd()
    os.chdir(path.parent)
    try:
        s.bind(path.name)
    finally:
        os.chdir(cwd)
    return s


class FakeAgent:
    """A minimal ssh-agent of the test's own (a thread on a unix socket; never the user's agent). It answers the identities request with
    the key blobs it is given (or with FAILURE) and ALWAYS refuses to sign: if a signature appears, it did not come from the agent."""

    def __init__(self, sockpath: Path, blobs=(), failure_for_identities=False):
        self.path = sockpath
        self.blobs = list(blobs)
        self.fail_ids = failure_for_identities
        self.requests = []
        self.srv = socket.socket(socket.AF_UNIX)
        cwd = os.getcwd()
        os.chdir(sockpath.parent)               # AF_UNIX paths are short: bind through a relative name
        try:
            self.srv.bind(sockpath.name)
        finally:
            os.chdir(cwd)
        self.srv.listen(8)
        self.srv.settimeout(0.2)
        self.stop = False
        self.th = threading.Thread(target=self.loop, daemon=True)
        self.th.start()

    def loop(self):
        while not self.stop:
            try:
                c, _ = self.srv.accept()
            except (socket.timeout, OSError):
                continue
            try:
                c.settimeout(2)
                while True:
                    hdr = c.recv(4)
                    if len(hdr) < 4:
                        break
                    (n,) = struct.unpack(">I", hdr)
                    body = b""
                    while len(body) < n:
                        chunk = c.recv(n - len(body))
                        if not chunk:
                            break
                        body += chunk
                    typ = body[0]
                    self.requests.append(typ)
                    if typ == 11 and not self.fail_ids:        # REQUEST_IDENTITIES -> IDENTITIES_ANSWER
                        out = bytes([12]) + struct.pack(">I", len(self.blobs))
                        for b in self.blobs:
                            out += struct.pack(">I", len(b)) + b + struct.pack(">I", 4) + b"test"
                    else:                                       # everything else (including SIGN_REQUEST): SSH_AGENT_FAILURE
                        out = bytes([5])
                    c.sendall(struct.pack(">I", len(out)) + out)
            except OSError:
                pass
            finally:
                c.close()

    def close(self):
        self.stop = True
        self.th.join(2)
        self.srv.close()
        try:
            self.path.unlink()
        except OSError:
            pass


_SSH_LIVE: list = []


@pytest.fixture(autouse=True, scope="session")
def _delete_all_throwaway_ssh_keys():
    """Every throwaway key a test made (also in tests that build an Ssh object by hand) is unlinked by name when the session ends."""
    yield
    for s in _SSH_LIVE:
        try:
            s.cleanup()
        except OSError:
            pass


class Ssh:
    def __init__(self, tmp: Path):
        _SSH_LIVE.append(self)
        self.tmp = tmp
        self.home = tmp / "home"
        self.home.mkdir()
        self.ev = tmp / "evidence"
        self.ev.mkdir()
        self.outside = tmp / "outside"
        self.outside.mkdir()
        self.manifest = self.ev / "release-manifest.json"
        self.manifest.write_text('{"files": {"a": "0"}}\n')
        self.keys = tmp / "keys"
        self.keys.mkdir()
        self.tree = make_tree(tmp / "sroot" / "packaging")                     # patched copy for the stub-tool --real tests (see make_tree)
        self.tscript = self.tree / "signing" / SSH.name
        self.tcommon = self.tree / "signing" / COMMON.name
        self.key = self.make_key("k1", "signproc-TEST-KEY")     # a throwaway key for tests that need a key pair of their own
        self.pub = self.key.with_name("k1.pub")

    def make_key(self, name, comment) -> Path:
        k = self.keys / name
        r = run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", k], clean_env(self.home))
        assert r.returncode == 0, r.stderr
        return k

    def fpr(self, pub: Path) -> str:
        return [x for x in run(["/usr/bin/ssh-keygen", "-lf", pub], clean_env(self.home)).stdout.split() if x.startswith("SHA256:")][0]

    def env(self, path_dirs=(), **extra):
        return clean_env(self.home, path_dirs, **extra)

    def digest(self):
        return sha_file(self.manifest)

    def run(self, *args, path_dirs=(), cwd=None, script=None, **extra):
        return run(["/bin/bash", script or SSH, *args], self.env(path_dirs, **extra), cwd=cwd)

    def selftest(self, sig=None, **kw):
        """The script generates its own throwaway key; the public half is exported to outside/test.pub for verify-only."""
        sig = sig or self.outside / "m.sig"
        extra = kw.pop("extra", [])
        return self.run("--self-test", "--manifest", self.manifest, "--expected-sha256", kw.pop("sha", self.digest()), "--sig-out", sig,
                        "--record-out", self.outside / "rec.txt", "--test-pubkey-out", self.outside / "test.pub", *extra, **kw), sig

    def cleanup(self):
        for p in self.keys.iterdir():          # the throwaway keys, deleted by name
            p.unlink()


@pytest.fixture
def ssh(tmp_path):
    s = Ssh(tmp_path)
    yield s
    s.cleanup()


def tree_state(d: Path):
    return sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in d.iterdir())


def test_sshsig_dry_run_default_creates_nothing_and_checks_the_digest(ssh):
    before = tree_state(ssh.ev)
    r = ssh.run("--manifest", ssh.manifest, "--pubkey", ssh.pub, "--expected-sha256", ssh.digest())
    assert r.returncode == 0, r.stderr
    assert "mode: dry-run" in r.stdout and "ssh-keygen -Y sign -U -n redevops-qa-release@v1 -f" in r.stdout and "no agent is contacted" in r.stdout
    assert "ssh-add -l" in r.stdout and "no fallback to a key file" in r.stdout
    assert tree_state(ssh.ev) == before and list(ssh.outside.iterdir()) == []
    r = ssh.run("--manifest", ssh.manifest, "--pubkey", ssh.pub, "--expected-sha256", "0" * 64)
    assert r.returncode == 1 and "not the expected digest" in r.stderr


def test_sshsig_selftest_makes_its_own_key_signs_without_adding_a_file_to_the_evidence_dir_and_verifies(ssh):
    before = tree_state(ssh.ev)
    r, sig = ssh.selftest()
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert sig.read_text().startswith("-----BEGIN SSH SIGNATURE-----")
    assert tree_state(ssh.ev) == before, "the evidence directory is byte-for-byte unchanged (names, sizes, mtimes)"
    assert "THROWAWAY KEY" in r.stdout and "Good \"redevops-qa-release@v1\" signature" in r.stdout
    rec = (ssh.outside / "rec.txt").read_text()
    assert ssh.digest() in rec and "no time" in rec and "SHA256:" in rec
    assert (ssh.outside / "test.pub").read_text().rstrip().endswith("signproc-TEST-KEY")
    v = ssh.run("--verify-only", "--manifest", ssh.manifest, "--sig", sig, "--pubkey", ssh.outside / "test.pub")
    assert v.returncode == 0 and "VERIFIED" in v.stdout
    # the generated key does not survive the run: nothing named key* is left in the system temp area used by the run
    assert not list(ssh.home.rglob("key*"))


def test_sshsig_selftest_cannot_be_handed_a_key_and_refuses_the_release_key_prefix(ssh):
    r, _ = ssh.selftest(extra=["--pubkey", ssh.pub])
    assert r.returncode == 1 and "generates its own throwaway key" in r.stderr
    r = ssh.run("--self-test", "--test-private-key", ssh.key, "--manifest", ssh.manifest, "--expected-sha256", ssh.digest(), "--sig-out", ssh.outside / "x.sig")
    assert r.returncode == 2 and "unknown argument" in r.stderr and not (ssh.outside / "x.sig").exists()
    # R4-08: the published release fingerprint is an exact constant compared by EQUALITY: "SHA256:" + 43 characters = 50
    src = SSH.read_text()
    const = re.search(r'RELEASE_KEY_FPR="(SHA256:[^"]+)"', src).group(1)
    assert const == RELEASE_FPR and len(const) == 50 and re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", const)
    assert "RELEASE_KEY_FPR_PREFIX" not in src and '[ "$FPR" != "$RELEASE_KEY_FPR" ]' in src
    # a stub ssh-keygen that reports a fingerprint with that prefix for the generated key: the self-test must refuse
    d = ssh.tmp / "fakebin"
    d.mkdir()
    write_exec(d / "ssh-keygen", textwrap.dedent(f'''\
        #!/bin/bash
        if [ "$1" = "-lf" ]; then echo "256 {const} signproc-TEST-KEY (ED25519)"; exit 0; fi
        exec /usr/bin/ssh-keygen "$@"
        '''))
    r = ssh.run("--self-test", "--manifest", ssh.manifest, "--expected-sha256", ssh.digest(), "--sig-out", ssh.outside / "y.sig", path_dirs=[d],
                R0P_ALLOW_NONSYSTEM_TOOLS="stub-test")
    assert r.returncode == 1 and "refuses the release key" in r.stderr and not (ssh.outside / "y.sig").exists()
    # a key that is merely similar (one character different) is not the release key: the self-test proceeds
    d2 = ssh.tmp / "fakebin2"
    d2.mkdir()
    write_exec(d2 / "ssh-keygen", (d / "ssh-keygen").read_text().replace(const, const[:-1] + "B"))
    r = ssh.run("--self-test", "--manifest", ssh.manifest, "--expected-sha256", ssh.digest(), "--sig-out", ssh.outside / "z.sig", path_dirs=[d2],
                R0P_ALLOW_NONSYSTEM_TOOLS="stub-test")
    assert "refuses the release key" not in r.stderr


def test_sshsig_failure_cases(ssh):
    r, sig = ssh.selftest()
    assert r.returncode == 0
    tpub = ssh.outside / "test.pub"
    ssh.manifest.write_text('{"files": {"a": "1"}}\n')           # tampered manifest
    v = ssh.run("--verify-only", "--manifest", ssh.manifest, "--sig", sig, "--pubkey", tpub)
    assert v.returncode == 5 and "NOT VERIFIED" in v.stderr
    ssh.manifest.write_text('{"files": {"a": "0"}}\n')
    assert ssh.run("--verify-only", "--manifest", ssh.manifest, "--sig", sig, "--pubkey", tpub).returncode == 0
    # wrong namespace: sign the same bytes directly with ssh-keygen in another namespace
    with open(ssh.manifest, "rb") as f:
        p = subprocess.run(["/usr/bin/ssh-keygen", "-Y", "sign", "-n", "other-namespace@v1", "-f", str(ssh.key)], stdin=f, capture_output=True, env=clean_env(ssh.home))
    assert p.returncode == 0
    bad = ssh.outside / "ns.sig"
    bad.write_bytes(p.stdout)
    assert ssh.run("--verify-only", "--manifest", ssh.manifest, "--sig", bad, "--pubkey", ssh.pub).returncode == 5
    # signature by another key
    k2 = ssh.make_key("k2", "signproc-TEST-KEY")
    with open(ssh.manifest, "rb") as f:
        p = subprocess.run(["/usr/bin/ssh-keygen", "-Y", "sign", "-n", "redevops-qa-release@v1", "-f", str(k2)], stdin=f, capture_output=True, env=clean_env(ssh.home))
    other = ssh.outside / "k2.sig"
    other.write_bytes(p.stdout)
    assert ssh.run("--verify-only", "--manifest", ssh.manifest, "--sig", other, "--pubkey", ssh.pub).returncode == 5
    v = ssh.run("--verify-only", "--manifest", ssh.manifest, "--sig", sig, "--pubkey", tpub, "--expected-fingerprint", "SHA256:" + "A" * 43)
    assert v.returncode == 1 and "not the expected one" in v.stderr


def test_sshsig_refusals(ssh):
    r, sig = ssh.selftest(sha="0" * 64)
    assert r.returncode == 1 and "nothing was signed" in r.stderr and not sig.exists()
    r, sig = ssh.selftest(sig=ssh.ev / "release-manifest.json.sig")
    assert r.returncode == 1 and "must not be added to the evidence set" in r.stderr and not sig.exists()
    (ssh.outside / "m.sig").write_text("x")
    assert "already exists" in ssh.selftest()[0].stderr
    (ssh.outside / "m.sig").unlink()
    r = ssh.run("--manifest", ssh.manifest, "--pubkey", ssh.key, "--expected-sha256", ssh.digest())
    assert r.returncode == 1 and "contains a private key" in r.stderr


def make_fake_tools(ssh: Ssh, real_key: Path) -> Path:
    """STUB ssh-keygen / ssh-add for the positive --real path: they log argv, the environment they got and the files beside the key they
    were given; the stub ssh-keygen signs with a THROWAWAY test key (never an agent, never a release key)."""
    d = ssh.tmp / "fakebin"
    d.mkdir()
    log = ssh.tmp / "tools.log"
    write_exec(d / "ssh-add", textwrap.dedent(f'''\
        #!/bin/bash
        echo "ssh-add $* SOCK=${{SSH_AUTH_SOCK:-unset}} HOME=${{HOME:-unset}}" >> "{log}"
        echo "256 $STUB_FPR comment (ED25519)"
        '''.replace("$STUB_FPR", ssh.fpr(ssh.pub))))
    write_exec(d / "ssh-keygen", textwrap.dedent(f'''\
        #!/bin/bash
        echo "ssh-keygen $*" >> "{log}"
        echo "ENV SOCK=${{SSH_AUTH_SOCK:-unset}} HOME=${{HOME:-unset}} ASKPASS=${{SSH_ASKPASS:-unset}} DISPLAY=${{DISPLAY:-unset}}" >> "{log}"
        if [ "$1" = "-Y" ] && [ "$2" = "sign" ]; then
          orig=("$@"); shift 2; args=(); f=""; while [ $# -gt 0 ]; do case "$1" in -f) f="$2"; shift 2;; -U) shift;; *) args+=("$1"); shift;; esac; done
          case "$f" in */release.pub) ;; *) exec /usr/bin/ssh-keygen "${{orig[@]}}";; esac
          echo "BESIDE_KEY: $(ls "$(dirname "$f")" | tr '\\n' ' ')" >> "{log}"
          exec /usr/bin/ssh-keygen -Y sign -f "{real_key}" "${{args[@]}}"
        fi
        exec /usr/bin/ssh-keygen "$@"
        '''))
    return d


def ssh_scope(ssh: Ssh, tree=False, **over) -> Path:
    now = datetime.now(timezone.utc)
    d = {"scope_version": "2", "not_before": iso(now - timedelta(hours=1)), "not_after": iso(now + timedelta(hours=1)),
         "script_sha256": sha_file(ssh.tscript if tree else SSH), "common_sha256": sha_file(ssh.tcommon if tree else COMMON), "identity_json_sha256": sha_file(IDJSON),
         "manifest_sha256": ssh.digest(), "key_fingerprint": ssh.fpr(ssh.pub), "namespace": "redevops-qa-release@v1",
         "build_commit": "b" * 40, "procedure_commit": "c" * 40}
    d.update(over)
    p = ssh.tmp / f"sc-{len(list(ssh.tmp.glob('sc-*')))}.txt"
    p.write_text("".join(f"{k}={v}\n" for k, v in d.items() if v is not None))
    return p


def real_args(ssh: Ssh, sc: Path, sig: Path, ledger: Path = None, fp=True, manifest=None):
    a = ["--real", "--manifest", manifest or ssh.manifest, "--pubkey", ssh.pub, "--expected-sha256", sha_file(Path(manifest or ssh.manifest)),
         "--i-have-owner-authorization", sha_file(sc), "--scope-file", sc, "--sig-out", sig, "--ledger", ledger or (ssh.tmp / "ledger")]
    if fp:
        a += ["--expected-fingerprint", ssh.fpr(ssh.pub)]
    return a


def test_sshsig_real_never_falls_back_to_a_private_key_file_beside_the_pub(ssh):
    """R4-01 regression (the reviewer's fake-agent reproducer): an agent that does NOT hold the key and a private key file next to
    the .pub. OpenSSH alone would sign from the key FILE; the script must refuse, with the REAL ssh-keygen/ssh-add, and sign nothing."""
    assert (ssh.keys / "k1").is_file() and ssh.pub.exists(), "the private key sits beside the .pub (the dangerous layout)"
    sock = ssh.tmp / "agent.sock"
    sc = ssh_scope(ssh)
    import base64
    k2 = ssh.make_key("k2", "someone-else")
    other_blob = base64.b64decode(k2.with_name("k2.pub").read_text().split()[1])
    # agent answers FAILURE / lists nothing / lists a DIFFERENT key (the fingerprint check)
    for n, (fail_ids, blobs) in enumerate(((True, []), (False, []), (False, [other_blob]))):
        agent = FakeAgent(sock, blobs=blobs, failure_for_identities=fail_ids)
        try:
            sig = ssh.outside / f"fb{n}.sig"
            led = ssh.tmp / f"l{n}"
            # AF_UNIX connect paths are short: the agent is reached through a relative name from the directory that holds it
            r = ssh.run(*real_args(ssh, sc, sig, ledger=led), SSH_AUTH_SOCK=sock.name, cwd=sock.parent)
            assert r.returncode == 1, (r.stdout, r.stderr)
            assert "ssh-add -l could not list" in r.stderr or "does not hold the key" in r.stderr, r.stderr
            if n == 2:
                assert "does not hold the key" in r.stderr
            assert not sig.exists(), "no signature may be made from the key file"
            assert 13 not in agent.requests, "the script did not even ask the agent to sign"
            assert not list((led / "claims").glob("*")) and ledger_text(led) == "", "the scope was not consumed: nothing was claimed or recorded"
        finally:
            agent.close()


def test_sshsig_real_agent_lists_the_key_but_refuses_to_sign_then_no_key_file_is_read(ssh):
    """The agent holds the key (fingerprint listed) but refuses to sign; the private key file is beside the .pub. The script must still not
    sign from the file: with -U and a private copy of the public key there is no file to fall back to."""
    import base64
    blob = base64.b64decode(ssh.pub.read_text().split()[1])
    sock = ssh.tmp / "agent.sock"
    agent = FakeAgent(sock, blobs=[blob])
    try:
        sc = ssh_scope(ssh)
        sig = ssh.outside / "r.sig"
        r = ssh.run(*real_args(ssh, sc, sig), SSH_AUTH_SOCK=sock.name, cwd=sock.parent)
        assert r.returncode == 1 and "agent did not sign" in r.stderr, (r.stdout, r.stderr)
        assert not sig.exists()
        assert 13 in agent.requests, "the agent was asked to sign (and refused)"
        led = ledger_text(ssh.tmp / "ledger")
        assert "event=started" in led and "event=ended" in led and "rc=sign-failed" in led
    finally:
        agent.close()


def test_sshsig_real_positive_path_uses_a_private_copy_of_the_public_key_and_a_quiet_environment(ssh):
    fake = make_fake_tools(ssh, ssh.key)
    sock = ssh.tmp / "agent.sock"
    s = bind_unix_socket(sock)
    try:
        sc = ssh_scope(ssh, tree=True)
        sig = ssh.outside / "real.sig"
        r = ssh.run(*real_args(ssh, sc, sig), path_dirs=[fake], SSH_AUTH_SOCK=str(sock), R0P_ALLOW_NONSYSTEM_TOOLS="stub-test", script=ssh.tscript)
        assert r.returncode == 0, (r.stdout, r.stderr)
        log = (ssh.tmp / "tools.log").read_text()
        sign_line = [l for l in log.splitlines() if l.startswith("ssh-keygen -Y sign")][0]
        assert "-U -n redevops-qa-release@v1 -f" in sign_line
        assert str(ssh.pub) not in sign_line and str(ssh.key) not in sign_line.split(), "the original .pub path (and any sibling) is not used"
        assert "BESIDE_KEY: release.pub" in log and "k1" not in [x for l in log.splitlines() if l.startswith("BESIDE_KEY") for x in l.split()][1:]
        lines = log.splitlines()
        sign_env = lines[lines.index(sign_line) + 1]          # the ENV line the stub wrote for the sign call
        assert f"SOCK={sock}" in sign_env and "ASKPASS=/usr/bin/false" in sign_env and "DISPLAY=unset" in sign_env
        assert str(ssh.home) not in sign_env, "a throwaway HOME, not the user's"
        assert sig.is_file() and "Good" in r.stdout
        led = ledger_text(ssh.tmp / "ledger")
        assert "event=started" in led and "event=ended" in led and "rc=0" in led
        # one-time ledger: the same scope is refused again, --allow-reuse is recorded
        sig2 = ssh.outside / "real2.sig"
        r2 = ssh.run(*real_args(ssh, sc, sig2), path_dirs=[fake], SSH_AUTH_SOCK=str(sock), R0P_ALLOW_NONSYSTEM_TOOLS="stub-test", script=ssh.tscript)
        assert r2.returncode == 1 and "already used" in r2.stderr and not sig2.exists()
        r3 = ssh.run(*real_args(ssh, sc, sig2), "--allow-reuse", path_dirs=[fake], SSH_AUTH_SOCK=str(sock), R0P_ALLOW_NONSYSTEM_TOOLS="stub-test", script=ssh.tscript)
        assert r3.returncode == 0 and "--allow-reuse" in r3.stdout and sig2.exists()
    finally:
        s.close()


def test_sshsig_agent_socket_is_never_passed_outside_real(ssh):
    fake = make_fake_tools(ssh, ssh.key)
    sock = ssh.tmp / "agent.sock"
    s = bind_unix_socket(sock)
    try:
        r, _ = ssh.selftest(path_dirs=[fake], SSH_AUTH_SOCK=str(sock), R0P_ALLOW_NONSYSTEM_TOOLS="stub-test")
        assert r.returncode == 0, r.stderr
        log = (ssh.tmp / "tools.log").read_text() if (ssh.tmp / "tools.log").exists() else ""
        assert str(sock) not in log
    finally:
        s.close()


def test_sshsig_real_mode_refusals(ssh):
    fake = make_fake_tools(ssh, ssh.key)
    sock = ssh.tmp / "agent.sock"
    s = bind_unix_socket(sock)
    try:
        def go(sc, sig="r.sig", manifest=None, fp=True, sock_env=True, ledger=None, extra=()):
            env = dict(R0P_ALLOW_NONSYSTEM_TOOLS="stub-test")
            if sock_env:
                env["SSH_AUTH_SOCK"] = str(sock)
            return ssh.run(*real_args(ssh, sc, ssh.outside / sig, ledger=ledger, fp=fp, manifest=manifest), *extra, path_dirs=[fake], script=ssh.tscript, **env)

        now = datetime.now(timezone.utc)
        assert "ssh-agent" in go(ssh_scope(ssh, tree=True), sock_env=False).stderr
        assert "--expected-fingerprint" in go(ssh_scope(ssh, tree=True), fp=False).stderr
        assert "outside the authorized window" in go(ssh_scope(ssh, tree=True, not_before=iso(now - timedelta(hours=3)), not_after=iso(now - timedelta(hours=2)))).stderr
        assert "longer than 24 hours" in go(ssh_scope(ssh, tree=True, not_after=iso(now + timedelta(hours=30)))).stderr
        assert "script_sha256" in go(ssh_scope(ssh, tree=True, script_sha256="1" * 64)).stderr
        assert "common_sha256" in go(ssh_scope(ssh, tree=True, common_sha256="1" * 64)).stderr
        assert "identity_json_sha256" in go(ssh_scope(ssh, tree=True, identity_json_sha256="1" * 64)).stderr
        assert "manifest_sha256" in go(ssh_scope(ssh, tree=True, manifest_sha256="1" * 64)).stderr
        assert "key_fingerprint" in go(ssh_scope(ssh, tree=True, key_fingerprint="SHA256:" + "B" * 43)).stderr
        assert "namespace" in go(ssh_scope(ssh, tree=True, namespace="other@v1")).stderr
        assert "unknown key" in go(ssh_scope(ssh, tree=True, extra="1")).stderr
        assert "required key" in go(ssh_scope(ssh, tree=True, build_commit=None)).stderr
        other = ssh.ev / "not-the-manifest.json"
        other.write_text("{}\n")
        assert "only the top-level release-manifest.json" in go(ssh_scope(ssh, tree=True, manifest_sha256=sha_file(other)), manifest=other).stderr
        r = ssh.run(*real_args(ssh, ssh_scope(ssh, tree=True), ssh.outside / "e.sig", fp=True)[:-2], "--expected-fingerprint", "SHA256:" + "C" * 43, path_dirs=[fake],
                    SSH_AUTH_SOCK=str(sock), R0P_ALLOW_NONSYSTEM_TOOLS="stub-test", script=ssh.tscript)
        assert "expected one" in r.stderr
        a = real_args(ssh, ssh_scope(ssh, tree=True), ssh.outside / "rl.sig")
        a[a.index("--ledger") + 1] = "relative-ledger.txt"
        assert "absolute" in ssh.run(*a, path_dirs=[fake], SSH_AUTH_SOCK=str(sock), R0P_ALLOW_NONSYSTEM_TOOLS="stub-test", script=ssh.tscript).stderr
        b = real_args(ssh, ssh_scope(ssh, tree=True), ssh.outside / "nl.sig")
        i = b.index("--ledger")
        del b[i:i + 2]
        assert "--ledger DIR" in ssh.run(*b, path_dirs=[fake], SSH_AUTH_SOCK=str(sock), R0P_ALLOW_NONSYSTEM_TOOLS="stub-test", script=ssh.tscript).stderr
        assert not any(p.suffix == ".sig" for p in ssh.outside.iterdir())
        assert not (ssh.tmp / "tools.log").exists() or "-Y sign" not in (ssh.tmp / "tools.log").read_text()
    finally:
        s.close()


def test_scope_file_is_read_once_and_the_copy_is_parsed(ssh):
    """R4-03: the scope file is hashed and parsed from ONE private copy. A `cat` stub (first in PATH under the test switch) swaps the scope
    file for garbage right after the script read it; the run must still be judged on the bytes that were hashed (and so not on the swap)."""
    fake = make_fake_tools(ssh, ssh.key)
    sc = ssh_scope(ssh, tree=True)
    write_exec(fake / "cat", textwrap.dedent(f'''\
        #!/bin/bash
        /bin/cat "$@"; rc=$?
        case "$1" in "{sc}") echo "garbage_after_hash=1" > "{sc}";; esac
        exit $rc
        '''))
    sock = ssh.tmp / "agent.sock"
    s = bind_unix_socket(sock)
    try:
        r = ssh.run(*real_args(ssh, sc, ssh.outside / "once.sig"), path_dirs=[fake], SSH_AUTH_SOCK=str(sock), R0P_ALLOW_NONSYSTEM_TOOLS="stub-test", script=ssh.tscript)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert "garbage" in sc.read_text(), "the swap happened"
    finally:
        s.close()


# ====================================================================================================================
# hand-off helper (Windows build host) and the Windows signing script, with stubs
# ====================================================================================================================
STUB_POWERSHELL = r'''#!/bin/bash
# stand-in for powershell.exe on the build host: prints what Get-AuthenticodeSignature would (configured in stub.conf)
D="$(cd "$(dirname "$0")" && pwd)"; . "$D/stub.conf"
echo "powershell.exe $*" >> "$LOG"
printf '%s|%s|%s|%s\r\n' "${SIG_STATUS:-Valid}" "${SIG_SUBJECT:-CN=Stub Test Org, O=Stub}" "${SIG_ISSUER:-CN=Stub Issuer CA 01}" "${SIG_TS:-True}"
'''


def helper_env(tmp: Path, mode: str, d: Path, requests: Path = None, stubs: Path = None, **env):
    e = {"R0P_HANDOFF_MODE": mode, "R0P_HANDOFF_DIR": str(d)}
    if mode == "apply":
        e.update(R0P_HANDOFF_REQUESTS=str(requests or (d / "REQUESTS.sha256")), R0P_EXPECT_SIGNER_CN="Stub Test Org", R0P_EXPECT_ISSUER_SUBSTR="Stub Issuer CA")
    e.update({k: str(v) for k, v in env.items()})
    (tmp / "h").mkdir(exist_ok=True)
    if stubs:
        e["R0P_POWERSHELL"] = str(stubs / "powershell.exe")     # the helper takes powershell.exe from an absolute path, not from PATH
    return clean_env(tmp / "h", [], **e)


def helper_run(tmp: Path, mode: str, f: Path, d: Path = None, requests: Path = None, stubs: Path = None, **env):
    return run(["/bin/bash", HELPER, f], helper_env(tmp, mode, d, requests, stubs, **env))


def make_ps_stub(tmp: Path, **conf) -> Path:
    s = tmp / "psstub"
    s.mkdir(exist_ok=True)
    (s / "stub.conf").write_text("".join(f"{k}={shlex.quote(str(v))}\n" for k, v in dict(LOG=str(tmp / "ps.log"), **conf).items()))
    write_exec(s / "powershell.exe", STUB_POWERSHELL)
    return s


def test_handoff_helper_collect_and_apply(tmp_path):
    d = tmp_path / "round1"
    d.mkdir()
    ps = make_ps_stub(tmp_path)
    bins = tmp_path / "bins"
    bins.mkdir()
    launcher = bins / "agentic-r0prime.exe"
    launcher.write_bytes(b"MZ unsigned launcher")
    sidecar = bins / "redevops-sidecar-x86_64-pc-windows-msvc.exe"
    sidecar.write_bytes(b"MZ unsigned sidecar")
    unins = bins / "uninstall.exe"
    unins.write_bytes(b"MZ unsigned uninstaller")
    dll = bins / "nsis_tauri_utils.dll"
    dll.write_bytes(b"MZ third party dll")
    other = bins / "mystery.exe"
    other.write_bytes(b"MZ mystery")
    msi = bins / MSI_NAME
    msi.write_bytes(b"msi unsigned")
    before = {p.name: sha_file(p) for p in bins.iterdir()}
    for f in (launcher, sidecar, unins, dll, other, msi):
        r = helper_run(tmp_path, "collect", f, d)
        assert r.returncode == 0, (f, r.stderr)
    assert {p.name: sha_file(p) for p in bins.iterdir()} == before, "collect never modifies a file"
    names = sorted(p.name for p in (d / "requests-inner").iterdir())
    assert names == sorted([f"launcher-{before[launcher.name]}.exe", f"sidecar-{before[sidecar.name]}.exe", f"uninstaller-{before[unins.name]}.exe",
                            f"unknown-{before[other.name]}.exe"]), "an unknown exe is recorded as 'unknown' (the signing script refuses that name); the dll is not requested"
    # the build host's own record of what it asked for
    reqs = (d / "REQUESTS.sha256").read_text().splitlines()
    assert f"{before[launcher.name]}  launcher-{before[launcher.name]}.exe" in reqs and len(reqs) == 4
    assert (d / "requests-container" / MSI_NAME).is_file()
    log = (d / "signcommand.log").read_text()
    assert "skipped (third-party" in log and "role=unknown" in log
    assert helper_run(tmp_path, "collect", launcher, d).returncode == 0
    assert len((d / "REQUESTS.sha256").read_text().splitlines()) == 4, "idempotent"
    msi.write_bytes(b"msi unsigned CHANGED")
    r = helper_run(tmp_path, "collect", msi, d)
    assert r.returncode == 1 and "different bytes" in r.stderr
    # round 2: signed files come back (stand-ins) with the SHA256SUMS of the signing run
    requests = d / "REQUESTS.sha256"
    d2 = tmp_path / "round2"
    (d2 / "signed-inner").mkdir(parents=True)
    sums = []
    for src in (launcher, sidecar, unins):
        req = f"{'launcher' if src is launcher else 'sidecar' if src is sidecar else 'uninstaller'}-{before[src.name]}.exe"
        signed = d2 / "signed-inner" / req
        signed.write_bytes(src.read_bytes() + b"\nSIGNED")
        sums.append(f"{sha_file(signed)}  {req}\n")
    (d2 / "signed-inner" / "SHA256SUMS").write_text("".join(sums))
    for src in (launcher, sidecar, unins):
        r = helper_run(tmp_path, "apply", src, d2, requests=requests, stubs=ps)
        assert r.returncode == 0 and "replaced" in r.stdout, r.stderr
        assert src.read_bytes().endswith(b"\nSIGNED")
        assert "already signed" in helper_run(tmp_path, "apply", src, d2, requests=requests, stubs=ps).stdout
    assert "Get-AuthenticodeSignature" in (tmp_path / "ps.log").read_text()
    assert helper_run(tmp_path, "apply", dll, d2, requests=requests, stubs=ps).returncode == 0 and dll.read_bytes() == b"MZ third party dll"
    r = helper_run(tmp_path, "apply", other, d2, requests=requests, stubs=ps)
    assert r.returncode == 1 and "unknown executable" in r.stderr
    new = bins / "agentic-r0prime.exe"
    new.write_bytes(b"MZ a different build of the launcher")
    r = helper_run(tmp_path, "apply", new, d2, requests=requests, stubs=ps)
    assert r.returncode == 1 and "not in the request list" in r.stderr and new.read_bytes() == b"MZ a different build of the launcher"
    req = f"launcher-{before['agentic-r0prime.exe']}.exe"
    (d2 / "signed-inner" / req).write_bytes(b"tampered")
    unsigned_again = tmp_path / "again" / "agentic-r0prime.exe"
    unsigned_again.parent.mkdir()
    unsigned_again.write_bytes(b"MZ unsigned launcher")
    r = helper_run(tmp_path, "apply", unsigned_again, d2, requests=requests, stubs=ps)
    assert r.returncode == 1 and "does not match its SHA256SUMS" in r.stderr
    (tmp_path / "again" / MSI_NAME).write_bytes(b"rebuilt msi")
    assert helper_run(tmp_path, "apply", tmp_path / "again" / MSI_NAME, d2, requests=requests, stubs=ps).returncode == 0
    assert (d2 / "requests-container" / MSI_NAME).read_bytes() == b"rebuilt msi"
    assert "absolute" in run(["/bin/bash", HELPER, launcher], clean_env(tmp_path, [], R0P_HANDOFF_MODE="collect", R0P_HANDOFF_DIR="rel")).stderr
    assert "collect or apply" in run(["/bin/bash", HELPER, launcher], clean_env(tmp_path, [], R0P_HANDOFF_DIR=str(d))).stderr


def _round(tmp_path, sig_conf=None):
    """collect one launcher on the 'build host', fabricate its signed counterpart, return everything the apply step needs"""
    d1 = tmp_path / "r1"
    d1.mkdir()
    exe = tmp_path / "bh" / "agentic-r0prime.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ launcher")
    unsigned = sha_file(exe)
    assert helper_run(tmp_path, "collect", exe, d1).returncode == 0
    d2 = tmp_path / "r2"
    (d2 / "signed-inner").mkdir(parents=True)
    req = f"launcher-{unsigned}.exe"
    signed = d2 / "signed-inner" / req
    signed.write_bytes(b"MZ launcher\nSIGNED")
    (d2 / "signed-inner" / "SHA256SUMS").write_text(f"{sha_file(signed)}  {req}\n")
    return exe, d1 / "REQUESTS.sha256", d2, make_ps_stub(tmp_path, **(sig_conf or {}))


@pytest.mark.parametrize("conf,msg", [
    (dict(SIG_STATUS="NotSigned"), "not Valid"),
    (dict(SIG_STATUS="HashMismatch"), "not Valid"),
    (dict(SIG_SUBJECT="CN=Somebody Else, O=x"), "signer subject is not CN=Stub Test Org"),
    (dict(SIG_SUBJECT="CN=Stub Test Org Evil, O=x"), "signer subject is not CN=Stub Test Org"),
    (dict(SIG_ISSUER="CN=Other CA"), "issuer does not contain"),
    (dict(SIG_TS="False"), "no timestamp"),
])
def test_handoff_apply_verifies_the_signature_identity_before_accepting(tmp_path, conf, msg):
    """R4-07: a planted file plus a matching SHA256SUMS line is not enough: Status Valid, the signer CN, the issuer text and a timestamp."""
    exe, req, d2, ps = _round(tmp_path, conf)
    r = helper_run(tmp_path, "apply", exe, d2, requests=req, stubs=ps)
    assert r.returncode == 1 and msg in r.stderr, r.stderr
    assert exe.read_bytes() == b"MZ launcher", "nothing was replaced"
    # and with the right identity it is accepted
    ps_ok = make_ps_stub(tmp_path)
    assert helper_run(tmp_path, "apply", exe, d2, requests=req, stubs=ps_ok).returncode == 0


def test_handoff_apply_needs_the_hosts_own_request_list_and_the_expectations(tmp_path):
    exe, req, d2, ps = _round(tmp_path)
    # a signed file planted with a matching SHA256SUMS line that this host never requested (the reviewer's reproducer)
    planted = d2 / "signed-inner" / ("launcher-" + "9" * 64 + ".exe")
    planted.write_bytes(b"random planted file")
    with open(d2 / "signed-inner" / "SHA256SUMS", "a") as f:
        f.write(f"{sha_file(planted)}  {planted.name}\n")
    victim = tmp_path / "bh" / "other" / "agentic-r0prime.exe"
    victim.parent.mkdir()
    victim.write_bytes(b"random planted file")        # hash equals the planted signed file: 'already signed' path
    r = helper_run(tmp_path, "apply", victim, d2, requests=req, stubs=ps)
    assert r.returncode == 1 and "not one of the requests this build host recorded" in r.stderr
    e = helper_env(tmp_path, "apply", d2, req, ps)
    e.pop("R0P_HANDOFF_REQUESTS")
    assert "R0P_HANDOFF_REQUESTS" in run(["/bin/bash", HELPER, exe], e).stderr
    e = helper_env(tmp_path, "apply", d2, req, ps)
    e.pop("R0P_EXPECT_SIGNER_CN")
    assert "R0P_EXPECT_SIGNER_CN" in run(["/bin/bash", HELPER, exe], e).stderr
    assert exe.read_bytes() == b"MZ launcher"


def test_handoff_helper_refuses_file_names_with_control_characters(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    bad = tmp_path / "bad\nname.exe"
    bad.write_bytes(b"MZ")
    r = helper_run(tmp_path, "collect", bad, d)
    assert r.returncode == 1 and "control character" in r.stderr
    assert not (d / "signcommand.log").exists() or "\nname" not in (d / "signcommand.log").read_text()


# ---- Windows signing script with stub java / python / az ------------------------------------------------------------
STUB_JAVA = r'''#!/bin/bash
D="$(cd "$(dirname "$0")" && pwd)"; . "$D/stub.conf"
case "$1" in -version) echo 'openjdk version "stub"' >&2; exit 0;; esac
echo "java $*" >> "$LOG"
env | sed 's/=.*//' | sort >> "$D/java-env-names.txt"
[ "${JAVA_SLEEP:-0}" -gt 0 ] && exec sleep "$JAVA_SLEEP"
[ "${JAVA_FAIL:-0}" = 1 ] && exit 1
for a in "$@"; do
  case "$a" in file:*) tf="${a#file:}"; [ -f "$tf" ] && cp "$tf" "$D/token-seen.txt";; esac
  if [ -f "$a" ]; then case "$a" in *.exe|*.msi) [ "${JAVA_NOSIGN:-0}" = 1 ] || printf '\nSTUB-AUTHENTICODE-SIGNATURE' >> "$a";; esac; fi
done
exit 0
'''

STUB_PY = r'''#!/bin/bash
D="$(cd "$(dirname "$0")" && pwd)"; . "$D/stub.conf"
# python -I - FILE  (signature count) | python -I verify_signature_v2.py ... | python -I -c ...
if [ "$1" = "-I" ] && [ "$2" = "-" ]; then
  cat >/dev/null
  [ -n "${SWAP_DURING_PRECHECK:-}" ] && [ "$3" = "$SWAP_DURING_PRECHECK" ] && printf 'SWAPPED BYTES' > "$3"
  if grep -q STUB-AUTHENTICODE "$3" 2>/dev/null; then echo 1; else echo 0; fi; exit 0
fi
if [ "$1" = "-I" ] && [ "$2" = "-c" ]; then case "$3" in *distributions*) echo "signify==0.0-stub";; *) echo "0.0-stub";; esac; exit 0; fi
if [ "$1" = "-I" ]; then
  echo "python $*" >> "$LOG"
  shift 2
  echo "trust store: stub"
  for f in "$@"; do case "$f" in --*) ;; *) [ -f "$f" ] && echo "===== $f" && echo "checks: {} => PASS";; esac; done
  [ "${VERIFY_FAIL:-0}" = 1 ] && { echo "FAIL: stub verification"; exit 1; }
  exit 0
fi
exit 0
'''

STUB_AZ = r'''#!/bin/bash
D="$(cd "$(dirname "$0")" && pwd)"; . "$D/stub.conf"
echo "az $*" >> "$LOG"
env | sed 's/=.*//' | sort >> "$D/az-env-names.txt"
[ -n "${SWAP_COPY_BEFORE_SIGN:-}" ] && printf 'SWAPPED AFTER COPY' > "$SWAP_COPY_BEFORE_SIGN"
echo "STUBTOKEN-0123456789-SECRET"
exit 0
'''

STUB_CP = r'''#!/bin/bash
# a cp that swaps the SOURCE right before copying it (the window between the hash check and the copy)
D="$(cd "$(dirname "$0")" && pwd)"; . "$D/stub.conf"
if [ -n "${SWAP_SOURCE_AT_CP:-}" ] && [ "${1:-}" = "$SWAP_SOURCE_AT_CP" ]; then printf 'SWAPPED AT COPY' > "$1"; fi
exec /bin/cp "$@"
'''


class Win:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.home.mkdir()
        self.stubs = tmp / "wstubs"
        self.stubs.mkdir()
        self.log = tmp / "wstub.log"
        self.log.write_text("")
        self.conf()
        write_exec(self.stubs / "java", STUB_JAVA)
        write_exec(self.stubs / "az", STUB_AZ)
        write_exec(self.stubs / "py", STUB_PY)
        self.tools = tmp / "tools"
        (self.tools / "jsign").mkdir(parents=True)
        (self.tools / "jsign" / "jsign-7.6.jar").write_bytes(b"stand-in jar")
        # a private copy of the script tree (patched library, see make_tree) with the pinned jar hash replaced by the stand-in's
        tree = make_tree(tmp / "wroot" / "packaging")
        text = (tree / "signing" / WIN.name).read_text()
        pin = "6cf58aa080e7ff82656f320cff8d4a3bff30408df8766806dcba3740dd839fa5"
        assert pin in text
        (tree / "signing" / WIN.name).write_text(text.replace(pin, sha_bytes(b"stand-in jar")))
        (tree / "signing" / WIN.name).chmod(0o755)
        self.script = tree / "signing" / WIN.name
        self.common = tree / "signing" / COMMON.name
        self.tree = tree
        self.lic = licenses_for(tree.parent, tmp / "licwork", platform="windows-x64")     # the Licenses folder of the (stub) installer tree
        self.files = tmp / "files"
        self.files.mkdir()

    def conf(self, **kw):
        vals = {"LOG": str(self.log)}
        vals.update(getattr(self, "_c", {}))
        vals.update({k: str(v) for k, v in kw.items()})
        self._c = vals
        (self.stubs / "stub.conf").write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in vals.items()))

    def inner(self, role="launcher", content=b"MZ unsigned file") -> tuple:
        h = sha_bytes(content)
        p = self.files / f"{role}-{h}.exe"
        p.write_bytes(content)
        return p, h

    def container(self, name=MSI_NAME, content=b"unsigned container") -> tuple:
        p = self.files / name
        p.write_bytes(content)
        return p, sha_bytes(content)

    def env(self, stubs=True, **extra):
        if stubs:
            extra.setdefault("R0P_ALLOW_NONSYSTEM_TOOLS", "stub-test")
        return clean_env(self.home, [self.stubs], **extra)

    def run(self, stage, pairs, *args, outdir=None, stubs=True, **env):
        a = ["/bin/bash", self.script, "--stage", stage]
        for p, h in pairs:
            a += ["--input", p, "--sha256", h]
        a += ["--outdir", outdir or (self.tmp / "wout"), "--tools-dir", self.tools, "--python", self.stubs / "py"] + list(args)
        return run(a, self.env(stubs=stubs, **env))

    @property
    def ledger(self) -> Path:
        return self.tmp / "wledger"

    def scope(self, st, pairs, **over) -> Path:
        now = datetime.now(timezone.utc)
        d = {"scope_version": "3", "not_before": iso(now - timedelta(hours=1)), "not_after": iso(now + timedelta(hours=1)),
             "script_sha256": sha_file(self.script), "common_sha256": sha_file(self.common), "verifier_sha256": sha_file(VERIFIER),
             "identity_json_sha256": sha_file(IDJSON), "stage": st, "inputs_sha256": ",".join(sorted(h for _, h in pairs)),
             "az_endpoint": "https://eus.codesigning.azure.net/", "az_account": "stub-account", "az_profile": "stub-profile", "expected_cn": "CN=Stub Test Org",
             "gate_sha256": sha_file(self.tree / GATE_JSON.name), "gate_tool_sha256": sha_file(GATE_PY), "distribution": "no",
             "evidence_manifest_sha256": "e" * 64, "procedure_commit": "c" * 40,
             "tool_az_sha256": sha_file(self.stubs / "az"), "tool_java_sha256": sha_file(self.stubs / "java"),
             "tool_python_sha256": sha_file(self.stubs / "py"), "tool_python_packages_sha256": sha_bytes(b"signify==0.0-stub\n"), "tool_python_pth_sha256": sha_bytes(b"\n"),
             "tool_osslsigncode_sha256": "none",
             "container_listing_sha256": "none" if st == "inner" else sha_bytes(self.listing_text)}
        d.update(about_pins(self.tree, self.lic))
        d.update(over)
        p = self.tmp / f"wscope-{len(list(self.tmp.glob('wscope-*')))}.txt"
        p.write_text("".join(f"{k}={v}\n" for k, v in d.items() if v is not None))
        return p

    REAL_ENV = {"R0P_AZ_ENDPOINT": "https://eus.codesigning.azure.net/", "R0P_AZ_ACCOUNT": "stub-account", "R0P_AZ_PROFILE": "stub-profile", "R0P_AZ_EXPECTED_CN": "CN=Stub Test Org"}

    @property
    def listing_text(self) -> bytes:
        """What the build host's listing of the installers holds: file names plus the sha256 of Licenses/about.json (RC3)."""
        return LISTING_TEXT + f"{sha_file(self.lic / 'about.json')}  Licenses/about.json\n".encode()

    def listing(self) -> Path:
        p = self.tmp / "container-listing.txt"
        p.write_bytes(self.listing_text)
        return p

    def real(self, stage, pairs, sc, *args, env=None, ledger=True, **kw):
        e = dict(self.REAL_ENV)
        e.update(env or {})
        extra = ["--ledger", self.ledger] if ledger else []
        if stage == "container" and "--container-listing" not in args:
            extra += ["--container-listing", self.listing()]
        if stage == "container" and "--licenses-dir" not in args:
            extra += ["--licenses-dir", self.lic]
        return self.run(stage, pairs, "--real", "--i-have-owner-authorization", sha_file(sc), "--scope-file", sc, *extra, *args, **dict(e, **kw))


LISTING_TEXT = b"Contents of the rebuilt installer:\nagentic-r0prime.exe\nredevops-sidecar.exe\nuninstall.exe\n"


@pytest.fixture
def win(tmp_path):
    return Win(tmp_path)


def test_windows_dry_run_default_prints_the_plan_and_starts_nothing(win):
    p, h = win.inner()
    r = win.run("inner", [(p, h)])
    assert r.returncode == 0, r.stderr
    assert "mode: dry-run" in r.stdout and "NOT executed" in r.stdout and "--storetype TRUSTEDSIGNING" in r.stdout and "--tsaurl http://timestamp.acs.microsoft.com" in r.stdout
    assert "NEXT (inner stage)" in r.stdout
    assert win.log.read_text() == "" and not (win.tmp / "wout").exists()
    assert "az account get-access-token" in r.stdout, "the plan documents the token step but does not run it"


def test_windows_name_and_stage_policy(win):
    f = win.files
    frozen = f / "Agentic Apps_0.1.0_x64_en-US.msi"
    frozen.write_bytes(b"x")
    r = win.run("container", [(frozen, sha_file(frozen))])
    assert r.returncode == 2 and "FROZEN" in r.stderr
    bad = f / "agentic-apps.exe"
    bad.write_bytes(b"x")
    assert "file name not allowed" in win.run("inner", [(bad, sha_file(bad))]).stderr
    msi, hm = win.container()
    assert "does not belong to stage" in win.run("inner", [(msi, hm)]).stderr
    lau, hl = win.inner()
    assert "does not belong to stage" in win.run("container", [(lau, hl)]).stderr
    assert "sha256 mismatch" in win.run("inner", [(lau, "0" * 64)]).stderr
    liar = f / ("launcher-" + "1" * 64 + ".exe")
    liar.write_bytes(b"other bytes")
    assert "not the file's sha256" in win.run("inner", [(liar, sha_file(liar))]).stderr
    assert "--stage must be" in win.run("both", [(lau, hl)]).stderr
    nsis, hn = win.container(NSIS_NAME, b"nsis")
    assert win.run("container", [(msi, hm), (nsis, hn)]).returncode == 0
    assert win.log.read_text() == ""


@pytest.mark.parametrize("var,val,msg", [
    ("R0P_AZ_ENDPOINT", "https://evil.example/", "not an allowed Artifact Signing endpoint"),
    ("R0P_AZ_ENDPOINT", "http://eus.codesigning.azure.net/", "not an allowed"),
    ("R0P_AZ_ENDPOINT", "https://eus.codesigning.azure.net@evil.example/", "not an allowed"),
    ("R0P_AZ_ENDPOINT", "https://eus.codesigning.azure.net:8443/", "not an allowed"),
    ("R0P_AZ_ENDPOINT", "https://a.b.codesigning.azure.net/", "not an allowed"),
    ("R0P_AZ_ACCOUNT", "a", "R0P_AZ_ACCOUNT"), ("R0P_AZ_ACCOUNT", "a/b", "R0P_AZ_ACCOUNT"), ("R0P_AZ_PROFILE", "x" * 64, "R0P_AZ_PROFILE"),
    ("R0P_AZ_EXPECTED_CN", "Stub Org", "CN="), ("R0P_AZ_EXPECTED_CN", "CN=a, O=b", "characters outside"),
])
def test_windows_service_coordinates_are_validated_before_anything(win, var, val, msg):
    p, h = win.inner()
    env = dict(win.REAL_ENV)
    env[var] = val
    r = win.run("inner", [(p, h)], "--real", "--i-have-owner-authorization", "0" * 64, "--scope-file", win.tmp / "none", **env)
    assert r.returncode == 2 and msg in r.stderr
    assert win.log.read_text() == "" and not (win.tmp / "wout").exists()
    r = win.run("inner", [(p, h)], **env)
    assert r.returncode == 2 and msg in r.stderr


_n = datetime.now(timezone.utc)
WIN_SCOPE_REFUSALS = [
    ("script", dict(script_sha256="1" * 64), "script_sha256"), ("common", dict(common_sha256="1" * 64), "common_sha256"),
    ("verifier", dict(verifier_sha256="1" * 64), "verifier_sha256"), ("identity.json", dict(identity_json_sha256="1" * 64), "identity_json_sha256"),
    ("stage", dict(stage="container"), "stage"), ("inputs", dict(inputs_sha256="1" * 64), "inputs_sha256"),
    ("endpoint", dict(az_endpoint="https://weu.codesigning.azure.net/"), "service coordinates"), ("account", dict(az_account="other-acct"), "service coordinates"),
    ("cn", dict(expected_cn="CN=Other"), "expected_cn"),
    ("expired", dict(not_before=iso(_n - timedelta(hours=3)), not_after=iso(_n - timedelta(hours=2))), "outside the authorized window"),
    ("longer than 24 h", dict(not_after=iso(_n + timedelta(hours=30))), "longer than 24 hours"),
    ("gate", dict(gate_sha256="1" * 64), "gate_sha256"), ("gate tool", dict(gate_tool_sha256="1" * 64), "gate_tool_sha256"),
    ("distribution disagrees", dict(distribution="yes"), "distribution=yes"),
    ("bad commit", dict(procedure_commit="abc"), "40-hex"), ("bad manifest", dict(evidence_manifest_sha256="x"), "evidence_manifest_sha256"),
    ("az tool hash", dict(tool_az_sha256="1" * 64), "tool_az_sha256"), ("java tool hash", dict(tool_java_sha256="1" * 64), "tool_java_sha256"),
    ("python tool hash", dict(tool_python_sha256="1" * 64), "tool_python_sha256"), ("python packages hash", dict(tool_python_packages_sha256="1" * 64), "tool_python_packages_sha256"), ("osslsigncode", dict(tool_osslsigncode_sha256="1" * 64), "tool_osslsigncode_sha256"),
    ("listing for inner", dict(container_listing_sha256="1" * 64), "container_listing_sha256"),
    ("unknown", dict(extra="1"), "unknown key"), ("missing", dict(az_profile=None), "required key"), ("old version", dict(scope_version="1"), "scope_version must be 3"),
]


@pytest.mark.parametrize("name,over,msg", WIN_SCOPE_REFUSALS, ids=[w[0] for w in WIN_SCOPE_REFUSALS])
def test_windows_real_scope_refusals_make_no_azure_call(win, name, over, msg):
    p, h = win.inner()
    sc = win.scope("inner", [(p, h)], **over)
    r = win.real("inner", [(p, h)], sc)
    assert r.returncode == 2 and msg in r.stderr, (r.stdout, r.stderr)
    assert "az " not in win.log.read_text() and not (win.tmp / "wout").exists() and ledger_claims(win.ledger) == []


def test_windows_real_requires_authorization_ledger_and_env(win):
    p, h = win.inner()
    r = win.run("inner", [(p, h)], "--real", **win.REAL_ENV)
    assert r.returncode == 2 and "--i-have-owner-authorization" in r.stderr
    sc = win.scope("inner", [(p, h)])
    r = win.run("inner", [(p, h)], "--real", "--i-have-owner-authorization", sha_file(sc), "--scope-file", sc)
    assert r.returncode == 2 and "R0P_AZ_ENDPOINT" in r.stderr
    r = win.real("inner", [(p, h)], sc, ledger=False)
    assert r.returncode == 2 and "--ledger DIR" in r.stderr
    r = win.run("inner", [(p, h)], "--dry-run", "--real", **win.REAL_ENV)
    assert r.returncode == 2 and "at most one" in r.stderr
    r = win.run("inner", [(p, h)], "--i-have-owner-authorization", "0" * 64)
    assert "used only with --real" in r.stderr
    r = win.run("inner", [(p, h)], "--ledger", win.ledger)
    assert "used only with --real" in r.stderr
    assert "az " not in win.log.read_text()


def test_windows_real_path_with_stubs_token_handling_ledger_and_two_stages(win):
    p, h = win.inner("launcher")
    q, hq = win.inner("sidecar", b"MZ sidecar")
    sc = win.scope("inner", [(p, h), (q, hq)])
    r = win.real("inner", [(p, h), (q, hq)], sc)
    assert r.returncode == 0, (r.stdout, r.stderr)
    log = win.log.read_text()
    assert log.count("az account get-access-token --resource https://codesigning.azure.net --query accessToken -o tsv") == 1
    jl = [l for l in log.splitlines() if l.startswith("java ")]
    assert len(jl) == 1, "one Jsign invocation for all files"
    assert "--storetype TRUSTEDSIGNING --keystore https://eus.codesigning.azure.net/ --storepass file:" in jl[0]
    assert "--alias stub-account/stub-profile --alg SHA-256 --tsmode RFC3161 --tsaurl http://timestamp.acs.microsoft.com --tsretries 3 --tsretrywait 10" in jl[0]
    assert "STUBTOKEN" not in log and "SECRET" not in log, "the token is never on a command line"
    assert (win.stubs / "token-seen.txt").read_text().strip() == "STUBTOKEN-0123456789-SECRET"
    out = win.tmp / "wout"
    assert sorted(x.name for x in out.iterdir()) == sorted(["EVIDENCE.txt", "SHA256SUMS", p.name, q.name])
    assert "STUBTOKEN" not in all_text(out) + r.stdout + r.stderr
    ev = (out / "EVIDENCE.txt").read_text()
    assert "stage:           inner" in ev and "role=launcher" in ev and "role=sidecar" in ev and "NOT yet in any installer" in ev
    assert "TRIPWIRE" in ev and "PRIVATE-CANDIDATE" in ev and "tools (sha256)" in ev and "build commit " + about_pins(win.tree, win.lic)["build_commit"] in ev
    assert "/Users/" not in ev and "/private/" not in ev and "/var/folders" not in ev
    assert "PRIVATE-CANDIDATE SIGNING" in r.stdout, "the gate output and the loud statement are printed"
    assert (out / p.name).read_bytes().endswith(b"STUB-AUTHENTICODE-SIGNATURE") and sha_file(p) == h
    names = (win.stubs / "java-env-names.txt").read_text().split()
    assert not [n for n in names if n.startswith(("AZURE", "AWS", "SSH_", "GITHUB", "GH_", "APPROVAL", "R0P_AZ"))]
    az_names = (win.stubs / "az-env-names.txt").read_text().split()
    assert not [n for n in az_names if n.startswith(("AWS", "SSH_", "GITHUB", "GH_", "APPROVAL", "R0P_AZ"))]
    led = ledger_text(win.ledger)
    assert "event=started" in led and "event=ended" in led and "success=1" in led
    assert (out / "SHA256SUMS").read_text().split()[1] == p.name
    # the same scope is refused a second time (one-time ledger), --allow-reuse is explicit
    r2 = win.real("inner", [(p, h), (q, hq)], sc, outdir=win.tmp / "wout-again")
    assert r2.returncode == 2 and "already used" in r2.stderr and not (win.tmp / "wout-again").exists()
    r3 = win.real("inner", [(p, h), (q, hq)], sc, "--allow-reuse", outdir=win.tmp / "wout-again")
    assert r3.returncode == 0 and "--allow-reuse" in r3.stdout
    # stage container: a separate run, own scope, the container listing is required and recorded
    m, hm = win.container()
    scc = win.scope("container", [(m, hm)])
    assert "--container-listing" in win.run("container", [(m, hm)], "--real", "--i-have-owner-authorization", sha_file(scc), "--scope-file", scc,
                                           "--ledger", win.ledger, outdir=win.tmp / "wout-nolist", **win.REAL_ENV).stderr
    r4 = win.real("container", [(m, hm)], scc, outdir=win.tmp / "wout2")
    assert r4.returncode == 0, (r4.stdout, r4.stderr)
    ev2 = (win.tmp / "wout2" / "EVIDENCE.txt").read_text()
    assert "stage:           container" in ev2 and "does NOT inspect the container contents" in ev2 and sha_bytes(win.listing_text) in ev2
    assert "Windows acceptance kit must check the installed files" in ev2


def test_windows_distribution_flag_runs_the_gate_and_refuses_while_decisions_are_open(win):
    p, h = win.inner()
    sc = win.scope("inner", [(p, h)], distribution="yes")
    r = win.real("inner", [(p, h)], sc, "--distribution")
    assert r.returncode == 2 and "REFUSES a distribution signing" in r.stderr and "D-LIC-1" in r.stderr
    assert "az " not in win.log.read_text() and ledger_claims(win.ledger) == []


def test_windows_verification_failure_releases_nothing_and_leaves_a_marker(win):
    win.conf(VERIFY_FAIL=1)
    p, h = win.inner()
    sc = win.scope("inner", [(p, h)])
    r = win.real("inner", [(p, h)], sc)
    assert r.returncode == 4
    out = win.tmp / "wout"
    assert [x.name for x in out.iterdir()] == ["FAILED"] and "FAILED-ATTEMPT" in (out / "FAILED").read_text()
    assert "STUBTOKEN" not in (out / "FAILED").read_text() + r.stdout + r.stderr
    assert not list(out.rglob("token"))
    assert "success=0" in ledger_text(win.ledger) and list((win.ledger / "claims").glob("*")), "a failed run burns the scope: it is claimed in the ledger"


def test_windows_swapped_input_is_detected_after_the_copy(win):
    """R4-02 regression: the input is replaced after its hash was checked (here: while the signature pre-check runs on it); the script
    must refuse and must NOT hand the replaced bytes to Jsign."""
    p, h = win.inner()
    win.conf(SWAP_DURING_PRECHECK=str(p))
    sc = win.scope("inner", [(p, h)])
    r = win.real("inner", [(p, h)], sc)
    assert r.returncode == 2 and "changed between the hash check and the copy" in r.stderr, (r.stdout, r.stderr)
    log = win.log.read_text()
    assert "java " not in log and "az " not in log, "nothing was signed and no token was requested"
    assert [x.name for x in (win.tmp / "wout").iterdir()] == ["FAILED"]


def test_windows_input_swapped_between_hash_and_cp_is_detected(win):
    """R4-02: a `cp` stub swaps the source right before copying; the hash of the COPY is compared with the authorized hash."""
    p, h = win.inner()
    write_exec(win.stubs / "cp", STUB_CP)
    win.conf(SWAP_SOURCE_AT_CP=str(p))
    sc = win.scope("inner", [(p, h)])
    r = win.real("inner", [(p, h)], sc)
    assert r.returncode == 2 and "changed between the hash check and the copy" in r.stderr
    assert "java " not in win.log.read_text()


def test_windows_copy_swapped_after_the_token_request_is_detected_before_jsign(win):
    """R4-02: the file in the private work directory is replaced just before the Jsign call (the az stub does it): re-hashed, refused."""
    p, h = win.inner()
    win.conf(SWAP_COPY_BEFORE_SIGN=str(win.tmp / "wout" / "work" / "signing" / p.name))
    sc = win.scope("inner", [(p, h)])
    r = win.real("inner", [(p, h)], sc)
    assert r.returncode == 2 and "is not the authorized file" in r.stderr, (r.stdout, r.stderr)
    assert "java " not in win.log.read_text(), "Jsign was never started on the swapped bytes"


def test_windows_environment_scrub_cannot_be_skipped_and_path_is_fixed(win):
    p, h = win.inner()
    sc = win.scope("inner", [(p, h)])
    # a pre-set marker variable and ambient credentials: the scrub still happens (the check looks at the environment itself)
    r = win.real("inner", [(p, h)], sc, env=dict(R0P_SCRUBBED="1", AZURE_CLIENT_SECRET="x", AWS_SECRET_ACCESS_KEY="y", GITHUB_TOKEN="z", SSH_AUTH_SOCK="/nonexistent"))
    assert r.returncode == 0, (r.stdout, r.stderr)
    for f in ("java-env-names.txt", "az-env-names.txt"):
        names = (win.stubs / f).read_text().split()
        assert not [n for n in names if n.startswith(("AZURE", "AWS", "SSH_", "GITHUB", "R0P_SCRUBBED", "R0P_AZ"))], (f, names)
    # without the test switch the stubs on the caller's PATH are not used at all
    p2, h2 = win.inner("sidecar", b"MZ other")
    sc2 = win.scope("inner", [(p2, h2)])
    (win.stubs / "java-env-names.txt").write_text("")
    win.log.write_text("")
    r = win.real("inner", [(p2, h2)], sc2, outdir=win.tmp / "wout-nostub", stubs=False)
    assert r.returncode != 0
    assert "java " not in win.log.read_text() and "az " not in win.log.read_text()


def test_windows_url_is_validated(win):
    p, h = win.inner()
    assert "--url must be an https URL" in win.run("inner", [(p, h)], "--url", "http://x/").stderr
    assert "--url must be an https URL" in win.run("inner", [(p, h)], "--url", "https://x/a b").stderr
    assert win.run("inner", [(p, h)], "--url", "https://example.invalid/app").returncode == 0


def test_windows_a_signer_that_signs_nothing_is_refused(win):
    win.conf(JAVA_NOSIGN=1)
    p, h = win.inner()
    r = win.run("inner", [(p, h)], "--throwaway-test", "--throwaway-dir", win.tmp / "tw")
    assert r.returncode == 2 and "throwaway keystore not found" in r.stderr
    tw = win.tmp / "tw"
    tw.mkdir()
    for n in ("throwaway.p12", "ks.pass", "ca.crt"):
        (tw / n).write_text("stub")
    r = win.run("inner", [(p, h)], "--throwaway-test", "--throwaway-dir", tw)
    assert r.returncode == 4 and "byte-identical" in (r.stderr + (win.tmp / "wout" / "FAILED").read_text())
    assert [x.name for x in (win.tmp / "wout").iterdir()] == ["FAILED"]


def test_windows_throwaway_test_mode_makes_no_azure_call(win):
    tw = win.tmp / "tw"
    tw.mkdir()
    for n in ("throwaway.p12", "ks.pass", "ca.crt"):
        (tw / n).write_text("stub")
    p, h = win.inner()
    r = win.run("inner", [(p, h)], "--throwaway-test", "--throwaway-dir", tw)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "az " not in win.log.read_text() and "THROWAWAY" in (win.tmp / "wout" / "EVIDENCE.txt").read_text()
    assert "--tsa-url is for --throwaway-test only" in win.run("inner", [(p, h)], "--tsa-url", "http://127.0.0.1:1", outdir=win.tmp / "x").stderr


def test_windows_watchdog_ends_a_hung_signer(win):
    win.conf(JAVA_SLEEP=30)
    p, h = win.inner()
    sc = win.scope("inner", [(p, h)])
    import time
    t0 = time.time()
    r = win.real("inner", [(p, h)], sc, R0P_SIGN_TIMEOUT_SECONDS="2")
    assert time.time() - t0 < 20
    assert r.returncode == 2 and "timed out after 2s" in r.stderr
    assert [x.name for x in (win.tmp / "wout").iterdir()] == ["FAILED"]


def test_end_to_end_handoff_with_stubs(win, tmp_path):
    """collect on the build host -> sign (stub) on the signing host -> apply on the build host: the signed bytes land in place."""
    d = tmp_path / "ho"
    d.mkdir()
    b = tmp_path / "bh"
    b.mkdir()
    exe = b / "agentic-r0prime.exe"
    exe.write_bytes(b"MZ launcher built on the windows host")
    unsigned = sha_file(exe)
    assert helper_run(tmp_path, "collect", exe, d).returncode == 0
    req = d / "requests-inner" / f"launcher-{unsigned}.exe"
    assert req.is_file()
    r = win.real("inner", [(req, unsigned)], win.scope("inner", [(req, unsigned)]), outdir=tmp_path / "signed-out")
    assert r.returncode == 0, (r.stdout, r.stderr)
    d2 = tmp_path / "ho2"
    (d2 / "signed-inner").mkdir(parents=True)
    shutil.copy(tmp_path / "signed-out" / req.name, d2 / "signed-inner" / req.name)
    shutil.copy(tmp_path / "signed-out" / "SHA256SUMS", d2 / "signed-inner" / "SHA256SUMS")
    ps = make_ps_stub(tmp_path)
    r = helper_run(tmp_path, "apply", exe, d2, requests=d / "REQUESTS.sha256", stubs=ps)
    assert r.returncode == 0 and exe.read_bytes().endswith(b"STUB-AUTHENTICODE-SIGNATURE")


# ====================================================================================================================
# round 4 regression tests for the macOS script (R4-03 .. R4-10)
# ====================================================================================================================
def test_macos_scope_is_read_once_and_the_copy_is_parsed(mac):
    """R4-03: a `cat` stub (first in PATH under the test switch) rewrites the scope file right after the script read it; the run is judged on the
    bytes that were hashed. (Before the fix the script parsed the path again and would have seen the garbage.)"""
    sc = mac.scope()
    write_exec(mac.stubs / "cat", f"#!/bin/bash\n/bin/cat \"$@\"; rc=$?\ncase \"$1\" in \"{sc}\") echo 'garbage_after_hash=1' > \"{sc}\";; esac\nexit $rc\n")
    r = mac.real(sc)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "garbage" in sc.read_text()


def test_macos_environment_must_be_the_allowlist_no_marker_variable_skips_it(mac):
    """R4-05: R0P_SCRUBBED=1 (the old skip marker) plus ambient credentials: the tools still see a clean environment."""
    r = mac.run(mode="--adhoc-test", R0P_SCRUBBED="1", AWS_SECRET_ACCESS_KEY="x", GITHUB_TOKEN="y", AZURE_CLIENT_SECRET="z", SSH_AUTH_SOCK="/nonexistent")
    assert r.returncode == 0, (r.stdout, r.stderr)
    names = (mac.state / "codesign-env").read_text().split()
    assert not [n for n in names if n.startswith(("AWS", "GITHUB", "AZURE", "SSH_", "R0P_SCRUBBED"))], names


def test_macos_identity_and_profile_are_not_exported_to_children(mac):
    """R4-05: after they are read, the names are unset: codesign (and every other child) never inherits them."""
    r = mac.real(mac.scope())
    assert r.returncode == 0, (r.stdout, r.stderr)
    names = (mac.state / "codesign-env").read_text().split()
    assert "R0P_CODESIGN_IDENTITY" not in names and "R0P_NOTARY_PROFILE" not in names, names
    assert (mac.state / "xcrun-env").exists() is False or "R0P" not in (mac.state / "xcrun-env").read_text()


def test_macos_ledger_makes_a_scope_single_use(mac):
    sc = mac.scope()
    r = mac.real(sc)
    assert r.returncode == 0
    led = ledger_text(mac.ledger)
    assert led.count("event=started") == 1 and "event=notary-submit" in led and "event=ended rc=0 finished=1" in led
    sub = (mac.out / "notary" / "SUBMISSIONS.txt").read_text()
    assert "status=submitting" in sub and "id=11111111-2222-3333-4444-555555555555 status=Accepted" in sub and "input_sha256=" in sub
    calls_before = len(mac.calls())
    r2 = mac.real(sc, out=mac.work / "out2")
    assert r2.returncode == 1 and "already used" in r2.stderr and not (mac.work / "out2").exists() and len(mac.calls()) == calls_before
    r3 = mac.real(sc, "--allow-reuse", out=mac.work / "out3")
    assert r3.returncode == 0 and "--allow-reuse" in r3.stdout
    assert ledger_text(mac.ledger).count("event=started") == 2


def test_macos_ledger_is_required_and_must_be_absolute(mac):
    sc = mac.scope()
    r = mac.real(sc, ledger=False)
    assert r.returncode == 1 and "--ledger DIR" in r.stderr
    r = mac.real(sc, "--ledger", "relative.txt", ledger=False)
    assert r.returncode == 1 and "absolute" in r.stderr
    assert mac.calls() == []


def test_macos_failed_run_burns_the_scope(mac):
    mac.conf(NOTARY_MODE="invalid")
    sc = mac.scope()
    assert mac.real(sc).returncode != 0
    assert "event=ended rc=1 finished=0" in ledger_text(mac.ledger)
    assert "already used" in mac.real(sc, out=mac.work / "retry").stderr


def test_macos_distribution_gate_private_statement_and_enforcement(mac):
    r = mac.real(mac.scope())
    assert r.returncode == 0
    assert "PRIVATE-CANDIDATE SIGNING: signing and notarization do NOT imply permission to distribute" in r.stdout
    assert "PRIVATE CANDIDATE build" in r.stdout and "D-LIC-1" in r.stdout, "the open decisions are printed"
    assert "PRIVATE-CANDIDATE signing" in (mac.out / "EVIDENCE.txt").read_text()
    # with --distribution the gate is enforced: open blockers refuse BEFORE anything is signed or uploaded
    n = len(mac.calls())
    sc = mac.scope(distribution="yes")
    # (the dmg was already used by the run above: the artifact claim of the ledger needs the owner's explicit --allow-reuse, so that this test reaches the gate)
    r = mac.real(sc, "--distribution", "--allow-reuse", out=mac.work / "dist")
    assert r.returncode == 1 and "REFUSES a distribution signing" in r.stderr and "D-LIC-1" in r.stderr
    assert not (mac.work / "dist").exists()
    assert len(mac.calls()) == n, "the refused run started no tool at all"


def test_macos_out_dir_with_shell_characters_is_refused_and_nothing_is_executed(mac, tmp_path):
    """R4-06: --out-dir used to reach `bash -c`; a quote broke out. Now such a path is refused and no marker file appears."""
    marker = mac.work / "MK"
    for bad in (f"{mac.work}/x';touch MK;'y", f"{mac.work}/x$(touch MK)", f"{mac.work}/x\nMK", f"{mac.work}/x`touch MK`"):
        r = run(["/bin/bash", MAC, "--adhoc-test", "--input-dmg", mac.dmg, "--expected-sha256", sha_file(mac.dmg), "--out-dir", bad], mac.env(), cwd=mac.work)
        assert r.returncode == 1 and "--out-dir may contain only" in r.stderr, (bad, r.stderr)
    assert not marker.exists() and not (Path.cwd() / "MK").exists()
    link = mac.work / "linkdir"
    (mac.work / "realdir").mkdir()
    os.symlink(mac.work / "realdir", link)
    r = mac.run(mode="--adhoc-test", out=link)
    assert r.returncode == 1 and "must not be a symlink" in r.stderr


def test_macos_hostile_names_inside_the_dmg_do_not_inject(mac):
    """R4-06: file names with spaces, quotes, $(...) and a newline inside the bundle are signed as plain data; nothing is executed."""
    lic = mac.fixture / APP_NAME / "Contents" / "Resources" / "Licenses"
    for name in ("$(touch HOSTILE1).txt", "a\"b'c;touch HOSTILE2.txt", "-rf", "with space.txt", "new\nline.txt"):
        (lic / name).write_text("x\n")
    r = mac.run(mode="--adhoc-test")
    assert r.returncode == 0, (r.stdout, r.stderr)
    for m in ("HOSTILE1", "HOSTILE2"):
        assert not list(mac.work.rglob(m + "*")) and not list(Path(os.getcwd()).glob(m + "*"))
    assert "licence files" in r.stdout or "layout OK" in r.stdout


def test_macos_notary_watchdog_ends_a_hung_notarytool(mac):
    """R4-10: besides notarytool's own --timeout the script bounds the call (timeout + margin)."""
    write_exec(mac.stubs / "xcrun", "#!/bin/bash\necho \"xcrun $*\" >> \"%s\"\nexec sleep 30\n" % mac.log)
    import time
    t0 = time.time()
    r = mac.real(mac.scope(), "--notary-timeout", "1s", "--notary-watchdog-margin", "1")
    assert time.time() - t0 < 25, "the hung notarytool was ended by the watchdog"
    assert r.returncode != 0 and "status is 'unknown'" in (r.stdout + r.stderr)
    assert len([c for c in mac.calls("xcrun") if "notarytool submit" in c]) == 1


def test_macos_outputs_have_the_documented_modes(mac):
    r = mac.run(mode="--adhoc-test")
    assert r.returncode == 0
    out = mac.out
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    for f in (out / "EVIDENCE.txt", out / "TRANSCRIPT.txt", out / f"ADHOC-TEST-ONLY_{DMG_NAME}"):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f


# ====================================================================================================================
# hand-typed literals: every digest and fingerprint in packaging/signing/** has a recorded provenance and is re-verified
# ====================================================================================================================
PROV = json.loads((SIGN / "LITERALS-PROVENANCE.json").read_text())
_TOKEN = re.compile(r"(?<![0-9A-Za-z+/])(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40}|SHA256:[A-Za-z0-9+/]{20,})(?![0-9A-Za-z+/])")
_ABBREV = re.compile(r"(?<![0-9a-f])([0-9a-f]{8})\.\.\.([0-9a-f]{4})?(?![0-9a-z])")


def _signing_texts():
    for p in sorted(SIGN.rglob("*")):
        if p.is_file() and p.name != "LITERALS-PROVENANCE.json" and p.suffix in (".sh", ".md", ".py", ".json", ""):
            yield p, p.read_text(errors="replace")


def test_every_literal_digest_and_fingerprint_has_a_recorded_provenance():
    """SELF-CONSISTENCY ONLY unless R0P_PROVENANCE_SOURCES is set (R5-05): this test proves that every literal is LISTED, and the fingerprint is
    re-derived from the committed public key; the other entries are verified against their sources only by the source-root run / the
    tools/check-literals-against-sources.sh gate, which a signer must run before the first real run."""
    if not os.environ.get("R0P_PROVENANCE_SOURCES"):
        import warnings
        warnings.warn("SELF-CONSISTENCY ONLY: LITERALS-PROVENANCE.json was NOT verified against its sources (set R0P_PROVENANCE_SOURCES / run tools/check-literals-against-sources.sh)", UserWarning)
    allowed = {e["value"].lower() if not e["value"].startswith("SHA256:") else e["value"]: e for e in PROV["entries"]}
    assert len(allowed) == len(PROV["entries"]), "duplicate entries"
    used = set()
    for p, text in _signing_texts():
        for m in _TOKEN.finditer(text):
            tok = m.group(0)
            key = tok if tok.startswith("SHA256:") else tok.lower()
            assert key in allowed, f"{p.relative_to(ROOT)}: literal {tok} has no entry in LITERALS-PROVENANCE.json"
            used.add(key)
        for m in _ABBREV.finditer(text):
            pre, suf = m.group(1), m.group(2) or ""
            hits = [k for k in allowed if k.startswith(pre) and k.endswith(suf)]
            assert hits, f"{p.relative_to(ROOT)}: abbreviated digest {m.group(0)} matches no entry"
            used.update(hits)
    assert used == set(allowed), f"entries never used: {set(allowed) - used}"
    for e in PROV["entries"]:
        assert e["source"] and e["meaning"] and e["check"]
        v = e["value"]
        if v.startswith("SHA256:"):
            assert re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", v) and len(v) == 50
        else:
            assert re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", v)
    # things that must never be typed into these files
    blob = "".join(text for _, text in _signing_texts())
    for secretish in ("Q8A92DM4T7", "A5726E26325013EE238988B8688E07FAA77B532D", "af674c05bd3a5278689e5d27119141c8dc08cf2f445d9e40aebbc44e7ed1a90e"):
        assert secretish not in blob


def test_the_release_fingerprint_constant_is_the_published_one_everywhere():
    for p, text in _signing_texts():
        for m in re.finditer(r"SHA256:[A-Za-z0-9+/]{30,}", text):
            assert m.group(0) == RELEASE_FPR, f"{p.relative_to(ROOT)}: {m.group(0)}"
    assert RELEASE_FPR in {e["value"] for e in PROV["entries"]}
    # derived from the committed PUBLIC key, in every run (R5-05)
    fp = subprocess.run(["/usr/bin/ssh-keygen", "-lf", str(SIGN / "release-signing-key.pub")], capture_output=True, text=True).stdout.split()[1]
    assert fp == RELEASE_FPR


def test_provenance_entries_verify_against_their_sources():
    base = os.environ.get("R0P_PROVENANCE_SOURCES")
    if not base or not Path(base).is_dir():
        pytest.skip("SELF-CONSISTENCY ONLY: set R0P_PROVENANCE_SOURCES to the scratch/evidence root to re-verify the literals against their sources")
    import importlib.util
    spec = importlib.util.spec_from_file_location("_chk", SIGN / "tools" / "check_literals_against_sources.py")
    chk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chk)
    problems = chk.verify(PROV["entries"], Path(base), ROOT)
    assert problems == [], problems
    # the namespace string is the one of the evidence set
    sr = Path(base) / "r0prime/candidate/evidence-macos-arm64-unsigned-final3/SIGNING_REQUEST.md"
    if sr.is_file():
        assert "redevops-qa-release@v1" in sr.read_text() and 'NAMESPACE="redevops-qa-release@v1"' in SSH.read_text()


# ====================================================================================================================
# round 5 regression tests
# ====================================================================================================================
HOSTILE_FUNCS = ("compgen", "declare", "uname", "dirname", "basename", "id", "shasum", "exec_probe")


def hostile_env_items(marker: Path) -> dict:
    """Exported shell functions with the names the reviewer used (and more), plus fake credentials. A function body appends to `marker`."""
    items = {f"BASH_FUNC_{n}%%": f"() {{ echo {n} >> {marker}; }}" for n in HOSTILE_FUNCS}
    items.update(FAKE_AWS_SECRET_ACCESS_KEY="fake-secret", FAKE_GITHUB_TOKEN="fake-token", SSH_AUTH_SOCK="/nonexistent/agent.sock")
    return items


def test_r5_01_exported_functions_cannot_blind_or_run_before_the_scrub(tmp_path):
    for n in "mws":
        (tmp_path / n).mkdir()
    mac, win, ssh = Mac(tmp_path / "m"), Win(tmp_path / "w"), Ssh(tmp_path / "s")
    marker = tmp_path / "HOSTILE.log"
    items = hostile_env_items(marker)
    # macOS: dry run and a full ad-hoc run with stubs (the stub codesign records the environment it receives)
    r = mac.run(**items)
    assert r.returncode == 0, r.stderr
    r = mac.run(mode="--adhoc-test", **items)
    assert r.returncode == 0, (r.stdout, r.stderr)
    names = (mac.state / "codesign-env").read_text().split()
    assert not [n for n in names if n.startswith(("FAKE_", "SSH_", "BASH_FUNC"))], names
    # Windows and SSHSIG dry runs
    p, h = win.inner()
    assert win.run("inner", [(p, h)], **items).returncode == 0
    assert ssh.run("--manifest", ssh.manifest, "--pubkey", ssh.pub, "--expected-sha256", ssh.digest(), **items).returncode == 0
    assert not marker.exists(), "no exported function ran: " + (marker.read_text() if marker.exists() else "")
    # the production (unpatched) scripts as well
    r = run(["/bin/bash", MAC, "--input-dmg", mac.dmg, "--expected-sha256", sha_file(mac.dmg), "--out-dir", mac.work / "o-prod"], {**clean_env(mac.home), **items})
    assert r.returncode == 0 and not marker.exists()


def test_r5_01_the_environment_check_cannot_be_blinded_and_a_preset_marker_is_not_trusted(mac, tmp_path):
    marker = tmp_path / "HOSTILE.log"
    argv = ["--input-dmg", mac.dmg, "--expected-sha256", sha_file(mac.dmg), "--out-dir", mac.out]
    # the privileged copy refuses a dirty environment itself: started with the positional marker in privileged mode but with extra variables
    env = clean_env(mac.home, [mac.stubs], FAKE_TOKEN="x", R0P_ALLOW_NONSYSTEM_TOOLS="stub-test")
    r = run(["/bin/bash", "-p", mac.script, "--r0p-reexec", *argv], env)
    assert r.returncode == 1 and "not the allowlist" in r.stderr and "FAKE_TOKEN" in r.stderr
    # the positional marker without privileged mode is NOT trusted, and a preset R0P_REEXEC/R0P_SCRUBBED means nothing
    env = {**clean_env(mac.home, [mac.stubs], R0P_REEXEC="1", R0P_SCRUBBED="1"), "FAKE_TOKEN": "x", **hostile_env_items(marker)}
    r = run(["/bin/bash", mac.script, "--r0p-reexec", *argv], env)
    assert r.returncode == 2 and "unknown argument: --r0p-reexec" in r.stderr and not marker.exists(), (r.stdout, r.stderr)   # re-executed, the stray marker is an ordinary (unknown) argument
    r = run(["/bin/bash", mac.script, *argv], env)
    assert r.returncode == 0 and not marker.exists(), (r.stdout, r.stderr)
    # an exported function that survives in the environment of the privileged copy is refused
    env = clean_env(mac.home, [])
    env["BASH_FUNC_uname%%"] = "() { echo x >> " + str(marker) + "; }"
    r = run(["/bin/bash", "-p", mac.script, "--r0p-reexec", *argv], env)
    assert r.returncode == 1 and "exported-function" in r.stderr and not marker.exists()
    # a start in privileged mode WITHOUT the marker still re-executes (the old R0P_REEXEC=1 shortcut is gone)
    env = {**clean_env(mac.home, [mac.stubs], R0P_REEXEC="1"), "FAKE_TOKEN": "x"}
    r = run(["/bin/bash", "-p", mac.script, *argv], env)
    assert r.returncode == 0, (r.stdout, r.stderr)


def test_r5_01_direct_execution_uses_privileged_mode_so_bash_env_is_not_read(mac, tmp_path):
    hook = tmp_path / "benv.sh"
    marker = tmp_path / "BASH_ENV.ran"
    hook.write_text(f"echo ran > {marker}\n")
    assert os.access(mac.script, os.X_OK) and mac.script.read_text().startswith("#!/bin/bash -p\n")
    r = run([str(mac.script), "--input-dmg", mac.dmg, "--expected-sha256", sha_file(mac.dmg), "--out-dir", mac.out], {**clean_env(mac.home), "BASH_ENV": str(hook)})
    assert r.returncode == 0 and not marker.exists(), "BASH_ENV was processed"
    for s in (MAC, WIN, SSH):
        assert s.read_text().startswith("#!/bin/bash -p\n")


def test_r5_03_nothing_path_resolved_runs_before_the_scrub(tmp_path):
    for n in "mws":
        (tmp_path / n).mkdir()
    mac, win, ssh = Mac(tmp_path / "m"), Win(tmp_path / "w"), Ssh(tmp_path / "s")
    poison = tmp_path / "poison"
    poison.mkdir()
    log = tmp_path / "poison.log"
    for c in ("dirname", "basename", "uname", "id", "shasum", "cat", "sed", "awk", "tr", "head", "date", "stat", "find", "ls", "wc", "grep", "mktemp", "plutil", "python3"):
        write_exec(poison / c, f"#!/bin/bash\necho {c} >> {log}\nexec /usr/bin/{c} \"$@\"\n")
    base = {"PATH": f"{poison}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(mac.home), "LANG": "C"}
    r = run(["/bin/bash", MAC, "--input-dmg", mac.dmg, "--expected-sha256", sha_file(mac.dmg), "--out-dir", mac.work / "op"], base)
    assert r.returncode == 0, r.stderr
    p, h = win.inner()
    r = run(["/bin/bash", WIN, "--stage", "inner", "--input", p, "--sha256", h, "--outdir", win.tmp / "op"], base)
    assert r.returncode == 0, r.stderr
    r = run(["/bin/bash", SSH, "--manifest", ssh.manifest, "--pubkey", ssh.pub, "--expected-sha256", ssh.digest()], base)
    assert r.returncode == 0, r.stderr
    assert not log.exists(), "PATH-resolved tools ran: " + (log.read_text() if log.exists() else "")
    # the same with the poisoned PATH ahead of the stubs' switch NOT set: ad-hoc mode never reaches them either
    r = run(["/bin/bash", MAC, "--adhoc-test", "--input-dmg", mac.dmg, "--expected-sha256", sha_file(mac.dmg), "--out-dir", mac.work / "oa"], base)
    assert not log.exists()
    # static: the first executable lines resolve nothing through PATH
    for s in (MAC, WIN, SSH):
        head = s.read_text().split("# the directory of the script without dirname/basename", 1)[0]
        code = "\n".join(ln for ln in head.splitlines() if not ln.lstrip().startswith("#"))
        for word in ("dirname", "basename", "command -v", "compgen", "declare", "uname", "$(", "`"):
            assert word not in code, (s.name, word)
        assert "builtin exec" in code and "/usr/bin/env -i" in code
        assert code.lstrip().startswith('R0P_ORIG_PATH="${PATH:-}"\nPATH=/usr/bin:/bin:/usr/sbin:/sbin'), "PATH is replaced by the first statements"
        selfblock = s.read_text().split("# the directory of the script without dirname/basename", 1)[1].split('case "$-" in *x*)', 1)[0]
        for word in ("dirname", "basename"):
            assert word not in selfblock, (s.name, word)


# A fresh process that opens `argv[1]` on each of the given descriptor numbers and then replaces itself with the command that follows. The numbers are free in
# a fresh interpreter. Doing this in the pytest process (os.dup2 onto fixed numbers, then os.close) replaced and closed descriptors it does not own, among them the
# copy of stderr that pytest's faulthandler plugin closes at the end of the session: the run then ended with "Bad file descriptor" and exit status 1 (tests/test_fd_guard.py).
_EXEC_WITH_OPEN_FDS = (
    "import os, sys\n"
    "src = os.open(sys.argv[1], os.O_RDONLY)\n"
    "for n in map(int, sys.argv[2].split(',')):\n"
    "    os.dup2(src, n)\n"
    "    os.set_inheritable(n, True)\n"
    "os.close(src)\n"
    "os.execv(sys.argv[3], sys.argv[3:])\n")
_INHERITED_FDS = (9, 12, 20, 64, 100, 200)


def test_r5_inherited_file_descriptors_are_closed(mac):
    def under_open_fds(cmd):
        return subprocess.run([sys.executable, "-c", _EXEC_WITH_OPEN_FDS, __file__, ",".join(map(str, _INHERITED_FDS)), *[str(x) for x in cmd]],
                              env=mac.env(), capture_output=True, text=True)
    # control: without the script's scrub the descriptors really are open in the command that is started (so the assertion below can fail)
    c = under_open_fds(["/bin/ls", "/dev/fd"])
    assert c.returncode == 0 and set(map(str, _INHERITED_FDS)) <= set(c.stdout.split()), c.stdout + c.stderr
    a = ["/bin/bash", mac.script, "--adhoc-test", "--input-dmg", mac.dmg, "--expected-sha256", sha_file(mac.dmg), "--out-dir", mac.out]
    r = under_open_fds(a)
    assert r.returncode == 0, r.stderr
    seen = set(" ".join((mac.state / "codesign-fds").read_text().split("\n")).split())
    assert not (seen & set(map(str, _INHERITED_FDS))), seen


@pytest.mark.parametrize("value", ["stub-test", "1", "anything"])
def test_r5_02_the_unit_test_switch_is_refused_in_real_mode_by_all_three_scripts(tmp_path, value):
    home = tmp_path / "home"
    home.mkdir()
    poison = tmp_path / "poison"
    poison.mkdir()
    log = tmp_path / "poison.log"
    for c in ("codesign", "xcrun", "hdiutil", "ssh-add", "ssh-keygen", "az", "java", "shasum"):
        write_exec(poison / c, f"#!/bin/bash\necho {c} >> {log}\nexit 3\n")
    env = clean_env(home, [poison], R0P_ALLOW_NONSYSTEM_TOOLS=value, R0P_CODESIGN_IDENTITY=TEST_IDENTITY, R0P_NOTARY_PROFILE=TEST_PROFILE)
    f = tmp_path / "in.dmg"
    f.write_bytes(b"x")
    cases = [
        [MAC, "--real", "--input-dmg", f, "--expected-sha256", sha_file(f), "--out-dir", tmp_path / "o", "--i-have-owner-authorization", "0" * 64, "--scope-file", f, "--notarize"],
        [WIN, "--stage", "inner", "--real", "--input", f, "--sha256", sha_file(f), "--outdir", tmp_path / "w"],
        [SSH, "--real", "--manifest", f, "--pubkey", f, "--expected-sha256", sha_file(f)],
    ]
    for c in cases:
        r = run(["/bin/bash", *c], env)
        assert r.returncode in (1, 2) and "unit-test switch" in r.stderr and "refused in --real" in r.stderr, (c[0].name, r.stderr)
    assert not log.exists(), "a hostile tool ran"
    # the switch still works for the dry run of a non-real mode (that is how the stub tests run)
    r = run(["/bin/bash", MAC, "--input-dmg", f, "--expected-sha256", sha_file(f), "--out-dir", tmp_path / "o2"], clean_env(home, [], R0P_ALLOW_NONSYSTEM_TOOLS=value))
    assert r.returncode == 0
    # the production library carries exactly one patch point and the unit-test copy differs from it only there
    assert COMMON.read_text().count("R0P-TEST-PATCH-POINT") == 1


# ---- R5-04 ledger -------------------------------------------------------------------------------------------------
def lib(tmp: Path, body: str, timeout=60):
    script = 'die() { echo "ERROR: $*" >&2; exit 1; }; set -euo pipefail; . "%s"; %s' % (COMMON, body)
    return run(["/bin/bash", "-p", "-c", script], clean_env(tmp), timeout=timeout)


S64 = "ab" * 32


def test_r5_04_ledger_claim_is_atomic_exactly_one_parallel_run_wins(tmp_path):
    led = tmp_path / "ledger"
    procs = [subprocess.Popen(["/bin/bash", "-p", "-c", 'die() { echo "ERROR: $*" >&2; exit 1; }; set -euo pipefail; . "%s"; r0p_ledger_check "%s" %s 0; r0p_ledger_claim "%s" %s 0 real x' % (COMMON, led, S64, led, S64)],
                              env=clean_env(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(16)]
    outs = []
    for p in procs:
        _, e = p.communicate(timeout=60)
        outs.append((p.returncode, e))
    assert sum(1 for rc, _ in outs if rc == 0) == 1, outs
    assert all("already" in e for rc, e in outs if rc != 0), outs
    assert ledger_text(led).count("event=started") == 1 and ledger_claims(led) == [S64]
    # the chain is intact and the modes are private
    assert lib(tmp_path, f'r0p_ledger_verify "{led}"').returncode == 0
    assert stat.S_IMODE(led.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in (led / "events").glob("*.txt"))


def test_r5_04_a_symlink_planted_at_the_next_event_or_over_the_ledger_is_never_followed(tmp_path):
    led = tmp_path / "ledger"
    assert lib(tmp_path, f'r0p_ledger_claim "{led}" {S64} 0 real x').returncode == 0
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n")
    os.symlink(victim, led / "events" / "000002.txt")
    r = lib(tmp_path, f'r0p_ledger_append "{led}" {S64} real ended rc=0')
    assert r.returncode == 1 and "not a regular file" in r.stderr and victim.read_text() == "precious\n"
    assert not (led / "lock").exists(), "a die inside the locked section must not leave a stale lock"
    (led / "events" / "000002.txt").unlink()
    # the symlink appears AFTER the chain was verified and BEFORE the event file is created (the race): O_EXCL must refuse it
    race = (f'eval "$(declare -f r0p_ledger_verify | sed "1s/r0p_ledger_verify/r0p_ledger_verify_orig/")"; '
            f'r0p_ledger_verify() {{ r0p_ledger_verify_orig "$@"; ln -s "{victim}" "$1/events/$(printf %06d $((R0P_LEDGER_N + 1))).txt"; }}; '
            f'r0p_ledger_append "{led}" {S64} real ended rc=0')
    r = lib(tmp_path, race)
    assert r.returncode == 1 and "already exists or is a symlink" in r.stderr and victim.read_text() == "precious\n"
    assert not (led / "lock").exists()
    (led / "events" / "000002.txt").unlink()
    # the ledger directory itself replaced by a symlink after the early check
    real = tmp_path / "ledger-real"
    led.rename(real)
    os.symlink(real, led)
    r = lib(tmp_path, f'r0p_ledger_append "{led}" {S64} real ended rc=0')
    assert r.returncode == 1 and "symlink" in r.stderr and not (real / "lock").exists()
    r = lib(tmp_path, f'r0p_ledger_check "{led}" {"cd" * 32} 0')
    assert r.returncode == 1 and "symlink" in r.stderr
    assert len(list((real / "events").glob("*.txt"))) == 1


def test_r5_04_truncation_modification_and_gaps_are_detected(tmp_path):
    led = tmp_path / "ledger"
    assert lib(tmp_path, f'r0p_ledger_claim "{led}" {S64} 0 real x; r0p_ledger_append "{led}" {S64} real ended rc=0; r0p_ledger_append "{led}" {S64} real extra a').returncode == 0
    ev = sorted((led / "events").glob("*.txt"))
    assert len(ev) == 3 and lib(tmp_path, f'r0p_ledger_verify "{led}"').returncode == 0
    saved = ev[2].read_text()
    ev[2].unlink()                                           # truncation: the last event is deleted
    r = lib(tmp_path, f'r0p_ledger_verify "{led}"')
    assert r.returncode == 1 and "truncated" in r.stderr
    ev[2].write_text(saved)
    assert lib(tmp_path, f'r0p_ledger_verify "{led}"').returncode == 0
    mid = ev[1].read_text()
    ev[1].write_text(mid.replace("rc=0", "rc=9"))             # a middle event is edited: the chain breaks at the next event
    r = lib(tmp_path, f'r0p_ledger_verify "{led}"')
    assert r.returncode == 1 and "broken" in r.stderr
    ev[1].write_text(mid)
    ev[1].rename(led / "events" / "000009.txt")              # numbering gap
    r = lib(tmp_path, f'r0p_ledger_verify "{led}"')
    assert r.returncode == 1 and "gap" in r.stderr
    (led / "events" / "000009.txt").rename(ev[1])
    (led / "LENGTH").write_text("3 " + "0" * 63 + "1\n")      # the recorded length/hash no longer matches
    assert lib(tmp_path, f'r0p_ledger_verify "{led}"').returncode == 1
    # an old single-file ledger is not accepted
    old = tmp_path / "old-ledger.txt"
    old.write_text("scope=x\n")
    r = lib(tmp_path, f'r0p_ledger_check "{old}" {S64} 0')
    assert r.returncode == 1 and "must be a directory" in r.stderr
    # group-writable directory refused
    os.chmod(led, 0o770)
    r = lib(tmp_path, f'r0p_ledger_check "{led}" {S64} 1')
    assert r.returncode == 1 and "writable by group or others" in r.stderr


def _slow_sign_tools(ssh: Ssh, real_key: Path, delay: float) -> Path:
    d = make_fake_tools(ssh, real_key)
    k = (d / "ssh-keygen").read_text().replace('echo "ssh-keygen $*"', f'case "$1 $2" in "-Y sign") sleep {delay};; esac; echo "ssh-keygen $*"', 1)
    write_exec(d / "ssh-keygen", k)
    return d


def test_r5_04_two_parallel_sshsig_runs_under_one_scope_exactly_one_signs(ssh):
    fake = _slow_sign_tools(ssh, ssh.key, 2.0)
    sock = ssh.tmp / "agent.sock"
    s = bind_unix_socket(sock)
    try:
        sc = ssh_scope(ssh, tree=True)
        env = ssh.env([fake], SSH_AUTH_SOCK=str(sock), R0P_ALLOW_NONSYSTEM_TOOLS="stub-test")
        procs = []
        for i in (1, 2):
            a = real_args(ssh, sc, ssh.outside / f"par{i}.sig")
            procs.append(subprocess.Popen(["/bin/bash", str(ssh.tscript), *[str(x) for x in a]], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        res = [p.communicate(timeout=60) for p in procs]
        rcs = [p.returncode for p in procs]
        errs = [e for _, e in res]
        assert sorted(rcs) == [0, 1], (rcs, errs)
        assert len(list(ssh.outside.glob("par*.sig"))) == 1
        assert any("already claimed" in e or "already used" in e for e in errs)
        assert ledger_text(ssh.tmp / "ledger").count("event=started") == 1
    finally:
        s.close()


def test_r5_04_two_parallel_macos_runs_under_one_scope_exactly_one_signs(mac):
    sc = mac.scope()
    base = mac.env(R0P_CODESIGN_IDENTITY=TEST_IDENTITY, R0P_NOTARY_PROFILE=TEST_PROFILE)
    procs = []
    for i in (1, 2):
        a = ["/bin/bash", mac.script, "--real", "--notarize", "--i-have-owner-authorization", sha_file(sc), "--scope-file", sc, "--ledger", mac.ledger,
             "--input-dmg", mac.dmg, "--expected-sha256", sha_file(mac.dmg), "--out-dir", mac.work / f"par{i}"]
        procs.append(subprocess.Popen([str(x) for x in a], env=base, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    res = [p.communicate(timeout=120) for p in procs]
    rcs = [p.returncode for p in procs]
    errs = [e for _, e in res]
    assert sorted(rcs) == [0, 1], (rcs, errs)
    assert ledger_text(mac.ledger).count("event=started") == 1
    assert len([c for c in mac.calls("xcrun") if "notarytool submit" in c]) == 1


# ---- R5-05 literal provenance gate ------------------------------------------------------------------------------------
CHECKER = SIGN / "tools" / "check-literals-against-sources.sh"


def test_r5_05_the_source_gate_fails_when_a_source_is_missing_or_a_literal_is_wrong(tmp_path):
    empty = tmp_path / "no-sources"
    empty.mkdir()
    r = run(["/bin/bash", CHECKER, empty], clean_env(tmp_path))
    assert r.returncode == 1 and "not found under the source root" in r.stderr, "a missing source is a FAILURE, not a skip"
    assert run(["/bin/bash", CHECKER], clean_env(tmp_path)).returncode == 2
    assert run(["/bin/bash", CHECKER, tmp_path / "nonexistent"], clean_env(tmp_path)).returncode == 2
    base = os.environ.get("R0P_PROVENANCE_SOURCES")
    if base:
        r = run(["/bin/bash", CHECKER, base], clean_env(tmp_path))
        assert r.returncode == 0 and "0 problems" in r.stdout, (r.stdout, r.stderr)
        # a wrong literal in the allowlist is caught by the gate (the default test cannot see it)
        bad = json.loads((SIGN / "LITERALS-PROVENANCE.json").read_text())
        bad["entries"][1]["value"] = "6cf58aa080e7ff82656f320cff8d4a3bff30408df8766806dcba3740dd839fa6"
        pbad = tmp_path / "prov-bad.json"
        pbad.write_text(json.dumps(bad))
        r = run(["/usr/bin/python3", "-I", SIGN / "tools" / "check_literals_against_sources.py", base, "--provenance", pbad], clean_env(tmp_path))
        assert r.returncode == 1 and "PROBLEM" in r.stderr
        # a wrong fingerprint is caught even without sources: the committed public key is the authority
        bad = json.loads((SIGN / "LITERALS-PROVENANCE.json").read_text())
        bad["entries"][0]["value"] = RELEASE_FPR[:-1] + "B"
        pbad.write_text(json.dumps(bad))
        r = run(["/usr/bin/python3", "-I", SIGN / "tools" / "check_literals_against_sources.py", base, "--provenance", pbad], clean_env(tmp_path))
        assert r.returncode == 1 and "ssh-keygen -lf" in r.stderr
    assert "check-literals-against-sources.sh" in (SIGN / "SIGNING-PROCEDURE-R0PRIME.md").read_text()


def test_r5_05_the_default_provenance_test_announces_that_it_is_self_consistency_only():
    src = Path(__file__).read_text()
    assert "SELF-CONSISTENCY ONLY" in src
    pub = (SIGN / "release-signing-key.pub").read_text()
    assert pub.startswith("ssh-ed25519 ") and "PRIVATE" not in pub and pub.count("\n") == 1


# ---- R5-06 / INFO -----------------------------------------------------------------------------------------------------
def test_r5_06_windows_evidence_states_what_the_tool_pins_cover(win):
    p, h = win.inner()
    sc = win.scope("inner", [(p, h)])
    r = win.real("inner", [(p, h)], sc)
    assert r.returncode == 0, (r.stdout, r.stderr)
    ev = (win.tmp / "wout" / "EVIDENCE.txt").read_text()
    assert "python packages (names+versions) " + sha_bytes(b"signify==0.0-stub\n") in ev
    assert "NOT hashed (residual)" in ev
    doc = (SIGN / "SIGNING-PROCEDURE-R0PRIME.md").read_text()
    assert "Tool pins cover executables only" in doc and "argv of `codesign`" in doc


def test_info_the_helper_takes_powershell_from_an_absolute_path_not_from_path(tmp_path):
    exe, req, d2, ps = _round(tmp_path)
    e = helper_env(tmp_path, "apply", d2, req, ps)
    e.pop("R0P_POWERSHELL")
    e["PATH"] = f"{ps}:/usr/bin:/bin"            # a powershell.exe on PATH must not be used
    r = run(["/bin/bash", HELPER, exe], e)
    assert r.returncode == 1 and "cannot locate powershell.exe" in r.stderr
    assert exe.read_bytes() == b"MZ launcher"


# ====================================================================================================================
# round 6 regression tests
# ====================================================================================================================
def _poison_dir(tmp_path: Path, log: Path, alt: Path = None) -> Path:
    poison = tmp_path / "poison6"
    poison.mkdir()
    names = ("dirname", "basename", "uname", "id", "shasum", "cat", "sed", "awk", "tr", "head", "date", "stat", "find", "ls", "wc", "grep", "mktemp", "plutil", "python3", "cygpath", "sort")
    for c in names:
        body = f"#!/bin/bash\necho {c} \"$SSH_AUTH_SOCK\" >> {log}\n"
        if c == "dirname" and alt is not None:
            body += f"echo {alt}; exit 0\n"                         # answers another directory: the script must never source a library from there
        else:
            body += f"exec /usr/bin/{c} \"$@\"\n"
        write_exec(poison / c, body)
    return poison


@pytest.mark.parametrize("start", ["direct", "bash-p", "bash", "sh"])
def test_r6_01_a_preset_reexec_marker_and_a_hostile_path_run_nothing_before_the_scrub(tmp_path, start):
    for n in "mws":
        (tmp_path / n).mkdir()
    mac, win, ssh = Mac(tmp_path / "m"), Win(tmp_path / "w"), Ssh(tmp_path / "s")
    log = tmp_path / "poison.log"
    alt = tmp_path / "alt"
    alt.mkdir()
    (alt / "signing-common.sh").write_text(f'echo ATTACKER-LIBRARY >> {tmp_path}/attacker.log\n')
    poison = _poison_dir(tmp_path, log, alt)
    p, h = win.inner()
    cases = [
        (MAC, ["--input-dmg", mac.dmg, "--expected-sha256", sha_file(mac.dmg), "--out-dir", mac.work / "o"]),
        (WIN, ["--stage", "inner", "--input", p, "--sha256", h, "--outdir", win.tmp / "o"]),
        (SSH, ["--manifest", ssh.manifest, "--pubkey", ssh.pub, "--expected-sha256", ssh.digest()]),
    ]
    prefix = {"direct": [], "bash-p": ["/bin/bash", "-p"], "bash": ["/bin/bash"], "sh": ["/bin/sh"]}[start]
    for script, args in cases:
        for extra in ({}, {"R0P_REEXEC": "1"}, {"R0P_REEXEC": "1", "R0P_SCRUBBED": "1", "R0P_STARTED_OK": "1"}):
            env = {"PATH": f"{poison}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(mac.home), "LANG": "C", "SSH_AUTH_SOCK": "/nonexistent/sock", **extra}
            r = run([*(str(x) for x in prefix), str(script), *[str(a) for a in args]], env)
            assert r.returncode == 0, (start, script.name, extra, r.stderr)
            assert not log.exists(), (start, script.name, extra, "PATH-resolved tools ran: " + (log.read_text() if log.exists() else ""))
            assert not (tmp_path / "attacker.log").exists(), "an attacker library was sourced"
            for d in ("o",):
                pass
            # each run needs a fresh output directory
            for o in (mac.work / "o", win.tmp / "o"):
                if o.exists():
                    for f in sorted(o.rglob("*"), key=lambda q: -len(q.parts)):
                        f.unlink() if (f.is_file() or f.is_symlink()) else f.rmdir()
                    o.rmdir()


def test_r6_01_a_privileged_start_that_skips_the_reexec_still_runs_nothing_hostile(tmp_path):
    for n in "mws":
        (tmp_path / n).mkdir()
    mac, win, ssh = Mac(tmp_path / "m"), Win(tmp_path / "w"), Ssh(tmp_path / "s")
    log = tmp_path / "poison.log"
    alt = tmp_path / "alt"
    alt.mkdir()
    (alt / "signing-common.sh").write_text(f'echo ATTACKER-LIBRARY >> {tmp_path}/attacker.log\n')
    poison = _poison_dir(tmp_path, log, alt)
    p, h = win.inner()
    for script, args in ((MAC, [mac.dmg, sha_file(mac.dmg), mac.work / "o"]), (WIN, [p, h, win.tmp / "o"]), (SSH, [ssh.manifest, ssh.pub, ssh.digest()])):
        a = {MAC: ["--input-dmg", args[0], "--expected-sha256", args[1], "--out-dir", args[2]],
             WIN: ["--stage", "inner", "--input", args[0], "--sha256", args[1], "--outdir", args[2]],
             SSH: ["--manifest", args[0], "--pubkey", args[1], "--expected-sha256", args[2]]}[script]
        # the attacker starts the script in privileged mode with the positional marker, a preset R0P_REEXEC and a dirty environment
        env = {"PATH": f"{poison}:/usr/bin:/bin", "HOME": str(mac.home), "LANG": "C", "R0P_REEXEC": "1", "FAKE_SECRET": "x", "SSH_AUTH_SOCK": "/nonexistent/s"}
        r = run(["/bin/bash", "-p", str(script), "--r0p-reexec", *[str(x) for x in a]], env)
        assert r.returncode == 1 and "not the allowlist" in r.stderr, (script.name, r.stderr)
        assert not log.exists() and not (tmp_path / "attacker.log").exists(), (script.name, "hostile code ran before the refusal")


def test_r6_01_non_privileged_starts_with_bash_env_are_refused_loudly_and_direct_start_is_clean(tmp_path):
    (tmp_path / "m").mkdir()
    mac = Mac(tmp_path / "m")
    hook = tmp_path / "benv.sh"
    marker = tmp_path / "ran"
    hook.write_text(f"echo x >> {marker}\n")
    argv = ["--input-dmg", mac.dmg, "--expected-sha256", sha_file(mac.dmg), "--out-dir", mac.out]
    r = run(["/bin/bash", MAC, *argv], {**clean_env(mac.home), "BASH_ENV": str(hook)})
    assert r.returncode == 1 and "not started in privileged mode" in r.stderr and "./script" in r.stderr
    # the hook ran in the first shell (nothing in the script can prevent that): the doc says so; the direct start never reads it
    marker.unlink(missing_ok=True)
    r = run([str(MAC), *argv], {**clean_env(mac.home), "BASH_ENV": str(hook)})
    assert r.returncode == 0 and not marker.exists()
    doc = (SIGN / "SIGNING-PROCEDURE-R0PRIME.md").read_text()
    for needle in ("`./script`", "`bash -p script`", "`bash script`", "`sh script`", "SHELLOPTS", "PS4", "`builtin`"):
        assert needle in doc, needle


def test_r6_01_help_does_not_print_the_first_block_comments():
    for s in (MAC, WIN, SSH):
        r = run([str(s), "--help"], clean_env(Path("/tmp")))
        assert r.returncode in (0, 2) and "Usage:" in (r.stdout + r.stderr)
        assert "FIRST EXECUTABLE" not in r.stdout + r.stderr and "R0P-TEST-PATCH" not in r.stdout + r.stderr


def test_r6_02_a_removed_claim_directory_is_detected_from_the_chain(tmp_path):
    led = tmp_path / "ledger"
    assert lib(tmp_path, f'r0p_ledger_claim "{led}" {S64} 0 real x; r0p_ledger_claim "{led}" {"cd" * 32} 0 real y').returncode == 0
    # a plain replay is refused as before
    assert "already used" in lib(tmp_path, f'r0p_ledger_check "{led}" {S64} 0').stderr
    (led / "claims" / S64).rmdir()                      # T7: the cheapest tamper
    for allow in (0, 1):
        r = lib(tmp_path, f'r0p_ledger_check "{led}" {S64} {allow}')
        assert r.returncode == 1 and "claim is missing" in r.stderr and "tampered" in r.stderr, (allow, r.stderr)
        r = lib(tmp_path, f'r0p_ledger_claim "{led}" {S64} {allow} real x')
        assert r.returncode == 1 and "claim is missing" in r.stderr
    assert not (led / "claims" / S64).exists() and ledger_text(led).count("event=started") == 2
    assert not (led / "lock").exists()
    # the other scope is unaffected
    assert lib(tmp_path, f'r0p_ledger_check "{led}" {"ef" * 32} 0').returncode == 0


def test_r6_04_the_shipped_helper_has_no_powershell_override(tmp_path):
    exe, req, d2, ps = _round(tmp_path)
    e = helper_env(tmp_path, "apply", d2, req, ps)               # sets R0P_POWERSHELL to a stub that would print Valid|...
    r = run(["/bin/bash", HELPER_PROD, exe], e)
    assert r.returncode == 1 and "cannot locate powershell.exe" in r.stderr and exe.read_bytes() == b"MZ launcher"
    e["SYSTEMROOT"] = str(ps)                                     # SYSTEMROOT is not consulted either
    r = run(["/bin/bash", HELPER_PROD, exe], e)
    assert r.returncode == 1 and "cannot locate powershell.exe" in r.stderr
    src = HELPER_PROD.read_text()
    assert "R0P_POWERSHELL" not in "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#")) and "SYSTEMROOT" not in "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))


@pytest.mark.parametrize("extra", [{}, {"R0P_REEXEC": "1"}])
def test_r6_05_the_test_switch_with_real_runs_nothing_from_the_inherited_path(tmp_path, extra):
    log = tmp_path / "poison.log"
    poison = _poison_dir(tmp_path, log)
    f = tmp_path / "in.dmg"
    f.write_bytes(b"x")
    env = {"PATH": f"{poison}:/usr/bin:/bin", "HOME": str(tmp_path), "LANG": "C", "R0P_ALLOW_NONSYSTEM_TOOLS": "stub-test", "SSH_AUTH_SOCK": "/nonexistent/s", **extra}
    for c in ([MAC, "--real", "--input-dmg", f, "--expected-sha256", sha_file(f), "--out-dir", tmp_path / "o"],
              [WIN, "--stage", "inner", "--real", "--input", f, "--sha256", sha_file(f), "--outdir", tmp_path / "w"],
              [SSH, "--real", "--manifest", f, "--pubkey", f, "--expected-sha256", sha_file(f)]):
        r = run(["/bin/bash", *[str(x) for x in c]], env)
        assert r.returncode == 1 and "unit-test switch" in r.stderr and "[first block]" in r.stderr, (c[0].name, r.stderr)
    assert not log.exists(), "a hostile tool from the inherited PATH ran: " + (log.read_text() if log.exists() else "")


def test_r6_03_python_start_up_hooks_in_the_environment_are_refused_and_pth_files_are_bound(win):
    p, h = win.inner()
    sc = win.scope("inner", [(p, h)])
    site = win.tmp / "lib" / "python3.12" / "site-packages"           # the venv root is the parent of the directory that holds the python (wstubs)
    site.mkdir(parents=True)
    (site / "sitecustomize.py").write_text("print('hook')\n")
    r = win.real("inner", [(p, h)], sc)
    assert r.returncode == 2 and "start-up hook" in r.stderr and "az " not in win.log.read_text()
    (site / "sitecustomize.py").unlink()
    (site / "zz.pth").write_text("import os\n")
    r = win.real("inner", [(p, h)], sc, outdir=win.tmp / "w2")
    assert r.returncode == 2 and "tool_python_pth_sha256" in r.stderr
    pth_sha = sha_bytes(f"lib/python3.12/site-packages/zz.pth {sha_file(site / 'zz.pth')}\n".encode())
    sc2 = win.scope("inner", [(p, h)], tool_python_pth_sha256=pth_sha)
    r = win.real("inner", [(p, h)], sc2, outdir=win.tmp / "w3")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert ".pth files " + pth_sha in (win.tmp / "w3" / "EVIDENCE.txt").read_text()


def test_r6_documentation_states_the_residuals(tmp_path):
    doc = (SIGN / "SIGNING-PROCEDURE-R0PRIME.md").read_text()
    for needle in ("rmdir <ledger>/lock", "ended", "contention", "sitecustomize", ".pth", "names and versions", "release-signing-key.pub", "not read by the signing scripts",
                   "G12", "procedural", "fresh venv", "-S"):
        assert needle in doc, needle
    assert "ledger.txt" not in doc and "--ledger \"$HOME/r0prime-signing-ledger\"" in doc
    tpl = (SIGN / "AUTHORIZATION-TEMPLATE.md").read_text()
    assert "tool_python_pth_sha256" in tpl and "fresh" in tpl


# ====================================================================================================================
# RC3 review fix RC3-04: the gate call passes --about-slots and the About record inside the artifact is checked
# (packaging/signing/** was edited on the RC3 branch only: a new independent review round is needed before any RC3 signing)
# ====================================================================================================================
def _decide_everything(root: Path, signer) -> None:
    ids = sorted({d["id"] for d in read_gate(root)["decisions"]})
    decide(root, ids)
    trust(root, signer)


def test_rc3_04_both_gate_calls_pass_about_slots_and_the_about_check_exists():
    code = code_of(COMMON)
    assert code.count("--about-slots") >= 2 and "r0p_about_check()" in code and "check_about.py" in code
    assert "r0p_about_check" in code_of(MAC) and "r0p_about_check" in code_of(WIN)
    assert "--licenses-dir" in WIN.read_text()


def test_rc3_04_the_private_run_prints_the_about_slot_state(mac):
    r = mac.real(mac.scope())
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "about slots: 5 of 5 are placeholders" in r.stdout


def test_rc3_04_macos_real_refuses_a_dmg_without_the_about_record(mac):
    (mac.fixture / APP_NAME / "Contents" / "Resources" / "Licenses" / "about.json").unlink()
    r = mac.real(mac.scope())
    assert r.returncode != 0 and "no Licenses/about.json" in r.stderr
    assert not mac.calls("codesign"), "nothing is signed when the record check fails"


def test_rc3_04_macos_real_refuses_a_tampered_about_record(mac):
    p = mac.fixture / APP_NAME / "Contents" / "Resources" / "Licenses" / "about.json"
    doc = json.loads(p.read_text())
    doc["product"]["version"] = "9.9.9"
    p.write_text(json.dumps(doc))
    r = mac.real(mac.scope())
    assert r.returncode != 0 and "failed check_about.py" in r.stderr and not mac.calls("codesign")


def test_rc3_04_macos_real_refuses_a_record_that_disagrees_with_the_manifest_in_the_dmg(mac):
    (mac.fixture / APP_NAME / "Contents" / "Resources" / "Licenses" / "LICENSE").write_text("altered after staging\n")
    r = mac.real(mac.scope())
    assert r.returncode != 0 and "failed check_about.py" in r.stderr and not mac.calls("codesign")


def test_rc3_04_macos_adhoc_test_mode_does_not_need_the_record(mac):
    (mac.fixture / APP_NAME / "Contents" / "Resources" / "Licenses" / "about.json").unlink()
    r = mac.run(mode="--adhoc-test")
    assert r.returncode == 0, (r.stdout, r.stderr)


def test_rc3_04_macos_distribution_signing_needs_a_distribution_record_and_valid_signatures(mac, signer):
    root = mac.tree.parent
    _decide_everything(root, signer)
    gj = mac.tree / "distribution-gate.json"
    # the dmg still carries the PRIVATE-candidate record built before the decisions: refused after the gate itself passed
    r = mac.real(mac.scope(distribution="yes", gate_sha256=sha_file(gj)), "--distribution")
    assert r.returncode != 0 and "distribution gate: passed" in r.stdout and "failed check_about.py" in r.stderr
    assert "private-candidate record" in r.stderr and not mac.calls("codesign")
    # a distribution record generated from the signed decisions passes the whole run
    mac.lic = licenses_for(root, mac.tmp / "licwork-dist", distribution=True)
    mac.reset_fixture()
    r = mac.real(mac.scope(distribution="yes", gate_sha256=sha_file(gj)), "--distribution", out=mac.work / "out2")
    assert r.returncode == 0, (r.stdout[-1500:], r.stderr[-1500:])
    assert "distribution gate: passed" in r.stdout and mac.calls("codesign")


def test_rc3_04_macos_distribution_gate_refuses_a_forged_but_unsigned_decision_set(mac, signer):
    root = mac.tree.parent
    ids = sorted({d["id"] for d in read_gate(root)["decisions"]})
    decide(root, ids)                                         # DECIDED with invented records and NO signatures; the allowed-signers file stays empty
    r = mac.real(mac.scope(distribution="yes", gate_sha256=sha_file(mac.tree / "distribution-gate.json")), "--distribution")
    assert r.returncode != 0 and "REFUSES a distribution signing" in r.stderr and not mac.calls("codesign")
    assert "no valid detached signature" in r.stderr


def test_rc3_04_windows_container_real_needs_the_licenses_dir_and_a_matching_listing(win, tmp_path):
    p, h = win.inner()
    m, hm = win.container()
    sc = win.scope("container", [(m, hm)])
    # missing --licenses-dir
    r = win.run("container", [(m, hm)], "--real", "--i-have-owner-authorization", sha_file(sc), "--scope-file", sc, "--ledger", win.ledger,
                "--container-listing", win.listing(), outdir=win.tmp / "wout-a", **win.REAL_ENV)
    assert r.returncode == 2 and "--licenses-dir" in r.stderr
    # the listing does not contain the sha256 of the record
    other = tmp_path / "other-listing.txt"
    other.write_bytes(LISTING_TEXT)
    sc2 = win.scope("container", [(m, hm)], container_listing_sha256=sha_file(other))
    r = win.real("container", [(m, hm)], sc2, "--container-listing", other, outdir=win.tmp / "wout-b")
    assert r.returncode == 2 and "does not appear in the --container-listing" in r.stderr
    # a tampered record
    bad = tmp_path / "bad-lic"
    shutil.copytree(win.lic, bad)
    doc = json.loads((bad / "about.json").read_text())
    doc["product"]["version"] = "9.9.9"
    (bad / "about.json").write_text(json.dumps(doc))
    lst = tmp_path / "bad-listing.txt"
    lst.write_bytes(LISTING_TEXT + f"{sha_file(bad / 'about.json')}  Licenses/about.json\n".encode())
    sc3 = win.scope("container", [(m, hm)], container_listing_sha256=sha_file(lst))
    r = win.real("container", [(m, hm)], sc3, "--container-listing", lst, "--licenses-dir", bad, outdir=win.tmp / "wout-c")
    assert r.returncode == 2 and "failed check_about.py" in r.stderr


def test_rc3_04_windows_licenses_dir_is_only_for_real_container(win):
    p, h = win.inner()
    r = win.run("inner", [(p, h)], "--licenses-dir", win.lic)
    assert r.returncode != 0


# ====================================================================================================================
# RC3 round-2 R2-02: the About check is pinned by the authorization scope (scope_version 3)
# ====================================================================================================================
ABOUT_PIN_KEYS = ("check_about_sha256", "generate_about_sha256", "about_record_sha256", "legal_signers_sha256")


@pytest.mark.parametrize("key", ABOUT_PIN_KEYS)
def test_r2_02_macos_scope_must_bind_each_about_file(mac, key):
    r = mac.real(mac.scope(**{key: "1" * 64}))
    assert r.returncode != 0 and f"{key} does not match" in r.stderr and not mac.calls("codesign")
    r = mac.real(mac.scope(**{key: None}), out=mac.work / "out-missing")
    assert r.returncode != 0 and f"required key '{key}' is missing" in r.stderr
    r = mac.real(mac.scope(**{key: "nothex"}), out=mac.work / "out-bad")
    assert r.returncode != 0 and "must be 64 hex chars" in r.stderr


@pytest.mark.parametrize("rel", ["notices/check_about.py", "notices/generate_about.py", "legal-allowed-signers"])
def test_r2_02_a_file_changed_after_the_scope_was_written_is_refused(mac, rel):
    sc = mac.scope()
    p = mac.tree / rel
    p.write_text(p.read_text() + "\n# changed after the owner wrote the scope\n")
    r = mac.real(sc)
    assert r.returncode != 0 and "does not match" in r.stderr and not mac.calls("codesign")


def test_r2_02_about_record_py_changed_after_the_scope_is_refused(mac):
    sc = mac.scope()
    p = mac.tree.parent / "agentic_os" / "about_record.py"
    p.write_text(p.read_text() + "\n# changed\n")
    r = mac.real(sc)
    assert r.returncode != 0 and "about_record_sha256 does not match" in r.stderr and not mac.calls("codesign")


def test_r2_02_the_record_commit_must_be_the_authorized_build_commit(mac):
    r = mac.real(mac.scope(build_commit="d" * 40))
    assert r.returncode != 0 and "is not the commit of the build that was authorized" in r.stderr and not mac.calls("codesign")
    assert mac.real(mac.scope(), out=mac.work / "out-ok").returncode == 0           # the matching commit passes


def test_r2_02_pins_are_checked_before_the_gate_and_before_anything_else_runs():
    for script in (MAC, WIN):
        code = code_of(script)
        assert code.index("r0p_about_pins") < code.index("r0p_gate ")
    c = code_of(COMMON)
    assert 'python3 -I -B "$t/packaging/notices/check_about.py"' in c and "--code-root" in c and "--expect-commit" in c
    for needle in ("about-check", "cat \"$pk/notices/check_about.py\"", "r0p_sha \"$t/packaging/notices/check_about.py\""):
        assert needle in c, needle


def test_r2_02_the_checker_executes_only_the_pinned_code_root(tmp_path):
    """check_about loads about_record.py and the gate tool from --code-root, never from --root: poison copies in the data root are not run."""
    from .about_fixture import chk_about, make_notices, make_root, run_generate, stage_folder
    root, notices = make_root(tmp_path), make_notices(tmp_path)
    assert run_generate(root, notices) == 0
    lic = stage_folder(root, notices, tmp_path / "Licenses")
    code = tmp_path / "code"
    (code / "agentic_os").mkdir(parents=True)
    (code / "packaging").mkdir()
    shutil.copy(root / "agentic_os" / "about_record.py", code / "agentic_os" / "about_record.py")
    shutil.copy(root / "packaging" / "check-distribution-gate.py", code / "packaging" / "check-distribution-gate.py")
    for poisoned in (root / "agentic_os" / "about_record.py", root / "packaging" / "check-distribution-gate.py"):
        poisoned.write_text("raise SystemExit('POISON executed from the data root')\n")
    assert chk_about.check(lic, root=root, code_root=code) == []
    assert chk_about.check(lic, root=root, code_root=code, expect_commit="0" * 40)            # wrong commit is reported
    with pytest.raises(BaseException):
        chk_about.check(lic, root=root)                                                         # without code_root it would run the poison


@pytest.mark.parametrize("key", ABOUT_PIN_KEYS)
def test_r2_02_windows_container_scope_binds_the_about_files(win, key):
    p, h = win.inner()
    m, hm = win.container()
    sc = win.scope("container", [(m, hm)], **{key: "1" * 64})
    r = win.real("container", [(m, hm)], sc, outdir=win.tmp / "wout-pin")
    assert r.returncode == 2 and f"{key} does not match" in r.stderr


def test_r2_02_windows_container_refuses_a_record_of_another_build(win):
    m, hm = win.container()
    sc = win.scope("container", [(m, hm)], build_commit="d" * 40)
    r = win.real("container", [(m, hm)], sc, outdir=win.tmp / "wout-commit")
    assert r.returncode == 2 and "is not the commit of the build that was authorized" in r.stderr


def test_r2_02_template_documents_scope_version_3_and_the_pins():
    t = (SIGN / "AUTHORIZATION-TEMPLATE.md").read_text()
    for k in ABOUT_PIN_KEYS:
        assert t.count(k + "=") == 2, k                      # macOS and Windows blocks
    assert t.count("scope_version=3") == 2 and t.count("scope_version=2") == 1       # the SSHSIG manifest block is unchanged
    assert "source.commit" in t and "independent" in t


# ====================================================================================================================
# RC3 scope review S3-02: the ledger's one-use is keyed on the scope FILE and now also on the INPUT ARTIFACT
# (packaging/signing/** edited on the RC3 branch only; the changed signing files need an independent review round before any RC3 signing)
# ====================================================================================================================
def _submits(mac) -> int:
    return len([c for c in mac.calls("xcrun") if "submit" in c])


def _rewritten_scope(mac, **over):
    """The reviewer's reproducer: the SAME facts, a window that differs by a little (a different byte string, hence a different scope sha256)."""
    now = datetime.now(timezone.utc)
    return mac.scope(not_before=iso(now - timedelta(hours=2, seconds=3)), **over)


def test_s3_02_a_rewritten_scope_for_the_same_dmg_cannot_notarize_twice(mac):
    sc = mac.scope()
    assert mac.real(sc).returncode == 0 and _submits(mac) == 1
    sc2 = _rewritten_scope(mac)
    assert sha_file(sc2) != sha_file(sc)
    calls = len(mac.calls())
    r = mac.real(sc2, out=mac.work / "out2")
    assert r.returncode == 1 and "input artifact (dmg-" in r.stderr and "needs --allow-reuse" in r.stderr
    assert not (mac.work / "out2").exists() and len(mac.calls()) == calls and _submits(mac) == 1, "no second notarytool call, no tool started"
    # the refusal burned nothing new: the rewritten scope is still unused
    assert sha_file(sc2) not in " ".join(ledger_claims(mac.ledger))
    led = ledger_text(mac.ledger)
    assert led.count("event=started") == 1 and f"artifact=dmg-{sha_file(mac.dmg)}" in led
    assert f"dmg-{sha_file(mac.dmg)}" in ledger_claims(mac.ledger)


def test_s3_02_allow_reuse_repeats_the_artifact_and_records_it(mac):
    assert mac.real(mac.scope()).returncode == 0
    sc2 = _rewritten_scope(mac)
    r = mac.real(sc2, "--allow-reuse", out=mac.work / "out2")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "this input artifact (dmg-" in r.stdout and "--allow-reuse" in r.stdout and _submits(mac) == 2
    led = ledger_text(mac.ledger)
    assert led.count("event=started") == 2 and "artifact_reuse=1" in led and "allow_reuse=1" in led


def test_s3_02_a_failed_run_burns_the_artifact_claim_too(mac):
    mac.conf(NOTARY_MODE="invalid")
    assert mac.real(mac.scope()).returncode != 0
    assert f"dmg-{sha_file(mac.dmg)}" in ledger_claims(mac.ledger)
    mac.conf(NOTARY_MODE="ok")
    r = mac.real(_rewritten_scope(mac), out=mac.work / "retry")
    assert r.returncode == 1 and "already used by an earlier real run" in r.stderr and _submits(mac) == 1


def test_s3_02_another_dmg_is_a_new_artifact(mac):
    assert mac.real(mac.scope()).returncode == 0
    other = mac.work / "input" / "other.dmg"
    other.write_bytes(b"another stub dmg")
    sc = mac.scope(input_dmg_sha256=sha_file(other))
    r = mac.real(sc, input_dmg=other, out=mac.work / "out-other")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert len([c for c in ledger_claims(mac.ledger) if c.startswith("dmg-")]) == 2


def test_s3_02_a_removed_artifact_claim_is_detected_by_the_chain(mac):
    assert mac.real(mac.scope()).returncode == 0
    (mac.ledger / "claims" / f"dmg-{sha_file(mac.dmg)}").rmdir()
    r = mac.real(_rewritten_scope(mac), out=mac.work / "out2")
    assert r.returncode == 1 and "artifact as started but its claim is missing" in r.stderr and _submits(mac) == 1


def test_s3_02_artifact_keys_are_validated(tmp_path):
    led = tmp_path / "ledger"
    for bad in ("dmg-xyz", "../x", "dmg-" + "A" * 64, "win-other-" + "a" * 64, "x"):
        r = lib(tmp_path, f'r0p_ledger_check "{led}" {S64} 0 "{bad}"')
        assert r.returncode == 1 and "invalid artifact key" in r.stderr, bad
    good = "dmg-" + "cd" * 32
    assert lib(tmp_path, f'r0p_ledger_check "{led}" {S64} 0 "{good}"; r0p_ledger_claim "{led}" {S64} 0 real x "{good}"').returncode == 0
    r = lib(tmp_path, f'r0p_ledger_check "{led}" {"ef" * 32} 0 "{good}"')
    assert r.returncode == 1 and "already used by an earlier real run" in r.stderr
    r = lib(tmp_path, f'r0p_ledger_claim "{led}" {"ef" * 32} 0 real x "{good}"')
    assert r.returncode == 1 and "already claimed" in r.stderr and ledger_claims(led) == [good, S64] or ledger_claims(led) == sorted([good, S64])


def test_s3_02_parallel_runs_of_one_artifact_with_different_scopes_exactly_one_wins(tmp_path):
    led = tmp_path / "ledger"
    art = "dmg-" + "12" * 32
    procs = [subprocess.Popen(["/bin/bash", "-p", "-c", 'die() { echo "ERROR: $*" >&2; exit 1; }; set -euo pipefail; . "%s"; r0p_ledger_claim "%s" %s 0 real x %s' % (COMMON, led, f"{i:02x}" * 32, art)],
                              env=clean_env(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(12)]
    outs = [(p.communicate(timeout=60)[1], p.returncode) for p in procs]
    assert sum(1 for _, rc in outs if rc == 0) == 1, outs
    assert all("already claimed" in e for e, rc in outs if rc != 0)
    assert ledger_text(led).count("event=started") == 1


def test_s3_02_windows_a_rewritten_scope_for_the_same_inputs_is_refused(win):
    p, h = win.inner("launcher")
    sc = win.scope("inner", [(p, h)])
    assert win.real("inner", [(p, h)], sc).returncode == 0
    n = len(win.log.read_text().splitlines())
    sc2 = win.scope("inner", [(p, h)], not_before=iso(datetime.now(timezone.utc) - timedelta(hours=2, seconds=3)))
    r = win.real("inner", [(p, h)], sc2, outdir=win.tmp / "wout-2")
    assert r.returncode == 2 and "input artifact (win-inner-" in r.stderr and len(win.log.read_text().splitlines()) == n, "no second token request or signing call"
    r = win.real("inner", [(p, h)], sc2, "--allow-reuse", outdir=win.tmp / "wout-3")
    assert r.returncode == 0 and "artifact_reuse=1" in ledger_text(win.ledger)
    # a different stage is a different artifact key
    m, hm = win.container()
    assert win.real("container", [(m, hm)], win.scope("container", [(m, hm)]), outdir=win.tmp / "wout-4").returncode == 0


def test_s3_02_sshsig_a_rewritten_scope_for_the_same_manifest_is_refused(ssh):
    fake = make_fake_tools(ssh, ssh.key)
    sock = ssh.tmp / "agent.sock"
    s = bind_unix_socket(sock)
    try:
        run1 = lambda sc, sig, *x: ssh.run(*real_args(ssh, sc, sig), *x, path_dirs=[fake], SSH_AUTH_SOCK=str(sock), R0P_ALLOW_NONSYSTEM_TOOLS="stub-test", script=ssh.tscript)
        sc = ssh_scope(ssh, tree=True)
        assert run1(sc, ssh.outside / "a.sig").returncode == 0
        sc2 = ssh_scope(ssh, tree=True, not_before=iso(datetime.now(timezone.utc) - timedelta(hours=2, seconds=3)))
        r = run1(sc2, ssh.outside / "b.sig")
        assert r.returncode == 1 and "input artifact (manifest-" in r.stderr and not (ssh.outside / "b.sig").exists()
        assert run1(sc2, ssh.outside / "b.sig", "--allow-reuse").returncode == 0
        assert "artifact_reuse=1" in ledger_text(ssh.tmp / "ledger")
    finally:
        s.close()


def test_s3_02_the_template_sentence_is_true():
    t = (SIGN / "AUTHORIZATION-TEMPLATE.md").read_text()
    assert "one submission only" in t and "same input artifact" in t
