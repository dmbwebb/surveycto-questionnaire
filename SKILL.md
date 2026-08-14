---
name: surveycto-questionnaire
description: "Create, edit, validate, inspect, convert, and upload XLSForm surveys for SurveyCTO, ODK, or Kobo. Supports both .xlsx files and Google Sheets — for gsheet-backed forms, reads via auto-export to a temp xlsx and writes directly to the sheet via the Sheets API."
---
# XLSForm Survey Design and Excel Editing

## Working with Google Sheets-backed forms

When the form lives as a Google Sheet (the K2 baseline/midline/endline pattern), the canonical workflow is:

- **Reads** (validate, inspect, convert-to-text): export the gsheet to a local temp xlsx via `gsheet_io.exported_xlsx(doc_id)`, then run the existing `surveycto_checker.py` / `surveycto_to_txt.py` against it. **`exported_xlsx` is a context manager**, not a function returning a path — use `with gsheet_io.exported_xlsx(doc_id) as xlsx:` and pass `xlsx` (a path string) into openpyxl/checker calls inside the block. Binding the bare call (`xlsx = gsheet_io.exported_xlsx(doc_id)`) yields a `_GeneratorContextManager` and any path-style use raises `TypeError: expected str, bytes or os.PathLike object, not _GeneratorContextManager`. The gsheet stays the source of truth; the xlsx is a transient build artifact. Drive's xlsx export materialises formulas (so `settings.version` cells with `NOW()`-based formulas come out evaluated), so no `recalc_excel.sh` step is needed.
- **Writes** (edit a label, rename a variable, add a choice list, mark a translation as red): use `gsheet_edit.py` against the live Sheet via the Sheets API. Do not download → edit xlsx → re-upload — co-authors editing simultaneously would lose work.
- **Upload to SurveyCTO**: use `surveycto_upload.py --from-gsheet <doc_id_or_pointer>`. It exports the gsheet to a temp xlsx and runs the normal CSRF/cookie upload pipeline. Works alongside `--update <form_id>` and `--media`.
- **Media-only fixes still need a greater form version.** SurveyCTO rejects replacement uploads when `settings.version` is not lexically greater than the deployed version, even if the XLSForm logic is unchanged and only `--media` changed. Before live media redeploys, export/read the `settings` tab and compare against `/forms/{form_id}/files`; bump a static version cell or ensure the formula export resolves above the deployed version.

`gsheet_io.resolve_to_doc_id` accepts either a raw Drive `doc_id` or a path to a `.gsheet` pointer file (the JSON stub Drive Desktop drops on the local FS), so CLI users can pass familiar paths.

### Edit primitives in `gsheet_edit.py`

| Function | Purpose |
|---|---|
| `open_tab(doc_id, tab_title)` | Cache header layout + sheet_id for a tab |
| `find_row_by_value(tab, header, value)` | Find row number by `name` (or any column) |
| `get_cell` / `update_cell` | Single-cell read/write (USER_ENTERED parsing) |
| `update_cell_checked(tab, row, header, expected_old, new_value)` | Compare-and-swap: writes only if current value matches expected — best-effort guard against concurrent edits. Signature: `update_cell_checked(tab, row, header, expected_old, new_value)` (all positional, no kwargs). Returns `None` on success and raises `StaleDataError` if current value does not match expected. |
| `batch_update_cells(tab, edits)` | Write many cells in one API call; `edits` is `[(row, header, value), ...]`. No CAS — verify preconditions via a single bulk read. Retries on 429. |
| `bulk_set_column(tab, rows, header, value)` | One-call sugar over `batch_update_cells` for setting the same value across many rows of one column (the "disable a module" pattern). |
| `append_row(tab, row_dict)` | Append to bottom; returns landed row |
| `insert_row_at(tab, position, row_dict)` | Insert mid-tab, shifting rows down — preserves group structure |
| `delete_row(tab, row)` | Remove a row (rejects header row 1) |
| `rename_variable(tab, old, new)` | Rename in the `name` column AND every `${...}` reference in relevance/constraint/calculation/label/etc. — single-batch round-trip |
| `add_choice_list(doc_id, list_name, choices)` | Append a choice list; auto-detects whether the choices tab uses `name` or `value` (XLSForm allows either) |
| `set_text_color` / `get_text_color` | Foreground (text) color — for translation-status semantics |
| `add_cell_comment` / `add_translation_comment` | Fail-loud placeholders for true Google Sheets cell comments with @mentions. Public Google APIs cannot create UI-backed cell comments reliably; do not substitute notes or unanchored Drive comments. |
| `gsheet_io.get_drive_modified_time` / `get_drive_version` | Whole-file change sentinels (note: propagation can lag 30s+ — use `update_cell_checked` for tight loops) |

### Concurrency, rate limits, row shifts

When the user (or another concurrent agent) is editing the gsheet alongside you, three things bite:

- **Sheets API rate limit is 60 reads/min/user.** Per-cell `update_cell_checked` does ~2 calls per cell (one read, one write); 30+ cells in a tight loop will hit HTTP 429. Use `batch_update_cells(tab, edits)` (or the `bulk_set_column` shortcut) for bulk writes — one API call covers all rows, with built-in 429 retry. Read once via the exported xlsx (no API hit) to confirm preconditions, then batch-write.
- **Locating rows by `(name, type)` pair: never write per-row scanners that call `get_cell` in a loop.** A naive `find_row_by_nametype` that calls `get_cell(tab, r, 'name')` and `get_cell(tab, r, 'type')` for every row in a 1700-row form makes ~3400 API calls per lookup → instantly hits 429 or just hangs for many minutes. Instead: re-export the gsheet (`gsheet_io.exported_xlsx(doc_id)`), find target rows in the local xlsx with openpyxl (zero API calls), then build a single `batch_update_cells` write. Pattern is in the `surveycto-questionnaire` skill's "Locating multiple rows by `(name, type)` pair" example.
- **Row numbers shift under you.** If you scan via `gsheet_io.exported_xlsx(doc_id)` and then write minutes later, a concurrent insertion above your target rows will shift everything down, and your row-number-keyed writes go to the wrong rows. The cell values *do* move with the content under insert/delete, so writes you already made stay correct — but verifications using stale row numbers will look broken even when the live state is fine. For risky write batches, prefer `find_row_by_value(tab, 'name', '<field_name>')` over hard-coded row numbers, and verify by name (not row) afterward.
- **Disabling a field requires updating its callers.** Before setting `disabled = yes`, grep the survey for `${field_name}` references in `relevance`, `constraint`, `calculation`, `label`, `choice_filter`, `repeat_count`, `required` (and the choices sheet). Either drop those refs or update the formulas — leaving them produces "field reference to non-existent field" errors at upload. Same applies to renames (already handled by `rename_variable`, but only it).
- **K2 forms with formula-based `name` cells (e.g. `="duration_"&B47`) trigger checker false-positive "blank/missing name" errors after an openpyxl save.** openpyxl writes formulas without cached values; the checker reads via pandas BEFORE the version-cell Excel recalc fires (recalc is the last check), so pandas sees these cells as NaN. To verify they're false positives, reload with `openpyxl.load_workbook(path, data_only=True)` AFTER running the checker — if the names resolve to real values like `duration_intro`, the form will upload fine. Common in K2 bracelet/lab/girls forms where `="duration_"&Bxx` appears at section ends. Don't try to "fix" the names; they're correct. Future Claudes: don't waste cycles trying to "fix" formula-based name false-positives — reload the checker output with `data_only=True` to confirm the formulas resolve, then ignore the error.
- **Disabling a whole module needs `disabled=yes` on EVERY row, not just the `begin group`.** A group-level relevance like `0=1` hides the questions on the device but the checker still treats the rows as active and flags duplicate names if you've added replacement questions elsewhere. Use `bulk_set_column(tab, rows, 'disabled', 'yes')` over the row range. Reads via the exported xlsx skip these rows correctly once disabled.
- **`find_row_by_value` returns the FIRST match, including disabled duplicates.** Survey forms commonly have an old disabled `pulldata` calculate at row ~150 and the active version at row ~400 with the same `name` AND same `type` (e.g. K2 endline mothers form has two `calculate id_exist` rows; three `note fin` rows). Filtering by `(name, type)` alone is therefore *not enough* — you also need to check `disabled`. Scan A:P (the disabled column is at index 15 in K2 forms) in a single `values.get` call and skip rows where `disabled.lower() == 'yes'`. That's also the rate-limit-friendly way to locate many anchors at once. Pattern:
  ```python
  svc = ge.sheets_service()
  res = svc.spreadsheets().values().get(
      spreadsheetId=tab.doc_id, range=f"'{tab.tab_title}'!A1:P"
  ).execute()
  values = res.get('values', [])
  for i, row in enumerate(values, start=1):
      if i == 1: continue  # header
      typ = row[0] if len(row) > 0 else ''
      nm  = row[1] if len(row) > 1 else ''
      disabled = row[15] if len(row) > 15 else ''
      if str(disabled).lower() == 'yes':
          continue
      # match (nm, typ) here
  ```
- **Inserting multiple rows at the same position: insert in REVERSE order to get the final order you want.** `insert_row_at(tab, 537, X)` followed by `insert_row_at(tab, 537, Y)` ends up with `Y` at row 537 and `X` at 538, because the second insert pushes the first one down. To land `[X, Y, Z]` at row 537+, call `insert_row_at(tab, 537, Z)`, then `Y`, then `X`. (Note: `insert_row_at` returns `None`; the landed row is the `position_row` arg you passed in.)
- **Appending choice rows may not land at the bottom of the visible choice list.** Google Sheets `values.append` appends to the API-detected data table, which can be shorter than the visible sheet if there are blank rows or separated blocks; `append_row()` can therefore land new choices near the top of `choices`. For choice-list additions that must stay adjacent to an existing list, prefer `insert_row_at()`/`moveDimension` near that list, then re-export and verify the final row order.
- **Bulk row deletion (10+ rows): use `batchUpdate` with `deleteDimension` requests, bottom-up.** `delete_row` is single-row and will rate-limit on large jobs. For multiple contiguous ranges, send one `batchUpdate` with multiple `deleteDimension` requests, ordered bottom-to-top so earlier deletions don't shift later ranges. Pattern:
  ```python
  svc = gio.sheets_service()
  reqs = []
  for start, end in [(395, 427), (189, 219), (157, 187)]:  # bottom-up
      reqs.append({"deleteDimension": {"range": {
          "sheetId": tab.sheet_id, "dimension": "ROWS",
          "startIndex": start - 1, "endIndex": end,  # API is 0-indexed, half-open
      }}})
  svc.spreadsheets().batchUpdate(spreadsheetId=tab.doc_id, body={"requests": reqs}).execute()
  ```
