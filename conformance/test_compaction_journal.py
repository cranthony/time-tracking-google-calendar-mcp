"""Compaction paths that write the journal in ways test_compaction.py's
and test_compaction_paths.py's don't, found by measuring which of the
journal's writes no scenario reached: a write that can't succeed after a
create and new facts were applied (the rebuild's aliases, and the
judgments still due), user edits
left inapplicable by events deleted under a proposal, feedback a newer
user edit supersedes, a backlog round that can't be extended, one
that no longer plans when it's confirmed, refusals while one is being
applied, and a journal grown long enough to be trimmed."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from conformance import seed

FIRST = date(2026, 10, 5)

RECAP = {"overview": "What the notes say.", "did": [{"text": "Recorded the days.", "event_ids": [], "note_ids": []}], "unsure": []}


@pytest.fixture
def plan(google, calendar_id):
    return seed.planned_days(google, calendar_id, date(2026, 10, 4), 4)


async def notes(session, *entries) -> None:
    for timestamp, description in entries:
        await session.call("note", noted_time={"timestamp": timestamp, "description": description})


def ids_of(context) -> dict[str, str]:
    return {n.get("description") or n["timestamp"]: n["id"] for n in context["notes"]}


async def propose(session, **kwargs):
    return await session.call("compact_notes", recap=kwargs.pop("recap", RECAP), **kwargs)


async def test_rebuild_after_a_keyed_create_and_new_facts(session, google, plan):
    d6 = plan[date(2026, 10, 6)]
    home = (await session.call("create_location", location={"name": "Home", "hint": "at home"}))["created_id"]
    await notes(session, ("2026-10-06T11:50:00-04:00", "worked from home"), ("2026-10-06T19:20:00-04:00", "dinner done"))
    note = ids_of(await session.call("prepare_compaction"))
    proposed = await propose(
        session,
        updates=[
            {"event_id": d6["Work"], "end_note": note["worked from home"], "facts": {"location_id": home, "notes": {"self": "quiet"}}},
            {"event_id": d6["Dinner"], "end_note": note["dinner done"]},
        ],
        creates=[
            {"summary": "Walk", "start": "2026-10-06T19:30:00-04:00", "end": "2026-10-06T20:00:00-04:00"},
            {"summary": "Stretch", "start": "2026-10-06T20:10:00-04:00", "end": "2026-10-06T20:30:00-04:00"},
        ],
    )
    pid = proposed["proposal_id"]
    # The user renames the dinner; their edit is carried through the rebuild.
    amended = await session.call("amend_proposal", proposal_id=pid, revision=proposed["revision"], updates=[{"event": {"id": d6["Dinner"], "summary": "Late dinner"}}])
    # Work, the dinner and the walk are written (creates come last), then
    # the stretch can never be: what's left is proposed again, the walk
    # known by its key and Work's facts still to be judged.
    google.fail_next.append(("POST", "/events", 404, "Not Found", 1))
    rebuilt = await session.call("confirm_proposal", proposal_id=pid, revision=amended["revision"])
    assert rebuilt["status"] == "rebuilt"
    await session.call("get_proposal", proposal_id=pid)
    done = await session.call("confirm_proposal", proposal_id=pid, revision=rebuilt["proposal"]["revision"])
    assert done["status"] == "applied"
    await session.call("prepare_judgments")


async def test_edits_left_inapplicable(session, plan):
    d5 = plan[FIRST]
    await notes(session, ("2026-10-05T12:40:00-04:00", "lunch done"))
    note = ids_of(await session.call("prepare_compaction"))
    proposed = await propose(session, updates=[{"event_id": d5["Lunch"], "end_note": note["lunch done"]}])
    pid = proposed["proposal_id"]
    # Edited, then cancelled by hand: Claude's next revision leaves the
    # edit out as inapplicable.
    amended = await session.call("amend_proposal", proposal_id=pid, revision=proposed["revision"], updates=[{"event": {"id": d5["Afternoon work"], "summary": "Errands"}}])
    await session.call("delete_event", cancels=[{"event_id": d5["Afternoon work"], "counts_against_follow_through": False}])
    revised = await propose(session, proposal_id=pid, revision=amended["revision"], updates=[{"event_id": d5["Lunch"], "end_note": note["lunch done"]}])
    shown = await session.call("get_proposal", proposal_id=pid)
    assert any(e["status"] == "inapplicable" for e in shown["user_edits"])
    # Again, and this time the user's own next edit leaves it out.
    amended = await session.call("amend_proposal", proposal_id=pid, revision=revised["revision"], updates=[{"event": {"id": d5["Breakfast"], "summary": "Brunch"}}])
    await session.call("delete_event", cancels=[{"event_id": d5["Breakfast"], "counts_against_follow_through": False}])
    amended = await session.call("amend_proposal", proposal_id=pid, revision=amended["revision"], updates=[{"event": {"id": d5["Dinner"], "summary": "Supper"}}])
    shown = await session.call("get_proposal", proposal_id=pid)
    assert sum(e["status"] == "inapplicable" for e in shown["user_edits"]) >= 2
    done = await session.call("confirm_proposal", proposal_id=pid, revision=amended["revision"])
    assert done["status"] == "applied"


async def test_feedback_superseded_by_a_newer_edit(session, plan):
    d5 = plan[FIRST]
    await notes(session, ("2026-10-05T19:15:00-04:00", "dinner ran late"))
    note = ids_of(await session.call("prepare_compaction"))
    proposed = await propose(session, updates=[{"event_id": d5["Dinner"], "end_note": note["dinner ran late"]}])
    pid, first = proposed["proposal_id"], proposed["revision"]
    feedback = await session.call("add_proposal_note", proposal_id=pid, text="It was with family", event_id=d5["Dinner"])
    # The user edits the dinner after Claude's revision began: Claude's
    # answer, from the revision it started from, is superseded by it.
    amended = await session.call("amend_proposal", proposal_id=pid, revision=first, updates=[{"event": {"id": d5["Dinner"], "summary": "Long dinner"}}])
    revised = await session.attempt(
        "compact_notes", recap=RECAP, proposal_id=pid, revision=first,
        updates=[{"event_id": d5["Dinner"], "end_note": note["dinner ran late"], "summary": "Family dinner"}],
        replies=[{"feedback_id": feedback["id"], "reply": "Renamed it Family dinner."}],
    )
    revision = amended["revision"] if revised.is_error else revised.content["revision"]
    shown = await session.call("get_proposal", proposal_id=pid)
    assert shown["feedback"]
    done = await session.call("confirm_proposal", proposal_id=pid, revision=revision)
    assert done["status"] in ("applied", "rechecked")


async def test_extending_a_backlog_round(session, google, calendar_id):
    days = seed.planned_days(google, calendar_id, date(2026, 9, 26), 12)
    await notes(
        session,
        ("2026-09-27T07:40:00-04:00", "breakfast done"),
        ("2026-10-05T12:45:00-04:00", "lunch ran short"),
        ("2026-10-05T19:10:00-04:00", "dinner done"),
    )
    context = await session.call("prepare_compaction")
    assert context["remaining_note_count"]
    note = ids_of(context)
    proposed = await propose(session, updates=[{"event_id": days[date(2026, 9, 27)]["Breakfast"], "end_note": note["breakfast done"]}])
    pid = proposed["proposal_id"]
    # A round of a backlog can't be cut short, run past now, or run on
    # past the days one round takes in: the notes there are the next
    # round's.
    await session.call_error("amend_proposal", proposal_id=pid, revision=proposed["revision"], through="2026-09-26T12:00:00-04:00")
    await session.call_error("amend_proposal", proposal_id=pid, revision=proposed["revision"], through="2026-10-09T12:00:00-04:00")
    await session.call_error("amend_proposal", proposal_id=pid, revision=proposed["revision"], through="2026-10-05T20:00:00-04:00")
    done = await session.call("confirm_proposal", proposal_id=pid, revision=proposed["revision"])
    assert done["status"] == "applied"
    assert await session.call("prepare_compaction")


async def test_no_longer_plans_when_confirmed(session, plan):
    d5 = plan[FIRST]
    await notes(session, ("2026-10-05T19:20:00-04:00", "dinner done, then a walk"))
    note = ids_of(await session.call("prepare_compaction"))
    proposed = await propose(
        session,
        updates=[{"event_id": d5["Dinner"], "end_note": note["dinner done, then a walk"]}],
        creates=[{"summary": "Walk", "start": "2026-10-05T19:30:00-04:00", "end": "2026-10-05T20:30:00-04:00"}],
    )
    pid = proposed["proposal_id"]
    # An event added by hand where the walk goes: it no longer plans.
    await session.call("create_event", events=[{"summary": "Call", "start": "2026-10-05T19:45:00-04:00", "end": "2026-10-05T20:00:00-04:00"}])
    handed = await session.call("confirm_proposal", proposal_id=pid, revision=proposed["revision"])
    assert handed["status"] in ("needs_claude", "rechecked")
    if handed["status"] == "needs_claude":
        await session.call_error("confirm_proposal", proposal_id=pid, revision=proposed["revision"])
    await session.call("get_proposal", proposal_id=pid)


async def test_refusals_while_applying(session, google, plan):
    d5 = plan[FIRST]
    await notes(session, ("2026-10-05T12:40:00-04:00", "lunch done"), ("2026-10-05T19:20:00-04:00", "dinner done"))
    note = ids_of(await session.call("prepare_compaction"))
    proposed = await propose(
        session,
        updates=[{"event_id": d5["Lunch"], "end_note": note["lunch done"]}, {"event_id": d5["Dinner"], "end_note": note["dinner done"]}],
    )
    pid = proposed["proposal_id"]
    google.fail_next.append(("PATCH", d5["Dinner"], 500, "Backend Error"))
    await session.call_error("confirm_proposal", proposal_id=pid, revision=proposed["revision"])
    # Being applied: no notes for Claude, and no confirming it again.
    await session.call_error("add_proposal_note", proposal_id=pid, text="one more thing")
    await session.call_error("confirm_proposal", proposal_id=pid, revision=proposed["revision"])
    finished = await session.call("finish_proposal", proposal_id=pid)
    assert finished["status"] == "applied"


async def test_journal_trimmed_once_it_grows(session, google, calendar_id):
    # Six weeks of backlog, a note a day: a compaction a week, each writing
    # a few rows a decision, until the journal's tab is long enough to be
    # trimmed of the finished ones.
    first = date(2026, 8, 24)
    days = seed.planned_days(google, calendar_id, first - timedelta(days=1), 45)
    for offset in range(42):
        day = first + timedelta(days=offset)
        await notes(session, (f"{day.isoformat()}T19:20:00-04:00", f"dinner done, day {offset + 1}"))
    rounds = 0
    while True:
        context = await session.call("prepare_compaction")
        if not context["notes"]:
            break
        decided = [{"event_id": days[date.fromisoformat(n["timestamp"][:10])]["Dinner"], "end_note": n["id"]} for n in context["notes"]]
        proposed = await propose(session, updates=decided)
        done = await session.call("confirm_proposal", proposal_id=proposed["proposal_id"], revision=proposed["revision"])
        assert done["status"] == "applied"
        rounds += 1
        assert rounds <= 10
    assert rounds >= 5
    await session.call("get_compaction_status")
