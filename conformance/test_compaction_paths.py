"""Compaction's less-traveled paths, beyond test_compaction.py's: a
backlog longer than a week (two compactions, the second over history the
first settled), a late note revisiting what the first settled, the
calendar or notes changing under a proposal (a
recheck, a proposal handed back to Claude), writes failing partway
through applying one (rebuilt, or finished later), the user's edits of
notes, descriptions and additions, Claude answering feedback, actions
suggested and priorities kept, habit backfills, and the refusals along
the way."""

from __future__ import annotations

from datetime import date

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


async def test_backlog_over_a_week(session, google, calendar_id):
    days = seed.planned_days(google, calendar_id, date(2026, 9, 26), 12)
    await notes(
        session,
        ("2026-09-27T07:40:00-04:00", "breakfast done"),
        ("2026-09-30T12:45:00-04:00", "lunch ran short"),
        ("2026-10-06T19:30:00-04:00", "late dinner done"),
    )
    first = await session.call("prepare_compaction")
    assert first["remaining_note_count"] == 1
    note = ids_of(first)
    proposed = await propose(
        session,
        updates=[{"event_id": days[date(2026, 9, 27)]["Breakfast"], "end_note": note["breakfast done"]}],
        ignore_notes=[note["lunch ran short"]],
    )
    await session.call("confirm_proposal", proposal_id=proposed["proposal_id"], revision=proposed["revision"])
    status = await session.call("get_compaction_status")
    assert status["last_compaction"]
    # The rest of the backlog: what the first compaction settled is history.
    second = await session.call("prepare_compaction")
    assert second["notes"]
    settled = [e for e in second["events"] if e["history_until"]]
    note = ids_of(second)
    if settled:
        await session.call_error("compact_notes", recap=RECAP, cancels=[{"event_id": settled[0]["id"], "counts_against_follow_through": False}])
        await session.call_error("compact_notes", recap=RECAP, updates=[{"event_id": settled[0]["id"], "start": "2026-10-03T00:00:00-04:00"}])
    proposed = await propose(session, updates=[{"event_id": days[date(2026, 10, 6)]["Dinner"], "end_note": note["late dinner done"]}])
    pid, revision = proposed["proposal_id"], proposed["revision"]
    if settled:
        # The user may change history, once they approve it.
        await session.call_error("amend_proposal", proposal_id=pid, revision=revision, cancels=[{"event_id": settled[0]["id"], "counts_against_follow_through": False}])
        amended = await session.attempt("amend_proposal", proposal_id=pid, revision=revision, cancels=[{"event_id": settled[0]["id"], "counts_against_follow_through": False}], allow_compacted_changes=True)
        if not amended.is_error:
            revision = amended.content["revision"]
    await session.call("confirm_proposal", proposal_id=pid, revision=revision)
    assert await session.call("get_notes") == []
    await session.call("prepare_compaction")


async def test_late_note_revisits_history(session, google, calendar_id):
    # The first compaction takes a week of the backlog and judges it; then a
    # note comes in for a breakfast it settled, which the second revisits.
    days = seed.planned_days(google, calendar_id, date(2026, 9, 26), 12)
    breakfast = days[date(2026, 9, 27)]["Breakfast"]
    friend = (await session.call("create_person", person={"name": "Robin Example"}))["created_id"]
    await session.call("create_trait", trait={
        "name": "Close", "parts": [{"kind": "judgment", "rubric": "How connected?", "ratings": {"0": "apart", "1": "close"}, "facts": ["general_notes"], "engagement_type": "with"}],
    })
    await notes(session, ("2026-09-27T07:20:00-04:00", "breakfast with Robin"), ("2026-10-06T19:30:00-04:00", "late dinner done"))
    first = ids_of(await session.call("prepare_compaction"))
    proposed = await propose(session, updates=[{"event_id": breakfast, "facts": {"with_ids": [friend]}}], ignore_notes=[first["breakfast with Robin"]])
    await session.call("confirm_proposal", proposal_id=proposed["proposal_id"], revision=proposed["revision"])
    due = await session.call("prepare_judgments")
    requests = [f"{e['event_id']}/{person}/{part}" for e in due["events"] for person, parts in e["people"].items() for part in parts]
    await session.call("record_judgments", compaction_id=due["compaction_id"], judgments=[{"request_id": r, "rating": 1, "reasoning": "close"} for r in requests])
    await notes(session, ("2026-09-27T07:25:00-04:00", "Robin starts a new job Monday"))
    second = await session.call("prepare_compaction")
    revisited = [e["id"] for e in second["events"] if e.get("revisited")]
    assert revisited == [breakfast]
    assert "revisited" in second["timeline"]["text"]
    note = ids_of(second)
    await session.call_error("compact_notes", recap=RECAP, updates=[{"event_id": breakfast, "end": "2026-09-27T07:40:00-04:00"}])
    proposed = await propose(
        session,
        updates=[
            {"event_id": breakfast, "facts": {"with_ids": [friend], "notes": {friend: "starts a new job Monday"}}},
            {"event_id": days[date(2026, 10, 6)]["Dinner"], "end_note": note["late dinner done"]},
        ],
    )
    shown = await session.call("get_proposal")
    assert [e["id"] for e in shown["events"] if e.get("revisited")] == [breakfast]
    await session.call("confirm_proposal", proposal_id=proposed["proposal_id"], revision=proposed["revision"])
    assert await session.call("get_notes") == []
    again = await session.call("prepare_judgments")
    assert [e["event_id"] for e in again["events"]][0] == breakfast
    await session.call("get_event", id=breakfast)


