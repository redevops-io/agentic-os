// Sidecar lifecycle: start it as ONE killable unit, stop the whole tree, never adopt a stray listener.
//
// Why this exists (R0-prime defect b). The sidecar is a PyInstaller ONEFILE executable, i.e. TWO
// processes: a bootloader (the PID we spawn) and the Python child that owns port 8787. The old
// launcher only `Child::kill()`ed the bootloader (SIGKILL / TerminateProcess, both uncatchable),
// so the Python child kept serving, `_MEI*` leaked, and the next start talked to the stale process.
//
// Mechanisms (all of them, because none is sufficient on every platform):
//   1. Own process group (Unix: `process_group(0)`) or a Windows Job Object with
//      JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: the tree can be signalled / killed as a unit, and on
//      Windows it dies with the launcher even if the launcher crashes.
//   2. Graceful stop: SIGTERM to the process group (Unix); closing the stdin tether (all
//      platforms, see 3); wait up to GRACE; then SIGKILL to the group / TerminateJobObject.
//   3. Stdin tether: the launcher keeps the write end of the sidecar's stdin pipe open. The sidecar
//      is started with `serve --exit-on-stdin-eof` and shuts uvicorn down cleanly when the pipe
//      reaches EOF, i.e. when the launcher closes it OR dies for any reason (SIGKILL, crash,
//      power-off of the GUI). A clean uvicorn exit lets the PyInstaller bootloader delete `_MEI*`.
//      It needs no console API, so it is also the graceful channel on Windows, where a GUI-subsystem
//      launcher has no console to send CTRL_BREAK on.
//   4. No adoption: the sidecar binds an OS-chosen port (PROJECTS_PORT=0) and the launcher proves it
//      is that process with the capability handshake (src/capability.rs, src/launch.rs); a stale or
//      foreign listener can neither take the port nor answer the handshake.
//
// Windows paths are written against the windows-sys API but have NOT been run: see the UNVERIFIED
// notes and the test plan in the R0-prime defects report.

use std::io;
#[cfg(test)]
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

/// How long a graceful stop may take before the tree is killed.
pub const GRACE: Duration = Duration::from_secs(3);
const POLL: Duration = Duration::from_millis(25);

/// What a port probe found (test helper: the product no longer pre-probes, see header note 4).
#[cfg(test)]
#[derive(Debug)]
pub enum PortState {
    Free,
    /// Something accepts connections there (possibly a stale ReDevOps control plane).
    Listening,
    /// Cannot connect, but binding fails too (port reserved/in use/denied); carries the bind error.
    #[allow(dead_code)]
    Unavailable(io::Error),
}

/// Look for an existing listener on `addr`.
#[cfg(test)]
pub fn probe_port(addr: SocketAddr) -> PortState {
    if TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_ok() {
        return PortState::Listening;
    }
    match TcpListener::bind(addr) {
        Ok(l) => {
            drop(l);
            PortState::Free
        }
        Err(e) => PortState::Unavailable(e),
    }
}

/// How a stop ended (for the message shown in the UI).
#[derive(Debug, PartialEq, Eq)]
pub enum StopOutcome {
    /// The tree exited by itself inside the grace period.
    Graceful,
    /// The grace period expired and the tree was killed.
    Forced,
}

/// The sidecar process tree, owned by the launcher.
pub struct ManagedChild {
    child: Child,
    /// Write end of the sidecar's stdin: the lifetime tether. Dropping it requests a clean stop.
    stdin: Option<ChildStdin>,
    #[cfg(windows)]
    job: Option<win::Job>,
}

