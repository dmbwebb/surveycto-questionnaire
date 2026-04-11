# surveycto-questionnaire

A Claude Code skill (with standalone CLI tools) for building, validating, and uploading XLSForm surveys for SurveyCTO, ODK, and KoboToolbox.

When loaded into [Claude Code](https://docs.claude.com/claude-code), this skill gives Claude the knowledge it needs to design XLSForm surveys, enforce validation rules, preserve Excel formatting, handle multi-language labels, and deploy forms to a SurveyCTO server — all from natural-language instructions.

## What it does

- **Design survey logic** — question types, skip logic, constraints, calculations, repeating groups, choice filters, cascading selects.
- **Validate XLSForms** — catches broken field references, undefined choice lists, expression syntax errors, duplicate names, missing translations, and ~20 other issues before you upload.
- **Preserve Excel formatting** — conditional formatting, red-text translation markers, cell styles.
- **Convert to readable text** — dump the whole form (labels, logic, choices) to plain text for review, sharing with non-technical collaborators, or diffing across versions.
- **Upload directly to SurveyCTO** — replaces a form definition in one command by reverse-engineering the web console's upload endpoint. No password handling — authenticates via your existing Chrome session cookie.

## Installation

### As a Claude Code skill (recommended)

Clone this repo into your Claude Code skills directory:

```bash
git clone https://github.com/dmbwebb/surveycto-questionnaire.git \
    ~/.claude/skills/surveycto-questionnaire
```

Set two environment variables in your shell profile (`~/.zshrc`, `~/.bashrc`, etc.):

```bash
export SURVEYCTO_SKILL_DIR="$HOME/.claude/skills/surveycto-questionnaire"
export SURVEYCTO_SERVER="your-server.surveycto.com"   # only needed for uploads
```

Install the Python dependencies (one-time, for the system Python):

```bash
python3 -m pip install --user openpyxl browser_cookie3 requests
```

Next time you use Claude Code, the skill will auto-activate whenever you mention surveys, XLSForms, SurveyCTO, questionnaires, skip logic, etc.

### As standalone CLI tools

You can also use the scripts directly without Claude. Clone anywhere and point `SURVEYCTO_SKILL_DIR` at the clone:

```bash
git clone https://github.com/dmbwebb/surveycto-questionnaire.git
export SURVEYCTO_SKILL_DIR="$PWD/surveycto-questionnaire"
```

## Quick examples (via Claude Code)

```
"Add a new integer question for household size after d1_age, with range 1–30 and a Hindi translation."

"Run the checker on survey.xlsx and fix any errors you find."

"Convert my_survey.xlsx to a plain text summary I can send to a collaborator."

"Upload survey.xlsx to SurveyCTO, replacing the existing ai_screening_main_v1 form."
```

Claude reads `SKILL.md`, follows its design rules and workflow, runs the CLI scripts as needed, and iterates on errors until the form is clean.

## CLI reference

All scripts live in `scripts/` and are invoked with `python3 "$SURVEYCTO_SKILL_DIR/scripts/<script>.py"`.

### `surveycto_checker.py` — validate a form

```bash
python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_checker.py" survey.xlsx
```

Runs 20+ checks including: broken `${field}` references, undefined choice lists, unbalanced expressions, duplicate names, empty groups, missing translations, conditional formatting preservation, and more. See [SKILL.md](./SKILL.md#checker-validations) for the full list.

### `surveycto_to_txt.py` — dump form to readable text

```bash
python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_to_txt.py" survey.xlsx [output.txt]
```

Flags: `--no-names`, `--no-relevance`, `--no-choices`, `--keep-html`.

### `surveycto_upload.py` — deploy form to SurveyCTO

Requires you to be logged into the SurveyCTO web console in Chrome's default profile (the script reads `JSESSIONID` from Chrome's cookie store — no password needed).

```bash
# Replace an existing form
python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_upload.py" \
    --update my_form_v1 \
    --media path/to/plugin.fieldplugin.zip \
    path/to/my_form_v1.xlsx

# Upload a new form
python3 "$SURVEYCTO_SKILL_DIR/scripts/surveycto_upload.py" path/to/new_form.xlsx
```

Pass the server via `--server` or `$SURVEYCTO_SERVER`. Full flag list and exit codes in [SKILL.md](./SKILL.md#usage).

### `recalc_excel.sh` — force-evaluate Excel formulas

```bash
bash "$SURVEYCTO_SKILL_DIR/scripts/recalc_excel.sh" survey.xlsx
```

Use before uploading if your `settings.version` is a `NOW()`-based formula — SurveyCTO doesn't evaluate Excel formulas at upload time.

## Requirements

- Python 3.9+
- Chrome (default profile) logged into SurveyCTO — only for the upload script
- macOS or Linux (the upload script uses `browser_cookie3`, which works on Windows too but is less tested)
- Python packages: `openpyxl`, `browser_cookie3`, `requests`

## Full reference

The complete design guide, validation rules, renaming workflow, multi-language handling, and deployment gotchas live in [**SKILL.md**](./SKILL.md). That file is also what Claude reads when the skill is invoked.

## License

MIT — see [LICENSE.txt](./LICENSE.txt).