async def test_recheck_after_a_note_changes(session, plan):
    d5 = plan[FIRST]
    await notes(session, ("2026-10-05T07:45:00-04:00", "finished breakfast"), ("2026-10-05T09:15:00-04:00", "deep work"))
    note = ids_of(await session.call("prepare_compaction"))
    proposed = await propose(session, updates=[{"event_id": d5["Breakfast"], "end_note": note["finished breakfast"]}])
    # The note it adds to Work says something else now: confirming plans it again.
    await session.call("edit_note", note_id=note["deep work"], description="deep work, then a call")
    rechecked = await session.call("confirm_proposal", proposal_id=proposed["proposal_id"], revision=proposed["revision"])
    assert rechecked["status"] == "rechecked"
    revision = rechecked["proposal"]["revision"]
    await session.call("get_proposal", since_revision=proposed["revision"])
    # A note moved: its id changes, and so does the plan.
    moved = await session.call("edit_note", note_id=note["deep work"], timestamp="2026-10-05T09:20:00-04:00")
    again = await session.call("confirm_proposal", proposal_id=proposed["proposal_id"], revision=revision)
    assert again["status"] in ("rechecked", "needs_claude")
    if again["status"] == "rechecked":
        done = await session.call("confirm_proposal", proposal_id=proposed["proposal_id"], revision=again["proposal"]["revision"])
        assert done["status"] == "applied"
    await session.call("get_notes", include_compacted=True)
    assert moved


async def test_handed_back_to_claude(session, plan):
    d5 = plan[FIRST]
    await notes(session, ("2026-10-05T12:40:00-04:00", "lunch done"))
    note = ids_of(await session.call("prepare_compaction"))
    proposed = await propose(session, updates=[{"event_id": d5["Lunch"], "end_note": note["lunch done"]}])
    # The event it changes is cancelled by hand: it no longer plans.
    await session.call("delete_event", cancels=[{"event_id": d5["Lunch"], "counts_against_follow_through": False}])
    handed = await session.call("confirm_proposal", proposal_id=proposed["proposal_id"], revision=proposed["revision"])
    assert handed["status"] == "needs_claude"
    shown = await session.call("get_proposal")
    assert shown["problem"] or shown["state"] == "awaiting_claude"
    context = await session.call("prepare_compaction")
    feedback = [f for f in context["proposal"]["feedback"] if f["status"] == "open"]
    assert feedback
    # Replies have to name open feedback, and say something.
    await session.call_error("compact_notes", recap=RECAP, proposal_id=proposed["proposal_id"], revision=proposed["revision"], replies=[{"feedback_id": "nosuchf1", "reply": "x"}])
    await session.call_error("compact_notes", recap=RECAP, proposal_id=proposed["proposal_id"], revision=proposed["revision"], replies=[{"feedback_id": feedback[0]["id"], "reply": "   "}])
    revised = await propose(
        session, proposal_id=proposed["proposal_id"], revision=proposed["revision"],
        replies=[{"feedback_id": feedback[0]["id"], "reply": "Lunch was cancelled, so I left it out."}],
    )
    await session.call_error("compact_notes", recap=RECAP, proposal_id=proposed["proposal_id"], revision=revised["revision"], replies=[{"feedback_id": feedback[0]["id"], "reply": "again"}])
    await session.call_error("withdraw_proposal_note", feedback_id=feedback[0]["id"])
    done = await session.call("confirm_proposal", proposal_id=proposed["proposal_id"], revision=revised["revision"])
    assert done["status"] == "applied"


