# Demo guide

How to reproduce the Diffract demo: 13 real pull requests from pallets/click, squashed into one
branch and split live in IBM Bob. Bob's plan can differ from run to run, so your numbers may too.
The invariants hold whatever the plan: every slice green, zero drift.

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

On Windows, run `powershell -ExecutionPolicy Bypass -File demo\setup_click_demo.ps1` instead. This script hasn't been tested on a real Windows machine yet.

Why the verify command looks the way it does:
- **pytest 9.0.2** is the version click's lockfile pins at this point. Other versions trip click's
  strict warnings-as-errors settings.
- **`-k "not pager"`** excludes the pager tests. They are environment-sensitive and crashed in our
  Linux test environment. Diffract flags the pager changes as untested, so review them by hand.
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
| **Live run in IBM Bob** (MacBook Air) | 7 slices, all green on the first test run, 0 repairs, zero drift. Largest slice **375 of 1,158 lines** (3.1× smaller). Testing all 7 in parallel took 51.9 s, against 183 s one by one; 6.5 minutes from start to proof. The stack-tip tree `fa953095f201` equals the branch head. |
| **Engine test: a plan with a realistic mistake** (NoSuchCommand wired into `Group` before the exception class exists; Linux, one CPU) | **Round 1:** 3 red slices. The engine's `suggested_repair` moved the class definition and its import (2 units, 45 lines) into the red slice. **Round 2:** one failing assertion gave no clue, so `probe` tried 6 candidate fixes in parallel; exactly one turned green, and the plan merged it. **Round 3:** only one slice re-ran (the rest came from cache). All green, with the same 8 slices that engine test started from. |
| **Engine test, hard case:** real PR #3030 (1,978 lines, one indivisible behaviour change) | 6 slices, all green, zero drift, after **2 automatic repair rounds** (suggested moves only). The core stays 1,335 lines because it rewrites ~800 lines of existing test expectations. Diffract keeps it together instead of faking a split. A flaky pager test was caught by the automatic retry, not "repaired". |
| **Requirement coverage** | With the .docx loaded, Diffract flags **R4, the new `get_pager_file()` API: 183 behavioural lines with no test changes**. That's a real gap: the next day, click merged a follow-up PR (#3405) adding 228 lines of tests and changing 36 lines of the pager code. |
| **Stack map** | `.diffract/report.html`: one self-contained file. See `demo/sample-report.html`, generated from the live IBM Bob run (open it in a browser; it also has a dark mode). |
| **Speed** | Parsing the diff and building every slice's branch takes about a second. A test run of click's suite took about 26 s on the MacBook Air and about 3.5 s on our Linux machine, which is why testing slices in parallel matters. |

### Optional: the hard case, with automatic repairs

PR #3030 triggers the automatic repair loop every time, so it's a reliable way to watch the
"red slice → diagnosis → green" cycle. Clean up the first session before starting this one.

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
