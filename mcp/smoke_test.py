#!/usr/bin/env python3

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

KEEP = "--keep" in sys.argv
TMP = Path(tempfile.mkdtemp(prefix="diffract-smoke-"))
os.environ["DIFFRACT_WORKTREE_ROOT"] = str(TMP / "worktrees")
os.environ["DIFFRACT_HOME"] = str(TMP / "state")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import diffract_mcp as d  # noqa: E402

REPO = TMP / "shop"
PY = sys.executable


def sh(*args: str, cwd: Path = REPO) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def write(rel: str, text: str | bytes) -> None:
    p = REPO / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text if isinstance(text, bytes) else text.encode())


def check(cond: bool, msg: str) -> None:
    if not cond:
        print(f"  ✗ {msg}")
        raise SystemExit(f"FAILED: {msg}\n(temp repo kept at {TMP})")
    print(f"  ✓ {msg}")


FILLER = "".join(f"    # padding line {i} keeps hunks apart\n" for i in range(8))
NOTES_BASE = [f"note {i}" for i in range(1, 13)]
NOTES_HEAD = list(NOTES_BASE)
NOTES_HEAD[1] = "note 2 (edited)"
NOTES_HEAD[10] = "note 11 (edited)"


def crlf_no_eol(lines: list[str]) -> bytes:
    return "\r\n".join(lines).encode()


def build_repo() -> None:
    REPO.mkdir(parents=True)
    sh("git", "init", "-q", "-b", "main")
    sh("git", "config", "user.email", "dev@example.com")
    sh("git", "config", "user.name", "Dev")
    sh("git", "config", "core.autocrlf", "false")
    write("shop/__init__.py", "")
    write("shop/pricing.py", 'def format_inr(amount):\n    return f"INR {amount:,.2f}"\n\n\n'
          + "".join(f"# pricing note {i}\n" for i in range(8))
          + "\ndef with_tax(amount, rate=0.18):\n    return round(amount * (1 + rate), 2)\n")
    write("shop/receipt.py", "from shop.pricing import format_inr\n\n"
          + "".join(f"# receipt note {i}\n" for i in range(8))
          + "\n\ndef line(name, amount):\n    return f\"{name}: {format_inr(amount)}\"\n")
    write("shop/cart.py", "from shop.pricing import with_tax\n\n\nclass Cart:\n    def __init__(self):\n"
          "        self.items = []\n\n" + FILLER
          + "    def add(self, name, price, qty=1):\n        self.items.append((name, price, qty))\n\n" + FILLER
          + "    def subtotal(self):\n        return sum(p * q for _, p, q in self.items)\n")
    write("shop/legacy.py", "OLD = True\n")
    write("tests/test_cart.py", "from shop.cart import Cart\n\n\ndef test_subtotal():\n    c = Cart()\n"
          "    c.add('pen', 100, 2)\n    assert c.subtotal() == 200\n")
    write("tests/test_receipt.py", "from shop.receipt import line\n\n\ndef test_line():\n"
          "    assert line('pen', 1234.5) == 'pen: INR 1,234.50'\n")
    write("README.md", "# Shop\n\nA tiny shop.\n")
    write("notes.txt", crlf_no_eol(NOTES_BASE))
    write("scripts/run.sh", "#!/bin/sh\necho run\n")
    write("docs/user guide.md", "# User guide\n\nStep one.\n")
    sh("git", "add", "-A")
    sh("git", "commit", "-qm", "base")

    sh("git", "checkout", "-qb", "feature/discounts")
    write("shop/discounts.py", "CODES = {'DIWALI10': 0.10}\n\n\ndef apply_discount(amount, code):\n"
          "    if code not in CODES:\n        raise ValueError(f'unknown code {code}')\n"
          "    return round(amount * (1 - CODES[code]), 2)\n")
    write("shop/cart.py", "from shop.discounts import apply_discount\nfrom shop.pricing import with_tax\n\n\n"
          "class Cart:\n    def __init__(self):\n        self.items = []\n\n" + FILLER
          + "    def add(self, name, price, qty=1):\n        self.items.append((name, price, qty))\n\n" + FILLER
          + "    def subtotal(self):\n        return sum(p * q for _, p, q in self.items)\n\n"
          + "    def total_with_code(self, code):\n        return with_tax(apply_discount(self.subtotal(), code))\n")
    sh("git", "commit", "-qam", "discounts wip")
    write("shop/pricing.py", 'def format_money(amount):\n    return f"INR {amount:,.2f}"\n\n\n'
          + "".join(f"# pricing note {i}\n" for i in range(8))
          + "\ndef with_tax(amount, rate=0.18):\n    return round(amount * (1 + rate), 2)\n")
    write("shop/receipt.py", "from shop.pricing import format_money\n\n"
          + "".join(f"# receipt note {i}\n" for i in range(8))
          + "\n\ndef line(name, amount):\n    return f\"{name}: {format_money(amount)}\"\n")
    write("tests/test_discounts.py", "import pytest\nfrom shop.discounts import apply_discount\n\n\n"
          "def test_code():\n    assert apply_discount(200, 'DIWALI10') == 180\n\n\n"
          "def test_unknown():\n    with pytest.raises(ValueError):\n        apply_discount(1, 'NOPE')\n")
    write("tests/test_cart.py", "from shop.cart import Cart\n\n\ndef test_subtotal():\n    c = Cart()\n"
          "    c.add('pen', 100, 2)\n    assert c.subtotal() == 200\n\n\ndef test_code():\n    c = Cart()\n"
          "    c.add('pen', 100, 2)\n    assert c.total_with_code('DIWALI10') == 212.4\n")
    write("README.md", "# Shop  \n\nA tiny   shop.\n")
    write("notes.txt", crlf_no_eol(NOTES_HEAD))
    write("assets/logo.png", bytes(range(256)) * 4)
    (REPO / "shop/legacy.py").unlink()
    write("docs/user guide.md", "# User guide\n\nStep one.\nStep two.\n")
    sh("git", "add", "-A")
    sh("git", "update-index", "--chmod=+x", "scripts/run.sh")
    sh("git", "commit", "-qm", "rename, tests, housekeeping")


