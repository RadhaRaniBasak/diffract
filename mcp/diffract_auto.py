#!/usr/bin/env python3

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diffract_mcp as engine  # noqa: E402

LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "poetry.lock",
             "pipfile.lock", "uv.lock", "cargo.lock", "go.sum", "composer.lock", "gemfile.lock"}
MANIFESTS = {"package.json", "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "go.mod", "cargo.toml",
             "composer.json", "gemfile", "pom.xml", "build.gradle", "build.gradle.kts", "tsconfig.json"}
SOURCE_ROOTS = {"src", "lib", "app", "source", "pkg", "internal"}
TEST_ROOTS = {"test", "tests", "__tests__", "spec", "specs", "testing"}
ORDER_HINTS = [
    re.compile(r"(^|/)_?(types?|models?|schema|entities|domain|core|utils?|helpers?|common|shared|lib)(/|$)"),
    re.compile(r"(^|/)(services?|api|server|handlers?|controllers?|routes?|store|db|data|parser)(/|$)"),
    re.compile(r"(^|/)(ui|components?|pages?|views?|screens?|client|web|frontend|termui)(/|$)"),
    re.compile(r"(^|/)(cli|scripts?|tools?|bin|cmd)(/|$)"),
]
SMALL_CONCERN = 60


def file_role(path: str) -> str:
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    if name in LOCKFILES:
        return "lockfile"
    if (name.endswith(".snap") or "/__snapshots__/" in f"/{lower}" or re.search(r"\.min\.(js|css)$", lower)
            or re.search(r"(^|/)(dist|build|vendor|generated)/", lower)):
        return "generated"
    if (re.search(r"(^|/)(tests?|__tests__|specs?)/", lower) or re.search(r"(^|/)test_[^/]*\.py$", lower)
            or re.search(r"_test\.(py|go|rb)$", lower) or re.search(r"\.(test|spec)\.[^/]+$", lower)
            or re.search(r"(^|/)[A-Z][A-Za-z0-9_]*Tests?\.(java|kt|cs)$", path)):
        return "test"
    if re.search(r"\.(md|rst|adoc|txt)$", lower) or re.search(r"(^|/)docs?/", lower):
        return "docs"
    if re.search(r"(^|/)(migrations?|alembic|db/migrate)/", lower):
        return "migration"
    if (name in MANIFESTS or lower.startswith(".github/") or re.search(r"(^|/)(dockerfile|makefile|\.gitignore|\.editorconfig)$", lower)
            or ("/" not in lower and re.search(r"\.(json|ya?ml|toml|ini|cfg)$", lower))):
        return "config"
    return "source"


def module_stem(file_name: str) -> str:
    stem = re.sub(r"\.[^.]+$", "", file_name)
    stem = re.sub(r"\.(test|spec)$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"^test_", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"_test$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"Tests?$", "", stem)
    return stem.lstrip("_").lower()


def concern_key(path: str) -> str:
    parts = path.split("/")
    folders = [folder for folder in parts[:-1] if folder.lower() not in SOURCE_ROOTS and folder.lower() not in TEST_ROOTS]
    return "/".join(folders[:2]).lower() if len(folders) >= 2 else module_stem(parts[-1])


def describe_files(session: dict) -> list[dict]:
    files = []
    for path, info in session["files"].items():
        role = file_role(path)
        units = [session["units"][u] for u in info["units"]]
        counted = 0 if role in ("lockfile", "generated") or info["binary"] else info["added"] + info["removed"]
        formatting = (role == "source" and units and all(u["kind"] == "hunk" and "whitespace-only" in u["hints"] for u in units))
        files.append({"path": path, "role": role, "lines": counted, "units": info["units"], "mechanical": bool(formatting)})
    return files


def pack_within_budget(files: list[dict], budget: int) -> list[list[dict]]:
    groups, current = [], []
    for file in files:
        if current and sum(f["lines"] for f in current) + file["lines"] > budget:
            groups.append(current)
            current = []
        current.append(file)
    if current:
        groups.append(current)
    return groups


