"""Compaction, end to end: preparing it, proposing what happened, the
user's edits and notes on the proposal, confirming it, judging its
events -- and history staying settled after it, the errors along the way,
and abandoning a proposal."""

from __future__ import annotations

from datetime import date

import pytest

from conformance import seed

FIRST = date(2026, 10, 5)
"""The calendar has planned days from here up to now (the 7th, 15:30)."""

RECAP = {"overview": "A normal few days.", "did": [{"text": "Moved **breakfast** to when it ended.", "event_ids": [], "note_ids": []}], "unsure": []}


@pytest.fixture
def plan(google, calendar_id):
    return seed.planned_days(google, calendar_id, date(2026, 10, 4), 4)


async def notes(session, *entries):
    for timestamp, description in entries:
        await session.call("note", noted_time={"timestamp": timestamp, "description": description})


def ids_of(context) -> dict[str, str]:
    """Note ids by description."""
    return {n.get("description") or n["timestamp"]: n["id"] for n in context["notes"]}


async def test_nothing_to_compact(session, plan):
    context = await session.call("prepare_compaction")
    assert context["notes"] == []
    status = await session.call("get_compaction_status")
    assert status["last_compaction"] is None and status["proposal"] is None


async def test_full_round(session, plan):
    d5, d6 = plan[FIRST], plan[date(2026, 10, 6)]
    friend = (await session.call("create_person", person={"name": "Robin Example"}))["created_id"]
    eat = (await session.call("create_action", action={"name": "eat a meal", "status": "active"}))["created_id"]
    await notes(
        session,
        ("2026-10-05T07:45:00-04:00", "finished breakfast"),
        ("2026-10-05T12:30:00-04:00", "lunch with Robin at the cafe"),
        ("2026-10-05T19:30:00-04:00", "dinner done"),
        ("2026-10-06T15:00:00-04:00", "left work early, walked in the park"),
        ("2026-10-06T16:00:00-04:00", "back"),
        ("2026-10-06T20:00:00-04:00", "random thought"),
    )
    context = await session.call("prepare_compaction")
    assert context["timeline"]
    note = ids_of(context)
    proposed = await session.call(
        "compact_notes",
        recap={**RECAP, "did": [{"text": "Breakfast ran late.", "event_ids": [d5["Breakfast"]], "note_ids": [note["finished breakfast"]]}], "unsure": [{"text": "Was the walk *work*?", "event_ids": ["walk"], "note_ids": []}]},
        updates=[
            {"event_id": d5["Breakfast"], "end_note": note["finished breakfast"], "action_ids": [eat]},
            {"event_id": d5["Lunch"], "facts": {"with_ids": [friend], "location_ids": ["new:cafe"]}, "action_ids": [eat]},
            {"event_id": d5["Dinner"], "end_note": note["dinner done"]},
            {"event_id": d6["Afternoon work"], "end_note": note["left work early, walked in the park"]},
        ],
        creates=[{"ref": "walk", "summary": "Walk in the park", "start_note": note["left work early, walked in the park"], "end_note": note["back"], "action_ids": ["new:walk"]}],
        ignore_notes=[note["random thought"]],
        new_actions=[{"ref": "new:walk", "name": "go for a walk", "status": "active"}],
        new_locations=[{"ref": "new:cafe", "name": "The Cafe", "hint": "the corner cafe"}],
    )
    proposal_id, revision = proposed["proposal_id"], proposed["revision"]
    assert proposed["timeline"]
    # Nothing written to the calendar yet.
    breakfast = await session.call("get_event", id=d5["Breakfast"])
    assert breakfast["end"] == "2026-10-05T07:30:00-04:00"
    status = await session.call("get_compaction_status")
    assert status["proposal"]["proposal_id"] == proposal_id if "proposal_id" in status["proposal"] else status["proposal"]
    shown = await session.call("get_proposal")
    assert shown == await session.call("get_proposal", proposal_id=proposal_id)
    await session.call("get_proposal", proposal_id=proposal_id, since_revision=revision)
    # A note for Claude holds up confirming until it's answered or withdrawn.
    feedback = await session.call("add_proposal_note", proposal_id=proposal_id, text="Dinner was with Robin too", event_id=d5["Dinner"])
    await session.call_error("confirm_proposal", proposal_id=proposal_id, revision=revision)
    await session.call("withdraw_proposal_note", feedback_id=feedback["id"])
    await session.attempt("withdraw_proposal_note", feedback_id=feedback["id"])
    # The user's edit, laid over Claude's decisions.
    amended = await session.call(
        "amend_proposal",
        proposal_id=proposal_id,
        revision=revision,
        updates=[{"event": {"id": d5["Dinner"], "summary": "Dinner with Robin", "facts": {"with_ids": [friend]}}}],
        additions=[{"ref": "new:cafe", "use": "create", "name": "Corner Cafe"}],
    )
    revision = amended["revision"]
    await session.call_error("confirm_proposal", proposal_id=proposal_id, revision=revision - 1)
    result = await session.call("confirm_proposal", proposal_id=proposal_id, revision=revision)
    assert result
    # Written now.
    assert (await session.call("get_event", id=d5["Breakfast"]))["end"] == "2026-10-05T07:45:00-04:00"
    assert (await session.call("get_event", id=d5["Dinner"]))["summary"] == "Dinner with Robin"
    assert "go for a walk" in [a["name"] for a in (await session.call("get_actions"))["actions"]]
    assert "Corner Cafe" in [loc["name"] for loc in await session.call("get_locations")]
    status = await session.call("get_compaction_status")
    assert status["last_compaction"] is not None
    assert await session.call("get_notes") == []
    assert len(await session.call("get_notes", include_compacted=True)) == 6
    # Compacted notes can't be changed.
    compacted = (await session.call("get_notes", include_compacted=True))[0]
    await session.call_error("delete_note", note_id=compacted["id"])
    await session.call_error("edit_note", note_id=compacted["id"], description="x")
    # Judgments.
    due = await session.call("prepare_judgments")
    if due.get("events"):
        requests = [f"{e['event_id']}/{person}/{part}" for e in due["events"] for person, parts in e["people"].items() for part in parts]
        half = len(requests) // 2
        partial = await session.call("record_judgments", compaction_id=due["compaction_id"], judgments=[{"request_id": r, "rating": 1, "reasoning": "fine"} for r in requests[:half]])
        assert partial
        assert (await session.call("get_compaction_status"))["judgments_pending"] == due["compaction_id"]
        await session.call_error("record_judgments", compaction_id=due["compaction_id"], judgments=[{"request_id": requests[half], "rating": 99, "reasoning": "off the scale"}])
        done = await session.call("record_judgments", compaction_id=due["compaction_id"], judgments=[{"request_id": r, "rating": 2, "reasoning": "good"} for r in requests[half:]])
        assert done
        assert (await session.call("get_compaction_status"))["judgments_pending"] is None
        judged = await session.call("get_event", id=due["events"][0]["event_id"])
        assert judged["judgments"]
        await session.call_error("record_judgments", compaction_id=due["compaction_id"], judgments=[{"request_id": "nosuch", "rating": 1, "reasoning": "x"}])
    await session.call("prepare_judgments", redo=True)
    await session.call("get_compaction_status")
    # History is settled now.
    await session.call_error("update_event", updates=[{"event": {"id": d5["Lunch"], "summary": "Long lunch"}}])
    await session.call_error("delete_event", cancels=[{"event_id": d5["Lunch"], "counts_against_follow_through": False}])
    await session.call_error("create_event", events=[{"summary": "Backfill", "start": "2026-10-05T20:00:00-04:00", "end": "2026-10-05T21:00:00-04:00"}])
    await session.call("update_event", updates=[{"event": {"id": d5["Lunch"], "summary": "Long lunch"}}], allow_compacted_changes=True)


