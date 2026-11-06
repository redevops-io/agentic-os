//! When to leave the deployment's page and when to come back. Pure state machine; `main.rs` only acts on it.
//!
//! A runtime that is merely down is not worth interrupting the user for after one failed probe, and it recovers by
//! itself — so three consecutive "unavailable" probes before going offline, then back off (2s, 4s … 30s) until it
//! returns. A runtime that REFUSES us (unauthenticated) or is the wrong version (incompatible) will not fix itself,
//! so those need the user at once.

use crate::probe::{State, Status};
use std::time::Duration;

pub const OFFLINE_AFTER: u32 = 3;
pub const ONLINE_INTERVAL: Duration = Duration::from_secs(10);
const BACKOFF_CAP: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    None,
    /// show the "can't reach the runtime" page (auto-retrying)
    GoOffline,
    /// the runtime is back: return to the deployment's page
    GoOnline,
    /// show the page that asks the user to act (sign-in refused / wrong version / bad address)
    NeedsAttention(State),
}

#[derive(Debug, Default)]
pub struct Monitor {
    fails: u32,
    away: bool,
}

impl Monitor {
    pub fn new() -> Self {
        Self::default()
    }

    /// `showing_deployment`: whether the window currently shows the deployment (so Ok after a failure returns to it).
    pub fn observe(&mut self, s: &Status) -> Action {
        match s.state {
            State::Ok => {
                self.fails = 0;
                if self.away {
                    self.away = false;
                    Action::GoOnline
                } else {
                    Action::None
                }
            }
            State::Unavailable => {
                self.fails += 1;
                if self.fails >= OFFLINE_AFTER && !self.away {
                    self.away = true;
                    Action::GoOffline
                } else {
                    Action::None
                }
            }
            other => {
                self.fails = 0;
                let first = !self.away;
                self.away = true;
                if first { Action::NeedsAttention(other) } else { Action::None }
            }
        }
    }

    /// The window was sent to an error page without going through `observe` (a failed first look at startup): record
    /// that we are away so the first successful probe brings the deployment back.
    pub fn mark_away(&mut self) {
        self.away = true;
    }

    #[cfg(test)]
    pub fn is_away(&self) -> bool {
        self.away
    }
    #[cfg(test)]
    pub fn failures(&self) -> u32 {
        self.fails
    }
    /// Reset after the user changes the connection.
    pub fn reset(&mut self) {
        *self = Self::default();
    }

    /// How long to wait before the next probe.
    pub fn next_delay(&self) -> Duration {
        if !self.away && self.fails == 0 {
            return ONLINE_INTERVAL;
        }
        let attempt = self.fails.max(1).min(6);
        (Duration::from_secs(2) * 2u32.pow(attempt - 1)).min(BACKOFF_CAP)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn st(state: State) -> Status {
        Status { state, detail: String::new(), project: None }
    }

    #[test]
    fn a_blip_is_ignored_an_outage_goes_offline_once_and_recovery_returns() {
        let mut m = Monitor::new();
        assert_eq!(m.observe(&st(State::Unavailable)), Action::None);
        assert_eq!(m.observe(&st(State::Ok)), Action::None);                 // recovered before the threshold
        assert_eq!(m.failures(), 0);
        assert_eq!(m.observe(&st(State::Unavailable)), Action::None);
        assert_eq!(m.observe(&st(State::Unavailable)), Action::None);
        assert_eq!(m.observe(&st(State::Unavailable)), Action::GoOffline);   // the third in a row
        assert_eq!(m.observe(&st(State::Unavailable)), Action::None);        // not re-announced every probe
        assert_eq!(m.observe(&st(State::Unavailable)), Action::None);
        assert!(m.is_away());
        assert_eq!(m.observe(&st(State::Ok)), Action::GoOnline);
        assert!(!m.is_away());
        assert_eq!(m.observe(&st(State::Ok)), Action::None);
    }

    #[test]
    fn a_refused_or_incompatible_runtime_needs_the_user_immediately_and_once() {
        for s in [State::Unauthenticated, State::Incompatible, State::Invalid] {
            let mut m = Monitor::new();
            assert_eq!(m.observe(&st(s)), Action::NeedsAttention(s));
            assert_eq!(m.observe(&st(s)), Action::None);
            assert_eq!(m.observe(&st(State::Ok)), Action::GoOnline);          // fixed elsewhere: come back
        }
    }

    #[test]
    fn backoff_grows_then_caps_and_polling_is_calm_when_healthy() {
        let mut m = Monitor::new();
        assert_eq!(m.next_delay(), ONLINE_INTERVAL);
        let mut seen = vec![];
        for _ in 0..8 {
            m.observe(&st(State::Unavailable));
            seen.push(m.next_delay().as_secs());
        }
        assert_eq!(seen, vec![2, 4, 8, 16, 30, 30, 30, 30]);
        assert!(seen.windows(2).all(|w| w[1] >= w[0]));
        m.observe(&st(State::Ok));
        assert_eq!(m.next_delay(), ONLINE_INTERVAL);
    }

    #[test]
    fn a_failed_first_look_at_startup_still_recovers_by_itself() {
        let mut m = Monitor::new();
        m.mark_away();                                                       // window showed "can't reach the runtime"
        assert_eq!(m.observe(&st(State::Unavailable)), Action::None);        // still down: stay put, no flapping
        assert_eq!(m.observe(&st(State::Ok)), Action::GoOnline);             // it came back: return to the deployment
    }

    #[test]
    fn reset_forgets_everything() {
        let mut m = Monitor::new();
        for _ in 0..5 { m.observe(&st(State::Unavailable)); }
        m.reset();
        assert!(!m.is_away());
        assert_eq!(m.failures(), 0);
    }
}
