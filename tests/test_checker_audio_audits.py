"""Tests for SurveyCTOChecker.check_audio_audits."""

import sys
from pathlib import Path

import pandas as pd

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from surveycto_checker import SurveyCTOChecker  # noqa: E402


def _row(field_type, name="", appearance=""):
    return {"type": field_type, "name": name, "appearance": appearance}


def _questions(prefix, count):
    return [_row("text", f"{prefix}_q{i}") for i in range(count)]


def _check(rows):
    checker = SurveyCTOChecker("dummy.xlsx")
    checker.survey_df = pd.DataFrame(rows)
    result = checker.check_audio_audits()
    return checker, result


def _messages(items):
    return "\n".join(items)


# --- anchor validity ---------------------------------------------------------


def test_valid_field_anchored_segments_pass():
    rows = [
        _row("select_one yesno", "audio_consent"),
        _row("audio audit", "audit", "s=audio_consent;d=health_intro"),
        _row("audio audit", "audit_02", "s=health_intro;d=gps"),
        *_questions("a", 4),
        _row("note", "health_intro"),
        *_questions("b", 4),
        _row("geopoint", "gps"),
    ]
    checker, result = _check(rows)

    assert result
    assert not checker.errors
    assert not checker.warnings


def test_anchor_on_missing_field_is_error():
    rows = [
        _row("select_one yesno", "audio_consent"),
        _row("audio audit", "audit", "s=audio_consent;d=no_such_field"),
        _row("geopoint", "gps"),
    ]
    checker, result = _check(rows)

    assert not result
    assert "does not exist" in _messages(checker.errors)


def test_anchor_on_calculate_is_error():
    rows = [
        _row("calculate", "consent_flag"),
        _row("audio audit", "audit", "s=consent_flag;d=gps"),
        _row("geopoint", "gps"),
    ]
    checker, result = _check(rows)

    assert not result
    assert "must be visible fields" in _messages(checker.errors)


def test_anchor_inside_repeat_is_error():
    rows = [
        _row("select_one yesno", "audio_consent"),
        _row("audio audit", "audit", "s=audio_consent;d=med_name"),
        _row("begin repeat", "meds"),
        _row("text", "med_name"),
        _row("end repeat", "meds"),
        _row("geopoint", "gps"),
    ]
    checker, result = _check(rows)

    assert not result
    assert "inside a repeat group" in _messages(checker.errors)


def test_dollar_brace_anchor_syntax_is_error():
    rows = [
        _row("select_one yesno", "audio_consent"),
        _row("audio audit", "audit", "s=${audio_consent};d=${gps}"),
        _row("geopoint", "gps"),
    ]
    checker, result = _check(rows)

    assert not result
    assert "bare field name" in _messages(checker.errors)


def test_audit_row_inside_repeat_is_error():
    rows = [
        _row("select_one yesno", "audio_consent"),
        _row("begin repeat", "meds"),
        _row("audio audit", "audit", "d=60"),
        _row("text", "med_name"),
        _row("end repeat", "meds"),
    ]
    checker, result = _check(rows)

    assert not result
    assert "does not function inside a repeat" in _messages(checker.errors)


def test_time_based_anchors_are_not_treated_as_field_names():
    rows = [
        _row("select_one yesno", "audio_consent"),
        _row("audio audit", "audit", "p=25;s=0-300;d=120"),
        _row("geopoint", "gps"),
    ]
    checker, result = _check(rows)

    assert result
    assert not checker.errors
    assert not checker.warnings


def test_no_audio_audit_passes():
    checker, result = _check([_row("text", "q1"), _row("geopoint", "gps")])

    assert result
    assert not checker.errors
    assert not checker.warnings


# --- span and duration warnings ---------------------------------------------


def test_single_whole_survey_field_anchored_audit_warns():
    rows = [
        _row("select_one yesno", "audio_consent"),
        _row("audio audit", "audit", "s=audio_consent;d=gps"),
        *_questions("a", 20),
        _row("geopoint", "gps"),
    ]
    checker, result = _check(rows)

    assert result
    warnings = _messages(checker.warnings)
    assert "Edit Saved Form" in warnings
    assert "field-anchored segments" in warnings


