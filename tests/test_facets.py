import pytest

from utilities.facets import MAX_FACETS_CHARS, Facets, facet_problems, goal_ids_in


class TestNormalized:
    def test_labels_are_trimmed_single_spaced_and_lowercase(self):
        facets = Facets(activity="  Salsa   Social ", place="The  Venue", why="  talked  it through ")

        assert facets.normalized() == Facets(activity="salsa social", place="the venue", why="talked it through")

    def test_blank_text_is_dropped_and_ids_deduplicated(self):
        facets = Facets(activity="  ", with_goal_ids=["g1", "g1", " g2 ", ""])

        assert facets.normalized() == Facets(with_goal_ids=["g1", "g2"])


class TestJson:
    def test_leaves_out_whats_unset(self):
        assert Facets(effort=0, with_goal_ids=[]).to_json() == '{"effort":0}'

    def test_reads_back_ignoring_unknown_keys(self):
        assert Facets.from_json('{"with":["g1"],"mood":"good","attention":3}') == Facets(
            with_goal_ids=["g1"], attention=3
        )

    @pytest.mark.parametrize("raw", ["", "[1]", "{"])
    def test_anything_but_an_object_is_none(self, raw):
        assert Facets.from_json(raw) is None


class TestProblems:
    def test_well_formed_facets_have_none(self):
        facets = Facets(
            with_goal_ids=["g1"], for_goal_ids=["g2"], activity="dinner", place="home", creative=0, new="both",
            effort=3, attention=2, why="cooked for them",
        )

        assert facet_problems(facets) == []

    @pytest.mark.parametrize(
        "facets, problem",
        [
            (Facets(creative=4), '"creative" must be a whole number from 0 to 3'),
            (Facets(effort=-1), '"effort" must be a whole number from 0 to 3'),
            (Facets(attention=True), '"attention" must be a whole number from 0 to 3'),
            (Facets(new="yes"), '"new" must be one of none, activity, place, both'),
            (Facets(activity="x" * 61), '"activity" must be a short label, at most 60 characters'),
            (Facets(why="x" * 201), '"why" must be one line, at most 200 characters'),
            (Facets(with_goal_ids="g1"), '"with_goal_ids" must be a list of goal ids'),
        ],
    )
    def test_refuses_what_isnt(self, facets, problem):
        assert facet_problems(facets) == [problem]

    def test_refuses_facets_too_long_for_calendar(self):
        facets = Facets(with_goal_ids=[f"g{i:05}" for i in range(120)])

        assert len(facets.to_json()) > MAX_FACETS_CHARS
        assert facet_problems(facets) == [f'are longer than {MAX_FACETS_CHARS} characters as JSON']


def test_goal_ids_in_lists_with_then_for():
    assert goal_ids_in(Facets(with_goal_ids=["g1"], for_goal_ids=["g2", "g1"])) == ["g1", "g2"]
    assert goal_ids_in(None) == []
