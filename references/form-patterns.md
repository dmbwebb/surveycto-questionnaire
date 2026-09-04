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

---

## Audio audits: segment long recordings

**When to use:** any SurveyCTO form long enough that enumerators sometimes save it part-way and finish it later. That is nearly every household baseline. A single whole-survey audio audit loses those interviews entirely.

### The mechanism

An `audio audit` field records invisibly while the enumerator works through the form. Its appearance holds semicolon-separated parameters:

- `p=#` is the percent of submissions sampled. It makes the audit time-based.
- `s=#` starts recording at a fixed number of seconds, `s=#-#` picks a random start in that range, and `s=fieldname` starts when the enumerator reaches that field.
- `d=#` records for that many seconds, and `d=fieldname` stops when the enumerator swipes away from that field.
- With no parameters at all, SurveyCTO records the first five minutes of every survey.

The trap is documented in SurveyCTO's support article on missing recordings: an audio audit **does not resume when a partially completed form is reopened through Edit Saved Form**. The recording stops at the pause and never restarts. With one audit anchored at a consent field near the top of the form, an interview that is parked and resumed produces no audio at all.

### The measured evidence

Checked directly on three long AI Health baseline forms in Rajasthan on 3 September 2026. Each carried a single audit with `p=100;s=audio_consent;d=gps`:

- Between 23 and 45 percent of consenting interviews had no recording whatsoever.
- Of those missing-audio interviews, 97 to 100 percent showed more than five minutes of wall-clock time that active form time could not explain, with a median idle gap of 22 to 26 minutes. That is the signature of a parked and resumed form.
- Among interviews that did have audio, only 12 to 17 percent showed such a gap.
- Device, form version, enumerator, month and interview length explained none of the variation.
- Short forms in the same project, 4 to 16 minutes long, captured 100 percent.

### The segmentation pattern

Split the form into several field-anchored audits, one per section. Multiple question-based audits are not mutually exclusive: all of them fire, and overlaps are allowed, producing duplicate audio rather than a conflict.

1. Choose one anchor field per section: the first visible, unconditional, non-repeat field of that section. Call them A1 to An.
2. Segment k runs `s=A_k;d=A_{k+1}`. Consecutive segments overlap by exactly one screen, so coverage has no gaps even when a section ends in conditional fields that some respondents skip.
3. The last segment ends at the final visible field of the form, typically the `gps` row.
4. Give every segment the same relevance as the consent that authorises recording, for example `${audio_consent} = 1`.

A three-segment example, with all the audit rows sitting together near the top of the survey sheet just after the `duration` calculate:

| type | name | label | appearance | relevance |
|---|---|---|---|---|
| audio audit | audit | | `s=audio_consent;d=health_intro` | `${audio_consent} = 1` |
| audio audit | audit_02 | | `s=health_intro;d=assets_intro` | `${audio_consent} = 1` |
| audio audit | audit_03 | | `s=assets_intro;d=gps` | `${audio_consent} = 1` |

### Anchor rules

- Anchors must be **visible** fields. A `calculate`, a `calculate_here`, a `begin group` row or a metadata row silently breaks the audit.
- Anchors cannot sit inside a repeat group, and an `audio audit` field does not function inside a repeat group either.
- Write the bare field name in the appearance. `s=${audio_consent}` is wrong; `s=audio_consent` is right.

### The `p=` trap

Mutual exclusion applies only to time-based audits, meaning those using `p=`. At most one of them fires per submission, and if the first has `p=100` no other time-based audit ever triggers. Leaving `p=100` on segmented rows therefore risks collapsing the whole scheme back to one recording. Drop `p=` from field-anchored segments so each row is unambiguously question-based. To sample a subset of interviews instead, gate the segments on a relevance expression rather than on `p=`.

### Naming and export consequences

- Name the rows `audit`, `audit_02`, `audit_03` and so on, and keep them adjacent in the survey sheet so the scheme is legible at a glance.
- The wide export gains one column per audit field, each holding that segment's attachment URL. Downstream code that used to read a single `audit` column must gather every `audit*` column instead, and an interview's audio is now several files rather than one.

### Remaining caveat

Segmentation does not recover everything. The segment that was in progress when the enumerator paused is still lost, because that specific audit does not resume. What changes is the worst case: instead of losing the entire interview, you lose one section of it, and every later section still records.

**Checker support:** `surveycto_checker.py` errors on anchors that name a missing, invisible or in-repeat field, on `${...}` anchor syntax, and on an audit row inside a repeat. It warns when a form's only audio audit spans more than half the visible fields or records more than 900 seconds in one stretch, and when `p=` is combined with a field anchor.

