import pytest

from utilities.goal_measures import MEASURE_KINDS, MEASURE_SHAPE_PROBLEM, measure_problems


@pytest.mark.parametrize(
    "measure",
    [
        {"kind": "duration", "target_min": 600},
        {"kind": "duration", "target_min": 90.5},
        {"kind": "duration", "target_min": 300, "interval_days": 7},
        {"kind": "duration", "target_min": 300, "interval_days": 7, "zero_at_days": 14},
        {"kind": "count", "target": 1},
        {"kind": "count", "target": 2, "noun": "dinners"},
        {"kind": "count", "target": 1, "noun": "visits", "interval_days": 60, "zero_at_days": 90},
        {"kind": "count", "target": 1, "zero_at_days": 3},
        {"kind": "count", "target": 2, "events_of": "g1"},
        {"kind": "duration", "target_min": 2400, "interval_days": 7, "events_of": "g1"},
        {"kind": "duration", "target_min": 60, "include_sub_goals": False},
        {"kind": "count", "target": 1, "events_of": "g1", "include_sub_goals": True},
        {"kind": "time_constraint", "edge": "start", "target": "07:00"},
        {"kind": "time_constraint", "edge": "end", "target": "17:30", "when": "by", "grace_min": 10, "zero_at_min": 60},
        {"kind": "time_constraint", "edge": "start", "target": "08:00", "when": "after", "events_of": "g1"},
        {"kind": "time_window", "from": "11:30", "to": "13:30"},
        {"kind": "time_window", "from": "11:30", "to": "13:30", "grace_min": 15, "zero_at_min": 90, "events_of": "g1"},
        {"kind": "subjective", "prompt": "How did it turn out?"},
        {"kind": "subjective", "prompt": "How are we doing?", "interval_days": 7},
        {"kind": "llm", "rubric": "Were the conversations meaningful?"},
        {"kind": "rollup"},
        {"kind": "rollup", "agg": "mean"},
        {"kind": "rollup", "agg": "weighted", "weights": {"g1": 2, "g2": 0.5}},
        {"kind": "rollup", "agg": "percentile", "percentile": 0},
        {"kind": "rollup", "agg": "percentile", "percentile": 100},
        {"kind": "rollup", "agg": "percentile", "percentile": 37.5},
    ],
)
def test_accepts_every_kinds_valid_specs(measure):
    assert measure_problems(measure) == []


