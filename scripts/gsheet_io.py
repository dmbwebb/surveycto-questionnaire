"""IO helpers for treating Google Sheets as the source of truth for XLSForm surveys.

Two halves:
  - read side: export gsheet -> temp xlsx so the existing surveycto_checker.py
    and surveycto_to_txt.py scripts can run unchanged.
  - utilities: read .gsheet pointer files (the JSON stub Drive desktop sync
    drops on the local FS); build authed services using the shared
    ~/.claude/.google token.

This is the canonical home of these helpers — the dev workspace at
~/Code/mada_gsheet_skill_dev/ was bootstrap scaffolding.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Reuse the shared OAuth bootstrapper from the google-sheets / google-drive skills.
sys.path.insert(0, str(Path.home() / ".claude" / ".google"))
from google_auth import build_service, load_credentials  # noqa: E402

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
GSHEET_MIME = "application/vnd.google-apps.spreadsheet"


def drive_service():
    creds = load_credentials()
    if creds is None:
        raise RuntimeError(
            "No Google credentials. Run: python3 ~/.claude/skills/google-drive/scripts/drive_manager.py auth"
        )
    return build_service("drive", "v3", creds)


def sheets_service():
    creds = load_credentials()
    if creds is None:
        raise RuntimeError(
            "No Google credentials. Run: python3 ~/.claude/skills/google-sheets/scripts/sheets_manager.py auth"
        )
    return build_service("sheets", "v4", creds)


def read_gsheet_pointer(path: Path | str) -> str:
    """Return the Drive ``doc_id`` from a local .gsheet pointer file.

    Drive Desktop drops a tiny JSON stub on the local FS for each Google Sheet:
        {"doc_id": "...", "email": "...", ...}
    """
    p = Path(path)
    data = json.loads(p.read_text())
    doc_id = data.get("doc_id")
    if not doc_id:
        raise ValueError(f"No doc_id found in {p} (not a .gsheet pointer?)")
    return doc_id


def resolve_to_doc_id(target: str) -> str:
    """Accept either a raw Drive doc_id or a path to a .gsheet pointer file.

    Lets CLIs accept user-friendly paths from Drive Desktop without forcing
    callers to handle the JSON stub themselves.
    """
    p = Path(target)
    if p.suffix == ".gsheet" or (p.exists() and p.is_file()):
        try:
            return read_gsheet_pointer(p)
        except Exception:
            pass
    # Heuristic: doc_ids are URL-safe and >= ~25 chars; paths usually contain '/'.
    if "/" in target or target.endswith(".gsheet"):
        # Looked like a path but couldn't resolve.
        raise ValueError(f"{target!r} looks like a path but isn't a valid .gsheet pointer")
    return target


def export_gsheet_to_xlsx(doc_id: str, dest: Path | str) -> Path:
    """Export a Google Sheet to xlsx via the Drive export endpoint.

    Formulas are evaluated server-side, so openpyxl's ``data_only=True`` will
    return cached values — important for the SurveyCTO ``settings.version``
    field, which is a NOW()-based formula.
    """
    from googleapiclient.http import MediaIoBaseDownload

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    request = drive_service().files().export_media(fileId=doc_id, mimeType=XLSX_MIME)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    dest.write_bytes(buf.getvalue())
    return dest


@contextmanager
def exported_xlsx(doc_id: str, *, suffix: str = ".xlsx") -> Iterator[Path]:
    """Context manager: export gsheet to a temp xlsx, yield path, cleanup on exit.

        with exported_xlsx(doc_id) as xlsx_path:
            wb = openpyxl.load_workbook(xlsx_path, data_only=True)
            ...
    """
    tmp = Path(tempfile.mkdtemp(prefix="gsheet_xlsx_"))
    try:
        path = tmp / f"export{suffix}"
        export_gsheet_to_xlsx(doc_id, path)
        yield path
    finally:
        for f in tmp.iterdir():
            f.unlink()
        tmp.rmdir()


def get_metadata(doc_id: str) -> dict:
    """Cheap metadata fetch (sheet titles, sheetIds, dimensions) via Sheets API."""
    return sheets_service().spreadsheets().get(spreadsheetId=doc_id).execute()


def sheet_id_for_tab(doc_id: str, tab_title: str) -> int:
    """Return the numeric sheetId for a tab title (used in batchUpdate range refs)."""
    md = get_metadata(doc_id)
    for s in md.get("sheets", []):
        if s["properties"]["title"] == tab_title:
            return s["properties"]["sheetId"]
    raise ValueError(f"No tab named '{tab_title}' in spreadsheet {doc_id}")


def get_drive_version(doc_id: str) -> int:
    """Read Drive's ``version`` counter for a file. Bumps on every modification.

    Useful as a cheap stale-data sentinel: read version V before planning,
    re-read after writing — if other writers slipped in, V advanced more than
    expected. Note: propagation can lag a couple of seconds after a Sheets API
    write, so use ``get_drive_modified_time`` for tight loops.
    """
    f = drive_service().files().get(fileId=doc_id, fields="version").execute()
    return int(f["version"])


def get_drive_modified_time(doc_id: str) -> str:
    """Read Drive's ``modifiedTime`` (RFC3339) for a file. Updates on every edit.

    More reliable than ``version`` for short test windows — but the value is a
    string timestamp, not an integer. Compare lexically.
    """
    f = drive_service().files().get(fileId=doc_id, fields="modifiedTime").execute()
    return f["modifiedTime"]
