from datetime import date, datetime, timedelta, timezone
from dataclasses import replace

import pytest

from calendar_clients.google_calendar import Event, EventLabel as RawEventLabel, EventLabelConflictError, color_for_priority
from tests.fake_sheets import FakeSheets
from utilities import calendar_metadata_sheet, goals as goals_module
from utilities.goal_sheet import Goal, GoalSheet
from utilities.goals import OVERALL_ID, Goals, GoalTree

_TODAY = date(2026, 10, 2)
_PRIORITY_1_COLOR = color_for_priority(1)[1]
_SPREADSHEET_KEY = "calendar-metadata-spreadsheet-id"
_UNNAMED = RawEventLabel(id="default-1", background_color="#039be5")
"""One of Calendar's own unnamed labels, which goals always leave alone."""


class FakeLabelCalendar:
    """The label and metadata calls `Goals` makes on a CalendarClient,
    with the real etag check on writes."""

    def __init__(self, labels=(), *, spreadsheet_id: str | None = "spreadsheet-1"):
        self.labels = [replace(label) for label in labels]
        self.metadata = {_SPREADSHEET_KEY: spreadsheet_id} if spreadsheet_id else {}
        self.version = 0
        self.writes = 0

    def get_calendar_metadata(self, key):
        return self.metadata.get(key)

    def set_calendar_metadata(self, key, value):
        self.metadata[key] = value

    def list_event_labels(self):
        return [replace(label) for label in self.labels], f"etag-{self.version}"

    def replace_event_labels(self, labels, etag=None):
        if etag is not None and etag != f"etag-{self.version}":
            raise EventLabelConflictError("stale etag")
        self.labels = [replace(label) for label in labels]
        self.version += 1
        self.writes += 1
        return [replace(label) for label in self.labels]

    def named(self):
        return {label.id: (label.name, label.background_color) for label in self.labels if label.name}


def _goals(calendar=None, sheets=None) -> tuple[Goals, FakeLabelCalendar, FakeSheets]:
    calendar = calendar if calendar is not None else FakeLabelCalendar([_UNNAMED])
    sheets = sheets if sheets is not None else FakeSheets()
    return Goals(calendar, sheets, today=lambda: _TODAY), calendar, sheets


def _others(goals):
    """`goals` but the overall goal, which every tree has."""
    return [goal for goal in goals if goal.id != OVERALL_ID]


def _by_name(goals: Goals) -> dict[str, Goal]:
    return {goal.name: goal for goal in goals.tree().goals}


