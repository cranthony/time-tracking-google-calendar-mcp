from datetime import date
from dataclasses import replace

import pytest

from calendar_clients.google_calendar import EventLabel as RawEventLabel, EventLabelConflictError, color_for_priority
from tests.fake_sheets import FakeSheets
from utilities import calendar_metadata_sheet, goals as goals_module
from utilities.event_label_sheet import EventLabel as LegacyLabel
from utilities.goal_sheet import Goal, GoalSheet
from utilities.goals import Goals, GoalTree

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


def _legacy_tab(sheets: FakeSheets, rows: list[list[str]]) -> int:
    """An already-tagged event labels tab holding `rows`."""
    sheet_id = 7
    sheets.create_sheet_metadata("spreadsheet-1", sheet_id, "sheet-role", "event-labels")
    sheets.titles[sheet_id] = "Event Labels"
    header = ["id", "name", "background_color", "priority", "fixed_time", "note"]
    sheets.write_rows_in_sheet("spreadsheet-1", sheet_id, "A1:F1", [header])
    sheets.write_rows_in_sheet("spreadsheet-1", sheet_id, "A2:F", rows)
    return sheet_id


def _goals(calendar=None, sheets=None) -> tuple[Goals, FakeLabelCalendar, FakeSheets]:
    calendar = calendar if calendar is not None else FakeLabelCalendar([_UNNAMED])
    sheets = sheets if sheets is not None else FakeSheets()
    return Goals(calendar, sheets, today=lambda: _TODAY), calendar, sheets


def _by_name(goals: Goals) -> dict[str, Goal]:
    return {goal.name: goal for goal in goals.tree().goals}