impl ManagedChild {
    /// Spawn `cmd` as its own killable unit. The caller sets program, args, env, stdout, stderr;
    /// this sets stdin (piped tether), the process group / Job Object and the console flags.
    pub fn spawn(cmd: &mut Command) -> io::Result<ManagedChild> {
        cmd.stdin(Stdio::piped());
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            cmd.process_group(0); // new group, pgid == child pid
        }
        #[cfg(windows)]
        hide_console(cmd);
        let mut child = cmd.spawn()?;
        let stdin = child.stdin.take();
        #[cfg(windows)]
        let job = {
            use std::os::windows::io::AsRawHandle;
            // Assigned right after spawn. The bootloader needs well over a millisecond (onefile
            // extraction) before it creates the Python child, which then inherits the job; if
            // the assignment fails we fall back to `taskkill /T /F` when stopping.
            win::Job::new_kill_on_close()
                .and_then(|j| j.assign(child.as_raw_handle()).map(|_| j))
                .ok()
        };
        Ok(ManagedChild {
            child,
            stdin,
            #[cfg(windows)]
            job,
        })
    }

    /// Take the piped stdout / stderr (the caller sets `Stdio::piped()` before `spawn`).
    pub fn take_stdout(&mut self) -> Option<std::process::ChildStdout> {
        self.child.stdout.take()
    }
    pub fn take_stderr(&mut self) -> Option<std::process::ChildStderr> {
        self.child.stderr.take()
    }

    /// Exit status if the leader already exited (non-blocking).
    pub fn try_exit_status(&mut self) -> Option<std::process::ExitStatus> {
        self.child.try_wait().ok().flatten()
    }

    /// PID of the bootloader (also the process-group id on Unix).
    #[allow(dead_code)] // used by the tests (and by callers that want the PID for logging)
    pub fn id(&self) -> u32 {
        self.child.id()
    }

    /// True while the process WE spawned is alive. (A foreign listener never counts as running.)
    pub fn is_running(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(None))
    }

    /// Stop the whole tree: ask politely, wait up to `grace`, then kill. Always reaps the child.
    pub fn stop(mut self, grace: Duration) -> StopOutcome {
        // Tether: EOF on the sidecar's stdin is the graceful-shutdown request on every platform.
        drop(self.stdin.take());
        #[cfg(unix)]
        unix::signal_group(self.child.id(), libc::SIGTERM);

        let deadline = Instant::now() + grace;
        let mut graceful = false;
        loop {
            let leader_gone = !matches!(self.child.try_wait(), Ok(None));
            if leader_gone && self.tree_gone() {
                graceful = true;
                break;
            }
            if Instant::now() >= deadline {
                break;
            }
            sleep(POLL);
        }
        if graceful {
            // Unix: SIGKILL anything still in the group; Windows: terminate stragglers via the job.
            self.kill_tree_stragglers();
        } else {
            self.kill_tree();
        }
        let _ = self.child.wait(); // reap (no-op when already reaped)
        if graceful {
            StopOutcome::Graceful
        } else {
            StopOutcome::Forced
        }
    }

    // ---- platform pieces -------------------------------------------------------------------

    #[cfg(unix)]
    fn tree_gone(&self) -> bool {
        unix::group_gone(self.child.id())
    }
    #[cfg(unix)]
    fn kill_tree(&mut self) {
        unix::signal_group(self.child.id(), libc::SIGKILL);
        let _ = self.child.kill();
    }
    #[cfg(unix)]
    fn kill_tree_stragglers(&mut self) {
        if !unix::group_gone(self.child.id()) {
            unix::signal_group(self.child.id(), libc::SIGKILL);
        }
    }

    #[cfg(windows)]
    fn tree_gone(&self) -> bool {
        // The bootloader waits for its Python child, so its exit means the tree is gone; stragglers
        // (if any) are killed with the job in kill_tree_stragglers().
        true
    }
    #[cfg(windows)]
    fn kill_tree(&mut self) {
        match &self.job {
            Some(job) => job.terminate(),
            None => {
                // Fallback when the Job Object could not be set up: tree kill by PID.
                let _ = Command::new("taskkill")
                    .args(["/PID", &self.child.id().to_string(), "/T", "/F"])
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status();
            }
        }
        let _ = self.child.kill();
    }
    #[cfg(windows)]
    fn kill_tree_stragglers(&mut self) {
        if let Some(job) = &self.job {
            job.terminate();
        }
    }
}

/// Keep console-subsystem children from flashing a console window (the launcher is a GUI process).
#[cfg(windows)]
pub fn hide_console(cmd: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    cmd.creation_flags(CREATE_NO_WINDOW);
}
#[cfg(not(windows))]
pub fn hide_console(_cmd: &mut Command) {}

#[cfg(unix)]
mod unix {
    /// Send `sig` to the process group whose id is `pgid` (== the bootloader PID). No-op for
    /// pgid <= 1 so a bug can never signal "everything" (kill(-1)) or the caller's own group.
    pub fn signal_group(pgid: u32, sig: i32) {
        if pgid > 1 {
            unsafe {
                libc::kill(-(pgid as i32), sig);
            }
        }
    }

    /// True when no process is left in the group.
    pub fn group_gone(pgid: u32) -> bool {
        if pgid <= 1 {
            return true;
        }
        let r = unsafe { libc::kill(-(pgid as i32), 0) };
        r == -1 && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
    }
}

#[cfg(windows)]
mod win {
    use std::ffi::c_void;
    use std::io;
    use std::mem::{size_of, zeroed};
    use std::os::windows::io::RawHandle;
    use std::ptr::null;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    /// A Job Object that kills every process in it when its last handle closes (launcher exit or
    /// crash) or on `terminate()`.
    pub struct Job(HANDLE);
    // A job handle is just a kernel handle; it may be used from any thread.
    unsafe impl Send for Job {}