class TestMigration:
    def test_migrates_the_calendars_named_labels_into_active_top_level_goals(self):
        calendar = FakeLabelCalendar(
            [
                RawEventLabel(id="l1", name="Cooking", background_color="#111111"),
                _UNNAMED,  # Calendar's own: not a goal
                RawEventLabel(id="l3", name="cooking", background_color="#333333"),  # same name, different case
            ]
        )

        goals, _, _ = _goals(calendar)

        migrated = _others(goals.tree().goals)
        assert [(g.name, g.label_id, g.background_color, g.active, g.parent_id) for g in migrated] == [
            ("Cooking", "l1", "#111111", True, None),
            ("cooking (2)", "l3", "#333333", True, None),
        ]
        assert len(migrated[0].id) == 6
        assert calendar.writes == 0  # migrating changes no labels

    def test_leaves_an_old_event_labels_tab_alone(self):
        # Goals replaced it; it's left for the user to delete.
        sheets = FakeSheets()
        sheets.create_sheet_metadata("spreadsheet-1", 7, "sheet-role", "event-labels")
        sheets.titles[7] = "Event Labels"
        sheets.write_rows_in_sheet("spreadsheet-1", 7, "A1:C2", [["id", "name", "background_color"], ["x", "Old", "#000000"]])
        calendar = FakeLabelCalendar([RawEventLabel(id="l1", name="Reading", background_color="#222222")])

        goals, _, _ = _goals(calendar, sheets)

        assert [g.name for g in _others(goals.tree().goals)] == ["Reading"]
        assert sheets.titles[7] == "Event Labels"
        assert sheets.read_rows_in_sheet("spreadsheet-1", 7, "A2:C") == [["x", "Old", "#000000"]]

    def test_a_new_spreadsheet_uses_its_first_tab(self):
        calendar = FakeLabelCalendar([], spreadsheet_id=None)

        goals, _, sheets = _goals(calendar)

        assert goals.spreadsheet_id == "new-spreadsheet"
        assert sheets.find_sheet_id("new-spreadsheet", "sheet-role", "goals") == 0

    def test_migrates_only_once(self):
        sheets = FakeSheets()
        calendar = FakeLabelCalendar([RawEventLabel(id="l1", name="Cooking", background_color="#111111")])
        first, _, _ = _goals(calendar, sheets)

        second, _, _ = _goals(calendar, sheets)

        assert [g.id for g in second.tree().goals] == [g.id for g in first.tree().goals]

    def test_a_tab_is_only_tagged_once_its_rows_are_written(self):
        sheets = FakeSheets()

        def fail(sheet_id):
            raise RuntimeError("write failed")

        with pytest.raises(RuntimeError):
            calendar_metadata_sheet.create_tab(
                sheets, "spreadsheet-1", role="goals", title="Goals", populate=fail
            )

        assert GoalSheet.find(sheets, "spreadsheet-1") is None