def rule_plan(session: dict, budget: int) -> list[dict]:
    files = describe_files(session)
    by_role = lambda role: [f for f in files if f["role"] == role and not f["mechanical"]]  # noqa: E731
    slices: list[dict] = []

    def add(title: str, members: list[dict], role: str) -> None:
        if members:
            slices.append({"title": title, "files": members, "role": role})

    add("Renames and formatting", [f for f in files if f["mechanical"]], "mechanical")
    add("Dependencies and configuration", by_role("config") + by_role("lockfile"), "mechanical")
    add("Database migrations", by_role("migration"), "code")

    concerns: dict[str, dict] = {}
    for file in by_role("source"):
        concerns.setdefault(concern_key(file["path"]), {"source": [], "tests": []})["source"].append(file)
    unmatched_tests = []
    for file in by_role("test"):
        key = concern_key(file["path"])
        match = concerns.get(key) or next((c for k, c in concerns.items() if k.split("/")[-1] == key.split("/")[-1]), None)
        (match["tests"] if match else unmatched_tests).append(file)

    def rank(key: str) -> float:
        return next((index for index, hint in enumerate(ORDER_HINTS) if hint.search(key)), 1.5)

    small: list[tuple[str, str, list[dict]]] = []

    def flush_small() -> None:
        if len(small) == 1:
            add(small[0][1], small[0][2], "code")
        elif small:
            add("Small changes: " + ", ".join(label for label, _, _ in small), [f for _, _, group in small for f in group], "code")
        small.clear()

    for key in sorted(concerns, key=lambda k: (rank(k), k)):
        concern = concerns[key]
        members = concern["source"] + concern["tests"]
        title = f"{key} and its tests" if concern["tests"] else key
        lines = sum(f["lines"] for f in members)
        if lines < SMALL_CONCERN:
            if sum(f["lines"] for _, _, group in small for f in group) + lines > budget:
                flush_small()
            small.append((key, title, members))
            continue
        flush_small()
        parts = pack_within_budget(members, budget)
        for index, part in enumerate(parts, 1):
            add(f"{title} (part {index} of {len(parts)})" if len(parts) > 1 else title, part, "code")
    flush_small()
    test_parts = pack_within_budget(unmatched_tests, budget)
    for index, part in enumerate(test_parts, 1):
        add(f"Other tests (part {index} of {len(test_parts)})" if len(test_parts) > 1 else "Other tests", part, "tests")
    add("Generated files", by_role("generated"), "mechanical")
    add("Docs and changelog", by_role("docs"), "docs")
    return slices


async def verify_and_repair(max_rounds: int) -> tuple[dict, list[str]]:
    repairs: list[str] = []
    for _ in range(max_rounds + 1):
        result = await engine.verify()
        summary = result["summary"]
        if summary["all_green"] or len(repairs) >= max_rounds:
            return result, repairs
        lowest = summary["lowest_failing"]
        total = len(engine._load(engine._root(None))["plan"]["slices"])
        if lowest is None or lowest == total:
            return result, repairs
        row = next(r for r in result["results"] if r["slice"] == lowest)
        suggestion = row.get("suggested_repair")
        if suggestion:
            units = json.loads(re.search(r"units=(\[.*?\])", suggestion["call"]).group(1))
            await engine.move_units(units, to_slice=lowest)
            reason = re.sub(r" \(now in slice \d+\)$", "", suggestion["why"][0].split(": ", 1)[-1])
            repairs.append(f"moved {suggestion['units']} into slice {lowest} because it {reason}")
            continue
        probe = await engine.probe(lowest)
        if probe["green_with"]:
            best = min(probe["green_with"], key=lambda j: next(r["lines"] for r in probe["probes"] if r["add_slice"] == j))
            await engine.merge_slices([lowest, best])
            repairs.append(f"merged slice {best} into slice {lowest} (probe: the only way to make it pass)")
        else:
            await engine.merge_slices([lowest, lowest + 1])
            repairs.append(f"merged slices {lowest} and {lowest + 1} (no smaller fix found)")
    return await engine.verify(), repairs


def findings_for(plan_slices: list[dict], status: dict, budget: int) -> list[str]:
    notes = []
    by_title = {slice_["title"]: slice_ for slice_ in plan_slices}
    for row in status["slices"]:
        planned = by_title.get(row["title"])
        if planned and planned["role"] == "code" and any(f["role"] == "source" and f["lines"] for f in planned["files"]) \
                and not any(f["role"] == "test" for f in planned["files"]):
            notes.append(f"Slice {row['slice']} ({row['title']}) changes code with no test changes alongside.")
        if row["lines"] > budget:
            notes.append(f"Slice {row['slice']} has {row['lines']:,} lines, over the {budget}-line budget; it could not be split further.")
    return notes