async def test_writes_failing_partway(session, google, plan):
    d5 = plan[FIRST]
    await notes(session, ("2026-10-05T12:40:00-04:00", "lunch done"), ("2026-10-05T19:20:00-04:00", "dinner done"))
    note = ids_of(await session.call("prepare_compaction"))
    proposed = await propose(
        session,
        updates=[
            {"event_id": d5["Lunch"], "end_note": note["lunch done"]},
            {"event_id": d5["Dinner"], "end_note": note["dinner done"]},
        ],
        creates=[{"summary": "Walk", "start": "2026-10-05T19:30:00-04:00", "end": "2026-10-05T20:00:00-04:00"}],
    )
    # A failure that may pass: applying stops, and finishes later.
    google.fail_next.append(("PATCH", d5["Dinner"], 500, "Backend Error"))
    await session.call_error("confirm_proposal", proposal_id=proposed["proposal_id"], revision=proposed["revision"])
    status = await session.call("get_compaction_status")
    assert status["proposal"]["state"] == "applying"
    await session.call_error("compact_notes", recap=RECAP, proposal_id=proposed["proposal_id"], revision=proposed["revision"])
    await session.call_error("amend_proposal", proposal_id=proposed["proposal_id"], revision=proposed["revision"], as_planned=[d5["Lunch"]])
    await session.call_error("edit_note", note_id=note["lunch done"], description="x")
    await session.call_error("delete_note", note_id=note["dinner done"])
    finished = await session.call("finish_proposal", proposal_id=proposed["proposal_id"])
    assert finished["status"] == "applied"
    again = await session.call("finish_proposal", proposal_id=proposed["proposal_id"])
    assert again["status"] == "applied"
    await session.call_error("abandon_compaction", proposal_id=proposed["proposal_id"])


async def test_write_that_cannot_succeed(session, google, plan):
    d5, d6 = plan[FIRST], plan[date(2026, 10, 6)]
    await notes(session, ("2026-10-05T12:40:00-04:00", "lunch done"), ("2026-10-06T19:20:00-04:00", "dinner done"))
    note = ids_of(await session.call("prepare_compaction"))
    proposed = await propose(
        session,
        updates=[
            {"event_id": d5["Lunch"], "end_note": note["lunch done"]},
            {"event_id": d6["Dinner"], "end_note": note["dinner done"]},
        ],
        creates=[{"summary": "Walk", "start": "2026-10-05T19:30:00-04:00", "end": "2026-10-05T20:00:00-04:00"}],
        cancels=[{"event_id": d6["Lunch"], "counts_against_follow_through": False}],
    )
    # The event's gone for good: what's left is proposed again.
    google.fail_next.append(("PATCH", d6["Dinner"], 410, "Resource has been deleted"))
    rebuilt = await session.call("confirm_proposal", proposal_id=proposed["proposal_id"], revision=proposed["revision"])
    assert rebuilt["status"] == "rebuilt"
    revision = rebuilt["proposal"]["revision"]
    done = await session.call("confirm_proposal", proposal_id=proposed["proposal_id"], revision=revision)
    assert done["status"] == "applied"