class TestCreateGoal:
    def test_assigns_ids_and_adds_its_label(self):
        goals, calendar, _ = _goals()

        result = goals.create_goal(Goal(name="Cooking", priority=1))

        cooking = _by_name(goals)["Cooking"]
        assert len(cooking.id) == 6 and cooking.active is True
        assert cooking.label_id == str(goals_module.uuid.uuid5(goals_module._LABEL_ID_NAMESPACE, cooking.id))
        assert calendar.named() == {cooking.label_id: ("Cooking", _PRIORITY_1_COLOR)}
        assert _UNNAMED in calendar.labels
        assert [g.name for g in _others(result.goals)] == ["Cooking"]
        assert result.label_slots_used == 2  # the goal's, plus the unnamed one
        assert result.created_id == _by_name(goals)["Cooking"].id

    def test_ignores_read_only_fields(self):
        goals, _, _ = _goals()

        goals.create_goal(Goal(id="mine", label_id="mine", health=50, name="Cooking"))

        cooking = _by_name(goals)["Cooking"]
        assert cooking.health is None
        assert cooking.id != "mine" and cooking.label_id != "mine"

    def test_a_sub_goal_takes_its_color_from_an_ancestors_priority(self):
        goals, calendar, _ = _goals()
        goals.create_goal(Goal(name="Cooking", priority=1))
        parent = _by_name(goals)["Cooking"]

        goals.create_goal(Goal(name="Tofu tikka", parent_id=parent.id))

        tofu = _by_name(goals)["Tofu tikka"]
        assert calendar.named()[tofu.label_id] == ("Tofu tikka", _PRIORITY_1_COLOR)

    def test_a_sub_goal_inherits_its_parents_color_even_over_its_own_priority(self):
        goals, calendar, _ = _goals()
        goals.create_goal(Goal(name="Cooking", background_color="#123456", priority=1))
        parent = _by_name(goals)["Cooking"]
        goals.create_goal(Goal(name="Tofu", parent_id=parent.id))
        goals.create_goal(Goal(name="Curry", parent_id=parent.id, priority=3))

        listed = {g.name: g for g in goals.get_goals().goals}

        assert listed["Tofu"].effective_color == "#123456"
        # A color up the tree wins; a priority's color is the last resort.
        assert listed["Curry"].effective_color == "#123456"
        assert listed["Cooking"].effective_color == "#123456"
        assert listed["Tofu"].background_color is None  # inherited, not its own
        assert calendar.named()[listed["Tofu"].label_id] == ("Tofu", "#123456")

    def test_a_listed_goal_carries_the_priority_it_inherits(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Cooking", priority=1))
        parent = _by_name(goals)["Cooking"]
        goals.create_goal(Goal(name="Tofu", parent_id=parent.id))
        goals.create_goal(Goal(name="Curry", parent_id=parent.id, priority=3))
        goals.create_goal(Goal(name="Reading"))

        listed = {g.name: g for g in goals.get_goals().goals}

        assert listed["Tofu"].effective_priority == 1
        assert listed["Tofu"].priority is None  # inherited, not its own
        assert listed["Curry"].effective_priority == 3
        assert listed["Cooking"].effective_priority == 1
        assert listed["Reading"].effective_priority is None

    def test_an_inactive_goal_takes_no_label(self):
        goals, calendar, _ = _goals()

        goals.create_goal(Goal(name="Someday", status="inactive"))

        assert calendar.named() == {}
        assert _by_name(goals)["Someday"].label_id

    @pytest.mark.parametrize(
        "goal, message",
        [
            (Goal(), "needs a name"),
            (Goal(name="x" * 51), "longer than 50"),
            (Goal(name="Cooking", parent_id="nope"), "parent 'nope' isn't a goal"),
            (Goal(name="Cooking", measure={"target": 3}), 'must be an object with a "kind"'),
            (
                Goal(name="Cooking", measure={"kind": "duration", "target_mins": 300}),
                'measure needs "target_min"; .*measure has no field "target_mins"',
            ),
        ],
    )
    def test_refuses_invalid_goals_without_writing_anything(self, goal, message):
        goals, calendar, _ = _goals()

        with pytest.raises(ValueError, match=message):
            goals.create_goal(goal)

        assert _others(goals.tree().goals) == []
        assert calendar.writes == 0

    def test_refuses_an_invalid_new_measure(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Wake"))
        wake = _by_name(goals)["Wake"]

        with pytest.raises(ValueError, match='"target" must be a time like "07:00"'):
            goals.update_goal(
                Goal(id=wake.id, measure={"kind": "time_constraint", "edge": "start", "target": "7am"})
            )

        assert _by_name(goals)["Wake"].measure is None

    def test_an_old_invalid_measure_doesnt_block_other_edits(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))
        goals.create_goal(Goal(name="Hosting"))
        saved = goals.tree().goals
        cooking = next(g for g in saved if g.name == "Cooking")
        cooking.measure = {"kind": "duration", "target_mins": 300}  # Saved before measures were checked.
        goals._sheet.write(saved)

        goals.update_goal(Goal(id=_by_name(goals)["Hosting"].id, note="Weekly"))
        goals.update_goal(Goal(id=cooking.id, note="Vegetarian"))

        assert _by_name(goals)["Hosting"].note == "Weekly"
        assert _by_name(goals)["Cooking"].note == "Vegetarian"
        with pytest.raises(ValueError, match='has no field "target_mins"'):
            goals.update_goal(Goal(id=cooking.id, measure={"kind": "duration", "target_mins": 600}))

    def test_sync_checks_every_measure(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))
        saved = goals.tree().goals
        saved[0].measure = {"kind": "rollup", "agg": "max"}  # A hand edit to the sheet.
        goals._sheet.write(saved)

        with pytest.raises(ValueError, match="goal '.+'s measure \"agg\" must be one of mean, weighted, percentile"):
            goals.sync()

    def test_a_measures_events_of_must_be_a_goal(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))
        cooking = _by_name(goals)["Cooking"]

        with pytest.raises(ValueError, match="names 'nope', which isn't a goal"):
            goals.update_goal(Goal(id=cooking.id, measure={"kind": "count", "target": 1, "events_of": "nope"}))

    def test_a_measures_only_if_events_of_must_be_a_goal(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))
        cooking = _by_name(goals)["Cooking"]
        measure = {"kind": "subjective", "prompt": "?", "only_if": {"events_of": "nope"}}

        with pytest.raises(ValueError, match="\"only_if\" \"events_of\" names 'nope', which isn't a goal"):
            goals.update_goal(Goal(id=cooking.id, measure=measure))

    def test_sibling_names_must_differ(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))

        with pytest.raises(ValueError, match="same name as its sibling"):
            goals.create_goal(Goal(name="cooking"))

    def test_the_same_name_is_fine_under_different_parents(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))
        goals.create_goal(Goal(name="Hosting"))
        parents = _by_name(goals)

        goals.create_goal(Goal(name="Practice", parent_id=parents["Cooking"].id))
        goals.create_goal(Goal(name="Practice", parent_id=parents["Hosting"].id))

        assert len(_others(goals.tree().goals)) == 4

    def test_refuses_past_the_label_budget_before_writing(self, monkeypatch):
        monkeypatch.setattr(goals_module, "MAX_LABELS", 2)
        goals, calendar, _ = _goals()  # one unnamed label already
        goals.create_goal(Goal(name="One"))

        with pytest.raises(ValueError, match="at most 1 goals can be active"):
            goals.create_goal(Goal(name="Two"))

        assert [g.name for g in _others(goals.tree().goals)] == ["One"]
        # ...but an inactive one still fits.
        goals.create_goal(Goal(name="Two", status="inactive"))

    def test_a_concurrent_label_change_is_refused(self):
        goals, calendar, _ = _goals()
        real_list = calendar.list_event_labels

        def list_then_race():
            result = real_list()
            calendar.version += 1  # someone else wrote in between
            return result

        calendar.list_event_labels = list_then_race

        with pytest.raises(EventLabelConflictError):
            goals.create_goal(Goal(name="Cooking"))


