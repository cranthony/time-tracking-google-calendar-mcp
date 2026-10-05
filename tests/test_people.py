import pytest

from tests.fake_sheets import FakeSheets
from utilities import calendar_metadata_sheet
from utilities.people import SELF_ID, Circle, People, Person


def _people(sheets=None) -> tuple[People, FakeSheets]:
    sheets = sheets or FakeSheets()
    return People.ensure(sheets, "spreadsheet"), sheets


def _person(people: People, **fields) -> str:
    return people.create_person(Person(**fields)).created_id


def _circle(people: People, **fields) -> str:
    return people.create_circle(Circle(**fields)).created_id


class TestSelf:
    def test_is_always_there_first_without_a_row(self):
        people, sheets = _people()

        (me,) = people.get_people()

        assert (me.id, me.name, me.status) == (SELF_ID, "Me", "active")
        assert people.get_person("self").id == SELF_ID
        assert sheets.tags[("sheet-role", calendar_metadata_sheet.PEOPLE_SHEET_ROLE)] is not None
        assert sheets.tags[("sheet-role", calendar_metadata_sheet.CIRCLES_SHEET_ROLE)] is not None

    def test_updating_self_adds_their_row_once(self):
        people, _ = _people()
        _person(people, name="Sam")

        people.update_person(Person(id=SELF_ID, what_matters="music"))
        people.update_person(Person(id=SELF_ID, name="Chris"))

        listed = people.get_people()
        assert [(p.id, p.name, p.what_matters) for p in listed[:1]] == [(SELF_ID, "Chris", "music")]
        assert [p.name for p in listed] == ["Chris", "Sam"]

    def test_is_always_active(self):
        people, _ = _people()

        with pytest.raises(ValueError, match="self is always active"):
            people.update_person(Person(id=SELF_ID, status="archived"))

    def test_another_person_cant_take_selfs_name_without_a_context(self):
        people, _ = _people()

        with pytest.raises(ValueError, match=r"already a person named 'Me' with no context \(self\)"):
            _person(people, name="me")


class TestCreatePerson:
    def test_assigns_an_id_and_is_active(self):
        people, _ = _people()

        created = people.create_person(Person(id="mine", name="Sam", context="met at salsa"))

        assert created.created_id != "mine" and len(created.created_id) == 6
        assert created.person.status == "active"
        assert people.get_person(created.created_id).context == "met at salsa"

    def test_a_name_and_context_together_must_be_unique(self):
        people, _ = _people()
        _person(people, name="Sam", context="met at salsa")
        _person(people, name="Sam", context="work")

        with pytest.raises(ValueError, match=r"already a person named 'Sam' with the context 'work'.*give a context"):
            _person(people, name="sam", context=" Work")

    def test_refuses_unknown_circles(self):
        people, _ = _people()

        with pytest.raises(ValueError, match=r"circles \['nope'\] aren't circles"):
            _person(people, name="Sam", circles=["nope"])

    def test_refuses_a_bad_status(self):
        people, _ = _people()

        with pytest.raises(ValueError, match="status must be one of active, archived, deleted"):
            _person(people, name="Sam", status="gone")


class TestUpdatePerson:
    def test_sets_given_fields_keeps_the_rest_and_clears(self):
        people, _ = _people()
        family = _circle(people, name="Family")
        sam = _person(people, name="Sam", context="cousin", what_matters="tea")

        listed = people.update_person(Person(id=sam, circles=[family]), clear_fields=["context"])

        assert (listed.name, listed.context, listed.circles, listed.circle_names, listed.what_matters) == (
            "Sam", None, [family], ["Family"], "tea"
        )

    @pytest.mark.parametrize(
        "person, clear, message",
        [
            (Person(), [], "needs the person's id"),
            (Person(id="nope"), [], "no person with the id or name 'nope'"),
            (Person(id="x"), ["name"], r"Can't clear \['name'\]"),
        ],
    )
    def test_refuses_what_it_cant_do(self, person, clear, message):
        people, _ = _people()

        with pytest.raises(ValueError, match=message):
            people.update_person(person, clear)


class TestGetPeople:
    def test_lists_active_people_by_default(self):
        people, _ = _people()
        sam = _person(people, name="Sam")
        _person(people, name="Alex", status="archived")

        assert [p.id for p in people.get_people()] == [SELF_ID, sam]
        assert [p.name for p in people.get_people(["archived"])] == ["Alex"]

    def test_a_shared_name_asks_for_the_id(self):
        people, _ = _people()
        a = _person(people, name="Sam", context="salsa")
        b = _person(people, name="Sam", context="work")

        with pytest.raises(ValueError, match=rf"more than one person named 'sam': {a} \(salsa\), {b} \(work\)"):
            people.get_person("sam")

    def test_suggests_close_matches(self):
        people, _ = _people()
        sam = _person(people, name="Samantha")

        with pytest.raises(ValueError, match=rf"did you mean {sam} \(Samantha\)"):
            people.get_person("samanta")


class TestCircles:
    def test_create_list_and_get(self):
        people, _ = _people()
        family = _circle(people, name="Family", note="by blood or choice")
        sam = _person(people, name="Sam", circles=[family])

        (listed,) = people.get_circles()

        assert (listed.id, listed.name, listed.note, listed.member_ids) == (family, "Family", "by blood or choice", [sam])
        assert people.get_circle("family").id == family

    def test_names_are_unique_among_circles_but_may_match_a_person(self):
        people, _ = _people()
        _person(people, name="Family")
        _circle(people, name="Family")

        with pytest.raises(ValueError, match=r"already a circle named 'Family' \(.*\); circle names must be unique"):
            _circle(people, name="family")

    def test_update_renames_and_clears(self):
        people, _ = _people()
        family = _circle(people, name="Family", note="n")

        listed = people.update_circle(Circle(id=family, name="Kin"), clear_fields=["note"])

        assert (listed.name, listed.note) == ("Kin", None)

    def test_deleting_one_takes_its_people_out_of_it(self):
        people, _ = _people()
        family = _circle(people, name="Family")
        friends = _circle(people, name="Friends")
        sam = _person(people, name="Sam", circles=[family, friends])
        alex = _person(people, name="Alex", circles=[family])

        deleted = people.delete_circle(family)

        assert (deleted.deleted.name, deleted.left) == ("Family", [sam, alex])
        assert people.get_person(sam).circles == [friends]
        assert people.get_person(alex).circles is None
        assert [c.name for c in people.get_circles()] == ["Friends"]

    def test_delete_needs_the_circles_id(self):
        people, _ = _people()
        family = _circle(people, name="Family")

        with pytest.raises(ValueError, match=f"by its id: 'Family' is {family}"):
            people.delete_circle("family")
        with pytest.raises(ValueError, match="no circle"):
            people.delete_circle("nothing")
