import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from surveycto_checker import SurveyCTOChecker  # noqa: E402


def _errors(expression):
    checker = SurveyCTOChecker("dummy.xlsx")
    return checker._check_expression(expression)


def test_expression_checker_flags_duplicated_boolean_operator():
    errors = _errors("${a}=1 or  or ${b}=1")

    assert any("Duplicated boolean operator" in error for error in errors)


def test_expression_checker_flags_malformed_quoted_selected_code():
    errors = _errors("not(selected(${items}, '-997)) and not(selected(${items}, '-998))")

    assert any("Malformed quoted selected()" in error for error in errors)


def test_expression_checker_flags_single_malformed_quoted_selected_code():
    errors = _errors("selected(${items}, '-997)")

    assert any("Malformed quoted selected()" in error for error in errors)


def test_expression_checker_accepts_valid_quoted_selected_code():
    errors = _errors("not(selected(${items}, '-997')) and not(selected(${items}, '-998'))")

    assert not errors


def test_expression_checker_flags_double_equals():
    errors = _errors("${consent} == 1")

    assert any("not '=='" in error for error in errors)


def test_expression_checker_flags_unsupported_odk_functions():
    for expr in (
        "substring-after(${id}, '-')",
        "starts-with(${name}, 'Dr')",
        "contains(${label}, 'x')",
        "substring-before(${id}, '-')",
    ):
        errors = _errors(expr)
        assert any("Unsupported function" in error for error in errors), expr


def test_expression_checker_accepts_supported_lookalike_functions():
    errors = _errors("count-selected(${items}) >= 1 and selected-at(${items}, 0) = 'a'")

    assert not errors