- **Moving a contiguous block of rows: use `batchUpdate` with one `moveDimension` request.** `gsheet_edit.py` has no built-in "move rows" primitive, but the Sheets API does it atomically via `moveDimension`. Use this for module reorderings (e.g. shoving a contamination-prone module to the end of a 1500-row form) — far safer than read+insert_row_at-loop+delete_row, which is many API calls and risks partial state. Source range is 0-indexed half-open. `destinationIndex` is interpreted in the **original** (pre-move) coordinate space — it's the index *before which* the moved rows will appear, so to land moved rows just before original 1-indexed row `R`, pass `destinationIndex = R - 1`. Pattern:
  ```python
  svc = gio.sheets_service()
  req = {"requests": [{"moveDimension": {
      "source": {
          "sheetId": tab.sheet_id, "dimension": "ROWS",
          "startIndex": src_first - 1,  # 1-indexed → 0-indexed
          "endIndex": src_last,          # 1-indexed inclusive → 0-indexed exclusive
      },
      "destinationIndex": dest_row - 1,  # before original 1-indexed row dest_row
  }}]}
  svc.spreadsheets().batchUpdate(spreadsheetId=tab.doc_id, body=req).execute()
  ```
  After the move, total row count is unchanged and cells in the moved range follow their content (so any references to fields inside the moved block remain valid). `${...}` references work by name, not by row position, so XLSForm logic is unaffected by the reorder. ⚠️ **On a deployed form with data, only reorder within the same group nesting.** SurveyCTO tracks each field by its full path including enclosing groups — moving fields INTO or OUT OF a group/repeat, renaming a group, or changing repeat enclosure between versions breaks the linkage to already-collected data (exports treat it as a different field). Whole-module moves that keep every field inside its original group are safe.
- **Dynamic choice lists referencing disabled roster fields produce 180+ "broken ref" errors even with no enabled consumer.** If a `pulldata`-style choice list (`list_hhmember`, etc.) has labels like `${hhmember1}` and the source `hhmember*` calculates are disabled, the checker flags every row in the choice list — even when the picker `select_one list_hhmember` is itself disabled. The choice list rows aren't auto-pruned. Fix: delete the dead choice-list rows outright (use the bulk-delete pattern above). **A single disabled roster can feed multiple choice lists.** AI Health pilot baseline had 5 lists (`household_members`, `companion_planning`, `household_members_hospital`, `household_members_decision`, `household_members_dm_select`) all referencing `${adult_label_1..15}` from the same disabled `adult_roster` repeat. After the first delete-and-recheck the checker still surfaced the others. Don't fix one and assume done — grep ALL cells in the choices sheet for `${<broken_ref>` patterns, find every `list_name` that hits, then bulk-delete in one pass.
- **Missing type-based conditional formatting in a gsheet export can be repaired from a healthy peer form.** If `surveycto_checker.py` reports missing `begin group`/`text`/`integer`/etc. rules on a Google Sheet-backed form, copy only the healthy survey tab's conditional-format rules whose formulas reference `$A1` or `$P1`, rewrite each range to the target survey sheet's `sheetId`/grid size, and add them with Sheets API `addConditionalFormatRule`. Then re-export and rerun the checker; this is a maintainability fix, not a SurveyCTO logic change.

### Translation status convention (K2)

K2 forms use an explicit `a_traduire` column in the survey/choices tabs to flag rows that still need Malagasy translation. **That is the canonical mechanism for this project.** Don't add red-text-on-Malagasy as a parallel channel — `a_traduire` is enough.

(The Hindi-style red-text rule documented elsewhere in this SKILL is for projects that don't have an `a_traduire` column. It still works mechanically — the `set_text_color` primitive is in place — but for K2 forms, prefer the column.)

### Google Sheets cell comments / @mentions

True Google Sheets cell comments with @mentions are **not currently automatable through the public Google APIs**. Existing UI-created comments appear through Drive `comments().list()` as anchored comments with `htmlContent` such as `@<a href="mailto:...">...</a>`, but Google documents that developer-defined Drive comment anchors are saved while Google Workspace editor apps treat them as unanchored. The Sheets API exposes cell `note`, not cell comments, and notes do not notify tagged users.

For that reason, `gsheet_edit.add_cell_comment(...)` and `add_translation_comment(...)` exist as fail-loud helpers: they validate the target shape and then raise `UnsupportedCellCommentsError` rather than creating a misleading note or file-level comment. The project default translation mention is `@elianeralison@gmail.com traduction à vérifier stp`.

### Tests

Live Google Sheets tests live in `tests/` and run against persistent Drive copies listed in local-only `tests/fixture_ids.json`. Start from `tests/fixture_ids.example.json`; the real fixture file is gitignored because it contains private Drive IDs. Each destructive test makes its own ephemeral copy and trashes it on exit, so fixtures themselves stay clean.

```bash
cd ~/.claude/skills/surveycto-questionnaire
cp tests/fixture_ids.example.json tests/fixture_ids.json  # then fill in real Drive IDs
PYTHONPATH=scripts ~/.venvs/mada-gsheet-tests/bin/pytest tests/ -v -c tests/pytest.ini
```

The `mada-gsheet-tests` venv exists only on some Macs; where it's missing, `PYTHONPATH=scripts ~/.venvs/lifecoach/bin/python3 -m pytest` works (has pytest + pandas + openpyxl).

The live-gsheet suite covers read flow, write flow, multi-tab edits, insert-at-position, concurrent-edit detection, delete, foreground color round-trip, batch writes, and fail-loud cell comment behavior. The `tests/test_checker_*.py` files (expression syntax incl. ODK-isms, impossible literals, public key) are offline — they need no fixtures and run on any Mac.



## Setup (one-time)

The example commands below reference scripts via `$SURVEYCTO_SKILL_DIR`. Set this env var once in your shell profile (`~/.zshrc`, `~/.bashrc`, etc.) so the examples work from any working directory:

```bash
export SURVEYCTO_SKILL_DIR="$HOME/.claude/skills/surveycto-questionnaire"
```

For the upload script, also set your SurveyCTO server host (no default is shipped):

```bash
export SURVEYCTO_SERVER="your-server.surveycto.com"
```

## ⚠️ MANDATORY: Validate After Every Edit

**After ANY edit to an XLSForm file, you MUST run the checker to validate the form:**

```bash
python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_checker.py" <path_to_xlsform.xlsx>
```

**This is not optional.** The checker catches errors that will break the form on SurveyCTO:
- References to non-existent fields (typos in `${field_name}`)
- Undefined choice lists
- Expression syntax errors (unbalanced parentheses, unclosed quotes)
- Duplicate field names
- Missing "other specify" fields
- select_multiple exclusive option constraints
- Missing Hindi translations
- Formatting/conditional formatting preservation
- And more

**Run it iteratively throughout your editing session.** After each major edit (adding questions, renaming variables, changing logic), run the checker before moving on to the next edit. This catches errors early — don't batch all edits and check only at the end. Fix errors, re-run, fix more, re-run — until you get zero errors. Warnings are informational but errors must be resolved.

Note: when `settings.version` holds an unevaluated formula (no cached value — e.g. any xlsx freshly saved by openpyxl), the checker auto-launches Excel via `recalc_excel.sh` to evaluate it, which steals keyboard focus (see upload gotchas). To check a form without triggering Excel, run the checker on a scratch copy whose version cell is overwritten with a static string.

For encrypted XLSForms, `settings.public_key` must contain only the
single-line base64-encoded DER payload. Do not paste the PEM header, footer,
or line breaks into the cell. The checker rejects PEM-wrapped or malformed
keys because SurveyCTO can deploy such a form but fail when the first
submission is finalized.

### Checker Validations

The checker (`surveycto_checker.py`) performs these checks:

| Check | Type | What it catches |
|-------|------|-----------------|
| Required columns | Error | Missing `type`, `name` columns |
| Blank/missing names | Error | Rows with a type but blank/whitespace-only name |
| Duplicate names | Error | Two fields with the same name |
| Empty groups | Error | Groups/repeats with no enabled children (all disabled) |
| Expression syntax | Error | Unbalanced parentheses, unclosed `${}`, unclosed quotes |
| ODK-isms | Error | `==` equality; unsupported functions `starts-with()`, `contains()`, `substring-before/after()` |
| Upload parser blockers | Error | `#ERROR!` in parsed columns, expression-only blank rows, self-references |
| Field references | Error | `${field_name}` pointing to non-existent fields |
| Choice list references | Error | `select_one`/`select_multiple` referencing undefined lists |
| Choices field references | Error | `${field}` in choice labels pointing to non-existent fields |
| Calculate fields | Error | Calculate type with empty calculation formula |
| Other specify fields | Warning | 'Other (specify)' choice without follow-up text field |
| select_multiple other | Warning | select_multiple with 'other' but no specify field |
| Exclusive options | Warning | select_multiple missing constraints for exclusive options (-97, -98) |
| Impossible literal values | Error | `${var}=X` / `selected(${var}, X)` where `X` is not a valid name/value in `var`'s choice list (silent dead logic) |
| Required fields | Warning | Questions without `required=yes` |
| Typos | Warning | Common misspellings in field names/labels |
| Constraint messages | Warning | Fields with constraints but no error message |
| Integer constraints | Warning | Integer fields without range validation |
| Numeric refuse (-999) | Warning | Numeric fields without -999 refuse option |
| Hindi translations | Warning | Questions missing `label:Hindi` |
| Naming conventions | Warning | camelCase, dots, spaces, uppercase in field names |
| Conditional formatting | Error | Type-based color coding rules removed from survey sheet |
| Cell formatting | Warning | Red text (unverified translations) removed |
| Encryption public key | Error | PEM-wrapped, whitespace-containing, invalid-base64, or non-DER `settings.public_key` |
| Version formula | Warning | Settings version formula not evaluated |

---

## Convert Survey to Text

Convert XLSForm surveys to human-readable text format using the CLI tool.

**Requirements:** Python 3 with openpyxl (`pip install openpyxl`)

```bash
# Basic usage - creates survey_questions.txt in same directory
python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_to_txt.py" survey.xlsx

# Specify output file
python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_to_txt.py" survey.xlsx output.txt

# Exclude variable names from output
python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_to_txt.py" survey.xlsx --no-names

# Exclude relevance conditions
python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_to_txt.py" survey.xlsx --no-relevance

# Exclude choice options
python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_to_txt.py" survey.xlsx --no-choices

# Keep HTML tags in labels (default strips them)
python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_to_txt.py" survey.xlsx --keep-html
```

Output format:
- Group headers become `## Section Name`
- Repeat groups become `### [REPEAT] Group Name`
- Questions become `• [variable_name] (If: relevance): Question text`
- Select questions show choices indented below: `    - Choice label`
- Calculate fields show: `• [variable_name] (calculate): formula`
- Notes ending with `_header` become section headers
- Disabled fields and duration calculations are skipped

**⚠️ When to use this tool vs. reading Excel directly:**

| Use Text Conversion | Read Excel Directly |
|---------------------|---------------------|
| Getting an overall picture of survey structure | Planning edits to the survey |
| Quick overview of questions and flow | Understanding complex skip logic |
| Sharing survey content with non-technical users | Debugging constraint or calculation issues |
| Reviewing question wording | Finding all references to a variable |

**The text conversion simplifies and omits details.** It does not include:
- Full constraint expressions and messages
- All calculation formulas
- Choice filter logic
- Appearance settings
- Hints and other metadata columns
- Cell formatting (e.g., red text for unverified translations)

**When editing surveys, ALWAYS read the actual Excel file** to understand the complete logic before making changes. The text view gives you the "what" but not the full "how" of the survey mechanics.

---

## Upload Form to SurveyCTO (CLI)

`scripts/surveycto_upload.py` uploads or replaces a SurveyCTO form definition directly from the terminal — no web UI, no file picker, no browser automation. **Always prefer this over the SurveyCTO web console upload dialog or the `surveycto-tester` Chrome extension flow.** It's faster, scriptable, and avoids the file-picker dead end that blocks Chrome automation.

### How it works (one-time setup)

1. Be logged in to the target SurveyCTO console in **Chrome's default profile** (the script reads `JSESSIONID` from Chrome's cookie store via `browser_cookie3`).
2. Install deps once for the system Python:

