# Diffract

**One monster PR in → a stack of small, green, provably complete PRs out.**

Diffract is an IBM Bob skill plus an MCP server. The split between them is the design:
**numbers come from git, words come from Bob.**

- **Bob** (skill `diffract`) does the judgment work:
  - reads the design doc or ticket;
  - classifies hunks with parallel `explore` subagents;
  - plans the stack by requirement;
  - writes the PR descriptions, again with parallel subagents;
  - diagnoses hard failures with parallel `general` subagents.
- **The MCP server** does everything that must be exact:
  - splits the diff into units;
  - builds one commit per slice with git plumbing (your working tree is never touched);
  - verifies all slices in parallel worktrees;
  - diagnoses red slices from their logs;
  - proves the stack tip is byte-identical to the PR head (zero drift);
  - maps every slice to the design doc's requirements and flags gaps;
  - renders the stack map, a self-contained HTML report;
  - opens stacked PRs with `gh`, each carrying `diffract/zero-drift` and `diffract/tests` checks.

```
skills/diffract/SKILL.md          the Bob skill (procedure + invariants)
skills/diffract/references/       slicing policy, subagent prompts, PR template, verification tips
mcp/diffract_mcp.py               MCP server, 15 tools, stdio
mcp/diffract_report.py            renders the stack map (self-contained HTML)
mcp/diffract_auto.py              automatic mode: rule-based plan, tested and repaired, no Bob needed
action.yml                        GitHub Action that runs automatic mode on pull requests (beta)
examples/github-workflow.yml      the workflow to copy into a repository
mcp/smoke_test.py                 end-to-end test on a throwaway repo (no Bob needed)
demo/                             setup scripts (bash, PowerShell), requirements .docx, sample stack map and run, cover image
DEMO.md                           demo recipe, measured results, 3-minute video script
docs/index.html                   project website (single file; see Website below)
bob-config/mcp_settings.example.json
.github/workflows/smoke.yml       runs the smoke test on mcp 1.x and 2.x
```

## Tools

| Tool | Purpose |
|---|---|
| `start_session` | Resolve base/head, parse the diff into units, write batch files for review |
| `set_requirements` | Record R1..Rn from the design doc; enables coverage and gap reports |
| `show_units` | Read units: ids, ranges (`h010-h018`) or globs (`path:src/api/*`) |
| `annotate_units` | Store mechanical/behavioral kind and requirement per unit |
| `set_plan` | Store the plan; build a branch and worktree per slice |
| `verify` | Test all slices in parallel; cached by tree; retries flaky failures; returns a `suggested_repair` for each red slice |
| `probe` | Red slice with no suggestion: try "slice k + slice j" for every later j, in parallel |
| `locate` | Which units define or change a symbol or test, and which slice they're in |
| `move_units` / `merge_slices` | Repair the plan (re-materializes automatically) |
| `drift_check` | Prove stack tip tree == PR head tree; write per-slice files for PR writers |
| `status` | Sizes, verify status, attention map, requirement coverage and gaps, timings, repair history |
| `report` | Write the stack map to `.diffract/report.html` (numbers from git, notes from Bob) |
| `publish` | Push, open stacked PRs with `gh`, post `diffract/zero-drift` and `diffract/tests` statuses (dry run by default) |
| `cleanup` | Remove worktrees (and optionally branches and session files) |

## Setup (about 5 minutes)

