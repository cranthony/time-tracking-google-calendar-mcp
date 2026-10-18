//! The one lock every write to Google holds, and the check that it does.
//! Tools run on worker threads, so calls that arrive together run at once:
//! reading alongside one another is fine, but a write reads, decides, then
//! writes, and two interleaved can lose one's write. So a tool that may
//! write holds `WRITE_LOCK` for its whole call, and each write to Google
//! checks this thread holds it (or `HEALTH_TAB_LOCK`, for the Health tab's
//! background writer).

use std::cell::Cell;
use std::sync::{Mutex, MutexGuard};

pub struct WriteLock {
    mutex: Mutex<()>,
    id: u8,
}

thread_local! {
    /// Which locks this thread holds, as bits of `WriteLock::id`, and how
    /// deep: the lock is reentrant within a thread.
    static HELD: Cell<(u8, u32, u32)> = const { Cell::new((0, 0, 0)) };
}

pub static WRITE_LOCK: WriteLock = WriteLock { mutex: Mutex::new(()), id: 1 };
pub static HEALTH_TAB_LOCK: WriteLock = WriteLock { mutex: Mutex::new(()), id: 2 };

pub struct Held {
    _guard: Option<MutexGuard<'static, ()>>,
    id: u8,
}

impl WriteLock {
    /// Hold the lock until the returned guard is dropped. Taking it again
    /// on the same thread just nests.
    pub fn hold(&'static self) -> Held {
        self.take(|mutex| Some(mutex.lock().unwrap_or_else(|e| e.into_inner()))).expect("lock() always takes it")
    }

    /// Hold the lock if it's free (or this thread holds it already), else
    /// `None` at once: for background work that gives way to tools.
    pub fn try_hold(&'static self) -> Option<Held> {
        self.take(|mutex| match mutex.try_lock() {
            Ok(guard) => Some(guard),
            Err(std::sync::TryLockError::Poisoned(e)) => Some(e.into_inner()),
            Err(std::sync::TryLockError::WouldBlock) => None,
        })
    }

    fn take(&'static self, lock: impl FnOnce(&'static Mutex<()>) -> Option<MutexGuard<'static, ()>>) -> Option<Held> {
        let (bits, write_depth, health_depth) = HELD.get();
        let depth = if self.id == 1 { write_depth } else { health_depth };
        let guard = if depth == 0 { Some(lock(&self.mutex)?) } else { None };
        let (write_depth, health_depth) =
            if self.id == 1 { (write_depth + 1, health_depth) } else { (write_depth, health_depth + 1) };
        HELD.set((bits | self.id, write_depth, health_depth));
        Some(Held { _guard: guard, id: self.id })
    }
}

impl Drop for Held {
    fn drop(&mut self) {
        let (mut bits, mut write_depth, mut health_depth) = HELD.get();
        if self.id == 1 {
            write_depth -= 1;
            if write_depth == 0 {
                bits &= !1;
            }
        } else {
            health_depth -= 1;
            if health_depth == 0 {
                bits &= !2;
            }
        }
        HELD.set((bits, write_depth, health_depth));
    }
}

/// Whether this thread may write to Google.
pub fn held() -> bool {
    HELD.get().0 != 0
}

/// Fail loudly when a write is attempted without the lock -- a tool that
/// writes without saying so is a bug, not a race to allow.
pub fn require(what: &str) -> anyhow::Result<()> {
    if held() {
        Ok(())
    } else {
        anyhow::bail!("{what} writes to Google, so its caller must hold WRITE_LOCK")
    }
}

#[cfg(test)]
#[path = "write_lock_tests.rs"]
mod tests;
