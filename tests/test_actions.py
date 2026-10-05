import pytest

from calendar_clients.google_calendar import EventLabel as RawEventLabel, EventLabelConflictError, color_for_priority
from tests.fake_labels import FakeLabelCalendar
from tests.fake_sheets import FakeSheets
from utilities import actions as actions_module, calendar_metadata_sheet
from utilities.action_groups import ActionGroup
from utilities.actions import Action, Actions

_UNNAMED = RawEventLabel(id="default-1", background_color="#039be5")
"""One of Calendar's own unnamed labels, which actions always leave alone."""


def _actions(calendar=None, sheets=None) -> tuple[Actions, FakeLabelCalendar, FakeSheets]:
    calendar = calendar if calendar is not None else FakeLabelCalendar([_UNNAMED])
    sheets = sheets if sheets is not None else FakeSheets()
    return Actions.ensure(calendar, sheets, "spreadsheet"), calendar, sheets


def _create(actions: Actions, **fields) -> str:
    return actions.create_action(Action(**fields)).created_id


class TestEnsure:
    def test_adds_an_empty_actions_tab_once(self):
        actions, _, sheets = _actions()
        _create(actions, name="Walk")

        again, _, _ = _actions(sheets=sheets)

        assert [a.name for a in again.all()] == ["Walk"]
        assert sheets.tags[("sheet-role", calendar_metadata_sheet.ACTIONS_SHEET_ROLE)] is not None


class TestCreateAction:
    def test_assigns_an_id_and_label_id_and_is_proposed(self):
        actions, _, _ = _actions()

        created = actions.create_action(Action(name="Play guitar", priority=1, note="for fun"))

        (action,) = actions.all()
        assert created.created_id == action.id and len(action.id) == 6
        assert action.label_id and action.status == "proposed"
        assert created.changed[0].name == "Play guitar"
        assert created.changed[0].effective_color == color_for_priority(1)[1]
        assert created.changed[0].holds_label

    def test_ignores_an_id_or_label_id_given(self):
        actions, _, _ = _actions()

        created = actions.create_action(Action(id="mine", label_id="theirs", name="Walk"))

        assert created.created_id != "mine"
        assert actions.all()[0].label_id != "theirs"

    def test_adds_its_label_to_the_calendar_leaving_others_alone(self):
        other = RawEventLabel(id="other", name="Someone else's", background_color="#111111")
        actions, calendar, _ = _actions(FakeLabelCalendar([_UNNAMED, other]))

        _create(actions, name="Walk", status="active", background_color="#222222")

        label_id = actions.all()[0].label_id
        assert calendar.named() == {"other": ("Someone else's", "#111111"), label_id: ("Walk", "#222222")}
        assert _UNNAMED in calendar.labels

    @pytest.mark.parametrize("name", ["walk", " WALK "])
    def test_refuses_a_name_already_used_whatever_its_case_or_status(self, name):
        actions, _, _ = _actions()
        walk = _create(actions, name="Walk")
        actions.update_action(Action(id=walk, status="deleted"))

        with pytest.raises(ValueError, match=r"already an action named 'Walk' \(.*, deleted\)"):
            _create(actions, name=name)
        assert len(actions.all()) == 1

    @pytest.mark.parametrize(
        "action, message",
        [
            (Action(), "needs a name"),
            (Action(name="x" * 51), "longer than 50 characters"),
            (Action(name="Walk", status="inactive"), "status must be one of proposed, active, archived, deleted"),
        ],
    )
    def test_refuses_what_isnt_well_formed(self, action, message):
        actions, calendar, _ = _actions()

        with pytest.raises(ValueError, match=message):
            actions.create_action(action)
        assert actions.all() == [] and calendar.writes == 0


