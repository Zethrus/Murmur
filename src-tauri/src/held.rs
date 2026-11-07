//! A transcript held back because focus changed during dictation.
//!
//! Memory only: held text is never logged, written to disk, or recorded in history until the
//! user inserts or copies it. One transcript is held at a time; a new dictation, Discard, or
//! expiry clears it. The controller thread is the only writer; UI threads read snapshots.
use serde_json::{json, Value};
use std::{
    sync::{Mutex, MutexGuard},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

pub const HOLD_FOR: Duration = Duration::from_secs(120);
pub const NOTICE_FOR: Duration = Duration::from_secs(4);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Held {
    pub id: u64,
    /// Engine transcript before post-processing; re-processed for the destination app.
    pub raw: String,
    /// Post-processed for the original window, shown in the overlay.
    pub preview: String,
    pub force_code: bool,
    /// Application class of the window dictation started in, when known.
    pub origin: Option<String>,
    pub duration_ms: u64,
    pub message: Option<String>,
    expires: Instant,
}

#[derive(Debug, Default)]
enum Slot {
    #[default]
    Empty,
    Held(Held),
    Notice {
        message: String,
        until: Instant,
    },
}

#[derive(Debug, Default)]
struct Inner {
    slot: Slot,
    next_id: u64,
}

/// Everything needed to hold a transcript, gathered by the controller.
pub struct Pending {
    pub raw: String,
    pub preview: String,
    pub force_code: bool,
    pub origin: Option<String>,
    pub duration_ms: u64,
}

#[derive(Debug, Default)]
pub struct HeldStore {
    inner: Mutex<Inner>,
}

impl HeldStore {
    fn lock(&self) -> MutexGuard<'_, Inner> {
        // A poisoned lock still holds a valid slot; recover it rather than lose the transcript.
        self.inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Hold a transcript, replacing anything shown before. Returns its id.
    pub fn hold(&self, pending: Pending, now: Instant) -> u64 {
        let mut inner = self.lock();
        inner.next_id += 1;
        let id = inner.next_id;
        inner.slot = Slot::Held(Held {
            id,
            raw: pending.raw,
            preview: pending.preview,
            force_code: pending.force_code,
            origin: pending.origin,
            duration_ms: pending.duration_ms,
            message: None,
            expires: now + HOLD_FOR,
        });
        id
    }

    /// Show a short message without held text (for example, after a copy).
    pub fn notice(&self, message: &str, now: Instant) {
        self.lock().slot = Slot::Notice {
            message: message.into(),
            until: now + NOTICE_FOR,
        };
    }

    /// Id of the held transcript, for shortcuts and Flipper buttons pressed now.
    pub fn current_id(&self) -> Option<u64> {
        match &self.lock().slot {
            Slot::Held(held) => Some(held.id),
            _ => None,
        }
    }

    pub fn get(&self, id: u64) -> Option<Held> {
        match &self.lock().slot {
            Slot::Held(held) if held.id == id => Some(held.clone()),
            _ => None,
        }
    }

    /// Attach a message to the held transcript, for example after a refused insert.
    pub fn set_message(&self, id: u64, message: &str) -> bool {
        match &mut self.lock().slot {
            Slot::Held(held) if held.id == id => {
                held.message = Some(message.into());
                true
            }
            _ => false,
        }
    }

    /// Remove the held transcript if it is still `id`. Returns whether anything changed.
    pub fn discard(&self, id: u64) -> bool {
        let mut inner = self.lock();
        if matches!(&inner.slot, Slot::Held(held) if held.id == id) {
            inner.slot = Slot::Empty;
            true
        } else {
            false
        }
    }

    /// Forget held text and notices. Returns whether anything changed.
    pub fn clear(&self) -> bool {
        let mut inner = self.lock();
        let changed = !matches!(inner.slot, Slot::Empty);
        inner.slot = Slot::Empty;
        changed
    }

    /// Expire held text and notices. Returns whether anything changed.
    pub fn tick(&self, now: Instant) -> bool {
        let mut inner = self.lock();
        let expired = match &inner.slot {
            Slot::Held(held) => now >= held.expires,
            Slot::Notice { until, .. } => now >= *until,
            Slot::Empty => false,
        };
        if expired {
            inner.slot = Slot::Empty;
        }
        expired
    }

    /// Held text and notices keep the overlay visible even when the idle view is hidden.
    pub fn holds_overlay(&self) -> bool {
        !matches!(self.lock().slot, Slot::Empty)
    }

    /// Overlay view: `null`, a notice, or the held transcript with its expiry in Unix ms.
    pub fn snapshot(&self, now: Instant) -> Value {
        match &self.lock().slot {
            Slot::Empty => Value::Null,
            Slot::Notice { message, .. } => json!({
                "kind": "notice",
                "id": null,
                "text": null,
                "origin_app": null,
                "message": message,
                "expires_at": null,
            }),
            Slot::Held(held) => json!({
                "kind": "held",
                "id": held.id.to_string(),
                "text": held.preview,
                "origin_app": held.origin,
                "message": held.message,
                "expires_at": unix_ms(held.expires, now),
            }),
        }
    }
}

fn unix_ms(at: Instant, now: Instant) -> u64 {
    let wall = SystemTime::now() + at.saturating_duration_since(now);
    wall.duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pending(raw: &str) -> Pending {
        Pending {
            raw: raw.into(),
            preview: format!("{raw}."),
            force_code: false,
            origin: Some("obsidian".into()),
            duration_ms: 1200,
        }
    }

    #[test]
    fn held_text_is_addressed_by_id() {
        let store = HeldStore::default();
        let now = Instant::now();
        let first = store.hold(pending("one"), now);
        let second = store.hold(pending("two"), now);
        assert_ne!(first, second);
        assert_eq!(store.current_id(), Some(second));
        assert!(store.get(first).is_none(), "replaced text is gone");
        assert!(!store.discard(first), "stale id cannot discard newer text");
        assert_eq!(store.get(second).unwrap().raw, "two");
        assert!(store.discard(second));
        assert_eq!(store.current_id(), None);
        assert!(!store.holds_overlay());
    }

    #[test]
    fn held_text_expires_after_two_minutes() {
        let store = HeldStore::default();
        let now = Instant::now();
        store.hold(pending("note"), now);
        assert!(!store.tick(now + HOLD_FOR - Duration::from_millis(1)));
        assert!(store.holds_overlay());
        assert!(store.tick(now + HOLD_FOR));
        assert_eq!(store.current_id(), None);
        assert_eq!(store.snapshot(now), Value::Null);
    }

    #[test]
    fn notice_has_no_actionable_id_and_expires() {
        let store = HeldStore::default();
        let now = Instant::now();
        store.notice("Copied", now);
        assert_eq!(store.current_id(), None);
        let snapshot = store.snapshot(now);
        assert_eq!(snapshot["kind"], "notice");
        assert!(snapshot["text"].is_null());
        assert!(store.tick(now + NOTICE_FOR));
        assert!(!store.holds_overlay());
    }

    #[test]
    fn clear_forgets_everything_and_reports_change() {
        let store = HeldStore::default();
        assert!(!store.clear());
        store.hold(pending("note"), Instant::now());
        assert!(store.clear());
        assert_eq!(store.current_id(), None);
    }

    #[test]
    fn snapshot_shows_preview_origin_and_message() {
        let store = HeldStore::default();
        let now = Instant::now();
        let id = store.hold(pending("note"), now);
        assert!(store.set_message(id, "Focus the app you want first."));
        let snapshot = store.snapshot(now);
        assert_eq!(snapshot["kind"], "held");
        assert_eq!(snapshot["id"], id.to_string());
        assert_eq!(snapshot["text"], "note.");
        assert_eq!(snapshot["origin_app"], "obsidian");
        assert_eq!(snapshot["message"], "Focus the app you want first.");
        assert!(snapshot["expires_at"].as_u64().unwrap() > 0);
    }
}
