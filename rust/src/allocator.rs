//! Keeping glibc's malloc from holding on to what the server frees. A burst
//! of tool calls (the app's refresh sends a dozen at once) runs each on its
//! own blocking thread, and glibc gives each thread an arena of its own,
//! which keeps what it frees ("Allocator: free but held" in get_health's
//! memory parts): 66 -> 221 MiB on one burst, nearly all of it free. So the
//! server caps the arenas at 2 as it starts (cap_arenas), and hands what's
//! free back to the system once no tool call is in flight (CALLS, Calls),
//! at most every TRIM_EVERY_MS. Elsewhere than Linux/glibc, neither does
//! anything.

use std::sync::Mutex;

/// The most arenas glibc makes (M_ARENA_MAX); MALLOC_ARENA_MAX, if set,
/// wins.
pub const ARENAS: i32 = 2;
/// The least time between two trims.
pub const TRIM_EVERY_MS: i64 = 10_000;

/// The server's tool calls in flight (see Calls).
pub static CALLS: Calls = Calls::new();

#[cfg(test)]
thread_local! {
    /// The unit tests' trims: counted here rather than made.
    pub static TEST_TRIMS: std::cell::Cell<Option<u32>> = const { std::cell::Cell::new(None) };
    /// The unit tests' clock, as set.
    pub static TEST_NOW_MS: std::cell::Cell<Option<i64>> = const { std::cell::Cell::new(None) };
}

/// The tool calls in flight, and when the allocator was last trimmed: as
/// the last call in flight ends, it trims, unless it did less than
/// TRIM_EVERY_MS ago -- then once that's passed, if still none is in flight
/// (a burst's calls end one by one, and its last is what leaves the most
/// to give back).
pub struct Calls {
    state: Mutex<State>,
}

struct State {
    in_flight: usize,
    last_trim_ms: Option<i64>,
    /// A trim waits for TRIM_EVERY_MS to pass.
    waiting: bool,
}

/// What to do as a call ends, or a trim's wait is over.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Trim {
    Now,
    /// In this many ms, if still none is in flight (see Calls::after_wait).
    After(i64),
    No,
}

impl Calls {
    pub const fn new() -> Self {
        Calls { state: Mutex::new(State { in_flight: 0, last_trim_ms: None, waiting: false }) }
    }

    /// A call starts: it's in flight until what this gives back is dropped,
    /// which then trims, or has a trim wait, as `finished` says.
    pub fn start(&'static self) -> InFlight {
        self.lock().in_flight += 1;
        InFlight { calls: self }
    }

    pub fn in_flight(&self) -> usize {
        self.lock().in_flight
    }

    /// A call ends at `now_ms`: whether to trim.
    pub fn finished(&self, now_ms: i64) -> Trim {
        let mut state = self.lock();
        state.in_flight = state.in_flight.saturating_sub(1);
        if state.in_flight > 0 {
            return Trim::No;
        }
        match state.last_trim_ms {
            Some(last) if now_ms - last < TRIM_EVERY_MS => {
                if std::mem::replace(&mut state.waiting, true) {
                    Trim::No
                } else {
                    Trim::After(last + TRIM_EVERY_MS - now_ms)
                }
            }
            _ => {
                state.last_trim_ms = Some(now_ms);
                Trim::Now
            }
        }
    }

    /// A trim's wait (Trim::After) is over at `now_ms`: trim, if none is in
    /// flight (one that is trims, or waits, as it ends).
    pub fn after_wait(&self, now_ms: i64) -> Trim {
        let mut state = self.lock();
        state.waiting = false;
        if state.in_flight > 0 || state.last_trim_ms.is_some_and(|last| now_ms - last < TRIM_EVERY_MS) {
            return Trim::No;
        }
        state.last_trim_ms = Some(now_ms);
        Trim::Now
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }
}

impl Default for Calls {
    fn default() -> Self {
        Self::new()
    }
}

/// A call in flight (Calls::start), until dropped.
pub struct InFlight {
    calls: &'static Calls,
}

impl Drop for InFlight {
    fn drop(&mut self) {
        match self.calls.finished(now_ms()) {
            Trim::Now => trim(),
            Trim::After(ms) => wait_then_trim(self.calls, ms),
            Trim::No => {}
        }
    }
}

