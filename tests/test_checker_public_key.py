"""Tests for SurveyCTOChecker.check_public_key_format."""

import base64
import sys
from pathlib import Path

import pandas as pd

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from surveycto_checker import SurveyCTOChecker  # noqa: E402


def checker_with_key(value):
    checker = SurveyCTOChecker("dummy.xlsx")
    checker.settings_df = pd.DataFrame([{"public_key": value}])
    return checker


def test_public_key_accepts_single_line_base64_der():
    key = base64.b64encode(b"\x30\x03\x02\x01\x01").decode()
    checker = checker_with_key(key)

    assert checker.check_public_key_format()
    assert not checker.errors


def test_public_key_rejects_pem_wrapper():
    checker = checker_with_key(
        "-----BEGIN PUBLIC KEY-----\nMAMCAQE=\n-----END PUBLIC KEY-----"
    )

    assert not checker.check_public_key_format()
    assert "without PEM headers" in "\n".join(checker.errors)


def test_public_key_rejects_invalid_base64():
    checker = checker_with_key("not!base64")

    assert not checker.check_public_key_format()
    assert "not valid base64" in "\n".join(checker.errors)


def test_public_key_allows_unencrypted_form():
    checker = checker_with_key("")

    assert checker.check_public_key_format()
    assert not checker.errors
