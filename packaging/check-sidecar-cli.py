#!/usr/bin/env python3
"""Fail-closed CLI check for the sidecar executable (RC3): help works, anything unknown starts NOTHING.

  python3 -I packaging/check-sidecar-cli.py [path/to/redevops-sidecar]          (default packaging/dist/redevops-sidecar)
  python3 -I packaging/check-sidecar-cli.py --python-entry packaging/sidecar_main.py   (the source entry, same matrix)

For each case the sidecar runs under a scrubbed environment (throwaway HOME and TMPDIR, PROJECTS_PORT set to a free port, a
cp1252 stdout/stderr hint) with a short timeout. Expected: `--help`, `-h`, `help` and `serve --help` print the usage on stdout and
exit 0; no argument, an unknown command and an unknown option print the usage (ASCII) on stderr and exit 2; in every case no
launch URL or code appears, nothing listens on the port, and nothing is created in the scratch HOME. A process that does not exit
within the timeout (a server was started) fails the case. Standard library only. Exit 0 = all cases pass.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

CASES = [  # (args, expected exit, which stream holds the usage)
    ([], 2, "stderr"), (["--help"], 0, "stdout"), (["-h"], 0, "stdout"), (["help"], 0, "stdout"), (["serve", "--help"], 0, "stdout"),
    (["bootstrap", "--help"], 0, "stdout"), (["--version"], 2, "stderr"), (["--bogus"], 2, "stderr"), (["frobnicate"], 2, "stderr"),
    (["serve", "--bogus"], 2, "stderr"), (["serve", "extra"], 2, "stderr"), (["bootstrap", "--x"], 2, "stderr"),
    (["SERVE"], 2, "stderr"), ([""], 2, "stderr"), (["--exit-on-stdin-eof"], 2, "stderr"),
]


def run_case(cmd: list[str], args: list[str]) -> tuple[int, bytes, bytes, list[str], bool]:
    with tempfile.TemporaryDirectory() as home:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        env = {"PATH": "/usr/bin:/bin", "HOME": home, "TMPDIR": home, "USERPROFILE": home, "PROJECTS_PORT": str(port),
               "PROJECTS_HOST": "127.0.0.1", "PYTHONIOENCODING": "cp1252", "LANG": "C"}
        if "SYSTEMROOT" in os.environ:
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        try:
            p = subprocess.run(cmd + args, env=env, cwd=home, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
            rc, out, err, hung = p.returncode, p.stdout, p.stderr, False
        except subprocess.TimeoutExpired as e:
            rc, out, err, hung = -1, e.stdout or b"", e.stderr or b"", True
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            listening = True
        except OSError:
            listening = False
        left = sorted(os.listdir(home))
        return rc, out, err, left, hung or listening


def main(argv=None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    if a[:1] == ["--python-entry"]:
        cmd = [sys.executable, "-I", str(Path(a[1]).resolve())]       # each case runs in a scratch directory: never a relative path
        env_py = True
    else:
        cmd = [str(Path(a[0]).resolve()) if a else str(Path(__file__).resolve().parent / "dist" / "redevops-sidecar")]
        env_py = False
    bad = 0
    for args, want_rc, stream in CASES:
        c = list(cmd)
        if env_py:                      # the source entry needs the repository on sys.path (-I ignores PYTHONPATH)
            c = [sys.executable, "-I", "-c", "import sys;sys.path.insert(0,%r);sys.argv=[%r]+sys.argv[1:];"
                 "import runpy;runpy.run_path(%r,run_name='__main__')" % (str(Path(cmd[-1]).resolve().parents[1]), cmd[-1], cmd[-1])]
        rc, out, err, left, started = run_case(c, args)
        text = (out if stream == "stdout" else err)
        problems = []
        if rc != want_rc:
            problems.append(f"exit {rc}, wanted {want_rc}")
        if b"usage:" not in text or b"serve" not in text or b"bootstrap" not in text:
            problems.append(f"no usage on {stream}")
        other = err if stream == "stdout" else out
        if other.strip():
            problems.append(f"unexpected output on the other stream: {other[:80]!r}")
        if any(b in out + err for b in (b"#lc=", b"launch", b"Open this one-time URL", b"http://")):
            problems.append("launch URL or code printed")
        try:
            (text + out + err).decode("ascii")
        except UnicodeDecodeError:
            problems.append("output is not ASCII")
        if left:
            problems.append(f"files created in HOME: {left}")
        if started:
            problems.append("a server was started (timeout or listening port)")
        print(("FAIL " if problems else "ok   ") + " ".join(args or ["(no arguments)"]) + ("  " + "; ".join(problems) if problems else ""))
        bad += bool(problems)
    print("sidecar CLI is fail-closed" if not bad else f"{bad} case(s) failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
