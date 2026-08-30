import math
import sys
from pathlib import Path

import pandas as pd
import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from surveycto_checker import SurveyCTOChecker  # noqa: E402


START_CLOCK = "once(format-date-time(now(), '%Y-%m-%dT%H:%M:%S'))"


def _row(field_type, name="", calculation="", relevance="", appearance=""):
    return {
        "type": field_type,
        "name": name,
        "calculation": calculation,
        "relevance": relevance,
        "appearance": appearance,
    }


def _survey_bundle():
    return [
        _row("start", "start"),
        _row("calculate_here", "survey_start_elapsed_sec", "once(duration())"),
        _row("calculate_here", "survey_start_time", START_CLOCK),
    ]


def _survey_finish():
    return [
        _row("calculate_here", "survey_end_elapsed_sec", "once(duration())"),
        _row("calculate_here", "survey_end_time", START_CLOCK),
        _row(
            "calculate",
            "overall_duration_sec",
            "if(string-length(${survey_end_elapsed_sec}) > 0 and "
            "string-length(${survey_start_elapsed_sec}) > 0, "
            "round(${survey_end_elapsed_sec} - ${survey_start_elapsed_sec}, 0), '')",
        ),
        _row("end", "end"),
    ]


def _section(slug, question_count=3, relevance=""):
    rows = [
        _row("begin group", slug, relevance=relevance),
        _row("calculate_here", f"{slug}_start_elapsed_sec", "once(duration())"),
        _row("calculate_here", f"{slug}_start_time", START_CLOCK),
    ]
    rows.extend(_row("text", f"{slug}_q{i}") for i in range(question_count))
    rows.extend(
        [
            _row("calculate_here", f"{slug}_end_elapsed_sec", "once(duration())"),
            _row("calculate_here", f"{slug}_end_time", START_CLOCK),
            _row(
                "calculate",
                f"{slug}_duration_sec",
                f"if(string-length(${{{slug}_end_elapsed_sec}}) > 0 and "
                f"string-length(${{{slug}_start_elapsed_sec}}) > 0, "
                f"round(${{{slug}_end_elapsed_sec}} - "
                f"${{{slug}_start_elapsed_sec}}, 0), '')",
            ),
            _row("end group", slug),
        ]
    )
    return rows


def _check(rows):
    checker = SurveyCTOChecker("dummy.xlsx")
    checker.survey_df = pd.DataFrame(rows)
    checker.check_timing_instrumentation()
    return checker


def test_complete_canonical_timing_passes():
    checker = _check(_survey_bundle() + _section("health") + _survey_finish())

    assert checker.errors == []
    assert checker.warnings == []


def test_missing_survey_and_section_timing_is_an_error():
    checker = _check(
        [_row("start", "start")]
        + [_row("text", f"q{i}") for i in range(5)]
        + [_row("end", "end")]
    )

    joined = "\n".join(checker.errors)
    assert "survey-level start timestamp" in joined
    assert "survey-level end timestamp" in joined
    assert "overall duration" in joined
    assert "No section timing" in joined


def test_missing_start_or_end_metadata_is_an_error():
    rows = _survey_bundle() + _section("health") + _survey_finish()
    rows = [row for row in rows if row["type"] not in {"start", "end"}]

    checker = _check(rows)

    joined = "\n".join(checker.errors)
    assert "metadata `start`" in joined
    assert "metadata `end`" in joined


def test_incomplete_canonical_section_bundle_is_an_error():
    rows = _survey_bundle() + [
        _row("begin group", "health"),
        _row("calculate_here", "health_start_elapsed_sec", "once(duration())"),
        _row("text", "health_q1"),
        _row("end group", "health"),
    ] + _survey_finish()

    checker = _check(rows)

    joined = "\n".join(checker.errors)
    assert "Incomplete timing bundle for section 'health'" in joined
    assert "health_start_time" in joined
    assert "health_end_elapsed_sec" in joined
    assert "health_end_time" in joined
    assert "health_duration_sec" in joined


def test_malformed_canonical_duration_is_an_error():
    rows = _survey_bundle() + _section("health") + _survey_finish()
    for row in rows:
        if row["name"] == "health_duration_sec":
            row["calculation"] = (
                "${health_end_elapsed_sec} - ${health_start_elapsed_sec}"
            )

    checker = _check(rows)

    assert any("health_duration_sec" in error and "guarded" in error
               for error in checker.errors)


def test_canonical_section_bundle_must_share_one_group_path():
    rows = _survey_bundle() + [
        _row("begin group", "health"),
        _row("calculate_here", "health_start_elapsed_sec", "once(duration())"),
        _row("calculate_here", "health_start_time", START_CLOCK),
        _row("text", "health_q1"),
        _row("end group", "health"),
        _row("calculate_here", "health_end_elapsed_sec", "once(duration())"),
        _row("calculate_here", "health_end_time", START_CLOCK),
        _row(
            "calculate",
            "health_duration_sec",
            "if(string-length(${health_end_elapsed_sec}) > 0 and "
            "string-length(${health_start_elapsed_sec}) > 0, "
            "round(${health_end_elapsed_sec} - ${health_start_elapsed_sec}, 0), '')",
        ),
    ] + _survey_finish()

    checker = _check(rows)

    assert any("health" in error and "same group path" in error
               for error in checker.errors)


