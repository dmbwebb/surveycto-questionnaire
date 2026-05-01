#!/usr/bin/env python3
"""Upload (or replace) a SurveyCTO form definition from the CLI.

Reverse-engineered from the SurveyCTO web console's POST to
``/console/forms/{groupId}/upload``. Authenticates by reading the user's
existing SurveyCTO session cookie from Chrome — no password handling.

USAGE
    # Upload a NEW form (appends to root group)
    python3 scripts/surveycto_upload.py path/to/form.xlsx

    # REPLACE an existing form, attaching media files (e.g. field plug-in zip)
    python3 scripts/surveycto_upload.py path/to/form.xlsx \
        --update ai_screening_main_v1 \
        --media path/to/plugin.fieldplugin.zip

    # Multiple media files
    python3 scripts/surveycto_upload.py form.xlsx -u my_form -m a.zip -m b.png

    # Server is required — pass --server or set $SURVEYCTO_SERVER
    python3 scripts/surveycto_upload.py form.xlsx --server your-server.surveycto.com

    # NEW: upload directly from a Google Sheet (auto-export to temp xlsx first)
    python3 scripts/surveycto_upload.py --from-gsheet <doc_id_or_pointer> \
        --update school_survey_k2_endline
    # `<doc_id_or_pointer>` can be:
    #   - a Drive doc_id like 1A9XwvDYIz...
    #   - a path to a .gsheet pointer file from Drive Desktop sync
    #   - a path to an actual .gsheet file in My Drive — the JSON stub is read
    #     to recover the doc_id.

PREREQUISITES
    - You must be logged in to the SurveyCTO web console in Chrome
      (default profile). The script reads JSESSIONID from Chrome's cookie store.
    - System Python deps (one-time):
        /usr/local/bin/python3 -m pip install --user browser_cookie3 requests

EXIT CODES
    0  success
    1  auth/cookie error
    2  network/HTTP error
    3  server-side rejection (form parse error, validation, etc.)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import browser_cookie3
import requests

DEFAULT_SERVER = os.environ.get("SURVEYCTO_SERVER")

# Multipart field names captured from the live web console request:
#   files_attach=on, keepMediaFiles=on, draft=false, authToken=,
#   updateExistingForm=<form_id or empty>,
#   locationContext=<json>,
#   form_def_file=<xlsx file>,
#   datafile=<media file>  (repeated for each media file)
#
# CSRF token is passed as a query string parameter (?csrf_token=...) and
# scraped from /main.html via `var csrfToken = "..."`.
CSRF_RE = re.compile(r'var\s+csrfToken\s*=\s*["\']([A-Za-z0-9_\-]+)["\']')


class UploadError(Exception):
    """Raised when the upload cannot be completed."""

    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code


def load_session(server: str, cookie_string: str | None = None) -> requests.Session:
    """Build a requests Session authenticated to the given SurveyCTO server.

    Order of preference:
      1. Explicit ``cookie_string`` arg (e.g. "JSESSIONID=...; _uid=...")
      2. SURVEYCTO_COOKIE environment variable
      3. Chrome cookie jar (default profile, domain-filtered)
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": "surveycto-upload-cli/1.0",
        "X-Requested-With": "XMLHttpRequest",
    })

    cookie_string = cookie_string or os.environ.get("SURVEYCTO_COOKIE")
    if cookie_string:
        for part in cookie_string.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                s.cookies.set(k, v, domain=server)
        return s

    try:
        jar = browser_cookie3.chrome(domain_name=server)
    except Exception as e:
        raise UploadError(
            f"Failed to read Chrome cookies for {server}: {e}\n"
            "Either log into SurveyCTO in Chrome (default profile), "
            "or pass --cookie 'JSESSIONID=...; _uid=...' / set $SURVEYCTO_COOKIE.",
            exit_code=1,
        )

    cookies = list(jar)
    if not any(c.name == "JSESSIONID" for c in cookies):
        raise UploadError(
            f"No JSESSIONID cookie found for {server} in Chrome.\n"
            "Make sure you're logged in to the SurveyCTO console in Chrome's "
            "default profile (not an Incognito or different-profile window).",
            exit_code=1,
        )

    s.cookies = jar
    return s


