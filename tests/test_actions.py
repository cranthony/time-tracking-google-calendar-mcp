import pytest

from calendar_clients.google_calendar import EventLabel as RawEventLabel, EventLabelConflictError, color_for_priority
from tests.fake_labels import FakeLabelCalendar
from tests.fake_sheets import FakeSheets
from utilities import actions as actions_module, calendar_metadata_sheet
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
        walk = _create(actions, name="Walk", priority=1, note="outside", group_id="g1")

        changes = actions.update_action(Action(id=walk, status="active", note="in the park"))

        (action,) = actions.all()
        assert (action.name, action.status, action.priority, action.note, action.group_id) == (
            "Walk", "active", 1, "in the park", "g1"
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
