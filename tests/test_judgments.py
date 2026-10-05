from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from calendar_clients.google_calendar import Event
from utilities.actions import Action
from utilities.facts import Facts
from utilities.judgments import Judging, Judgment
from utilities.locations import Location
from utilities.people import Person
from utilities.traits import Trait

_DAY = datetime(2026, 10, 5, 18, tzinfo=timezone.utc)

_NEW = {
    "kind": "judgment",
    "rubric": "Was this activity or place new?",
    "ratings": {"0": "routine", "1": "a twist", "2": "new", "3": "adventurous"},
    "facts": ["action", {"fact": "action_history", "lookback_days": 30}, "location", "location_history"],
}
_FOR = {
    "kind": "judgment",
    "engagement_type": "for",
    "rubric": "Was it built on what matters to them?",
    "ratings": {"0": "no", "1": "yes"},
    "facts": ["action", "person_notes"],
}
_WITH_NOTES = {
    "kind": "judgment",
    "rubric": "Were they heard?",
    "ratings": {"0": "no", "1": "yes"},
    "facts": ["person_notes", "general_notes"],
}
_TRAITS = [
    Trait(id="adventurous", name="Adventurous", status="active", definition="New things together.", parts=[_NEW]),
    Trait(id="thoughtful", name="Thoughtful", status="active", parts=[_WITH_NOTES, _FOR, {"kind": "follow_through"}]),
    Trait(id="off", name="Off", status="off", parts=[_NEW]),
]


def _event(event_id, days_ago=0, **fields) -> Event:
    start = _DAY - timedelta(days=days_ago)
    return Event(id=event_id, summary=fields.pop("summary", "Dinner"), start=start, end=start + timedelta(hours=2), **fields)


class _Client:
    def __init__(self, events):
        self.events = {e.id: e for e in events}
        self.updates = []

    def list_events(self, time_min, time_max):
        return [e for e in self.events.values() if e.end > time_min and e.start < time_max]

    def get_event(self, event_id):
        return self.events[event_id]

    def update_event(self, event):
        self.updates.append(event)
        self.events[event.id] = replace(self.events[event.id], judgments=event.judgments)
        return event


def _judging(events, people=None, traits=_TRAITS) -> tuple[Judging, _Client]:
    client = _Client(events)
    stores = {name: MagicMock() for name in ("actions", "people", "locations", "traits")}
    stores["actions"].all.return_value = [Action(id="cook", name="Cook"), Action(id="hike", name="Hike")]
    stores["people"].all.return_value = people or [
        Person(id="self", name="Me"), Person(id="sam", name="Sam", context="salsa"), Person(id="mom", name="Mom"),
    ]
    stores["locations"].all.return_value = [Location(id="home", name="Home"), Location(id="peak", name="The peak")]
    stores["traits"].all.return_value = list(traits)
    return Judging(client=client, **stores), client


_TONIGHT = _event(
    "e1", action_ids=["hike"], description="Long climb",
    facts=Facts(location_id="peak", with_ids=["sam"], for_ids=["mom"], notes={"self": "tired", "sam": "loved it"}),
)
_SPAN = (_TONIGHT.start, _TONIGHT.end)