Requires Python 3.10+, git 2.28+, and optionally the [GitHub CLI](https://cli.github.com)
(`gh auth login`) for publishing.

```bash
# 1. environment
python3 -m venv ~/.venvs/diffract
~/.venvs/diffract/bin/pip install -r mcp/requirements.txt

# 2. prove the engine works on your machine
~/.venvs/diffract/bin/python mcp/smoke_test.py        # ends with: ALL CHECKS PASSED

# 3. install the skill globally (or copy into <target-repo>/.bob/skills/diffract)
mkdir -p ~/.bob/skills && cp -r skills/diffract ~/.bob/skills/
```

4. Register the MCP server. In Bob, open the MCP settings and choose **Edit Global MCP**
   (`~/.bob/mcp_settings.json`), or use a project-level `.bob/mcp.json`. Merge in
   `bob-config/mcp_settings.example.json` with your absolute paths.
   - Windows: the interpreter is `...\Scripts\python.exe`.
   - `publish` and `cleanup` are deliberately **not** in `alwaysAllow`, so Bob must ask before
     pushing or deleting.

## First run

1. Open the target repo in Bob and check out the PR branch.
2. Install its dependencies once at that head.
3. In Agent mode:

```
/diffract Split feature/discounts against main. Requirements are in @docs/discounts-design.docx.
Verify with "npx tsc --noEmit && npx jest --silent", share node_modules.
```

## Website

`docs/index.html` is the project website: one self-contained file, with no external requests.
- Before sharing it, set `SITE.repoUrl` and `SITE.videoUrl` near the bottom of the file. Empty values hide the GitHub and video buttons.
- The Terms of Service and Privacy Policy pages are drafts: fill in the bracketed items first.
- **Preview a split** (`#/preview`) is dynamic. Visitors can:
  - paste a public GitHub pull request link and get a preview plan, computed in the browser with Diffract's planning rules and clearly labelled as untested;
  - paste a diff instead;
  - open a real run's `.diffract/stack.json`, which renders as a verified stack map.
- Links like `#/preview?pr=https://github.com/owner/repo/pull/123` open straight into a preview.
- GitHub mode needs a normal host such as GitHub Pages. The claude.ai-hosted copy blocks outside requests, so there the page offers the paste option instead.
- GitHub allows 60 unauthenticated requests an hour per network, and the page explains when that limit is hit.
- To host it on GitHub Pages, go to Settings → Pages, choose "Deploy from a branch", and pick `main` with the `/docs` folder.

For a ready-made, reproducible demo on real code (13 merged pallets/click PRs squashed into one
1,158-line branch), follow **[DEMO.md](DEMO.md)**.

## Run it on every pull request (GitHub Action, beta)

`action.yml` makes this repository a GitHub Action. On each pull request, it runs `mcp/diffract_auto.py`:
1. plans a split with Diffract's rules;
2. builds every slice from the branch's own hunks and tests it with your command;
3. repairs failures automatically, proves zero drift, and comments the verified plan on the pull request;
4. uploads the stack map as the `diffract-stack-map` artifact.

Copy `examples/github-workflow.yml` into the target repository's `.github/workflows/` and set `verify` (and optionally `setup`).
- For Python projects, avoid editable installs in `setup`; use `verify: PYTHONPATH=src python -m pytest -q`.
- On the real pallets/click #3030 with click's own tests, automatic mode made 5 repairs and ended all green with zero drift in about a minute on one CPU.
- Pull requests from forks get a read-only token, so there the plan appears in the job's Summary tab instead of a comment.
- If the base branch already fails your command, the Action stops early and says so.
- The Action itself has not yet run on GitHub's runners, which is why it's marked beta.

## GitLab

`publish` detects GitLab from the remote URL, or you can pass `forge="gitlab"`. It opens stacked merge requests with the GitLab CLI (`glab auth login` first). Each merge request gets the stack table and the `diffract/zero-drift` and `diffract/tests` commit statuses. The website preview also accepts GitLab merge request links. Both paths are tested here against recorded CLI calls and documented API responses, not a live GitLab.

## Windows

- **Engine:** every file read and write uses UTF-8, commands run through `cmd`, test processes are stopped with `taskkill`, and shared folders fall back to junctions. The smoke test avoids OS-specific behaviour.
- **Demo setup:** use `powershell -ExecutionPolicy Bypass -File demo\setup_click_demo.ps1`.
- **Status:** the smoke test passes on Linux and on an Intel Mac. Windows hasn't been verified on a real machine yet.

## Troubleshooting

- **Nothing happens on `/diffract`:** check that the skill folder name and its `name:` field are
  both `diffract`, and that the MCP server shows as connected in the MCP settings.
- **Every slice fails the same way:** the base may be red, or your test runner version differs
  from the project's. Run `verify(slices=[0])` and see `references/verification.md`.
- **Suspiciously green Python slices:** an editable install is pointing every worktree at the
  main checkout. Use `python -m pytest` from the worktree root, or `PYTHONPATH=src`.
- **A slice flips between red and green:** that's a flaky test. `verify` retries once and flags
  it `flaky` rather than moving code around.
- **Worktree disk usage:** worktrees live in `~/.cache/diffract/worktrees/`
  (override with `DIFFRACT_WORKTREE_ROOT`). `cleanup` removes them.
- **Platforms:** tested on Linux (Python 3.12, git 2.43, mcp 1.30 and 2.2) and on an Intel Mac
  (Python 3.14, mcp 2.2). Windows hasn't been tested yet.