async def test_refusals(session, plan):
    d5 = plan[FIRST]
    await notes(session, ("2026-10-05T07:45:00-04:00", "finished breakfast"))
    note = ids_of(await session.call("prepare_compaction"))
    # Overlapping what's next.
    overlap = await session.call_error("compact_notes", recap=RECAP, updates=[{"event_id": d5["Breakfast"], "end": "2026-10-05T08:30:00-04:00"}])
    assert "Work" in overlap
    await session.call_error("compact_notes", recap=RECAP, updates=[{"event_id": "nosuch", "end_note": note["finished breakfast"]}])
    await session.call_error("compact_notes", recap=RECAP, updates=[{"event_id": d5["Breakfast"], "end_note": "nosuch"}])
    await session.call_error("compact_notes", recap=RECAP, updates=[{"event_id": d5["Breakfast"], "action_ids": ["new:undeclared"]}])
    await session.call_error("compact_notes", recap=RECAP, creates=[{"summary": "Nothing", "start": "2026-10-05T09:00:00-04:00", "end": "2026-10-05T08:00:00-04:00"}])
    proposed = await session.call("compact_notes", recap=RECAP, updates=[{"event_id": d5["Breakfast"], "end_note": note["finished breakfast"]}])
    # Revising needs the proposal and the revision started from.
    await session.call_error("compact_notes", recap=RECAP, proposal_id=proposed["proposal_id"], revision=proposed["revision"] + 5)
    await session.call_error("amend_proposal", proposal_id=proposed["proposal_id"], revision=proposed["revision"], updates=[{"event": {"id": "nosuch", "summary": "x"}}])
    await session.call_error("amend_proposal", proposal_id="nosuch", revision=1)
    await session.call_error("get_proposal", proposal_id="nosuch")
    await session.call_error("finish_proposal", proposal_id=proposed["proposal_id"])
    abandoned = await session.call("abandon_compaction", proposal_id=proposed["proposal_id"])
    assert abandoned
    assert (await session.call("get_compaction_status"))["proposal"] is None
    assert len(await session.call("get_notes")) == 1


