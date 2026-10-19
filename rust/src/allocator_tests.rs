use super::*;

fn calls() -> &'static Calls {
    Box::leak(Box::new(Calls::new()))
}

/// Counts trims rather than making them, from `now_ms`.
fn counting(now_ms: i64) {
    TEST_TRIMS.with(|c| c.set(Some(0)));
    TEST_NOW_MS.with(|c| c.set(Some(now_ms)));
}

fn trims() -> u32 {
    TEST_TRIMS.with(std::cell::Cell::get).unwrap()
}

#[test]
fn the_last_call_in_flight_trims_as_it_ends() {
    counting(1_000);
    let calls = calls();
    let first = calls.start();
    let second = calls.start();
    assert_eq!(calls.in_flight(), 2);
    drop(first);
    assert_eq!((calls.in_flight(), trims()), (1, 0));
    drop(second);
    assert_eq!((calls.in_flight(), trims()), (0, 1));
}

#[test]
fn a_trim_waits_until_the_last_was_long_enough_ago() {
    let calls = calls();
    assert_eq!(calls.finished_after_start(0), Trim::Now);
    // Ended 4 s after that trim: one wait, the rest of the 10 s.
    assert_eq!(calls.finished_after_start(4_000), Trim::After(6_000));
    // Already waiting: no other.
    assert_eq!(calls.finished_after_start(5_000), Trim::No);
    assert_eq!(calls.after_wait(10_000), Trim::Now);
    // Its wait over, the next is 10 s after that trim.
    assert_eq!(calls.finished_after_start(12_000), Trim::After(8_000));
    assert_eq!(calls.finished_after_start(25_000), Trim::Now);
}

#[test]
fn a_waited_trim_gives_way_to_a_call_in_flight() {
    let calls = calls();
    assert_eq!(calls.finished_after_start(0), Trim::Now);
    assert_eq!(calls.finished_after_start(1_000), Trim::After(9_000));
    calls.lock().in_flight += 1;
    assert_eq!(calls.after_wait(10_000), Trim::No);
    // The call in flight ends: it trims, the wait being over.
    assert_eq!(calls.finished(11_000), Trim::Now);
}

#[test]
fn a_wait_with_no_runtime_leaves_the_trim_to_the_next_call() {
    counting(1_000);
    let calls = calls();
    drop(calls.start());
    TEST_NOW_MS.with(|c| c.set(Some(2_000)));
    drop(calls.start());
    assert_eq!(trims(), 1);
    // No wait left pending: a call ending after 10 s trims.
    assert!(!calls.lock().waiting);
    TEST_NOW_MS.with(|c| c.set(Some(11_000)));
    drop(calls.start());
    assert_eq!(trims(), 2);
}

#[tokio::test]
async fn a_waited_trim_is_made_once_the_wait_is_over() {
    // The current-thread runtime runs the wait on this thread, by this
    // clock.
    counting(0);
    let calls = calls();
    drop(calls.start());
    TEST_NOW_MS.with(|c| c.set(Some(TRIM_EVERY_MS - 50)));
    drop(calls.start());
    assert_eq!(trims(), 1);
    assert!(calls.lock().waiting);
    TEST_NOW_MS.with(|c| c.set(Some(TRIM_EVERY_MS)));
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    let state = calls.lock();
    assert!(!state.waiting);
    assert_eq!(state.last_trim_ms, Some(TRIM_EVERY_MS));
}

impl Calls {
    /// A call starts and ends at `now_ms` (with no InFlight to trim).
    fn finished_after_start(&self, now_ms: i64) -> Trim {
        self.lock().in_flight += 1;
        self.finished(now_ms)
    }
}

#[cfg(all(target_os = "linux", target_env = "gnu"))]
#[test]
fn glibc_has_mallopt_and_malloc_trim() {
    assert!(glibc::mallopt().is_some());
    let malloc_trim = glibc::malloc_trim().expect("malloc_trim");
    // SAFETY: as trim.
    let gave_back = unsafe { malloc_trim(0) };
    assert!(gave_back == 0 || gave_back == 1);
}
