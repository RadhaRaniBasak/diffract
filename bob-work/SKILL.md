---
name: diffract
user-invocable: true
description: Split a large pull request or feature branch into a stack of small, reviewable PRs where every slice builds and passes tests on its own and the stack tip is byte-identical to the original branch (zero drift). Use this whenever the user wants to split, break up, slice, untangle or stack a big PR, branch, commit range or diff, make a PR easier to review, prepare stacked PRs, or says a PR is "too big to review" — even if they never say "Diffract".
---

# Diffract

Turn one monster PR into an ordered stack of small PRs. Each slice builds and passes tests
by itself, and together they add up to exactly the original change. You do the judgment work
(understanding the change, planning slices, diagnosing failures). The `diffract` MCP server
does everything that must be exact: diff parsing, building commits, parallel verification,
and the zero-drift proof.

## Invariants — why the result can be trusted

1. **Never edit source code to make a slice pass.** Only move units between slices or merge
   slices. The author's code is the source of truth; Diffract changes how the PR is reviewed,
   not what it does. Editing code would break the zero-drift guarantee.
2. **Every unit lands in exactly one slice.** A unit is one hunk (`h…`) or one whole-file
   change (`f…`, used for added, deleted, binary, mode-only files). The server rejects plans
   that miss or duplicate a unit.
3. **Slices are cumulative.** Slice k contains slices 1..k, exactly as a stacked PR branch
   would. When several slices fail, the lowest failing one is usually the real cause.
4. **Publish only after `drift_check` returns `zero_drift: true`, and only with the user's
   explicit approval.**

## Tools (MCP server `diffract`)

| Tool | Use it to |
|---|---|
| `start_session` | Resolve base/head, parse the diff into units, write batch files for review |
| `set_requirements` | Record R1..Rn from the design doc so coverage gaps can be computed |
| `show_units` | Read specific units (`h012`, ranges `h010-h018`, globs `path:src/api/*`) |
| `annotate_units` | Store each unit's kind (mechanical/behavioral) and requirement |
| `set_plan` | Store the slice plan and build branch + worktree per slice |
| `verify` | Run the verify command in all slice worktrees **in parallel** (cached by tree, flaky retries) and return a `suggested_repair` for each red slice |
| `probe` | For a red slice with no suggestion: try "slice k + slice j" for every later j in parallel |
| `locate` | Find which units define or change a symbol or test, and which slice they're in |
| `move_units` / `merge_slices` | Repair the plan; re-materializes automatically |
| `drift_check` | Prove stack tip tree == PR head tree; writes the certificate and one file per slice |
| `status` | Everything for PR descriptions and the report (sizes, timings, repairs) |
| `report` | Write the stack map, `.diffract/report.html`; you supply only the summary and slice notes |
| `publish` | Push branches, open stacked PRs with `gh`, post `diffract/zero-drift` and `diffract/tests` checks (dry run first) |
| `cleanup` | Remove worktrees (and optionally branches/session) |

After `start_session`, the other tools default to the same repo, so `repo_path` can be omitted.

## Procedure

Work in Agent mode (Plan mode may block MCP calls). Keep the main conversation lean: batch
files, logs and diffs are for subagents to read, not for you to paste into the chat.

### Step 0 — Gather inputs

- **Base and head.** Default: head = the checked-out branch, base = the repo's default branch.
  If the user gave a PR number or URL, check it out first
  (`gh pr checkout <n>` or `git fetch origin pull/<n>/head:pr-<n>`). Uncommitted changes are
  not part of the split, so ask the user to commit them first.
- **Verify command.** Pick the fastest command that proves a slice works: build/type-check
  plus unit tests, e.g. `npm run build && npm test`, `npx tsc --noEmit && npx jest`,
  `python -m pytest -q`. Read `package.json`, `pyproject.toml`, `Makefile` or CI config to
  choose. Ask only if it's genuinely unclear. See `references/verification.md` for sharing
  dependencies across worktrees; getting this wrong makes verification slow or silently wrong.
- **Design doc / ticket.** If the user attached or mentioned a spec, design doc, ticket or
  test-case sheet (.docx, .pdf, .xlsx, .md), read it now. Extract numbered requirements
  `R1..Rn`, 12 words each at most. This list drives slice titles and flags unrelated changes.
  If the doc states review-size expectations ("about 400 lines per PR"), use that as `line_budget`;
  if it lists out-of-scope items, changes matching them belong in the "Unrelated changes" slice.
  With no doc, derive requirements from the PR title and description if available, or skip.

### Step 1 — Start the session

Call `start_session(repo_path, head, base, verify_command, shared_paths, line_budget=400)`.
Note `totals.minimum_slices` and the `batches` list (one markdown file per batch). If Step 0
produced requirements, call `set_requirements` with them right away, with `source` set to the
document's file name.

### Step 2 — Map the change (parallel explore subagents)

If the PR has more than about 25 units or 600 budgeted lines, **spawn one `explore` subagent
per batch, all in the same turn, so they run in parallel**. Isolated contexts keep hundreds of
diff lines out of this conversation, and running them together cuts wall-clock time.
Subagents do not see this conversation, so each description must be self-contained. Use the
template in `references/subagent-prompts.md` (batch reviewer). For small PRs, read the batch
file yourself.

