# Verification: making "every slice is green" fast and truthful

Each slice gets its own git worktree, outside the repo (default `~/.cache/diffract/worktrees/`).
The verify command runs from the worktree root with `CI=1` and `NO_COLOR=1`. Results are cached
by the slice's git tree, so re-running `verify()` after a repair only re-tests slices whose
content actually changed.

## Choosing the command

Choose the fastest command that would catch a broken slice. Aim for under 2 minutes per slice:
- **TypeScript/JS:** `npx tsc --noEmit && npx jest --silent` (or `npm run build && npm test`).
  Type-checking catches most slicing mistakes (missing imports, symbols defined later) in seconds.
- **Python:** `python -m pytest -q`. Avoid `-x` when the suite is fast: the end-of-run summary lists
  every failing test, and `verify` turns those names into a `suggested_repair`. Add
  `python -m compileall -q src` or `mypy` if tests are sparse.
- **Java/Maven:** `mvn -q -o test` (offline once dependencies are cached).
  **Gradle:** `./gradlew test --offline`.
- **Go:** `go build ./... && go test ./...`. **Rust:** `cargo test --offline`.

Very slow suites: use a narrower command for the repair loop (build + type-check + the test
folders the PR touches), then optionally one final full `verify(command=...)` run.

**Use the test-runner version the project pins** (its lockfile or dev requirements). A newer
runner can turn new deprecation warnings into errors under a strict config (for example
`filterwarnings = error`), and every slice then fails for reasons unrelated to the split.

**Environment-sensitive tests** (pagers, terminals, network, GPUs) can be excluded with `-k "not ..."`
or `--deselect`. Say so in the PR descriptions.

## Dependencies without reinstalling everything N times

- **Node, single package:** install dependencies in the main checkout **at the PR head** (head's
  dependencies are a superset of every slice's needs), then pass `shared_paths=["node_modules"]`.
  Each worktree gets a symlink.
- **Node monorepo / workspaces:** do NOT share `node_modules`. Workspace packages are symlinks into
  the main checkout, so slices would silently test head code (false passes). Use
  `setup_command="npm ci --prefer-offline"` (or `pnpm install --frozen-lockfile --offline`),
  which runs once per new worktree, in parallel.
- **Python:** use one virtualenv outside the repo and call its interpreter in the verify command
  (`/path/to/venv/bin/python -m pytest -q`). **Trap:** if the project is installed in editable
  mode (`pip install -e .`) from the main checkout, every worktree imports the main checkout's
  code → false passes. Run `python -m pytest` from the worktree root: this puts the worktree
  first on `sys.path` for flat layouts. For `src/` layouts, use `PYTHONPATH=src python -m pytest -q`.
- **Java/Go/Rust:** shared global caches (`~/.m2`, Go module cache, `~/.cargo`) already work across
  worktrees. Nothing to do.
- **Submodules:** add `setup_command="git submodule update --init --recursive"`.
- **Generated code or env files not in git** (`.env`, codegen output): list them in
  `shared_paths`, or generate them in `setup_command`.

## Reading results

- **Only the last slice fails** → the PR head itself is red. Stop and tell the user.
- **Every slice fails the same way** → check the base: `verify(slices=[0])`. If the base is
  red, narrow the verify command to what the PR touches.
- **Slice k fails, slices after it pass** → classic ordering bug: slice k uses something a
  later slice introduces. Move that unit earlier.
- **Timeouts** → raise `timeout_seconds` in `start_session`, lower `max_parallel` (CPU
  oversubscription), or narrow the command.
- **Flaky tests** → `verify` already retries a failing slice once (`retries=1`). A pass on retry
  comes back green with `flaky: true`: mention it in that slice's PR description rather than moving
  units around. Running slices in parallel makes timing-sensitive tests flakier than usual.
