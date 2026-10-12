//! Python's datetimes in a named zone (zoneinfo): adding a timedelta to
//! one keeps its wall-clock time and recomputes the offset, rather than
//! keeping the instant's distance -- which is how a recurring series keeps
//! its 9:00 across a daylight saving change. Where the Python server did
//! that arithmetic, this does it the same way.

use chrono::{DateTime, Duration, FixedOffset, LocalResult, NaiveDateTime, Offset, TimeZone};
use chrono_tz::Tz;

pub type Moment = DateTime<FixedOffset>;

/// A local time in `zone` as zoneinfo resolves it with fold=0: the first
/// of two in a repeated hour; in a skipped hour, the offset from before
/// the change.
pub fn resolve(zone: &Tz, naive: NaiveDateTime) -> Moment {
    match zone.from_local_datetime(&naive) {
        LocalResult::Single(dt) => dt.fixed_offset(),
        LocalResult::Ambiguous(earliest, _) => earliest.fixed_offset(),
        LocalResult::None => {
            let before = zone.from_local_datetime(&(naive - Duration::hours(3))).earliest().map(|d| d.offset().fix());
            let offset = before.unwrap_or_else(|| FixedOffset::east_opt(0).unwrap());
            offset.from_local_datetime(&naive).unwrap()
        }
    }
}

/// `moment.astimezone(zone)`: the same instant, with the zone's offset then.
pub fn in_zone(moment: &Moment, zone: &Tz) -> Moment {
    moment.with_timezone(zone).fixed_offset()
}

/// `dt + delta` for `dt` aware in `zone` (a ZoneInfo datetime): wall-clock
/// arithmetic.
pub fn add(moment: &Moment, zone: &Tz, delta: Duration) -> Moment {
    let local = moment.with_timezone(zone).naive_local() + delta;
    resolve(zone, local)
}

/// `dt - other` for two aware datetimes: the instants' difference (Python
/// takes it in UTC when the tzinfos differ, and the wall-clock difference
/// when they're the same zone -- for times read back from Google, with
/// their own fixed offsets, that's the instants' difference too).
pub fn sub(a: &Moment, b: &Moment) -> Duration {
    *a - *b
}

/// Two datetimes in the same zone subtracted as Python does: the wall
/// clocks' difference.
pub fn sub_in_zone(a: &Moment, b: &Moment, zone: &Tz) -> Duration {
    a.with_timezone(zone).naive_local() - b.with_timezone(zone).naive_local()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_the_wall_clock_across_dst() {
        let zone: Tz = "America/New_York".parse().unwrap();
        let start = DateTime::parse_from_rfc3339("2026-10-31T09:00:00-04:00").unwrap();
        let later = add(&start, &zone, Duration::days(2));
        assert_eq!(crate::py::isoformat(&later), "2026-11-02T09:00:00-05:00");
        // A skipped hour keeps the offset from before the change.
        let gap = resolve(&zone, NaiveDateTime::parse_from_str("2026-03-08T02:30:00", "%Y-%m-%dT%H:%M:%S").unwrap());
        assert_eq!(crate::py::isoformat(&gap), "2026-03-08T02:30:00-05:00");
        let repeated = resolve(&zone, NaiveDateTime::parse_from_str("2026-11-01T01:30:00", "%Y-%m-%dT%H:%M:%S").unwrap());
        assert_eq!(crate::py::isoformat(&repeated), "2026-11-01T01:30:00-04:00");
    }
}