    impl Job {
        pub fn new_kill_on_close() -> io::Result<Job> {
            unsafe {
                let h = CreateJobObjectW(null(), null());
                if h.is_null() {
                    return Err(io::Error::last_os_error());
                }
                let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = zeroed();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let ok = SetInformationJobObject(
                    h,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const c_void,
                    size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                );
                if ok == 0 {
                    let e = io::Error::last_os_error();
                    CloseHandle(h);
                    return Err(e);
                }
                Ok(Job(h))
            }
        }

        pub fn assign(&self, process: RawHandle) -> io::Result<()> {
            let ok = unsafe { AssignProcessToJobObject(self.0, process as HANDLE) };
            if ok == 0 {
                Err(io::Error::last_os_error())
            } else {
                Ok(())
            }
        }

        pub fn terminate(&self) {
            unsafe {
                TerminateJobObject(self.0, 1);
            }
        }
    }

    impl Drop for Job {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn loopback(port: u16) -> SocketAddr {
        SocketAddr::from(([127, 0, 0, 1], port))
    }

    #[test]
    fn probe_reports_free_then_listening() {
        // Reserve an ephemeral port, release it, expect Free; hold it, expect Listening.
        let l = TcpListener::bind(loopback(0)).unwrap();
        let addr = l.local_addr().unwrap();
        assert!(matches!(probe_port(addr), PortState::Listening));
        drop(l);
        // Other tests fork children; a child between fork() and exec() can briefly hold a copy of the
        // listener, so allow a moment for the port to become free.
        let end = Instant::now() + Duration::from_secs(3);
        let mut state = probe_port(addr);
        while !matches!(state, PortState::Free) && Instant::now() < end {
            sleep(POLL);
            state = probe_port(addr);
        }
        assert!(matches!(state, PortState::Free), "got {state:?}");
    }

    #[cfg(unix)]
    mod unix_tree {
        use super::*;
        use std::io::{BufRead, BufReader};

        fn alive(pid: i32) -> bool {
            unsafe { libc::kill(pid, 0) == 0 }
        }

        /// `sh` that starts a grandchild `sleep` (like the Python child behind the bootloader),
        /// prints its PID, and waits. Returns (managed, grandchild pid).
        fn spawn_tree(script: &str) -> (ManagedChild, i32) {
            let mut cmd = Command::new("sh");
            cmd.arg("-c").arg(script).stdout(Stdio::piped()).stderr(Stdio::null());
            let mut m = ManagedChild::spawn(&mut cmd).expect("spawn");
            let out = m.child.stdout.take().unwrap();
            let mut line = String::new();
            BufReader::new(out).read_line(&mut line).unwrap();
            let gc: i32 = line.trim().parse().expect("grandchild pid");
            assert!(alive(gc) && m.is_running());
            (m, gc)
        }

        fn wait_dead(pid: i32) -> bool {
            let end = Instant::now() + Duration::from_secs(3);
            while Instant::now() < end {
                if !alive(pid) {
                    return true;
                }
                sleep(POLL);
            }
            false
        }

        #[test]
        fn the_child_is_its_own_process_group_leader() {
            let (m, gc) = spawn_tree("sleep 300 & echo $!; wait");
            let pid = m.id() as i32;
            assert_eq!(unsafe { libc::getpgid(pid) }, pid, "bootloader leads its own group");
            assert_eq!(unsafe { libc::getpgid(gc) }, pid, "grandchild is in the same group");
            assert_ne!(unsafe { libc::getpgid(0) }, pid, "and that group is not the launcher's");
            m.stop(GRACE);
        }

        #[test]
        fn stop_terminates_leader_and_grandchild_gracefully() {
            let (m, gc) = spawn_tree("sleep 300 & echo $!; wait");
            let pid = m.id() as i32;
            let t0 = Instant::now();
            assert_eq!(m.stop(GRACE), StopOutcome::Graceful);
            assert!(t0.elapsed() < GRACE, "SIGTERM must not need the full grace period");
            assert!(wait_dead(gc), "the grandchild must not survive the stop");
            assert!(unix::group_gone(pid as u32), "no process left in the group");
        }

        #[test]
        fn stop_escalates_to_sigkill_when_sigterm_is_ignored() {
            // Both shell and grandchild ignore TERM; only the escalation can end them.
            let (m, gc) = spawn_tree("trap '' TERM; (trap '' TERM; exec sleep 300) & echo $!; while :; do sleep 1; done");
            let pid = m.id() as i32;
            let t0 = Instant::now();
            assert_eq!(m.stop(Duration::from_millis(400)), StopOutcome::Forced);
            assert!(t0.elapsed() >= Duration::from_millis(400));
            assert!(wait_dead(gc), "the SIGKILL escalation must take the grandchild too");
            assert!(unix::group_gone(pid as u32));
        }

