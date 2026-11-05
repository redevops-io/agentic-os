// Graceful exit on SIGTERM / SIGINT / SIGHUP (unix). `RunEvent::Exit` is where the launcher stops the control
// plane and deletes its redirect pages (main.rs), but the OS default action for these signals ends the process
// without running it (OBSERVED natively: SIGTERM left the redirect pages behind; the sidecar still stopped through
// its stdin tether). Here the signal is turned into an ordinary `AppHandle::exit(0)` request:
//   * the handler is async-signal-safe: it only `write(2)`s one byte (the signal number) into a non-blocking pipe;
//   * a watcher thread blocks on the read end and calls the callback (never inside the handler);
//   * no new crate: `libc` is already a unix dependency.
// SIGKILL (and a crash) cannot be handled by any program: after those the redirect pages stay until their own
// 60 s timer or the next write sweep (opener.rs), and the sidecar still stops through its stdin tether.

use std::os::raw::c_int;
use std::sync::atomic::{AtomicI32, Ordering};

/// The signals turned into a graceful exit.
pub const HANDLED: [c_int; 3] = [libc::SIGTERM, libc::SIGINT, libc::SIGHUP];

/// Write end of the self-pipe, read by the handler. -1 = not installed.
static WRITE_FD: AtomicI32 = AtomicI32::new(-1);

/// Conventional exit status for "terminated by signal" (used only by the forced fallback).
pub fn exit_code_for(sig: c_int) -> i32 {
    128 + sig
}

/// `write(2)` of the signal number (one byte) to `fd`. Async-signal-safe: no allocation, no locks.
pub fn forward(fd: c_int, sig: c_int) {
    if fd < 0 {
        return;
    }
    let byte = [sig as u8];
    // The result is ignored on purpose: a full pipe (EAGAIN) already holds a pending request.
    // SAFETY: write(2) on a valid or closed descriptor with a 1-byte stack buffer.
    let _ = unsafe { libc::write(fd, byte.as_ptr().cast(), 1) };
}

extern "C" fn on_signal(sig: c_int) {
    forward(WRITE_FD.load(Ordering::Relaxed), sig);
}

/// Read one forwarded signal number from `fd` (blocking). None on EOF or a hard error. Retries on EINTR.
pub fn read_signal(fd: c_int) -> Option<c_int> {
    let mut b = [0u8; 1];
    loop {
        // SAFETY: read(2) into a 1-byte stack buffer.
        let n = unsafe { libc::read(fd, b.as_mut_ptr().cast(), 1) };
        if n == 1 {
            return Some(c_int::from(b[0]));
        }
        if n < 0 && std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
            continue;
        }
        return None;
    }
}

