# Section Timing and Timestamps

Every SurveyCTO questionnaire should record how long each substantive section took and when the section began and ended. Apply this whenever creating, converting, or editing a form. The canonical pattern below is mandatory for new and undeployed forms. Deployed K2 forms with established cumulative-checkpoint pipelines use the explicit compatibility alternative below.

This reference is SurveyCTO-specific because it uses `calculate_here`. Do not apply it unchanged to a form that must remain compatible with ODK or Kobo; use a timing pattern verified for that platform instead.

## Canonical pattern for new forms

Put the timing rows inside the section's group. The start rows go immediately after `begin group`. The end rows, duration calculation, and then `end group` close the section.

```text
type             | name                       | calculation
begin group      | health                     |
calculate_here   | health_start_elapsed_sec    | once(duration())
calculate_here   | health_start_time           | once(format-date-time(now(), '%Y-%m-%dT%H:%M:%S'))
... section questions ...
calculate_here   | health_end_elapsed_sec      | once(duration())
calculate_here   | health_end_time             | once(format-date-time(now(), '%Y-%m-%dT%H:%M:%S'))
calculate        | health_duration_sec          | if(string-length(${health_end_elapsed_sec}) > 0 and string-length(${health_start_elapsed_sec}) > 0, round(${health_end_elapsed_sec} - ${health_start_elapsed_sec}, 0), '')
end group        | health                     |
```

Use literal names rather than Excel formulas that concatenate a group name. Formula-generated names are harder to audit and can appear blank to openpyxl-based checks after a save because formula caches are absent.

The elapsed checkpoints and wall-clock timestamps serve different purposes:

- `once(duration())` records cumulative seconds when the enumerator first reaches that row. Subtracting the start from the end gives a reliable section duration and does not overwrite the checkpoint when the enumerator navigates backward.
- `once(format-date-time(now(), '%Y-%m-%dT%H:%M:%S'))` records the local device clock time in a stable, sortable format. Do not store bare `now()`, which may retain only the date when used alone.
- The explicit guard around the subtraction is required. A skipped section leaves both checkpoints empty; subtracting empty values produces `NaN`, which can be exported as literal text.

Use elapsed checkpoints as the primary duration measure. Wall-clock timestamps use the device clock and can be affected if that clock changes during an interview.

## What counts as a section

Instrument every substantive interview module that analysts may want to assess separately. Also instrument a module with its own relevance or early-exit logic, even if it is short.

Do not automatically instrument every XLSForm group. Technical wrappers, `field-list` display groups, one-question layout groups, and helper groups are not separate sections unless their duration matters analytically. A short survey with one substantive module still gets one canonical section timer.

Use one non-overlapping analytical level by default. Time both a parent module and one of its nested modules only when both totals answer distinct analysis questions.

For a section containing repeats, put the required timers in an enclosing section group, before and after the repeat. This records the duration of the whole module. Add timing fields inside the repeat only when per-instance duration is an explicit requirement; those values will be repeated data.

## Relevance and early exits

For new forms, put the section gate on the enclosing substantive group and keep all five timing rows inside it. They then inherit the group's relevance:

- when the section is shown, both boundaries and the duration populate;
- when the section is skipped, all timing fields remain blank;
- the duration calculation never turns a skipped path into `NaN`.

If an undeployed form has an ungrouped section, either wrap it in a named group without changing its substantive logic, or repeat exactly the same relevance on every timing row. A group is safer because one later relevance edit cannot leave the timing rows inconsistent. If an existing group is always relevant but all substantive questions share a separate gate, define one shared section-shown calculation and apply it to all five timing rows; otherwise an entirely skipped module records a misleading near-zero duration.

When a section has several legitimate exit branches, place its end rows at the common point that every completed branch reaches. Do not put the end marker inside only one branch.

## Overall interview timing

Every form also needs survey-level start and end timestamps and an overall duration checkpoint. Keep them outside all section groups and without relevance.

