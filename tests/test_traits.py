import pytest

from tests.fake_sheets import FakeSheets
from utilities import calendar_metadata_sheet
from utilities.goal_measures import measure_problems
from utilities.traits import SEED_TRAITS, Trait, Traits, part_keys, part_problems, trait_problems


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
        sheets.write_rows_in_sheet("spreadsheet", 1, "E2:E2", [['[{"kind": "prep", "target": 0}]']])

        listed = traits.get_traits()[0]

        assert listed.problems == ['part 1 (prep) "target" must be a number above 0']
        assert traits.get_traits()[1].problems is None

    def test_refuses_an_unknown_status(self):
        traits, _ = _traits()

        with pytest.raises(ValueError, match="Unknown trait status"):
            traits.get_traits(["gone"])


class TestCreateTrait:
    def test_gets_an_id_from_its_name_and_is_active(self):
        traits, _ = _traits()

        created = traits.create_trait(Trait(name="Playful Spirit", parts=[{"kind": "novelty"}]))

        assert created.id == "playful-spirit"
        assert created.status == "active"
        assert traits.all()[-1] == created

    def test_a_taken_id_gets_a_number(self):
        traits, _ = _traits()

        created = traits.create_trait(Trait(name="Thoughtful!", parts=[{"kind": "prep"}]))

        assert created.id == "thoughtful-2"

    def test_refuses_a_name_already_used(self):
        traits, _ = _traits()

        with pytest.raises(ValueError, match="There's already a trait named 'Generous'"):
            traits.create_trait(Trait(name="generous", parts=[{"kind": "prep"}]))

    def test_refuses_a_bad_part_saying_why(self):
        traits, _ = _traits()

        with pytest.raises(ValueError, match=r"The trait 'Kind': part 1 \(effort_paid\) needs \"target\""):
            traits.create_trait(Trait(name="Kind", parts=[{"kind": "effort_paid"}]))
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
            {"kind": "prep"},
            {"kind": "prep", "target": 2, "window_days": 60, "weight": 0},
            {"kind": "prep_regularity", "weeks": 8},
            {"kind": "continuity", "last_within_days": 30, "next_within_days": 7},
            {"kind": "together_creative", "min_creative": 3},
            {"kind": "novelty", "target": 2},
            {"kind": "effort_paid", "target": 600, "weight": 2.5},
            {"kind": "attention"},
            {"kind": "judgment", "rubric": "Did they feel heard?"},
            {"kind": "count", "target": 1, "interval_days": 14, "zero_at_days": 30},
            {"kind": "duration", "target_min": 120},
            {"kind": "follow_through", "penalty": 20},
        ],
    )
    def test_accepts_every_kind(self, part):
        assert part_problems(part) == []

    @pytest.mark.parametrize(
        "part, problem",
        [
            ("prep", 'must be an object with a "kind"'),
            ({"kind": "rollup"}, "has kind 'rollup'; a part's kind is one of prep,"),
            ({"kind": "count", "target": 1, "events_of": "g1"}, 'has "events_of", but a part has no scope'),
            ({"kind": "novelty", "noun": "x"}, 'has no field "noun"; a novelty part takes "target", "weight", "window_days"'),
            ({"kind": "prep", "weight": -1}, '"weight" must be a number, 0 or more'),
            ({"kind": "prep_regularity", "weeks": 1.5}, '"weeks" must be a whole number, 1 or more'),
            ({"kind": "together_creative", "min_creative": 0}, '"min_creative" must be 1, 2 or 3'),
            ({"kind": "judgment", "rubric": " "}, '"rubric" must be non-empty text'),
            ({"kind": "count", "target": 0}, '"target" must be a number above 0'),
        ],
    )
    def test_refuses_what_isnt_a_part(self, part, problem):
        assert part_problems(part)[0].startswith(problem)


def test_part_keys_number_repeated_kinds():
    assert part_keys([{"kind": "count"}, {"kind": "prep"}, {"kind": "count"}]) == ["count", "prep", "count#2"]


class TestTraitsMeasure:
    @pytest.mark.parametrize(
        "measure",
        [
            {"kind": "traits", "traits": "all"},
            {"kind": "traits", "traits": ["reliable"], "window_days": 14},
            {"kind": "traits", "traits": "all", "weights": {"reliable": 2, "creative": 0}},
            {"kind": "traits", "traits": ["reliable", "generous"], "weights": {"generous": 0.5}, "only_if": {}},
        ],
    )
    def test_accepts_valid_specs(self, measure):
        assert measure_problems(measure, trait_ids={"reliable", "creative", "generous"}) == []

    @pytest.mark.parametrize(
        "measure, problem",
        [
            ({"kind": "traits"}, 'needs "traits"'),
            ({"kind": "traits", "traits": []}, '"traits" must be "all" or a list of trait ids'),
            ({"kind": "traits", "traits": ["reliable", "reliable"]}, '"traits" names a trait more than once'),
            ({"kind": "traits", "traits": "all", "weights": {"reliable": -1}}, '"weights" must be {trait id'),
            ({"kind": "traits", "traits": ["reliable"], "weights": {"creative": 1}}, "\"weights\" names 'creative'"),
            ({"kind": "traits", "traits": ["kind"]}, "names 'kind', which isn't a trait"),
            ({"kind": "traits", "traits": "all", "window_days": 0}, '"window_days" must be a number above 0'),
            ({"kind": "traits", "traits": "all", "events_of": "g1"}, 'has no field "events_of"'),
        ],
    )
    def test_refuses_invalid_specs(self, measure, problem):
        assert measure_problems(measure, trait_ids={"reliable", "creative"})[0].startswith(problem)