@pytest.mark.parametrize(
    "measure, problem",
    [
        ({"target_min": 600}, MEASURE_SHAPE_PROBLEM),
        ("duration", MEASURE_SHAPE_PROBLEM),
        ({"kind": "hours"}, "kind must be one of duration, count"),
        ({"kind": "duration"}, 'needs "target_min"'),
        (
            {"kind": "duration", "target_mins": 600},
            'has no field "target_mins"; a duration measure takes "events_of", "include_sub_goals", '
            '"interval_days", "target_min", "zero_at_days"',
        ),
        ({"kind": "duration", "target_min": 0}, '"target_min" must be a number above 0'),
        ({"kind": "duration", "target_min": "600"}, '"target_min" must be a number above 0'),
        ({"kind": "duration", "target_min": True}, '"target_min" must be a number above 0'),
        ({"kind": "duration", "target_min": 60, "interval_days": 0}, '"interval_days" must be a number above 0'),
        (
            {"kind": "count", "target": 1, "interval_days": 60, "zero_at_days": 60},
            '"zero_at_days" must be a number above "interval_days" (60)',
        ),
        ({"kind": "count", "target": 1, "zero_at_days": 1}, '"zero_at_days" must be a number above "interval_days" (1)'),
        ({"kind": "count", "target": -1}, '"target" must be a number above 0'),
        ({"kind": "count", "target": 1, "events_of": ""}, '"events_of" must be a goal id'),
        ({"kind": "duration", "target_min": 1, "events_of": ["g1"]}, '"events_of" must be a goal id'),
        ({"kind": "duration", "target_min": 1, "goal_ids": ["g1"]}, 'has no field "goal_ids"'),
        ({"kind": "wake_time", "target": "07:00"}, "kind must be one of"),
        (
            {"kind": "time_constraint", "edge": "start", "target": "07:00", "interval_days": 7},
            'has no field "interval_days"',
        ),
        ({"kind": "time_constraint", "target": "07:00"}, 'needs "edge"'),
        ({"kind": "time_constraint", "edge": "middle", "target": "07:00"}, '"edge" must be one of start, end'),
        ({"kind": "time_constraint", "edge": "end", "target": "07:00", "when": "near"}, '"when" must be one of by, after'),
        ({"kind": "count", "target": 1, "include_sub_goals": "no"}, '"include_sub_goals" must be true or false'),
        ({"kind": "count", "target": 1, "noun": " "}, '"noun" must be non-empty text'),
        ({"kind": "time_constraint", "edge": "start"}, 'needs "target"'),
        ({"kind": "time_constraint", "edge": "start", "target": "7:00"}, '"target" must be a time like "07:00"'),
        ({"kind": "time_constraint", "edge": "start", "target": "24:00"}, '"target" must be a time like "07:00"'),
        (
            {"kind": "time_constraint", "edge": "start", "target": "07:00", "grace_min": -5},
            '"grace_min" must be a number, 0 or more',
        ),
        (
            {"kind": "time_constraint", "edge": "start", "target": "07:00", "grace_min": 10, "zero_at_min": 10},
            '"zero_at_min" must be a number above "grace_min" (10)',
        ),
        ({"kind": "time_window", "from": "11:30"}, 'needs "to"'),
        ({"kind": "time_window", "from": "11:30", "to": "1:30pm"}, '"to" must be a time like "07:00"'),
        ({"kind": "time_window", "from": "13:30", "to": "11:30"}, '"from" must be before "to"'),
        ({"kind": "time_window", "from": "11:30", "to": "11:30"}, '"from" must be before "to"'),
        ({"kind": "time_window", "from": "11:30", "to": "13:30", "edge": "start"}, 'has no field "edge"'),
        (
            {"kind": "time_window", "from": "11:30", "to": "13:30", "grace_min": 30, "zero_at_min": 20},
            '"zero_at_min" must be a number above "grace_min" (30)',
        ),
        ({"kind": "subjective"}, 'needs "prompt"'),
        ({"kind": "subjective", "prompt": ""}, '"prompt" must be non-empty text'),
        ({"kind": "subjective", "prompt": "?", "interval_days": -1}, '"interval_days" must be a number above 0'),
        ({"kind": "subjective", "prompt": "?", "target": 3}, 'has no field "target"; a subjective measure takes'),
        ({"kind": "llm"}, 'needs "rubric"'),
        ({"kind": "rollup", "agg": "max"}, '"agg" must be one of mean, weighted, percentile'),
        ({"kind": "rollup", "agg": "min"}, '"agg" must be one of mean, weighted, percentile'),
        ({"kind": "rollup", "agg": "weighted"}, 'a weighted rollup needs "weights"'),
        ({"kind": "rollup", "agg": "weighted", "weights": {"g1": -1}}, 'a weighted rollup needs "weights"'),
        ({"kind": "rollup", "weights": {"g1": 1}}, '"weights" is only for a weighted rollup'),
        ({"kind": "rollup", "agg": "percentile"}, 'a percentile rollup needs "percentile"'),
        ({"kind": "rollup", "agg": "percentile", "percentile": 101}, 'a percentile rollup needs "percentile"'),
        ({"kind": "rollup", "percentile": 50}, '"percentile" is only for a percentile rollup'),
        ({"kind": "rollup", "agg": "mean", "x": 1}, 'has no field "x"; a rollup measure takes "agg"'),
    ],
)
def test_names_whats_wrong(measure, problem):
    problems = measure_problems(measure)

    assert any(p.startswith(problem) for p in problems), problems


def test_weights_must_name_sub_goals_when_theyre_known():
    measure = {"kind": "rollup", "agg": "weighted", "weights": {"g1": 1, "zz": 2}}

    assert measure_problems(measure) == []
    assert measure_problems(measure, sub_goal_ids={"g1", "g2"}) == [
        "\"weights\" names 'zz', which isn't one of its sub-goals"
    ]


def test_lists_every_problem_at_once():
    problems = measure_problems({"kind": "time_constraint", "grace_min": -1, "zero": 5})

    assert problems == [
        'needs "edge"',
        'needs "target"',
        'has no field "zero"; a time_constraint measure takes "edge", "events_of", "grace_min", '
        '"include_sub_goals", "target", "when", "zero_at_min"',
        '"grace_min" must be a number, 0 or more',
    ]


def test_covers_the_kinds_goal_health_measures():
    from utilities.goal_health import _MEASURES, MEASURED_KINDS

    assert set(_MEASURES) <= set(MEASURE_KINDS)
    assert MEASURED_KINDS <= set(MEASURE_KINDS)