class TestLabels:
    def test_archiving_frees_the_label_and_reactivating_restores_the_same_one(self):
        actions, calendar, _ = _actions()
        walk = _create(actions, name="Walk", status="active")
        label_id = actions.all()[0].label_id

        actions.update_action(Action(id=walk, status="archived"))
        assert calendar.named() == {}

        actions.update_action(Action(id=walk, status="active"))
        assert list(calendar.named()) == [label_id]

    def test_active_actions_beyond_the_calendars_room_are_refused(self, monkeypatch):
        monkeypatch.setattr(actions_module, "MAX_LABELS", 2)
        actions, _, _ = _actions()  # The unnamed label takes one.
        _create(actions, name="Walk", status="active")

        with pytest.raises(ValueError, match="at most 1 actions can be active"):
            _create(actions, name="Run", status="active")

    def test_proposed_actions_hold_labels_only_while_theres_room(self, monkeypatch):
        monkeypatch.setattr(actions_module, "MAX_LABELS", 3)
        actions, calendar, _ = _actions()  # The unnamed label takes one.
        walk = _create(actions, name="Walk")
        run = _create(actions, name="Run")
        swim = _create(actions, name="Swim")

        listed = {a.name: a.holds_label for a in actions.get_actions().actions}
        assert listed == {"Walk": True, "Run": True, "Swim": False}

        # Activating Swim takes Run's label: active ones come first.
        changes = actions.update_action(Action(id=swim, status="active"))
        assert {(a.id, a.holds_label) for a in changes.changed} == {(swim, True), (run, False)}
        assert {name for name, _ in calendar.named().values()} == {"Walk", "Swim"}
        assert changes.label_slots_used == 3

        # Archiving Walk gives Run its label back.
        changes = actions.update_action(Action(id=walk, status="archived"))
        assert {(a.id, a.holds_label) for a in changes.changed} == {(walk, False), (run, True)}

    def test_renaming_and_recoloring_update_the_label(self):
        actions, calendar, _ = _actions()
        walk = _create(actions, name="Walk", status="active")

        actions.update_action(Action(id=walk, name="Stroll", priority=3))

        assert list(calendar.named().values()) == [("Stroll", color_for_priority(3)[1])]

    def test_an_unchanged_label_isnt_rewritten(self):
        actions, calendar, _ = _actions()
        walk = _create(actions, name="Walk", status="active")
        writes = calendar.writes

        actions.update_action(Action(id=walk, note="outside"))

        assert calendar.writes == writes

    def test_a_concurrent_label_change_fails_loudly(self):
        actions, calendar, _ = _actions()
        list_labels = calendar.list_event_labels

        def stale():
            labels = list_labels()
            calendar.version += 1
            return labels

        calendar.list_event_labels = stale
        with pytest.raises(EventLabelConflictError):
            _create(actions, name="Walk", status="active")


class TestUpdateAction:
    def test_sets_given_fields_and_keeps_the_rest(self):
        actions, _, _ = _actions()
        outdoors = actions.create_action_group(ActionGroup(name="Outdoors")).created_id
        walk = _create(actions, name="Walk", priority=1, note="outside", group_id=outdoors)

        changes = actions.update_action(Action(id=walk, status="active", note="in the park"))

        (action,) = actions.all()
        assert (action.name, action.status, action.priority, action.note, action.group_id) == (
            "Walk", "active", 1, "in the park", outdoors
        )
        assert [a.id for a in changes.changed] == [walk]

    def test_clears_fields(self):
        actions, _, _ = _actions()
        walk = _create(actions, name="Walk", priority=1, note="outside", background_color="#123456")

        actions.update_action(Action(id=walk), clear_fields=["priority", "note", "background_color"])

        (action,) = actions.all()
        assert (action.priority, action.note, action.background_color) == (None, None, None)

    def test_never_changes_the_label_id(self):
        actions, _, _ = _actions()
        walk = _create(actions, name="Walk")
        label_id = actions.all()[0].label_id

        actions.update_action(Action(id=walk, label_id="other"))

        assert actions.all()[0].label_id == label_id

    def test_may_keep_its_own_name_in_another_case(self):
        actions, _, _ = _actions()
        walk = _create(actions, name="Walk")

        actions.update_action(Action(id=walk, name="walk"))

        assert actions.all()[0].name == "walk"

    def test_refuses_another_actions_name(self):
        actions, _, _ = _actions()
        _create(actions, name="Walk")
        run = _create(actions, name="Run")

        with pytest.raises(ValueError, match="already an action named 'Walk'"):
            actions.update_action(Action(id=run, name="walk"))

    @pytest.mark.parametrize(
        "action, clear, message",
        [
            (Action(), [], "needs the action's id"),
            (Action(id="nope"), [], "no action with the id or name 'nope'"),
            (Action(id="x"), ["name"], r"Can't clear \['name'\]"),
            (Action(id="x", note="n"), ["note"], r"Can't both set and clear \['note'\]"),
        ],
    )
    def test_refuses_what_it_cant_do(self, action, clear, message):
        actions, _, _ = _actions()

        with pytest.raises(ValueError, match=message):
            actions.update_action(action, clear)


