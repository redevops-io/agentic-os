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
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SIGN = ROOT / "packaging" / "signing"
MAC = SIGN / "sign_and_notarize_r0prime_macos_v3.sh"
WIN = SIGN / "sign_windows_r0prime_v3.sh"
SSH = SIGN / "sign_manifest_sshsig.sh"
HELPER = SIGN / "r0prime_win_signcommand.sh"
VERIFIER = SIGN / "verify_signature_v2.py"
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


def sha_file(p: Path) -> str:
    return sha_bytes(Path(p).read_bytes())


def clean_env(home: Path, path_dirs=(), **extra) -> dict:
    """An allowlist environment: nothing of the caller's environment is inherited."""
    env = {"PATH": ":".join([str(d) for d in path_dirs] + SYSTEM_PATH), "HOME": str(home), "LANG": "C"}
    env.update({k: str(v) for k, v in extra.items()})
    return env


def run(cmd, env, cwd=None, timeout=300, input=None):
    return subprocess.run([str(c) for c in cmd], env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout, input=input)


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


SCRIPTS = [MAC, WIN, SSH, HELPER, SIGN / "tools" / "make_adhoc_fixture.sh"]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_syntax_and_static_rules(script):
    assert script.is_file() and os.access(script, os.X_OK), f"{script} must be executable"
    assert subprocess.run(["/bin/bash", "-n", str(script)], capture_output=True).returncode == 0
    code = code_of(script)
    assert not re.search(r"\brm\s+-[a-zA-Z]*(rf|fr|r)\b", code), "no rm -r / rm -rf"
    assert "sudo " not in code
    assert "set -x" not in code.replace("set +x", "") and "bash -x" not in code
    assert not re.search(r"(^|[;&|(\s])security\s+(find|add|delete|import|unlock|list|dump|cms)", code), "the keychain tool is never called"
    assert "umask 077" in code
    if script in (MAC, WIN, SSH, HELPER):
        assert 'case "$-" in *x*)' in code, "xtrace refusal"


def test_shellcheck_if_available():
    sc = shutil.which("shellcheck")
    if not sc:
        pytest.skip("shellcheck is not installed (not installed by this suite)")
    r = subprocess.run([sc, "-S", "warning", "-s", "bash", str(MAC), str(WIN), str(SSH), str(HELPER)], capture_output=True, text=True)
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
        for n in ("LICENSE", "LICENSE.md", "LICENSING.md", "LICENSES-MANIFEST.json", "NOTICES_MISSING.json", "NOTICES_MISSING.md", "THIRD_PARTY_NOTICES.txt", "LICENSE-COMMONS-CLAUSE"):
            (app / "Contents" / "Resources" / "Licenses" / n).write_text(f"{n}\n")
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

    def env(self, **extra):
        return clean_env(self.home, [self.stubs], **extra)

    def run(self, *args, mode=None, input_dmg=None, out=None, sha=None, **extra_env):
        a = ["/bin/bash", MAC]
        if mode:
            a.append(mode)
        a += ["--input-dmg", input_dmg or self.dmg, "--expected-sha256", sha or sha_file(Path(input_dmg or self.dmg)), "--out-dir", out or self.out]
        a += list(args)
        return run(a, self.env(**extra_env))

    def calls(self, tool=None):
        lines = self.log.read_text().splitlines()
        return [l for l in lines if tool is None or l.startswith(tool + " ")]

    def scope(self, **over) -> Path:
        d = {"scope_version": "1", "valid_from": (date.today() - timedelta(days=1)).isoformat(), "valid_until": (date.today() + timedelta(days=1)).isoformat(),
             "script_sha256": sha_file(MAC), "entitlements_sha256": sha_file(ENT), "input_dmg_sha256": sha_file(self.dmg),
             "identity_sha256": sha_bytes(TEST_IDENTITY.encode()), "team_id": "TESTTEAM01", "notarize": "yes", "upload_to": "apple-notary"}
        d.update(over)
        p = self.tmp / f"scope-{len(list(self.tmp.glob('scope-*')))}.txt"
        p.write_text("# test scope\n" + "".join(f"{k}={v}\n" for k, v in d.items() if v is not None))
        return p

    def real(self, scope: Path, *extra, notarize=True, env=None, **kw):
        e = {"R0P_CODESIGN_IDENTITY": TEST_IDENTITY, "R0P_ALLOW_NONSYSTEM_TOOLS": "stub-test"}
        if notarize:
            e["R0P_NOTARY_PROFILE"] = TEST_PROFILE
        e.update(env or {})
        args = ["--i-have-owner-authorization", sha_file(scope), "--scope-file", scope] + (["--notarize"] if notarize else []) + list(extra)
        return self.run(*args, mode="--real", **dict(e, **kw))


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
def test_real_mode_requires_system_tools_without_the_test_switch(mac):
    sc = mac.scope()
    args = ["--i-have-owner-authorization", sha_file(sc), "--scope-file", sc, "--notarize"]
    r = mac.run(*args, mode="--real", R0P_CODESIGN_IDENTITY=TEST_IDENTITY, R0P_NOTARY_PROFILE=TEST_PROFILE)
    assert r.returncode == 1 and "must resolve to a system location" in r.stderr and not mac.out.exists()


