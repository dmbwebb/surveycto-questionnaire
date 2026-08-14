#!/usr/bin/env python3
"""Upload (or replace) a SurveyCTO form definition from the CLI.

Reverse-engineered from the SurveyCTO web console's POST to
``/console/forms/{groupId}/upload``. Authenticates from the macOS Keychain,
an explicit cookie, or the user's existing SurveyCTO session in Chrome.

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

    # One-time setup on each Mac (security prompts for the password itself)
    python3 scripts/surveycto_upload.py --setup-keychain \
        --server your-server.surveycto.com --username you@example.com

    # Verify unattended login without uploading anything
    python3 scripts/surveycto_upload.py --login-only \
        --server your-server.surveycto.com --username you@example.com

    # NEW: upload directly from a Google Sheet (auto-export to temp xlsx first)
    python3 scripts/surveycto_upload.py --from-gsheet <doc_id_or_pointer> \
        --update school_survey_k2_endline
    # `<doc_id_or_pointer>` can be:
    #   - a Drive doc_id like 1A9XwvDYIz...
    #   - a path to a .gsheet pointer file from Drive Desktop sync
    #   - a path to an actual .gsheet file in My Drive — the JSON stub is read
    #     to recover the doc_id.

PREREQUISITES
    - Recommended: run --setup-keychain once on each Mac. The password is
      entered directly into macOS's security tool and is never stored in this
      script, a shell variable, or a command-line argument.
    - Otherwise, be logged in to SurveyCTO in Chrome's default profile.
    - Python deps in the interpreter used to run this script (one-time):
        python3 -m pip install browser_cookie3 requests

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
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import browser_cookie3
import requests

DEFAULT_SERVER = os.environ.get("SURVEYCTO_SERVER")
DEFAULT_USERNAME = os.environ.get("SURVEYCTO_USERNAME")
KEYCHAIN_SERVICE_PREFIX = "surveycto-console"

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


def _new_session() -> requests.Session:
    """Return a consistently configured HTTP session."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "surveycto-upload-cli/1.1",
        "X-Requested-With": "XMLHttpRequest",
    })
    return session


def keychain_service(server: str) -> str:
    """Return the per-server macOS Keychain service name."""
    return f"{KEYCHAIN_SERVICE_PREFIX}:{server}"