async def test_user_edits_of_notes_and_descriptions(session, plan):
    d5 = plan[FIRST]
    await notes(
        session,
        ("2026-10-05T09:00:00-04:00", "call with the team"),
        ("2026-10-05T10:30:00-04:00", "focus time"),
        ("2026-10-05T11:00:00-04:00", "coffee"),
        ("2026-10-05T12:30:00-04:00", "lunch outside"),
    )
    note = ids_of(await session.call("prepare_compaction"))
    proposed = await propose(session, ignore_notes=[note["coffee"]])
    pid, revision = proposed["proposal_id"], proposed["revision"]
    # What notes are for: one ignored, one moved to lunch, coffee back.
    amended = await session.call(
        "amend_proposal", proposal_id=pid, revision=revision,
        notes=[
            {"note_id": note["focus time"], "use": "ignore"},
            {"note_id": note["call with the team"], "use": "annotate", "event_id": d5["Lunch"]},
            {"note_id": note["coffee"], "use": "annotate"},
        ],
    )
    revision = amended["revision"]
    # The user's own description of Work: the notes it leaves out stay out.
    amended = await session.call(
        "amend_proposal", proposal_id=pid, revision=revision,
        updates=[{"event": {"id": d5["Work"], "description": "Morning block."}}],
    )
    revision = amended["revision"]
    # As Claude had them again, then annotated after all.
    amended = await session.call("amend_proposal", proposal_id=pid, revision=revision, as_planned=[note["coffee"], note["call with the team"]])
    revision = amended["revision"]
    amended = await session.call("amend_proposal", proposal_id=pid, revision=revision, notes=[{"note_id": note["focus time"], "use": "annotate"}])
    revision = amended["revision"]
    # Claude's next revision keeps every one of them.
    context = await session.call("prepare_compaction")
    assert context["proposal"]["user_edits"]
    revised = await propose(session, proposal_id=pid, revision=revision, ignore_notes=[note["lunch outside"]])
    await session.call("get_proposal", since_revision=1)
    done = await session.call("confirm_proposal", proposal_id=pid, revision=revised["revision"])
    assert done["status"] == "applied"
    work = await session.call("get_event", id=d5["Work"])
    assert work["description"].startswith("Morning block.")


async def test_additions_settled_early(session, plan):
    d5 = plan[FIRST]
    friend = (await session.call("create_person", person={"name": "Sam Example"}))["created_id"]
    await notes(session, ("2026-10-05T12:30:00-04:00", "lunch with Sam at the office"), ("2026-10-05T19:00:00-04:00", "dinner with Alex"))
    note = ids_of(await session.call("prepare_compaction"))
    proposed = await propose(
        session,
        updates=[
            {"event_id": d5["Lunch"], "facts": {"with_ids": ["new:sam"], "location_ids": ["new:office"], "notes": {"new:sam": "chatty", "self": "relaxed"}}, "action_ids": ["new:eat"]},
            {"event_id": d5["Dinner"], "facts": {"with_ids": ["new:alex"], "location_ids": ["new:home"]}},
        ],
        new_people=[{"ref": "new:sam", "name": "Sam Example", "context": "from work"}, {"ref": "new:alex", "name": "Alex Example"}],
        new_locations=[{"ref": "new:office", "name": "Office", "hint": "the office"}, {"ref": "new:home", "name": "Home", "hint": "at home"}],
        new_actions=[{"ref": "new:eat", "name": "eat a meal"}],
    )
    pid, revision = proposed["proposal_id"], proposed["revision"]
    # Settling them: one's someone already there, one's dropped, one's
    # made now with a better name.
    await session.call_error("amend_proposal", proposal_id=pid, revision=revision, additions=[{"ref": "new:nobody", "use": "drop"}])
    await session.call_error("amend_proposal", proposal_id=pid, revision=revision, additions=[{"ref": "new:sam", "use": "existing", "id": "nosuch"}])
    await session.call_error("amend_proposal", proposal_id=pid, revision=revision, additions=[{"ref": "new:office", "use": "drop", "id": friend}])
    amended = await session.call(
        "amend_proposal", proposal_id=pid, revision=revision,
        additions=[
            {"ref": "new:sam", "use": "existing", "id": friend},
            {"ref": "new:home", "use": "drop"},
            {"ref": "new:office", "use": "create", "name": "The Office"},
        ],
    )
    revision = amended["revision"]
    assert amended["settled_additions"]
    assert "The Office" in [loc["name"] for loc in await session.call("get_locations")]
    # Back to Claude's plan for one of them.
    amended = await session.call("amend_proposal", proposal_id=pid, revision=revision, as_planned=["new:home"])
    revision = amended["revision"]
    context = await session.call("prepare_compaction")
    assert context["proposal"]["settled_additions"]
    revised = await propose(
        session, proposal_id=pid, revision=revision,
        updates=[
            {"event_id": d5["Lunch"], "facts": {"with_ids": [friend], "location_ids": ["new:office"]}, "action_ids": ["new:eat"]},
            {"event_id": d5["Dinner"], "facts": {"with_ids": ["new:alex"], "location_ids": ["new:home"]}},
        ],
        new_people=[{"ref": "new:sam", "name": "Sam Example", "context": "from work"}, {"ref": "new:alex", "name": "Alex Example"}],
        new_locations=[{"ref": "new:office", "name": "Office", "hint": "the office"}, {"ref": "new:home", "name": "Home", "hint": "at home"}],
        new_actions=[{"ref": "new:eat", "name": "eat a meal"}],
    )
    done = await session.call("confirm_proposal", proposal_id=pid, revision=revised["revision"])
    assert done["status"] == "applied"
    await session.call("get_people")
    await session.call("get_locations")


