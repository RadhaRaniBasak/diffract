# Demo guide

Everything below was dry-run end to end: the engine was driven over real MCP stdio, the way Bob
calls it, on a freshly cloned repo. Bob's own plan may differ from ours, so your numbers can too.
The invariants must hold whatever the plan: every slice green, zero drift.

## The branch: 13 real PRs, squashed

We took 13 consecutive merged pull requests from [pallets/click](https://github.com/pallets/click),
#3372 through #3423 (April–May 2026), and squashed them into a single commit.

| | |
|---|---|
| Branch | `monster-pr` on top of `base-monster` (`25edc1e`) |
| Size | 24 files, +886 −272, **1,158 budgeted lines, 96 units** |
| Contents | typing refactors, translated strings, a new `NoSuchCommand` error with suggestions, a file-like pager API, prompt/readline fixes, fish completion, Windows fixes, changelog, one `.gitignore` tweak |

Diffract never sees the original PR boundaries; it only sees the squashed diff.

`demo/q2-release-plan.docx` is the requirements document Bob reads. It lists:
- requirements R1–R7;
- what's out of scope;
- review expectations (one sitting, about 400 lines).

We wrote it as a demo prop summarising these PRs, and the document says so. It isn't a pallets
document.

## Setup (about 2 minutes)

```bash
bash demo/setup_click_demo.sh ~/click-demo       # macOS, Linux or WSL
cp demo/q2-release-plan.docx ~/click-demo/        # untracked: Bob can read it, git ignores it
```

The script does four things:
1. clones click;
2. builds both branches;
3. creates `~/click-demo-venv` with pytest 9.0.2;
4. runs the suite once and prints the verify command for Bob.

On Windows, run `powershell -ExecutionPolicy Bypass -File demo\setup_click_demo.ps1` instead. It hasn't been run on a real Windows machine yet, so allow a few minutes to fix anything it trips on.

Why the verify command looks the way it does:
- **pytest 9.0.2** is the version click's lockfile pins at this point. Other versions trip click's
  strict warnings-as-errors settings.
- **`-k "not pager"`** excludes the pager tests. They are environment-sensitive and crashed in our
  Linux sandbox. Say this in the video: it's honest, and it shows you understand your verify command.
- **`PYTHONPATH=src` is an environment variable** because `test_imports.py` starts a separate
  Python process that must import the slice's code.

## The prompt (Bob, Agent mode, repo `~/click-demo` open)

```
/diffract Split monster-pr against base-monster in this repo.
The requirements are in @q2-release-plan.docx.
Verify with: PYTHONPATH=src /ABSOLUTE/PATH/click-demo-venv/bin/python -m pytest -q -p no:cacheprovider -k "not pager"
```

## What we measured

| Scenario | Result |
|---|---|
| **Sensible plan** (by requirement) | 8 slices, all green on the first verify, zero drift. Largest slice **375 of 1,158 lines** (3.1× smaller). The stack-tip tree was `fa953095f201`, identical to the branch head, on every run. |
| **Plan with a realistic mistake** (NoSuchCommand wired into `Group` before the exception class exists) | **Round 1:** 3 red slices. The engine's `suggested_repair` moved the class definition and its import (2 units, 45 lines) into the red slice. **Round 2:** one failing assertion gave no clue, so `probe` tried 6 candidate fixes in parallel; exactly one turned green, and the plan merged it. **Round 3:** only one slice re-ran (the rest came from cache). All green; same 8 slices. |
| **Hard case:** real PR #3030 (1,978 lines, one indivisible behaviour change) | 6 slices, all green, zero drift, after **2 automatic repair rounds** (suggested moves only). The core stays 1,335 lines because it rewrites ~800 lines of existing test expectations. Diffract keeps it together instead of faking a split. A flaky pager test was caught by the automatic retry, not "repaired". |
| **Requirement coverage** | With the .docx loaded, Diffract flags **R4, the new `get_pager_file()` API: 193 behavioural lines with no test changes**. That's a real gap: the next day, click merged a follow-up PR (#3405) adding 228 lines of tests and changing 36 lines of the pager code. Use this as your "measurable impact" moment. |
| **Stack map** | `.diffract/report.html`: 20 KB, one self-contained file. See `demo/sample-report.html`, generated from this run (open it in a browser; it also has a dark mode). |
| **Engine speed** | Diff parsing plus building 8 branches and worktrees: about 1 second. Each test run: about 3.5 s. |
| **Parallel verify** | Our sandbox had 1 CPU, so the 8 runs went one after another (about 35 s). On a multi-core laptop they run concurrently. |

### Optional: the hard case for guaranteed repair footage

PR #3030 triggers the automatic repair loop every time, which makes it reliable footage for the
"red slice → diagnosis → green" moment. Clean up the first session before starting this one.

```bash
cd ~/click-demo
git branch base-3030 b64ea07128a6368b5f6f93035c75d5693c7ba572^
git branch pr-3030   b64ea07128a6368b5f6f93035c75d5693c7ba572
python3 -m venv ~/click-3030-venv && ~/click-3030-venv/bin/pip install "pytest==8.4.1"
# verify command: PYTHONPATH=src ~/click-3030-venv/bin/python -m pytest -q -p no:cacheprovider
```

## Publishing to your fork

```bash
gh repo fork pallets/click --clone=false
cd ~/click-demo
git remote add fork https://github.com/<you>/click.git
git push fork base-monster            # the first PR targets this branch
gh repo set-default <you>/click
```

Then ask Bob to publish to the remote `fork`. It runs `publish(dry_run=True, remote="fork")` first
and waits for your yes.

## Video script (3:00)

| Time | On screen | Say |
|---|---|---|
| 0:00–0:15 | The 1,158-line branch diff | "Nobody reviews this properly. Review studies find defect detection falls off sharply beyond a few hundred lines. So big PRs get skimmed and approved." |
| 0:15–0:35 | Bob, Agent mode, typing the `/diffract` prompt; the .docx | "Diffract is a Bob skill plus an MCP engine. It turns one monster PR into a stack of small PRs that each pass the tests, and proves nothing was lost." |
| 0:35–1:00 | Parallel subagents panel mapping the batches; R1–R7 pulled from the .docx; the gap message | "Bob reads the requirements doc and fans out explore subagents to classify every hunk. Diffract already found something: R4, a brand-new public API, ships with no test changes. The click maintainers had to add those tests in a follow-up PR the next day." |
| 1:00–1:20 | Plan table with R-ids and an "Unrelated changes" slice; approve | "Every slice maps to a requirement. Anything that maps to none, like this .gitignore change, is isolated." |
| 1:20–1:55 | `verify` running all slices at once; a red slice → `suggested_repair` / `probe` → green (hard-case clip if needed) | "Each slice builds in its own worktree and runs the real test suite in parallel. When one fails, the engine reads the log, finds the missing hunk and moves it. Cached slices don't even re-run." |
| 1:55–2:15 | `ZERO DRIFT` statement, then the stack map: prism, requirements grid, repair timeline | "The stack tip is byte-identical to the original branch, so no line was lost or changed. Every number on this page comes from git; only the notes come from Bob." |
| 2:15–2:40 | The stacked PRs on GitHub, zooming in on the green `diffract/zero-drift` and `diffract/tests` checks. If you set up the Action, cut briefly to its comment on a test PR | "One approval later, reviewers get eight PRs in merge order, each small enough for one sitting, and each carries its proof in GitHub's own checks. The same engine also runs on every pull request as a GitHub Action." |
| 2:40–3:00 | Numbers slide + architecture | "1,158 lines → largest slice 375. Eight green slices, zero drift, in {minutes from `status()`} minutes. Diffract: review the change, not the monster." |

## Recording checklist

- **Rehearse once**, then reset with `cleanup(delete_branches=true, delete_session=true)`.
- **Auto-approve** subagent spawns and every Diffract tool except `publish` and `cleanup`.
- **Plug in the laptop and close heavy apps:** the parallel test runs need the cores.
- **Enlarge Bob's font or zoom** so the tables read on video.
- **Record in real time** and cut the waits in editing. Show the real elapsed minutes from `status()` so the speed claim stays honest.
- **Keep `.diffract/report.html` open** in a browser tab, ready for the proof shot.
- **Optional extra beat (15 s):** on the website's "Preview a split" page, paste any public PR link to show a live preview. Then open your run's `.diffract/stack.json` to show the verified version. Host the site on GitHub Pages for this, because the claude.ai copy can't reach GitHub.
- **Cover image:** `demo/cover.png` (1920×1080, the prism in dark mode) is ready to upload. For your own run, switch your OS to dark mode and screenshot the top of your report.

## Submission checklist

These are lablab's usual fields; confirm them on the event page.
- title and one-liner;
- long description;
- cover image;
- video link;
- slide deck;
- public GitHub repo;
- demo link: the fork's PR stack or the report.

Submissions close **Sunday 27 September 2026, 15:00 UTC (8:30 pm IST)**.
