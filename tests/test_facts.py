import pytest

from utilities.facts import MAX_NOTE_CHARS, Facts, fact_problems


def test_round_trip_as_compact_json_under_short_keys():
    facts = Facts(location_id="home", with_ids=["sam"], for_ids=["mom"], notes={"self": "tired", "sam": "glad"})

    assert facts.to_json() == '{"location":"home","with":["sam"],"for":["mom"],"notes":{"self":"tired","sam":"glad"}}'
    assert Facts.from_json(facts.to_json()) == facts


def test_from_json_ignores_unknown_keys_and_refuses_what_isnt_facts():
    assert Facts.from_json('{"with":["sam"],"mood":"good"}') == Facts(with_ids=["sam"])
    assert Facts.from_json("not json") is None
    assert Facts.from_json("[1]") is None


def test_normalized_trims_dedupes_and_drops_blank_notes():
    facts = Facts(location_id=" home ", with_ids=[" sam", "sam", ""], notes={"sam": "  so  glad ", "self": "  "})

    assert facts.normalized() == Facts(location_id="home", with_ids=["sam"], notes={"sam": "so glad"})


def test_empty_and_people():
    assert Facts().is_empty() and Facts(with_ids=[], notes={}).is_empty()
    assert Facts(with_ids=["a"], for_ids=["b"], notes={"self": "x", "a": "y"}).people() == ["a", "b", "self"]


@pytest.mark.parametrize(
    "facts, problem",
    [
        (Facts(location_id=3), '"location_id" must be a location id'),
        (Facts(with_ids="sam"), '"with_ids" must be a list of person ids'),
        (Facts(with_ids=["self"]), '"with_ids" never names "self"'),
        (Facts(with_ids=["sam"], for_ids=["sam"]), "['sam'] can't be both \"with\" (there) and \"for\" (not there)"),
        (Facts(notes=["x"]), '"notes" must be {person id: a note}'),
        (Facts(notes={"self": "x" * (MAX_NOTE_CHARS + 1)}), "\"notes\" for ['self'] must be at most"),
        (Facts(for_ids=["mom"], notes={"mom": "x"}), "\"notes\" are for the people who were there"),
    ],
)
def test_problems(facts, problem):
    assert fact_problems(facts)[0].startswith(problem)


def test_well_formed_facts_have_no_problems():
    assert fact_problems(Facts(location_id="home", with_ids=["sam"], for_ids=["mom"], notes={"self": "a", "sam": "b"})) == []