class TestMigration:
    def test_migrates_the_event_labels_tab_into_active_top_level_goals(self):
        sheets = FakeSheets()
        legacy = _legacy_tab(
            sheets,
            [
                ["l1", "Cooking", "#111111", "2", "TRUE", "dinners"],
                ["default-1", "", "#039be5"],  # an unnamed label: not a goal
                ["l3", "cooking", "#333333"],  # same name, different case
            ],
        )
        calendar = FakeLabelCalendar(
            [RawEventLabel(id="l1", name="Cooking", background_color="#111111"), _UNNAMED]
        )

        goals, _, _ = _goals(calendar, sheets)

        migrated = goals.tree().goals
        assert [(g.name, g.label_id, g.active, g.parent_id) for g in migrated] == [
            ("Cooking", "l1", True, None),
            ("cooking (2)", "l3", True, None),
        ]
        cooking = migrated[0]
        assert (cooking.background_color, cooking.priority, cooking.fixed_time, cooking.note) == (
            "#111111", 2, True, "dinners"
        )
        assert cooking.created == _TODAY
        assert len(cooking.id) == 6
        assert sheets.titles[legacy] == "Event Labels (migrated)"
        assert calendar.writes == 0  # migrating changes no labels

    def test_without_an_event_labels_tab_migrates_the_calendars_named_labels(self):
        calendar = FakeLabelCalendar(
            [RawEventLabel(id="l1", name="Reading", background_color="#222222"), _UNNAMED]
        )

        goals, _, _ = _goals(calendar)

        assert [(g.name, g.label_id, g.background_color) for g in goals.tree().goals] == [
            ("Reading", "l1", "#222222")
        ]

    def test_a_new_spreadsheet_uses_its_first_tab(self):
        calendar = FakeLabelCalendar([], spreadsheet_id=None)

        goals, _, sheets = _goals(calendar)

        assert goals.spreadsheet_id == "new-spreadsheet"
        assert sheets.find_sheet_id("new-spreadsheet", "sheet-role", "goals") == 0

    def test_migrates_only_once(self):
        sheets = FakeSheets()
        _legacy_tab(sheets, [["l1", "Cooking", "#111111"]])
        first, calendar, _ = _goals(sheets=sheets)

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
        assert len(cooking.id) == 6 and cooking.active is True and cooking.created == _TODAY
        assert cooking.label_id == str(goals_module.uuid.uuid5(goals_module._LABEL_ID_NAMESPACE, cooking.id))
        assert calendar.named() == {cooking.label_id: ("Cooking", _PRIORITY_1_COLOR)}
        assert _UNNAMED in calendar.labels
        assert [g.name for g in result.goals] == ["Cooking"]
        assert result.label_slots_used == 2  # the goal's, plus the unnamed one

    def test_ignores_read_only_fields(self):
        goals, _, _ = _goals()

        goals.create_goal(Goal(id="mine", label_id="mine", created=date(2000, 1, 1), name="Cooking"))

        cooking = _by_name(goals)["Cooking"]
        assert (cooking.id, cooking.label_id, cooking.created) != ("mine", "mine", date(2000, 1, 1))

    def test_a_sub_goal_takes_its_color_from_an_ancestors_priority(self):
        goals, calendar, _ = _goals()
        goals.create_goal(Goal(name="Cooking", priority=1))
        parent = _by_name(goals)["Cooking"]

        goals.create_goal(Goal(name="Tofu tikka", parent_id=parent.id))

        tofu = _by_name(goals)["Tofu tikka"]
        assert calendar.named()[tofu.label_id] == ("Tofu tikka", _PRIORITY_1_COLOR)

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
            (Goal(name="Cooking", cadence="hourly"), "cadence must be one of"),
            (Goal(name="Cooking", measure={"target": 3}), 'must be an object with a "kind"'),
        ],
    )
    def test_refuses_invalid_goals_without_writing_anything(self, goal, message):
        goals, calendar, _ = _goals()

        with pytest.raises(ValueError, match=message):
            goals.create_goal(goal)

        assert goals.tree().goals == []
        assert calendar.writes == 0

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

        assert len(goals.tree().goals) == 4

    def test_refuses_past_the_label_budget_before_writing(self, monkeypatch):
        monkeypatch.setattr(goals_module, "MAX_LABELS", 2)
        goals, calendar, _ = _goals()  # one unnamed label already
        goals.create_goal(Goal(name="One"))

        with pytest.raises(ValueError, match="at most 1 goals can be active"):
            goals.create_goal(Goal(name="Two"))

        assert [g.name for g in goals.tree().goals] == ["One"]
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
        assert [(g.name, g.status) for g in result.goals] == [("Cooking", "inactive")]
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
        goals.create_goal(Goal(name="Tofu", parent_id=parent.id, cadence="weekly", note="soon"))
        tofu = _by_name(goals)["Tofu"]

        goals.update_goal(Goal(id=tofu.id), ["parent_id", "cadence", "note"])

        tofu = _by_name(goals)["Tofu"]
        assert (tofu.parent_id, tofu.cadence, tofu.note) == (None, None, None)

    def test_read_only_fields_are_left_alone(self):
        goals, _, _ = _goals()
        goals.create_goal(Goal(name="Cooking"))
        cooking = _by_name(goals)["Cooking"]

        goals.update_goal(Goal(id=cooking.id, label_id="other", created=date(2000, 1, 1), note="hi"))

        updated = _by_name(goals)["Cooking"]
        assert (updated.label_id, updated.created, updated.note) == (cooking.label_id, _TODAY, "hi")

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

        assert [g.path for g in active.goals] == ["Cooking", "Cooking › Tofu", "Hosting"]
        assert [g.path for g in default.goals] == ["Cooking", "Cooking › Tofu", "Hosting", "Old"]
        assert active.label_slots_used == 4  # three active goals + the unnamed label
        assert active.label_slots_total == 200

    def test_lists_proposed_active_and_inactive_goals_unless_asked_for_others(self):
        goals, calendar, _ = _goals()
        for status in ("proposed", "active", "inactive", "completed", "archived", "deleted"):
            goals.create_goal(Goal(name=status.title(), status=status))

        assert [g.status for g in goals.get_goals().goals] == ["proposed", "active", "inactive"]
        assert [g.status for g in goals.get_goals(["completed", "deleted"]).goals] == ["completed", "deleted"]
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
            Goal(id="a", name="A", status="active", label_id="la", priority=1, fixed_time=True),
            Goal(id="b", name="B", status="inactive", label_id="lb", parent_id="a"),
            Goal(id="c", name="C", status="inactive", label_id="lc", parent_id="b", priority=3),
        ])

    def test_inherits_priority_and_fixed_time_from_the_nearest_ancestor_that_sets_them(self):
        tree = self._tree()

        assert (tree.priority("c"), tree.fixed_time("c")) == (3, True)
        assert (tree.priority("b"), tree.fixed_time("b")) == (1, True)

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
        assert {g.id for g in tree.ordered()} == {"a", "b"}