async def test_feedback_answered_over_user_edits(session, plan):
    d5 = plan[FIRST]
    await notes(session, ("2026-10-05T19:15:00-04:00", "dinner ran late"))
    note = ids_of(await session.call("prepare_compaction"))
    proposed = await propose(session, updates=[{"event_id": d5["Dinner"], "end_note": note["dinner ran late"]}])
    pid = proposed["proposal_id"]
    amended = await session.call("amend_proposal", proposal_id=pid, revision=proposed["revision"], updates=[{"event": {"id": d5["Dinner"], "summary": "Long dinner"}}])
    await session.call_error("add_proposal_note", proposal_id=pid, text="   ")
    feedback = await session.call("add_proposal_note", proposal_id=pid, text="It was with family", event_id=d5["Dinner"], at="2026-10-05T18:30:00-04:00")
    other = await session.call("add_proposal_note", proposal_id=pid, text="Which note was this?", note_id=note["dinner ran late"])
    await session.call_error("confirm_proposal", proposal_id=pid, revision=amended["revision"])
    context = await session.call("prepare_compaction")
    assert len(context["proposal"]["feedback"]) == 2
    revised = await propose(
        session, proposal_id=pid, revision=amended["revision"],
        updates=[{"event_id": d5["Dinner"], "end_note": note["dinner ran late"], "summary": "Family dinner"}],
        replies=[{"feedback_id": feedback["id"], "reply": "Renamed it Family dinner."}, {"feedback_id": other["id"], "reply": "The 19:15 one."}],
    )
    shown = await session.call("get_proposal", proposal_id=pid, since_revision=amended["revision"])
    assert shown["feedback"][0]["status"] == "answered"
    assert any(e["status"] == "replaced" for e in shown["user_edits"])
    done = await session.call("confirm_proposal", proposal_id=pid, revision=revised["revision"])
    assert done["status"] == "applied"
    assert (await session.call("get_event", id=d5["Dinner"]))["summary"] == "Family dinner"


async def test_suggested_actions_and_kept_priorities(session, plan):
    d4, d5 = plan[date(2026, 10, 4)], plan[FIRST]
    group = (await session.call("create_action_group", group={"name": "Meals", "priority": 1}))["created_id"]
    eat = (await session.call("create_action", action={"name": "eat a meal", "status": "active", "group_id": group}))["created_id"]
    work = (await session.call("create_action", action={"name": "do work", "status": "active", "priority": 3}))["created_id"]
    # Yesterday's breakfast had an action: today's is offered it.
    await session.call("update_event", updates=[{"event": {"id": d4["Breakfast"], "action_ids": [eat]}}, {"event": {"id": d5["Work"], "action_ids": [work]}}])
    await notes(session, ("2026-10-05T07:45:00-04:00", "breakfast done"))
    context = await session.call("prepare_compaction")
    assert any(e["suggested_action_ids"] for e in context["events"])
    note = ids_of(context)
    proposed = await propose(session, updates=[{"event_id": d5["Breakfast"], "end_note": note["breakfast done"], "action_ids": [eat]}])
    assert any(c["reason"].startswith("kept the priority") for c in proposed["changes"])
    await session.call_error("compact_notes", recap=RECAP, proposal_id=proposed["proposal_id"], revision=proposed["revision"], updates=[{"event_id": d5["Lunch"], "action_ids": ["eat a meel"]}])
    await session.call("confirm_proposal", proposal_id=proposed["proposal_id"], revision=proposed["revision"])
    assert (await session.call("get_event", id=d5["Work"]))["priority"] == 3


