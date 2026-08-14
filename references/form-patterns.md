# Form-Design Patterns for SurveyCTO

Home-grown reusable XLSForm recipes that don't fit the vendored references. Each pattern: when to use → structure → gotchas.

---

## Select from earlier answers (in-form nomination dedup)

**When to use:** a later question should offer the respondent's OWN earlier free-text answers as choices — e.g. name-nomination rounds where round 2 ("who would be best at spreading information?") should let the enumerator pick people already named in round 1 instead of retyping them. Kills any manual-deduplication step: each unique person is entered exactly once, and later rounds reference them by selection.

**Structure:**

1. **Fixed text slots, not a repeat.** Round 1 collects up to N names in fixed fields (`name_1`, `name_2`, `name_3`), each relevant on the previous being non-empty:

   | type | name | label | relevance |
   |---|---|---|---|
   | text | nom_a_name_1 | First person — name | |
   | text | nom_a_name_2 | Second person — name (if any) | `${nom_a_name_1} != ''` |
   | text | nom_a_name_3 | Third person — name (if any) | `${nom_a_name_2} != ''` |

2. **A choice list whose labels are field references,** with a `filter` column, plus fixed `new` / `none` options:

   | list_name | value | label | filter |
   |---|---|---|---|
   | prior_names_b | a1 | `${nom_a_name_1}` | a1 |
   | prior_names_b | a2 | `${nom_a_name_2}` | a2 |
   | prior_names_b | a3 | `${nom_a_name_3}` | a3 |
   | prior_names_b | new | Someone else (enter the name below) | fixed |
   | prior_names_b | none | No one / cannot say | fixed |

3. **Round 2 is a `select_multiple` over that list,** with a `choice_filter` hiding empty slots and its own new-name slots gated on selecting `new`:

   ```
   choice_filter = filter = 'fixed' or (filter = 'a1' and ${nom_a_name_1} != '')
                   or (filter = 'a2' and ${nom_a_name_2} != '')
                   or (filter = 'a3' and ${nom_a_name_3} != '')
   constraint    = not(selected(., 'none') and count-selected(.) > 1)
   ```

   Round 3 repeats the trick with a longer list covering round-1 AND round-2 new-name slots.

4. **Per-person follow-ups are fixed field-list blocks,** one per name slot, each relevant on that slot's name being non-empty (`begin group` with `relevance = ${nom_a_name_1} != ''`, label `About ${nom_a_name_1}`). Role/flag variables are derived afterwards with calculates over the `selected()` sets, e.g. `concat('broker', if(selected(${round2_pick}, 'a1'), ' spreader', ''))`.

**Gotchas:**

- Fixed slots beat a `begin repeat` here — repeat answers cannot feed a choice list without `indexed-repeat()` calculates for every slot, which lands you in the same fixed-slot layout with extra plumbing.
- The local `surveycto_checker.py` cannot validate `choice_filter` semantics — a broken filter silently empties the select. Device/web-test the flow before deploying.
- Give `new` and `none` exclusivity constraints or enumerators will combine them with real picks.
- Keep slot values (`a1`, `s2`, ...) short and stable: downstream calculates reference them literally.

**Worked example:** STATUS QUO BIAS repo, `background/scoping/questionnaires/build_sqb_pilot_recruiter_v2.py` (Phase 4) — three nomination rounds, nine detail blocks, role tags derived per slot.