/// Trims in `ms`, on the runtime this runs on -- if none, the next call to
/// end trims instead.
fn wait_then_trim(calls: &'static Calls, ms: i64) {
    let Ok(runtime) = tokio::runtime::Handle::try_current() else {
        calls.lock().waiting = false;
        return;
    };
    runtime.spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(ms.max(0) as u64)).await;
        if calls.after_wait(now_ms()) == Trim::Now {
            let _ = tokio::task::spawn_blocking(trim).await;
        }
    });
}

fn now_ms() -> i64 {
    #[cfg(test)]
    if let Some(ms) = TEST_NOW_MS.with(std::cell::Cell::get) {
        return ms;
    }
    chrono::Utc::now().timestamp_millis()
}

/// Hands what the allocator holds free back to the system.
pub fn trim() {
    #[cfg(test)]
    if let Some(trims) = TEST_TRIMS.with(std::cell::Cell::get) {
        TEST_TRIMS.with(|c| c.set(Some(trims + 1)));
        return;
    }
    if let Some(malloc_trim) = glibc::malloc_trim() {
        // SAFETY: malloc_trim(0) only gives back free memory.
        unsafe { malloc_trim(0) };
    }
}

/// Caps glibc's arenas at ARENAS, unless MALLOC_ARENA_MAX says otherwise.
/// Before the runtime starts its threads.
pub fn cap_arenas() {
    if std::env::var_os("MALLOC_ARENA_MAX").is_some() {
        return;
    }
    if let Some(mallopt) = glibc::mallopt() {
        // SAFETY: mallopt only sets the allocator's tuning.
        if unsafe { mallopt(glibc::M_ARENA_MAX, ARENAS) } != 1 {
            tracing::warn!("Couldn't cap the allocator's arenas at {ARENAS}");
        }
    }
}

/// glibc's mallopt and malloc_trim, looked up when first needed (as
/// health::glibc_heap's mallinfo2 is), so a C library without them gives
/// none rather than failing to start.
#[cfg(all(target_os = "linux", target_env = "gnu"))]
pub mod glibc {
    use std::ffi::{c_char, c_int, c_void};
    use std::sync::OnceLock;

    /// mallopt's parameter for the most arenas (malloc.h).
    pub const M_ARENA_MAX: c_int = -8;

    pub type MalloptFn = unsafe extern "C" fn(c_int, c_int) -> c_int;
    pub type MallocTrimFn = unsafe extern "C" fn(usize) -> c_int;

    extern "C" {
        fn dlsym(handle: *mut c_void, symbol: *const c_char) -> *mut c_void;
    }

    fn lookup(symbol: &std::ffi::CStr) -> Option<*mut c_void> {
        // SAFETY: RTLD_DEFAULT (null) searches the loaded objects.
        let found = unsafe { dlsym(std::ptr::null_mut(), symbol.as_ptr()) };
        (!found.is_null()).then_some(found)
    }

    pub fn mallopt() -> Option<MalloptFn> {
        static FOUND: OnceLock<Option<MalloptFn>> = OnceLock::new();
        // SAFETY: the symbol, if there, is mallopt, of this signature.
        *FOUND.get_or_init(|| lookup(c"mallopt").map(|f| unsafe { std::mem::transmute::<*mut c_void, MalloptFn>(f) }))
    }

    pub fn malloc_trim() -> Option<MallocTrimFn> {
        static FOUND: OnceLock<Option<MallocTrimFn>> = OnceLock::new();
        // SAFETY: the symbol, if there, is malloc_trim, of this signature.
        *FOUND.get_or_init(|| lookup(c"malloc_trim").map(|f| unsafe { std::mem::transmute::<*mut c_void, MallocTrimFn>(f) }))
    }
}

#[cfg(not(all(target_os = "linux", target_env = "gnu")))]
pub mod glibc {
    pub const M_ARENA_MAX: i32 = -8;

    pub type MalloptFn = unsafe extern "C" fn(i32, i32) -> i32;
    pub type MallocTrimFn = unsafe extern "C" fn(usize) -> i32;

    pub fn mallopt() -> Option<MalloptFn> {
        None
    }

    pub fn malloc_trim() -> Option<MallocTrimFn> {
        None
    }
}

#[cfg(test)]
#[path = "allocator_tests.rs"]
mod tests;