async def test_habit_backfill(session, plan):
    d5, d6 = plan[FIRST], plan[date(2026, 10, 6)]
    eat = (await session.call("create_action", action={"name": "eat a meal", "status": "active"}))["created_id"]
    await session.call("create_trait", trait={
        "name": "Mindful", "definition": "Present at meals.",
        "parts": [{"kind": "judgment", "rubric": "How present was it?", "ratings": {"0": "distracted", "1": "partly", "2": "present"}, "facts": ["action", {"fact": "action_history", "lookback_days": 7}, "general_notes"]}],
    })
    habit = (await session.call("create_habit", habit={"name": "Eat slowly", "action_id": eat, "note": "no screens"}))["created_id"]
    await session.call("update_event", updates=[
        {"event": {"id": d5["Lunch"], "action_ids": [eat], "facts": {"notes": {"self": "slow lunch"}}}},
        {"event": {"id": d6["Dinner"], "action_ids": [eat], "facts": {"notes": {"self": "rushed"}}}},
    ])
    await session.call_error("prepare_habit_judgments", habit_id=habit)
    await notes(session, ("2026-10-06T08:00:00-04:00", "work"))
    proposed = await propose(session)
    await session.call("confirm_proposal", proposal_id=proposed["proposal_id"], revision=proposed["revision"])
    due = await session.call("prepare_habit_judgments", habit_id="Eat slowly")
    requests = [f"{e['event_id']}/{person}/{part}" for e in due["events"] for person, parts in e["people"].items() for part in parts]
    assert requests
    await session.call_error("record_judgments", compaction_id="habit:" + habit + "@notadate", judgments=[])
    await session.call_error("record_judgments", compaction_id=due["backfill_id"], judgments=[{"request_id": requests[0], "rating": 1, "reasoning": "x"}, {"request_id": requests[0], "rating": 1, "reasoning": "y"}])
    await session.call_error("record_judgments", compaction_id=due["backfill_id"], judgments=[{"request_id": requests[0], "rating": 1, "reasoning": " "}])
    await session.call_error("record_judgments", compaction_id=due["backfill_id"], judgments=[{"request_id": requests[0], "rating": 1, "reasoning": "x" * 301}])
    partial = await session.call("record_judgments", compaction_id=due["backfill_id"], judgments=[{"request_id": requests[0], "rating": 2, "reasoning": "Slow, as the note says."}])
    assert partial["remaining"] == requests[1:]
    redo = await session.call("prepare_habit_judgments", habit_id=habit, since="2026-10-01T00:00:00-04:00", redo=True)
    assert redo["events"]
    await session.call("update_habit", habit={"id": habit, "status": "archived"})
    await session.call_error("prepare_habit_judgments", habit_id=habit)


async def test_judgments_redone(session, plan):
    d5 = plan[FIRST]
    friend = (await session.call("create_person", person={"name": "Robin Example"}, facts={"thoughtful": {"what_matters": "time outdoors"}}))["created_id"]
    await session.call("create_trait", trait={
        "name": "Close", "parts": [{"kind": "judgment", "rubric": "How connected?", "ratings": {"0": "apart", "1": "close"}, "facts": ["general_notes", {"fact": "location_history", "lookback_days": 14}], "engagement_type": "with"}],
    })
    await notes(session, ("2026-10-05T12:30:00-04:00", "lunch with Robin"))
    note = ids_of(await session.call("prepare_compaction"))
    proposed = await propose(session, updates=[{"event_id": d5["Lunch"], "facts": {"with_ids": [friend], "notes": {friend: "laughed a lot"}}}])
    applied = await session.call("confirm_proposal", proposal_id=proposed["proposal_id"], revision=proposed["revision"])
    assert applied["status"] == "applied"
    due = await session.call("prepare_judgments")
    requests = [f"{e['event_id']}/{person}/{part}" for e in due["events"] for person, parts in e["people"].items() for part in parts]
    await session.call("record_judgments", compaction_id=due["compaction_id"], judgments=[{"request_id": r, "rating": 1, "reasoning": "  close   \n  and warm "} for r in requests])
    redo = await session.call("prepare_judgments", compaction_id=due["compaction_id"], redo=True)
    assert redo["events"][0]["current"]
    await session.call("record_judgments", compaction_id=due["compaction_id"], judgments=[{"request_id": requests[0], "rating": 0, "reasoning": "on second thought"}])
    await session.call("get_event", id=d5["Lunch"])
    assert note


