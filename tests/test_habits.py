import pytest

from tests.fake_sheets import FakeSheets
from utilities import calendar_metadata_sheet
from utilities.habits import Habit, Habits, action_scopes, subject_id

# Every action's and group's id, with its path, as action_scopes gives them.
SCOPES = {"guitar": "Creative › Guitar", "practice": "Creative › Guitar › Practice guitar", "walk": "Walk"}


def _habits(scopes=None, trait_ids=None) -> tuple[Habits, FakeSheets]:
    sheets = FakeSheets()
    return (
        Habits.ensure(
            sheets,
            "spreadsheet",
            scopes=lambda: SCOPES if scopes is None else scopes,
            trait_ids=(lambda: trait_ids) if trait_ids is not None else None,
        ),
        sheets,
    )


def _create(habits: Habits, **fields) -> str:
    return habits.create_habit(Habit(**fields)).created_id


def test_creates_and_finds_a_habit_by_id_or_name_with_its_actions_path():
    habits, sheets = _habits()

    created = habits.create_habit(Habit(id="mine", name="Guitar", action_id="guitar", note="Daily, mostly scales"))

    assert created.created_id != "mine"
    assert created.habit.status == "active"
    assert created.habit.action_path == "Creative › Guitar"
    assert habits.get_habit(created.created_id).note == "Daily, mostly scales"
    assert habits.get_habit("GUITAR").id == created.created_id
    assert sheets.tags[("sheet-role", calendar_metadata_sheet.HABITS_SHEET_ROLE)] is not None


def test_lists_active_habits_by_default():
    habits, _ = _habits()
    _create(habits, name="Guitar", action_id="guitar")
    _create(habits, name="Walk", action_id="walk", status="archived")

    assert [h.name for h in habits.get_habits()] == ["Guitar"]
    assert [h.name for h in habits.get_habits(["active", "archived"])] == ["Guitar", "Walk"]
    with pytest.raises(ValueError, match="Unknown habit status"):
        habits.get_habits(["gone"])


def test_needs_a_unique_name_and_an_action_or_group():
    habits, _ = _habits()
    _create(habits, name="Guitar", action_id="guitar")

    with pytest.raises(ValueError, match=r"already a habit named 'Guitar' \(.*\); habit names must be unique"):
        _create(habits, name="guitar", action_id="practice")
    with pytest.raises(ValueError, match="needs a name"):
        _create(habits, action_id="walk")
    with pytest.raises(ValueError, match="needs an action_id"):
        _create(habits, name="Walk")
    with pytest.raises(ValueError, match="'nope' isn't an action or an action group"):
        _create(habits, name="Walk", action_id="nope")


def test_traits_are_checked_as_a_persons():
    habits, _ = _habits(trait_ids=["patient", "present"])

    created = _create(
        habits,
        name="Guitar",
        action_id="guitar",
        traits={"select": ["patient"], "parts": {"patient": [{"kind": "count", "target": 5, "noun": "sessions"}]}},
    )

    assert habits.get_habit(created).traits["select"] == ["patient"]
    with pytest.raises(ValueError, match="names 'kind', which isn't a trait"):
        _create(habits, name="Walk", action_id="walk", traits={"select": ["kind"]})
    with pytest.raises(ValueError, match='has no field "people"'):
        _create(habits, name="Walk", action_id="walk", traits={"people": []})


def test_updates_and_clears():
    habits, _ = _habits()
    habit = _create(habits, name="Guitar", action_id="guitar", note="Daily", traits={"select": "all"})

    updated = habits.update_habit(Habit(id=habit, action_id="practice", status="archived"), ["note", "traits"])

    assert (updated.action_id, updated.action_path, updated.status) == (
        "practice", "Creative › Guitar › Practice guitar", "archived",
    )
    assert (updated.note, updated.traits) == (None, None)
    with pytest.raises(ValueError, match="needs the habit's id"):
        habits.update_habit(Habit(name="x"))
    with pytest.raises(ValueError, match="There's no habit with the id or name 'nope'"):
        habits.update_habit(Habit(id="nope", name="x"))


def test_another_habits_action_gone_doesnt_stop_writing_this_one():
    scopes = dict(SCOPES)
    habits, _ = _habits(scopes=scopes)
    _create(habits, name="Walk", action_id="walk")
    guitar = _create(habits, name="Guitar", action_id="guitar")
    del scopes["walk"]  # Its group deleted, say.

    habits.update_habit(Habit(id=guitar, note="Scales"))

    assert habits.get_habit("Walk").action_path is None
    with pytest.raises(ValueError, match="'walk' isn't an action or an action group"):
        habits.update_habit(Habit(id=habits.get_habit("Walk").id, note="Outside"))


def test_suggests_close_matches():
    habits, _ = _habits()
    _create(habits, name="Guitar", action_id="guitar")

    with pytest.raises(ValueError, match=r"did you mean .*\(Guitar\)"):
        habits.get_habit("Guitr")


def test_scopes_are_actions_and_groups_with_their_paths_but_not_deleted_actions():
    from utilities.action_groups import ActionGroup, GroupTree
    from utilities.actions import Action

    groups = GroupTree([ActionGroup(id="creative", name="Creative"), ActionGroup(id="g", name="Guitar", group_id="creative")])
    actions = [
        Action(id="practice", name="Practice guitar", group_id="g", status="active"),
        Action(id="old", name="Old", status="deleted"),
    ]

    assert action_scopes(actions, groups) == {
        "creative": "Creative",
        "g": "Creative › Guitar",
        "practice": "Creative › Guitar › Practice guitar",
    }


def test_a_habit_goes_by_a_prefixed_id_where_a_persons_would():
    assert subject_id("a7k2qp") == "habit:a7k2qp"
