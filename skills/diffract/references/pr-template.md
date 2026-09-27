# PR description template

Fill it from `status()` (sizes, review_closely units, verify status) and your annotations.
Keep it short. The reviewer should know within 20 seconds what to scrutinize and what to skim.
`publish` appends the stack table and the zero-drift certificate automatically, so don't
write those yourself.

```markdown
## {title}

**Part {k} of {K}** of `{original PR / branch}` · implements **{R-ids or "no requirement: cleanup"}**

{2–3 sentences: what this slice changes and why it comes at this point in the stack.
Mention what it enables for the next slice.}

### Review focus (~{behavioral_lines} lines)
- `{path}:{line}`: {what changed in behavior and what could go wrong}
- `{path}:{line}`: {...}

### Safe to skim (~{mechanical_lines} lines)
{e.g. "Pure rename of formatInr → formatMoney across 30 files; no logic changes."}

### Verification
`{verify command}` passes on this slice alone (slices 1–{k} applied), in an isolated worktree.
```

Guidelines:
- Point "Review focus" at the riskiest lines: new branches, error handling, data writes,
  security checks, public API changes. Use the `review_closely` units from `status()`.
- If `status()` lists a requirement gap for this slice (for example, behavioural changes with no
  test changes), say so in one line under Review focus. Reviewers decide whether to ask for tests.
- If a slice is over budget, say why in one line (e.g. "one cohesive algorithm, split would be artificial").
- For the "Unrelated changes" slice, list each change in one line. Reviewers may push back on
  them independently.
- Never claim things you didn't check. "Tests pass" means the verify command passed, nothing more.
