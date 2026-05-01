"""pytest fixtures for the surveycto-questionnaire skill (gsheet flow tests).

Tests run against real Google Drive. They need a valid token at
~/.claude/.google/token.json and local-only fixture IDs in tests/fixture_ids.json.
Copy tests/fixture_ids.example.json to tests/fixture_ids.json and fill in real
Drive IDs before running live tests.

Two fixture flavours:
  - persistent_fixture(name)  -> doc_id of a long-lived test sheet (read-only-ish)
  - ephemeral_fixture(name)   -> a fresh Drive *copy*, yielded for one test only,
                                  trashed at end of test. Use for any write/format/
                                  delete operations so tests don't trip each other.

Run from inside the skill dir:
    cd ~/.claude/skills/surveycto-questionnaire
    PYTHONPATH=scripts ~/.venvs/mada-gsheet-tests/bin/pytest tests/ -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent  # the skill dir
sys.path.insert(0, str(ROOT / "scripts"))

from gsheet_io import drive_service  # noqa: E402

FIXTURE_IDS_PATH = Path(__file__).parent / "fixture_ids.json"


@pytest.fixture(scope="session")
def fixture_ids() -> dict:
    if not FIXTURE_IDS_PATH.exists():
        raise FileNotFoundError(
            "Missing tests/fixture_ids.json. Copy tests/fixture_ids.example.json "
            "to tests/fixture_ids.json and fill in real Drive fixture IDs."
        )
    return json.loads(FIXTURE_IDS_PATH.read_text())


@pytest.fixture
def persistent_fixture(fixture_ids):
    """doc_id of a persistent test fixture. DO NOT mutate from tests; use
    ephemeral_fixture for that."""
    def _get(name: str) -> str:
        try:
            return fixture_ids["fixtures"][name]["doc_id"]
        except KeyError as e:
            raise KeyError(
                f"No fixture named '{name}'. Available: "
                f"{list(fixture_ids['fixtures'].keys())}"
            ) from e
    return _get


@pytest.fixture
def ephemeral_fixture(fixture_ids, request):
    """Yield doc_id of a fresh Drive copy of a fixture; trash on test exit.

    Cleanup trashes (does not permanently delete) so accidents are recoverable.
    """
    import uuid
    svc = drive_service()
    folder_id = fixture_ids["fixtures_folder_id"]
    created_ids: list[str] = []

    def _copy(name: str) -> str:
        try:
            source_id = fixture_ids["fixtures"][name]["doc_id"]
        except KeyError as e:
            raise KeyError(
                f"No fixture named '{name}'. Available: "
                f"{list(fixture_ids['fixtures'].keys())}"
            ) from e
        short = uuid.uuid4().hex[:8]
        new_name = f"{name}_EPHEMERAL_{request.node.name}_{short}"
        f = svc.files().copy(
            fileId=source_id,
            body={"name": new_name, "parents": [folder_id]},
            fields="id,name",
        ).execute()
        created_ids.append(f["id"])
        return f["id"]

    yield _copy

    for fid in created_ids:
        try:
            svc.files().update(fileId=fid, body={"trashed": True}).execute()
        except Exception as e:  # noqa: BLE001
            print(f"WARN: failed to trash ephemeral fixture {fid}: {e}",
                  file=sys.stderr)