async def test_revise_and_extend(session, plan):
    d5, d6 = plan[FIRST], plan[date(2026, 10, 6)]
    await notes(session, ("2026-10-05T07:45:00-04:00", "finished breakfast"))
    note = ids_of(await session.call("prepare_compaction"))
    first = await session.call("compact_notes", recap=RECAP, updates=[{"event_id": d5["Breakfast"], "end_note": note["finished breakfast"]}])
    # A new note, then Claude's next revision of the same proposal.
    await notes(session, ("2026-10-06T12:15:00-04:00", "lunch late"))
    context = await session.call("prepare_compaction")
    assert context["proposal"]
    note = ids_of(context)
    second = await session.call(
        "compact_notes",
        recap=RECAP,
        proposal_id=first["proposal_id"],
        revision=first["revision"],
        updates=[{"event_id": d5["Breakfast"], "end_note": note["finished breakfast"]}],
        cancels=[{"event_id": d6["Dinner"], "counts_against_follow_through": True}],
        annotate_notes=[{"note_id": note["lunch late"], "event_id": d6["Lunch"]}],
    )
    assert second["revision"] > first["revision"]
    # The user extends it to now, and edits what a note is for.
    extended = await session.call("amend_proposal", proposal_id=first["proposal_id"], revision=second["revision"], through="2026-10-07T15:30:00-04:00", notes=[{"note_id": note["lunch late"], "use": "ignore"}], as_planned=[d6["Dinner"]])
    await session.call_error("amend_proposal", proposal_id=first["proposal_id"], revision=extended["revision"], through="2026-10-08T12:00:00-04:00")
    result = await session.call("confirm_proposal", proposal_id=first["proposal_id"], revision=extended["revision"])
    assert result
    assert (await session.call("get_event", id=d6["Dinner"]))["summary"] == "Dinner"


async def test_cancel_counts_against_people_and_habits(session, plan):
    d6 = plan[date(2026, 10, 6)]
    friend = (await session.call("create_person", person={"name": "Robin Example"}))["created_id"]
    eat = (await session.call("create_action", action={"name": "eat a meal", "status": "active"}))["created_id"]
    await session.call("create_trait", trait={"name": "Dependable", "definition": "Shows up.", "parts": [{"kind": "follow_through", "penalty": 20, "recovery": 30, "look_back_days": 90, "action": eat}]})
    await session.call("create_habit", habit={"name": "Eat together", "action_id": eat})
    await session.call("update_event", updates=[{"event": {"id": d6["Dinner"], "action_ids": [eat], "facts": {"with_ids": [friend]}}}])
    await notes(session, ("2026-10-06T18:30:00-04:00", "skipped dinner"))
    await session.call("prepare_compaction")
    proposed = await session.call("compact_notes", recap=RECAP, cancels=[{"event_id": d6["Dinner"], "counts_against_follow_through": True}])
    await session.call("confirm_proposal", proposal_id=proposed["proposal_id"], revision=proposed["revision"])
    person = await session.call("get_person", id_or_name=friend)
    assert person["cancelled_events"]
    habit = (await session.call("get_habits"))[0]
    assert habit["cancelled_events"] is not None
    due = await session.call("prepare_habit_judgments", habit_id=habit["id"])
    assert due
    await session.call_error("prepare_habit_judgments", habit_id="nosuch")
    # Cancelling by hand counts too, when it says so.
    await session.call("create_event", events=[{"summary": "Dinner again", "start": "2026-10-07T20:00:00-04:00", "end": "2026-10-07T21:00:00-04:00", "action_ids": [eat], "facts": {"with_ids": [friend]}}])
    later = (await session.call("list_events", min_time="2026-10-07T17:00:00-04:00", max_time="2026-10-07T22:00:00-04:00"))
    dinner = next(e for e in later if e["summary"] == "Dinner again")
    await session.call("delete_event", cancels=[{"event_id": dinner["id"], "counts_against_follow_through": True}])
    assert len((await session.call("get_person", id_or_name=friend))["cancelled_events"]) == 2


async def test_no_judgments_without_compaction(session, plan):
    await session.call_error("prepare_judgments")
    await session.call_error("record_judgments", compaction_id="nosuch", judgments=[])
