# Subagent prompts

Subagents start with an empty context: they see only the description you pass. Fill in every
`{placeholder}`. Spawn all subagents of one step **in the same turn** so they run in parallel
and appear together in Bob's parallel subagents panel.

## 1. Batch reviewer — `explore` subagent (Step 2)

One per batch file from `start_session`. The `explore` type is read-only and runs on a lighter
model, which is ideal here.

```
You are reviewing one batch of a pull request diff. Diffract will split the PR into a stack of
small PRs, and your classification decides what goes where. Do not modify anything.

Read this file completely: {batch_file}
It lists units: each is one hunk (id h...) or one whole-file change (id f...).
Source files at the PR head are under {repo_path}. Read them if you need context on a symbol.

Requirements from the design doc (may be empty):
{R1: ...}
{R2: ...}

For EVERY unit in the batch ({unit_ids}) output exactly one line in this format, nothing else:
<unit> | <mechanical|behavioral> | <R-id or none> | defines: <symbols or -> | uses: <symbols or -> | <note, max 12 words>

Definitions:
- mechanical: does not change behavior. Renames, moves, formatting, import reordering,
  comments, generated code, lockfiles, snapshots.
- behavioral: changes what the code does. Logic, APIs, schema, config values, error handling,
  and tests (a test changes what is verified).
- defines: new or renamed symbols, files, config keys, routes or DB columns this unit introduces.
- uses: symbols this unit relies on that are NEW in this PR (not ones that already existed).
- R-id: the requirement the unit serves. For tests, use the requirement of the code under test.
  Use "none" for changes that serve no requirement (drive-by fixes, unrelated cleanup).
```

## 2. Failure diagnoser — `general` subagent (Step 5)

Use these only when the engine's `suggested_repair` and `probe` didn't resolve the failure. Spawn
one per failing slice. Get unit lists and worktree paths from `status()`.

```
A Diffract slice failed verification. Find the root cause and propose a fix that only MOVES
units between slices or MERGES slices. Never edit, create or delete files: the code itself is
correct, because the full PR passes. Only the slicing is wrong.

Repository: {repo_path}
Slice {k} of {K}: "{title}". Its worktree (slices 1..{k} applied) is: {worktree}
Verify command: {command}
Full log: {log_path}
Units in slice {k}: {units_k}
Units in later slices, which are NOT present in this worktree:
{for each later slice: "slice j: <title>: <unit ids>"}

Already tried: {what the engine's suggested_repair / probe reported, or "nothing"}

You may read files, search the worktree, and re-run the verify command in the worktree. You may
also call two diffract MCP tools:
- show_units(units=[...]) shows what any unit contains;
- locate(symbols=[...]) shows which units define or change a function, class or test, and which
  slice each unit is in.

Typical causes:
- code in slices 1..{k} uses a symbol, import, file or config key introduced by a later unit;
- a renamed symbol's definition and its callers are in different slices;
- a test in this slice exercises behavior added later;
- a lockfile is separated from its manifest.

Reply in exactly this format:
CAUSE: <one sentence>
FIX: move <unit ids> to slice <n>      (or: merge slices <a> and <b>)
EVIDENCE: <file:line from the log or source that proves it>
```

Apply the fix with `move_units(units=[...], to_slice=n)` or `merge_slices([a, b])`. When two
diagnosers propose conflicting fixes, apply the one for the lower slice first and re-verify.

## 3. PR-description writer — `explore` subagent (Step 7)

One per slice, spawned together after `drift_check`. Paste the filled template from
`pr-template.md` into the prompt, so the subagent doesn't need access to the skill folder.

```
Write the pull request description for one slice of a stacked PR. Do not modify anything.

Read this file completely: {slice_file}   (stats, then every change in this slice as a diff)
Stack context: slice {k} of {K} of "{original PR title}". Previous slice: "{title k-1}". Next: "{title k+1}".
Requirements from the design doc:
{R1: ...}

Follow this template exactly and keep it under 250 words:
{contents of pr-template.md's markdown block}

Rules:
- Point "Review focus" at concrete path:line locations from the diff (new branches, error
  handling, data writes, public API changes).
- Describe only what the diff shows. Don't invent motivation or claim anything was tested beyond
  the verify command named in the file.
```
