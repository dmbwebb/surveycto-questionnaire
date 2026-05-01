---
name: surveycto-questionnaire
description: "Create, edit, validate, inspect, convert, and upload XLSForm surveys for SurveyCTO, ODK, or Kobo. Supports both .xlsx files and Google Sheets — for gsheet-backed forms, reads via auto-export to a temp xlsx and writes directly to the sheet via the Sheets API."
---
# XLSForm Survey Design and Excel Editing

## Working with Google Sheets-backed forms

When the form lives as a Google Sheet (the K2 baseline/midline/endline pattern), the canonical workflow is:

- **Reads** (validate, inspect, convert-to-text): export the gsheet to a local temp xlsx via `gsheet_io.exported_xlsx(doc_id)`, then run the existing `surveycto_checker.py` / `surveycto_to_txt.py` against it. The gsheet stays the source of truth; the xlsx is a transient build artifact. Drive's xlsx export materialises formulas (so `settings.version` cells with `NOW()`-based formulas come out evaluated), so no `recalc_excel.sh` step is needed.
- **Writes** (edit a label, rename a variable, add a choice list, mark a translation as red): use `gsheet_edit.py` against the live Sheet via the Sheets API. Do not download → edit xlsx → re-upload — co-authors editing simultaneously would lose work.
- **Upload to SurveyCTO**: use `surveycto_upload.py --from-gsheet <doc_id_or_pointer>`. It exports the gsheet to a temp xlsx and runs the normal CSRF/cookie upload pipeline. Works alongside `--update <form_id>` and `--media`.

`gsheet_io.resolve_to_doc_id` accepts either a raw Drive `doc_id` or a path to a `.gsheet` pointer file (the JSON stub Drive Desktop drops on the local FS), so CLI users can pass familiar paths.

### Edit primitives in `gsheet_edit.py`

| Function | Purpose |
|---|---|
| `open_tab(doc_id, tab_title)` | Cache header layout + sheet_id for a tab |
| `find_row_by_value(tab, header, value)` | Find row number by `name` (or any column) |
| `get_cell` / `update_cell` | Single-cell read/write (USER_ENTERED parsing) |
| `update_cell_checked` | Compare-and-swap: writes only if current value matches expected — best-effort guard against concurrent edits |
| `batch_update_cells(tab, edits)` | Write many cells in one API call; `edits` is `[(row, header, value), ...]`. No CAS — verify preconditions via a single bulk read. Retries on 429. |
| `bulk_set_column(tab, rows, header, value)` | One-call sugar over `batch_update_cells` for setting the same value across many rows of one column (the "disable a module" pattern). |
| `append_row(tab, row_dict)` | Append to bottom; returns landed row |
| `insert_row_at(tab, position, row_dict)` | Insert mid-tab, shifting rows down — preserves group structure |
| `delete_row(tab, row)` | Remove a row (rejects header row 1) |
| `rename_variable(tab, old, new)` | Rename in the `name` column AND every `${...}` reference in relevance/constraint/calculation/label/etc. — single-batch round-trip |
| `add_choice_list(doc_id, list_name, choices)` | Append a choice list; auto-detects whether the choices tab uses `name` or `value` (XLSForm allows either) |
| `set_text_color` / `get_text_color` | Foreground (text) color — for translation-status semantics |
| `gsheet_io.get_drive_modified_time` / `get_drive_version` | Whole-file change sentinels (note: propagation can lag 30s+ — use `update_cell_checked` for tight loops) |

### Concurrency, rate limits, row shifts

When the user (or another concurrent agent) is editing the gsheet alongside you, three things bite:

- **Sheets API rate limit is 60 reads/min/user.** Per-cell `update_cell_checked` does ~2 calls per cell (one read, one write); 30+ cells in a tight loop will hit HTTP 429. Use `batch_update_cells(tab, edits)` (or the `bulk_set_column` shortcut) for bulk writes — one API call covers all rows, with built-in 429 retry. Read once via the exported xlsx (no API hit) to confirm preconditions, then batch-write.
- **Row numbers shift under you.** If you scan via `gsheet_io.exported_xlsx(doc_id)` and then write minutes later, a concurrent insertion above your target rows will shift everything down, and your row-number-keyed writes go to the wrong rows. The cell values *do* move with the content under insert/delete, so writes you already made stay correct — but verifications using stale row numbers will look broken even when the live state is fine. For risky write batches, prefer `find_row_by_value(tab, 'name', '<field_name>')` over hard-coded row numbers, and verify by name (not row) afterward.
- **Disabling a field requires updating its callers.** Before setting `disabled = yes`, grep the survey for `${field_name}` references in `relevance`, `constraint`, `calculation`, `label`, `choice_filter`, `repeat_count`, `required` (and the choices sheet). Either drop those refs or update the formulas — leaving them produces "field reference to non-existent field" errors at upload. Same applies to renames (already handled by `rename_variable`, but only it).

### Translation status convention (K2)

K2 forms use an explicit `a_traduire` column in the survey/choices tabs to flag rows that still need Malagasy translation. **That is the canonical mechanism for this project.** Don't add red-text-on-Malagasy as a parallel channel — `a_traduire` is enough.

