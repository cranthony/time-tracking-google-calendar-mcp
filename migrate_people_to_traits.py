"""Move people goals from cadence sub-goals to a traits measure: each
person's sub-goals (a visit every 21 days, a call every week) become their
own Reliable parts -- count parts by activity, see utilities/traits.py --
and are archived, and past events tagged with one gain facets saying who
they were with and what they were, so the activity cadences have a
history to count.

    python migrate_people_to_traits.py plan.json            # preview only
    python migrate_people_to_traits.py plan.json --apply    # write it

The plan names goals by id only, so keep it out of the repo:

    {
      "people": {
        "<person goal id>": {
          "<sub-goal id>": "visit",   # a cadence of that activity
          "<sub-goal id>": null,      # a cadence of any event with them
          "<sub-goal id>": false      # not a cadence (say, a subjective
                                      # question): just archived
        }
      },
      "adjust": {
        "<person goal id>": {
          "continuity_days": 1,       # instead of the shortest cadence
          "cadences": [               # more cadences, not from a sub-goal
            {"activity": "dinner", "interval_days": 7}
          ]
        }
      },
      "switch_series": true,
      "backfill_days": 365
    }

For each person:

- **Measure.** `{"kind": "traits", "traits": "all", "parts": {"reliable":
  [...]}}`: continuity (the last event with them within, and the next
  within, the shortest of their cadences' days), follow-through, and a
  count part for each cadence sub-goal with a count measure (its target,
  interval and zero-at, and the activity), plus any `adjust` cadences
  (`target` 1 unless given). A person goal that has a count measure of
  its own (and no cadences) keeps it as a cadence of any event. `adjust`'s
  `continuity_days` sets the continuity window instead.
- **Sub-goals** in the plan are archived, which keeps their history; their
  events still count toward the person, whose sub-goals they stay.
- **Past events** (the last `backfill_days`) tagged with one of the
  sub-goals are given the person instead (so they keep a label: an
  archived goal holds none), and, if they have no facets yet, facets of
  `{"with": [person], "activity": <its sub-goal's activity>}`.
- **Future events** that aren't part of a recurring series are given the
  person instead of the sub-goal too. A recurring series tagged with one
  is only listed -- or, with `switch_series`, given the person from its
  next event on ("this and following", see update_recurrence), which
  leaves its past events as they are: editing a whole series would reset
  their own fields.

Nothing is written without --apply; the preview says what would be.
Run it with the main checkout's credentials.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from calendar_clients.google_calendar import Event
from calendar_clients.write_lock import WRITE_LOCK
from config import build_calendar_client, build_goals, build_traits
from utilities.facets import Facets
from utilities.goal_calendar import GoalCalendar, fill_in_from_goals
from utilities.goal_sheet import Goal
from utilities.goals import Goals, GoalTree
from utilities.recurrences import Recurrences
from utilities.traits import activity_label

DEFAULT_WITHIN_DAYS = 14


def reliable_parts(
    person: Goal, cadences: dict[str, str | None | bool], tree: GoalTree, adjust: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """The person's own Reliable parts, from their cadence sub-goals and
    `adjust` (see the module docstring)."""
    adjust = adjust or {}
    counts = []
    for sub_goal_id, activity in cadences.items():
        if activity is False:
            continue
        measure = tree.by_id[sub_goal_id].measure or {}
        if measure.get("kind") != "count":
            continue
        part: dict[str, Any] = {"kind": "count", "target": measure.get("target", 1)}
        for name in ("interval_days", "zero_at_days"):
            if name in measure:
                part[name] = measure[name]
        part.setdefault("interval_days", 1)
        if isinstance(activity, str):
            part["activity"] = activity_label(activity)
        counts.append(part)
    for extra in adjust.get("cadences", []):
        part = {"kind": "count", "target": extra.get("target", 1), "interval_days": extra["interval_days"]}
        if "zero_at_days" in extra:
            part["zero_at_days"] = extra["zero_at_days"]
        if extra.get("activity"):
            part["activity"] = activity_label(extra["activity"])
        counts.append(part)
    own = person.measure or {}
    if not counts and own.get("kind") == "count":
        counts.append(
            {"kind": "count", **{k: own[k] for k in ("target", "interval_days", "zero_at_days") if k in own}}
        )
    shortest = adjust.get("continuity_days") or min((p["interval_days"] for p in counts), default=DEFAULT_WITHIN_DAYS)
    return [
        {"kind": "continuity", "last_within_days": shortest, "next_within_days": shortest},
        {"kind": "follow_through"},
        *counts,
    ]


def plan_events(
    events: list[Event], people: dict[str, dict[str, Any]], now: datetime
) -> tuple[list[tuple[Event, Event]], list[Event]]:
    """The patches to make -- (event, patch) -- and the recurring events
    to change by hand (see the module docstring)."""
    owner = {sub: person for person, subs in people.items() for sub in subs}
    patches, by_hand = [], []
    for event in events:
        ids = list(event.goal_ids or ())
        mine = [g for g in ids if g in owner]
        if not mine or not event.id:
            continue
        past = event.start < now
        if not past and event.recurring_event_id:
            by_hand.append(event)
            continue
        person = owner[mine[0]]
        retagged = list(dict.fromkeys(owner.get(g, g) for g in ids))
        patch = Event(id=event.id, goal_ids=retagged)
        if past and (event.facets is None or event.facets.is_empty()):
            activity = next((people[owner[g]][g] for g in mine if isinstance(people[owner[g]][g], str)), None)
            patch.facets = Facets(
                with_goal_ids=[person], activity=activity_label(activity) if activity else None
            )
        patches.append((event, patch))
    return patches, by_hand


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("plan", help="the plan, as JSON (see the module docstring)")
    parser.add_argument("--apply", action="store_true", help="write it, rather than only preview it")
    args = parser.parse_args(argv)
    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)
    people: dict[str, dict[str, Any]] = plan["people"]
    days = plan.get("backfill_days", 365)

    with WRITE_LOCK:
        traits = build_traits()
        goals: Goals = build_goals(trait_ids=lambda: [t.id for t in traits.all()])
        tree = goals.tree()
        client = build_calendar_client()
        problems = [
            f"{goal_id} isn't a goal"
            for goal_id in [*people, *(s for subs in people.values() for s in subs)]
            if goal_id not in tree.by_id
        ]
        problems += [
            f"{sub} isn't under {person}"
            for person, subs in people.items() for sub in subs
            if sub in tree.by_id and not tree.under(sub, person)
        ]
        if problems:
            print("Refused:", "; ".join(problems))
            return 1

        adjusts = plan.get("adjust", {})
        measures = {
            person: {
                "kind": "traits",
                "traits": "all",
                "parts": {"reliable": reliable_parts(tree.by_id[person], subs, tree, adjusts.get(person))},
            }
            for person, subs in people.items()
        }
        now = datetime.now(client.get_time_zone())
        events = [
            e for e in fill_in_from_goals(client.list_events(now - timedelta(days=days), now + timedelta(days=365)), tree)
            if e.status != "cancelled"
        ]
        patches, by_hand = plan_events(events, people, now)

        for person, measure in measures.items():
            print(f"{person}: {json.dumps(measure)}")
            archived = [s for s in people[person] if tree.by_id[s].status != "archived"]
            print(f"  archive: {', '.join(archived) or 'nothing'}")
        given = Counter(p.facets.activity or "(any)" for _, p in patches if p.facets is not None)
        retagged = sum(1 for e, p in patches if p.goal_ids != e.goal_ids)
        print(f"\n{len(patches)} events to patch: {retagged} given the person instead of a sub-goal; "
              f"facets for {sum(given.values())} ({', '.join(f'{a} ×{n}' for a, n in given.most_common())})")
        # Each series' next event, and its goals from then on.
        owner = {sub: person for person, subs in people.items() for sub in subs}
        series: dict[str, tuple[Event, list[str]]] = {}
        for event in sorted(by_hand, key=lambda e: e.start):
            if event.recurring_event_id not in series:
                retagged = list(dict.fromkeys(owner.get(g, g) for g in event.goal_ids or ()))
                series[event.recurring_event_id] = (event, retagged)
        switch = bool(plan.get("switch_series"))
        if series:
            print(
                f"\nRecurring series tagged with a sub-goal ({len(series)}): "
                + ("given the person from their next event on" if switch else "to change by hand")
            )
            for series_id, (event, retagged) in series.items():
                print(
                    f"  {series_id}: {event.summary!r} from {event.start:%Y-%m-%d}, "
                    f"{', '.join(event.goal_ids or ())} -> {', '.join(retagged)}"
                )
        if not args.apply:
            print("\nNothing written: run again with --apply to write it.")
            return 0

        for person, measure in measures.items():
            goals.update_goal(Goal(id=person, measure=measure))
        writer = GoalCalendar(client, goals)
        for _, patch in patches:
            writer.update_event(patch)
        if switch:
            recurrences = Recurrences(writer, client.list_instances, client.get_time_zone)
            for series_id, (event, retagged) in series.items():
                recurrences.update(Event(id=series_id, goal_ids=retagged), starting_at=event.id)
        for subs in people.values():
            for sub in subs:
                if goals.tree().by_id[sub].status != "archived":
                    goals.update_goal(Goal(id=sub, status="archived"))
        print(f"\nWritten: {len(measures)} measures, {len(patches)} events, sub-goals archived.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