def test_survey_timing_must_be_outside_groups():
    rows = [
        _row("start", "start"),
        _row("begin group", "wrapper"),
        _row("calculate_here", "survey_start_elapsed_sec", "once(duration())"),
        _row("calculate_here", "survey_start_time", START_CLOCK),
        _row("end group", "wrapper"),
    ] + _section("health") + _survey_finish()[0:3] + [
        _row("end", "end")
    ]

    checker = _check(rows)

    assert any("Survey-level timing fields must be outside" in error
               for error in checker.errors)


def test_sparse_section_timing_is_a_warning():
    rows = _survey_bundle() + _section("health", question_count=3)
    rows.extend(_row("text", f"outside_q{i}") for i in range(117))
    rows.extend(_survey_finish())

    checker = _check(rows)

    assert checker.errors == []
    assert any("Sparse section timing coverage" in warning
               and "1 timing unit" in warning
               and f"at least {math.ceil(120 / 40)}" in warning
               for warning in checker.warnings)


def test_legacy_k2_checkpoint_pairs_are_accepted():
    rows = [
        _row("start", "start"),
        _row("end", "end"),
        _row("calculate_here", "time_survey_start", START_CLOCK),
        _row("begin group", "consent"),
        _row("text", "consent_q1"),
        _row("text", "consent_q2"),
        _row("text", "consent_q3"),
        _row("end group", "consent"),
        _row("calculate_here", "duration_consent", "once(duration())"),
        _row("calculate_here", "time_consent", START_CLOCK),
        _row("begin group", "health"),
        _row("text", "health_q1"),
        _row("text", "health_q2"),
        _row("text", "health_q3"),
        _row("end group", "health"),
        _row("calculate_here", "duration_health", "once(duration())"),
        _row("calculate_here", "time_health", START_CLOCK),
        _row("calculate_here", "duration_fin", "once(duration())"),
        _row("calculate_here", "time_survey_end", START_CLOCK),
    ]

    checker = _check(rows)

    assert checker.errors == []
    assert checker.warnings == []


def test_incomplete_legacy_checkpoint_pair_is_an_error():
    rows = [
        _row("start", "start"),
        _row("calculate_here", "time_survey_start", START_CLOCK),
        _row("text", "health_q1"),
        _row("calculate_here", "duration_health", "once(duration())"),
        _row("calculate_here", "duration_fin", "once(duration())"),
        _row("calculate_here", "time_survey_end", START_CLOCK),
        _row("end", "end"),
    ]

    checker = _check(rows)

    assert any("Incomplete legacy timing pair 'health'" in error
               for error in checker.errors)


def test_standalone_business_duration_does_not_create_canonical_intent():
    rows = [
        _row("start", "start"),
        _row("calculate_here", "time_survey_start", START_CLOCK),
        _row("calculate", "callback_duration_sec", "${callback_days} * 86400"),
        _row("text", "health_q1"),
        _row("text", "health_q2"),
        _row("text", "health_q3"),
        _row("calculate_here", "duration_health", "once(duration())"),
        _row("calculate_here", "time_health", START_CLOCK),
        _row("calculate_here", "duration_fin", "once(duration())"),
        _row("calculate_here", "time_survey_end", START_CLOCK),
        _row("end", "end"),
    ]

    checker = _check(rows)

    assert not any("callback" in error for error in checker.errors)


def test_ordinary_time_questions_do_not_create_canonical_intent():
    rows = [
        _row("start", "start"),
        _row("end", "end"),
        _row("calculate_here", "time_survey_start", START_CLOCK),
        _row("time", "work_start_time"),
        _row("time", "work_end_time"),
        _row("text", "work_notes"),
        _row("calculate_here", "duration_work", "once(duration())"),
        _row("calculate_here", "time_work", START_CLOCK),
        _row("calculate_here", "time_survey_end", START_CLOCK),
    ]

    checker = _check(rows)

    assert not any("Incomplete timing bundle for section 'work'" in error
                   for error in checker.errors)


def test_legacy_pair_before_later_questions_does_not_supply_overall_duration():
    rows = [
        _row("start", "start"),
        _row("end", "end"),
        _row("calculate_here", "time_survey_start", START_CLOCK),
        _row("text", "health_q1"),
        _row("calculate_here", "duration_health", "once(duration())"),
        _row("calculate_here", "time_health", START_CLOCK),
        _row("text", "later_q1"),
        _row("text", "later_q2"),
        _row("text", "later_q3"),
        _row("calculate_here", "time_survey_end", START_CLOCK),
    ]

    checker = _check(rows)

    assert any("Missing overall duration" in error for error in checker.errors)


