#!/usr/bin/env python3
"""Lifecycle acceptance for the FROZEN sidecar on macOS/Linux (R0-prime defect b). No GUI, no Rust.

    python3 packaging/check-sidecar-lifecycle.py [path/to/redevops-sidecar]

The sidecar is a PyInstaller onefile binary = a bootloader process + a Python child, extracting to
$TMPDIR/_MEIxxxx. For each scenario this starts the real binary the way the launcher does (own process
group, piped stdin, `serve --exit-on-stdin-eof`, throwaway TMPDIR/HOME, a free port) and reports what is
left behind: processes, listener, _MEI dirs. Scenarios marked [limit] document what is NOT achievable
(a hard kill of the whole group skips the bootloader's cleanup).

Exit status 0 when every non-limit scenario ends with no process, no listener and no _MEI dir.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent / "dist" / "redevops-sidecar"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def wait(pred, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


def group_pids(pgid: int) -> list[int]:
    out = subprocess.run(["ps", "-axo", "pid=,pgid="], capture_output=True, text=True).stdout
    return [int(p) for p, g in (l.split() for l in out.splitlines() if l.strip()) if int(g) == pgid]


class Scenario:
    def __init__(self, binary: Path, tether: bool = True):
        self.root = Path(tempfile.mkdtemp(prefix="rdo-life-"))
        (self.root / "tmp").mkdir()
        (self.root / "home").mkdir()
        self.port = free_port()
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.root / "home"), "TMPDIR": str(self.root / "tmp"),
               "PROJECTS_PORT": str(self.port), "PROJECTS_HOST": "127.0.0.1", "LANG": "en_US.UTF-8"}
        args = [str(binary), "serve"] + (["--exit-on-stdin-eof"] if tether else [])
        self.p = subprocess.Popen(args, env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, process_group=0)
        self.pgid = self.p.pid
        if not wait(lambda: listening(self.port), 90):
            self.cleanup_force()
            raise SystemExit("sidecar never listened")

    def mei(self) -> list[str]:
        return sorted(x.name for x in (self.root / "tmp").glob("_MEI*"))

    def state(self) -> str:
        self.p.poll()  # reap the bootloader (an unreaped zombie would still show in the group)
        return f"procs={len(group_pids(self.pgid))} listener={listening(self.port)} mei={len(self.mei())}"

    def clean(self, timeout: float = 10) -> bool:
        def gone() -> bool:
            self.p.poll()  # reap the bootloader (an unreaped zombie would still show in the group)
            return not group_pids(self.pgid) and not listening(self.port) and not self.mei()
        return wait(gone, timeout)

    def cleanup_force(self) -> None:
        try:
            os.killpg(self.pgid, signal.SIGKILL)
        except OSError:
            pass
        try:
            self.p.wait(timeout=5)
        except Exception:
            pass
        shutil.rmtree(self.root, ignore_errors=True)


def main() -> int:
    binary = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not binary.exists():
        print(f"sidecar not found: {binary}")
        return 2
    bad = 0

    def report(name: str, ok: bool, detail: str, limit: bool = False) -> None:
        nonlocal bad
        tag = "ok  " if ok else ("LIMIT" if limit else "FAIL")
        if not ok and not limit:
            bad += 1
        print(f"{tag} {name}: {detail}")

    # 1. launcher closes the stdin tether (Stop / quit)
    s = Scenario(binary)
    before = s.state()
    s.p.stdin.close()
    ok = s.clean()
    report("stop via stdin tether", ok, f"before[{before}] after[{s.state()}]")
    s.cleanup_force()

    # 2. SIGTERM to the process group (what lifecycle::stop sends)
    s = Scenario(binary)
    before = s.state()
    os.killpg(s.pgid, signal.SIGTERM)
    ok = s.clean()
    report("SIGTERM to the process group", ok, f"before[{before}] after[{s.state()}]")
    s.cleanup_force()

    # 3. the launcher dies by SIGKILL: stand-in launcher holds the pipe; killing it closes the tether
    s = Scenario(binary)
    # the stand-in is the test process itself: simulate "launcher SIGKILLed" by closing our end without
    # any signal to the sidecar (the kernel does the same when the launcher process is killed)
    before = s.state()
    s.p.stdin.close()
    ok = s.clean()
    report("launcher killed (pipe closed by the kernel)", ok, f"before[{before}] after[{s.state()}]")
    s.cleanup_force()

    # 3b/3c. real SIGKILL of a real parent process that owns the pipes. 3c is the launcher's real mode:
    # PROJECTS_PORT=0 + AGENTIC_OS_HANDOFF=stdout (the parent reads the hand-off line, prints only pid+port).
    for label, handoff in (("3b", False), ("3c", True)):
        root = Path(tempfile.mkdtemp(prefix="rdo-life-parent-"))
        (root / "tmp").mkdir()
        (root / "home").mkdir()
        port = free_port()
        parent_src = (
            "import subprocess,sys,time,os,json\n"
            "h=sys.argv[5]=='1'\n"
            "env={'PATH':'/usr/bin:/bin','HOME':sys.argv[3],'TMPDIR':sys.argv[2],'PROJECTS_PORT':'0' if h else sys.argv[4],"
            "'PROJECTS_HOST':'127.0.0.1','LANG':'en_US.UTF-8'}\n"
            "if h: env['AGENTIC_OS_HANDOFF']='stdout'\n"
            "p=subprocess.Popen([sys.argv[1],'serve','--exit-on-stdin-eof'],env=env,stdin=subprocess.PIPE,"
            "stdout=subprocess.PIPE if h else subprocess.DEVNULL,stderr=subprocess.DEVNULL,process_group=0)\n"
            "port=sys.argv[4]\n"
            "if h:\n"
            "    port=json.loads(p.stdout.readline())['port']\n"
            "print(p.pid,port,flush=True)\n"
            "time.sleep(600)\n"
        )
        parent = subprocess.Popen([sys.executable, "-c", parent_src, str(binary), str(root / "tmp"),
                                   str(root / "home"), str(port), "1" if handoff else "0"],
                                  stdout=subprocess.PIPE, text=True)
        pg, pt = parent.stdout.readline().split()
        pgid, port = int(pg), int(pt)
        wait(lambda: listening(port), 90)
        before = f"procs={len(group_pids(pgid))} listener={listening(port)} mei={len(list((root / 'tmp').glob('_MEI*')))}"
        parent.send_signal(signal.SIGKILL)
        parent.wait()
        ok = wait(lambda: not group_pids(pgid) and not listening(port) and not list((root / "tmp").glob("_MEI*")), 10)
        after = f"procs={len(group_pids(pgid))} listener={listening(port)} mei={len(list((root / 'tmp').glob('_MEI*')))}"
        report(f"{label} launcher process SIGKILLed" + (" (hand-off mode, port 0)" if handoff else ""), ok,
               f"before[{before}] after[{after}]")
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
        shutil.rmtree(root, ignore_errors=True)

    # 4. [limit] the whole group is SIGKILLed: nothing runs, but the bootloader never cleans _MEI
    s = Scenario(binary)
    before = s.state()
    os.killpg(s.pgid, signal.SIGKILL)
    wait(lambda: s.p.poll() is not None and not group_pids(s.pgid), 5)
    report("[limit] SIGKILL of the whole group (forced stop after the grace period)",
           s.clean(2), f"before[{before}] after[{s.state()}] -> _MEI is leaked by design of a hard kill",
           limit=True)
    s.cleanup_force()

    # 5. [control] old behaviour: no tether flag + SIGKILL of the bootloader only -> orphan keeps serving
    s = Scenario(binary, tether=False)
    before = s.state()
    os.kill(s.p.pid, signal.SIGKILL)
    s.p.wait()
    time.sleep(1.0)
    orphan = listening(s.port)
    report("[control] v0.3.2 stop (SIGKILL of the bootloader only)", not orphan,
           f"before[{before}] after[{s.state()}] -> orphan still serving: {orphan}", limit=True)
    s.cleanup_force()

    print("RESULT:", "PASS" if bad == 0 else f"{bad} FAILURE(S)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