class TestUpdateGoal:
    def test_deactivating_frees_the_label_and_reactivating_restores_the_same_one(self):
        goals, calendar, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))
        cooking = _by_name(goals)["Cooking"]

        result = goals.update_goal(Goal(id=cooking.id, status="inactive"))

        assert calendar.named() == {}
        assert [(g.name, g.status) for g in _others(result.goals)] == [("Cooking", "inactive")]
        assert _by_name(goals)["Cooking"].label_id == cooking.label_id

        goals.update_goal(Goal(id=cooking.id, status="active"))

        assert list(calendar.named()) == [cooking.label_id]

    def test_renaming_renames_the_label(self):
        goals, calendar, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))
        cooking = _by_name(goals)["Cooking"]

        goals.update_goal(Goal(id=cooking.id, name="Vegetarian cooking", background_color="#123456"))

        assert calendar.named() == {cooking.label_id: ("Vegetarian cooking", "#123456")}

    def test_clears_fields(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))
        parent = _by_name(goals)["Cooking"]
        goals.create_goal(Goal(name="Tofu", parent_id=parent.id, priority=2, note="soon"))
        tofu = _by_name(goals)["Tofu"]

        goals.update_goal(Goal(id=tofu.id), ["parent_id", "priority", "note"])

        tofu = _by_name(goals)["Tofu"]
        assert (tofu.parent_id, tofu.priority, tofu.note) == (None, None, None)

    def test_read_only_fields_are_left_alone(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))
        cooking = _by_name(goals)["Cooking"]

        goals.update_goal(Goal(id=cooking.id, label_id="other", health=50, note="hi"))

        updated = _by_name(goals)["Cooking"]
        assert (updated.label_id, updated.health, updated.note) == (cooking.label_id, None, "hi")

    def test_refuses_a_cycle(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))
        parent = _by_name(goals)["Cooking"]
        goals.create_goal(Goal(name="Tofu", parent_id=parent.id))
        child = _by_name(goals)["Tofu"]

        with pytest.raises(ValueError, match="can't be its own ancestor"):
            goals.update_goal(Goal(id=parent.id, parent_id=child.id))

    def test_unknown_ids_get_suggestions(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))

        with pytest.raises(ValueError, match=r"'cooking' isn't a goal; did you mean \w{6} \(Cooking\)"):
            goals.update_goal(Goal(id="cooking", note="x"))

    def test_refuses_clearing_what_cant_be_cleared_or_setting_and_clearing_at_once(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))
        cooking = _by_name(goals)["Cooking"]

        with pytest.raises(ValueError, match="Can't clear"):
            goals.update_goal(Goal(id=cooking.id), ["name"])
        with pytest.raises(ValueError, match="both set and clear"):
            goals.update_goal(Goal(id=cooking.id, note="x"), ["note"])


