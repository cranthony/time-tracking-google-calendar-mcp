//! tests/test_write_lock.py.

use super::*;

fn in_another_thread<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
    std::thread::spawn(f).join().unwrap()
}

#[test]
fn is_held_only_by_the_thread_that_took_it() {
    assert!(!held());

    {
        let _held = WRITE_LOCK.hold();
        assert!(held());
        assert!(!in_another_thread(held));
    }

    assert!(!held());
}

#[test]
fn is_reentrant_and_held_until_the_outermost_release() {
    {
        let _outer = WRITE_LOCK.hold();
        {
            let _inner = WRITE_LOCK.hold();
            assert!(held());
        }
        assert!(held());
    }

    assert!(!held());
}

#[test]
fn another_thread_waits_for_it() {
    let entered = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let waiting = {
        let _held = WRITE_LOCK.hold();
        let flag = entered.clone();
        let thread = std::thread::spawn(move || {
            let _held = WRITE_LOCK.hold();
            flag.store(true, std::sync::atomic::Ordering::SeqCst);
        });
        std::thread::sleep(std::time::Duration::from_millis(50));
        assert!(!entered.load(std::sync::atomic::Ordering::SeqCst));
        thread
    };
    waiting.join().unwrap();

    assert!(entered.load(std::sync::atomic::Ordering::SeqCst));
}

#[test]
fn a_write_refuses_without_the_lock_and_runs_with_it() {
    let error = require("SheetsClient.write_rows").unwrap_err();
    assert!(error.to_string().contains("SheetsClient.write_rows writes to Google"), "{error}");

    let _held = WRITE_LOCK.hold();
    require("SheetsClient.write_rows").unwrap();
    // But not on a thread other than the holder's.
    assert!(in_another_thread(|| require("SheetsClient.write_rows").is_err()));
}

#[test]
fn the_health_tabs_lock_lets_its_writer_write_too() {
    let _held = HEALTH_TAB_LOCK.hold();

    require("SheetsClient.write_rows").unwrap();
}
