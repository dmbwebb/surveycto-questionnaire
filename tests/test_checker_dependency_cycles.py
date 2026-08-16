"""Tests for SurveyCTOChecker XPath dependency-cycle detection."""

from pathlib import Path
import sys

import pandas as pd

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from surveycto_checker import SurveyCTOChecker  # noqa: E402


def _checker(rows):
    checker = SurveyCTOChecker("dummy.xlsx")
    checker.survey_df = pd.DataFrame(rows)
    return checker


def test_detects_cycle_between_required_and_relevance():
    checker = _checker([
        {
            "type": "select_one decision",
            "name": "r_early_task_decision",
            "required": "${s8_disposition} = '' or ${s8_disposition} = 'continue'",
        },
        {
            "type": "select_one task_accept",
            "name": "r_task_accept",
            "relevance": "${r_early_task_decision} = 'continue'",
            "required": "${s8_disposition} = '' or ${s8_disposition} = 'continue'",
        },
        {
            "type": "select_one continue_stop",
            "name": "s8_disposition",
            "relevance": (
                "${r_early_task_decision} != 'stop_interview' and "
                "${r_task_accept} != 'stop_interview'"
            ),
            "required": "yes",
        },
    ])

    assert checker.check_upload_parser_blockers() is False
    assert any("XPath dependency cycle" in error for error in checker.errors)


def test_accepts_acyclic_required_and_relevance_dependencies():
    checker = _checker([
        {
            "type": "select_one decision",
            "name": "r_early_task_decision",
            "required": "yes",
        },
        {
            "type": "select_one task_accept",
            "name": "r_task_accept",
            "relevance": "${r_early_task_decision} = 'continue'",
            "required": "yes",
        },
        {
            "type": "select_one continue_stop",
            "name": "s8_disposition",
            "relevance": (
                "${r_early_task_decision} != 'stop_interview' and "
                "${r_task_accept} != 'stop_interview'"
            ),
            "required": "yes",
        },
    ])

    assert checker.check_upload_parser_blockers() is True
    assert checker.errors == []