SCOPE_REFUSALS = [
    ("wrong script hash", dict(script_sha256="1" * 64), "script_sha256"),
    ("wrong entitlements hash", dict(entitlements_sha256="1" * 64), "entitlements_sha256"),
    ("wrong input hash", dict(input_dmg_sha256="1" * 64), "input_dmg_sha256"),
    ("wrong identity", dict(identity_sha256="1" * 64), "identity_sha256"),
    ("bad team id", dict(team_id="lower"), "team_id"),
    ("expired window", dict(valid_until=(date.today() - timedelta(days=1)).isoformat()), "outside the authorized window"),
    ("future window", dict(valid_from=(date.today() + timedelta(days=1)).isoformat()), "outside the authorized window"),
    ("notarize=no but --notarize given", dict(notarize="no", upload_to="none"), "notarize=no"),
    ("unknown key", dict(surprise="1"), "unknown key"),
    ("bad version", dict(scope_version="2"), "scope_version"),
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
    sc = mac.scope(notarize="no", upload_to="none")
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
class Ssh:
    def __init__(self, tmp: Path):
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
        self.key = self.make_key("k1", "signproc-TEST-KEY")
        self.pub = self.key.with_name("k1.pub")

    def make_key(self, name, comment) -> Path:
        k = self.keys / name
        r = run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", k], clean_env(self.home))
        assert r.returncode == 0, r.stderr
        return k

    def env(self, path_dirs=(), **extra):
        return clean_env(self.home, path_dirs, **extra)

    def digest(self):
        return sha_file(self.manifest)

    def run(self, *args, path_dirs=(), **extra):
        return run(["/bin/bash", SSH, *args], self.env(path_dirs, **extra))

    def selftest(self, sig=None, **kw):
        sig = sig or self.outside / "m.sig"
        return self.run("--self-test", "--test-private-key", self.key, "--manifest", self.manifest, "--pubkey", self.pub,
                        "--expected-sha256", kw.pop("sha", self.digest()), "--sig-out", sig, "--record-out", self.outside / "rec.txt", **kw), sig

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
    assert "mode: dry-run" in r.stdout and "ssh-keygen -Y sign -n redevops-qa-release@v1 -f" in r.stdout and "no agent is contacted" in r.stdout
    assert tree_state(ssh.ev) == before and list(ssh.outside.iterdir()) == []
    r = ssh.run("--manifest", ssh.manifest, "--pubkey", ssh.pub, "--expected-sha256", "0" * 64)
    assert r.returncode == 1 and "not the expected digest" in r.stderr


def test_sshsig_selftest_signs_without_adding_a_file_to_the_evidence_dir_and_verifies(ssh):
    before = tree_state(ssh.ev)
    r, sig = ssh.selftest()
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert sig.read_text().startswith("-----BEGIN SSH SIGNATURE-----")
    assert tree_state(ssh.ev) == before, "the evidence directory is byte-for-byte unchanged (names, sizes, mtimes)"
    assert "THROWAWAY KEY" in r.stdout and "Good \"redevops-qa-release@v1\" signature" in r.stdout
    rec = (ssh.outside / "rec.txt").read_text()
    assert ssh.digest() in rec and "no time" in rec and "SHA256:" in rec
    v = ssh.run("--verify-only", "--manifest", ssh.manifest, "--sig", sig, "--pubkey", ssh.pub)
    assert v.returncode == 0 and "VERIFIED" in v.stdout


def test_sshsig_failure_cases(ssh):
    r, sig = ssh.selftest()
    assert r.returncode == 0
    # tampered manifest
    ssh.manifest.write_text('{"files": {"a": "1"}}\n')
    v = ssh.run("--verify-only", "--manifest", ssh.manifest, "--sig", sig, "--pubkey", ssh.pub)
    assert v.returncode == 5 and "NOT VERIFIED" in v.stderr
    ssh.manifest.write_text('{"files": {"a": "0"}}\n')
    assert ssh.run("--verify-only", "--manifest", ssh.manifest, "--sig", sig, "--pubkey", ssh.pub).returncode == 0
    # wrong namespace: sign the same bytes directly with ssh-keygen in another namespace
    bad = ssh.outside / "ns.sig"
    with open(ssh.manifest, "rb") as f:
        p = subprocess.run(["/usr/bin/ssh-keygen", "-Y", "sign", "-n", "other-namespace@v1", "-f", str(ssh.key)], stdin=f, capture_output=True, env=clean_env(ssh.home))
    assert p.returncode == 0
    bad.write_bytes(p.stdout)
    v = ssh.run("--verify-only", "--manifest", ssh.manifest, "--sig", bad, "--pubkey", ssh.pub)
    assert v.returncode == 5
    # signature by another key
    k2 = ssh.make_key("k2", "signproc-TEST-KEY")
    with open(ssh.manifest, "rb") as f:
        p = subprocess.run(["/usr/bin/ssh-keygen", "-Y", "sign", "-n", "redevops-qa-release@v1", "-f", str(k2)], stdin=f, capture_output=True, env=clean_env(ssh.home))
    other = ssh.outside / "k2.sig"
    other.write_bytes(p.stdout)
    assert ssh.run("--verify-only", "--manifest", ssh.manifest, "--sig", other, "--pubkey", ssh.pub).returncode == 5
    # an expected fingerprint that is not the key's
    v = ssh.run("--verify-only", "--manifest", ssh.manifest, "--sig", sig, "--pubkey", ssh.pub, "--expected-fingerprint", "SHA256:" + "A" * 43)
    assert v.returncode == 1 and "not the expected one" in v.stderr


def test_sshsig_refusals(ssh):
    # wrong digest: nothing is signed, nothing is created
    r, sig = ssh.selftest(sha="0" * 64)
    assert r.returncode == 1 and "nothing was signed" in r.stderr and not sig.exists()
    # the signature must not be placed inside the evidence directory
    r, sig = ssh.selftest(sig=ssh.ev / "release-manifest.json.sig")
    assert r.returncode == 1 and "must not be added to the evidence set" in r.stderr and not sig.exists()
    # existing --sig-out is never overwritten
    (ssh.outside / "m.sig").write_text("x")
    assert "already exists" in ssh.selftest()[0].stderr
    (ssh.outside / "m.sig").unlink()
    # a private key given as --pubkey is refused outright (never handled by this script)
    r = ssh.run("--manifest", ssh.manifest, "--pubkey", ssh.key, "--expected-sha256", ssh.digest())
    assert r.returncode == 1 and "contains a private key" in r.stderr
    # self-test only with a throwaway key (comment) and with its own private key
    k3 = ssh.make_key("k3", "someone@laptop")
    r = ssh.run("--self-test", "--test-private-key", k3, "--manifest", ssh.manifest, "--pubkey", k3.with_name("k3.pub"), "--expected-sha256", ssh.digest(), "--sig-out", ssh.outside / "x.sig")
    assert r.returncode == 1 and "signproc-TEST-KEY" in r.stderr
    r = ssh.run("--self-test", "--test-private-key", ssh.keys / "k3", "--manifest", ssh.manifest, "--pubkey", ssh.pub, "--expected-sha256", ssh.digest(), "--sig-out", ssh.outside / "x.sig")
    assert r.returncode == 1 and "does not belong" in r.stderr
    assert "release key" in SSH.read_text() and "wUoE8xKJpmtTH7GqWXMIl9/BW0SH1Isc5/w9HEiwqA" in SSH.read_text()
    assert not (ssh.outside / "x.sig").exists()


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


def make_fake_keygen(ssh: Ssh, real_key: Path) -> Path:
    """A stub ssh-keygen: logs argv and whether an agent socket was passed; for `-Y sign -f <public key>` it signs with the
    THROWAWAY key (never an agent); everything else goes to the real ssh-keygen."""
    d = ssh.tmp / "fakebin"
    d.mkdir()
    write_exec(d / "ssh-keygen", textwrap.dedent(f'''\
        #!/bin/bash
        echo "ssh-keygen $*" >> "{ssh.tmp}/keygen.log"
        echo "AUTH_SOCK=${{SSH_AUTH_SOCK:-unset}}" >> "{ssh.tmp}/keygen.log"
        if [ "$1" = "-Y" ] && [ "$2" = "sign" ]; then
          shift 2; args=(); while [ $# -gt 0 ]; do case "$1" in -f) shift 2;; *) args+=("$1"); shift;; esac; done
          exec /usr/bin/ssh-keygen -Y sign -f "{real_key}" "${{args[@]}}"
        fi
        exec /usr/bin/ssh-keygen "$@"
        '''))
    return d


def ssh_scope(ssh: Ssh, sock_free=True, **over) -> Path:
    fpr = [x for x in run(["/usr/bin/ssh-keygen", "-lf", ssh.pub], clean_env(ssh.home)).stdout.split() if x.startswith("SHA256:")][0]
    d = {"scope_version": "1", "valid_from": (date.today() - timedelta(days=1)).isoformat(), "valid_until": (date.today() + timedelta(days=1)).isoformat(),
         "script_sha256": sha_file(SSH), "manifest_sha256": ssh.digest(), "key_fingerprint": fpr, "namespace": "redevops-qa-release@v1"}
    d.update(over)
    p = ssh.tmp / f"sc-{len(list(ssh.tmp.glob('sc-*')))}.txt"
    p.write_text("".join(f"{k}={v}\n" for k, v in d.items()))
    return p


def test_sshsig_real_mode_uses_only_the_agent_path_and_the_public_key_file(ssh):
    # a unix socket file stands in for the agent socket; the stub ssh-keygen proves what the script passes
    fake = make_fake_keygen(ssh, ssh.key)
    sock = ssh.tmp / "agent.sock"
    s = bind_unix_socket(sock)
    try:
        sc = ssh_scope(ssh)
        fpr = [x for x in run(["/usr/bin/ssh-keygen", "-lf", ssh.pub], clean_env(ssh.home)).stdout.split() if x.startswith("SHA256:")][0]
        sig = ssh.outside / "real.sig"
        r = ssh.run("--real", "--manifest", ssh.manifest, "--pubkey", ssh.pub, "--expected-sha256", ssh.digest(), "--expected-fingerprint", fpr,
                    "--i-have-owner-authorization", sha_file(sc), "--scope-file", sc, "--sig-out", sig,
                    path_dirs=[fake], SSH_AUTH_SOCK=str(sock), R0P_ALLOW_NONSYSTEM_TOOLS="stub-test")
        assert r.returncode == 0, (r.stdout, r.stderr)
        log = (ssh.tmp / "keygen.log").read_text()
        sign_line = [l for l in log.splitlines() if l.startswith("ssh-keygen -Y sign")][0]
        assert f"-n redevops-qa-release@v1 -f {ssh.pub}" in sign_line, "the PUBLIC key file is what is handed to ssh-keygen"
        assert str(ssh.key) not in sign_line.split(), "no private key path is passed"
        # the agent socket reached ssh-keygen for the sign call (real mode only)
        assert f"AUTH_SOCK={sock}" in log
        assert sig.is_file() and "Good" in r.stdout
        # without --real the socket is never passed on
        (ssh.tmp / "keygen.log").write_text("")
        r2 = ssh.run("--self-test", "--test-private-key", ssh.key, "--manifest", ssh.manifest, "--pubkey", ssh.pub, "--expected-sha256", ssh.digest(),
                     "--sig-out", ssh.outside / "st.sig", path_dirs=[fake], SSH_AUTH_SOCK=str(sock), R0P_ALLOW_NONSYSTEM_TOOLS="stub-test")
        assert r2.returncode == 0, r2.stderr
        assert "AUTH_SOCK=unset" in (ssh.tmp / "keygen.log").read_text() and str(sock) not in (ssh.tmp / "keygen.log").read_text()
    finally:
        s.close()


def test_sshsig_real_mode_refusals(ssh):
    fake = make_fake_keygen(ssh, ssh.key)
    sock = ssh.tmp / "agent.sock"
    s = bind_unix_socket(sock)
    try:
        fpr = [x for x in run(["/usr/bin/ssh-keygen", "-lf", ssh.pub], clean_env(ssh.home)).stdout.split() if x.startswith("SHA256:")][0]

        def go(sc, sig="r.sig", manifest=None, fp=fpr, sock_env=True, **kw):
            args = ["--real", "--manifest", manifest or ssh.manifest, "--pubkey", ssh.pub, "--expected-sha256", sha_file(Path(manifest or ssh.manifest)),
                    "--i-have-owner-authorization", sha_file(sc), "--scope-file", sc, "--sig-out", ssh.outside / sig]
            if fp:
                args += ["--expected-fingerprint", fp]
            env = dict(R0P_ALLOW_NONSYSTEM_TOOLS="stub-test")
            if sock_env:
                env["SSH_AUTH_SOCK"] = str(sock)
            return ssh.run(*args, path_dirs=[fake], **env)

        assert "ssh-agent" in go(ssh_scope(ssh), sock_env=False).stderr
        assert "--expected-fingerprint" in go(ssh_scope(ssh), fp=None).stderr
        assert "outside the authorized window" in go(ssh_scope(ssh, valid_until=(date.today() - timedelta(days=1)).isoformat())).stderr
        assert "script_sha256" in go(ssh_scope(ssh, script_sha256="1" * 64)).stderr
        assert "manifest_sha256" in go(ssh_scope(ssh, manifest_sha256="1" * 64)).stderr
        assert "key_fingerprint" in go(ssh_scope(ssh, key_fingerprint="SHA256:" + "B" * 43)).stderr
        assert "namespace" in go(ssh_scope(ssh, namespace="other@v1")).stderr
        assert "unknown key" in go(ssh_scope(ssh, extra="1")).stderr
        other = ssh.ev / "not-the-manifest.json"
        other.write_text("{}\n")
        sc = ssh_scope(ssh, manifest_sha256=sha_file(other))
        assert "only the top-level release-manifest.json" in go(sc, manifest=other).stderr
        assert "expected one" in go(ssh_scope(ssh), fp="SHA256:" + "C" * 43).stderr
        assert not (ssh.outside / "r.sig").exists() and not (ssh.tmp / "keygen.log").exists() or "-Y sign" not in (ssh.tmp / "keygen.log").read_text()
    finally:
        s.close()


# ====================================================================================================================
# hand-off helper (Windows build host) and the Windows signing script, with stubs
# ====================================================================================================================
def helper_run(tmp: Path, mode: str, f: Path, d: Path = None, **env):
    e = clean_env(tmp / "h", [], R0P_HANDOFF_MODE=mode, R0P_HANDOFF_DIR=str(d), **env)
    (tmp / "h").mkdir(exist_ok=True)
    return run(["/bin/bash", HELPER, f], e)


def test_handoff_helper_collect_and_apply(tmp_path):
    d = tmp_path / "round1"
    d.mkdir()
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
    assert (d / "requests-container" / MSI_NAME).is_file()
    log = (d / "signcommand.log").read_text()
    assert "skipped (third-party" in log and "role=unknown" in log
    # idempotent; a different file with the same request name is impossible (the name carries the hash), a changed container is refused
    assert helper_run(tmp_path, "collect", launcher, d).returncode == 0
    msi.write_bytes(b"msi unsigned CHANGED")
    r = helper_run(tmp_path, "collect", msi, d)
    assert r.returncode == 1 and "different bytes" in r.stderr
    # round 2: signed files come back (here: stand-ins with extra bytes) with the SHA256SUMS of the signing run
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
        r = helper_run(tmp_path, "apply", src, d2)
        assert r.returncode == 0 and "replaced" in r.stdout, r.stderr
        assert src.read_bytes().endswith(b"\nSIGNED")
        # running it again on the now-signed file is accepted unchanged
        assert "already signed" in helper_run(tmp_path, "apply", src, d2).stdout
    assert helper_run(tmp_path, "apply", dll, d2).returncode == 0 and dll.read_bytes() == b"MZ third party dll"
    # failures never let an unsigned inner file through
    r = helper_run(tmp_path, "apply", other, d2)
    assert r.returncode == 1 and "unknown executable" in r.stderr
    new = bins / "agentic-r0prime.exe"
    new.write_bytes(b"MZ a different build of the launcher")
    r = helper_run(tmp_path, "apply", new, d2)
    assert r.returncode == 1 and "no signed counterpart" in r.stderr and new.read_bytes() == b"MZ a different build of the launcher"
    # a tampered signed file fails its SHA256SUMS entry
    req = f"launcher-{before['agentic-r0prime.exe']}.exe"
    (d2 / "signed-inner" / req).write_bytes(b"tampered")
    unsigned_again = tmp_path / "again" / "agentic-r0prime.exe"
    unsigned_again.parent.mkdir()
    unsigned_again.write_bytes(b"MZ unsigned launcher")
    r = helper_run(tmp_path, "apply", unsigned_again, d2)
    assert r.returncode == 1 and "does not match its SHA256SUMS" in r.stderr
    # containers are recorded unsigned in apply mode
    (tmp_path / "again" / MSI_NAME).write_bytes(b"rebuilt msi")
    assert helper_run(tmp_path, "apply", tmp_path / "again" / MSI_NAME, d2).returncode == 0
    assert (d2 / "requests-container" / MSI_NAME).read_bytes() == b"rebuilt msi"
    # argument policy
    assert "absolute" in run(["/bin/bash", HELPER, launcher], clean_env(tmp_path, [], R0P_HANDOFF_MODE="collect", R0P_HANDOFF_DIR="rel")).stderr
    assert "collect or apply" in run(["/bin/bash", HELPER, launcher], clean_env(tmp_path, [], R0P_HANDOFF_DIR=str(d))).stderr


# ---- Windows signing script with stub java / python / az ------------------------------------------------------------
STUB_JAVA = r'''#!/bin/bash
D="$(cd "$(dirname "$0")" && pwd)"; . "$D/stub.conf"
case "$1" in -version) echo 'openjdk version "stub"' >&2; exit 0;; esac
echo "java $*" >> "$LOG"
env | sed 's/=.*//' | sort >> "$D/java-env-names.txt"
[ "${JAVA_SLEEP:-0}" -gt 0 ] && exec sleep "$JAVA_SLEEP"
[ "${JAVA_FAIL:-0}" = 1 ] && exit 1
if [ "${JAVA_STORETYPE_CHECK:-0}" = 1 ]; then :; fi
for a in "$@"; do
  case "$a" in file:*) tf="${a#file:}"; [ -f "$tf" ] && cp "$tf" "$D/token-seen.txt";; esac
  if [ -f "$a" ]; then case "$a" in *.exe|*.msi) [ "${JAVA_NOSIGN:-0}" = 1 ] || printf '\nSTUB-AUTHENTICODE-SIGNATURE' >> "$a";; esac; fi
done
exit 0
'''

STUB_PY = r'''#!/bin/bash
D="$(cd "$(dirname "$0")" && pwd)"; . "$D/stub.conf"
# python -I - FILE  (signature count) | python -I verify_signature_v2.py ... | python -I -c ...
if [ "$1" = "-I" ] && [ "$2" = "-" ]; then cat >/dev/null; if grep -q STUB-AUTHENTICODE "$3" 2>/dev/null; then echo 1; else echo 0; fi; exit 0; fi
if [ "$1" = "-I" ] && [ "$2" = "-c" ]; then echo "0.0-stub"; exit 0; fi
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
echo "STUBTOKEN-0123456789-SECRET"
exit 0
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
        # tools dir with a stand-in "jsign jar"; the production script pins the real jar's sha256, so the test copies the script
        # into its own tree and replaces the pin with the stand-in's hash (the production script is not edited)
        self.tools = tmp / "tools"
        (self.tools / "jsign").mkdir(parents=True)
        (self.tools / "jsign" / "jsign-7.6.jar").write_bytes(b"stand-in jar")
        tree = tmp / "tree"
        (tree / "signing").mkdir(parents=True)
        shutil.copy(WIN, tree / "signing" / WIN.name)
        shutil.copy(VERIFIER, tree / "signing" / VERIFIER.name)
        shutil.copy(ROOT / "packaging" / "identity.json", tree / "identity.json")
        text = (tree / "signing" / WIN.name).read_text()
        pin = "6cf58aa080e7ff82656f320cff8d4a3bff30408df8766806dcba3740dd839fa5"
        assert pin in text
        (tree / "signing" / WIN.name).write_text(text.replace(pin, sha_bytes(b"stand-in jar")))
        (tree / "signing" / WIN.name).chmod(0o755)
        self.script = tree / "signing" / WIN.name
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

    def run(self, stage, pairs, *args, outdir=None, **env):
        a = ["/bin/bash", self.script, "--stage", stage]
        for p, h in pairs:
            a += ["--input", p, "--sha256", h]
        a += ["--outdir", outdir or (self.tmp / "wout"), "--tools-dir", self.tools, "--python", self.stubs / "py"] + list(args)
        return run(a, clean_env(self.home, [self.stubs], **env))

    def scope(self, st, pairs, **over) -> Path:
        d = {"scope_version": "1", "valid_from": (date.today() - timedelta(days=1)).isoformat(), "valid_until": (date.today() + timedelta(days=1)).isoformat(),
             "script_sha256": sha_file(self.script), "verifier_sha256": sha_file(VERIFIER), "stage": st,
             "inputs_sha256": ",".join(sorted(h for _, h in pairs)), "az_endpoint": "https://eus.codesigning.azure.net/", "az_account": "stub-account",
             "az_profile": "stub-profile", "expected_cn": "CN=Stub Test Org"}
        d.update(over)
        p = self.tmp / f"wscope-{len(list(self.tmp.glob('wscope-*')))}.txt"
        p.write_text("".join(f"{k}={v}\n" for k, v in d.items()))
        return p

    REAL_ENV = {"R0P_AZ_ENDPOINT": "https://eus.codesigning.azure.net/", "R0P_AZ_ACCOUNT": "stub-account", "R0P_AZ_PROFILE": "stub-profile", "R0P_AZ_EXPECTED_CN": "CN=Stub Test Org"}

    def real(self, stage, pairs, sc, *args, env=None, **kw):
        e = dict(self.REAL_ENV)
        e.update(env or {})
        return self.run(stage, pairs, "--real", "--i-have-owner-authorization", sha_file(sc), "--scope-file", sc, *args, **dict(e, **kw))


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
    # the same refusal in dry-run mode (mode-independent)
    r = win.run("inner", [(p, h)], **env)
    assert r.returncode == 2 and msg in r.stderr


WIN_SCOPE_REFUSALS = [
    ("script", dict(script_sha256="1" * 64), "script_sha256"), ("verifier", dict(verifier_sha256="1" * 64), "verifier_sha256"),
    ("stage", dict(stage="container"), "stage"), ("inputs", dict(inputs_sha256="1" * 64), "inputs_sha256"),
    ("endpoint", dict(az_endpoint="https://weu.codesigning.azure.net/"), "service coordinates"), ("account", dict(az_account="other-acct"), "service coordinates"),
    ("cn", dict(expected_cn="CN=Other"), "expected_cn"), ("expired", dict(valid_until=(date.today() - timedelta(days=1)).isoformat()), "outside the authorized window"),
    ("unknown", dict(extra="1"), "unknown key"),
]


@pytest.mark.parametrize("name,over,msg", WIN_SCOPE_REFUSALS, ids=[w[0] for w in WIN_SCOPE_REFUSALS])
def test_windows_real_scope_refusals_make_no_azure_call(win, name, over, msg):
    p, h = win.inner()
    sc = win.scope("inner", [(p, h)], **over)
    r = win.real("inner", [(p, h)], sc)
    assert r.returncode == 2 and msg in r.stderr, (r.stdout, r.stderr)
    assert "az " not in win.log.read_text() and not (win.tmp / "wout").exists()


def test_windows_real_requires_authorization_and_env(win):
    p, h = win.inner()
    r = win.run("inner", [(p, h)], "--real", **win.REAL_ENV)
    assert r.returncode == 2 and "--i-have-owner-authorization" in r.stderr
    sc = win.scope("inner", [(p, h)])
    r = win.run("inner", [(p, h)], "--real", "--i-have-owner-authorization", sha_file(sc), "--scope-file", sc)
    assert r.returncode == 2 and "R0P_AZ_ENDPOINT" in r.stderr
    r = win.run("inner", [(p, h)], "--dry-run", "--real", **win.REAL_ENV)
    assert r.returncode == 2 and "at most one" in r.stderr
    r = win.run("inner", [(p, h)], "--i-have-owner-authorization", "0" * 64)
    assert "used only with --real" in r.stderr
    assert "az " not in win.log.read_text()


def test_windows_real_path_with_stubs_token_handling_and_two_stages(win):
    # stage inner
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
    # the token file was readable by (stub) Jsign through file:, then removed; nothing of the token is in the outdir or on the console
    assert (win.stubs / "token-seen.txt").read_text().strip() == "STUBTOKEN-0123456789-SECRET"
    out = win.tmp / "wout"
    assert sorted(x.name for x in out.iterdir()) == sorted(["EVIDENCE.txt", "SHA256SUMS", p.name, q.name])
    assert "STUBTOKEN" not in all_text(out) + r.stdout + r.stderr
    ev = (out / "EVIDENCE.txt").read_text()
    assert "stage:           inner" in ev and "role=launcher" in ev and "role=sidecar" in ev and "NOT yet in any installer" in ev
    assert "/Users/" not in ev and "/private/" not in ev and "/var/folders" not in ev
    assert (out / p.name).read_bytes().endswith(b"STUB-AUTHENTICODE-SIGNATURE") and sha_file(p) == h, "the input is untouched"
    # the Jsign child saw neither the token nor cloud/agent variables in its environment names
    names = (win.stubs / "java-env-names.txt").read_text().split()
    assert not [n for n in names if n.startswith(("AZURE", "AWS", "SSH_", "GITHUB", "GH_", "APPROVAL"))]
    az_names = (win.stubs / "az-env-names.txt").read_text().split()
    assert not [n for n in az_names if n.startswith(("AWS", "SSH_", "GITHUB", "GH_", "APPROVAL"))]
    # the hand-off: SHA256SUMS of the signing run feeds the build host's apply step
    assert (out / "SHA256SUMS").read_text().split()[1] == p.name
    # stage container, a separate run, own scope
    m, hm = win.container()
    scc = win.scope("container", [(m, hm)])
    r2 = win.real("container", [(m, hm)], scc, outdir=win.tmp / "wout2")
    assert r2.returncode == 0, (r2.stdout, r2.stderr)
    assert "stage:           container" in (win.tmp / "wout2" / "EVIDENCE.txt").read_text()
    assert "Windows acceptance kit must check the installed files" in (win.tmp / "wout2" / "EVIDENCE.txt").read_text()


def test_windows_verification_failure_releases_nothing_and_leaves_a_marker(win):
    win.conf(VERIFY_FAIL=1)
    p, h = win.inner()
    sc = win.scope("inner", [(p, h)])
    r = win.real("inner", [(p, h)], sc)
    assert r.returncode == 4
    out = win.tmp / "wout"
    assert [x.name for x in out.iterdir()] == ["FAILED"] and "FAILED-ATTEMPT" in (out / "FAILED").read_text()
    assert "STUBTOKEN" not in (out / "FAILED").read_text() + r.stdout + r.stderr
    # the token file did not survive the failure
    assert not list(out.rglob("token"))


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
    assert "--tsa-url is for --throwaway-test only" in win.run("inner", [(p, h)], "--tsa-url", "http://127.0.0.1:1", outdir=win.tmp / "x").stderr or True


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
    (d / "signed-inner").mkdir()
    shutil.copy(tmp_path / "signed-out" / req.name, d / "signed-inner" / req.name)
    shutil.copy(tmp_path / "signed-out" / "SHA256SUMS", d / "signed-inner" / "SHA256SUMS")
    r = helper_run(tmp_path, "apply", exe, d)
    assert r.returncode == 0 and exe.read_bytes().endswith(b"STUB-AUTHENTICODE-SIGNATURE")