def test_section_timing_must_bracket_its_questions():
    rows = _survey_bundle() + [
        _row("begin group", "health"),
        _row("calculate_here", "health_start_elapsed_sec", "once(duration())"),
        _row("calculate_here", "health_start_time", START_CLOCK),
        _row("calculate_here", "health_end_elapsed_sec", "once(duration())"),
        _row("calculate_here", "health_end_time", START_CLOCK),
        _row(
            "calculate",
            "health_duration_sec",
            "if(string-length(${health_end_elapsed_sec}) > 0 and "
            "string-length(${health_start_elapsed_sec}) > 0, "
            "round(${health_end_elapsed_sec} - ${health_start_elapsed_sec}, 0), '')",
        ),
        _row("text", "health_q1"),
        _row("text", "health_q2"),
        _row("text", "health_q3"),
        _row("end group", "health"),
    ] + _survey_finish()

    checker = _check(rows)

    assert any("health" in error and "bracket" in error
               for error in checker.errors)


def test_malformed_legacy_clock_format_is_an_error():
    rows = [
        _row("start", "start"),
        _row("end", "end"),
        _row("calculate_here", "time_survey_start", START_CLOCK),
        _row("text", "health_q1"),
        _row("calculate_here", "duration_health", "once(duration())"),
        _row(
            "calculate_here",
            "time_health",
            "once(format-date-time(now(), 'banana'))",
        ),
        _row("calculate_here", "time_survey_end", START_CLOCK),
    ]

    checker = _check(rows)

    assert any("time_health" in error and "date and time" in error
               for error in checker.errors)


def test_enumerator_fields_count_as_respondent_input():
    checker = _check([
        _row("start", "start"),
        _row("enumerator", "enumerator_id"),
        _row("end", "end"),
    ])

    assert any("No section timing was found for 1 question fields" in error
               for error in checker.errors)


def test_timing_check_is_skipped_for_odk_and_kobo_profiles():
    for platform in ("odk", "kobo"):
        checker = SurveyCTOChecker("dummy.xlsx", platform=platform)
        checker.survey_df = pd.DataFrame([
            _row("text", "q1"),
        ])

        checker.check_timing_instrumentation()

        assert checker.errors == []
        assert checker.warnings == []


def test_unknown_platform_profile_is_rejected():
    with pytest.raises(ValueError, match="platform must be one of"):
        SurveyCTOChecker("dummy.xlsx", platform="other")


def test_legacy_survey_timestamps_must_bracket_respondent_input():
    rows = [
        _row("start", "start"),
        _row("end", "end"),
        _row("text", "first_q"),
        _row("calculate_here", "time_survey_start", START_CLOCK),
        _row("text", "health_q1"),
        _row("calculate_here", "duration_health", "once(duration())"),
        _row("calculate_here", "time_health", START_CLOCK),
        _row("calculate_here", "time_survey_end", START_CLOCK),
    ]

    checker = _check(rows)

    assert any("survey-level start timestamp" in error
               and "precede the first respondent-input field" in error
               for error in checker.errors)


def test_legacy_pair_named_for_group_must_follow_that_group():
    rows = [
        _row("start", "start"),
        _row("end", "end"),
        _row("calculate_here", "time_survey_start", START_CLOCK),
        _row("calculate_here", "duration_health", "once(duration())"),
        _row("calculate_here", "time_health", START_CLOCK),
        _row("begin group", "health"),
        _row("text", "health_q1"),
        _row("text", "health_q2"),
        _row("text", "health_q3"),
        _row("end group", "health"),
        _row("calculate_here", "time_survey_end", START_CLOCK),
    ]

    checker = _check(rows)

    assert any("Legacy timing pair 'health'" in error
               and "after the matching group" in error
               for error in checker.errors)


def test_ungrouped_canonical_timing_must_contain_questions_between_boundaries():
    rows = _survey_bundle() + [
        _row("calculate_here", "health_start_elapsed_sec", "once(duration())"),
        _row("calculate_here", "health_start_time", START_CLOCK),
        _row("calculate_here", "health_end_elapsed_sec", "once(duration())"),
        _row("calculate_here", "health_end_time", START_CLOCK),
        _row(
            "calculate",
            "health_duration_sec",
            "if(string-length(${health_end_elapsed_sec}) > 0 and "
            "string-length(${health_start_elapsed_sec}) > 0, "
            "round(${health_end_elapsed_sec} - ${health_start_elapsed_sec}, 0), '')",
        ),
        _row("text", "health_q1"),
        _row("text", "health_q2"),
        _row("text", "health_q3"),
    ] + _survey_finish()

    checker = _check(rows)

    assert any("health" in error and "at least one respondent-input field" in error
               for error in checker.errors)