(The Hindi-style red-text rule documented elsewhere in this SKILL is for projects that don't have an `a_traduire` column. It still works mechanically — the `set_text_color` primitive is in place — but for K2 forms, prefer the column.)

### Tests

Live Google Sheets tests live in `tests/` and run against persistent Drive copies listed in local-only `tests/fixture_ids.json`. Start from `tests/fixture_ids.example.json`; the real fixture file is gitignored because it contains private Drive IDs. Each destructive test makes its own ephemeral copy and trashes it on exit, so fixtures themselves stay clean.

```bash
cd ~/.claude/skills/surveycto-questionnaire
cp tests/fixture_ids.example.json tests/fixture_ids.json  # then fill in real Drive IDs
PYTHONPATH=scripts ~/.venvs/mada-gsheet-tests/bin/pytest tests/ -v -c tests/pytest.ini
```

Currently 25 passing tests (read flow + write flow + multi-tab edits + insert-at-position + concurrent-edit detection + delete + foreground color round-trip + batch writes).



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

### Checker Validations

The checker (`surveycto_checker.py`) performs these checks:

| Check | Type | What it catches |
|-------|------|-----------------|
| Required columns | Error | Missing `type`, `name` columns |
| Blank/missing names | Error | Rows with a type but blank/whitespace-only name |
| Duplicate names | Error | Two fields with the same name |
| Empty groups | Error | Groups/repeats with no enabled children (all disabled) |
| Expression syntax | Error | Unbalanced parentheses, unclosed `${}`, unclosed quotes |
| Field references | Error | `${field_name}` pointing to non-existent fields |
| Choice list references | Error | `select_one`/`select_multiple` referencing undefined lists |
| Choices field references | Error | `${field}` in choice labels pointing to non-existent fields |
| Calculate fields | Error | Calculate type with empty calculation formula |
| Other specify fields | Warning | 'Other (specify)' choice without follow-up text field |
| select_multiple other | Warning | select_multiple with 'other' but no specify field |
| Exclusive options | Warning | select_multiple missing constraints for exclusive options (-97, -98) |
| Required fields | Warning | Questions without `required=yes` |
| Typos | Warning | Common misspellings in field names/labels |
| Constraint messages | Warning | Fields with constraints but no error message |
| Integer constraints | Warning | Integer fields without range validation |
| Numeric refuse (-999) | Warning | Numeric fields without -999 refuse option |
| Hindi translations | Warning | Questions missing `label:Hindi` |
| Naming conventions | Warning | camelCase, dots, spaces, uppercase in field names |
| Conditional formatting | Error | Type-based color coding rules removed from survey sheet |
| Cell formatting | Warning | Red text (unverified translations) removed |
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

No password handling, no API token, no `--cookie` flag required as long as the user is logged in to Chrome. (If they aren't, fall back to `--cookie 'JSESSIONID=...; _uid=...'` or `$SURVEYCTO_COOKIE`.)

### Usage

```bash
# Replace an existing form (most common case — pair with media files)
python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_upload.py" \
    --update ai_screening_main_v1 \
    --media path/to/plugin.fieldplugin.zip \
    path/to/ai_screening_main_v1.xlsx

# Upload a NEW form (appends to root group)
python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_upload.py" \
    path/to/new_form.xlsx

# Multiple media files
python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_upload.py" \
    -u my_form -m a.zip -m b.png -m choices.csv path/to/form.xlsx

# Override server for a single run (normally read from $SURVEYCTO_SERVER)
python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_upload.py" \
    --server other-server.surveycto.com \
    path/to/form.xlsx

# Dry run (auth + csrf check + plan, no upload)
python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_upload.py" \
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

- **Version bump rule.** SurveyCTO refuses to replace a form unless `settings.version` in the new xlsx is **lexically greater** than the deployed version. The CLI passes the server's exact error message through (e.g. `you can't change the form attachments without also increasing the version number ... lexically greater than the previous one (2026040705)`). Bump the version in the `settings` sheet of the xlsx and retry. The convention used in this project is `YYYYMMDDNN` (e.g. `2026040801`). If you wrote the version as a `NOW()`-based formula, run `recalc_excel.sh` to evaluate it before uploading — SurveyCTO does not evaluate Excel formulas.
- **Session expired.** If you see `error: Authentication failed (HTTP 403)`, log into the SurveyCTO console in Chrome again (the JSESSIONID has expired) and retry.
- **Wrong Chrome profile.** `browser_cookie3.chrome()` reads the **default** Chrome profile. If the user is logged into SurveyCTO in a non-default profile, pass cookies explicitly with `--cookie` or set `$SURVEYCTO_COOKIE`.
- **Group ID for non-root uploads.** `--parent-group-id` defaults to `1` (root group). If the user wants the form inside a specific group, find that group's id by inspecting the SurveyCTO Design page (or just upload to root and let the user drag it).
- **The Chrome MCP cannot do native file picker uploads** (tracked in `~/.claude/chrome-extension.md`) — that's the whole reason this CLI exists. Don't fall back to clicking the upload button via the browser extension; use this script instead.

---

Create and edit XLSForm surveys in Excel format for mobile data collection platforms (SurveyCTO, ODK, KoboToolbox).

## Quick Start Workflow

### Creating New XLSForm Survey

```python
from openpyxl import Workbook
from openpyxl.styles import Font

wb = Workbook()

# Create survey sheet
survey = wb.active
survey.title = 'survey'
survey.append(['type', 'name', 'label', 'required', 'relevance', 'constraint', 'calculation'])
survey.append(['text', 'respondent_name', 'What is your name?', 'yes'])
survey.append(['integer', 'age', 'What is your age?', 'yes', '', '. >= 0 and . <= 120'])

# Create choices sheet
choices = wb.create_sheet('choices')
choices.append(['list_name', 'name', 'label'])
choices.append(['yes_no', '1', 'Yes'])
choices.append(['yes_no', '0', 'No'])

# Create settings sheet
settings = wb.create_sheet('settings')
settings.append(['form_title', 'form_id', 'version'])
settings.append(['My Survey 2025', 'my_survey_v1', '=TEXT(YEAR(NOW())-2000+2, "00") & TEXT(MONTH(NOW()), "00") & TEXT(DAY(NOW()), "00") & TEXT(HOUR(NOW()), "00") & TEXT(MINUTE(NOW()), "00")'])

# Bold headers
for sheet in wb:
    for cell in sheet[1]:
        cell.font = Font(bold=True)

wb.save('survey.xlsx')
```

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
- `disabled` - When set to `yes`, the question is completely excluded from the survey. **Treat disabled questions as if they don't exist** - they won't appear in the form, won't collect data, and should be ignored when analyzing survey structure or adding new questions. Disabled rows are kept in the Excel file for reference but are functionally removed from the active survey.

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
integer         | child_age      | Age of child ${position}   |
text            | child_name     | Name of child ${position}  |
end repeat      | child_roster   |                            |
```

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
index()                 - Position (1-indexed)
position(..)            - Position (0-indexed)
```

### Multi-language Surveys

Use language suffixes:

- `label::English`, `label::Hindi`, `label::Swahili`
- `hint::English`, `hint::Hindi`

Set `default_language = English` in settings sheet.

### Randomization

For RCT-style randomization (choice-order shuffling, A/B arm assignment, counterbalancing block order, list/item-count experiments, pre-randomized lists via `pulldata`, etc.) see [`references/randomization-patterns.md`](references/randomization-patterns.md). Covers 9 transferable patterns with XLSForm rows + gotchas.

Quickest hits:
- Shuffle choices on one select with reproducible seed + pinned "Other": `appearance: randomized(${respondent_id}, 0, 2)` — args are `(seed, top_excluded, bottom_excluded)`, not `(seed, min, max)`.
- Stable A/B switch: `calculate` with `once(random())`, then `if(${draw} > 0.5, 'a', 'b')`. Never put `random()` directly in `relevance`.
- Random order of N items per respondent: pre-randomize externally, save as `;`-separated string, pull with `pulldata`, unpack via `item-at(';', list, index() - 1)` inside a `begin repeat`.

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

1. **Unsupported functions:** SurveyCTO doesn't support `starts-with()`, `contains()`, `substring-before()`, or `substring-after()` — even though [ODK docs](https://docs.getodk.org/form-operators-functions/) list the latter two, SurveyCTO's JavaRosa parser rejects them with "cannot handle function 'substring-after'". Use `substr(string, 0, N) = 'prefix'` and `regex(string, '.*pattern.*')` for matching, and `selected-at(string, N)` (zero-indexed, space-separated) for extraction. **When in doubt, check [SurveyCTO's expressions reference](https://docs.surveycto.com/02-designing-forms/01-core-concepts/09.expressions.html), NOT the ODK docs** — SurveyCTO's XPath function set is a strict subset of ODK's.

2. **Extracting structured data from a field plug-in's output:** Use the `plug-in-metadata()` + `selected-at()` pattern. The plug-in calls `setMetaData(spaceSeparatedString)` (where any value containing spaces has its spaces replaced with `_`), and a calculate field then reads `selected-at(plug-in-metadata(${plugin_field}), N)` for the Nth value (zero-indexed). Direct JSON parsing in the form is impossible — there's no `substring-before/after`, `regex-replace`, or JSON parser.
1. **Duplicate names:** Each question needs unique name
2. **Missing list_name:** select questions must reference existing choice list
3. **Syntax errors:** Check parentheses, quotes, operators in logic
4. **Invalid references:** `${field_name}` must exist
5. **Circular references:** Field A can't use Field B if B uses Field A
6. **Invalid characters:** Names must be letters, numbers, underscores only
7. **Mismatched tags:** Every `begin group` needs `end group`

## Resources

- **XLSForm specification:** https://xlsform.org
- **SurveyCTO documentation:** https://docs.surveycto.com
- **ODK documentation:** https://docs.getodk.org
- **XLSForm validation:** https://getodk.org/xlsform/

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