async def test_trait_facts_kept(session, plan):
    d5 = plan[FIRST]
    friend = (await session.call("create_person", person={"name": "Robin Example"}, facts={"thoughtful": {"what_matters": "- time outdoors"}}))["created_id"]
    await notes(session, ("2026-10-05T12:30:00-04:00", "lunch with Robin, who starts a new job Monday"))
    context = await session.call("prepare_compaction")
    assert any(f["fact"] == "thoughtful/what_matters" for f in context["trait_facts"])
    robin = next(p for p in context["people"] if p["id"] == friend)
    assert {"fact": "thoughtful/what_matters", "summary": "- time outdoors"} in robin["facts"]
    lunch = {"event_id": d5["Lunch"], "facts": {"with_ids": [friend], "logs": {friend: {"thoughtful/what_matters": "Starts a new job Monday."}}}}
    summary = {"person_id": friend, "fact": "thoughtful/what_matters", "summary": "- time outdoors\n- a new job, from Monday"}
    await session.call_error("compact_notes", recap=RECAP, updates=[lunch])
    await session.call_error("compact_notes", recap=RECAP, fact_summaries=[summary])
    proposed = await propose(session, updates=[lunch], fact_summaries=[summary])
    shown = await session.call("get_proposal")
    assert shown["fact_summaries"] == [{**summary, "previous": "- time outdoors"}]
    await session.call("confirm_proposal", proposal_id=proposed["proposal_id"], revision=proposed["revision"])
    assert (await session.call("get_person", id_or_name=friend))["facts"]["thoughtful"]["what_matters"] == summary["summary"]
    logs = await session.call("get_fact_logs", person_id=friend)
    assert [(log["event_id"], log["log"]) for log in logs] == [(d5["Lunch"], "Starts a new job Monday.")]
    due = await session.call("prepare_judgments")
    assert due["facts"][friend]["thoughtful/what_matters"] == summary["summary"]