```text
type             | name                   | calculation
calculate_here   | survey_start_elapsed_sec | once(duration())
calculate_here   | survey_start_time      | once(format-date-time(now(), '%Y-%m-%dT%H:%M:%S'))
... questionnaire sections ...
calculate_here   | survey_end_elapsed_sec | once(duration())
calculate_here   | survey_end_time        | once(format-date-time(now(), '%Y-%m-%dT%H:%M:%S'))
calculate        | overall_duration_sec   | if(string-length(${survey_end_elapsed_sec}) > 0 and string-length(${survey_start_elapsed_sec}) > 0, round(${survey_end_elapsed_sec} - ${survey_start_elapsed_sec}, 0), '')
end              | end                    |
```

Put `survey_start_elapsed_sec` and `survey_start_time` at the point where the interview itself begins, after any device metadata that should not count as interview work. Put the three end rows on the last active rows before the `end` metadata field. This lets refusals and shortened interviews record their final time and duration after all later questions have been skipped. Keep the standard `start` and `end` metadata too; form open and finalisation times answer a different question.

Never calculate overall duration by subtracting the form's `start` metadata from its `end` metadata in a plain `calculate`. SurveyCTO stamps `end` at finalisation and does not re-evaluate ordinary calculations then, so that field exports empty.

## Deployed forms and the K2 compatibility alternative

Before adding timing to an existing form, determine whether it is deployed, whether it has submissions, which convention it already uses, and whether downstream code depends on its names or export order. For a deployed form:

- add only new fields within existing group paths;
- never move, rename, regroup, or wrap existing collected fields;
- preserve the established timing convention and names;
- state the schema expansion in the edit plan and final recap;
- ask before adding fields if downstream expectations are unknown.

Existing K2 forms commonly store one cumulative pair immediately after each substantive section:

```text
type             | name                   | calculation
end group        | health                 |
calculate_here   | duration_health        | once(duration())
calculate_here   | time_health            | once(format-date-time(now(), '%Y-%b-%e %H:%M:%S'))
```

The `duration_*` value is cumulative seconds since the form began, not the duration of that section. Existing K2 analysis subtracts consecutive checkpoints in export order. This compatibility alternative takes precedence over the canonical five-field rule whenever an existing form or cross-wave pipeline depends on it. Add missing K2 coverage in the same convention, and preserve the existing relevance semantics for both members of each pair. Use the self-contained canonical pattern for new forms because it does not depend on the preceding section, export column order, or an unskipped prior checkpoint.

## Optional field-level audit

A `text audit` field with `appearance = eventlog` records per-field navigation and timing. Add it when fine-grained diagnostics are useful, but keep the section timers. The event log is harder to analyze and does not replace clean module-level duration columns.

## Verification

After adding or changing canonical timing rows:

1. Run `surveycto_checker.py` on the form.
2. Test one path that completes every section and confirm that start time, end time, and duration populate for each section.
3. Test a path that skips at least one section and confirm that all five fields for that section are blank, not `NaN`.
4. Navigate backward into one completed section and confirm that `once(...)` preserves the original checkpoints.
5. Confirm that both survey elapsed checkpoints, both survey timestamps, and `overall_duration_sec` populate on a refusal or shortened-interview path.
6. Record a timing inventory listing every substantive section and its timing fields. Do not declare the questionnaire complete while a section lacks coverage or a documented compatibility exception.

For the K2 compatibility alternative, confirm that every substantive module has a complete `duration_*`/`time_*` pair, both fields use `once(...)`, formula-generated names resolve to the intended literal names, relevance matches the established form convention, and downstream checkpoint order remains unchanged.

## Why this is the default

The pattern combines the strongest parts of questionnaires already used across projects:

- K2 baseline and endline forms pair cumulative `duration()` checkpoints with absolute `now()` timestamps, usually at group boundaries.
- AI Health baseline and endline forms use cumulative `timing_*` checkpoints at section boundaries.
- The Status Quo Bias recruiter form records start and end checkpoints inside each section and calculates a guarded section duration. This is the safest structure for skipped modules.

The required pattern adds literal field names and both wall-clock boundaries to the Status Quo Bias structure. It keeps timing self-contained within each section and avoids the formula-name and skipped-section weaknesses found in older forms.