def read_keychain_password(server: str, username: str) -> str | None:
    """Read a SurveyCTO password from macOS Keychain without displaying it."""
    command = [
        "/usr/bin/security",
        "find-generic-password",
        "-a", username,
        "-s", keychain_service(server),
        "-w",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None

    if result.returncode == 44:
        return None
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise UploadError(
            f"Could not read the SurveyCTO credential from macOS Keychain: "
            f"{detail}",
            exit_code=1,
        )
    return result.stdout.removesuffix("\n")


def store_keychain_password(server: str, username: str) -> None:
    """Prompt securely and add or replace a per-server Keychain credential.

    ``security -w`` is deliberately the final argument with no password value.
    The security tool therefore reads the secret directly from the terminal;
    it never appears in this process's arguments or environment.
    """
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        raise UploadError(
            "macOS does not permit writing the login Keychain from an SSH "
            "session. Run --setup-keychain once in a local Terminal or cmux "
            "pane on that Mac.",
            exit_code=1,
        )

    print(
        f"Saving SurveyCTO credential for {username} on {server}.\n"
        "Enter the password at the macOS Keychain prompt:",
        flush=True,
    )
    command = [
        "/usr/bin/security",
        "add-generic-password",
        "-U",
        "-a", username,
        "-s", keychain_service(server),
        "-l", f"SurveyCTO console: {server}",
        "-j", "Used by surveycto_upload.py for unattended console login",
        "-w",
    ]
    try:
        result = subprocess.run(command, check=False)
    except FileNotFoundError as exc:
        raise UploadError(
            "macOS Keychain's /usr/bin/security tool is unavailable.",
            exit_code=1,
        ) from exc
    if result.returncode != 0:
        raise UploadError(
            "The SurveyCTO Keychain credential was not saved.",
            exit_code=1,
        )


def login_with_password(
    server: str,
    username: str,
    password: str,
) -> requests.Session:
    """Create a SurveyCTO console session using local account credentials."""
    session = _new_session()
    login_page = session.get(f"https://{server}/index.html", timeout=20)
    login_page.raise_for_status()
    csrf_match = CSRF_RE.search(login_page.text)
    if not csrf_match:
        raise UploadError(
            "Could not find the login CSRF token. SurveyCTO's login page may "
            "have changed.",
            exit_code=2,
        )
    login_csrf = csrf_match.group(1)

    options_response = session.post(
        f"https://{server}/users/options",
        params={"t": int(time.time() * 1000)},
        data={"u": username},
        headers={"X-csrf-token": login_csrf},
        timeout=20,
    )
    options_response.raise_for_status()
    try:
        options = options_response.json()
    except ValueError as exc:
        raise UploadError(
            "SurveyCTO returned an unexpected response while checking the "
            "account's login method.",
            exit_code=2,
        ) from exc
    if options.get("requiresExternalAuth") is True:
        raise UploadError(
            "This SurveyCTO account requires external single sign-on, so a "
            "stored password cannot create an unattended console session.",
            exit_code=1,
        )

    login_response = session.post(
        f"https://{server}/login",
        params={"spring-security-redirect": "/"},
        data={
            "username": username,
            "password": password,
            "csrf_token": login_csrf,
            "timezoneOffsetMinutes": "0",
        },
        timeout=20,
    )
    login_response.raise_for_status()
    final_path = urlparse(login_response.url).path
    if (
        final_path in ("/index.html", "/login")
        or 'id="login-password"' in login_response.text
    ):
        raise UploadError(
            "SurveyCTO rejected the stored username or password. Run "
            "--setup-keychain again to replace the credential.",
            exit_code=1,
        )

    setattr(session, "_surveycto_auth_source", "keychain")
    return session


def load_session(
    server: str,
    cookie_string: str | None = None,
    username: str | None = None,
    auth_mode: str = "auto",
) -> requests.Session:
    """Build a requests Session authenticated to the given SurveyCTO server.

    Order of preference:
      1. Explicit ``cookie_string`` arg (e.g. "JSESSIONID=...; _uid=...")
      2. SURVEYCTO_COOKIE environment variable
      3. macOS Keychain credential when a username is supplied
      4. Chrome cookie jar (default profile, domain-filtered)

    ``auth_mode`` can force ``keychain`` or ``chrome``. In ``auto`` mode, a
    configured Keychain credential wins over Chrome so an expired browser
    session cannot break unattended uploads.
    """
    if auth_mode not in {"auto", "keychain", "chrome"}:
        raise ValueError(f"Unsupported authentication mode: {auth_mode}")

    session = _new_session()

    cookie_string = cookie_string or os.environ.get("SURVEYCTO_COOKIE")
    if cookie_string:
        for part in cookie_string.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                session.cookies.set(k, v, domain=server)
        setattr(session, "_surveycto_auth_source", "cookie")
        return session

    username = username or DEFAULT_USERNAME
    if auth_mode in {"auto", "keychain"}:
        if not username:
            if auth_mode == "keychain":
                raise UploadError(
                    "Keychain authentication needs --username or "
                    "$SURVEYCTO_USERNAME.",
                    exit_code=1,
                )
        else:
            password = read_keychain_password(server, username)
            if password is not None:
                return login_with_password(server, username, password)
            if auth_mode == "keychain":
                raise UploadError(
                    f"No SurveyCTO Keychain credential was found for {server}. "
                    "Run --setup-keychain once on this Mac.",
                    exit_code=1,
                )

    if auth_mode == "keychain":
        raise AssertionError(
            "keychain authentication should have returned or raised"
        )

    try:
        jar = browser_cookie3.chrome(domain_name=server)
    except Exception as e:
        raise UploadError(
            f"Failed to read Chrome cookies for {server}: {e}\n"
            "Run --setup-keychain (recommended), log into SurveyCTO in "
            "Chrome's default profile, or pass --cookie / set "
            "$SURVEYCTO_COOKIE.",
            exit_code=1,
        )

    cookies = list(jar)
    if not any(c.name == "JSESSIONID" for c in cookies):
        raise UploadError(
            f"No JSESSIONID cookie found for {server} in Chrome.\n"
            "Run --setup-keychain (recommended), or log into the SurveyCTO "
            "console in Chrome's default profile.",
            exit_code=1,
        )

    session.cookies = jar
    setattr(session, "_surveycto_auth_source", "chrome")
    return session


def fetch_csrf_token(session: requests.Session, server: str) -> str:
    """Scrape ``var csrfToken`` from main.html. Validates session is alive."""
    url = f"https://{server}/main.html"
    r = session.get(url, timeout=20)
    final_path = urlparse(r.url).path
    is_login_page = (
        final_path in ("/index.html", "/login")
        or 'id="login-password"' in r.text
    )
    if r.status_code in (401, 403) or is_login_page:
        source = getattr(session, "_surveycto_auth_source", "session")
        remedy = (
            "Run --setup-keychain again to replace the stored credential."
            if source == "keychain"
            else "Log in to the console in Chrome or run --setup-keychain."
        )
        raise UploadError(
            f"Authentication failed (HTTP {r.status_code}) — your SurveyCTO "
            f"{source} authentication is not valid. {remedy}",
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
    """Resolve a doc_id / .gsheet pointer / pointer path to a temp xlsx path.

    Lazy-imports gsheet_io so the xlsx-only path doesn't require
    google-api-python-client. The caller registers cleanup of the parent
    tempdir (see ``main`` — atexit hook).
    """
    import tempfile
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
        "--username", default=DEFAULT_USERNAME,
        help="SurveyCTO login email for Keychain authentication. Defaults to "
             "$SURVEYCTO_USERNAME.",
    )
    p.add_argument(
        "--auth", choices=("auto", "keychain", "chrome"), default="auto",
        help="Authentication source (default: auto, preferring Keychain when "
             "configured and otherwise using Chrome).",
    )
    p.add_argument(
        "--setup-keychain", action="store_true",
        help="Securely prompt for and store the SurveyCTO password in this "
             "Mac's Keychain, then verify the login. No form is required.",
    )
    p.add_argument(
        "--login-only", action="store_true",
        help="Verify console authentication without uploading a form.",
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

    if args.setup_keychain and not args.username:
        print(
            "error: --setup-keychain needs --username or "
            "$SURVEYCTO_USERNAME.",
            file=sys.stderr,
        )
        return 2

    if args.setup_keychain:
        try:
            store_keychain_password(args.server, args.username)
        except KeyboardInterrupt:
            print("\nKeychain setup cancelled; no credential was saved.",
                  file=sys.stderr)
            return 1
        except UploadError as e:
            print(f"error: {e}", file=sys.stderr)
            return e.exit_code
        args.auth = "keychain"

    # Resolve the input form: either a local xlsx OR a gsheet (auto-export).
    # Exactly one source must be provided.
    if args.from_gsheet and args.form_xlsx:
        print("error: pass form_xlsx OR --from-gsheet, not both.", file=sys.stderr)
        return 2
    if (
        not args.from_gsheet
        and not args.form_xlsx
        and not args.login_only
        and not args.setup_keychain
    ):
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
        # Best-effort cleanup at process exit so we don't leak the temp dir
        # whether upload succeeds, fails, or the user Ctrl-C's mid-flight.
        import atexit
        import shutil
        atexit.register(shutil.rmtree, gsheet_temp_path.parent,
                        ignore_errors=True)
        print(f"resolved gsheet {args.from_gsheet} -> {gsheet_temp_path}")

    if args.form_xlsx is not None and not args.form_xlsx.is_file():
        print(f"error: form xlsx not found: {args.form_xlsx}", file=sys.stderr)
        return 2
    for mf in args.media:
        if not mf.is_file():
            print(f"error: media file not found: {mf}", file=sys.stderr)
            return 2

    try:
        session = load_session(
            args.server,
            args.cookie,
            username=args.username,
            auth_mode=args.auth,
        )
        csrf = fetch_csrf_token(session, args.server)
    except UploadError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code
    except requests.RequestException as e:
        print(f"network error during SurveyCTO login: {e}", file=sys.stderr)
        return 2

    auth_source = getattr(session, "_surveycto_auth_source", "session")
    print(f"server:    {args.server}")
    print(f"auth:      {auth_source} (ok)")
    print(f"csrf:      {len(csrf)}-char token (ok)")

    if args.form_xlsx is None:
        print("OK: SurveyCTO console login verified; no form uploaded.")
        return 0

    action = f"replace '{args.update}'" if args.update else "create new form"
    print(f"action:    {action}")
    print(f"form xlsx: {args.form_xlsx} ({args.form_xlsx.stat().st_size} bytes)")
    for mf in args.media:
        print(f"media:     {mf} ({mf.stat().st_size} bytes)")
    print(f"draft:     {args.draft}")

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