class TestGetActions:
    def test_lists_proposed_and_active_ones_by_default(self):
        actions, _, _ = _actions()
        _create(actions, name="Walk")
        _create(actions, name="Run", status="active")
        _create(actions, name="Swim", status="archived")
        _create(actions, name="Fly", status="deleted")

        assert [a.name for a in actions.get_actions().actions] == ["Walk", "Run"]
        assert [a.name for a in actions.get_actions(["archived", "deleted"]).actions] == ["Swim", "Fly"]

    def test_counts_label_slots(self):
        actions, _, _ = _actions()
        _create(actions, name="Walk", status="active")
        _create(actions, name="Swim", status="archived")

        listing = actions.get_actions()

        assert (listing.label_slots_used, listing.label_slots_total) == (2, 200)

    def test_refuses_an_unknown_status(self):
        actions, _, _ = _actions()

        with pytest.raises(ValueError, match="Unknown action status"):
            actions.get_actions(["inactive"])


class TestGetAction:
    def test_finds_one_by_id_or_by_name_ignoring_case(self):
        actions, _, _ = _actions()
        walk = _create(actions, name="Walk")

        assert actions.get_action(walk).name == "Walk"
        assert actions.get_action("WALK").id == walk

    def test_suggests_close_matches(self):
        actions, _, _ = _actions()
        walk = _create(actions, name="Play guitar")

        with pytest.raises(ValueError, match=rf"did you mean {walk} \(Play guitar\)\?"):
            actions.get_action("play guitr")


def _group(actions: Actions, **fields) -> str:
    return actions.create_action_group(ActionGroup(**fields)).created_id


class TestActionGroups:
    def test_creates_groups_inside_groups(self):
        actions, _, sheets = _actions()
        creative = _group(actions, name="Creative", priority=1)
        guitar = _group(actions, name="Guitar", group_id=creative)

        listed = {g.name: g for g in actions.get_action_groups()}

        assert listed["Guitar"].path == "Creative › Guitar"
        assert listed["Guitar"].effective_priority == 1
        assert actions.get_action_group("guitar").id == guitar
        assert sheets.tags[("sheet-role", calendar_metadata_sheet.ACTION_GROUPS_SHEET_ROLE)] is not None

    def test_lists_enclosing_groups_first(self):
        actions, _, _ = _actions()
        top = _group(actions, name="Top")
        inner = _group(actions, name="Inner")
        actions.update_action_group(ActionGroup(id=inner, group_id=top))
        _group(actions, name="Other")

        assert [g.name for g in actions.get_action_groups()] == ["Top", "Inner", "Other"]

    def test_actions_inherit_their_groups_color_and_priority(self):
        actions, calendar, _ = _actions()
        creative = _group(actions, name="Creative", background_color="#123456", priority=3)
        guitar = _group(actions, name="Guitar", group_id=creative)
        play = _create(actions, name="Play guitar", group_id=guitar, status="active")

        action = actions.get_action(play)

        assert (action.path, action.effective_color, action.effective_priority) == (
            "Creative › Guitar › Play guitar", "#123456", 3
        )
        assert list(calendar.named().values()) == [("Play guitar", "#123456")]

    def test_recoloring_a_group_recolors_its_actions_labels(self):
        actions, calendar, _ = _actions()
        creative = _group(actions, name="Creative", background_color="#123456")
        play = _create(actions, name="Play guitar", group_id=creative, status="active")
        _create(actions, name="Walk", status="active", background_color="#000000")

        changes = actions.update_action_group(ActionGroup(id=creative, background_color="#654321"))

        assert [a.id for a in changes.affected_actions] == [play]
        assert changes.affected_actions[0].effective_color == "#654321"
        assert ("Play guitar", "#654321") in calendar.named().values()

    def test_a_group_may_share_an_actions_name_but_not_another_groups(self):
        actions, _, _ = _actions()
        _create(actions, name="Cooking")
        _group(actions, name="Cooking")

        with pytest.raises(ValueError, match=r"already an action group named 'Cooking' \(.*\); group names must"):
            _group(actions, name="cooking")

    @pytest.mark.parametrize(
        "group, message",
        [
            (ActionGroup(), "needs a name"),
            (ActionGroup(name="X", group_id="nope"), "is inside 'nope', which isn't an action group"),
        ],
    )
    def test_refuses_what_isnt_well_formed(self, group, message):
        actions, _, _ = _actions()

        with pytest.raises(ValueError, match=message):
            actions.create_action_group(group)

    def test_refuses_a_group_inside_itself(self):
        actions, _, _ = _actions()
        top = _group(actions, name="Top")
        inner = _group(actions, name="Inner", group_id=top)

        with pytest.raises(ValueError, match="can't be inside itself"):
            actions.update_action_group(ActionGroup(id=top, group_id=inner))

    def test_refuses_an_action_in_a_group_that_doesnt_exist(self):
        actions, _, _ = _actions()

        with pytest.raises(ValueError, match="group_id 'nope' isn't an action group"):
            _create(actions, name="Walk", group_id="nope")

    def test_clears_fields(self):
        actions, _, _ = _actions()
        top = _group(actions, name="Top")
        inner = _group(actions, name="Inner", group_id=top, note="n")

        actions.update_action_group(ActionGroup(id=inner), clear_fields=["group_id", "note"])

        assert actions.get_action_group(inner).group_id is None
        assert actions.get_action_group(inner).note is None

    def test_an_unknown_group_suggests_close_matches(self):
        actions, _, _ = _actions()
        guitar = _group(actions, name="Guitar")

        with pytest.raises(ValueError, match=rf"did you mean {guitar} \(Guitar\)"):
            actions.get_action_group("guitr")
        with pytest.raises(ValueError, match="no action group"):
            actions.update_action_group(ActionGroup(id="nope", note="x"))


