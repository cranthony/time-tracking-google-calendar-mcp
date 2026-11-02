"""The circadian rhythm: natural wake times, the windows' lengths, and
each day's temperature minimum and dim and bright windows derived from
them -- across New York's change back from daylight saving time."""

from __future__ import annotations


async def test_circadian_rhythm(session):
    empty = await session.call("get_circadian_rhythm")
    assert empty["revisions"] == [] and empty["days"] is None and empty["time_zone"] == "America/New_York"
    assert empty["windows"] == {"minimum_before_waking_minutes": 120, "dim_before_minutes": 360, "bright_after_minutes": 240}
    await session.call("set_natural_wake_time", date="2026-10-30", time="7:10", note="woke without alarm")
    await session.call("set_natural_wake_time", date="2026-11-02", time="06:40")
    replaced = await session.call("set_natural_wake_time", date="2026-10-30", time="07:00")
    assert [(r["date"], r["time"], r["note"]) for r in replaced["revisions"]] == [("2026-10-30", "07:00", None), ("2026-11-02", "06:40", None)]
    await session.call_error("set_natural_wake_time", date="2026-02-30", time="07:00")
    await session.call_error("set_natural_wake_time", date="2026-11-02", time="7pm")
    await session.call_error("set_natural_wake_time", date="2027-10-08", time="07:00")
    await session.call_error("set_natural_wake_time", date="2026-11-02", time="07:00", note="x" * 501)

    got = await session.call("get_circadian_rhythm", **{"from": "2026-10-29", "to": "2026-11-02"})
    assert [d["date"] for d in got["days"]] == ["2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02"]
    assert got["days"][2]["dim"] == {"start": "2026-11-01T00:00:00-04:00", "end": "2026-11-01T05:00:00-05:00"}
    await session.call_error("get_circadian_rhythm", **{"from": "2026-11-02", "to": "2026-11-01"})
    await session.call_error("get_circadian_rhythm", **{"from": "2026-11-02"})

    windows = await session.call("set_circadian_windows", minimum_before_waking_minutes=90, dim_before_minutes=300, bright_after_minutes=180)
    assert [r["temperature_minimum"] for r in windows["revisions"]] == ["05:30", "05:10"]
    await session.call_error("set_circadian_windows", minimum_before_waking_minutes=90, dim_before_minutes=1441, bright_after_minutes=180)
    after = await session.call("get_circadian_rhythm", **{"from": "2026-11-02", "to": "2026-11-02"})
    assert after["days"][0]["bright"] == {"start": "2026-11-02T05:10:00-05:00", "end": "2026-11-02T08:10:00-05:00"}