Collect the one-line-per-unit results and call `annotate_units` once with all of them. Its
`requirement_gaps` are findings for the reviewers, not problems to fix, so tell the user plainly. For
example: "R4 changes 193 behavioural lines with no test changes." Diffract never adds tests: it
surfaces the gap so reviewers can ask for them.

### Step 2b — Check factual claims

If the user gave a pull request description or an agent's report, list its factual claims (for
example: no API changes, only touched X, tests unchanged, all tests pass), call `facts`, and mark
each claim as **holds**, **does not hold**, or **can't tell**, citing the fact that decides it.
Put claims that don't hold in the report summary, and warn about any `weakened_units`.

### Step 3 — Plan the stack, then get approval

Follow `references/slicing-policy.md`. In short:
- Order by dependency: definitions before uses.
- Mechanical changes (renames, moves, formatting) go into their own early "skim" slice.
- Tests travel with the code they test.
- Stay under the line budget where possible.
- Put units that map to no requirement into a final "Unrelated changes" slice.

Show the user the proposed plan as a table: `# | title | units | lines | requirement | what to review`.
Ask them to approve or edit it. This is the human gate. Then call `set_plan(slices)`. Its
warnings (over budget, unrelated units mixed in) are worth fixing before verifying.

### Step 4 — Verify in parallel

Call `verify()`. All slices run concurrently in their own worktrees. A slice that fails and then
passes on the automatic retry is reported green but flagged `flaky`; mention it in that slice's PR
description. If the stack root (base) might itself be red, also run `verify(slices=[0])`. If the
**last** slice fails, the PR head itself is red: stop and ask the user whether to continue with a
narrower verify command.

### Step 5 — Repair loop

Fix the **lowest** red slice first, one repair per round, then `verify()` again. Unchanged slices
come from cache, so only affected slices re-run. Escalate in this order:

1. **Apply `suggested_repair`.** Each red row in the verify result carries one when the log
   explains the failure. It reads missing-symbol errors and failing test names, maps them to the
   units that define, import or change them, and proposes moving those units into the red slice.
   Read its `why` lines; if they make sense, run its `call` (a `move_units`).
2. **No suggestion → `probe(failing_slice=k)`.** It tries "slice k + slice j" for every later slice j,
   all in parallel, and recommends the `merge_slices` that turns k green. Use it when a failing
   assertion gives no clue about which change is missing.
3. **Probe finds nothing, or several slices fail for different reasons →** spawn one `general`
   subagent per failing slice **in the same turn**, using the diagnoser template in
   `references/subagent-prompts.md`. They must not edit files, and they can call `locate`.
4. **Same slice still red after 3 attempts →** `merge_slices([k, k+1])`.

This loop always terminates: in the worst case the stack collapses back into the original PR,
which is green by definition. You never end with a broken stack.

### Step 6 — Prove

Call `drift_check()`. Report its `statement` verbatim. If `zero_drift` is false, do not
continue: report the diffstat and fix the plan.

### Step 7 — Narrate

`drift_check` writes one file per slice (`.diffract/slices/slice-NN.md`: stats plus every unit's
diff). Call `status()`, then write a PR description for each slice with `references/pr-template.md`:
- what the slice does and why it exists,
- the requirement it implements,
- what to review closely (behavioral units) versus skim (mechanical units),
- how it was verified.

For stacks of four or more slices, spawn one `explore` subagent per slice **in the same turn** to
draft the descriptions in parallel (template 3 in `references/subagent-prompts.md`), then review
their drafts for accuracy.

Then call `report(summary, slice_notes)`. It writes the stack map, `.diffract/report.html`, a
self-contained page with:
- the prism view of the split;
- the gaps worth a look;
- requirement coverage per slice;
- slice sizes, split into behavioural and mechanical lines;
- test status;
- the repair timeline;
- the zero-drift proof.

Every number on it comes from git. You supply only words:
- `summary`: 2–3 sentences on what the PR does and how the stack is organised.
- `slice_notes`: one line per slice on where reviewers should focus.

Tell the user the file path so they can open it. `report` also writes `.diffract/stack.json`, the same
run as data. Tell the user they can open it on the Diffract website ("Preview a split", then "Open a
Diffract run") to share an interactive stack map.

### Step 8 — Publish (only on explicit approval)

Call `publish(dry_run=True)` and show the commands. Only after the user says yes, call
`publish(dry_run=False, bodies={"1": "...", ...})` with the descriptions from Step 7. Each PR also
gets two commit statuses, `diffract/zero-drift` and `diffract/tests`, so the proof shows in the checks
list. For a GitLab remote, `publish` opens stacked merge requests with `glab` instead: it detects this from
the remote URL, or pass `forge="gitlab"`.
Finish with a short summary:
- the PR links in order,
- the certificate statement,
- the headline metrics.

## Reference files

- `references/slicing-policy.md`: how to order and size slices, with examples. Read before Step 3.
- `references/subagent-prompts.md`: exact prompts for batch reviewers, failure diagnosers and PR-description writers. Read before Steps 2, 5 and 7.
- `references/pr-template.md`: PR description template. Read in Step 7.
- `references/verification.md`: verify commands, dependency sharing, common false passes and failures. Read in Step 0 or when verification misbehaves.
