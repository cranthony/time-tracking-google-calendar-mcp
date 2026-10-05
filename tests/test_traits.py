import pytest

from tests.fake_sheets import FakeSheets
from utilities import calendar_metadata_sheet
from utilities.traits import (
    SEED_TRAITS,
    Trait,
    Traits,
    fact_lookbacks,
    judgment_scale,
    part_keys,
    part_problems,
    person_traits_problems,
    trait_problems,
)


def _traits(sheets=None) -> tuple[Traits, FakeSheets]:
    sheets = sheets or FakeSheets()
    return Traits.ensure(sheets, "spreadsheet"), sheets


class TestEnsure:
    def test_a_new_tab_is_seeded_with_the_five_starting_traits(self):
        traits, sheets = _traits()

        assert [t.id for t in traits.all()] == ["thoughtful", "reliable", "creative", "adventurous", "generous"]
        assert traits.all() == SEED_TRAITS
        assert sheets.tags[("sheet-role", calendar_metadata_sheet.TRAITS_SHEET_ROLE)] is not None

    def test_an_existing_tab_isnt_seeded_again(self):
        traits, sheets = _traits()
        traits.update_trait(Trait(id="creative", status="off"))

        again, _ = _traits(sheets)

        assert again.by_id()["creative"].status == "off"
        assert len(again.all()) == 5

    def test_the_seeds_are_well_formed(self):
        assert all(trait_problems(t) == [] for t in SEED_TRAITS)


class TestGetTraits:
    def test_lists_active_and_off_traits_by_default(self):
        traits, _ = _traits()
        traits.update_trait(Trait(id="creative", status="off"))
        traits.update_trait(Trait(id="adventurous", status="archived"))

        assert [t.id for t in traits.get_traits()] == ["thoughtful", "reliable", "creative", "generous"]
        assert [t.id for t in traits.get_traits(["archived"])] == ["adventurous"]

    def test_a_hand_edited_trait_shows_its_problems(self):
        traits, sheets = _traits()
        sheets.write_rows_in_sheet("spreadsheet", 1, "E2:E2", [['[{"kind": "count", "target": 0}]']])

        listed = traits.get_traits()[0]

        assert listed.problems == ['part 1 (count) "target" must be a number above 0']
        assert traits.get_traits()[1].problems is None

    def test_refuses_an_unknown_status(self):
        traits, _ = _traits()

        with pytest.raises(ValueError, match="Unknown trait status"):
            traits.get_traits(["gone"])


_JUDGMENT = {
    "kind": "judgment",
    "rubric": "Was this activity or place new?",
    "ratings": {"0": "routine", "1": "a twist", "2": "new", "3": "adventurous"},
    "facts": ["action", {"fact": "location_history", "lookback_days": 90}],
}


class TestCreateTrait:
    def test_gets_an_id_from_its_name_and_is_active(self):
        traits, _ = _traits()

        created = traits.create_trait(Trait(name="Playful Spirit", parts=[_JUDGMENT]))

        assert created.id == "playful-spirit"
        assert created.status == "active"
        assert traits.all()[-1] == created

    def test_a_taken_id_gets_a_number(self):
        traits, _ = _traits()

        created = traits.create_trait(Trait(name="Thoughtful!", parts=[_JUDGMENT]))

        assert created.id == "thoughtful-2"

    def test_refuses_a_name_already_used(self):
        traits, _ = _traits()

        with pytest.raises(ValueError, match="There's already a trait named 'Generous'"):
            traits.create_trait(Trait(name="generous", parts=[_JUDGMENT]))

    def test_refuses_a_bad_part_saying_why(self):
        traits, _ = _traits()

        with pytest.raises(ValueError, match=r"The trait 'Kind': part 1 \(judgment\) needs \"facts\""):
            traits.create_trait(Trait(name="Kind", parts=[{"kind": "judgment", "rubric": "R", "ratings": {"0": "a", "1": "b"}}]))
        assert len(traits.all()) == 5


class TestUpdateTrait:
    def test_renames_and_rewords_keeping_its_id_and_parts(self):
        traits, _ = _traits()

        updated = traits.update_trait(Trait(id="generous", name="Giving", definition="Give."))

        assert (updated.id, updated.name, updated.definition) == ("generous", "Giving", "Give.")
        assert updated.parts == SEED_TRAITS[4].parts
        assert traits.by_id()["generous"] == updated

    def test_new_parts_replace_the_old_whole(self):
        traits, _ = _traits()

        traits.update_trait(Trait(id="reliable", parts=[{"kind": "follow_through", "weight": 2}]))

        assert traits.by_id()["reliable"].parts == [{"kind": "follow_through", "weight": 2}]

    def test_clears_the_definition(self):
        traits, _ = _traits()

        traits.update_trait(Trait(id="creative"), clear_fields=["definition"])

        assert traits.by_id()["creative"].definition is None

    @pytest.mark.parametrize(
        "trait, clear, message",
        [
            (Trait(id="nope", name="X"), (), "'nope' isn't a trait; the traits are thoughtful"),
            (Trait(id="creative", parts=[]), (), "needs parts"),
            (Trait(id="creative", status="gone"), (), "its status must be one of active, off, archived"),
            (Trait(id="creative"), ["name"], "Can't clear"),
            (Trait(id="creative", definition="x"), ["definition"], "Can't both set and clear"),
            (Trait(name="x"), (), "needs the trait's id"),
        ],
    )
    def test_refuses_what_it_cant_do(self, trait, clear, message):
        traits, _ = _traits()

        with pytest.raises(ValueError, match=message):
            traits.update_trait(trait, clear)