class TestSync:
    def test_removes_labels_no_active_goal_owns_and_keeps_unnamed_ones(self):
        goals, calendar, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))
        calendar.labels.append(RawEventLabel(id="stray", name="Hand-made", background_color="#000000"))

        goals.sync()

        assert list(calendar.named()) == [_by_name(goals)["Cooking"].label_id]
        assert _UNNAMED in calendar.labels

    def test_writes_nothing_when_already_in_sync(self):
        goals, calendar, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))
        writes = calendar.writes

        goals.sync()

        assert calendar.writes == writes

    def test_refuses_to_wipe_labels_from_an_empty_goals_tab(self):
        calendar = FakeLabelCalendar([RawEventLabel(id="l1", name="Reading", background_color="#222222")])
        goals, _, _ = _goals(calendar)
        goals._sheet.write([])

        with pytest.raises(ValueError, match="refusing to delete them all"):
            goals.sync()

        assert calendar.named() == {"l1": ("Reading", "#222222")}


class TestGetGoals:
    def test_lists_parents_before_children_with_paths(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))
        goals.create_goal(Goal(name="Hosting"))
        cooking = _by_name(goals)["Cooking"]
        goals.create_goal(Goal(name="Tofu", parent_id=cooking.id))
        goals.create_goal(Goal(name="Old", status="inactive"))

        active = goals.get_goals(["active"])
        default = goals.get_goals()

        # The overall goal first, whatever's asked for.
        assert [g.path for g in active.goals] == ["Overall", "Cooking", "Cooking › Tofu", "Hosting"]
        assert [g.path for g in default.goals] == ["Overall", "Cooking", "Cooking › Tofu", "Hosting", "Old"]
        assert active.label_slots_used == 4  # three active goals + the unnamed label
        assert active.label_slots_total == 200

    def test_lists_proposed_active_and_inactive_goals_unless_asked_for_others(self):
        goals, calendar, _ = _goals()
        for status in ("proposed", "active", "inactive", "completed", "archived", "deleted"):
            goals.create_goal(Goal(name=status.title(), status=status))

        assert [g.status for g in _others(goals.get_goals().goals)] == ["proposed", "active", "inactive"]
        assert [g.status for g in _others(goals.get_goals(["completed", "deleted"]).goals)] == ["completed", "deleted"]
        # Only the active one holds a label.
        assert [name for name, _color in calendar.named().values()] == ["Active"]

    def test_refuses_an_unknown_status(self):
        goals, _, _ = _goals()

        with pytest.raises(ValueError, match="Unknown goal status"):
            goals.get_goals(["done"])
        with pytest.raises(ValueError, match="status must be one of proposed, active"):
            goals.create_goal(Goal(name="Cooking", status="done"))

    def test_a_deleted_goal_cant_be_given_to_an_event_but_one_can_keep_it(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Oops", status="deleted"))
        tree = goals.tree()
        oops = _by_name(goals)["Oops"]

        tree.check_goal_ids([oops.id])  # it still exists
        with pytest.raises(ValueError, match=r"Deleted goals can't be given to an event: \w+ \(Oops\)"):
            tree.check_goal_ids([oops.id], for_events=True)
        tree.check_goal_ids([oops.id], for_events=True, already=[oops.id])


class TestGoalTree:
    def _tree(self):
        return GoalTree([
            Goal(id="a", name="A", status="active", label_id="la", priority=1),
            Goal(id="b", name="B", status="inactive", label_id="lb", parent_id="a"),
            Goal(id="c", name="C", status="inactive", label_id="lc", parent_id="b", priority=3),
        ])

    def test_inherits_priority_from_the_nearest_ancestor_that_sets_one(self):
        tree = self._tree()

        assert tree.priority("c") == 3
        assert tree.priority("b") == 1

    def test_active_label_is_the_nearest_active_goals(self):
        tree = self._tree()

        assert tree.active_label_id("c") == "la"
        assert GoalTree([Goal(id="x", status="inactive", label_id="lx")]).active_label_id("x") is None

    def test_survives_a_cycle_from_a_hand_edit(self):
        tree = GoalTree([
            Goal(id="a", name="A", parent_id="b", status="active", label_id="la"),
            Goal(id="b", name="B", parent_id="a", status="active", label_id="lb"),
        ])

        assert [g.id for g in tree.chain("a")] == ["a", "b"]
        assert {g.id for g in tree.ordered()} == {OVERALL_ID, "a", "b"}


class TestReorderGoals:
    def _names(self, goals):
        return [goal.name for goal in _others(goals.get_goals().goals)]

    def test_orders_siblings_among_the_places_they_hold(self):
        goals, calendar, _ = _goals()
        for name in ["Cooking", "Hosting", "Reading"]:
            goals.create_goal(Goal(name=name))
        goals.create_goal(Goal(name="Tofu", parent_id=_by_name(goals)["Cooking"].id))
        goals.create_goal(Goal(name="Curry", parent_id=_by_name(goals)["Cooking"].id))
        by_name = _by_name(goals)
        writes = calendar.writes

        goals.reorder_goals([by_name["Reading"].id, by_name["Cooking"].id, by_name["Hosting"].id])
        goals.reorder_goals([by_name["Curry"].id, by_name["Tofu"].id])

        assert self._names(goals) == ["Reading", "Cooking", "Curry", "Tofu", "Hosting"]
        assert calendar.writes == writes  # No labels touched.

    @pytest.mark.parametrize("which, message", [([], "Say which"), (["Cooking", "Tofu"], "Only sibling goals")])
    def test_refuses_what_it_cant_order(self, which, message):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))
        goals.create_goal(Goal(name="Tofu", parent_id=_by_name(goals)["Cooking"].id))
        by_name = _by_name(goals)

        with pytest.raises(ValueError, match=message):
            goals.reorder_goals([by_name[name].id for name in which])