class TestDeleteActionGroup:
    def test_moves_its_actions_and_groups_up_to_its_parent(self):
        actions, calendar, _ = _actions()
        creative = _group(actions, name="Creative", background_color="#111111")
        guitar = _group(actions, name="Guitar", group_id=creative, background_color="#222222")
        lessons = _group(actions, name="Lessons", group_id=guitar)
        play = _create(actions, name="Play guitar", group_id=guitar, status="active")
        sing = _create(actions, name="Sing", group_id=creative)

        deleted = actions.delete_action_group(guitar)

        assert deleted.deleted.name == "Guitar"
        assert [g.id for g in deleted.changed] == [lessons]
        assert deleted.changed[0].path == "Creative › Lessons"
        assert [a.id for a in deleted.affected_actions] == [play]
        assert actions.get_action(play).group_id == creative
        assert actions.get_action(sing).group_id == creative
        assert {g.name for g in actions.get_action_groups()} == {"Creative", "Lessons"}
        assert ("Play guitar", "#111111") in calendar.named().values()

    def test_a_top_level_groups_contents_move_to_the_top(self):
        actions, _, _ = _actions()
        top = _group(actions, name="Top")
        play = _create(actions, name="Play", group_id=top)

        actions.delete_action_group(top)

        assert actions.get_action(play).group_id is None
        assert actions.get_action_groups() == []

    def test_needs_the_groups_id(self):
        actions, _, _ = _actions()
        top = _group(actions, name="Top")

        with pytest.raises(ValueError, match=f"by its id: 'Top' is {top}"):
            actions.delete_action_group("top")
        with pytest.raises(ValueError, match="no action group"):
            actions.delete_action_group("nothing")


class TestActionTree:
    def test_priorities_names_and_labels(self):
        actions, _, _ = _actions()
        food = _group(actions, name="Food", priority=1)
        cook = _create(actions, name="Cook", group_id=food, status="active")

        tree = actions.tree()

        assert tree.priority(cook) == 1
        assert tree.priority("nope") is None
        assert (tree.name(cook), tree.name("nope")) == ("Cook", "(unknown action nope)")
        assert tree.action_for_label(tree.by_id[cook].label_id).id == cook

    def test_checks_ids_for_events(self):
        actions, _, _ = _actions()
        cook = _create(actions, name="Cook")
        gone = _create(actions, name="Gone", status="deleted")
        tree = actions.tree()

        tree.check_action_ids([cook])
        tree.check_action_ids([gone], already=[gone])
        with pytest.raises(ValueError, match="Deleted actions can't be given to an event"):
            tree.check_action_ids([gone])
        with pytest.raises(ValueError, match=rf"no action with the id or name 'Cok'; did you mean {cook} \(Cook\)"):
            tree.check_action_ids(["Cok"])