async def main() -> None:
    print(f"temp dir: {TMP}")
    build_repo()
    verify_cmd = f'"{PY}" -m pytest -q -p no:cacheprovider'

    print("\n1. start_session")
    info = d.start_session(str(REPO), head="feature/discounts", base="main", verify_command=verify_cmd, line_budget=40)
    print(f"   {info['totals']}")
    units = d._load(REPO)["units"]
    by_path: dict[str, list[str]] = {}
    for uid, u in units.items():
        by_path.setdefault(u["path"], []).append(uid)
    check(len(by_path["shop/cart.py"]) == 2, "cart.py split into 2 hunks (import + new method)")
    check(len(by_path["notes.txt"]) == 2, "CRLF / no-final-newline file split into 2 hunks")
    check(all(units[u]["kind"] == "file" for p in ("shop/discounts.py", "assets/logo.png", "shop/legacy.py", "scripts/run.sh")
              for u in by_path[p]), "added / binary / deleted / mode-only files are whole-file units")
    check(units[by_path["scripts/run.sh"][0]]["change"] == "mode", "mode-only change detected")
    check("whitespace-only" in units[by_path["README.md"][0]]["hints"], "whitespace-only hunk flagged")
    check("docs/user guide.md" in by_path, "path with a space handled")
    check(Path(info["batches"][0]["file"]).exists(), "batch file written for explore subagents")

    U = {p: ids for p, ids in by_path.items()}
    cart_import, cart_method = U["shop/cart.py"]
    notes_1, notes_2 = U["notes.txt"]

    print("\n2. plan validation")
    bad = await d.set_plan([{"title": "Only one", "units": [cart_import]}])
    check(not bad["ok"] and "not assigned" in " ".join(bad["errors"]), "incomplete plan rejected with the missing units listed")

    print("\n3. deliberately naive plan (two hidden dependency bugs)")
    plan = [
        {"title": "Add total_with_code to Cart", "units": [cart_import, cart_method, *U["tests/test_cart.py"]],
         "requirement": "R1"},
        {"title": "Add discount engine", "units": [*U["shop/discounts.py"], *U["tests/test_discounts.py"]], "requirement": "R1"},
        {"title": "Rename format_inr to format_money", "units": U["shop/pricing.py"]},
        {"title": "Housekeeping", "units": ["path:shop/receipt.py", *U["README.md"], notes_1, *U["assets/logo.png"],
                                            *U["shop/legacy.py"], *U["scripts/run.sh"], *U["docs/user guide.md"]]},
        {"title": "Notes tweak", "units": [notes_2]},
    ]
    res = await d.set_plan(plan)
    check(res["ok"] and res["materialized"]["drift_ok"], "plan stored; 5 branches built; stack tip == PR head")
    wt4 = Path(res["materialized"]["worktree_root"]) / "s04"
    expected = NOTES_BASE.copy()
    expected[1] = NOTES_HEAD[1]
    check((wt4 / "notes.txt").read_bytes() == crlf_no_eol(expected),
          "slice 4 has exactly one of the two notes.txt edits, CRLF and missing EOL newline preserved")
    mode = sh("git", "ls-files", "-s", "scripts/run.sh", cwd=wt4).split()[0]
    check(not (wt4 / "shop/legacy.py").exists() and mode == "100755", "deletion and chmod applied in slice 4")

    print("\n4. verify all slices in parallel")
    v = await d.verify()
    print(f"   {v['summary']['status_by_slice']}  wall={v['wall_seconds']}s")
    check(v["summary"]["failed"] == [1, 3], "slice 1 (missing discounts module) and slice 3 (rename without its caller) fail")
    check(v["summary"]["lowest_failing"] == 1, "lowest failing slice reported")
    red = {row["slice"]: row for row in v["results"] if row["status"] != "pass"}
    fix1 = red[1].get("suggested_repair") or {}
    fix3 = red[3].get("suggested_repair") or {}
    print(f"   suggestion for slice 1: {fix1.get('units')}  why: {fix1.get('why', [''])[0]}")
    print(f"   suggestion for slice 3: {fix3.get('units')}  why: {fix3.get('why', [''])[0]}")
    check(U["shop/discounts.py"][0] in fix1.get("units", ""), "engine diagnosed slice 1: move the discounts module earlier")
    check(all(u in fix3.get("units", "") for u in U["shop/receipt.py"]),
          "engine diagnosed slice 3: the renamed function's callers must move with the rename")

    print("\n5. parallel probe (the fallback when there is no suggestion)")
    p = await d.probe(1)
    check(2 in p["green_with"] and "merge_slices(slices=[1, 2])" in p["recommendation"],
          f"probe tried {len(p['probes'])} candidates in parallel; adding slice 2 turns slice 1 green")

    print("\n6. repair slice 1 with the engine's suggestion")
    r1 = await d.move_units(U["shop/discounts.py"], to_slice=1)
    check(r1["ok"], "move accepted")
    v = await d.verify()
    ran = [x["slice"] for x in v["results"] if not x["cached"]]
    check(ran == [1], f"only slice 1 re-ran (others cached by tree): ran={ran}")
    check(v["summary"]["failed"] == [3], "slice 1 now green, slice 3 still red")

    print("\n7. repair slice 3 with the engine's suggestion")
    fix3 = next(row for row in v["results"] if row["slice"] == 3)["suggested_repair"]
    await d.move_units(json.loads(fix3["call"].split("units=")[1].split(", to_slice")[0]), to_slice=3)
    v = await d.verify()
    ran = [x["slice"] for x in v["results"] if not x["cached"]]
    check(v["summary"]["all_green"], "all slices green")
    check(ran == [3], f"only slice 3 re-ran: ran={ran}")
    hits = d.locate(["format_money"])["results"][0]["definitions"]
    check(any(U["shop/pricing.py"][0] in h["units"] for h in hits), "locate() finds the unit that defines format_money")

    print("\n8. merge the tiny last slice, then prove zero drift")
    m = await d.merge_slices([4, 5], title="Housekeeping")
    check(m["ok"] and len(m["slices"]) == 4, "slices 4 and 5 merged")
    v = await d.verify()
    check(v["summary"]["all_green"] and all(x["cached"] for x in v["results"]), "merged stack still green, fully from cache")
    v0 = await d.verify(slices=[0])
    check(v0["results"][0]["status"] == "pass", "stack root (base) is green")
    cert = d.drift_check()
    print(f"   {cert['statement']}")
    check(cert["zero_drift"], "zero-drift certificate issued")
    check(len(cert["slice_files"]) == 4 and Path(cert["slice_files"][0]).exists(), "per-slice files written for PR writers")
    head = sh("git", "rev-parse", "feature/discounts^{tree}")
    tip = sh("git", "rev-parse", f"{m['materialized']['slices'][-1]['branch']}^{{tree}}")
    check(head == tip, "independent check: git says tip tree == head tree")

    print("\n9. annotations, status, show_units, publish dry-run")
    a = d.annotate_units([{"unit": u, "kind": "mechanical", "requirement": "none"} for u in U["README.md"]]
                         + [{"unit": u, "kind": "behavioral", "requirement": "R1"} for u in (cart_import, cart_method)])
    check(a["annotated"] == 3 and a["attention_map"]["mechanical_lines"] > 0, "annotations stored, attention map computed")
    st = d.status()
    check(st["drift_ok"] and st["plan_versions"] == 4 and len(st["repairs"]) == 3, "status reports drift, plan versions, repairs")
    text = d.show_units([cart_method, *U["assets/logo.png"]])
    check("total_with_code" in text and "binary file" in text, "show_units renders hunks and binary units")
    pub = d.publish(dry_run=True)
    check(pub["dry_run"] and pub["commands"][1].startswith("gh pr create --base main"), "publish dry-run: first PR targets main")
    check("--base diffract/feature-discounts/01" in pub["commands"][2], "publish dry-run: PR 2 stacks on slice 1's branch")
    author = sh("git", "log", "-1", "--format=%an <%ae>", m["materialized"]["slices"][0]["branch"])
    check(author == "Dev <dev@example.com>", "slice commits keep the PR author")

    print("\n10. requirement coverage from a design doc")
    d.set_requirements([{"id": "R1", "text": "Discount codes at checkout"},
                        {"id": "R2", "text": "Rename format_inr to format_money"},
                        {"id": "R3", "text": "Export receipts as PDF"}], source="design.docx")
    notes = ([{"unit": u, "kind": "behavioral", "requirement": "R1"}
              for u in (*U["shop/discounts.py"], *U["tests/test_discounts.py"], *U["tests/test_cart.py"])]
             + [{"unit": u, "kind": "mechanical", "requirement": "R2"} for u in (*U["shop/pricing.py"], *U["shop/receipt.py"])]
             + [{"unit": notes_1, "kind": "behavioral", "requirement": "none"}])
    gaps = d.annotate_units(notes)["requirement_gaps"]
    print("   " + "\n   ".join(gaps))
    check(any(g.startswith("R3 is in the requirements") for g in gaps), "flags a requirement that nothing implements")
    check(any("serves no requirement" in g and notes_1 in g for g in gaps), "flags a behavioural change outside every requirement")
    rows = {row["id"]: row for row in d.status()["requirements"]["requirements"]}
    check(rows["R1"]["status"] == "code + tests" and rows["R2"]["status"] == "mechanical only",
          "classifies R1 as code + tests and R2 as mechanical only")

    print("\n11. stack map report")
    rep = d.report(summary="Discount codes plus a rename, split for review.", slice_notes={"1": "Start with the cart wiring."})
    page = Path(rep["path"]).read_text(encoding="utf-8")
    check(Path(rep["path"]).name == "report.html" and '<svg class="prism"' in page, "report written with the prism view")
    check("Export receipts as PDF" in page and "Proof that nothing was lost" in page and "Start with the cart wiring." in page,
          "report shows requirements, the proof and Bob's notes")
    check("http://" not in page and "https://" not in page, "report is self-contained (no network)")
    run = json.loads(Path(rep["stack_json"]).read_text(encoding="utf-8"))
    check(run["generator"] == "diffract" and len(run["slices"]) == 4 and run["proof"]["zero_drift"],
          "stack.json holds the same run as data (4 slices, zero drift)")

    print("\n12. real publish against a local remote, with a stand-in gh")
    if os.name == "nt":
        print("  (skipped on Windows: the stand-in gh is a script)")
    else:
        await publish_for_real()
        await publish_failures()
        await publish_to_gitlab()

    print("\n13. automatic mode (what the GitHub Action runs)")
    import subprocess as sp
    env = dict(os.environ, DIFFRACT_WORKTREE_ROOT=str(TMP / "wt-auto"), DIFFRACT_HOME=str(TMP / "state-auto"))
    auto = sp.run([PY, str(Path(__file__).with_name("diffract_auto.py")), "--repo", str(REPO), "--base", "main",
                   "--head", "feature/discounts", "--verify", verify_cmd, "--budget", "40", "--summary", str(TMP / "auto.md")],
                  capture_output=True, text=True, env=env, timeout=600)
    summary = (TMP / "auto.md").read_text(encoding="utf-8") if (TMP / "auto.md").exists() else auto.stderr[-800:]
    check(auto.returncode == 0 and "zero drift" in summary and "Every slice passes" in summary,
          "rule-based plan, tested and repaired automatically: all green, zero drift")
    check("| 1 |" in summary and "passed" in summary, "the pull request comment has a slice table")
    red = sp.run([PY, str(Path(__file__).with_name("diffract_auto.py")), "--repo", str(REPO), "--base", "main",
                  "--head", "feature/discounts", "--verify", f'"{PY}" -c "import sys; sys.exit(1)"', "--budget", "40",
                  "--summary", str(TMP / "auto-red.md")], capture_output=True, text=True, env=env, timeout=300)
    red_summary = (TMP / "auto-red.md").read_text(encoding="utf-8") if (TMP / "auto-red.md").exists() else red.stderr[-500:]
    check(red.returncode == 0 and "base branch is red" in red_summary, "a red base branch stops early with a clear message")

    print("\n14. MCP protocol round-trip (stdio)")
    await mcp_roundtrip()

    print("\n15. cleanup")
    c = d.cleanup(delete_branches=True, delete_session=True)
    check(len(c["worktrees_removed"]) >= 4 and c["branches_deleted"], "worktrees and branches removed")
    print("\nALL CHECKS PASSED")