class TestRequests:
    def test_one_per_person_trait_and_judgment_part_by_engagement(self):
        judging, _ = _judging([_TONIGHT])

        requests = judging.requests(["e1"], _SPAN)

        assert [r.id for r in requests] == [
            "e1/self/adventurous/judgment",
            "e1/self/thoughtful/judgment",
            "e1/sam/adventurous/judgment",
            "e1/sam/thoughtful/judgment",
            "e1/mom/thoughtful/judgment#2",  # Mom wasn't there: only "for" parts.
        ]

    def test_resolves_the_facts_each_part_names_for_its_person(self):
        judging, _ = _judging([_TONIGHT])

        by_id = {r.id: r for r in judging.requests(["e1"], _SPAN)}

        new = by_id["e1/sam/adventurous/judgment"]
        assert new.facts["action"] == ["Hike"]
        assert new.facts["location"] == "The peak"
        assert by_id["e1/sam/thoughtful/judgment"].facts == {"person_notes": "loved it", "general_notes": "Long climb"}
        # A "for" engagement's notes are the user's own.
        assert by_id["e1/mom/thoughtful/judgment#2"].facts == {"action": ["Hike"], "person_notes": "tired"}
        assert (new.ratings, new.engagement, new.trait_name) == (_NEW["ratings"], "with", "Adventurous")

    def test_frames_each_judgment_for_one_person(self):
        judging, _ = _judging([_TONIGHT])

        by_id = {r.id: r for r in judging.requests(["e1"], _SPAN)}

        assert by_id["e1/sam/adventurous/judgment"].framing.startswith(
            'For the trait Adventurous (New things together.), judge how "Dinner" went for Sam (one person, salsa), '
            "who was there: Was this activity or place new? Rate it 0-3"
        )
        assert "for the user themself, who was there" in by_id["e1/self/adventurous/judgment"].framing
        assert "for whom the user did this while they weren't there" in by_id["e1/mom/thoughtful/judgment#2"].framing

    def test_history_is_what_they_did_together_within_the_lookback(self):
        earlier = [
            _event("e2", days_ago=3, action_ids=["cook"], facts=Facts(location_id="home", with_ids=["sam"])),
            _event("e3", days_ago=5, action_ids=["cook"], facts=Facts(location_id="home")),  # without Sam
            _event("e4", days_ago=40, action_ids=["hike"], facts=Facts(location_id="peak", with_ids=["sam"])),  # too long ago
        ]
        judging, _ = _judging([_TONIGHT, *earlier])

        by_id = {r.id: r for r in judging.requests(["e1"], _SPAN)}

        sam = by_id["e1/sam/adventurous/judgment"].facts
        assert sam["action_history"] == {"days": 30, "times": {"Cook": 1}}
        assert sam["location_history"] == {"days": 30, "times": {"Home": 1}}
        # The user was at all of them.
        assert by_id["e1/self/adventurous/judgment"].facts["action_history"] == {"days": 30, "times": {"Cook": 2}}

    def test_a_persons_traits_choose_and_replace(self):
        people = [
            Person(id="self", name="Me", traits={"select": ["thoughtful"]}),
            Person(id="sam", name="Sam", traits={"parts": {"adventurous": [{**_NEW, "rubric": "New to Sam?"}]}}),
        ]
        event = replace(_TONIGHT, facts=Facts(with_ids=["sam"]))
        judging, _ = _judging([event], people=people)

        requests = judging.requests(["e1"], _SPAN)

        assert [r.id for r in requests] == [
            "e1/self/thoughtful/judgment", "e1/sam/adventurous/judgment", "e1/sam/thoughtful/judgment",
        ]
        assert requests[1].rubric == "New to Sam?"

    def test_skips_events_without_facts_cancelled_ones_and_ones_not_asked_for(self):
        events = [
            _TONIGHT,
            _event("bare", summary="Email"),
            replace(_TONIGHT, id="gone", status="cancelled"),
        ]
        judging, _ = _judging(events)

        assert {r.event_id for r in judging.requests(["bare", "gone"], _SPAN)} == set()
        assert judging.requests([], _SPAN) == []

    def test_made_judgments_are_left_out_unless_redone(self):
        made = {"sam": {"adventurous": {"judgment": {"rating": 2, "scale": 3, "reasoning": "First climb."}}}}
        judging, _ = _judging([replace(_TONIGHT, judgments=made)])

        assert "e1/sam/adventurous/judgment" not in [r.id for r in judging.requests(["e1"], _SPAN)]
        redo = {r.id: r for r in judging.requests(["e1"], _SPAN, include_judged=True)}
        assert redo["e1/sam/adventurous/judgment"].current == made["sam"]["adventurous"]["judgment"]


class TestRecord:
    def test_writes_each_judgment_beside_the_ones_made_with_its_scale(self):
        made = {"sam": {"thoughtful": {"judgment": {"rating": 1, "scale": 1, "reasoning": "Listened."}}}}
        judging, client = _judging([replace(_TONIGHT, judgments=made)])
        requests = judging.requests(["e1"], _SPAN, include_judged=True)

        recorded = judging.record(
            requests,
            [
                Judgment(request_id="e1/sam/adventurous/judgment", rating=3, reasoning="  Their first   summit. "),
                Judgment(request_id="e1/self/adventurous/judgment", rating=2, reasoning="A new trail."),
            ],
        )

        assert recorded == 2
        (update,) = client.updates
        assert update.judgments == {
            "sam": {
                "thoughtful": {"judgment": {"rating": 1, "scale": 1, "reasoning": "Listened."}},
                "adventurous": {"judgment": {"rating": 3, "scale": 3, "reasoning": "Their first summit."}},
            },
            "self": {"adventurous": {"judgment": {"rating": 2, "scale": 3, "reasoning": "A new trail."}}},
        }

    @pytest.mark.parametrize(
        "judgment, problem",
        [
            (Judgment(request_id="e1/x/y/z", rating=1, reasoning="r"), "isn't one of the compaction's judgment requests"),
            (Judgment(request_id="e1/sam/adventurous/judgment", rating=4, reasoning="r"), "the rating 4 isn't one of its ratings (0, 1, 2, 3)"),
            (Judgment(request_id="e1/sam/adventurous/judgment", rating=1, reasoning=" "), "give a line of reasoning"),
            (Judgment(request_id="e1/sam/adventurous/judgment", rating=1, reasoning="x" * 301), "keep the reasoning to one line"),
        ],
    )
    def test_refuses_what_doesnt_answer_a_request_before_writing_anything(self, judgment, problem):
        judging, client = _judging([_TONIGHT])
        requests = judging.requests(["e1"], _SPAN)

        with pytest.raises(ValueError, match=problem.replace("(", r"\(").replace(")", r"\)")):
            judging.record(requests, [Judgment(request_id="e1/self/adventurous/judgment", rating=1, reasoning="ok"), judgment])
        assert client.updates == []