fn pipe_cloexec() -> std::io::Result<(c_int, c_int)> {
    let mut fds = [0 as c_int; 2];
    // SAFETY: `fds` is a valid 2-element array.
    if unsafe { libc::pipe(fds.as_mut_ptr()) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    for fd in fds {
        // SAFETY: fcntl on descriptors we just created. CLOEXEC: the sidecar and other children never inherit them.
        unsafe {
            let fl = libc::fcntl(fd, libc::F_GETFD);
            libc::fcntl(fd, libc::F_SETFD, fl | libc::FD_CLOEXEC);
        }
    }
    // The write end must never block inside a signal handler.
    // SAFETY: as above.
    unsafe {
        let fl = libc::fcntl(fds[1], libc::F_GETFL);
        libc::fcntl(fds[1], libc::F_SETFL, fl | libc::O_NONBLOCK);
    }
    Ok((fds[0], fds[1]))
}

/// Install the handlers and start the watcher thread. `on_request(signal, count)` runs on the watcher thread for
/// every received signal (count = 1 for the first). Errors are returned; the launcher then simply keeps the OS
/// default behaviour (the previous state).
pub fn start<F>(on_request: F) -> std::io::Result<()>
where
    F: Fn(c_int, u32) + Send + 'static,
{
    let (r, w) = pipe_cloexec()?;
    WRITE_FD.store(w, Ordering::SeqCst);
    for sig in HANDLED {
        // SAFETY: a zeroed sigaction with a plain extern "C" handler, SA_RESTART, and the signal set emptied.
        unsafe {
            let mut sa: libc::sigaction = std::mem::zeroed();
            sa.sa_sigaction = on_signal as extern "C" fn(c_int) as usize;
            sa.sa_flags = libc::SA_RESTART;
            libc::sigemptyset(&mut sa.sa_mask);
            if libc::sigaction(sig, &sa, std::ptr::null_mut()) != 0 {
                return Err(std::io::Error::last_os_error());
            }
        }
    }
    std::thread::Builder::new().name("signal-watcher".into()).spawn(move || {
        let mut n = 0u32;
        while let Some(sig) = read_signal(r) {
            n += 1;
            on_request(sig, n);
        }
    })?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pipe() -> (c_int, c_int) {
        pipe_cloexec().unwrap()
    }

    #[test]
    fn forward_and_read_round_trip_the_signal_number() {
        let (r, w) = pipe();
        for s in HANDLED {
            forward(w, s);
        }
        assert_eq!([read_signal(r), read_signal(r), read_signal(r)], [Some(libc::SIGTERM), Some(libc::SIGINT), Some(libc::SIGHUP)]);
        unsafe { libc::close(w) };
        assert_eq!(read_signal(r), None, "EOF after the write end is closed");
        unsafe { libc::close(r) };
    }

    #[test]
    fn forward_to_no_descriptor_or_a_full_pipe_never_blocks_or_panics() {
        forward(-1, libc::SIGTERM);
        let (r, w) = pipe();
        for _ in 0..200_000 {
            forward(w, libc::SIGTERM); // fills the pipe buffer; the write end is non-blocking
        }
        let mut b = [0u8; 1];
        assert_eq!(unsafe { libc::read(r, b.as_mut_ptr().cast(), 1) }, 1);
        unsafe {
            libc::close(w);
            libc::close(r);
        }
    }

    #[test]
    fn both_ends_are_close_on_exec_and_the_write_end_is_non_blocking() {
        let (r, w) = pipe();
        unsafe {
            assert_ne!(libc::fcntl(r, libc::F_GETFD) & libc::FD_CLOEXEC, 0);
            assert_ne!(libc::fcntl(w, libc::F_GETFD) & libc::FD_CLOEXEC, 0);
            assert_ne!(libc::fcntl(w, libc::F_GETFL) & libc::O_NONBLOCK, 0);
            libc::close(r);
            libc::close(w);
        }
    }

    #[test]
    fn the_exit_code_follows_the_shell_convention() {
        assert_eq!(exit_code_for(libc::SIGTERM), 128 + 15);
        assert_eq!(exit_code_for(libc::SIGINT), 128 + 2);
        assert_eq!(exit_code_for(libc::SIGHUP), 128 + 1);
    }

    #[test]
    fn the_handled_set_is_term_int_hup_and_never_kill() {
        assert_eq!(HANDLED, [libc::SIGTERM, libc::SIGINT, libc::SIGHUP]);
        assert!(!HANDLED.contains(&libc::SIGKILL));
    }

    /// A real signal through the real handler: installs the handler for SIGHUP only, raises it in this thread,
    /// reads the byte from the pipe, then restores the previous disposition. (SIGTERM/SIGINT are not raised here:
    /// they are used by the test runner.)
    #[test]
    fn a_real_sighup_reaches_the_pipe_through_the_async_signal_safe_handler() {
        let (r, w) = pipe();
        let prev_fd = WRITE_FD.swap(w, Ordering::SeqCst);
        unsafe {
            let mut sa: libc::sigaction = std::mem::zeroed();
            sa.sa_sigaction = on_signal as extern "C" fn(c_int) as usize;
            sa.sa_flags = libc::SA_RESTART;
            libc::sigemptyset(&mut sa.sa_mask);
            let mut old: libc::sigaction = std::mem::zeroed();
            assert_eq!(libc::sigaction(libc::SIGHUP, &sa, &mut old), 0);
            libc::raise(libc::SIGHUP);
            assert_eq!(read_signal(r), Some(libc::SIGHUP));
            assert_eq!(libc::sigaction(libc::SIGHUP, &old, std::ptr::null_mut()), 0);
            libc::close(r);
            libc::close(w);
        }
        WRITE_FD.store(prev_fd, Ordering::SeqCst);
    }

    #[test]
    fn the_handler_only_writes_no_allocation_or_locks() {
        let src = include_str!("signals.rs");
        let handler = src.split("extern \"C\" fn on_signal").nth(1).unwrap().split("\n}\n").next().unwrap();
        assert!(handler.contains("forward(") && !handler.contains("Box") && !handler.contains("Mutex") && !handler.contains("println") && !handler.contains("format!"));
        let fwd = src.split("pub fn forward").nth(1).unwrap().split("\n}\n").next().unwrap();
        assert!(!fwd.contains("Vec") && !fwd.contains("String") && !fwd.contains("Mutex") && !fwd.contains("format!"));
    }
}