def fetch_csrf_token(session: requests.Session, server: str) -> str:
    """Scrape ``var csrfToken`` from main.html. Validates session is alive."""
    url = f"https://{server}/main.html"
    r = session.get(url, timeout=20)
    if r.status_code in (401, 403):
        raise UploadError(
            f"Authentication failed (HTTP {r.status_code}) — your SurveyCTO "
            "session has expired. Log in to the console in Chrome and retry.",
            exit_code=1,
        )
    r.raise_for_status()
    m = CSRF_RE.search(r.text)
    if not m:
        raise UploadError(
            "Could not find csrfToken in /main.html — the SurveyCTO web "
            "console layout may have changed; the regex needs updating.",
            exit_code=2,
        )
    return m.group(1)


def upload_form(
    session: requests.Session,
    server: str,
    csrf_token: str,
    form_xlsx: Path,
    update_form_id: str | None = None,
    media_files: list[Path] | None = None,
    parent_group_id: int = 1,
    draft: bool = False,
    keep_media_files: bool = True,
) -> dict:
    """POST the upload request and return the parsed JSON response."""
    url = f"https://{server}/console/forms/{parent_group_id}/upload"

    location_context = {
        "parentGroupId": parent_group_id,
        "siblingAbove": None,
        "siblingBelow": None,
    }

    data = {
        "files_attach": "on",
        "keepMediaFiles": "on" if keep_media_files else "",
        "draft": "true" if draft else "false",
        "authToken": "",
        "updateExistingForm": update_form_id or "",
        "locationContext": json.dumps(location_context, separators=(",", ":")),
    }

    files: list[tuple[str, tuple[str, bytes, str]]] = []
    files.append((
        "form_def_file",
        (
            form_xlsx.name,
            form_xlsx.read_bytes(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    ))
    for mf in media_files or []:
        files.append((
            "datafile",
            (mf.name, mf.read_bytes(), "application/octet-stream"),
        ))

    r = session.post(
        url,
        params={"csrf_token": csrf_token},
        data=data,
        files=files,
        timeout=120,
    )

    # The server returns 200 with a JSON body even on application errors:
    # {"code": 500, "message": "...", "responseObject": null}
    try:
        body = r.json()
    except ValueError:
        raise UploadError(
            f"Unexpected non-JSON response (HTTP {r.status_code}):\n"
            f"{r.text[:600]}",
            exit_code=2,
        )

    code = body.get("code")
    if code != 200:
        raise UploadError(
            f"SurveyCTO rejected the upload (code={code}):\n"
            f"  {body.get('message', '<no message>')}",
            exit_code=3,
        )
    return body


def _resolve_gsheet_to_temp_xlsx(target: str) -> Path:
    """Resolve a doc_id / .gsheet pointer / pointer path to a temp xlsx.

    Caller is responsible for cleanup (temp dir uses tempfile.mkdtemp).
    Imported lazily so the rest of the CLI works without google-api-python-client
    installed.
    """
    import tempfile
    # Local import — gsheet_io lives next to this script.
    sys.path.insert(0, str(Path(__file__).parent))
    from gsheet_io import export_gsheet_to_xlsx, resolve_to_doc_id

    doc_id = resolve_to_doc_id(target)
    tmpdir = Path(tempfile.mkdtemp(prefix="surveycto_upload_gsheet_"))
    dest = tmpdir / f"{doc_id}.xlsx"
    return export_gsheet_to_xlsx(doc_id, dest)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="surveycto_upload.py",
        description="Upload or replace a SurveyCTO form definition from the CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # form_xlsx becomes optional when --from-gsheet is supplied.
    p.add_argument("form_xlsx", type=Path, nargs="?",
                   help="Path to the form XLSX file (or omit and use --from-gsheet)")
    p.add_argument(
        "--from-gsheet", dest="from_gsheet", metavar="DOC_ID_OR_POINTER",
        help="Upload from a Google Sheet: pass a Drive doc_id or a path to a "
             ".gsheet pointer file. Internally exports the sheet to a temp xlsx, "
             "then runs the normal upload pipeline.",
    )
    p.add_argument(
        "-u", "--update",
        metavar="FORM_ID",
        help="Replace an existing form by id (e.g. ai_screening_main_v1). "
             "If omitted, uploads as a new form.",
    )
    p.add_argument(
        "-m", "--media",
        action="append", type=Path, default=[],
        metavar="FILE",
        help="Attach a media file (field plug-in .zip, image, csv, etc.). "
             "May be passed multiple times.",
    )
    p.add_argument(
        "--server", default=DEFAULT_SERVER,
        help="SurveyCTO server hostname (e.g. your-server.surveycto.com). "
             "Required — pass via --server or set $SURVEYCTO_SERVER.",
    )
    p.add_argument(
        "--parent-group-id", type=int, default=1,
        help="Group ID to upload into (default 1 = root group)",
    )
    p.add_argument(
        "--draft", action="store_true",
        help="Upload as draft (default: deploy immediately)",
    )
    p.add_argument(
        "--cookie", metavar="STRING",
        help="Override cookie source: 'JSESSIONID=...; _uid=...'. "
             "Otherwise reads $SURVEYCTO_COOKIE or Chrome's cookie jar.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Authenticate and print plan, but don't actually upload.",
    )
    args = p.parse_args(argv)

    if not args.server:
        print(
            "error: no SurveyCTO server specified. Pass --server <host> or set "
            "$SURVEYCTO_SERVER (e.g. your-server.surveycto.com).",
            file=sys.stderr,
        )
        return 2

    # Resolve the input form: either a local xlsx OR a gsheet (auto-export).
    # Exactly one source must be provided.
    if args.from_gsheet and args.form_xlsx:
        print("error: pass form_xlsx OR --from-gsheet, not both.", file=sys.stderr)
        return 2
    if not args.from_gsheet and not args.form_xlsx:
        print("error: provide form_xlsx or --from-gsheet <doc_id_or_pointer>.",
              file=sys.stderr)
        return 2

    gsheet_temp_path: Path | None = None
    if args.from_gsheet:
        try:
            gsheet_temp_path = _resolve_gsheet_to_temp_xlsx(args.from_gsheet)
        except Exception as e:
            print(f"error: could not export gsheet {args.from_gsheet!r}: {e}",
                  file=sys.stderr)
            return 2
        args.form_xlsx = gsheet_temp_path
        print(f"resolved gsheet {args.from_gsheet} -> {gsheet_temp_path}")

    if not args.form_xlsx.is_file():
        print(f"error: form xlsx not found: {args.form_xlsx}", file=sys.stderr)
        return 2
    for mf in args.media:
        if not mf.is_file():
            print(f"error: media file not found: {mf}", file=sys.stderr)
            return 2

    try:
        session = load_session(args.server, args.cookie)
        csrf = fetch_csrf_token(session, args.server)
    except UploadError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code

    action = f"replace '{args.update}'" if args.update else "create new form"
    print(f"server:    {args.server}")
    print(f"action:    {action}")
    print(f"form xlsx: {args.form_xlsx} ({args.form_xlsx.stat().st_size} bytes)")
    for mf in args.media:
        print(f"media:     {mf} ({mf.stat().st_size} bytes)")
    print(f"draft:     {args.draft}")
    print(f"csrf:      {len(csrf)}-char token (ok)")

    if args.dry_run:
        print("\n[dry-run] Skipping upload.")
        return 0

    print("\nUploading...")
    try:
        body = upload_form(
            session=session,
            server=args.server,
            csrf_token=csrf,
            form_xlsx=args.form_xlsx,
            update_form_id=args.update,
            media_files=args.media,
            parent_group_id=args.parent_group_id,
            draft=args.draft,
        )
    except UploadError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code
    except requests.RequestException as e:
        print(f"network error: {e}", file=sys.stderr)
        return 2

    print(f"OK: {body.get('message', '(no message)')}")
    if body.get("responseObject"):
        # Pretty-print useful pieces of the response (form id, version, etc.)
        ro = body["responseObject"]
        if isinstance(ro, dict):
            for k in ("formId", "id", "version", "deployedVersion", "title"):
                if k in ro:
                    print(f"  {k}: {ro[k]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
