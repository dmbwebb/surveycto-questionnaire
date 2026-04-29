# Randomization Patterns for SurveyCTO

Reusable XLSForm recipes for randomization in SurveyCTO. Distilled from real RCT questionnaires + SurveyCTO docs. Each pattern: when to use → XLSForm rows → gotchas.

For function reference, see [SurveyCTO expressions](https://docs.surveycto.com/02-designing-forms/01-core-concepts/09.expressions.html) and [Randomizing survey elements](https://docs.surveycto.com/02-designing-forms/03-advanced-topics/01.randomizing.html). This doc covers **the form side only** — pre-randomized lists are loaded via `pulldata` from a CSV that you generate externally (R/Python/Stata).

---

## Core primitives

| Primitive | What it does | Notes |
|---|---|---|
| `random()` | Uniform draw in `[0, 1)` | **Re-fires on every recalculation** — never use bare. Always wrap in `once()`. |
| `once(<expr>)` | Calculates `<expr>` once per form-instance, then freezes it | Survives save/restore/edit. Put on a `calculate` field. |
| `appearance: randomized` (modifier on `select_one`/`select_multiple`) | Fisher-Yates shuffle of choice order | Stable per form instance. Without seed, drawn once at form open. |
| `appearance: randomized(seed)` | Seeded shuffle (reproducible across opens) | `seed` = integer or `${field}` reference (e.g. `${hhid}`). |
| `appearance: randomized(seed, top_n, bottom_n)` | Seeded shuffle, keep first `top_n` and last `bottom_n` choices fixed | Useful for pinning "Other (specify)" / "Don't know" at the bottom. **Argument order is `(seed, top_excluded, bottom_excluded)` — NOT `(seed, min, max)`.** |
| `pulldata('file', 'col', 'key_col', ${id})` | Look up a pre-computed value from an attached CSV | Used to inject per-respondent randomization computed externally. |
| `item-at(';', list, n)` | Get index `n` from a `;`-delimited string | Zero-indexed. Pair with `index() - 1` inside a repeat to walk the list. |
| `item-index(';', list, value)` | Reverse-lookup: position of `value` in delimited list | Zero-indexed. Add 1 if you want a 1-based position. |
| `indexed-repeat(${f}, ${repeat}, n)` | Read field `f` from iteration `n` of a repeat group, from outside the repeat | Use to expose pre-randomized order to downstream questions. |
| `choice-label(${field}, value)` | Look up the label for a choice value | Modern equivalent of `jr:choice-name`. |

---

## Pattern 1 — Randomize choice order on a single select question

**When:** Mitigate primacy/recency effects on a Likert or multiple-choice question. The randomization should be reproducible per respondent (so you can audit / replay) and pin special choices like "Other" or "Refuse" to the bottom.

**XLSForm:**

| type | name | label | appearance |
|---|---|---|---|
| `select_one provider_kind` | `q1_provider` | Q1: Which provider did you visit? | `randomized(${respondent_id}, 0, 2)` |

`(${respondent_id}, 0, 2)` = use `respondent_id` as seed, keep first 0 fixed, keep last 2 fixed. Put "Other (specify)" and "Don't know" as the last two rows of the choice list.

**Gotchas:**
- The argument is `top_excluded, bottom_excluded`, not `min_idx, max_idx`. Easy to misread.
- Without a seed, the shuffle is per-form-instance — you cannot reproduce it post-hoc unless you also enable a `text_audit` with `choices` in its appearance, which logs the displayed order. Recommended for any study where order effects matter.
- The form designer GUI's "Auto-randomize choice order" tickbox writes `randomized` only — for seed/exclusion variants, type into the appearance cell directly.

---

## Pattern 2 — Stable A/B Bernoulli switch

**When:** Two-arm randomization of anything (which video plays first, which photo is "worker A", which question wording).

**XLSForm:**

| type | name | calculation |
|---|---|---|
| `calculate` | `arm_draw` | `once(random())` |
| `calculate` | `arm` | `if(${arm_draw} > 0.5, 'a', 'b')` |

Then use `${arm}` in `relevance` / `label` / branching `if()` downstream.

**Gotchas:**
- **Never** `relevance: random() > 0.5` — relevance recalculates every keystroke, so the value flips constantly. Always go through a `once(random())` calculate field.
- Bare `once(random() > 0.5)` works too and gives you a `0/1` flag in one step.
- For unequal probabilities: `if(${arm_draw} > 0.7, 'control', 'treatment')` for a 70/30 split.

---

## Pattern 3 — Counterbalance order of two blocks (within-subject)

**When:** Each respondent answers about two stimuli (X then Y, or Y then X), and you want to randomize which comes first while keeping the rest of the form structure identical.

**XLSForm:**

| type | name | calculation | repeat_count |
|---|---|---|---|
| `calculate` | `which_first` | `once(random())` | |
| `calculate` | `block_a_first` | `if(${which_first} > 0.5, 'a', 'b')` | |
| `begin repeat` | `block_repeat` | | `2` |
| `calculate` | `block_id` | `if(index()=1, if(${block_a_first}='a','a','b'), if(${block_a_first}='a','b','a'))` | |
| `calculate` | `stimulus_name` | `if(${block_id}='a', ${name_a}, ${name_b})` | |
| `note` | `intro` | `Now let's talk about ${stimulus_name}.` | |
| ... questions about ${stimulus_name} ... | | | |
| `end repeat` | `block_repeat` | | |

The same question rows fire twice; on iteration 1 they reference whichever block was chosen first, on iteration 2 the other.

**Variant — counterbalance 4 questions (2 about A, 2 about B):**

```
calculation: if(index()=1 or index()=2,
                 if(${which_first}='a','a','b'),
                 if(index()=3 or index()=4,
                    if(${which_first}='a','b','a'),
                    'error'))
```

**Gotchas:**
- The `'error'` fallback isn't paranoia — if `index()` ever returns something unexpected (e.g. a malformed repeat), you'll see `'error'` in the data and know exactly where it leaked from.
- Pair with `indexed-repeat(${stimulus_name}, ${block_repeat}, 1)` in a downstream question if you need to refer back to "the first block" by name.

---

## Pattern 4 — Swap two stimuli (no repeat)

**When:** Two stimuli are shown side by side (e.g. two photos, two prices) and you want to randomize which is on the left/right.

**XLSForm:**

| type | name | calculation |
|---|---|---|
| `calculate` | `photo_1_raw` | `pulldata('respondents', 'photo_1', 'id', ${id})` |
| `calculate` | `photo_2_raw` | `pulldata('respondents', 'photo_2', 'id', ${id})` |
| `calculate` | `swap` | `once(random() > 0.5)` |
| `calculate` | `photo_left` | `if(${swap}=1, ${photo_2_raw}, ${photo_1_raw})` |
| `calculate` | `photo_right` | `if(${swap}=1, ${photo_1_raw}, ${photo_2_raw})` |

Use `${photo_left}` / `${photo_right}` in the question labels. Save `${swap}` so post-hoc analysis can recover which stimulus was on which side.

---

## Pattern 5 — Pre-randomized list per respondent (canonical "randomly ordered list")

**When:** Each respondent should see a different random order of N items (e.g. names of 3 group members, names of 5 candidates), AND you need that order to be:
- reproducible (auditable, replayable post-hoc),
- accessible from anywhere in the form (not just inside a repeat),
- consistent across multiple uses in the same form.

**Workflow:**

1. **Outside SurveyCTO** (R/Python/Stata): for each respondent, generate a randomly shuffled order and save as a string column in your preload CSV. E.g. `silent_order = "3;1;2"` (a permutation of the item IDs, `;`-separated).
2. **Attach the CSV** to the form as a media file.
3. **Inside the form**, pull the string and unpack with a repeat:

| type | name | calculation | repeat_count |
|---|---|---|---|
| `calculate` | `order_list` | `pulldata('respondents', 'silent_order', 'id', ${id})` | |
| `begin repeat` | `order_rep` | | `3` |
| `calculate` | `order_i` | `item-at(';', ${order_list}, index() - 1)` | |
| `end repeat` | `order_rep` | | |

Now `${order_i}` inside iteration `n` of `order_rep` holds the item ID for position `n` (1-indexed). To access from outside the repeat:

| type | name | calculation |
|---|---|---|
| `calculate` | `first_in_order` | `indexed-repeat(${order_i}, ${order_rep}, 1)` |
| `calculate` | `second_in_order` | `indexed-repeat(${order_i}, ${order_rep}, 2)` |

**Reverse lookup** (find someone's position in the random order):

```
calculation: item-index(';', ${order_list}, ${person_id}) + 1
```

**Why pre-randomize externally?** SurveyCTO has no built-in shuffle that returns a permutation as a single value. Doing it inside the form requires N `once(random())` draws and `rank-index` gymnastics (Pattern 9), which is brittle and not auditable. Pre-randomizing in R/Python is one line of `sample()` and gives you a copy of every respondent's assignment alongside the data.

**Gotchas:**
- The delimiter is whatever you pick (`;` is conventional — `,` collides with CSV escaping).
- `item-at` is **zero-indexed**, so use `index() - 1` inside the repeat (where `index()` is 1-indexed).
- `count-items(';', ${list})` counts items including empties — beware trailing semicolons.

---

## Pattern 6 — List experiment (item-count technique)

**When:** Measure prevalence of a sensitive attitude/behaviour without identifying individual respondents. Two arms see different lists; outcome is **how many** items the respondent agrees with (not which). Difference in mean count between arms = prevalence of the sensitive item.

**XLSForm:**

| type | name | label | calculation | relevance | choice_filter | appearance |
|---|---|---|---|---|---|---|
| `calculate` | `arm_draw` | | `once(random())` | | | |
| `calculate` | `list_arm` | | `if(${arm_draw} > 0.5, 1, 2)` | | | |
| `calculate` | `max_arm1` | | `if(${list_arm}=1, 6, 5)` | | | |
| `calculate` | `max_arm2` | | `if(${list_arm}=2, 6, 5)` | | | |
| `begin group` | `list_a` | | | `(index()=1 and ${order}=1) or (index()=2 and ${order}=2)` | | `field-list` |
| `note` | `a1` | Innocuous statement 1 | | | | |
| `note` | `a2` | Innocuous statement 2 | | | | |
| ... 4 more innocuous ... | | | | | | |
| `note` | `a_sensitive` | The sensitive statement | | `${list_arm} = 1` | | |
| `select_one count_0_to_6` | `a_count` | How many do you agree with? | | | `filter <= ${max_arm1}` | `randomize` |
| `end group` | `list_a` | | | | | |

The choice list `count_0_to_6` is `0, 1, 2, 3, 4, 5, 6` with a `filter` column (= `5` for values 0–5, = `6` for value 6). `choice_filter: filter <= ${max_arm1}` hides the "6" option for the control arm so the upper bound matches list length.

**Gotchas:**
- Outcome **must** be a single integer count (`select_one` with numeric labels is fine). Never use `select_multiple` — that recovers individual choices and breaks the anonymity guarantee.
- `appearance: randomize` on the count question is harmless but conventional — keeps any inadvertent ordering bias out.
- Wrap the list group in `appearance: field-list` so all statements appear on one screen with the count question — separating them across screens lets respondents go back and change items, defeating the design.
- Two-list crossover design (each respondent sees both arms in randomized order, with the sensitive item in one): use a `begin repeat` of `repeat_count: 2` wrapping both groups, gated by `relevance` keyed off `index()` and a `${list_order_first}` random draw.

---

## Pattern 7 — Randomize section / group order

**When:** Three or more sections, want to randomize which appears first, second, third.

**XLSForm:**

| type | name | calculation | relevance |
|---|---|---|---|
| `calculate` | `sec_draw` | `once(random())` | |
| `begin group` | `sec_a` | | `(${sec_draw} <= 0.333 and ${slot}=1) or (${sec_draw} > 0.333 and ${sec_draw} <= 0.5 and ${slot}=2) or ...` |

**Cleaner alternative — pre-randomize externally:** generate a permutation column `section_order = "B;A;C"` per respondent, pull via `pulldata`, and use `item-at(';', ${section_order}, n)` to gate each slot. Same advantages as Pattern 5: auditable, easy in R/Python, no expression-soup in the form.

**Gotcha:** Don't try to do this with `once(random())` *inside* each group's relevance — relevance fires before the `calculate` on first paint, and you'll get inconsistent gating. Always have a single top-level `once(random())` that all relevance expressions reference.

---

## Pattern 8 — Pin choices to top/bottom while shuffling the rest

Already covered as a variant of Pattern 1, but worth highlighting since it's the most common production use case.

```
appearance: randomized(${id}, 1, 2)
```

= shuffle middle, keep choice 1 at top (e.g. "Strongly Agree"), keep last 2 fixed (e.g. "Other", "Refuse to answer").

**Order in the choices sheet matters** — the top/bottom counts are positional within the list as written, before any shuffle.

---

## Pattern 9 — Randomize repeat-group iteration order (form-only, no preload)

**When:** You absolutely cannot pre-randomize externally (e.g. ad-hoc enumerator-entered list).

**XLSForm:**

| type | name | calculation |
|---|---|---|
| `begin repeat` | `items` | (whatever) |
| `calculate` | `r_draw` | `once(random())` |
| ... item fields ... | | |
| `end repeat` | `items` | |
| `begin repeat` | `items_shuffled` | `repeat_count: count(${items})` |
| `calculate` | `target_idx` | `rank-index(${r_draw}, index())` *(see SurveyCTO repeat-data cookbook)* |
| `calculate` | `item_in_order` | `indexed-repeat(${some_field}, ${items}, ${target_idx})` |
| `end repeat` | `items_shuffled` | |

In practice this is fragile and hard to debug. **Strongly prefer Pattern 5** when you have any opportunity to compute the order outside the form.

Reference: [SurveyCTO Guide to repeated data, part 4: Cookbook](https://support.surveycto.com/hc/en-us/articles/18524462095507).

---

## Anti-patterns

- **`relevance: random() > 0.5`** — re-fires on every recalculation, value flips on every keystroke. Use `once(random())` in a calculate field, then reference that.
- **Hardcoded seed for actual randomization** — `randomized(329)` gives every respondent the same order. Use `${respondent_id}` or `${enumerator_id}` so seeds vary.
- **`select_multiple` for list-experiment outcome** — defeats the anonymity property. Always a single `select_one` count or `integer`.
- **Randomizing inside relevance branches without `once()`** — even if the immediate parent is `once`, downstream `random()` calls without `once()` will drift.
- **Using `,` as the pulldata list delimiter** — collides with CSV. Use `;` or `|`.

---

## Auditing your randomization

For any randomized field, save the seed/draw to the data so you can replay:

| type | name | calculation |
|---|---|---|
| `calculate` | `arm_assignment` | `if(${arm_draw} > 0.5, 'A', 'B')` |
| `calculate` | `arm_seed` | `${arm_draw}` |

For `randomized` choice order: add a `text_audit` field with `choices` in its appearance to log the actual displayed order per respondent. See [randomization sample form](https://docs.surveycto.com/02-designing-forms/04-sample-forms/03.randomizing.html).

---

## Quick decision tree

- **Choice order on one question** → Pattern 1 (`appearance: randomized(seed, top, bot)`)
- **Two-arm assignment (which video, which wording)** → Pattern 2 (`once(random())` + `if`)
- **Order of two within-subject blocks** → Pattern 3
- **Left/right swap of two stimuli** → Pattern 4
- **Random order of N items per respondent** → Pattern 5 (pre-randomize, pulldata, item-at)
- **Sensitive attitude prevalence** → Pattern 6 (list experiment)
- **Order of 3+ sections** → Pattern 7 (preferably pre-randomized)
- **Pin choices while shuffling** → Pattern 8 (`randomized(seed, top_n, bot_n)`)
- **Shuffle iterations of an in-form repeat** → Pattern 9 (last resort)