```bash
/usr/local/bin/python3 -m pip install --user browser_cookie3 requests
```

⚠️ **Always invoke `surveycto_upload.py` with `/usr/local/bin/python3` explicitly, not bare `python3`.** On many Macs `python3` resolves to homebrew (`/opt/homebrew/bin/python3` or similar) which does NOT have `browser_cookie3` installed — you'll get `ModuleNotFoundError: No module named 'browser_cookie3'` even though the install command above succeeded. The dep install path and the invocation path must match.

No password handling, no API token, no `--cookie` flag required as long as the user is logged in to Chrome. (If they aren't, fall back to `--cookie 'JSESSIONID=...; _uid=...'` or `$SURVEYCTO_COOKIE`.)

### Usage

```bash
# Replace an existing form (most common case — pair with media files)
/usr/local/bin/python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_upload.py" \
    --update ai_screening_main_v1 \
    --media path/to/plugin.fieldplugin.zip \
    path/to/ai_screening_main_v1.xlsx

# Upload a NEW form (appends to root group)
/usr/local/bin/python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_upload.py" \
    path/to/new_form.xlsx

# Multiple media files
/usr/local/bin/python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_upload.py" \
    -u my_form -m a.zip -m b.png -m choices.csv path/to/form.xlsx

# Override server for a single run (normally read from $SURVEYCTO_SERVER)
/usr/local/bin/python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_upload.py" \
    --server other-server.surveycto.com \
    path/to/form.xlsx

# Dry run (auth + csrf check + plan, no upload)
/usr/local/bin/python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_upload.py" \
    --dry-run path/to/form.xlsx
```

### Flags

| Flag | Default | Description |
|---|---|---|
| `form_xlsx` (positional) | — | Path to the form `.xlsx` file (required) |
| `-u`, `--update FORM_ID` | new form | Replace an existing form (e.g. `ai_screening_main_v1`) |
| `-m`, `--media FILE` | none | Attach a media file. Pass repeatedly for multiple files. |
| `--server HOST` | `$SURVEYCTO_SERVER` (required if env var unset) | SurveyCTO server hostname |
| `--parent-group-id N` | `1` | Group ID to upload into (`1` = root) |
| `--draft` | off | Upload as draft instead of deploying immediately |
| `--cookie 'JSESSIONID=...; _uid=...'` | Chrome cookie jar | Override cookie source |
| `--dry-run` | off | Authenticate, scrape CSRF, print plan; skip the actual upload |

### Exit codes

- `0` — success
- `1` — auth/cookie error (not logged in, JSESSIONID missing, session expired)
- `2` — network/HTTP error
- `3` — server-side rejection (form parse error, version-bump rule, validation, etc.) — the server's error message is printed verbatim

### Reverse-engineering notes (so you can fix it if SurveyCTO changes)

The web console submits a `POST /console/forms/{groupId}/upload?csrf_token={32-char-token}` with a `multipart/form-data` body containing:

| Field | Value |
|---|---|
| `files_attach` | `on` |
| `keepMediaFiles` | `on` |
| `draft` | `false` (or `true`) |
| `authToken` | (empty) |
| `updateExistingForm` | form id (when replacing) or empty |
| `locationContext` | JSON: `{"parentGroupId":1,"siblingAbove":null,"siblingBelow":null}` |
| `form_def_file` | the `.xlsx` file part |
| `datafile` | media file part — repeat once per attachment |

Header: `X-Requested-With: XMLHttpRequest`. Auth: standard Java servlet `JSESSIONID` cookie. The CSRF token is scraped from `var csrfToken = "..."` in `/main.html`.

### Common gotchas

- **Version bump rule.** SurveyCTO refuses to replace a form unless `settings.version` in the new xlsx is **lexically greater** than the deployed version. Versions are also capped at **10 digits** ("must be a number less than or equal to 9999999999") — `YYYYMMDDHHMM` is rejected; for same-day redeploys use `YYYYMMDD` + a 2-digit quarter-hour serial (`(hour*60+min)//15`, 00–95). The CLI passes the server's exact error message through (e.g. `you can't change the form attachments without also increasing the version number ... lexically greater than the previous one (2026040705)`). Bump the version in the `settings` sheet of the xlsx and retry. The convention used in this project is `YYYYMMDDNN` (e.g. `2026040801`). If you wrote the version as a `NOW()`-based formula, run `recalc_excel.sh` to evaluate it before uploading — SurveyCTO does not evaluate Excel formulas.
- **Field-plugin manifest version bump rule.** The same lexically-greater rule applies independently to attached field-plugin zips. SurveyCTO scans the `version` field in `manifest.json` inside the `.fieldplugin.zip` and rejects with `The version of the field plug-in "<name>" (<old>) should be greater than <old>` if it isn't bumped. Bump `manifest.json::version` (project convention `YYYY.MMDD.HHMM`, e.g. `2026.508.1130`) before rebuilding the zip — re-uploading an unchanged-version plugin under the same form fails even if the form xlsx version is fresh.
- **`#ERROR!` in upload-sensitive columns.** Google Sheets formulas can leave `#ERROR!` in columns SurveyCTO parses (`required`, `relevance`, `calculation`, `constraint`, etc.); SurveyCTO rejects these with messages like `Invalid 'required' expression [#ERROR!]`. Clear the bad cell in the live gsheet and retry. Ignore `#ERROR!` in unnamed helper columns unless it feeds a parsed column.
- **Session expired.** If you see `error: Authentication failed (HTTP 403)`, log into the SurveyCTO console in Chrome again (the JSESSIONID has expired) and retry.
- **Check the session EARLY in multi-step deploys.** `load_session(server)` + `GET /forms/{id}/files` is a cheap validity probe; an expired session needs the *user* to log in, so probe before long build/deploy prep and escalate immediately (say + focused login tab) rather than discovering it at upload time.
- **Wrong Chrome profile.** `browser_cookie3.chrome()` reads the **default** Chrome profile. If the user is logged into SurveyCTO in a non-default profile, pass cookies explicitly with `--cookie` or set `$SURVEYCTO_COOKIE`.
- **Drive `.gsheet` pointer hangs.** If `surveycto_upload.py --from-gsheet path/to/form.gsheet` stalls while reading a Google Drive Desktop stub, pass the raw doc id instead; the export path still uses the same live Sheet.
- **Group ID for non-root uploads.** `--parent-group-id` defaults to `1` (root group). If the user wants the form inside a specific group, find that group's id by inspecting the SurveyCTO Design page (or just upload to root and let the user drag it).
- **The Chrome MCP cannot do native file picker uploads** (tracked in `~/.claude/chrome-extension.md`) — that's the whole reason this CLI exists. Don't fall back to clicking the upload button via the browser extension; use this script instead.
- **`recalc_excel.sh` steals keyboard focus** — it opens the file in Excel via osascript for ~5s; if the user is typing, their keystrokes land in whatever cell has focus and silently corrupt the sheet (observed: garbage in survey row 3 → upload rejected with `Invalid question name [el so]`). Warn the user (e.g. `say`) before running it, and after any rejected upload naming a garbage token, re-copy the xlsx from source and recalc again rather than debugging the corrupted copy.
- **Verify a deployed attachment byte-for-byte** via its `downloadLink` from `GET /forms/{form_id}/files` → `deployedGroupFiles.mediaFiles[<name>].downloadLink` (cookie auth). Guessing URL patterns like `/files/media/<name>` 404s. For field-plugin zips, unzip in-memory and check `manifest.json` version + the substituted URL in `script.js`.
- **Submission data via the console cookie**: `GET /api/v2/forms/data/wide/json/{form_id}?date=0` works with the JSESSIONID session (no separate API user needed) — handy for verifying a test submission's fields right after upload.

### User management via the console API (add/remove server users)

The console's user-management UI is plain AJAX over the same JSESSIONID + CSRF auth as uploads (reuse `load_session` + `fetch_csrf_token` from `surveycto_upload.py`; note `surveycto_upload.py` needs an interpreter with `browser_cookie3` — on Duncan's Macs that's `~/.venvs/lifecoach/bin/python`, not system python or python3.12). Verified working (added a real user, Aug 2026):

- `GET /users/get?tg=true&t=<epoch-ms>` (header `X-csrf-token`) → `{roles: [...], users: [{username, roleId}, ...]}` — lists all users and role definitions. Built-in role ids: `GLOBAL_ADMIN`, `GLOBAL_MANAGER` (forms+data+users), `GLOBAL_FORM_MANAGER` (forms+data, no user admin — the usual choice for collaborators/RAs), `GLOBAL_DATA_MANAGER`, `GLOBAL_COLLECTOR`; plus any custom roles.
- `POST /users/add` with form data `{u: <email>, roleId: <role id>, blankPassword: "true", includePasswordInEmail: "false", password1: "", password2: ""}` (header `X-csrf-token`) — `blankPassword=true` = "Invite user to create their own password": SurveyCTO emails the invite, no password handling needed. Success response: `{"failedPasswordIndex":0,"errorMessage":null}`. Verify by re-fetching `/users/get`.
- Also available, same auth pattern: `POST /users/role` `{u, role}`, `POST /users/delete` `{u}`, `POST /users/password` `{u, password1, password2}`.
- The official Access Control API (`/api/v2/users`) may be **disabled by subscription** (403 "Read access to Access Control APIs is disabled") — the console endpoints above work regardless, since they're what the web UI itself calls.

### Listing currently-attached form files (audit pattern)

To check what media/data files are currently attached to a deployed form, hit `GET /forms/{form_id}/files` (auth via JSESSIONID cookie). The response includes an `xForm` object with deployment metadata worth reading before any redeploy:

- `xForm.version` — The deployed form's version string (e.g. `2026040705`). Unchanged since your last deploy proves nobody else re-uploaded in between, since SurveyCTO enforces lexically-greater versions.
- `xForm.creationDate` — Timestamp when this version was deployed.
- `xForm.lastIncomingDataDate` — Timestamp of the last submission received. Tells you whether the form is actively collecting data, which matters for mid-round replacements (a form with recent submissions is risky to change).

The attachments live at `deployedGroupFiles.mediaFiles` as an **object keyed by filename** (`{ "foo.csv": {...meta}, "bar.png": {...meta}, ... }`). Just read `Object.keys(...)` for the list of uploaded filenames. There's also `draftGroupFiles.mediaFiles` for the draft version. Useful for diffing referenced-in-form media (`media:image*`, `media:audio*`, `media:video*`, `image*`) against what's actually deployed. From Chrome MCP, `fetch('/forms/{form_id}/files?t=' + Date.now(), {credentials:'same-origin'})` works once the user is logged in; from Python, reuse the cookie-loading + CSRF-scrape helpers in `surveycto_upload.py`.

### Downloading the deployed form definition (verify deployed-vs-local labeling)

SurveyCTO has **no API to download the original `.xlsx` source** — but the **console UI does keep it, including for PREVIOUS deployed versions**: Design tab → form → Download → "Form files" opens a dialog with the current form definition/attachments AND a "Previous deployed versions" list (one row per version with uploader + timestamp; each row is a direct download link for that version's original xlsx). This is the recovery path when a rebuild/reupload clobbered someone's server-side edits (used 3 Aug 2026 to recover the transport baseline's round-1 additions). Drive `.xlsx` revision history is a second recovery source when the file was edited via Drive. Via API, only the **compiled XForm XML** is downloadable. `GET /forms/{id}/xml` accepts either OpenRosa HTTP basic auth or the authenticated console `JSESSIONID` session returned by `load_session(server)`. This is the way to confirm that a local XLSForm correctly labels submission data when the deployed version differs (e.g. a redeploy you don't have the source for):

```bash
# 1. List forms + deployed versions + download URLs
curl -s -u "$USER:$PASS" -H "X-OpenRosa-Version: 1.0" \
  "https://$SERVER.surveycto.com/formList"
# 2. Download the compiled XForm XML (note: /forms/{id}/xml, NOT /form.xml — that 404s to an HTML error page)
curl -s -u "$USER:$PASS" -H "X-OpenRosa-Version: 1.0" \
  -o deployed.xml "https://$SERVER.surveycto.com/forms/{form_id}/xml"
```

The XML carries the deployed `version="..."` attribute on the form's instance-root element (for example, `<my_form id="my_form" version="...">`), not on the outer `<h:html>` element. It also carries all field names (`<bind nodeset>`) and choices as `<select1>/<select>` `<item>` blocks with `<value>` + itext-referenced `<label>`. To diff field names / choice values / label text against a local xlsx: parse the XML (ElementTree), resolve `jr:itext('id')` labels via the `<itext>` English `<translation>`, and compare. Note: the compiled XForm collapses repeats and select_multiples differently from the wide data export — field-name diffs from auto-generated fields (`*_count`, `*_position`, pulldata list names) are expected; the **value/label diffs are what matter**. (Basic auth works on `/formList` and `/forms/{id}/*`; the console cookie works on `/forms/{id}/xml` and is required for console pages such as `/forms/{id}/files`.)

---

Create and edit XLSForm surveys in Excel format for mobile data collection platforms (SurveyCTO, ODK, KoboToolbox).

## Quick Start Workflow

### Creating New XLSForm Survey

Start from the bundled template — do NOT build a fresh workbook with `openpyxl.Workbook()` or `pandas.ExcelWriter`:

```bash
cp "$SURVEYCTO_SKILL_DIR/assets/xlsform-template.xlsx" path/to/new_form.xlsx
```

The template (vendored from the official SurveyCTO skill) ships with the exact SurveyCTO column headers on all three sheets, starter metadata rows (`starttime`/`endtime`/`deviceid`/`username`, `device_info`/`duration` calculates, a `caseid` row — delete `caseid` for non-case-management forms), a `yesno` choice list, `help-survey`/`help-choices`/`help-settings` reference sheets, the type-based conditional formatting that this skill's checker verifies, and a NOW()-based auto-updating `version` formula. A from-scratch workbook loses all of that and immediately fails the checker's conditional-formatting check. Then edit the copy with openpyxl as below.

- The template's version formula is `=TEXT(YEAR(NOW())-2000, "00") & ...` — this project's convention adds a `+2` year offset (`YEAR(NOW())-2000+2`) to stay lexically greater than legacy versions. Fine for a brand-new form either way; apply the `+2` variant when consistency with this project's other forms matters.
- Set `form_title`/`form_id` in settings row 2 (`default_language` is `english`).
- When form B derives from an existing form A, the build-script pattern below (copy the project's own form, not the template) is the right move.

### Deriving a New Form from an Existing One (build-script pattern)

When form B is a heavy edit of form A (e.g. a pilot variant: ~150 deletions, moved blocks, new modules), do NOT mutate a copy incrementally. Write ONE re-runnable Python build script that `shutil.copy`s the source form fresh and applies every transformation (delete-by-name bottom-up, capture+delete+insert to move blocks, insert-after-named-anchor for new rows, choice-list edits, then auto-prune unused choice lists by scanning `select_one/select_multiple` types). Each fix iteration = edit script → re-run → re-run checker. Benefits: deterministic rebuilds, reviewable diff of intent, audit fixes append as a phase-2 section, and the source form stays pristine. Worked example: `AI HEALTH/piloting/transport_pilot/questionnaires/build_transport_baseline.py` (Jan baseline → transport baseline; group split, scale-block relocation, ~90 new rows, settings/version/instance_name). Two gotchas: never hand-edit the generated xlsx (rebuild wipes it — put a note in the project CLAUDE.md); write a computed static `settings.version` string from `datetime.now()` in the script rather than the NOW() formula (openpyxl can't cache formula values, so a rebuild would otherwise upload a literal `=TEXT(...)` string).

### Editing Existing XLSForm Survey

```python
from openpyxl import load_workbook

wb = load_workbook('survey.xlsx')
survey = wb['survey']

# Add new question (find last row first)
last_row = survey.max_row + 1
survey.append(['select_one yes_no', 'consent', 'Do you consent?', 'yes'])

# Modify existing question
for row in survey.iter_rows(min_row=2):
    if row[1].value == 'age':  # Find by name column
        row[3].value = 'yes'    # Set required column
        row[5].value = '. >= 18 and . <= 100'  # Update constraint

wb.save('survey_updated.xlsx')
```

### Reading and Analyzing Survey Data

```python
import pandas as pd

# Read all sheets
sheets = pd.read_excel('survey.xlsx', sheet_name=None)
survey_df = sheets['survey']
choices_df = sheets['choices']
settings_df = sheets['settings']

# Analyze structure
print(f"Total questions: {len(survey_df)}")
print(f"Required questions: {(survey_df['required'] == 'yes').sum()}")
print(f"Questions with skip logic: {survey_df['relevance'].notna().sum()}")
```

## XLSForm Structure

### Required Sheets

**survey** - Question structure and logic
**choices** - Response options for select questions
**settings** - Form-level configuration

### survey Sheet Columns

**Essential Columns:**

- `type` (required) - Question type: `text`, `integer`, `decimal`, `date`, `time`, `geopoint`, `select_one [list]`, `select_multiple [list]`, `note`, `calculate`, `begin group`, `end group`, `begin repeat`, `end repeat`
- `name` (required) - Unique identifier (lowercase, underscores only, e.g., `d1_age`, `s2_saw_provider`)
- `label` (required for questions) - Question text shown to users. Can include HTML (`<b>bold</b>`) and field references (`${field_name}`)

**Logic Columns:**

- `required` - Make mandatory: `yes` or logic `${age} >= 18`
- `relevance` - Skip logic: `${q1} = 'yes'`, `${age} >= 18 and ${consent} = 'yes'`, `selected(${symptoms}, 'fever')`
- `constraint` - Validation: `. >= 0 and . <= 120`, `. > ${start_date}`, `regex(., '^\d{10}$')`
- `constraint_message` - Error message for failed constraint
- `calculation` - Formula: `today()`, `${price} * ${quantity}`, `if(${age} >= 18, 'adult', 'child')`, `count(${roster})`, `sum(${expenses})`, `index()`
- `choice_filter` - Filter choices: `district = ${selected_district}`
- `repeat_count` - Repeat iterations: `${num_children}`

**Other Columns:**

- `default` - Pre-filled value (static or formula)
- `hint` - Help text below question
- `appearance` - Display control: `minimal`, `compact`, `multiline`, `numbers`, `horizontal-compact`, `signature`
- `read_only` - Display only: `yes`
- `disabled` - When set to `yes`, the question is completely excluded from the survey. **Treat disabled questions as if they don't exist** - they won't appear in the form, won't collect data, and should be ignored when analyzing survey structure or adding new questions. Disabled rows are kept in the Excel file for reference but are functionally removed from the active survey. Caveat for live forms: disabling (like deleting) removes the field from the deployed form definition, so previously collected data for it stops exporting alongside new submissions; SurveyCTO's own recommendation when old data must remain exportable mid-round is `relevance = 0` (field stays defined, never shown) rather than `disabled`.

### choices Sheet Columns

- `list_name` (required) - Links to `select_one [list_name]` in survey sheet
- `name` OR `value` (required) - Choice identifier (e.g., `male`, `female`, `yes`, `no`). XLSForm spec accepts either column name; some K2 forms (e.g. bulletin_notes) use `value`. The `add_choice_list` helper in `gsheet_edit.py` auto-detects which is in use.
- `label` (required) - Display text
- `image` (optional) - Image filename
- `filter` (optional) - For cascading selects

Example:

```
list_name | name   | label
----------|--------|------------
yes_no    | 1      | Yes
yes_no    | 0      | No
gender    | male   | Male
gender    | female | Female
```

### settings Sheet Columns

- `form_title` (required) - Survey name displayed to users
- `form_id` (required) - Unique identifier (lowercase with underscores)
- `version` - **MUST use a NOW()-based formula, NEVER a hardcoded value.** Use: `=TEXT(YEAR(NOW())-2000+2, "00") & TEXT(MONTH(NOW()), "00") & TEXT(DAY(NOW()), "00") & TEXT(HOUR(NOW()), "00") & TEXT(MINUTE(NOW()), "00")`. The `+2` offset ensures versions are always lexically greater than legacy versions. **IMPORTANT:** SurveyCTO cannot evaluate Excel formulas — the formula must be evaluated (cached) before upload. After writing the formula with openpyxl, run `recalc_excel.sh <file>` to open the file in Excel, evaluate the formula, and save the cached value. The checker will do this automatically if the script is available.
- `default_language` - Default language for multi-language surveys
- `instance_name` - Display format: `concat(${name}, ' - ', ${date})`

## Common Patterns

### Skip Logic

Show follow-up:

```
relevance: ${q1} = 'yes'
```

Multiple conditions (AND):

```
relevance: ${age} >= 18 and ${consent} = 'yes'
```

Multiple conditions (OR):

```
relevance: ${q1} = 'yes' or ${q2} = 'yes'
```

Check multi-select choice:

```
relevance: selected(${symptoms}, 'fever')
```

"Other (specify)" follow-up:

```
type: text
name: provider_other
relevance: ${provider_type} = 'other'
```

### Repeating Groups (Rosters)

```
type            | name           | label                      | calculation
----------------|----------------|----------------------------|-------------
integer         | num_children   | How many children?         |
begin repeat    | child_roster   | Children                   |
calculate       | child_position |                            | index()
integer         | child_age      | Age of child ${child_position}   |
text            | child_name     | Name of child ${child_position}  |
end repeat      | child_roster   |                            |
```

**Gotcha:** XLSForm does NOT auto-provide a `${position}` variable inside repeat groups. You must define a `calculate` field with `index()` and reference it by its declared name (e.g. `${child_position}`). Using `${position}` in labels (including the `begin repeat` label like `Advisor ${position}`) fails the checker with "References non-existent field". Always use `${<your_calc_name>}`.

### Calculations

Date/time:

```
today()           - Current date
now()             - Current datetime
duration()        - Seconds since survey start
```

Math:

```
${price} * ${quantity}
${total} - ${paid}
round(${value}, 2)
```

Conditional:

```
if(${age} >= 18, 'adult', 'child')
if(${score} >= 80, 'A', if(${score} >= 70, 'B', 'C'))
```

String:

```
concat(${first_name}, ' ', ${last_name})
string-length(${text})
```

Repeats:

```
count(${roster})        - Count items
sum(${expenses})        - Sum values
index()                 - Position (1-indexed) — always prefer this
position(..)            - 0-indexed ODK-ism; can fail in a non-repeating group inside a repeat
```

### Multi-language Surveys

Use single-colon language suffixes — SurveyCTO's convention, unlike ODK's double-colon `label::Language`:

- `label:Hindi`, `label:Swahili`
- `hint:Hindi`, `constraint message:Hindi`, `required message:Hindi`

Set `default_language = English` in settings sheet. **The default language lives in the UNSUFFIXED columns** (`label`, `hint`, ...); putting it in a suffixed column (`label:English`) and leaving the base column empty silently breaks the form — the single most common structural translation mistake.

For actual translation work — adding a language, updating translations after source edits, verifying someone else's translations, glossary files, back-translation spot checks, and the preserve-verbatim rules (`${refs}`, HTML tags, choice values, expressions) — read [`references/translation.md`](references/translation.md) first.

### Randomization

For RCT-style randomization (choice-order shuffling, A/B arm assignment, counterbalancing block order, list/item-count experiments, pre-randomized lists via `pulldata`, etc.) see [`references/randomization-patterns.md`](references/randomization-patterns.md). Covers 9 transferable patterns with XLSForm rows + gotchas.

Quickest hits:
- Shuffle choices on one select with reproducible seed + pinned "Other": `appearance: randomized(${respondent_id}, 0, 2)` — args are `(seed, top_excluded, bottom_excluded)`, not `(seed, min, max)`.
- Stable A/B switch: `calculate` with `once(random())`, then `if(${draw} > 0.5, 'a', 'b')`. Never put `random()` directly in `relevance`.
- Random order of N items per respondent: pre-randomize externally, save as `;`-separated string, pull with `pulldata`, unpack via `item-at(';', list, index() - 1)` inside a `begin repeat`.

### Other form-design patterns

Letting a later question offer the respondent's own earlier free-text answers as choices (nomination rounds with in-form dedup, no manual dedup step): see [`references/form-patterns.md`](references/form-patterns.md).

## Naming Conventions

**Question names:**

- Section prefixes: `d1_`, `d2_` (demographics), `s1_`, `s2_` (symptoms)
- Descriptive: `saw_provider`, not `q3`
- Underscores only, no camelCase or spaces

**Choice names:**

- Simple: `yes`/`no`, not `option_yes`/`option_no`
- Consistent codes: `1` for Yes, `0` for No

### Question Numbering System

A systematic approach to numbering questions helps with survey organization, data analysis, and cross-referencing between forms and documentation.

**Pattern:**

- **Variable name** (lowercase): `[section][number]_[description]`
- **Label** (uppercase): `[SECTION].[number]: [question text]`
- **Multi-language labels**: Keep the same numbering prefix across all languages

**Examples:**

Demographics section (D):

```
name: d1_district
label: D.1: Name of District
label:Hindi: D.1: जिले का नाम
```

```
name: d5_gender
label: D.5: Gender
label:Hindi: D.5: लिंग
```

Symptoms/Health section (S):

```
name: s1_symptoms
label: S.1: Did you suffer from any health problems in the past 30 days?
label:Hindi: S.1: क्या आपको पिछले 30 दिनों में कोई स्वास्थ्य समस्या हुई थी?
```

```
name: s3_saw_provider
label: S.3: Did you visit any health provider in the last 30 days for any reason?
label:Hindi: S.3: क्या आपने पिछले 30 दिनों में किसी भी कारण से किसी स्वास्थ्य प्रदाता से मुलाकात की?
```

Treatment/Screening section (T):

```
name: t1_screening_result
label: T.1: Did the screening tool recommend the respondent to go to a doctor?
label:Hindi: T.1: क्या स्क्रीनिंग उपकरण ने उत्तरदाता को डॉक्टर के पास जाने की सिफारिश की?
```

**Benefits:**

- **Traceability**: Easy to reference specific questions in documentation, codebooks, and analysis scripts
- **Organization**: Clear section structure visible in both data and questionnaire
- **Multi-language consistency**: Numbering helps align translations
- **Data merging**: Consistent prefixes make it easy to identify which questions come from which section when merging datasets

**Common sections:**

- `D` (Demographics): `d1_`, `d2_`, `d3_`, ... - Basic respondent information
- `S` (Symptoms/Health): `s1_`, `s2_`, `s3_`, ... - Health status and symptoms
- `T` (Treatment): `t1_`, `t2_`, `t3_`, ... - Healthcare seeking, treatment, screening
- `E` (Economics): `e1_`, `e2_`, `e3_`, ... - Income, expenditure, costs
- `H` (Household): `h1_`, `h2_`, `h3_`, ... - Household-level information

**Implementation in Python:**

```python
from openpyxl import load_workbook

wb = load_workbook('survey.xlsx')
survey = wb['survey']

# Add numbered question
survey.append([
    'select_one yes_no',                                    # type
    't1_screening_result',                                  # name (lowercase, section prefix)
    'T.1: Did the screening tool recommend going to a doctor?',  # label (uppercase, numbered)
    'yes',                                                  # required
    '',                                                     # relevance
    '',                                                     # constraint
    ''                                                      # calculation
])

# For multi-language surveys, ensure label columns match
# Column headers would be: 'label', 'label:Hindi', etc.
wb.save('survey.xlsx')
```

## Excel Manipulation for XLSForm

### Reading Survey Files

```python
import pandas as pd

# Read specific sheet
survey_df = pd.read_excel('survey.xlsx', sheet_name='survey')

# Read all sheets
all_sheets = pd.read_excel('survey.xlsx', sheet_name=None)
survey = all_sheets['survey']
choices = all_sheets['choices']
settings = all_sheets['settings']

# Analyze
print(survey_df.head())
print(survey_df.columns)
print(choices_df['list_name'].unique())
```

### Adding Questions

```python
from openpyxl import load_workbook

wb = load_workbook('survey.xlsx')
survey = wb['survey']

# Add at end
survey.append([
    'select_one yes_no',     # type
    'consent',               # name
    'Do you consent?',       # label
    'yes',                   # required
    '',                      # relevance
    '',                      # constraint
    ''                       # calculation
])

wb.save('survey.xlsx')
```

### Adding Choices

```python
wb = load_workbook('survey.xlsx')
choices = wb['choices']

# Add new choice list
choices.append(['symptoms', 'fever', 'Fever'])
choices.append(['symptoms', 'cough', 'Cough'])
choices.append(['symptoms', 'headache', 'Headache'])

wb.save('survey.xlsx')
```

### Modifying Questions

```python
wb = load_workbook('survey.xlsx')
survey = wb['survey']

# Find and modify question by name
for row in survey.iter_rows(min_row=2):  # Skip header
    if row[1].value == 'age':  # name column
        row[2].value = 'What is your age in years?'  # Update label
        row[5].value = '. >= 0 and . <= 120'  # Update constraint
        break

wb.save('survey.xlsx')
```

### Batch Operations

```python
import pandas as pd

# Read survey
df = pd.read_excel('survey.xlsx', sheet_name='survey')

# Add section prefix to all names
df['name'] = 'd1_' + df['name']

# Make all text questions required
df.loc[df['type'] == 'text', 'required'] = 'yes'

# Save back
with pd.ExcelWriter('survey.xlsx', engine='openpyxl') as writer:
    df.to_excel(writer, sheet_name='survey', index=False)
```

### Formatting Surveys

```python
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

wb = load_workbook('survey.xlsx')
survey = wb['survey']

# Bold headers
for cell in survey[1]:
    cell.font = Font(bold=True)
    cell.fill = PatternFill('solid', start_color='D3D3D3')  # Light gray

# Auto-adjust column widths
for column in survey.columns:
    max_length = 0
    column_letter = column[0].column_letter
    for cell in column:
        if cell.value:
            max_length = max(max_length, len(str(cell.value)))
    survey.column_dimensions[column_letter].width = min(max_length + 2, 50)

wb.save('survey.xlsx')
```

## Best Practices

### ⚠️ CRITICAL: Preserving Cell Formatting

**This project uses cell formatting with semantic meaning:**

- **Red text** = Unverified Hindi translations that need review
- **Gray background** = Section headers or special rows
- Removing this formatting loses important information about translation status

**ALWAYS use openpyxl (not pandas) when editing Excel files:**

```python
# ✅ CORRECT: Use openpyxl to preserve formatting
from openpyxl import load_workbook

wb = load_workbook('survey.xlsx')
survey = wb['survey']

# Edit cells directly - formatting is preserved
for row in survey.iter_rows(min_row=2):
    if row[1].value == 'field_name':
        row[2].value = 'New label text'  # Formatting preserved!

wb.save('survey.xlsx')
```

```python
# ❌ WRONG: pandas destroys all formatting!
import pandas as pd

df = pd.read_excel('survey.xlsx', sheet_name='survey')
df.loc[df['name'] == 'field_name', 'label'] = 'New label text'

# This DESTROYS all formatting (red text, colors, etc.)
df.to_excel('survey.xlsx', sheet_name='survey', index=False)
```

**When adding new Hindi translations:**

```python
from openpyxl import load_workbook
from openpyxl.styles import Font

wb = load_workbook('survey.xlsx')
survey = wb['survey']

# Find the cell and add translation with RED text (needs verification)
for row in survey.iter_rows(min_row=2):
    if row[1].value == 'new_question':
        hindi_cell = row[3]  # Assuming column D is label:Hindi
        hindi_cell.value = 'नया प्रश्न'
        hindi_cell.font = Font(color='FF0000')  # RED = unverified
        break

wb.save('survey.xlsx')
```

**After translation is verified by Hindi speaker:**

```python
# Change red text to black after verification
hindi_cell.font = Font(color='000000')  # BLACK = verified
```

**Before saving any Excel file, run the checker to verify formatting is preserved:**

```bash
python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_checker.py" survey.xlsx
```

### ⚠️ CRITICAL: Renaming Variables

**When renaming any variable (changing the `name` column), you MUST search the entire sheet for ALL other references to that variable and update them too.**

Variables are referenced using `${variable_name}` syntax in these columns:
- `relevance` - Skip logic conditions
- `constraint` - Validation rules
- `calculation` - Calculated fields
- `label` - Dynamic text in question labels
- `choice_filter` - Cascading select filters
- `repeat_count` - Dynamic repeat counts
- `required` - Conditional required logic

**Failing to update all references will break the form!**

**Example - Renaming a variable:**

If you rename `s3_saw_provider` to `s3_visited_provider`, you must find and update:

```
# Before:
relevance: ${s3_saw_provider} = 1
label: You said you saw a provider (${s3_saw_provider}). Which one?
calculation: if(${s3_saw_provider} = 1, 'visited', 'not visited')

# After:
relevance: ${s3_visited_provider} = 1
label: You said you saw a provider (${s3_visited_provider}). Which one?
calculation: if(${s3_visited_provider} = 1, 'visited', 'not visited')
```

**How to find all references:**

```python
from openpyxl import load_workbook

wb = load_workbook('survey.xlsx')
survey = wb['survey']

old_name = 's3_saw_provider'
new_name = 's3_visited_provider'
old_ref = f'${{{old_name}}}'  # ${s3_saw_provider}
new_ref = f'${{{new_name}}}'  # ${s3_visited_provider}

# Search all cells for references
columns_to_check = ['relevance', 'constraint', 'calculation', 'label',
                    'label:Hindi', 'choice_filter', 'repeat_count', 'required']

# Get header row to find column indices
headers = {cell.value: cell.column for cell in survey[1]}

for row in survey.iter_rows(min_row=2):
    for col_name in columns_to_check:
        if col_name in headers:
            cell = row[headers[col_name] - 1]
            if cell.value and old_ref in str(cell.value):
                print(f"Found reference in row {cell.row}, column {col_name}: {cell.value}")
                cell.value = str(cell.value).replace(old_ref, new_ref)

# Don't forget to also rename the variable itself in the 'name' column!
for row in survey.iter_rows(min_row=2):
    name_cell = row[headers['name'] - 1]
    if name_cell.value == old_name:
        name_cell.value = new_name
        break

wb.save('survey.xlsx')
```

**Checklist before renaming a variable:**

1. [ ] Search for `${old_variable_name}` in ALL sheets (survey, choices)
2. [ ] Update every reference found
3. [ ] Rename the variable itself in the `name` column
4. [ ] Run the SurveyCTO checker to validate: `python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_checker.py" survey.xlsx`
5. [ ] Test the form in XLSForm validator

### Form Structure

1. **Start with metadata:**

   - deviceid, username, start, caseid
2. **Use section headers:**

   - `type = note`, `label = <b>SECTION 1: DEMOGRAPHICS</b>`
3. **Group related questions:**

   - `begin group` / `end group` for organization
   - Can add group-level relevance
4. **End with metadata:**

   - GPS coordinates, enumerator notes, end time
5. **Include data quality:**

   - Constraints on ranges
   - Required fields for critical data
   - Validation messages

### Excel Operations

**Choose the right library:**

- **pandas**: Data analysis, bulk operations, reading data
- **openpyxl**: Adding/modifying questions, preserving structure, formatting

**Preserve structure:**

- Always maintain the three required sheets: survey, choices, settings
- Keep header rows intact
- Preserve column order for compatibility

**Avoid errors:**

- Use `load_workbook()` without `data_only=True` to preserve formulas
- Test survey after modifications with XLSForm validation tool
- Check for duplicate question names
- Verify all `select_one`/`select_multiple` reference existing choice lists

## Testing Surveys

Before deployment:

1. **Validate XLSForm:** Use https://getodk.org/xlsform/
2. **Test skip logic:** Questions appear/hide correctly
3. **Test calculations:** Computed values are correct
4. **Test constraints:** Validation works as expected
5. **Test on device:** Run through on mobile device
6. **Check data output:** Export works correctly

## Common Errors

1. **SurveyCTO diverges from ODK/XPath** — never copy generic ODK snippets unedited. The load-bearing divergences: equality is `=` (never `==`); division is `div` (not `/`); current repeat index is `index()` (ODK's `position(..)` can fail in a non-repeating group inside a repeat); choice labels via `choice-label(field, value)` (preferred over `jr:choice-name()`, different arg order); the skip-logic column is `relevance` (not `relevant`); `select_multiple` membership needs `selected()`, never `=`. The checker errors on `==` and the unsupported functions below. Full function tables with signatures, worked patterns, and a symptom→cause→fix pitfalls table: [`references/expressions.md`](references/expressions.md).

2. **Unsupported functions:** SurveyCTO doesn't support `starts-with()`, `contains()`, `substring-before()`, or `substring-after()` — even though [ODK docs](https://docs.getodk.org/form-operators-functions/) list the latter two, SurveyCTO's JavaRosa parser rejects them with "cannot handle function 'substring-after'". Use `substr(string, 0, N) = 'prefix'` and `regex(string, '.*pattern.*')` for matching, and `selected-at(string, N)` (zero-indexed, space-separated) for extraction. **When in doubt, check [SurveyCTO's expressions reference](https://docs.surveycto.com/02-designing-forms/01-core-concepts/09.expressions.html), NOT the ODK docs** — SurveyCTO's XPath function set is a strict subset of ODK's.

3. **Integers cap at nine digits.** Longer values (phone numbers, long IDs) silently break — use `text` with `appearance: numbers` / `numbers_phone` instead, which shows the numeric keyboard but stores text (also preserves leading zeros).

4. **Extracting structured data from a field plug-in's output:** Use the `plug-in-metadata()` + `selected-at()` pattern. The plug-in calls `setMetaData(spaceSeparatedString)` (where any value containing spaces has its spaces replaced with `_`), and a calculate field then reads `selected-at(plug-in-metadata(${plugin_field}), N)` for the Nth value (zero-indexed). Direct JSON parsing in the form is impossible — there's no `substring-before/after`, `regex-replace`, or JSON parser.
1. **Duplicate names:** Each question needs unique name
2. **Missing list_name:** select questions must reference existing choice list
3. **Syntax errors:** Check parentheses, quotes, operators in logic
4. **Invalid references:** `${field_name}` must exist
5. **Circular references:** Field A can't use Field B if B uses Field A
6. **Invalid characters:** Names must be letters, numbers, underscores only
7. **Mismatched tags:** Every `begin group` needs `end group`

## Field Plug-ins

When authoring or debugging a `.fieldplugin.zip`, read [`references/field-plugins.md`](references/field-plugins.md) — the full form API (`fieldProperties`, provided/called JS functions, Mustache `{{{LABEL}}}`/`{{{HINT}}}` triple-brace rules, `{{PLUGINDIR}}` attachment paths), packaging rules, and platform caveats (intents/phone APIs are Android-only). Quick hits:

- Plug-ins only work on `text`/`integer`/`decimal`/`select_one`/`select_multiple`. The `custom-<name>` appearance token is the **zip filename stem** (`myplugin.fieldplugin.zip` → `custom-myplugin`), NOT `manifest.name`. All files at the zip root — subdirectories get flattened on upload and duplicate basenames error.
- Start from SurveyCTO's `baseline-*` GitHub repos (`baseline-text`/`-integer`/`-decimal`/`-select_one`/`-select_multiple`) or the [catalog](https://support.surveycto.com/hc/en-us/articles/360045235134-Field-plug-in-catalog); `assets/field-plugin-template/` is a minimal offline text-only skeleton (see the reference for the baseline behaviors it omits). **Read an existing plug-in's README before building form-side conversion layers** — it often already accepts your raw value.
- Local fast loop: open `assets/field-plugin-test-harness/preview.html` in a browser (folder mode or paste-in-textareas mode) — it renders the four core files with the host JS bridge stubbed, real Mustache, platform-class toggles, and a mirrored console. `node assets/field-plugin-test-harness/validate.mjs <plugin-dir-or-zip>` statically checks manifest/filenames/`clearAnswer`/`setAnswer`. Both zero-dependency.
- Final validation: the in-product plug-in console — form designer → **Test** → navigate to the plug-in field → icon on the left edge. It live-edits HTML/CSS/JS against real form context; edits are session-scoped, so copy fixes back to source and re-upload with a bumped `manifest.version` (see version-bump gotcha in the upload section).
- `setAnswer()` for `select_multiple` takes a **space**-separated value list. Read parameters with `getPluginParameter('key')`, not the order-sensitive `PARAMETERS` array. Restore UI state from `fieldProperties.CURRENT_ANSWER` on load; always define `clearAnswer()`.
- Plug-in metadata (`setMetaData`) is encrypted only when both the form AND the field are encrypted, and never when the field is `publishable` — treat it as response data; no secrets or PII beyond what the field itself would hold.

## Converting Forms from Other Platforms

Converting a Kobo/ODK XLSForm, CommCare XForms XML, or Qualtrics `.qsf` export into a SurveyCTO form: read [`references/form-conversion.md`](references/form-conversion.md) first (workflow contract: read source with your own tooling, plan the full mapping, convert onto a template copy, write a conversion report next to the output), then the platform reference (`form-conversion-kobo.md`, `-odk.md`, `-commcare.md`, `-qualtrics.md`). Highest-value rules: Kobo `begin_kobomatrix`/`kobo--*` tokens are rejected outright (expand matrices into `field-list` groups); ODK forms sometimes put the default language only in `label::English` leaving `label` empty, which silently breaks the form.

## Resources

- **XLSForm specification:** https://xlsform.org
- **SurveyCTO documentation:** https://docs.surveycto.com
- **ODK documentation:** https://docs.getodk.org — CAUTION: SurveyCTO's function set diverges (see Common Errors above); check SurveyCTO's own expressions reference first
- **XLSForm validation:** https://getodk.org/xlsform/

### Bundled reference library

`references/` vendors the official SurveyCTO agent skill's reference docs (Apache-2.0; provenance and refresh recipe in [`references/README.md`](references/README.md)). Read on demand:

- [`references/xlsform.md`](references/xlsform.md) — exact column spellings (`constraint message` with a space, `choice_filter` with an underscore), ALL field types incl. SurveyCTO-specific ones (`enumerator`, `text audit`, `audio audit`, `speed violations *`, `sensor_*`, `calculate_here`, `comments`), the full appearance catalog, settings columns, choices ordering rules, template anatomy.
- [`references/expressions.md`](references/expressions.md) — full function tables with signatures, SurveyCTO-vs-ODK divergence table, worked patterns (age-from-DOB, search(), randomization), pitfalls table, debugging checklist.
- [`references/translation.md`](references/translation.md) — translation workflows (see Multi-language Surveys above).
- [`references/field-plugins.md`](references/field-plugins.md) — plug-in form API, manifest, packaging, testing (see Field Plug-ins above).
- [`references/datasets-xml.md`](references/datasets-xml.md) + [`references/dataset-validation.md`](references/dataset-validation.md) — dataset definition XML (see the server-datasets section).
- [`references/form-conversion.md`](references/form-conversion.md) + platform variants — form conversion (see above).
- [`references/data-explorer.md`](references/data-explorer.md) — Data Explorer monitoring-workbook definitions (rarely needed; we do monitoring in R).

SurveyCTO also runs a public no-auth MCP server (`https://assistant-be.surveycto.net/mcp`; tool docs in [`references/mcp.md`](references/mcp.md)). Its `kb_search` tool searches docs./support./www.surveycto.com — a good fallback when the local references don't answer a product-behavior question (plain WebFetch of docs.surveycto.com also works). Its XLSForm session-editing tools are NOT part of this skill's workflow: the gsheet/checker/upload pipeline above is strictly more capable for our forms, and uploading questionnaires to SurveyCTO's assistant backend is unnecessary exposure.

## Enumerator Instructions

Use note fields to provide instructions for enumerators (field staff) that won't be stored as data.

### Format

Use "ENUMERATOR:" prefix in ALL CAPS:

```
type | name                  | label
-----|-----------------------|--------------------------------------------------
note | consent_script        | ENUMERATOR: READ OUT CONSENT
     |                       |
     |                       | [Insert consent script here]
note | screening_instruction | ENUMERATOR: Now conduct the screening tool with the respondent
note | followup_note         | ENUMERATOR: Call back in two weeks to check whether they visited
```

### Style Guidelines

- **ALL CAPS** for "ENUMERATOR:" prefix
- Use `note` type (not stored in data, only displayed during collection)
- Variable names: `[purpose]_script`, `[purpose]_instruction`, `[purpose]_note`
- Keep instructions clear and action-oriented
- Use separate note rows (don't mix with question labels)

### Respondent-Facing Notes

Notes intended for respondents should NOT use "ENUMERATOR:" prefix:

```
type | name                | label
-----|---------------------|--------------------------------------------------
note | no_consent_note     | The respondent has declined to participate. Please thank them for their time and end the survey.
note | household_skip_note | NOTE: Since this is not the first person in the household, household-level questions will be skipped.
```

### Translation

Enumerator instructions typically remain in English only (not translated), since field staff are trained in English. Respondent-facing notes should be translated.

## Approach

When working with surveys:

1. **Creating:** Ask about purpose, question types, logic needs
2. **Editing:** Load with openpyxl to preserve structure
3. **Adding logic:** Clarify conditions and affected questions
4. **Translations:** Ask which languages needed
5. **Bulk operations:** Use pandas for efficiency
6. **Testing:** Validate after changes

Always explain modifications so users understand XLSForm structure.

## Server datasets: prefill, roster pickers & console automation (learned Jun 2026, transport pilot)

Pattern for one form to prefill from another form's submissions (e.g. transport-day forms reading the baseline):

- **Publish form → dataset:** Design tab → create server dataset → "Publish into" the source form → "Add all" → set **unique ID = the key field** (gives update-or-insert). Attach the dataset to each consuming form. There is a **~5–10 min refresh lag** before the attached CSV regenerates (+ device sync) — just-collected rows won't appear instantly.
- **Prefill a value:** `pulldata('dataset','col','keycol',${key})` (returns `''` on no match — guard with `string-length(...) = 0`).
- **Roster picker from a dataset, filtered:** `select_one/multiple <list>` with `appearance: search('dataset','matches','<filtercol>',${field})`. The choices sheet needs ONE placeholder row whose `value` cell = the dataset column to STORE and `label` cell = comma-separated columns to DISPLAY (e.g. `d4_name,d5b_age`). `search()` works identically on a published server dataset or a static `.csv` (source = dataset id / filename-without-.csv).
- **Filter-key values must be unique across rounds/waves.** The dataset accumulates ALL rounds' records, so reusing a choice code (e.g. village `v01` in round 1 AND round 2) makes `search()` surface the earlier round's records in the new round's roster (transport round 2, Aug 2026: Bhumlawas showed round-1 Kalimagri patients). Assign fresh codes each round (v06, v07, …) or add a round column to the filter.
- **Dataset attachments in `deployedGroupFiles.mediaFiles` have server-relative `downloadLink`s** (`/forms/{id}/dataset-attachment/...`) unlike regular media files' absolute URLs — prepend `https://{server}` before fetching.
- **Repeat over a select_multiple's picks** (ask sub-questions per selected item): `begin repeat` with `repeat_count=count-selected(${picks})`; inside, `calculate idx = index()-1`, `calculate this = selected-at(${picks}, ${idx})` (0-indexed, returns the stored value), then `pulldata('dataset','col','keycol',${this})` for per-item detail.
- **Flatten repeat-group answers into one string** (e.g. to pass to a field plug-in parameter, which cannot reference repeat fields directly): per-instance `calculate flat = concat(field1, ' [', field2, ']')` inside the repeat, then outside it `calculate all = join(' || ', ${flat})` — SurveyCTO supports `join(separator, repeatedfield)`. Pick a separator that cannot appear in enumerator-typed text. A `field-list`-appearance group inside each repeat instance puts that instance's fields on one screen (worked example: AI Health transport baseline medications/documents module, Aug 2026).
- **Long-format repeat publishing** (one dataset row per repeat instance): the "Form field to identify unique records" MUST be a field INSIDE the repeat group (listed with a trailing `*`, e.g. `b_household_id*`); a non-repeat key is rejected with "must be in a repeat group". The `*` source columns publish as clean names (no asterisk) in long format. An **empty dataset (0 records) does NOT appear in `/forms/{id}/files`** — verify attachment via the dataset block's "Attached to:" line in the console instead.
- **`choice_filter` cannot reference the reserved `value`/`name` column.** `choice_filter = value = 'x' or ...` silently filters out ALL options (empty select → blocks the form). Add a dedicated filter column to `choices` and reference it: `choice_filter = filter = 'all' or (filter = 'pm' and ${has_pm} = 1)`. The local `surveycto_checker.py` cannot validate `choice_filter`/`search()`/`pulldata()` semantics — only live test-view testing catches these.

**Dataset definitions as XML files:** a server dataset's full definition (columns, form attachments, publishing rules, case-management options) can be authored as an XML file and created via console → Design → "Upload dataset definition" — a reviewable, diffable alternative to clicking through the dataset builder. (For scripted live-server deploys, the reverse-engineered console API below — `create-new-dataset` + `publish/{ds}/configure` — is usually the better route; the XML file wins when you want the whole definition as a version-controlled artifact, e.g. case-management setups.) Before authoring one, read [`references/datasets-xml.md`](references/datasets-xml.md) (server-source-derived — more accurate than the public support article: strict element ordering in `<definition>` and `<dataLink>`, always emit `<formLinks/>`/`<dataLinks/>` even when empty, the wide-vs-long `*`-suffix rules where a wrong suffix silently publishes nothing). Then validate before uploading: `~/.venvs/lifecoach/bin/python3 "$SURVEYCTO_SKILL_DIR/assets/dataset-validation/validate_dataset.py" dataset.xml --form form.xlsx --json` ([`references/dataset-validation.md`](references/dataset-validation.md)). ⚠️ *Editing* an existing dataset via XML is destructive (download data → delete dataset → re-upload definition → re-upload CSV in Append mode) — mid-survey, prefer the console attach/CSV endpoints below.

**Console automation (reverse-engineered — call via `fetch`, same-origin + cookie; bypasses the flaky iCheck UI):**
- List datasets + full publish configs: `GET /console/datasets/get?t=<epoch-ms>` (header `X-csrf-token`) → array of `{id, title, status, uniqueRecordField, outgoingFormIds, dataLinkSummaries:[{linkType:"INCOMING", linkObjectId:<form_id>, joiningField, fieldMap:[...]}]}` — the ground truth for "which columns actually publish" (diff it against what a consuming form pulls; a missed column fails silently as empty string).
- Create a dataset: `POST /groups/{parentGroupId}/create-new-dataset` (root group = 1), **multipart** form-data with `dataset_id`, `dataset_title` (requests: `files={"dataset_id": (None, id), ...}`; plain `data=` fails with "request was not multipart"). Verify via the list endpoint (full Python flow: AI HEALTH `piloting/planning_info_test/questionnaires/DEPLOYMENT.md`, deployed 2026-08-11).
- Configure form→dataset publishing (the "publish into" wizard): `POST /forms/{formId}/publish/{datasetId}/configure?autoConfigureUniqueRecordField=true` (boolean — passing a field name 500s), JSON body `{"id": null, "objectId": <datasetId>, "linkType": "INCOMING", "linkClass": "FORM", "linkFormat": 0, "linkObjectId": <formId>, "joiningField": "<key_field>", "relevanceField": "", "publishExistingData": true, "fieldMap": [{"formField": f, "datasetField": f, "updateLogicAction": "REPLACE", "updateLogicOptions": {"separator": null, "position": null}}, ...]}` + `Content-Type: application/json` + `X-csrf-token`. `autoConfigureUniqueRecordField=true` sets the dataset's unique ID to the joining field. Success returns `columnsAdded`.
- Dataset settings (rename / unique ID / offline updates): `POST /datasets/{id}/save-dataset-settings?dataset_title=...&datasetUniqueRecordField=...&datasetAllowOfflineUpdates=...`.
- Attach/detach a dataset to a form: `POST /datasets/{datasetId}/attach/{formId}/{on|off}` (header `X-Requested-With: XMLHttpRequest`; 200 on success). Far more reliable than the attach checklist. Works before the dataset has any data; a form referencing a not-yet-attached dataset still uploads fine (console flags the missing attachment rather than rejecting).
- Endpoint discovery pattern when the console UI changes: the SPA's endpoints live in `/js/scto/console/*.js` and lazy-loaded `/modules/**` — from a logged-in console tab, `performance.getEntriesByType('resource')` lists them and a sync XHR + regex finds the `$.ajax` call sites (datasets CRUD in `console/datasets.js`, publish config in `console/export.js` `savePublishingConfig`).
- Upload CSV rows to a dataset: `POST /datasets/{datasetId}/upload?csrf_token=...` multipart with `dataset_file` (a CSV File/Blob), `dataset_upload_mode` ∈ `append|merge|clear`, `dataset_id`. From the browser, set the modal's `input[name=dataset_file]` via DataTransfer (a `new File([csv],name,{type:'text/csv'})` Blob) + dispatch `change`, then click the "Upload" submit — no native file picker needed.

**SurveyCTO skill Python on this Mac:** `/usr/local/bin/python3` does NOT exist, and bare `python3` can lack `pandas`, `browser_cookie3`, or the Google libraries. Run `surveycto_checker.py`, `surveycto_to_txt.py`, and `surveycto_upload.py` with `~/.venvs/lifecoach/bin/python3`; install any missing upload deps (`browser_cookie3 requests`) into that venv. The same venv runs the google-docs/drive/sheets/email scripts.

### Data API gotchas (wide-JSON endpoint, learned Aug 2026)

- Pull data with `GET /api/v2/forms/data/wide/json/{form_id}?date=0` (basic
  auth, API-enabled role). The `wide/csv` route WITHOUT a `date` parameter
  returns 404 even for forms that exist — indistinguishable from a missing
  form or a permission problem, so don't diagnose from the 404 alone.
- **417 = "export still being prepared", not an error.** First-ever API pulls
  and pulls right after new submissions 417 while the server builds the
  export; retry after ~15s (a few attempts) or keep exports warm with a
  periodic sync. Persistent 417 can also mean an encrypted form with no
  publishable fields.
- The JSON export renders `SubmissionDate` as "Aug 4, 2026 6:56:56 PM"
  (server time, UTC on kilongajfl) — normalize before comparing to ISO dates.
- Real (non-test) submissions can be created headlessly via OpenRosa: fetch
  the compiled XForm (`/forms/{id}/xml`, console cookie or basic auth) for the `version`
  attribute, then POST multipart `xml_submission_file` to `/submission` with
  an instance XML naming that id+version — returns 201. Console "Test" view
  submissions do NOT reach the data API.
- **417 "Please wait N seconds": full-history pulls (`?date=0`) are
  rate-limited per form (~15 min window).** Every early retry collides with
  the window again, so a tight retry loop (or several agents polling the same
  form) keeps the export perpetually "preparing". Stop touching the endpoint
  for the full stated wait; a scheduled sync will land it on a later run.
  Redeploying a form invalidates the export cache and restarts the cycle.
- **Deleting specific submissions:** Monitor tab → the form's "Purge form
  data" → "Purge specific submissions" → paste the `KEY` value(s)
  (comma-separated). "Purge by date" is the other, dangerous branch — never
  use it for single-record cleanup. Get the KEY from the console's "Look up
  by key" view or your own import records.
- **Anonymous web-form access is per form:** Collect tab → *Web data
  collection* section → the form's **Settings** ("Allow anonymous form
  access" toggle + Save); each row shows "Anonymous access: Yes/No". A form
  without it serves "This form is private" at its `/collect/` URL. When
  driving this UI programmatically, Bootstrap moves `title` into
  `data-original-title` after tooltip init — match both attributes.