class TestPartProblems:
    @pytest.mark.parametrize(
        "part",
        [
            _JUDGMENT,
            {**_JUDGMENT, "engagement_type": "for", "weight": 0, "facts": ["general_notes", "person_notes"]},
            {**_JUDGMENT, "facts": []},
            {"kind": "continuity", "last_within_days": 30, "next_within_days": 7, "engagement_type": "with"},
            {"kind": "count", "target": 1, "interval_days": 14, "zero_at_days": 30, "action": "a1b2c3"},
            {"kind": "duration", "target_min": 120, "engagement_type": "for", "weight": 2.5},
            {"kind": "follow_through", "penalty": 20},
        ],
    )
    def test_accepts_every_kind(self, part):
        assert part_problems(part) == []

    @pytest.mark.parametrize(
        "part, problem",
        [
            ("judgment", 'must be an object with a "kind"'),
            ({"kind": "prep"}, "has kind 'prep'; a part's kind is one of judgment, continuity, count, duration"),
            ({"kind": "count", "target": 1, "events_of": "g1"}, 'has no field "events_of"; a count part takes'),
            ({"kind": "follow_through", "weight": -1}, '"weight" must be a number, 0 or more'),
            ({"kind": "continuity", "engagement_type": "at"}, '"engagement_type" must be "with" or "for"'),
            ({"kind": "count", "target": 0}, '"target" must be a number above 0'),
            ({"kind": "count", "target": 1, "interval_days": 30, "zero_at_days": 14}, '"zero_at_days" must be'),
            ({"kind": "count", "target": 1, "action": ""}, '"action" must be an action or action group id'),
            ({"kind": "follow_through", "penalty": 101}, '"penalty" must be a number from 0 to 100'),
            ({**_JUDGMENT, "rubric": " "}, '"rubric" must be non-empty text'),
            ({**_JUDGMENT, "ratings": {"0": "only one"}}, '"ratings" must be an object of at least two ratings'),
            ({**_JUDGMENT, "ratings": {"low": "a", "high": "b"}}, '"ratings" must be an object'),
            ({**_JUDGMENT, "facts": "action"}, '"facts" must be a list of facts'),
            ({**_JUDGMENT, "facts": ["mood"]}, "\"facts\" has 'mood'; a fact is one of action, action_history"),
            ({**_JUDGMENT, "facts": [{"fact": "action", "lookback_days": 7}]}, '"facts": only action_history and'),
            ({**_JUDGMENT, "facts": [{"fact": "action_history", "lookback_days": 0}]}, "\"facts\": action_history's"),
            ({**_JUDGMENT, "facts": [{"fact": "location", "days": 1}]}, "\"facts\": location has no field 'days'"),
            ({**_JUDGMENT, "facts": ["action", {"fact": "action"}]}, '"facts" names action more than once'),
        ],
    )
    def test_refuses_what_isnt_a_part(self, part, problem):
        assert part_problems(part)[0].startswith(problem), part_problems(part)


def test_judgment_scale_and_fact_lookbacks():
    assert judgment_scale(_JUDGMENT) == 3
    assert fact_lookbacks({**_JUDGMENT, "facts": ["action", "action_history", {"fact": "location_history", "lookback_days": 90}]}) == {
        "action": 0, "action_history": 30, "location_history": 90,
    }


class TestPersonTraits:
    @pytest.mark.parametrize(
        "spec",
        [
            {},
            {"select": "all"},
            {"select": ["reliable"], "parts": {"reliable": [{"kind": "count", "target": 1, "interval_days": 21}]}},
            {"parts": {"creative": [_JUDGMENT]}},
        ],
    )
    def test_accepts_valid_specs(self, spec):
        assert person_traits_problems(spec, {"reliable", "creative"}) == []

    @pytest.mark.parametrize(
        "spec, problem",
        [
            ([], "must be an object"),
            ({"traits": "all"}, 'has no field "traits"'),
            ({"select": "some"}, '"select" must be "all" or a list of trait ids'),
            ({"select": ["kind"]}, "\"select\" names 'kind', which isn't a trait"),
            ({"select": ["reliable"], "parts": {"creative": [_JUDGMENT]}}, "\"parts\" names 'creative', which isn't selected"),
            ({"parts": {"reliable": []}}, "\"parts\" for 'reliable' must be a list of at least one part"),
            ({"parts": {"reliable": [{"kind": "count"}]}}, "\"parts\" for 'reliable': part 1 (count) needs \"target\""),
            ({"parts": []}, '"parts" must be {trait id: [parts]}'),
        ],
    )
    def test_refuses_invalid_specs(self, spec, problem):
        assert person_traits_problems(spec, {"reliable", "creative"})[0].startswith(problem)


def test_part_keys_number_repeated_kinds():
    assert part_keys([{"kind": "count"}, {"kind": "judgment"}, {"kind": "count"}]) == ["count", "judgment", "count#2"]
