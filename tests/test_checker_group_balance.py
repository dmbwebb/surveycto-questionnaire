import sys
from pathlib import Path

import pandas as pd

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from surveycto_checker import SurveyCTOChecker  # noqa: E402


def _check(rows):
    checker = SurveyCTOChecker("dummy.xlsx")
    checker.survey_df = pd.DataFrame(rows, columns=["type", "name"])
    ok = checker.check_empty_groups()
    return ok, checker.errors


def test_balanced_groups_pass():
    ok, errors = _check([("begin group", "g"), ("text", "q"), ("end group", "g")])
    assert ok and not errors


def test_end_row_without_begin_is_an_error():
    # A build script that indexes rows by name can copy a group's END row where
    # its begin row belongs; SurveyCTO then rejects "Unmatched end statement".
    ok, errors = _check([("end group", "g"), ("text", "q"), ("end group", "g")])
    assert not ok
    assert any("no matching begin row" in e for e in errors)


def test_unclosed_group_is_an_error():
    ok, errors = _check([("begin group", "g"), ("text", "q")])
    assert not ok
    assert any("never closed" in e for e in errors)