FAKE_GH = """#!{python}
import json, os, sys
log = os.environ["FAKE_GH_LOG"]
args = sys.argv[1:]
entry = {{"args": args}}
if args[:2] == ["repo", "view"]:
    print("acme/shop")
elif args[:2] == ["pr", "view"]:
    if os.environ.get("FAKE_GH_EXISTING"):
        print("https://github.com/acme/shop/pull/7")
    else:
        sys.exit(1)
elif args[:2] == ["pr", "create"]:
    created = sum(1 for line in open(log) if '"create"' in line) if os.path.exists(log) else 0
    print(f"https://github.com/acme/shop/pull/{{101 + created}}")
elif args[:2] == ["pr", "edit"] and "--base" in args and os.environ.get("FAKE_GH_FAIL_RETARGET"):
    sys.stderr.write("GraphQL: base branch not found")
    sys.exit(1)
elif args[:2] == ["pr", "edit"] and "--body-file" in args and os.environ.get("FAKE_GH_FAIL_EDIT"):
    sys.stderr.write("HTTP 502: server error")
    sys.exit(1)
elif args[:2] == ["pr", "edit"] and "--body-file" in args:
    entry["body"] = open(args[args.index("--body-file") + 1]).read()
with open(log, "a") as handle:
    handle.write(json.dumps(entry) + "\\n")
"""