        #[test]
        fn dropping_the_stdin_tether_is_seen_by_the_child() {
            // The child exits when its stdin reaches EOF: this is the sidecar's
            // `--exit-on-stdin-eof` contract, here with `cat` standing in for it.
            let mut cmd = Command::new("sh");
            cmd.arg("-c").arg("cat >/dev/null").stdout(Stdio::null()).stderr(Stdio::null());
            let mut m = ManagedChild::spawn(&mut cmd).expect("spawn");
            assert!(m.is_running());
            drop(m.stdin.take());
            let end = Instant::now() + Duration::from_secs(3);
            while m.is_running() && Instant::now() < end {
                sleep(POLL);
            }
            assert!(!m.is_running(), "EOF on the tether must end a tether-aware child");
        }

        #[test]
        fn is_running_is_false_after_the_child_exits_by_itself() {
            let mut cmd = Command::new("sh");
            cmd.arg("-c").arg("exit 0").stdout(Stdio::null()).stderr(Stdio::null());
            let mut m = ManagedChild::spawn(&mut cmd).expect("spawn");
            let end = Instant::now() + Duration::from_secs(3);
            while m.is_running() && Instant::now() < end {
                sleep(POLL);
            }
            assert!(!m.is_running());
            assert_eq!(m.stop(GRACE), StopOutcome::Graceful);
        }

        #[test]
        fn signal_group_never_targets_group_zero_one_or_all() {
            // Guard rails: these must be no-ops (and must not kill the test process).
            unix::signal_group(0, libc::SIGTERM);
            unix::signal_group(1, libc::SIGTERM);
            assert!(unix::group_gone(0) && unix::group_gone(1));
        }
    }

    /// UNVERIFIED on a real Windows host (compile-checked only: cargo check --tests --target
    /// x86_64-pc-windows-msvc). Starts a Job-assigned PowerShell that spawns a long-running
    /// grandchild (`ping -n 300`), then checks the whole tree is gone after `stop`. PowerShell does
    /// not watch stdin, so this exercises the forced path (TerminateJobObject), not the tether.
    #[cfg(windows)]
    #[test]
    fn windows_stop_kills_the_grandchild_through_the_job_object() {
        use std::io::{BufRead, BufReader};
        fn alive(pid: u32) -> bool {
            let out = Command::new("tasklist")
                .args(["/FI", &format!("PID eq {pid}"), "/NH"])
                .output()
                .unwrap();
            String::from_utf8_lossy(&out.stdout).contains(&pid.to_string())
        }
        let mut cmd = Command::new("powershell");
        cmd.args([
            "-NoProfile",
            "-Command",
            "$p = Start-Process -PassThru -WindowStyle Hidden ping -ArgumentList '-n','300','127.0.0.1'; $p.Id; Start-Sleep 300",
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
        let mut m = ManagedChild::spawn(&mut cmd).expect("spawn");
        let mut line = String::new();
        BufReader::new(m.child.stdout.take().unwrap()).read_line(&mut line).unwrap();
        let gc: u32 = line.trim().parse().expect("grandchild pid");
        assert!(alive(gc), "grandchild must be running before the stop");
        assert_eq!(m.stop(Duration::from_millis(500)), StopOutcome::Forced);
        let end = Instant::now() + Duration::from_secs(5);
        while alive(gc) && Instant::now() < end {
            sleep(Duration::from_millis(100));
        }
        assert!(!alive(gc), "the job object must take the grandchild down with the leader");
    }

    /// Real frozen sidecar, only when RDO_SIDECAR points at it:
    /// `RDO_SIDECAR=/path/redevops-sidecar cargo test -- --ignored real_sidecar`.
    #[cfg(unix)]
    #[test]
    #[ignore]
    fn real_sidecar_stops_cleanly_with_no_listener_left() {
        let bin = std::env::var("RDO_SIDECAR").expect("set RDO_SIDECAR");
        let port: u16 = 18947;
        let addr = loopback(port);
        assert!(matches!(probe_port(addr), PortState::Free), "port must start free");
        let mut cmd = Command::new(bin);
        cmd.args(["serve", "--exit-on-stdin-eof"])
            .env("PROJECTS_PORT", port.to_string())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        let m = ManagedChild::spawn(&mut cmd).expect("spawn sidecar");
        let end = Instant::now() + Duration::from_secs(60);
        while !matches!(probe_port(addr), PortState::Listening) {
            assert!(Instant::now() < end, "sidecar never listened");
            sleep(Duration::from_millis(200));
        }
        let pid = m.id();
        assert_eq!(m.stop(GRACE), StopOutcome::Graceful);
        assert!(unix::group_gone(pid), "bootloader and Python child are both gone");
        assert!(matches!(probe_port(addr), PortState::Free), "port closed");
    }
}
