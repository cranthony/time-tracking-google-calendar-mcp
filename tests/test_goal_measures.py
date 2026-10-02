import pytest

from utilities.goal_measures import MEASURE_KINDS, MEASURE_SHAPE_PROBLEM, measure_problems


@pytest.mark.parametrize(
    "measure",
    [
        {"kind": "duration", "target_min": 600},
        {"kind": "duration", "target_min": 90.5},
        {"kind": "count", "target": 1},
        {"kind": "count", "target": 2, "noun": "dinners"},
        {"kind": "count", "target": 2, "goal_ids": ["g1", "g2"]},
        {"kind": "duration", "target_min": 60, "goal_ids": ["g1"]},
        {"kind": "duration", "target_min": 60, "include_sub_goals": False},
        {"kind": "count", "target": 1, "goal_ids": ["g1"], "include_sub_goals": True},
        {"kind": "wake_time", "target": "07:00"},
        {"kind": "wake_time", "target": "23:59", "grace_min": 10, "zero_at_min": 60},
        {"kind": "wake_time", "target": "06:30", "grace_min": 0},
        {"kind": "subjective"},
        {"kind": "subjective", "prompt": "How did it turn out?"},
        {"kind": "llm", "rubric": "Were the conversations meaningful?"},
        {"kind": "rollup"},
        {"kind": "rollup", "agg": "mean"},
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
        ({"kind": "duration", "target_mins": 600}, 'has no field "target_mins"; a duration measure takes "goal_ids", "include_sub_goals", "target_min"'),
        ({"kind": "duration", "target_min": 0}, '"target_min" must be a number above 0'),
        ({"kind": "duration", "target_min": "600"}, '"target_min" must be a number above 0'),
        ({"kind": "duration", "target_min": True}, '"target_min" must be a number above 0'),
        ({"kind": "count", "target": -1}, '"target" must be a number above 0'),
        ({"kind": "count", "target": 1, "goal_ids": []}, '"goal_ids" must be a list of goal ids'),
        ({"kind": "duration", "target_min": 1, "goal_ids": "g1"}, '"goal_ids" must be a list of goal ids'),
        ({"kind": "wake_time", "target": "07:00", "goal_ids": ["g1"]}, 'has no field "goal_ids"'),
        ({"kind": "count", "target": 1, "include_sub_goals": "no"}, '"include_sub_goals" must be true or false'),
        ({"kind": "count", "target": 1, "noun": " "}, '"noun" must be non-empty text'),
        ({"kind": "wake_time"}, 'needs "target"'),
        ({"kind": "wake_time", "target": "7:00"}, '"target" must be a time like "07:00"'),
        ({"kind": "wake_time", "target": "24:00"}, '"target" must be a time like "07:00"'),
        ({"kind": "wake_time", "target": "07:00", "grace_min": -5}, '"grace_min" must be a number, 0 or more'),
        (
            {"kind": "wake_time", "target": "07:00", "grace_min": 10, "zero_at_min": 10},
            '"zero_at_min" must be a number above "grace_min" (10)',
        ),
        ({"kind": "subjective", "prompt": ""}, '"prompt" must be non-empty text'),
        ({"kind": "subjective", "target": 3}, 'has no field "target"; a subjective measure takes "prompt"'),
        ({"kind": "llm"}, 'needs "rubric"'),
        ({"kind": "rollup", "agg": "max"}, '"agg" must be one of min, mean'),
        ({"kind": "rollup", "agg": "min", "x": 1}, 'has no field "x"; a rollup measure takes "agg"'),
    ],
)
def test_names_whats_wrong(measure, problem):
    problems = measure_problems(measure)

    assert any(p.startswith(problem) for p in problems), problems


def test_lists_every_problem_at_once():
    problems = measure_problems({"kind": "wake_time", "grace_min": -1, "zero": 5})

    assert problems == [
        'needs "target"',
        'has no field "zero"; a wake_time measure takes "grace_min", "target", "zero_at_min"',
        '"grace_min" must be a number, 0 or more',
    ]


def test_covers_the_kinds_goal_health_measures():
    from utilities.goal_health import _MEASURES

    assert set(_MEASURES) <= set(MEASURE_KINDS)