async def publish_for_real() -> None:
    remote = TMP / "remote.git"
    sh("git", "init", "-q", "--bare", str(remote), cwd=TMP)
    sh("git", "remote", "add", "origin", str(remote))
    fake_bin = TMP / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text(FAKE_GH.format(python=PY), encoding="utf-8")
    os.chmod(fake_bin / "gh", 0o755)
    log = TMP / "gh-calls.jsonl"
    saved_path = os.environ["PATH"]
    os.environ.update(PATH=f"{fake_bin}{os.pathsep}{saved_path}", FAKE_GH_LOG=str(log))
    try:
        out = d.publish(dry_run=False, bodies={"1": "Wires discount codes into the cart."})
    finally:
        os.environ["PATH"] = saved_path
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    creates = [c["args"] for c in calls if c["args"][:2] == ["pr", "create"]]
    statuses = [c["args"] for c in calls if c["args"][:1] == ["api"]]
    bodies = [c["body"] for c in calls if "body" in c]
    branches = sh("git", "ls-remote", "--heads", str(remote)).count("refs/heads/diffract/")
    check(branches == len(creates) == len(out["prs"]) == 4, f"pushed {branches} branches and opened {len(creates)} stacked PRs")
    check(creates[1][creates[1].index("--base") + 1] == creates[0][creates[0].index("--head") + 1],
          "PR 2 targets PR 1's branch")
    check(len(statuses) == 8 and all("state=success" in a for a in statuses), "posted 8 green commit statuses (2 per PR)")
    check({"context=diffract/zero-drift", "context=diffract/tests"} <= {x for a in statuses for x in a},
          "statuses are diffract/zero-drift and diffract/tests")
    check(all("review and merge in order" in b and "ZERO DRIFT" in b for b in bodies) and "Wires discount codes" in bodies[0],
          "every PR body carries the stack table and the certificate")
    check(out["status_checks"][0] == {"slice": 1, "diffract/zero-drift": "success", "diffract/tests": "success"},
          "publish reports the posted statuses")