class TestRated:
    def test_a_goal_is_rated_with_a_measure_or_rated_sub_goals(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Neighbor"))
        neighbor = _by_name(goals)["Neighbor"]
        goals.create_goal(Goal(name="Parents", parent_id=neighbor.id, measure={"kind": "count", "target": 1}))
        goals.create_goal(Goal(name="Folder"))
        goals.create_goal(Goal(name="Paused", status="inactive", measure={"kind": "count", "target": 1}))
        tree = goals.tree()
        by_name = {g.name: g.id for g in tree.goals}

        # Overall too: rated by its sub-goals, the top-level goals.
        assert {name for name, goal_id in by_name.items() if tree.rated(goal_id)} == {"Overall", "Neighbor", "Parents"}
        assert tree.measure(by_name["Neighbor"]) == {"kind": "rollup", "agg": "mean"}
        assert tree.measure(by_name["Parents"]) == {"kind": "count", "target": 1}
        assert tree.measure(by_name["Folder"]) is None
        assert [g.name for g in tree.rated_children(by_name["Neighbor"])] == ["Parents"]


class _EventCalendar(FakeLabelCalendar):
    def __init__(self, labels=()):
        super().__init__(labels)
        self.events: list[Event] = []
        self.listed: list[tuple[datetime, datetime]] = []

    def list_events(self, time_min, time_max):
        self.listed.append((time_min, time_max))
        return [e for e in self.events if e.end > time_min and e.start < time_max]


class TestRecentTime:
    def _setup(self, last_compaction):
        calendar = _EventCalendar([_UNNAMED])
        goals = Goals(calendar, FakeSheets(), today=lambda: _TODAY, last_compaction=lambda: last_compaction)
        return goals, calendar

    def test_counts_each_goals_minutes_with_its_sub_goals_up_to_the_last_compaction(self):
        as_of = datetime(2026, 10, 2, 21, tzinfo=timezone.utc)
        goals, calendar = self._setup(as_of)
        goals.create_goal(Goal(name="Neighbor"))
        neighbor = _by_name(goals)["Neighbor"]
        goals.create_goal(Goal(name="Cousins", parent_id=neighbor.id))
        goals.create_goal(Goal(name="Other"))
        cousins = _by_name(goals)["Cousins"]

        def event(hours_before: float, minutes: int, goal_ids, **fields):
            start = as_of - timedelta(hours=hours_before)
            return Event(id=str(hours_before), start=start, end=start + timedelta(minutes=minutes), goal_ids=goal_ids, **fields)

        calendar.events = [
            event(2, 60, [cousins.id]),  # 60 in both windows, for Cousins and Neighbor
            event(25, 120, [neighbor.id]),  # 60 of it in the last 24 hours
            event(24 * 5, 30, [cousins.id]),  # 7 days only
            event(24 * 8, 30, [cousins.id]),  # too long ago
            event(1, 30, [cousins.id], status="cancelled"),
            event(-1, 30, [cousins.id]),  # after the last compaction: not yet fact
        ]

        listing = goals.get_goals()

        listed = {g.name: g for g in listing.goals}
        assert listing.as_of == as_of
        assert (listed["Cousins"].minutes_24h, listed["Cousins"].minutes_7d) == (60, 90)
        assert (listed["Neighbor"].minutes_24h, listed["Neighbor"].minutes_7d) == (120, 210)
        assert (listed["Other"].minutes_24h, listed["Other"].minutes_7d) == (0, 0)
        assert calendar.listed[-1] == (as_of - timedelta(days=7), as_of)

    def test_without_a_compaction_theres_nothing_to_count_up_to(self):
        goals, calendar = self._setup(None)
        goals.create_goal(Goal(name="Cooking"))

        listing = goals.get_goals()

        assert listing.as_of is None
        assert listing.goals[0].minutes_24h is None and listing.goals[0].minutes_7d is None
        assert calendar.listed == []

    def test_splits_the_time_spent_on_goals_by_their_statuses(self):
        as_of = datetime(2026, 10, 2, 21, tzinfo=timezone.utc)
        goals, calendar = self._setup(as_of)
        goals.create_goal(Goal(name="Work"))
        goals.create_goal(Goal(name="Paused", status="inactive"))
        work = _by_name(goals)["Work"]
        goals.create_goal(Goal(name="Side", parent_id=work.id, status="inactive"))
        by_name = _by_name(goals)

        def event(hours_before: float, minutes: int, goal_ids):
            start = as_of - timedelta(hours=hours_before)
            return Event(id=str(hours_before), start=start, end=start + timedelta(minutes=minutes), goal_ids=goal_ids)

        calendar.events = [
            event(2, 60, [work.id]),  # active
            event(4, 30, [by_name["Paused"].id]),  # inactive
            event(6, 20, [by_name["Side"].id]),  # inactive: its parent being active doesn't matter
            event(30, 45, [work.id, by_name["Paused"].id]),  # both, once; 7 days only
            event(8, 15, []),  # no goal: not counted
        ]

        listing = goals.get_goals()

        def split(statuses):
            return [(m.statuses, m.minutes_24h, m.minutes_7d) for m in statuses]

        assert split(listing.minutes_by_statuses) == [
            (["active"], 60, 60),
            (["active", "inactive"], 0, 45),
            (["inactive"], 50, 50),
        ]
        listed = {g.name: g for g in listing.goals}
        # Work's own time: its inactive sub-goal's counts as inactive, and
        # the event it shares with Paused counts only as its own.
        assert split(listed["Work"].minutes_by_statuses) == [(["active"], 60, 105), (["inactive"], 20, 20)]
        assert split(listed["Paused"].minutes_by_statuses) == [(["inactive"], 30, 75)]
        assert listed["Overall"].minutes_by_statuses == listing.minutes_by_statuses
        overall = next(g for g in listing.goals if g.id == OVERALL_ID)
        # Every goal's time, each event once.
        assert (overall.minutes_24h, overall.minutes_7d) == (110, 155)


class TestOverall:
    def test_every_tree_has_it_first_holding_no_label(self):
        goals, calendar, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))

        listing = goals.get_goals()

        overall = listing.goals[0]
        assert (overall.id, overall.name, overall.path, overall.status) == (OVERALL_ID, "Overall", "Overall", "active")
        assert overall.label_id is None
        assert [name for name, _color in calendar.named().values()] == ["Cooking"]
        assert listing.label_slots_used == 2  # Cooking's, and Calendar's unnamed one
        # Listed whatever statuses are asked for.
        assert [g.id for g in goals.get_goals(["deleted"]).goals] == [OVERALL_ID]

    def test_is_rated_from_the_top_level_goals_or_its_own_measure(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Cooking", measure={"kind": "subjective", "prompt": "?"}))
        goals.create_goal(Goal(name="Idle"))
        cooking = _by_name(goals)["Cooking"]
        goals.create_goal(Goal(name="Tofu", parent_id=cooking.id))
        tree = goals.tree()

        assert [g.name for g in tree.children(OVERALL_ID)] == ["Cooking", "Idle"]
        assert [g.name for g in tree.rated_children(OVERALL_ID)] == ["Cooking"]
        assert tree.measure(OVERALL_ID) == {"kind": "rollup", "agg": "mean"}
        assert tree.under(_by_name(goals)["Tofu"].id, OVERALL_ID)

        goals.update_goal(
            Goal(id=OVERALL_ID, measure={"kind": "rollup", "agg": "weighted", "weights": {cooking.id: 2}})
        )

        saved = goals.tree().by_id[OVERALL_ID]
        assert saved.measure["weights"] == {cooking.id: 2}
        with pytest.raises(ValueError, match="isn't one of its sub-goals"):
            goals.update_goal(
                Goal(id=OVERALL_ID, measure={"kind": "rollup", "agg": "weighted", "weights": {"nope": 2}})
            )

    @pytest.mark.parametrize(
        "change, message",
        [
            (lambda ids: Goal(id=OVERALL_ID, status="inactive"), "always active"),
            (lambda ids: Goal(id=OVERALL_ID, parent_id=ids["Cooking"]), "has no parent"),
            (lambda ids: Goal(id=ids["Cooking"], parent_id=OVERALL_ID), "can't name the overall goal as its parent"),
        ],
    )
    def test_stays_active_and_above_the_rest(self, change, message):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))
        ids = {g.name: g.id for g in goals.tree().goals}

        with pytest.raises(ValueError, match=message):
            goals.update_goal(change(ids))

    def test_cant_be_reordered_or_given_to_an_event(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))

        with pytest.raises(ValueError, match="can't be reordered"):
            goals.reorder_goals([OVERALL_ID, _by_name(goals)["Cooking"].id])
        with pytest.raises(ValueError, match="can't be given to an event"):
            goals.tree().check_goal_ids([OVERALL_ID], for_events=True)
