# Slicing policy

The goal is a stack a human reviewer can move through quickly and confidently. Each slice
should answer one question: "is this one idea correct?" Keep that goal in mind when the
rules below conflict.

## Ordering: what goes first

1. **Mechanical groundwork.** Renames, file moves, formatting, import reshuffles, generated
   code, dependency bumps together with their lockfile. Label the slice "(mechanical — skim)".
   Reviewers can approve a large mechanical slice in minutes if it contains nothing else.
2. **Foundations.** Schema, migrations, types and interfaces, config and feature flags, new
   helper modules that nothing uses yet.
3. **Core behavior.** Domain logic, services, algorithms. Split by requirement (R1, R2, …)
   when the design doc gives them.
4. **Wiring.** API routes, handlers, CLI commands, dependency injection, UI components that
   consume the core.
5. **Docs and cleanup.** Docs for features already in earlier slices can travel with those
   slices. Otherwise put them here.
6. **Unrelated changes.** Drive-by fixes and edits that map to no requirement go last, in
   their own slice. Reviewers can then ask for them to be dropped without blocking the rest.

## Hard constraints (verification will catch violations — cheaper to get right up front)

- **Definitions before uses.** If unit A uses a symbol, file or config key that unit B
  introduces, B must be in the same slice as A or an earlier one. The batch reviewers'
  `defines:` / `uses:` columns exist for exactly this check.
- **Renames stay whole.** A renamed symbol's definition and all of its call sites go in one
  slice. Otherwise the slice in between doesn't compile.
- **Tests travel with the code they test.** A test for behavior introduced in slice 3 must be
  in slice 3 or later — never earlier, because it would fail. Prefer the same slice: each
  slice then proves itself.
- **Lockfiles travel with their manifest** (`package.json` with `package-lock.json`, and so on).
- **Snapshot files travel with the tests that produce them.**
- **Migrations travel with the model or schema change they implement.**

## Size

- Budget: `line_budget` budgeted lines per slice (default 400; lockfiles, snapshots and minified
  files don't count). Review effectiveness drops sharply beyond a few hundred lines per session.
- Aim for 3–8 slices. Fewer is fine for a small PR. More than about 10 turns the stack itself
  into a review burden.
- Avoid slivers: a behavioral slice under ~40 lines usually belongs with a neighbor, unless it
  is a risky change that deserves isolated attention (security, data migration).
- Going over budget is acceptable when a slice is one cohesive change: a mechanical rename
  across 80 files, or a single algorithm that can't be meaningfully cut. Say so in the rationale.

## Titles and rationales

- Title: imperative, specific, ≤ 60 chars — "Add discount engine with code validation", not
  "Part 2" or "Backend changes".
- Rationale: 1–3 sentences on why this slice exists and what it enables next.
- Requirement: the R-ids it implements. This is how the stack maps back to the design doc.

## Worked example

A 2,400-line PR "Add discount codes to checkout":
- design doc R1 discount engine; R2 apply code at checkout; R3 show savings on receipt;
- plus a drive-by renaming of `formatInr` to `formatMoney` across 30 files.

| # | Title | Contents | Lines |
|---|---|---|---|
| 1 | Rename formatInr to formatMoney (mechanical — skim) | definition + 30 call sites | 180 |
| 2 | Add discount engine (R1) | `discounts.ts`, its unit tests | 310 |
| 3 | Persist applied discount on orders (R2) | migration, model field, repository | 240 |
| 4 | Apply discount codes at checkout (R2) | service + API route + integration test | 390 |
| 5 | Show savings on receipts (R3) | receipt template, component, snapshot | 260 |
| 6 | Unrelated changes | logging tweak, README typo | 40 |

Why this order:
- The rename is first because slices 3–5 touch files that use `formatMoney`.
- The engine comes before checkout because checkout imports it.
- The migration comes before the service that writes the new column.
- The unrelated edits are isolated, so reviewers can push back on them cheaply.