FAKE_GLAB = """#!{python}
import json, os, sys
log = os.environ["FAKE_GLAB_LOG"]
args = sys.argv[1:]
with open(log, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
method = args[args.index("--method") + 1] if "--method" in args else "GET"
endpoint = args[-1] if method == "GET" else next(a for a in args if a.startswith("projects/"))
if method == "GET" and "/merge_requests?" in endpoint:
    print("[]")
elif method == "POST" and endpoint.endswith("/merge_requests"):
    created = sum(1 for line in open(log, encoding="utf-8") if "POST" in line and line.rstrip().endswith('merge_requests"]') or '"projects/:fullpath/merge_requests", "-f"' in line)
    print(json.dumps({{"iid": created, "web_url": "https://gitlab.com/acme/shop/-/merge_requests/%d" % created}}))
else:
    print("{{}}")
"""


async def publish_to_gitlab() -> None:
    fake_bin = TMP / "fake-glab"
    fake_bin.mkdir()
    (fake_bin / "glab").write_text(FAKE_GLAB.format(python=PY), encoding="utf-8")
    os.chmod(fake_bin / "glab", 0o755)
    log = TMP / "glab-calls.jsonl"
    saved_path = os.environ["PATH"]
    os.environ.update(PATH=f"{fake_bin}{os.pathsep}{saved_path}", FAKE_GLAB_LOG=str(log))
    try:
        dry = d.publish(dry_run=True, forge="gitlab")
        out = d.publish(dry_run=False, forge="gitlab", bodies={"1": "Wires discount codes into the cart."})
    finally:
        os.environ["PATH"] = saved_path
    check(dry["forge"] == "gitlab" and "projects/:fullpath/merge_requests" in dry["commands"][1], "GitLab dry run lists merge request commands")
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    creates = [c for c in calls if "POST" in c and "projects/:fullpath/merge_requests" in c]
    targets = [next(a for a in c if a.startswith("target_branch=")) for c in creates]
    check(len(creates) == len(out["merge_requests"]) == 4 and targets[0] == "target_branch=main"
          and targets[1] == "target_branch=" + next(a for a in creates[0] if a.startswith("source_branch=")).split("=", 1)[1],
          "GitLab: 4 stacked merge requests, each targeting the one before")
    statuses = [c for c in calls if any(a.startswith("projects/:fullpath/statuses/") for a in c)]
    check(len(statuses) == 8 and all("state=success" in c for c in statuses), "GitLab: 8 green commit statuses posted")
    described = [c for c in calls if "PUT" in c and any(a.startswith("description=") and "review and merge in order" in a for a in c)]
    check(len(described) == 4, "GitLab: every merge request description carries the stack table")