async def test_refusals(session, plan):
    d5 = plan[FIRST]
    await notes(session, ("2026-10-05T07:45:00-04:00", "finished breakfast"), ("2026-10-05T12:30:00-04:00", "lunch"))
    note = ids_of(await session.call("prepare_compaction"))
    breakfast, lunch = note["finished breakfast"], note["lunch"]
    for arguments in [
        {"ignore_notes": ["nosuch#9"]},
        {"annotate_notes": [{"note_id": lunch, "event_id": d5["Work"]}, {"note_id": lunch, "event_id": d5["Lunch"]}]},
        {"annotate_notes": [{"note_id": lunch, "event_id": "nosuch"}]},
        {"annotate_notes": [{"note_id": "nosuch#9", "event_id": d5["Lunch"]}]},
        {"ignore_notes": [lunch], "annotate_notes": [{"note_id": lunch, "event_id": d5["Lunch"]}]},
        {"creates": [{"summary": "Nap", "start": "2026-10-05T14:00:00-04:00"}]},
        {"creates": [{"summary": "Nap", "start_note": lunch, "end": "2026-10-05T12:40:00-04:00", "key": "abcabcabcabcc7"}]},
        {"creates": [{"summary": "A", "ref": "x", "start": "2026-10-05T19:10:00-04:00", "end": "2026-10-05T19:20:00-04:00"}, {"summary": "B", "ref": "x", "start": "2026-10-05T19:30:00-04:00", "end": "2026-10-05T19:40:00-04:00"}]},
        {"creates": [{"summary": "Overlaps", "start": "2026-10-05T11:00:00-04:00", "end": "2026-10-05T12:30:00-04:00"}]},
        {"updates": [{"event_id": d5["Breakfast"], "end_note": breakfast}], "cancels": [{"event_id": d5["Breakfast"], "counts_against_follow_through": False}]},
        {"updates": [{"event_id": d5["Lunch"], "facts": {"with_ids": ["nobody"]}}]},
        {"updates": [{"event_id": d5["Lunch"], "facts": {"location_ids": ["nowhere"]}}]},
        {"updates": [{"event_id": d5["Lunch"], "facts": {"with_ids": ["self"]}}]},
        {"updates": [{"event_id": d5["Lunch"], "action_ids": ["nosuch"]}]},
        {"updates": [{"event_id": d5["Lunch"], "annotate": "x" * 9000}]},
        {"new_actions": [{"ref": "walk", "name": "go for a walk"}]},
        {"new_actions": [{"ref": "new:a", "name": "go for a walk"}, {"ref": "new:a", "name": "go for a run"}]},
        {"new_people": [{"ref": "new:me", "name": "Me"}]},
        {"new_locations": [{"ref": "new:x", "name": ""}]},
        {"recap": {"overview": " ", "did": [], "unsure": [{"text": "", "event_ids": ["nosuch"], "note_ids": ["nosuch#1"]}]}},
    ]:
        await session.call_error("compact_notes", **{"recap": RECAP, **arguments})
    proposed = await propose(session, updates=[{"event_id": d5["Breakfast"], "end_note": breakfast}])
    pid, revision = proposed["proposal_id"], proposed["revision"]
    await session.call_error("compact_notes", recap=RECAP, updates=[{"event_id": d5["Breakfast"], "end_note": breakfast}])
    await session.call_error("compact_notes", recap=RECAP, proposal_id=pid)
    await session.call_error("compact_notes", recap=RECAP, proposal_id=pid, revision=revision, creates=[{"summary": "Nap", "start": "2026-10-05T14:00:00-04:00", "end": "2026-10-05T14:30:00-04:00", "key": pid + "c9"}])
    for arguments in [
        {},
        {"through": "2026-10-01T00:00:00-04:00"},
        {"through": "2026-10-09T00:00:00-04:00"},
        {"updates": [{"event": {"id": d5["Lunch"], "is_cancelled": True}}]},
        {"updates": [{"event": {"id": d5["Lunch"], "judgments": {"self": {}}}}]},
        {"updates": [{"event": {"id": d5["Lunch"]}, "clear_fields": ["priority"]}]},
        {"updates": [{"event": {"id": d5["Lunch"], "description": "x"}, "clear_fields": ["description"]}]},
        {"updates": [{"event": {"summary": "No id"}}]},
        {"notes": [{"note_id": "nosuch#9", "use": "ignore"}]},
        {"notes": [{"note_id": lunch, "use": "annotate", "event_id": "nosuch"}]},
        {"updates": [{"event": {"id": d5["Lunch"], "start": "2026-10-05T07:15:00-04:00"}}]},
    ]:
        await session.call_error("amend_proposal", proposal_id=pid, revision=revision, **arguments)
    await session.call_error("amend_proposal", proposal_id=pid, revision=revision + 3, as_planned=[d5["Breakfast"]])
    await session.call_error("confirm_proposal", proposal_id=pid, revision=revision + 1)
    await session.call_error("finish_proposal", proposal_id=pid)
    await session.call_error("withdraw_proposal_note", feedback_id=pid + "f7")
    await session.call_error("get_proposal", proposal_id=pid + "x")
    await session.call_error("prepare_judgments", compaction_id=pid + "r1")
    extended = await session.call("amend_proposal", proposal_id=pid, revision=revision, through="2026-10-07T15:30:00-04:00", updates=[{"event": {"id": d5["Lunch"], "summary": "Lunch break", "location": "Park", "priority": 1}, "start_note": lunch}])
    # Nothing moves to make room for what the user adds.
    await session.call_error("amend_proposal", proposal_id=pid, revision=extended["revision"], creates=[{"summary": "Nap", "start": "2026-10-05T14:00:00-04:00", "end": "2026-10-05T14:30:00-04:00"}])
    await session.call("amend_proposal", proposal_id=pid, revision=extended["revision"], creates=[{"summary": "Nap", "start": "2026-10-05T19:30:00-04:00", "end": "2026-10-05T20:00:00-04:00"}], cancels=[{"event_id": d5["Afternoon work"], "counts_against_follow_through": True}])
    abandoned = await session.call("abandon_compaction", proposal_id=pid)
    assert abandoned["status"] == "abandoned"
    # A revision by its own id: a compaction from before proposals, as far
    # as abandoning goes.
    await session.attempt("abandon_compaction", proposal_id=pid + "r1")
    await session.call_error("abandon_compaction", proposal_id="nosuch")
    await session.call_error("confirm_proposal", proposal_id=pid, revision=revision)
    await session.call("prepare_compaction")