def test_single_short_segment_does_not_warn():
    rows = [
        _row("select_one yesno", "audio_consent"),
        _row("audio audit", "audit", "s=audio_consent;d=section_b"),
        *_questions("a", 3),
        _row("note", "section_b"),
        *_questions("b", 20),
        _row("geopoint", "gps"),
    ]
    checker, result = _check(rows)

    assert result
    assert not checker.warnings


def test_several_segments_do_not_trigger_the_span_warning():
    rows = [
        _row("select_one yesno", "audio_consent"),
        _row("audio audit", "audit", "s=audio_consent;d=section_b"),
        _row("audio audit", "audit_02", "s=section_b;d=gps"),
        *_questions("a", 10),
        _row("note", "section_b"),
        *_questions("b", 10),
        _row("geopoint", "gps"),
    ]
    checker, result = _check(rows)

    assert result
    assert not checker.warnings


def test_long_time_based_audit_warns():
    rows = [
        _row("audio audit", "audit", "p=100;d=1800"),
        *_questions("a", 10),
        _row("geopoint", "gps"),
    ]
    checker, result = _check(rows)

    assert result
    assert "Edit Saved Form" in _messages(checker.warnings)


def test_short_time_based_audit_does_not_warn():
    rows = [
        _row("audio audit", "audit", "p=100;d=300"),
        *_questions("a", 10),
        _row("geopoint", "gps"),
    ]
    checker, result = _check(rows)

    assert result
    assert not checker.warnings


# --- p= combined with field anchors -----------------------------------------


def test_percent_with_field_anchor_warns():
    rows = [
        _row("select_one yesno", "audio_consent"),
        _row("audio audit", "audit", "p=100;s=audio_consent;d=section_b"),
        *_questions("a", 3),
        _row("note", "section_b"),
        *_questions("b", 20),
        _row("geopoint", "gps"),
    ]
    checker, result = _check(rows)

    assert result
    warnings = _messages(checker.warnings)
    assert "Drop `p=`" in warnings
    assert "mutually exclusive" in warnings


def test_field_anchor_without_percent_does_not_warn():
    rows = [
        _row("select_one yesno", "audio_consent"),
        _row("audio audit", "audit", "s=audio_consent;d=section_b"),
        *_questions("a", 3),
        _row("note", "section_b"),
        *_questions("b", 20),
        _row("geopoint", "gps"),
    ]
    checker, result = _check(rows)

    assert result
    assert not checker.warnings


# --- existing behaviour ------------------------------------------------------


def test_audio_audit_still_counts_as_respondent_input_for_timing():
    """check_timing_instrumentation rejects a survey start stamp placed after
    the audio audit row; that behaviour must survive the new check."""
    start_clock = "once(format-date-time(now(), '%Y-%m-%dT%H:%M:%S'))"
    rows = [
        {"type": "start", "name": "start", "calculation": "", "appearance": ""},
        {"type": "audio audit", "name": "audit", "calculation": "",
         "appearance": "s=audio_consent;d=gps"},
        {"type": "calculate_here", "name": "survey_start_elapsed_sec",
         "calculation": "once(duration())", "appearance": ""},
        {"type": "calculate_here", "name": "survey_start_time",
         "calculation": start_clock, "appearance": ""},
        {"type": "select_one yesno", "name": "audio_consent", "calculation": "",
         "appearance": ""},
        {"type": "geopoint", "name": "gps", "calculation": "", "appearance": ""},
        {"type": "end", "name": "end", "calculation": "", "appearance": ""},
    ]
    checker = SurveyCTOChecker("dummy.xlsx")
    checker.survey_df = pd.DataFrame(rows)
    checker.check_timing_instrumentation()

    assert any("must precede the first respondent-input field" in e
               for e in checker.errors)