async def publish_failures() -> None:
    fake_bin = TMP / "fake-bin"
    saved_path = os.environ["PATH"]
    os.environ.update(PATH=f"{fake_bin}{os.pathsep}{saved_path}", FAKE_GH_FAIL_EDIT="1")
    try:
        out = d.publish(dry_run=False)
    finally:
        os.environ["PATH"] = saved_path
        os.environ.pop("FAKE_GH_FAIL_EDIT", None)
    check(len(out.get("description_problems", [])) == 4 and len(out["prs"]) == 4,
          "failed PR description updates are reported, not silently ignored")
    os.environ.update(PATH=f"{fake_bin}{os.pathsep}{saved_path}", FAKE_GH_EXISTING="1", FAKE_GH_FAIL_RETARGET="1")
    retarget_error = ""
    try:
        d.publish(dry_run=False)
    except d.DiffractError as error:
        retarget_error = str(error)
    finally:
        os.environ["PATH"] = saved_path
        for key in ("FAKE_GH_EXISTING", "FAKE_GH_FAIL_RETARGET"):
            os.environ.pop(key, None)
    check("could not point the existing PR" in retarget_error,
          "a failed re-target of an existing PR stops publishing with a clear error")


async def mcp_roundtrip() -> None:
    try:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError as e:
        print(f"  (skipped: {e})")
        return
    params = StdioServerParameters(command=PY, args=[str(Path(__file__).with_name("diffract_mcp.py"))],
                                   env=dict(os.environ))
    async with stdio_client(params) as (read, write_):
        async with ClientSession(read, write_) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            expected = {"start_session", "set_requirements", "show_units", "annotate_units", "set_plan", "move_units",
                        "merge_slices", "verify", "locate", "probe", "drift_check", "status", "report", "publish", "cleanup",
                        "facts"}
            check(set(names) == expected, f"server lists all {len(names)} tools over stdio")
            res = await session.call_tool("status", {"repo_path": str(REPO)})
            flag = getattr(res, "isError", getattr(res, "is_error", False))
            check(not flag, "status tool callable over MCP")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        if not KEEP:
            shutil.rmtree(TMP, ignore_errors=True)
