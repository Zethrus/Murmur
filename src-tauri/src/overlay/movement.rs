//! Gesture ownership is explicit: programmatic move events outside a drag never save placement.
use std::time::{Duration, Instant};

const SETTLE: Duration = Duration::from_millis(150);
pub(super) const MAX_DRAG: Duration = Duration::from_secs(300);

#[derive(Debug)]
pub struct Drag {
    pub generation: u64,
    pub cancelled: bool,
    pub initial: (i32, i32),
    started: Instant,
    moved: Instant,
    released: Option<Instant>,
    pub position: (i32, i32),
}

#[derive(Debug, PartialEq)]
pub enum Progress {
    Held,
    Settled,
    TimedOut,
}

impl Drag {
    pub fn new(position: (i32, i32), now: Instant) -> Self {
        Self {
            generation: 0,
            cancelled: false,
            initial: position,
            started: now,
            moved: now,
            released: None,
            position,
        }
    }

    pub fn moved(&mut self, position: (i32, i32), now: Instant) {
        self.position = position;
        self.moved = now;
    }

    pub fn poll(&mut self, down: bool, now: Instant) -> Progress {
        if now.duration_since(self.started) >= MAX_DRAG {
            return Progress::TimedOut;
        }
        if down {
            self.released = None;
            return Progress::Held;
        }
        let released = *self.released.get_or_insert(now);
        if now.duration_since(released) >= SETTLE && now.duration_since(self.moved) >= SETTLE {
            Progress::Settled
        } else {
            Progress::Held
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn immediate_move_is_recorded_without_a_programmatic_grace_period() {
        let now = Instant::now();
        let mut drag = Drag::new((10, 10), now);
        drag.moved((-200, 500), now + Duration::from_millis(10));
        assert_eq!(drag.position, (-200, 500));
    }

    #[test]
    fn a_stationary_held_button_never_counts_as_release() {
        let now = Instant::now();
        let mut drag = Drag::new((10, 10), now);
        assert_eq!(
            drag.poll(true, now + Duration::from_secs(2)),
            Progress::Held
        );
        assert_eq!(
            drag.poll(false, now + Duration::from_secs(3)),
            Progress::Held
        );
        assert_eq!(
            drag.poll(false, now + Duration::from_millis(3150)),
            Progress::Settled
        );
    }

    #[test]
    fn late_native_motion_must_settle_after_button_release() {
        let now = Instant::now();
        let mut drag = Drag::new((10, 10), now);
        drag.poll(false, now);
        drag.moved((200, 300), now + Duration::from_millis(140));
        assert_eq!(
            drag.poll(false, now + Duration::from_millis(160)),
            Progress::Held
        );
        assert_eq!(
            drag.poll(false, now + Duration::from_millis(300)),
            Progress::Settled
        );
    }

    #[test]
    fn repress_resets_release_and_lost_gestures_have_a_bound() {
        let now = Instant::now();
        let mut drag = Drag::new((0, 0), now);
        drag.poll(false, now);
        drag.poll(true, now + Duration::from_millis(100));
        assert_eq!(
            drag.poll(false, now + Duration::from_millis(200)),
            Progress::Held
        );
        assert_eq!(drag.poll(true, now + MAX_DRAG), Progress::TimedOut);
    }
}