def markdown_summary(status: dict, cert: dict, command: str, repairs: list[str], findings: list[str], budget: int) -> str:
    slices = status["slices"]
    green = all(row["verify"] == "pass" for row in slices)
    lines = [f"## Diffract: this pull request as {len(slices)} reviewable slices", "",
             f"**{status['original']['budgeted_lines']:,} lines became {len(slices)} slices; the largest is "
             f"{status['largest_slice_lines']:,} lines. "
             + (f"Every slice passes `{command}` on its own. " if green else "Some slices do not pass yet. ")
             + ("The stack matches this branch exactly (zero drift).**" if cert["zero_drift"] else "The stack does not match this branch.**"),
             ""]
    if slices and slices[-1]["verify"] != "pass":
        lines += [f"**The branch itself fails `{command}`**, and the last slice is identical to it, so the slices can't all "
                  f"pass. Fix the branch, then run again.", ""]
    lines += ["| # | Slice | Lines | Tests |", "|---|---|---:|---|"]
    for row in slices:
        shown = {"pass": "passed", "fail": "failed", "timeout": "timed out"}.get(row["verify"], "not run")
        lines.append(f"| {row['slice']} | {row['title']} | {row['lines']:,} | {shown} |")
    if findings:
        lines += ["", "**Worth a look**", *[f"- {note}" for note in findings]]
    lines += ["", "<details><summary>How this was made</summary>", "",
              f"Planned with Diffract's rules: tests travel with their code, shared code comes first, docs last, "
              f"at most {budget} lines per slice. Every slice was then built from this branch's own hunks and tested "
              f"in its own worktree. {len(repairs)} automatic repair{'s' if len(repairs) != 1 else ''}"
              + (": " + "; ".join(repairs) + "." if repairs else "."), "", cert["statement"], "",
              "The `diffract-stack-map` artifact holds the full report and `stack.json`, which opens on the Diffract "
              "website under Preview a split, then Open a Diffract run.", "</details>", "",
              "_For a plan that follows your design document, run Diffract in IBM Bob._"]
    return "\n".join(lines) + "\n"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Split a branch into tested slices without Bob.")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--verify", required=True, help="command that builds and tests one slice")
    parser.add_argument("--setup", default="", help="optional one-time setup per slice worktree, e.g. 'npm ci'")
    parser.add_argument("--budget", type=int, default=400)
    parser.add_argument("--max-rounds", type=int, default=8)
    parser.add_argument("--summary", default="diffract-summary.md")
    parser.add_argument("--fail-on-red", action="store_true", help="exit 1 if any slice is still red")
    args = parser.parse_args()

    info = engine.start_session(args.repo, head=args.head, base=args.base, verify_command=args.verify,
                                setup_command=args.setup, line_budget=args.budget)
    summary_path = Path(args.summary)
    if info["totals"]["budgeted_lines"] <= args.budget:
        summary_path.write_text(f"## Diffract: no split needed\n\nThis pull request changes "
                                f"{info['totals']['budgeted_lines']:,} lines, within the {args.budget}-line budget.\n",
                                encoding="utf-8")
        print(summary_path.read_text(encoding="utf-8"))
        return 0
    session = engine._load(engine._root(None))
    plan = rule_plan(session, args.budget)
    stored = await engine.set_plan([{"title": s["title"], "units": [u for f in s["files"] for u in f["units"]],
                                     "rationale": f"Planned by rule: {s['role']}."} for s in plan])
    if not stored["ok"]:
        raise SystemExit("plan rejected: " + "; ".join(stored["errors"]))
    base_check = await engine.verify(slices=[0])
    if base_check["results"][0]["status"] != "pass":
        summary_path.write_text(f"## Diffract: the base branch is red\n\n`{args.verify}` already fails on the base branch, "
                                f"so no slice could pass. Fix the base branch, or narrow the verify command, then run again.\n",
                                encoding="utf-8")
        print(summary_path.read_text(encoding="utf-8"))
        return 0
    verification, repairs = await verify_and_repair(args.max_rounds)
    cert = engine.drift_check()
    engine.report(summary="Planned by Diffract's rules and verified with the project's own tests.")
    status = engine.status()
    text = markdown_summary(status, cert, args.verify, repairs, findings_for(plan, status, args.budget), args.budget)
    summary_path.write_text(text, encoding="utf-8")
    print(text)
    return 1 if args.fail_on_red and not verification["summary"]["all_green"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
