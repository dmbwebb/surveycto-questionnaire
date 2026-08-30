# References

`randomization-patterns.md`, `form-patterns.md`, and `timing.md` are home-grown (RCT randomization recipes, general form-design patterns, and the cross-project timing convention for this skill).

Everything else in this directory, plus the sibling `assets/` directory (XLSForm template,
field-plugin test harness and template, dataset validator, CommCare loader), is vendored from
the official SurveyCTO agent skill:

- Source: https://github.com/surveycto/surveycto-agent-skill
- Vendored version: 1.0.0-beta.8 (copied 2026-08-12)
- License: Apache-2.0 — see `LICENSE-official-surveycto-skill.txt` in this directory

To refresh: clone the repo, re-copy `references/*.md` and the `assets/` subtrees listed above,
and update the version line here. Watch the repo's `.gitignore`: it ignores `*.xlsx`, so any
new xlsx asset needs its own `!`-exception (assets/xlsform-template.xlsx already has one —
its absence from a commit is silent otherwise; bit us 2026-08-12) — after committing, confirm
with `git ls-files assets/`. Cross-links between these files (and `../assets/...` links)
assume the official layout, so keep filenames and the flat `references/` + root `assets/`
structure intact. `mcp.md` refers to "SKILL.md" meaning the *official* skill's SKILL.md, not
ours; only its tool/endpoint documentation matters here.

Intentionally NOT vendored: the official SKILL.md (its workflow assumes the SurveyCTO MCP
server and no local tooling — our checker/upload/gsheet pipeline supersedes it), and its CI
scripts/tests.
