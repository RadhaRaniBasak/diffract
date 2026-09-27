#!/usr/bin/env python3

import asyncio
import fnmatch
import hashlib
import contextlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from diffract_report import render_report

try:
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:
    from mcp.server.fastmcp import FastMCP as _Server

INSTRUCTIONS = """Diffract splits one large PR into a stack of small PRs.
Order of use: start_session -> (show_units / annotate_units) -> set_plan -> verify ->
(move_units / merge_slices -> verify)* -> drift_check -> status -> publish.
Never edit source files to make a slice pass: only move units between slices or merge slices.
The stack tip must always equal the PR head; drift_check proves it."""

server = _Server("diffract", instructions=INSTRUCTIONS)

STATE_HOME = Path(os.environ.get("DIFFRACT_HOME", str(Path.home() / ".cache" / "diffract"))).expanduser()
SESSION_DIRNAME = ".diffract"
DEFAULT_BUDGET_EXCLUDE = [
    "*package-lock.json", "*npm-shrinkwrap.json", "*yarn.lock", "*pnpm-lock.yaml", "*bun.lockb",
    "*poetry.lock", "*Pipfile.lock", "*uv.lock", "*Cargo.lock", "*go.sum", "*composer.lock",
    "*Gemfile.lock", "*.snap", "*.min.js", "*.min.css",
]
LOCKFILES = {Path(p.lstrip("*")).name.lower() for p in DEFAULT_BUDGET_EXCLUDE if "lock" in p or p.endswith("go.sum")}
MANIFESTS = {"package.json", "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "go.mod", "cargo.toml",
             "composer.json", "gemfile", "pom.xml", "build.gradle", "build.gradle.kts", "tsconfig.json"}
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|specs?|testing)/|(^|/)test_[^/]*\.py$|_test\.(py|go|rb)$"
    r"|\.(test|spec)\.[^/]+$"
)
JVM_TEST_RE = re.compile(r"(^|/)[A-Z][A-Za-z0-9_]*Tests?\.(java|kt|cs)$")
HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
REGULAR_MODES = {"100644", "100755"}
MAX_EVENTS = 200


class DiffractError(Exception):
    pass


def _git(repo: Path, *args: str, stdin: bytes | None = None, env: dict[str, str] | None = None,
         check: bool = True) -> bytes:
    full_env = dict(os.environ)
    full_env["GIT_LITERAL_PATHSPECS"] = "1"
    full_env["GIT_TERMINAL_PROMPT"] = "0"
    if env:
        full_env.update(env)
    proc = subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True, env=full_env)
    if check and proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
        raise DiffractError(f"`git {' '.join(args[:4])}` failed: {msg or 'exit ' + str(proc.returncode)}")
    return proc.stdout


def _gtext(repo: Path, *args: str, **kw: Any) -> str:
    return _git(repo, *args, **kw).decode("utf-8", "replace").strip()


def _git_ok(repo: Path, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True).returncode == 0


def _toplevel(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.exists():
        raise DiffractError(f"Path does not exist: {p}")
    try:
        return Path(_gtext(p, "rev-parse", "--show-toplevel")).resolve()
    except DiffractError:
        raise DiffractError(f"Not inside a git repository: {p}") from None


def _commit(root: Path, ref: str) -> str:
    try:
        return _gtext(root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    except DiffractError:
        raise DiffractError(f"Cannot resolve '{ref}' to a commit (fetch it first?)") from None


def _short_branch(root: Path, ref: str) -> str | None:
    try:
        full = _gtext(root, "rev-parse", "--symbolic-full-name", ref)
    except DiffractError:
        return None
    if full.startswith("refs/heads/"):
        return full[len("refs/heads/"):]
    if full.startswith("refs/remotes/") and full.count("/") >= 3:
        return full.split("/", 3)[3]
    return None


def _default_base(root: Path) -> str:
    try:
        ref = _gtext(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        return ref.replace("refs/remotes/", "", 1)
    except DiffractError:
        pass
    for cand in ("main", "master", "origin/main", "origin/master", "develop"):
        if _git_ok(root, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}"):
            return cand
    raise DiffractError("Could not detect the base branch; pass base explicitly (e.g. 'main').")


def _blob(root: Path, sha: str) -> bytes:
    return _git(root, "cat-file", "blob", sha)


def _split_lines(data: bytes) -> list[bytes]:
    if not data:
        return []
    parts = data.split(b"\n")
    lines = [p + b"\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _compose(base_lines: list[bytes], head_lines: list[bytes], hunks: list[dict]) -> bytes:
    out: list[bytes] = []
    pos = 0
    for h in sorted(hunks, key=lambda h: h["old_lo"]):
        out.extend(base_lines[pos:h["old_lo"]])
        out.extend(head_lines[h["new_lo"]:h["new_hi"]])
        pos = h["old_hi"]
    out.extend(base_lines[pos:])
    return b"".join(out)


_current_root: Path | None = None


def _root(repo_path: str | None) -> Path:
    global _current_root
    if repo_path:
        root = _toplevel(repo_path)
    elif _current_root is not None:
        root = _current_root
    else:
        ptr = STATE_HOME / "current_repo"
        if ptr.exists() and ptr.read_text(encoding="utf-8").strip():
            root = Path(ptr.read_text(encoding="utf-8").strip())
        else:
            raise DiffractError("No active session: pass repo_path, or call start_session first.")
    _current_root = root
    return root


def _set_current(root: Path) -> None:
    global _current_root
    _current_root = root
    try:
        STATE_HOME.mkdir(parents=True, exist_ok=True)
        (STATE_HOME / "current_repo").write_text(str(root), encoding="utf-8")
    except OSError:
        pass


def _sdir(root: Path) -> Path:
    return root / SESSION_DIRNAME


def _load(root: Path) -> dict:
    f = _sdir(root) / "session.json"
    if not f.exists():
        raise DiffractError(f"No Diffract session in {root}. Call start_session first.")
    return json.loads(f.read_text(encoding="utf-8"))


def _save(root: Path, s: dict) -> None:
    d = _sdir(root)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "session.json.tmp"
    tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
    tmp.replace(d / "session.json")


def _event(s: dict, event: str, **data: Any) -> None:
    s.setdefault("events", []).append({"t": round(time.time(), 1), "event": event, **data})
    s["events"] = s["events"][-MAX_EVENTS:]


def _ensure_excluded(root: Path) -> None:
    common = Path(_gtext(root, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = (root / common).resolve()
    excl = common / "info" / "exclude"
    excl.parent.mkdir(parents=True, exist_ok=True)
    text = excl.read_text(encoding="utf-8", errors="replace") if excl.exists() else ""
    if f"/{SESSION_DIRNAME}/" not in text.splitlines():
        with excl.open("a", encoding="utf-8") as fh:
            fh.write(("\n" if text and not text.endswith("\n") else "") + f"/{SESSION_DIRNAME}/\n")


def _path_hints(path: str) -> list[str]:
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    hints = []
    if TEST_PATH_RE.search(lower) or JVM_TEST_RE.search(path):
        hints.append("test")
    if name in LOCKFILES:
        hints.append("lockfile")
    if name.endswith(".snap") or "/__snapshots__/" in f"/{lower}":
        hints.append("snapshot")
    if Path(name).suffix in {".md", ".rst", ".adoc", ".txt"} or lower.startswith("docs/") or "/docs/" in lower:
        hints.append("docs")
    if re.search(r"(^|/)(migrations?|alembic|db/migrate)/", lower):
        hints.append("migration")
    if re.search(r"\.min\.(js|css)$|(^|/)(dist|build|vendor|generated)/", lower):
        hints.append("generated")
    return hints


def _budget_lines(path: str, added: int, removed: int, binary: bool, exclude: list[str]) -> int:
    if binary or any(fnmatch.fnmatchcase(path, pat) for pat in exclude):
        return 0
    return added + removed


def _ordered(s: dict, ids: set[str] | list[str]) -> list[str]:
    pos = {u: i for i, u in enumerate(s["unit_order"])}
    return sorted(set(ids), key=lambda u: pos[u])


def _compress(ids: list[str]) -> str:
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            out.append(run[0] if len(run) == 1 else f"{run[0]}-{run[-1]}")
            run.clear()

    for uid in ids:
        if run and uid[0] == run[-1][0] and int(uid[1:]) == int(run[-1][1:]) + 1:
            run.append(uid)
        else:
            flush()
            run.append(uid)
    flush()
    return ", ".join(out)


def _resolve(s: dict, selectors: list[str]) -> tuple[list[str], list[str]]:
    units = s["units"]
    found: list[str] = []
    errors: list[str] = []
    for raw in selectors:
        sel = raw.strip()
        if not sel:
            continue
        if sel.lower().startswith("path:"):
            pat = sel[5:].strip()
            hits = [u for u in s["unit_order"] if fnmatch.fnmatchcase(units[u]["path"], pat)]
            if not hits:
                errors.append(f"selector '{raw}' matched no units")
            found += hits
            continue
        m = re.fullmatch(r"([hf])(\d+)\s*-\s*([hf])?(\d+)", sel.lower())
        if m:
            if m[3] and m[3] != m[1]:
                errors.append(f"range '{raw}' mixes unit kinds")
                continue
            width = len(next(iter(units))) - 1 if units else 3
            lo, hi = int(m[2]), int(m[4])
            ids = [f"{m[1]}{i:0{width}d}" for i in range(lo, hi + 1)]
            missing = [i for i in ids if i not in units]
            if missing:
                errors.append(f"range '{raw}' includes unknown units: {_compress(missing)}")
            found += [i for i in ids if i in units]
            continue
        uid = sel.lower()
        if uid in units:
            found.append(uid)
        else:
            errors.append(f"unknown unit '{raw}'")
    return found, errors


def _fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _describe_file_unit(root: Path, f: dict, u: dict, max_lines: int) -> str:
    change = u["change"]
    if change == "added" and not f["binary"] and f["new_mode"] in REGULAR_MODES:
        lines = _blob(root, f["new_sha"]).decode("utf-8", "replace").splitlines()
        body = [f"new file (mode {f['new_mode']}), {len(lines)} lines"] + ["+" + ln for ln in lines[:max_lines]]
        if len(lines) > max_lines:
            body.append(f"... ({len(lines) - max_lines} more lines)")
        return "\n".join(body)
    if change == "deleted" and not f["binary"] and f["old_mode"] in REGULAR_MODES:
        lines = _blob(root, f["old_sha"]).decode("utf-8", "replace").splitlines()
        shown = min(max_lines, 40)
        body = [f"deleted file, {len(lines)} lines"] + ["-" + ln for ln in lines[:shown]]
        if len(lines) > shown:
            body.append(f"... ({len(lines) - shown} more lines)")
        return "\n".join(body)
    if f["binary"]:
        def size(sha: str) -> str:
            return "absent" if set(sha) == {"0"} else _gtext(root, "cat-file", "-s", sha) + " bytes"
        return f"binary file ({change}): {size(f['old_sha'])} -> {size(f['new_sha'])}"
    if change == "mode":
        return f"mode change only: {f['old_mode']} -> {f['new_mode']}"
    if change == "submodule":
        return f"submodule pointer: {f['old_sha'][:12]} -> {f['new_sha'][:12]}"
    if change in ("symlink", "type-change"):
        return f"{change}: mode {f['old_mode']} -> {f['new_mode']}"
    diff = _git(root, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames",
                f["_a"], f["_b"], "--", u["path"]).decode("utf-8", "replace").replace("\r", "")
    lines = diff.splitlines()
    return "\n".join(lines[:max_lines] + ([f"... ({len(lines) - max_lines} more lines)"] if len(lines) > max_lines else []))


def _render_unit(root: Path, s: dict, uid: str, max_lines: int = 250) -> str:
    u = s["units"][uid]
    f = dict(s["files"][u["path"]], _a=s["root"], _b=s["head"])
    head = f"### {uid} · {u['path']} · +{u['added']} -{u['removed']}"
    if u["hints"]:
        head += " · " + ", ".join(u["hints"])
    ann = s.get("annotations", {}).get(uid)
    if ann:
        head += f" · [{ann['kind']}{(' ' + ann['requirement']) if ann.get('requirement') else ''}]"
    if u["kind"] == "hunk":
        lines = u["diff"].splitlines()
        body = "\n".join(lines[:max_lines] + ([f"... ({len(lines) - max_lines} more lines)"] if len(lines) > max_lines else []))
    else:
        head += f" · whole-file change ({u['change']})"
        body = _describe_file_unit(root, f, u, max_lines)
    fence = _fence(body)
    return f"{head}\n{fence}diff\n{body}\n{fence}\n"


def _slice_stats(s: dict, sl: dict) -> dict:
    us = [s["units"][u] for u in sl["units"]]
    ann = s.get("annotations", {})
    lines = sum(u["lines"] for u in us)
    return {
        "title": sl["title"],
        "units": _compress(sl["units"]),
        "unit_count": len(us),
        "files": len({u["path"] for u in us}),
        "added": sum(u["added"] for u in us),
        "removed": sum(u["removed"] for u in us),
        "lines": lines,
        "mechanical_lines": sum(u["lines"] for u in us if ann.get(u["id"], {}).get("kind") == "mechanical"),
        "behavioral_lines": sum(u["lines"] for u in us if ann.get(u["id"], {}).get("kind") == "behavioral"),
        "over_budget": lines > s["config"]["line_budget"],
    }


def _plan_warnings(s: dict) -> list[str]:
    warns = []
    ann = s.get("annotations", {})
    budget = s["config"]["line_budget"]
    for k, sl in enumerate(s["plan"]["slices"], 1):
        st = _slice_stats(s, sl)
        if st["over_budget"]:
            mech = st["mechanical_lines"] >= 0.8 * st["lines"] and st["lines"] > 0
            warns.append(f"slice {k} '{sl['title']}' has {st['lines']} budgeted lines (> {budget})"
                         + (" but is mostly mechanical, which is acceptable if labelled 'skim'" if mech else
                            "; split it further unless it is one cohesive change"))
        if sl.get("requirement"):
            stray = [u for u in sl["units"] if ann.get(u, {}).get("requirement", "").strip().lower() in {"none", "unrelated", "-"}]
            if stray:
                warns.append(f"slice {k} implements '{sl['requirement']}' but contains unrelated units "
                             f"{_compress(stray)}; consider an 'Unrelated changes' slice")
    return warns


def _validate_plan(s: dict, specs: list[dict]) -> tuple[list[dict], list[str], dict]:
    errors: list[str] = []
    slices: list[dict] = []
    owner: dict[str, list[int]] = {}
    if not specs:
        errors.append("plan has no slices")
    for k, spec in enumerate(specs, 1):
        title = (spec.get("title") or "").strip()
        if not title:
            errors.append(f"slice {k} has no title")
        ids, errs = _resolve(s, list(spec.get("units") or []))
        errors += [f"slice {k}: {e}" for e in errs]
        if not ids and not errs:
            errors.append(f"slice {k} selects no units")
        for u in dict.fromkeys(ids):
            owner.setdefault(u, []).append(k)
        slices.append({
            "title": title or f"Slice {k}",
            "rationale": (spec.get("rationale") or "").strip(),
            "requirement": (spec.get("requirement") or "").strip(),
            "units": _ordered(s, ids),
        })
    missing = [u for u in s["unit_order"] if u not in owner]
    dupes = {u: ks for u, ks in owner.items() if len(ks) > 1}
    if missing:
        errors.append(f"{len(missing)} unit(s) not assigned to any slice: {_compress(missing)}")
    if dupes:
        errors.append("units assigned to more than one slice: "
                      + "; ".join(f"{u} -> slices {ks}" for u, ks in list(dupes.items())[:20]))
    return slices, errors, {"unassigned": _compress(missing), "duplicates": dupes}


def _wt_root(root: Path) -> Path:
    base = Path(os.environ.get("DIFFRACT_WORKTREE_ROOT", str(STATE_HOME / "worktrees"))).expanduser()
    return base / f"{root.name}-{hashlib.sha1(str(root).encode()).hexdigest()[:8]}"


def _registered_worktrees(root: Path) -> set[Path]:
    out = _gtext(root, "worktree", "list", "--porcelain")
    return {Path(line[9:]).resolve() for line in out.splitlines() if line.startswith("worktree ")}


def _checkout_worktree(root: Path, path: Path, commit: str, registered: set[Path]) -> bool:
    if path.exists() and path.resolve() in registered:
        _git(path, "checkout", "--detach", "--force", "--quiet", commit)
        _git(path, "clean", "-fdq")
        return False
    if path.exists():
        shutil.rmtree(path)
    _git(root, "worktree", "prune")
    _git(root, "worktree", "add", "--detach", "--force", str(path), commit)
    return True


def _remove_worktree(root: Path, path: Path) -> None:
    _git(root, "worktree", "remove", "--force", str(path), check=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _link_shared(root: Path, wt: Path, shared: list[str]) -> list[str]:
    linked = []
    for rel in shared:
        src, dst = root / rel, wt / rel
        if not src.exists() or dst.exists() or dst.is_symlink():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(src, dst, target_is_directory=src.is_dir())
        except OSError:
            if os.name == "nt" and src.is_dir():
                subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)], capture_output=True)
            else:
                raise
        linked.append(rel)
    return linked


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _tail(path: Path, max_lines: int = 60, max_chars: int = 4000) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    text = ANSI_RE.sub("", text).replace("\r", "")
    tail = "\n".join(text.splitlines()[-max_lines:])
    return tail[-max_chars:]


async def _run(cmd: str, cwd: Path, timeout: int, log_path: Path) -> dict:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, CI="1", NO_COLOR="1", FORCE_COLOR="0")
    start = time.monotonic()
    kw: dict[str, Any] = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                          else {"start_new_session": True})
    with open(log_path, "wb") as fh:
        fh.write(f"$ {cmd}\n# cwd: {cwd}\n".encode())
        fh.flush()
        proc = await asyncio.create_subprocess_shell(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                                     stdout=fh, stderr=subprocess.STDOUT, env=env, **kw)
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=timeout)
            status = "pass" if code == 0 else "fail"
        except asyncio.TimeoutError:
            _kill_tree(proc.pid)
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
            code, status = None, "timeout"
    return {"status": status, "exit_code": code, "seconds": round(time.monotonic() - start, 1),
            "log": str(log_path), "tail": _tail(log_path)}


def _identity_env(root: Path, head: str) -> dict[str, str]:
    an, ae, ad, cd = _gtext(root, "log", "-1", "--format=%an%x00%ae%x00%ad%x00%cd", "--date=raw", head).split("\x00")
    env = {"GIT_AUTHOR_NAME": an, "GIT_AUTHOR_EMAIL": ae, "GIT_AUTHOR_DATE": ad,
           "GIT_COMMITTER_DATE": cd}
    if not _gtext(root, "config", "user.name", check=False):
        env["GIT_COMMITTER_NAME"] = "Diffract"
    if not _gtext(root, "config", "user.email", check=False):
        env["GIT_COMMITTER_EMAIL"] = "diffract@localhost"
    return env


class _TreeWriter:

    def __init__(self, root: Path, s: dict):
        self.root, self.s = root, s
        self.zero_sha = "0" * len(s["head"])
        self.env = _identity_env(root, s["head"])
        handle, self.index_path = tempfile.mkstemp(prefix="diffract-index-")
        os.close(handle)
        os.unlink(self.index_path)
        self.env["GIT_INDEX_FILE"] = self.index_path
        self.file_lines: dict[str, tuple[list[bytes], list[bytes]]] = {}

    def close(self) -> None:
        Path(self.index_path).unlink(missing_ok=True)

    def _entry_for(self, path: str, selected: set[str]) -> tuple[str, str, str]:
        units, file_info = self.s["units"], self.s["files"][path]
        if units[file_info["units"][0]]["kind"] == "file":
            if file_info["status"] == "D":
                return "0", self.zero_sha, path
            return file_info["new_mode"], file_info["new_sha"], path
        chosen = [u for u in file_info["units"] if u in selected]
        if len(chosen) == len(file_info["units"]):
            return file_info["new_mode"], file_info["new_sha"], path
        if path not in self.file_lines:
            self.file_lines[path] = (_split_lines(_blob(self.root, file_info["old_sha"])),
                                     _split_lines(_blob(self.root, file_info["new_sha"])))
        content = _compose(*self.file_lines[path], [units[u] for u in chosen])
        return file_info["new_mode"], _gtext(self.root, "hash-object", "-w", "--stdin", stdin=content), path

    def tree(self, selected: set[str], label: str) -> str:
        _git(self.root, "read-tree", self.s["root"], env=self.env)
        paths = sorted({self.s["units"][u]["path"] for u in selected})
        records = [self._entry_for(path, selected) for path in paths]
        if records:
            payload = b"".join(f"{mode} {sha}\t".encode() + path.encode("utf-8", "surrogateescape") + b"\0"
                              for mode, sha, path in records)
            try:
                _git(self.root, "update-index", "-z", "--index-info", stdin=payload, env=self.env)
            except DiffractError as error:
                raise DiffractError(f"{label} cannot be built ({error}). This usually means a file/directory swap "
                                    "was split across slices; put those units in the same slice.") from None
        return _gtext(self.root, "write-tree", env=self.env)

    def commit(self, tree: str, parent: str, message: str) -> str:
        return _gtext(self.root, "commit-tree", tree, "-p", parent, stdin=message.encode(), env=self.env)


def _build_stack(root: Path, s: dict) -> list[dict]:
    slices = s["plan"]["slices"]
    prefix = f"diffract/{s['stack_name']}"
    writer = _TreeWriter(root, s)
    selected: set[str] = set()
    parent = s["root"]
    built = []
    try:
        for number, sl in enumerate(slices, 1):
            selected.update(sl["units"])
            tree = writer.tree(selected, f"slice {number}")
            message = f"{sl['title']}\n\n"
            if sl.get("rationale"):
                message += sl["rationale"] + "\n\n"
            if sl.get("requirement"):
                message += f"Requirement: {sl['requirement']}\n"
            message += (f"Diffract: slice {number}/{len(slices)} of {s['head_ref'] or s['head'][:12]} "
                        f"(units {_compress(sl['units'])})\n")
            commit = writer.commit(tree, parent, message)
            branch = f"{prefix}/{number:02d}-{_slug(sl['title'])}"
            _git(root, "update-ref", f"refs/heads/{branch}", commit)
            parent = commit
            built.append({"slice": number, "title": sl["title"], "branch": branch, "commit": commit, "tree": tree})
    finally:
        writer.close()
    keep = {f"refs/heads/{entry['branch']}" for entry in built}
    for ref in _gtext(root, "for-each-ref", "--format=%(refname)", f"refs/heads/{prefix}/").splitlines():
        if ref not in keep:
            _git(root, "update-ref", "-d", ref)
    return built


def _slug(text: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n].rstrip("-") or "slice"


async def _setup_all(s: dict, worktrees: list[Path]) -> list[dict]:
    cmd = s["config"]["setup_command"]
    if not cmd or not worktrees:
        return []
    sem = asyncio.Semaphore(max(1, min(len(worktrees), os.cpu_count() or 4)))

    async def one(wt: Path) -> dict:
        async with sem:
            r = await _run(cmd, wt, s["config"]["timeout_seconds"], wt.parent / "logs" / f"setup-{wt.name}.log")
            return {"worktree": wt.name, "status": r["status"], "seconds": r["seconds"],
                    **({"log": r["log"], "tail": r["tail"]} if r["status"] != "pass" else {})}

    return list(await asyncio.gather(*(one(w) for w in worktrees)))


async def _materialize(root: Path, s: dict) -> dict:
    stack = _build_stack(root, s)
    wroot = _wt_root(root)
    wroot.mkdir(parents=True, exist_ok=True)
    registered = _registered_worktrees(root)
    created: list[Path] = []
    for o in stack:
        wt = wroot / f"s{o['slice']:02d}"
        if _checkout_worktree(root, wt, o["commit"], registered):
            created.append(wt)
        _link_shared(root, wt, s["config"]["shared_paths"])
        o["worktree"] = str(wt)
    for child in (wroot.iterdir() if wroot.exists() else []):
        m = re.fullmatch(r"s(\d{2})", child.name)
        if m and int(m[1]) > len(stack):
            _remove_worktree(root, child)
    setup = await _setup_all(s, created)
    s["materialized"] = {"plan_version": s["plan"]["version"], "slices": stack}
    s.setdefault("setup_failed", {})
    for r in setup:
        if r["status"] == "pass":
            s["setup_failed"].pop(r["worktree"], None)
        else:
            s["setup_failed"][r["worktree"]] = r.get("log", "")
    drift_ok = stack[-1]["tree"] == s["head_tree"]
    _event(s, "materialize", plan_version=s["plan"]["version"], slices=len(stack), drift_ok=drift_ok)
    return {
        "slices": [{"slice": o["slice"], "branch": o["branch"], "commit": o["commit"][:12],
                    **{k: v for k, v in _slice_stats(s, s["plan"]["slices"][o["slice"] - 1]).items()
                       if k in ("title", "lines", "files", "over_budget")}} for o in stack],
        "worktree_root": str(wroot),
        "setup": setup,
        "drift_ok": drift_ok,
    }


def _current_status(s: dict, tree: str) -> str:
    cmd = s["config"]["verify_command"]
    r = s.get("verify_cache", {}).get(f"{tree}|{cmd}")
    return r["status"] if r else "unverified"


DEFINITION_MODIFIERS = (r"(?:(?:export|default|public|private|protected|internal|static|async|final|abstract|"
                        r"override|pub(?:\([^)]*\))?|open|sealed|data|inline|suspend|declare)\s+)*")
DEFINITION_KEYWORDS = r"(?:def|class|function\*?|interface|type|enum|struct|trait|const|let|var|val|fun|fn)"
CONTROL_WORDS = {"return", "await", "new", "throw", "yield", "else", "if", "for", "while", "case", "not", "and", "or"}

FAILED_TEST_PATTERNS = [
    re.compile(r"^(?:FAILED|ERROR) ([^\s:]+\.py)::(\S+?)(?:\[[^\]]*\])?(?: - .*)?$", re.M),
    re.compile(r"^\s*--- FAIL: (\w+)", re.M),
    re.compile(r"^\[ERROR\]\s+(?:[\w.]+\.)?(\w+)\.(\w+)[:(]", re.M),
]
MISSING_SYMBOL_PATTERNS = [
    re.compile(r"cannot import name '(\w+)'"),
    re.compile(r"NameError: name '(\w+)' is not defined"),
    re.compile(r"has no attribute '(\w+)'"),
    re.compile(r"No module named '[\w.]*?(\w+)'"),
    re.compile(r"Cannot find name '(\w+)'"),
    re.compile(r"has no exported member(?: named)? '(\w+)'"),
    re.compile(r"Property '(\w+)' does not exist on type"),
    re.compile(r"Cannot find module '(?:[^']*/)?([\w.-]+?)(?:\.\w+)?'"),
    re.compile(r"ReferenceError: (\w+) is not defined"),
    re.compile(r"TypeError: (?:[\w$.]+\.)?(\w+) is not a function"),
    re.compile(r"symbol:\s+(?:method|class|variable|interface)\s+(\w+)"),
    re.compile(r"undefined: (?:\w+\.)?(\w+)"),
    re.compile(r"cannot find (?:value|function|type|struct|trait|macro) `(\w+)`"),
    re.compile(r"unresolved import `(?:[\w:]*::)?(\w+)`"),
]


def _indentation(line: str) -> int:
    return len(line) - len(line.lstrip())


def _is_definition(line: str, name: str) -> bool:
    escaped = re.escape(name)
    stripped = line.strip()
    if re.match(rf"\s*{DEFINITION_MODIFIERS}{DEFINITION_KEYWORDS}\s+{escaped}\b", line):
        return True
    if re.match(rf"\s*func\s+(?:\([^)]*\)\s*)?{escaped}\s*[\[(]", line):
        return True
    if re.match(rf"\s*(?:(?:async|static|get|set|public|private|protected)\s+)*{escaped}\s*\([^)]*\)\s*(?::\s*[^{{]+)?\{{\s*$", line):
        return True
    typed = re.match(rf"\s*{DEFINITION_MODIFIERS}((?:[\w<>\[\],.?]+\s+)+){escaped}\s*\(", line)
    if typed and typed.group(1).split()[0] not in CONTROL_WORDS and stripped.endswith(("{", ")", ",")):
        return True
    return False


def _definition_block(lines: list[str], start: int) -> tuple[int, int]:
    indent = _indentation(lines[start])
    top = start
    index = start - 1
    while index >= 0:
        stripped = lines[index].strip()
        if not stripped:
            break
        line_indent = _indentation(lines[index])
        if line_indent > indent:
            index -= 1
            continue
        if line_indent == indent and stripped.startswith(("@", "#", "//", "/*", "*")):
            top = index
            index -= 1
            continue
        if line_indent == indent and stripped[0] in ")]":
            index -= 1
            continue
        break
    last = start
    index = start + 1
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped:
            line_indent = _indentation(lines[index])
            if line_indent > indent:
                last = index
            elif line_indent == indent and stripped[0] in "})]":
                last = index
                break
            else:
                break
        index += 1
    return top, last + 1


def _units_touching_symbol(root: Path, s: dict, name: str, only_path: str | None = None,
                           sides: tuple[str, ...] = ("head", "base"), cache: dict | None = None) -> list[dict]:
    cache = {} if cache is None else cache
    matches = []
    for path, file_info in s["files"].items():
        if only_path and path != only_path:
            continue
        if file_info["binary"]:
            continue
        for side in sides:
            sha, mode = ((file_info["new_sha"], file_info["new_mode"]) if side == "head"
                         else (file_info["old_sha"], file_info["old_mode"]))
            if set(sha) == {"0"} or mode not in REGULAR_MODES:
                continue
            key = (path, side)
            if key not in cache:
                cache[key] = [line.decode("utf-8", "replace").rstrip("\r\n")
                              for line in _split_lines(_blob(root, sha))]
            lines = cache[key]
            for number, line in enumerate(lines):
                if name not in line or not _is_definition(line, name):
                    continue
                top, bottom = _definition_block(lines, number)
                touching = []
                for unit_id in file_info["units"]:
                    unit = s["units"][unit_id]
                    if unit["kind"] == "file":
                        touching.append(unit_id)
                        continue
                    low, high = ((unit["new_lo"], unit["new_hi"]) if side == "head" else (unit["old_lo"], unit["old_hi"]))
                    overlaps = (low < bottom and high > top) if high > low else (top <= low <= bottom)
                    if overlaps:
                        touching.append(unit_id)
                matches.append({"path": path, "side": side, "lines": f"{top + 1}-{bottom}", "units": touching})
    return matches


def _units_importing(s: dict, name: str) -> list[str]:
    pattern = re.compile(rf"^\+.*(?:\bimport\b|\brequire\(|^\+\s*use\s).*\b{re.escape(name)}\b", re.M)
    return [unit_id for unit_id in s["unit_order"]
            if s["units"][unit_id]["kind"] == "hunk" and pattern.search(s["units"][unit_id]["diff"])]


def _units_adding_module(s: dict, name: str) -> list[str]:
    hits = []
    for unit_id in s["unit_order"]:
        path = PurePosixPath(s["units"][unit_id]["path"])
        if path.stem == name or (path.stem in ("__init__", "index", "mod") and path.parent.name == name):
            hits.append(unit_id)
    return hits


def _units_dropping_references(s: dict, name: str) -> list[str]:
    pattern = re.compile(rf"^-(?!--).*\b{re.escape(name)}\b", re.M)
    return [unit_id for unit_id in s["unit_order"]
            if s["units"][unit_id]["kind"] == "hunk" and pattern.search(s["units"][unit_id]["diff"])]


def _read_log(path: str, max_bytes: int = 2_000_000) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - max_bytes))
            return ANSI_RE.sub("", handle.read().decode("utf-8", "replace"))
    except OSError:
        return ""


def _failure_clues(log_text: str) -> tuple[list[tuple[str | None, str]], list[str]]:
    failing_tests: dict[tuple[str | None, str], None] = {}
    for pattern in FAILED_TEST_PATTERNS:
        for match in pattern.finditer(log_text):
            if pattern.groups == 2 and match.group(1).endswith(".py"):
                failing_tests[(match.group(1), match.group(2).split("::")[-1])] = None
            elif pattern.groups == 2:
                failing_tests[(None, match.group(2))] = None
            else:
                failing_tests[(None, match.group(1))] = None
    missing = {}
    for pattern in MISSING_SYMBOL_PATTERNS:
        for match in pattern.finditer(log_text):
            missing[match.group(1)] = None
    return list(failing_tests)[:40], list(missing)[:20]


def _suggest_repair(root: Path, s: dict, failing_slice: int, log_path: str) -> dict:
    failing_tests, missing_symbols = _failure_clues(_read_log(log_path))
    slice_of = {unit: number for number, sl in enumerate(s["plan"]["slices"], 1) for unit in sl["units"]}
    reasons: dict[str, str] = {}
    cache: dict = {}
    for path, test_name in failing_tests:
        for match in _units_touching_symbol(root, s, test_name, path, cache=cache):
            for unit in match["units"]:
                if slice_of[unit] > failing_slice:
                    reasons.setdefault(unit, f"changes failing test {test_name} (now in slice {slice_of[unit]})")
    for symbol in missing_symbols:
        for match in _units_touching_symbol(root, s, symbol, sides=("head",), cache=cache):
            for unit in match["units"]:
                if slice_of[unit] > failing_slice:
                    reasons.setdefault(unit, f"defines missing symbol {symbol} (now in slice {slice_of[unit]})")
        for unit in _units_importing(s, symbol):
            if slice_of[unit] > failing_slice:
                reasons.setdefault(unit, f"imports missing symbol {symbol} (now in slice {slice_of[unit]})")
        for unit in _units_adding_module(s, symbol):
            if slice_of[unit] > failing_slice:
                reasons.setdefault(unit, f"adds missing module {symbol} (now in slice {slice_of[unit]})")
        for unit in _units_dropping_references(s, symbol):
            if slice_of[unit] > failing_slice:
                reasons.setdefault(unit, f"stops using {symbol}, which slices 1..{failing_slice} remove "
                                         f"(now in slice {slice_of[unit]})")
    clues = {"failed_tests": [f"{p}::{n}" if p else n for p, n in failing_tests[:20]],
             "missing_symbols": missing_symbols}
    if not reasons:
        return {**clues, "suggested_repair": None}
    units = _ordered(s, reasons)
    return {**clues, "suggested_repair": {
        "call": f"move_units(units={json.dumps(units)}, to_slice={failing_slice})",
        "units": _compress(units),
        "lines": sum(s["units"][u]["lines"] for u in units),
        "why": [f"{u}: {reasons[u]}" for u in units[:12]] + ([f"... and {len(units) - 12} more"] if len(units) > 12 else []),
    }}


def _write_slice_files(root: Path, s: dict) -> list[str]:
    folder = _sdir(root) / "slices"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    annotations = s.get("annotations", {})
    slices = s["plan"]["slices"]
    written = []
    for number, sl in enumerate(slices, 1):
        stats = _slice_stats(s, sl)
        behavioral = [u for u in sl["units"] if annotations.get(u, {}).get("kind") == "behavioral"]
        header = [
            f"# Slice {number} of {len(slices)}: {sl['title']}",
            f"Requirement: {sl['requirement'] or 'none stated'}",
            f"Rationale: {sl['rationale'] or '-'}",
            f"Size: {stats['lines']} budgeted lines in {stats['files']} files "
            f"(+{stats['added']} -{stats['removed']}); mechanical {stats['mechanical_lines']}, "
            f"behavioral {stats['behavioral_lines']}",
            f"Review closely: {_compress(behavioral) or 'not annotated'}",
            f"Verified with: `{s['config']['verify_command']}` on slices 1..{number}",
            "",
        ]
        path = folder / f"slice-{number:02d}.md"
        path.write_text("\n".join(header + [_render_unit(root, s, u) for u in sl["units"]]), encoding="utf-8")
        written.append(str(path))
    return written


NO_REQUIREMENT = {"", "none", "unrelated", "-", "n/a"}


def _requirement_ids(label: str, known_ids: list[str]) -> list[str]:
    if not label or label.strip().lower() in NO_REQUIREMENT:
        return []
    if known_ids:
        return [rid for rid in known_ids if re.search(rf"(?<![\w-]){re.escape(rid)}(?![\w-])", label, re.IGNORECASE)]
    return re.findall(r"\b[A-Z]{1,6}-?\d+\b", label)


def _natural_key(text: str) -> list:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def _requirement_coverage(s: dict) -> dict:
    annotations = s.get("annotations", {})
    texts = {r["id"]: r["text"] for r in s.get("requirements") or []}
    ids = _known_requirement_ids(s)
    slice_of = {unit: number for number, sl in enumerate((s["plan"] or {}).get("slices", []), 1) for unit in sl["units"]}
    rows = {rid: {"code": [], "tests": [], "docs": [], "lines": 0, "behavioral_lines": 0, "slices": set(),
                  "docs_slices": set()} for rid in ids}
    serves_nothing = []
    for unit_id in s["unit_order"]:
        note = annotations.get(unit_id)
        if not note:
            continue
        unit = s["units"][unit_id]
        mapped = _requirement_ids(note.get("requirement", ""), ids)
        if not mapped:
            if note["kind"] == "behavioral" and "test" not in unit["hints"]:
                serves_nothing.append(unit_id)
            continue
        role = "tests" if "test" in unit["hints"] else "docs" if "docs" in unit["hints"] else "code"
        for rid in mapped:
            row = rows[rid]
            row[role].append(unit_id)
            row["lines"] += unit["lines"]
            if role == "code" and note["kind"] == "behavioral":
                row["behavioral_lines"] += unit["lines"]
            if unit_id in slice_of:
                row["docs_slices" if role == "docs" else "slices"].add(slice_of[unit_id])

    table, not_implemented, untested = [], [], []
    for rid in ids:
        row = rows[rid]
        if not (row["code"] or row["tests"] or row["docs"]):
            status = "not in this PR"
            not_implemented.append(f"{rid} is in the requirements, but no change in this PR implements it")
        elif row["code"] and row["tests"]:
            status = "code + tests"
        elif row["code"] and row["behavioral_lines"]:
            status = "no test changes"
            numbers = sorted(row["slices"])
            where = (f" in slice {numbers[0]}" if len(numbers) == 1 else
                     f" in slices {', '.join(map(str, numbers))}" if numbers else "")
            untested.append((row["behavioral_lines"],
                             f"{rid} changes {row['behavioral_lines']} behavioural lines{where} with no test changes"))
        elif row["code"]:
            status = "mechanical only"
        elif row["tests"]:
            status = "tests only"
        else:
            status = "docs only"
        table.append({"id": rid, "text": texts.get(rid, ""), "status": status, "slices": sorted(row["slices"]),
                      "docs_slices": sorted(row["docs_slices"] - row["slices"]),
                      "code_units": _compress(row["code"]), "test_units": _compress(row["tests"]),
                      "docs_units": _compress(row["docs"]), "lines": row["lines"],
                      "behavioral_lines": row["behavioral_lines"]})
    gaps = not_implemented + [text for _lines, text in sorted(untested, key=lambda item: -item[0])]
    if serves_nothing:
        lines = sum(s["units"][u]["lines"] for u in serves_nothing)
        count = len(serves_nothing)
        gaps.append(f"{count} behavioural unit{'s' if count != 1 else ''} ({lines} lines) "
                    f"serve{'s' if count == 1 else ''} no requirement: {_compress(serves_nothing)}")
    unannotated = [u for u in s["unit_order"] if u not in annotations]
    return {"source": s.get("requirements_source", ""), "requirements": table, "gaps": gaps,
            "serves_no_requirement": _compress(serves_nothing),
            "unannotated_units": len(unannotated)}


def _drift_facts(root: Path, s: dict) -> dict:
    stack = s["materialized"]["slices"]
    stack_tree = _gtext(root, "rev-parse", f"{stack[-1]['commit']}^{{tree}}")
    linear, parent = True, s["root"]
    for entry in stack:
        if _gtext(root, "rev-parse", f"{entry['commit']}^") != parent:
            linear = False
        parent = entry["commit"]
    assigned = [u for sl in s["plan"]["slices"] for u in sl["units"]]
    once = len(assigned) == len(set(assigned)) == len(s["units"])
    identical = stack_tree == s["head_tree"]
    return {"stack_tree": stack_tree, "head_tree": s["head_tree"], "trees_identical": identical,
            "linear_chain": linear, "units_once": once, "zero_drift": identical and linear and once}


def _known_requirement_ids(s: dict) -> list[str]:
    known = [r["id"] for r in s.get("requirements") or []]
    if known:
        return known
    return sorted({rid for note in s.get("annotations", {}).values()
                   for rid in _requirement_ids(note.get("requirement", ""), [])}, key=_natural_key)


def _requirement_presence(s: dict) -> dict[str, dict[int, set[str]]]:
    ids = _known_requirement_ids(s)
    slice_of = {unit: number for number, sl in enumerate((s["plan"] or {}).get("slices", []), 1) for unit in sl["units"]}
    presence: dict[str, dict[int, set[str]]] = {}
    for unit_id, note in s.get("annotations", {}).items():
        if unit_id not in slice_of:
            continue
        hints = s["units"][unit_id]["hints"]
        role = "tests" if "test" in hints else "docs" if "docs" in hints else "code"
        for rid in _requirement_ids(note.get("requirement", ""), ids):
            presence.setdefault(rid, {}).setdefault(slice_of[unit_id], set()).add(role)
    return presence


def _timeline_entries(s: dict) -> list[dict]:
    start = s["created_at"]
    entries = []
    for event in s.get("events", []):
        kind, via = event["event"], event.get("via")
        if kind == "start":
            text = f"Parsed {event['units']} units in {event['files']} files"
        elif kind == "plan" and via == "set_plan":
            text = f"Planned {event['slices']} slices (plan v{event['version']})"
        elif kind == "plan" and via == "move_units":
            text = f"Repair: moved {event.get('moved')} into slice {event.get('to')}"
        elif kind == "plan" and via == "merge_slices":
            text = f"Repair: merged slices {' and '.join(map(str, event.get('merged', [])))}"
        elif kind == "verify":
            ran, cached, failed = event.get("ran", []), event.get("cached", []), event.get("failed", [])
            if ran == [0] and not cached:
                text = "Checked the base: " + ("red" if 0 in failed else "green")
            else:
                together = " in parallel" if event.get("parallel", 1) > 1 else ""
                text = (f"Tested {len(ran)} slice{'s' if len(ran) != 1 else ''}{together} "
                        f"({event.get('wall_seconds', 0):g} s)" + (f", {len(cached)} reused from cache" if cached else "")
                        + (f"; red: {', '.join(map(str, failed))}" if failed else "; all green"))
        elif kind == "probe":
            green = event.get("green") or []
            together = " in parallel" if event.get("parallel", 1) > 1 else ""
            text = (f"Probed slice {event['slice']} against {event['candidates']} candidates{together}: "
                    + (f"green with slice {green[0]}" if green else "no single slice fixes it"))
        elif kind == "drift_check":
            text = "Proved zero drift" if event.get("zero_drift") else "Drift detected"
        elif kind == "publish":
            text = f"Published {len(event.get('urls', {}))} pull requests"
        else:
            continue
        entries.append({"seconds": max(0.0, event["t"] - start), "text": text})
    return entries


def _report_view(root: Path, s: dict, title: str, summary: str, notes: dict[str, str]) -> dict:
    if not s["plan"] or not s["materialized"]:
        raise DiffractError("Build and verify the stack first (set_plan, verify, drift_check), then call report.")
    if s["materialized"]["plan_version"] != s["plan"]["version"]:
        raise DiffractError("The plan changed since the last build: run verify and drift_check again first.")
    built = {entry["slice"]: entry for entry in s["materialized"]["slices"]}
    command = s["config"]["verify_command"]
    cache = s.get("verify_cache", {})
    slices = []
    for number, sl in enumerate(s["plan"]["slices"], 1):
        stats = _slice_stats(s, sl)
        result = cache.get(f"{built[number]['tree']}|{command}", {})
        slices.append({
            "number": number, "title": sl["title"], "requirement": sl.get("requirement", ""),
            "lines": stats["lines"], "mechanical_lines": stats["mechanical_lines"],
            "behavioral_lines": stats["behavioral_lines"], "files": stats["files"], "over_budget": stats["over_budget"],
            "status": result.get("status", "unverified"), "flaky": bool(result.get("flaky")),
            "branch": built[number]["branch"], "note": notes.get(str(number), "").strip(),
        })
    coverage = _requirement_coverage(s)
    presence = _requirement_presence(s)
    events = s.get("events", [])
    work_events = [e["t"] for e in events if e["event"] in ("plan", "verify", "probe", "drift_check")]
    head = s["head_ref"] or s["head"][:12]
    base = s["base_ref"] or s["base"][:12]
    return {
        "title": title.strip() or f"Diffract stack map: {head} into {base}",
        "head": head, "base": base, "summary": summary.strip(),
        "original": {"lines": sum(u["lines"] for u in s["units"].values()), "files": len(s["files"]),
                     "units": len(s["units"])},
        "budget": s["config"]["line_budget"],
        "slices": slices,
        "requirements": [{**row, "per_slice": presence.get(row["id"], {})} for row in coverage["requirements"]],
        "requirements_source": coverage["source"],
        "gaps": coverage["gaps"],
        "timeline": _timeline_entries(s),
        "proof": _drift_facts(root, s),
        "elapsed_minutes": round((max(work_events) - s["created_at"]) / 60, 1) if work_events else 0,
        "repairs": sum(1 for e in events if e["event"] == "plan" and e.get("via") in ("move_units", "merge_slices")),
        "verify_runs": sum(1 for e in events if e["event"] == "verify"),
        "verify_command": command,
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


def _planned_statuses(s: dict, stack: list[dict]) -> list[dict]:
    command = s["config"]["verify_command"]
    cache = s.get("verify_cache", {})
    drift = (f"Stack tip tree {stack[-1]['tree'][:12]} equals the original PR's tree; "
             f"{len(s['units'])} changes in {len(stack)} slices")
    planned = []
    for entry in stack:
        result = cache.get(f"{entry['tree']}|{command}", {})
        number = entry["slice"]
        scope = "slice 1" if number == 1 else f"slices 1-{number}"
        if result.get("status") == "pass":
            tests = ("success", f"Tests pass on {scope} in isolation" + (" (after one retry)" if result.get("flaky") else ""))
        elif result:
            tests = ("failure", f"Tests fail on {scope} in isolation")
        else:
            tests = ("pending", f"Not verified yet on {scope}")
        planned.append({"slice": number, "sha": entry["commit"], "zero_drift": ("success", drift[:140]),
                        "tests": (tests[0], tests[1][:140])})
    return planned


def _post_statuses(root: Path, planned: list[dict]) -> tuple[list[dict], list[str]]:
    view = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                          cwd=root, capture_output=True, text=True)
    slug = view.stdout.strip()
    if view.returncode != 0 or "/" not in slug:
        return [], [f"could not resolve the GitHub repo for statuses: {(view.stderr or view.stdout).strip()}"]
    posted, problems = [], []
    for item in planned:
        states = {}
        for context, (state, description) in (("diffract/zero-drift", item["zero_drift"]), ("diffract/tests", item["tests"])):
            call = subprocess.run(["gh", "api", "--method", "POST", f"repos/{slug}/statuses/{item['sha']}",
                                   "-f", f"state={state}", "-f", f"context={context}", "-f", f"description={description}"],
                                  cwd=root, capture_output=True, text=True)
            if call.returncode == 0:
                states[context] = state
            else:
                problems.append(f"slice {item['slice']} {context}: {(call.stderr or call.stdout).strip()[:200]}")
        posted.append({"slice": item["slice"], **states})
    return posted, problems


def _stack_document(view: dict) -> dict:
    requirements = [{**{key: value for key, value in row.items() if key != "per_slice"},
                     "per_slice": {str(number): sorted(roles) for number, roles in row["per_slice"].items()}}
                    for row in view["requirements"]]
    return {"generator": "diffract", "schema": 1,
            **{key: value for key, value in view.items() if key != "requirements"}, "requirements": requirements}


def _detect_forge(root: Path, remote: str) -> str:
    url = _gtext(root, "remote", "get-url", remote, check=False).lower()
    return "gitlab" if "gitlab" in url else "github"


def _glab_json(root: Path, *args: str) -> Any:
    run = subprocess.run(["glab", "api", *args], cwd=root, capture_output=True, text=True, encoding="utf-8")
    if run.returncode != 0:
        raise DiffractError(f"glab api {' '.join(args[-1:])} failed: {(run.stderr or run.stdout).strip()[:300]}")
    return json.loads(run.stdout) if run.stdout.strip() else None


def _publish_gitlab(root: Path, s: dict, stack: list[dict], base: str, bodies: dict[str, str], push_cmd: list[str],
                    dry_run: bool, draft: bool, title_prefix: str, status_checks: bool) -> dict[str, Any]:
    total = len(stack)
    titles = {entry["slice"]: f"{'Draft: ' if draft else ''}{title_prefix}[{entry['slice']}/{total}] {entry['title']}"
              for entry in stack}
    targets = {entry["slice"]: base if entry["slice"] == 1 else stack[entry["slice"] - 2]["branch"] for entry in stack}
    statuses = _planned_statuses(s, stack)
    if dry_run:
        commands = [" ".join(push_cmd)] + [
            f'glab api --method POST projects/:fullpath/merge_requests -f source_branch={entry["branch"]} '
            f'-f target_branch={targets[entry["slice"]]} -f title="{titles[entry["slice"]]}"' for entry in stack]
        if status_checks:
            commands.append(f"glab api --method POST projects/:fullpath/statuses/<sha> for each merge request: "
                            f"diffract/zero-drift and diffract/tests ({len(statuses) * 2} statuses)")
        return {"dry_run": True, "forge": "gitlab", "commands": commands,
                "note": "Show these to the user; call publish(dry_run=False, bodies=...) only after approval."}
    if not shutil.which("glab"):
        raise DiffractError("GitLab CLI `glab` not found on PATH (install it and run `glab auth login`).")
    proof = drift_check(str(root))
    if not proof.get("zero_drift", proof["statement"].startswith("ZERO DRIFT")):
        raise DiffractError("Drift detected: the stack tip does not match the PR head. Not publishing.")
    push = subprocess.run(push_cmd, cwd=root, capture_output=True, text=True)
    if push.returncode != 0:
        raise DiffractError(f"git push failed: {push.stderr.strip()}")
    iids: dict[int, int] = {}
    urls: dict[int, str] = {}
    for entry in stack:
        number = entry["slice"]
        existing = _glab_json(root, f"projects/:fullpath/merge_requests?state=opened&source_branch={quote(entry['branch'], safe='')}")
        if existing:
            iids[number], urls[number] = existing[0]["iid"], existing[0]["web_url"]
            _glab_json(root, "--method", "PUT", f"projects/:fullpath/merge_requests/{iids[number]}",
                       "-f", f"target_branch={targets[number]}", "-f", f"title={titles[number]}")
        else:
            created = _glab_json(root, "--method", "POST", "projects/:fullpath/merge_requests",
                                 "-f", f"source_branch={entry['branch']}", "-f", f"target_branch={targets[number]}",
                                 "-f", f"title={titles[number]}", "-f", f"description={bodies.get(str(number), entry['title'])}")
            iids[number], urls[number] = created["iid"], created["web_url"]
    statement = proof["statement"]
    description_problems: list[str] = []
    for entry in stack:
        number = entry["slice"]
        table = "\n".join(f"{j}. {'**' if j == number else ''}[{stack[j - 1]['title']}]({urls[j]})"
                          f"{'** (this merge request)' if j == number else ''}" for j in range(1, total + 1))
        description = (f"{bodies.get(str(number), entry['title'])}\n\n---\n**Stack: review and merge in order.**\n{table}"
                       f"\n\n_Split with Diffract. {statement}_")
        try:
            _glab_json(root, "--method", "PUT", f"projects/:fullpath/merge_requests/{iids[number]}", "-f", f"description={description}")
        except DiffractError as error:
            description_problems.append(f"merge request {number}: description not updated: {error}")
    posted, problems = [], []
    if status_checks:
        gitlab_states = {"success": "success", "failure": "failed", "pending": "pending"}
        for item in statuses:
            states = {}
            for name, (state, text) in (("diffract/zero-drift", item["zero_drift"]), ("diffract/tests", item["tests"])):
                try:
                    _glab_json(root, "--method", "POST", f"projects/:fullpath/statuses/{item['sha']}",
                               "-f", f"state={gitlab_states[state]}", "-f", f"name={name}", "-f", f"description={text}")
                    states[name] = gitlab_states[state]
                except DiffractError as error:
                    problems.append(f"slice {item['slice']} {name}: {error}")
            posted.append({"slice": item["slice"], **states})
    s = _load(root)
    _event(s, "publish", urls=urls, forge="gitlab")
    _save(root, s)
    result: dict[str, Any] = {"dry_run": False, "forge": "gitlab",
                              "merge_requests": [{"slice": k, "url": u} for k, u in sorted(urls.items())]}
    if description_problems:
        result["description_problems"] = description_problems
    if status_checks:
        result["status_checks"] = posted
        if problems:
            result["status_check_problems"] = problems
    return result


RepoPath = Annotated[str | None, Field(description="Absolute path to the target repository. Optional after "
                                                   "start_session: defaults to the active session.")]


class SliceSpec(BaseModel):
    title: str = Field(description="Short PR title in the imperative, e.g. 'Rename price_with_tax to apply_tax'")
    units: list[str] = Field(description="Unit selectors: ids ('h012', 'f003'), ranges ('h010-h018') "
                                         "or path globs ('path:src/api/*', '*' also matches '/')")
    rationale: str = Field("", description="Why this slice exists and what the reviewer should know (1-3 sentences)")
    requirement: str = Field("", description="Requirement ids from the design doc this slice implements, e.g. 'R2, R3'")


class UnitNote(BaseModel):
    unit: str = Field(description="Unit id, e.g. 'h012'")
    kind: Literal["mechanical", "behavioral"] = Field(
        description="mechanical = rename/move/format/generated, safe to skim; behavioral = changes what the code does")
    requirement: str = Field("", description="Requirement id it serves (e.g. 'R2'), or 'none' if it maps to no requirement")
    note: str = Field("", description="Optional: symbols it defines/uses, dependencies, anything the planner should know")


def _as_dict(x: Any) -> dict:
    return x.model_dump() if isinstance(x, BaseModel) else dict(x)


@server.tool()
def start_session(
    repo_path: Annotated[str, Field(description="Absolute path to the target repository (any directory inside it)")],
    head: Annotated[str, Field(description="The PR to split: branch, ref or SHA. Default: HEAD")] = "HEAD",
    base: Annotated[str, Field(description="Branch the PR targets, e.g. 'main' or 'origin/main'. Empty: auto-detect")] = "",
    verify_command: Annotated[str, Field(description="Shell command that builds and tests ONE slice, run from the slice "
                                                     "worktree root, e.g. 'npm run build && npm test' or 'python -m pytest -q'")] = "",
    setup_command: Annotated[str, Field(description="Optional one-time command per new worktree, e.g. 'npm ci'. "
                                                    "Prefer shared_paths when dependencies can be shared")] = "",
    shared_paths: Annotated[list[str] | None, Field(description="Paths symlinked from the main checkout into every "
                                                                "worktree, e.g. ['node_modules'] (install deps at the PR head first)")] = None,
    line_budget: Annotated[int, Field(description="Max budgeted changed lines per slice (Cisco study: ~200-400)")] = 400,
    context_lines: Annotated[int, Field(description="Diff context lines. 3 = git default; 0 = finest-grained hunks")] = 3,
    budget_exclude: Annotated[list[str] | None, Field(description="Path globs that don't count toward the budget. "
                                                                  "Default: lockfiles, snapshots, minified files")] = None,
    timeout_seconds: Annotated[int, Field(description="Timeout for each verify/setup run")] = 900,
) -> dict[str, Any]:
    """Start (or restart) a Diffract session: resolve the PR, parse its diff into units, and write
    per-batch unit files for parallel review. Returns a compact overview. Call this first."""
    root = _toplevel(repo_path)
    base = base or _default_base(root)
    head_sha, base_sha = _commit(root, head), _commit(root, base)
    stack_root = _gtext(root, "merge-base", base_sha, head_sha)
    if stack_root == head_sha:
        raise DiffractError(f"'{head}' has no commits beyond '{base}': nothing to split.")
    head_ref = _short_branch(root, head)
    cfg = {
        "verify_command": verify_command.strip(),
        "setup_command": setup_command.strip(),
        "shared_paths": list(shared_paths or []),
        "line_budget": int(line_budget),
        "context_lines": max(0, int(context_lines)),
        "budget_exclude": list(budget_exclude) if budget_exclude is not None else list(DEFAULT_BUDGET_EXCLUDE),
        "timeout_seconds": int(timeout_seconds),
    }
    raw = _git(root, "diff", "--raw", "-z", "--no-abbrev", "--no-renames", "--no-ext-diff", stack_root, head_sha)
    toks = raw.split(b"\0")
    changes = []
    i = 0
    while i < len(toks):
        if toks[i].startswith(b":"):
            om, nm, osha, nsha, st = toks[i][1:].decode().split()[:5]
            changes.append({"path": toks[i + 1].decode("utf-8", "surrogateescape"), "status": st[0],
                            "old_mode": om, "new_mode": nm, "old_sha": osha, "new_sha": nsha})
            i += 2
        else:
            i += 1
    numstat: dict[str, tuple[int | None, int | None]] = {}
    for rec in _git(root, "diff", "--numstat", "-z", "--no-renames", "--no-ext-diff", stack_root, head_sha).split(b"\0"):
        parts = rec.split(b"\t", 2)
        if len(parts) == 3:
            numstat[parts[2].decode("utf-8", "surrogateescape")] = (
                (None, None) if parts[0] == b"-" else (int(parts[0]), int(parts[1])))

    pending: list[dict] = []
    files: dict[str, dict] = {}
    for ch in sorted(changes, key=lambda c: c["path"]):
        path, st = ch["path"], ch["status"]
        add, rem = numstat.get(path, (0, 0))
        binary = add is None
        add, rem = add or 0, rem or 0
        hints = _path_hints(path)
        files[path] = {**ch, "binary": binary, "added": add, "removed": rem, "units": [], "hints": hints}
        hunks: list[dict] = []
        if st == "M" and ch["old_mode"] in REGULAR_MODES and ch["new_mode"] in REGULAR_MODES and not binary:
            out = _git(root, "-c", "diff.suppressBlankEmpty=false", "diff", "--no-color", "--no-ext-diff",
                       "--no-textconv", "--no-renames", f"-U{cfg['context_lines']}", stack_root, head_sha, "--", path)
            cur = None
            for line in out.split(b"\n"):
                m = HUNK_RE.match(line)
                if m:
                    oc = int(m[2]) if m[2] is not None else 1
                    nc = int(m[4]) if m[4] is not None else 1
                    olo = int(m[1]) - 1 if oc > 0 else int(m[1])
                    nlo = int(m[3]) - 1 if nc > 0 else int(m[3])
                    cur = {"old_lo": olo, "old_hi": olo + oc, "new_lo": nlo, "new_hi": nlo + nc, "body": [line]}
                    hunks.append(cur)
                elif cur is not None and line[:1] in (b"+", b"-", b" ", b"\\"):
                    cur["body"].append(line)
            if hunks:
                bl, hl = _split_lines(_blob(root, ch["old_sha"])), _split_lines(_blob(root, ch["new_sha"]))
                spans = sorted((h["old_lo"], h["old_hi"]) for h in hunks)
                sane = all(a[1] <= b[0] for a, b in zip(spans, spans[1:])) and all(
                    0 <= h["old_lo"] <= h["old_hi"] <= len(bl) and 0 <= h["new_lo"] <= h["new_hi"] <= len(hl) for h in hunks)
                if not (sane and _compose(bl, hl, hunks) == _blob(root, ch["new_sha"])):
                    hunks = []
                    hints.append("atomic-fallback")
        if hunks:
            for h in hunks:
                removed = [ln[1:] for ln in h["body"][1:] if ln.startswith(b"-")]
                added = [ln[1:] for ln in h["body"][1:] if ln.startswith(b"+")]
                uh = list(hints)
                if (removed or added) and re.sub(rb"\s+", b"", b"".join(removed)) == re.sub(rb"\s+", b"", b"".join(added)):
                    uh.append("whitespace-only")
                pending.append({"kind": "hunk", "path": path, "old_lo": h["old_lo"], "old_hi": h["old_hi"],
                                "new_lo": h["new_lo"], "new_hi": h["new_hi"], "added": len(added), "removed": len(removed),
                                "binary": False, "hints": uh,
                                "diff": b"\n".join(h["body"]).decode("utf-8", "replace").replace("\r", "")})
        else:
            if "160000" in (ch["old_mode"], ch["new_mode"]):
                change = "submodule"
            elif st == "A":
                change = "added"
            elif st == "D":
                change = "deleted"
            elif st == "T":
                change = "type-change"
            elif binary:
                change = "binary"
            elif "120000" in (ch["old_mode"], ch["new_mode"]):
                change = "symlink"
            elif ch["old_mode"] != ch["new_mode"] and add == 0 and rem == 0:
                change = "mode"
            else:
                change = "modified"
            uh = list(hints) + (["binary"] if binary else [])
            pending.append({"kind": "file", "path": path, "change": change, "added": add, "removed": rem,
                            "binary": binary, "hints": uh})

    n_h = sum(1 for p in pending if p["kind"] == "hunk")
    width = max(3, len(str(max(n_h, len(pending) - n_h, 1))))
    units: dict[str, dict] = {}
    order: list[str] = []
    counters = {"hunk": 0, "file": 0}
    for p in pending:
        counters[p["kind"]] += 1
        uid = f"{'h' if p['kind'] == 'hunk' else 'f'}{counters[p['kind']]:0{width}d}"
        p["id"] = uid
        p["lines"] = _budget_lines(p["path"], p["added"], p["removed"], p.pop("binary"), cfg["budget_exclude"])
        units[uid] = p
        order.append(uid)
        files[p["path"]]["units"].append(uid)

    s = {
        "version": 1, "repo": str(root), "created_at": time.time(),
        "base_ref": _short_branch(root, base), "head_ref": head_ref,
        "base": base_sha, "head": head_sha, "root": stack_root,
        "head_tree": _gtext(root, "rev-parse", f"{head_sha}^{{tree}}"),
        "stack_name": _slug(head_ref or f"pr-{head_sha[:8]}", 50),
        "config": cfg, "files": files, "units": units, "unit_order": order,
        "annotations": {}, "plan": None, "plan_history": [], "materialized": None,
        "verify_cache": {}, "events": [],
    }
    _event(s, "start", units=len(units), files=len(files))

    _ensure_excluded(root)
    sd = _sdir(root)
    if (sd / "batches").exists():
        shutil.rmtree(sd / "batches")
    (sd / "batches").mkdir(parents=True, exist_ok=True)
    total_lines = sum(u["lines"] for u in units.values())
    target = max(700, math.ceil(total_lines / 8))
    batches: list[list[str]] = []
    cur_paths: list[str] = []
    cur_lines = 0
    for path, f in files.items():
        fl = max(5, sum(units[u]["lines"] for u in f["units"]))
        if cur_paths and cur_lines + fl > target:
            batches.append(cur_paths)
            cur_paths, cur_lines = [], 0
        cur_paths.append(path)
        cur_lines += fl
    if cur_paths:
        batches.append(cur_paths)
    batch_info = []
    for bi, paths in enumerate(batches, 1):
        ids = [u for p in paths for u in files[p]["units"]]
        doc = [f"# Diffract batch {bi} of {len(batches)}",
               f"PR {head_ref or head_sha[:12]} vs {s['base_ref'] or base_sha[:12]} · {len(paths)} files · units {_compress(ids)}",
               "Each unit is one hunk (h...) or one whole-file change (f...). Line numbers refer to the base version.", ""]
        doc += [_render_unit(root, s, u) for u in ids]
        bf = sd / "batches" / f"batch-{bi:02d}.md"
        bf.write_text("\n".join(doc), encoding="utf-8")
        batch_info.append({"batch": bi, "file": str(bf), "files": len(paths), "units": _compress(ids),
                           "lines": sum(units[u]["lines"] for u in ids)})
    _save(root, s)
    _set_current(root)

    file_rows = [{"path": p, "status": f["status"], "units": _compress(f["units"]), "+": f["added"], "-": f["removed"],
                  **({"hints": f["hints"]} if f["hints"] else {})} for p, f in files.items()]
    dirty = _gtext(root, "status", "--porcelain", "--untracked-files=no") if head == "HEAD" else ""
    return {
        "session": {"repo": str(root), "base": s["base_ref"] or base_sha[:12], "head": head_ref or head_sha[:12],
                    "stack_root": stack_root[:12], "stack_name": s["stack_name"]},
        "totals": {"files": len(files), "units": len(units), "hunk_units": n_h, "file_units": len(units) - n_h,
                   "added": sum(f["added"] for f in files.values()), "removed": sum(f["removed"] for f in files.values()),
                   "budgeted_lines": total_lines, "line_budget": cfg["line_budget"],
                   "minimum_slices": max(1, math.ceil(total_lines / max(1, cfg["line_budget"])))},
        "files": file_rows[:80] + ([{"note": f"{len(file_rows) - 80} more files; use show_units"}] if len(file_rows) > 80 else []),
        "batches": batch_info,
        "warnings": (["uncommitted changes in the working tree are NOT part of the split (commit them first)"] if dirty else [])
                    + ([] if cfg["verify_command"] else ["no verify_command set: pass one to verify()"]),
        "next": "Review each batch file (one explore subagent per batch, in parallel), then annotate_units and set_plan.",
    }


@server.tool()
def show_units(
    units: Annotated[list[str] | None, Field(description="Unit selectors: ids, ranges 'h010-h018', or 'path:<glob>'")] = None,
    max_chars: Annotated[int, Field(description="Truncate output after this many characters")] = 20000,
    repo_path: RepoPath = None,
) -> str:
    """Show the diff of specific units (hunks or whole-file changes) as markdown."""
    root = _root(repo_path)
    s = _load(root)
    ids, errors = _resolve(s, units or [])
    if not ids:
        raise DiffractError("; ".join(errors) or "Pass unit selectors, e.g. ['h001-h010', 'path:src/api/*'].")
    out, used, shown = [], 0, 0
    for uid in dict.fromkeys(ids):
        block = _render_unit(root, s, uid)
        if used + len(block) > max_chars and shown:
            rest = [u for u in dict.fromkeys(ids)][shown:]
            out.append(f"... truncated: {len(rest)} more units ({_compress(rest)}); request them separately.")
            break
        out.append(block)
        used += len(block)
        shown += 1
    if errors:
        out.insert(0, "Warnings: " + "; ".join(errors) + "\n")
    return "\n".join(out)


class Requirement(BaseModel):
    id: str = Field(description="Requirement id as written in the document, e.g. 'R4'")
    text: str = Field(description="The requirement in at most ~15 words")


@server.tool()
def set_requirements(
    requirements: Annotated[list[Requirement], Field(description="Requirements from the design doc, ticket or spec, in order")],
    source: Annotated[str, Field(description="Where they came from, e.g. 'q2-release-plan.docx'")] = "",
    repo_path: RepoPath = None,
) -> dict[str, Any]:
    """Record the requirements from the design doc (call right after start_session). Diffract then maps
    units and slices to them and flags gaps: requirements nothing implements, behavioural changes with
    no test changes, and behavioural changes that serve no requirement."""
    root = _root(repo_path)
    s = _load(root)
    stored, seen = [], set()
    for item in requirements:
        entry = _as_dict(item)
        requirement_id = entry["id"].strip()
        if requirement_id and requirement_id.lower() not in seen:
            seen.add(requirement_id.lower())
            stored.append({"id": requirement_id, "text": entry.get("text", "").strip()})
    if not stored:
        raise DiffractError("No requirements given.")
    s["requirements"] = stored
    s["requirements_source"] = source.strip()
    _save(root, s)
    return {"stored": [r["id"] for r in stored], "source": s["requirements_source"],
            "next": "Annotate units with these ids (annotate_units); coverage and gaps appear in its result and in status()."}


@server.tool()
def annotate_units(
    notes: Annotated[list[UnitNote], Field(description="One entry per unit from the review of the batch files")],
    repo_path: RepoPath = None,
) -> dict[str, Any]:
    """Store the classification of units (mechanical vs behavioral, requirement served). Used for the
    attention map in PR descriptions, plan warnings, and the final report."""
    root = _root(repo_path)
    s = _load(root)
    unknown = []
    for n in notes:
        d = _as_dict(n)
        uid = d["unit"].strip().lower()
        if uid not in s["units"]:
            unknown.append(d["unit"])
            continue
        s["annotations"][uid] = {"kind": d["kind"], "requirement": d.get("requirement", ""), "note": d.get("note", "")}
    _save(root, s)
    ann = s["annotations"]
    total = sum(u["lines"] for u in s["units"].values())
    mech = sum(s["units"][u]["lines"] for u, a in ann.items() if a["kind"] == "mechanical")
    missing = [u for u in s["unit_order"] if u not in ann]
    return {"annotated": len(ann), "of": len(s["units"]), "unknown_ids": unknown,
            "not_yet_annotated": _compress(missing),
            "attention_map": {"mechanical_lines": mech, "total_budgeted_lines": total,
                              "skimmable_pct": round(100 * mech / total, 1) if total else 0.0},
            "unmapped_units": _compress([u for u, a in ann.items()
                                         if a.get("requirement", "").strip().lower() in NO_REQUIREMENT]),
            "requirement_gaps": _requirement_coverage(s)["gaps"]}


@server.tool()
async def set_plan(
    slices: Annotated[list[SliceSpec], Field(description="Slices in stack order (1 = reviewed and merged first). "
                                                         "Every unit must be in exactly one slice.")],
    materialize: Annotated[bool, Field(description="Build branches and worktrees right away")] = True,
    repo_path: RepoPath = None,
) -> dict[str, Any]:
    """Validate and store the slice plan, then (by default) build one commit, branch and worktree per slice."""
    root = _root(repo_path)
    s = _load(root)
    normalized, errors, extra = _validate_plan(s, [_as_dict(x) for x in slices])
    if errors:
        return {"ok": False, "errors": errors, **extra}
    version = (s["plan"] or {}).get("version", 0) + 1
    if s["plan"]:
        s["plan_history"] = (s["plan_history"] + [s["plan"]])[-20:]
    s["plan"] = {"version": version, "slices": normalized}
    _event(s, "plan", version=version, slices=len(normalized), via="set_plan")
    result: dict[str, Any] = {"ok": True, "plan_version": version,
                              "slices": [{"slice": k, **_slice_stats(s, sl)} for k, sl in enumerate(normalized, 1)],
                              "warnings": _plan_warnings(s)}
    if materialize:
        result["materialized"] = await _materialize(root, s)
    _save(root, s)
    return result


async def _replan(root: Path, s: dict, new_slices: list[dict], materialize: bool, via: str, **info: Any) -> dict:
    normalized, errors, extra = _validate_plan(s, new_slices)
    if errors:
        return {"ok": False, "errors": errors, **extra}
    version = s["plan"]["version"] + 1
    s["plan_history"] = (s["plan_history"] + [s["plan"]])[-20:]
    s["plan"] = {"version": version, "slices": normalized}
    _event(s, "plan", version=version, slices=len(normalized), via=via, **info)
    result: dict[str, Any] = {"ok": True, "plan_version": version,
                              "slices": [{"slice": k, **_slice_stats(s, sl)} for k, sl in enumerate(normalized, 1)],
                              "warnings": _plan_warnings(s)}
    if materialize:
        result["materialized"] = await _materialize(root, s)
    _save(root, s)
    return result


@server.tool()
async def move_units(
    units: Annotated[list[str], Field(description="Unit selectors to move")],
    to_slice: Annotated[int, Field(description="Destination slice number (1-based, current numbering)")],
    materialize: bool = True,
    repo_path: RepoPath = None,
) -> dict[str, Any]:
    """Repair move: put units into another slice (typically an earlier one that needs a definition).
    Slices left empty are dropped and later slices renumbered."""
    root = _root(repo_path)
    s = _load(root)
    if not s["plan"]:
        raise DiffractError("No plan yet: call set_plan first.")
    ids, errors = _resolve(s, units)
    if errors or not ids:
        raise DiffractError("; ".join(errors) or "no units selected")
    K = len(s["plan"]["slices"])
    if not 1 <= to_slice <= K:
        raise DiffractError(f"to_slice must be between 1 and {K}")
    moving = set(ids)
    new = []
    for k, sl in enumerate(s["plan"]["slices"], 1):
        kept = [u for u in sl["units"] if u not in moving]
        if k == to_slice:
            kept = _ordered(s, set(kept) | moving)
        new.append({**sl, "units": kept})
    renumber = {}
    compact = []
    for k, sl in enumerate(new, 1):
        if sl["units"]:
            compact.append(sl)
            renumber[k] = len(compact)
    result = await _replan(root, s, compact, materialize, "move_units", moved=_compress(_ordered(s, moving)), to=to_slice)
    if len(compact) != K:
        result["renumbered"] = renumber
    return result


@server.tool()
async def merge_slices(
    slices: Annotated[list[int], Field(description="Slice numbers to merge (merged slice takes the lowest position)")],
    title: Annotated[str, Field(description="Title for the merged slice; empty = join titles")] = "",
    materialize: bool = True,
    repo_path: RepoPath = None,
) -> dict[str, Any]:
    """Fallback repair: merge slices that cannot be made green separately. Merging everything
    reproduces the original PR, so repair always terminates."""
    root = _root(repo_path)
    s = _load(root)
    if not s["plan"]:
        raise DiffractError("No plan yet: call set_plan first.")
    K = len(s["plan"]["slices"])
    ks = sorted(set(slices))
    if len(ks) < 2 or any(not 1 <= k <= K for k in ks):
        raise DiffractError(f"Pass at least two slice numbers between 1 and {K}.")
    olds = [s["plan"]["slices"][k - 1] for k in ks]
    merged = {
        "title": title.strip() or " + ".join(o["title"] for o in olds),
        "rationale": " ".join(o["rationale"] for o in olds if o["rationale"]),
        "requirement": ", ".join(dict.fromkeys(r.strip() for o in olds for r in o["requirement"].split(",") if r.strip())),
        "units": _ordered(s, {u for o in olds for u in o["units"]}),
    }
    new = []
    for k, sl in enumerate(s["plan"]["slices"], 1):
        if k == ks[0]:
            new.append(merged)
        elif k not in ks:
            new.append(sl)
    return await _replan(root, s, new, materialize, "merge_slices", merged=ks)


@server.tool()
async def verify(
    slices: Annotated[list[int] | None, Field(description="Slice numbers to verify; empty = all. "
                                                           "0 = the stack root (checks the base itself is green)")] = None,
    command: Annotated[str, Field(description="Override the session verify command for this run")] = "",
    force: Annotated[bool, Field(description="Re-run even if this exact tree already passed/failed")] = False,
    max_parallel: Annotated[int, Field(description="Max concurrent runs; 0 = number of CPUs")] = 0,
    retries: Annotated[int, Field(description="Re-run a failing slice up to this many times. A pass on retry is "
                                              "reported as green but flagged 'flaky' (parallel runs expose flaky tests)")] = 1,
    repo_path: RepoPath = None,
) -> dict[str, Any]:
    """Run the verify command in every slice worktree IN PARALLEL. Results are cached by git tree,
    so after a repair only slices whose content changed are re-run. Each slice is cumulative
    (slice k contains slices 1..k), so fix the LOWEST failing slice first."""
    root = _root(repo_path)
    s = _load(root)
    if not s["plan"]:
        raise DiffractError("No plan yet: call set_plan first.")
    if not s["materialized"] or s["materialized"]["plan_version"] != s["plan"]["version"]:
        await _materialize(root, s)
        _save(root, s)
    cmd = command.strip() or s["config"]["verify_command"]
    if not cmd:
        raise DiffractError("No verify command: pass command=... or set verify_command in start_session.")
    stack = s["materialized"]["slices"]
    K = len(stack)
    targets = sorted(set(slices)) if slices else list(range(1, K + 1))
    bad = [k for k in targets if not 0 <= k <= K]
    if bad:
        raise DiffractError(f"Unknown slice numbers {bad}; valid: 0..{K}")
    wroot = _wt_root(root)
    jobs: list[tuple[int, str, str, Path]] = []
    for k in targets:
        if k == 0:
            wt = wroot / "s00"
            if _checkout_worktree(root, wt, s["root"], _registered_worktrees(root)):
                _link_shared(root, wt, s["config"]["shared_paths"])
                await _setup_all(s, [wt])
            _link_shared(root, wt, s["config"]["shared_paths"])
            jobs.append((0, "stack root (base)", _gtext(root, "rev-parse", f"{s['root']}^{{tree}}"), wt))
        else:
            o = stack[k - 1]
            jobs.append((k, o["title"], o["tree"], Path(o["worktree"])))
    cache = s.get("verify_cache", {})
    results: dict[int, dict] = {}
    to_run = []
    for k, title, tree, wt in jobs:
        hit = cache.get(f"{tree}|{cmd}")
        if hit and not force:
            results[k] = {"slice": k, "title": title, **hit, "cached": True}
        else:
            to_run.append((k, title, tree, wt))
    sem = asyncio.Semaphore(max(1, max_parallel or (os.cpu_count() or 4)))

    async def one(k: int, title: str, tree: str, wt: Path) -> None:
        log_path = _sdir(root) / "logs" / f"slice-{k:02d}.log"
        async with sem:
            outcome = await _run(cmd, wt, s["config"]["timeout_seconds"], log_path)
            attempt = 1
            while outcome["status"] == "fail" and attempt <= max(0, retries):
                failed_log = log_path.with_name(f"slice-{k:02d}.attempt-{attempt}.log")
                shutil.copyfile(log_path, failed_log)
                attempt += 1
                retry = await _run(cmd, wt, s["config"]["timeout_seconds"], log_path)
                if retry["status"] == "pass":
                    outcome = {**retry, "flaky": True, "failed_attempt_log": str(failed_log)}
                    break
                outcome = retry
        results[k] = {"slice": k, "title": title, **outcome, "attempts": attempt, "cached": False}

    t0 = time.monotonic()
    await asyncio.gather(*(one(*j) for j in to_run))
    wall = round(time.monotonic() - t0, 1)

    fresh = _load(root)
    for k, title, tree, wt in to_run:
        r = {key: results[k][key] for key in ("status", "exit_code", "seconds", "log", "tail", "flaky", "failed_attempt_log")
             if key in results[k]}
        fresh.setdefault("verify_cache", {})[f"{tree}|{cmd}"] = r
    ran = [k for k, *_ in to_run]
    failed = sorted(k for k, r in results.items() if r["status"] != "pass")
    _event(fresh, "verify", ran=ran, cached=[k for k in results if k not in ran], failed=failed, wall_seconds=wall,
           serial_seconds=round(sum(results[k]["seconds"] for k in ran), 1),
           parallel=min(len(to_run), max(1, max_parallel or (os.cpu_count() or 4))))
    _save(root, fresh)

    all_status = {o["slice"]: _current_status(fresh, o["tree"]) if cmd == fresh["config"]["verify_command"]
                  else results.get(o["slice"], {}).get("status", "unverified") for o in stack}
    rows = []
    for k in sorted(results):
        r = results[k]
        row = {"slice": k, "title": r["title"], "status": r["status"], "seconds": r["seconds"], "cached": r["cached"]}
        if r.get("flaky"):
            row.update({"flaky": True, "failed_attempt_log": r.get("failed_attempt_log")})
        if r["status"] != "pass":
            row.update({"exit_code": r["exit_code"], "log": r["log"], "tail": r["tail"],
                        "worktree": str(wroot / "s00") if k == 0 else stack[k - 1]["worktree"]})
            if k > 0:
                row.update(_suggest_repair(root, fresh, k, r["log"]))
            if k and stack[k - 1]["worktree"].split(os.sep)[-1] in fresh.get("setup_failed", {}):
                row["setup_failed"] = True
        rows.append(row)
    lowest = min((k for k in failed if k > 0), default=None)
    hint = ("all green" if not failed else
            "the stack root itself fails: the base is red, so narrow the verify command" if 0 in failed else
            f"start with slice {lowest}: later failures may be the same problem inherited cumulatively"
            + (" — the LAST slice equals the PR head, so the PR itself is red; ask the user before continuing"
               if lowest == K else ""))
    return {"command": cmd, "wall_seconds": wall, "results": rows,
            "summary": {"all_green": all(v == "pass" for v in all_status.values()), "status_by_slice": all_status,
                        "failed": failed, "lowest_failing": lowest,
                        "flaky": sorted(k for k, r in results.items() if r.get("flaky"))}, "hint": hint}


@server.tool()
def locate(
    symbols: Annotated[list[str], Field(description="Names to find: functions, classes, constants or tests. "
                                                    "Pytest ids like 'tests/test_x.py::test_name' restrict the search to that file")],
    repo_path: RepoPath = None,
) -> dict[str, Any]:
    """Find which units change the definition of a symbol, in the base or the head version of the
    PR's changed files, and which slice each unit is in. Use it to diagnose red slices: the fix
    is usually to move the units that define a missing symbol, or that change a failing test,
    into the failing slice."""
    root = _root(repo_path)
    s = _load(root)
    slice_of = {unit: number for number, sl in enumerate((s["plan"] or {}).get("slices", []), 1) for unit in sl["units"]}
    cache: dict = {}
    results = []
    for raw in symbols:
        path, _, name = raw.rpartition("::") if "::" in raw else ("", "", raw)
        path = path.split("::")[0] if path else None
        name = re.sub(r"\[.*\]$", "", name.strip())
        matches = _units_touching_symbol(root, s, name, path, cache=cache)
        for match in matches:
            match["slices"] = sorted({slice_of[u] for u in match["units"] if u in slice_of})
            match["units"] = _compress(_ordered(s, match["units"]))
        results.append({"symbol": raw, "definitions": matches or "not defined in any file this PR changes"})
    return {"results": results}


@server.tool()
async def probe(
    failing_slice: Annotated[int, Field(description="Number of the red slice to fix")],
    max_parallel: Annotated[int, Field(description="Max concurrent runs; 0 = number of CPUs")] = 0,
    repo_path: RepoPath = None,
) -> dict[str, Any]:
    """Use when a red slice has no suggested_repair. For every later slice j, builds 'slices 1..k plus
    slice j' and runs the verify command on all candidates IN PARALLEL. Reports which later slices make
    slice k green and the exact merge_slices call to apply. Results are cached by tree, so the
    recommended merge verifies instantly."""
    root = _root(repo_path)
    s = _load(root)
    if not s["plan"]:
        raise DiffractError("No plan yet: call set_plan first.")
    slices = s["plan"]["slices"]
    total = len(slices)
    if not 1 <= failing_slice < total:
        raise DiffractError(f"failing_slice must be between 1 and {total - 1} (the last slice has nothing after it).")
    command = s["config"]["verify_command"]
    if not command:
        raise DiffractError("No verify command set in start_session.")
    earlier_units = {u for sl in slices[:failing_slice] for u in sl["units"]}
    writer = _TreeWriter(root, s)
    candidates = []
    try:
        for later in range(failing_slice + 1, total + 1):
            tree = writer.tree(earlier_units | set(slices[later - 1]["units"]), f"probe {failing_slice}+{later}")
            commit = writer.commit(tree, s["root"], f"diffract probe: slice {failing_slice} + slice {later}\n")
            candidates.append((later, tree, commit))
    finally:
        writer.close()

    cache = s.get("verify_cache", {})
    outcomes: dict[int, dict] = {}
    pending = []
    for later, tree, commit in candidates:
        hit = cache.get(f"{tree}|{command}")
        if hit:
            outcomes[later] = {**hit, "cached": True}
        else:
            pending.append((later, tree, commit))
    worktree_root = _wt_root(root)
    worktree_root.mkdir(parents=True, exist_ok=True)
    registered = _registered_worktrees(root)
    created = []
    for later, _tree, commit in pending:
        worktree = worktree_root / f"p{later:02d}"
        if _checkout_worktree(root, worktree, commit, registered):
            created.append(worktree)
        _link_shared(root, worktree, s["config"]["shared_paths"])
    await _setup_all(s, created)
    limit = asyncio.Semaphore(max(1, max_parallel or (os.cpu_count() or 4)))

    async def run_candidate(later: int) -> None:
        async with limit:
            log_path = _sdir(root) / "logs" / f"probe-{failing_slice:02d}-plus-{later:02d}.log"
            outcomes[later] = {**await _run(command, worktree_root / f"p{later:02d}",
                                            s["config"]["timeout_seconds"], log_path), "cached": False}

    started = time.monotonic()
    await asyncio.gather(*(run_candidate(later) for later, _tree, _commit in pending))
    wall = round(time.monotonic() - started, 1)

    fresh = _load(root)
    for later, tree, _commit in pending:
        fresh.setdefault("verify_cache", {})[f"{tree}|{command}"] = {
            key: outcomes[later][key] for key in ("status", "exit_code", "seconds", "log", "tail")}
    green = [later for later in sorted(outcomes) if outcomes[later]["status"] == "pass"]
    _event(fresh, "probe", slice=failing_slice, candidates=len(candidates), green=green, wall_seconds=wall,
           parallel=min(len(pending), max(1, max_parallel or (os.cpu_count() or 4))))
    _save(root, fresh)

    rows = [{"add_slice": later, "title": slices[later - 1]["title"],
             "lines": _slice_stats(s, slices[later - 1])["lines"], "status": outcomes[later]["status"],
             "cached": outcomes[later]["cached"]} for later in sorted(outcomes)]
    if green:
        best = min(green, key=lambda later: _slice_stats(s, slices[later - 1])["lines"])
        recommendation = (f"merge_slices(slices=[{failing_slice}, {best}]): slice {best} "
                          f"'{slices[best - 1]['title']}' supplies what slice {failing_slice} needs")
    else:
        recommendation = (f"No single later slice fixes slice {failing_slice}. Diagnose with a general subagent "
                          f"(see references/subagent-prompts.md), or merge_slices(slices=[{failing_slice}, {failing_slice + 1}]).")
    return {"failing_slice": failing_slice, "wall_seconds": wall, "probes": rows, "green_with": green,
            "recommendation": recommendation}


@server.tool()
def drift_check(repo_path: RepoPath = None) -> dict[str, Any]:
    """Prove the stack is complete: the tree of the last slice must equal the tree of the PR head,
    the commits must form one linear chain from the stack root, and every unit must sit in exactly
    one slice. Writes .diffract/certificate.json."""
    root = _root(repo_path)
    s = _load(root)
    if not s["materialized"]:
        raise DiffractError("Nothing materialized yet: call set_plan first.")
    stack = s["materialized"]["slices"]
    facts = _drift_facts(root, s)
    stack_tree, chain_ok, once, identical = (facts["stack_tree"], facts["linear_chain"], facts["units_once"],
                                             facts["trees_identical"])
    per_slice = [_slice_stats(s, sl)["lines"] for sl in s["plan"]["slices"]]
    total = sum(u["lines"] for u in s["units"].values())
    cert = {
        "zero_drift": identical and chain_ok and once,
        "statement": (f"ZERO DRIFT: stack tip tree {stack_tree[:12]} == PR head tree {s['head_tree'][:12]}; "
                      f"{len(s['units'])} units, each in exactly one of {len(stack)} slices; linear chain from "
                      f"{s['root'][:12]}." if identical and chain_ok and once else "DRIFT DETECTED — do not publish."),
        "stack_tip_tree": stack_tree, "pr_head_tree": s["head_tree"], "trees_identical": identical,
        "linear_chain": chain_ok, "units_assigned_exactly_once": once,
        "original_budgeted_lines": total, "largest_slice_lines": max(per_slice), "slice_lines": per_slice,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if not identical:
        cert["diffstat"] = _gtext(root, "diff", "--stat", stack[-1]["commit"], s["head"])
    else:
        cert["slice_files"] = _write_slice_files(root, s)
    (_sdir(root) / "certificate.json").write_text(json.dumps(cert, indent=2), encoding="utf-8")
    _event(s, "drift_check", zero_drift=cert["zero_drift"])
    _save(root, s)
    return cert


@server.tool()
def status(repo_path: RepoPath = None) -> dict[str, Any]:
    """Everything needed for the PR descriptions and the final report: slices with sizes, branches,
    verify status and attention map; drift; timings; repair rounds; unmapped units."""
    root = _root(repo_path)
    s = _load(root)
    ann = s.get("annotations", {})
    total = sum(u["lines"] for u in s["units"].values())
    mech_total = sum(s["units"][u]["lines"] for u, a in ann.items() if a["kind"] == "mechanical")
    events = s.get("events", [])
    verifies = [e for e in events if e["event"] == "verify"]
    out: dict[str, Any] = {
        "repo": s["repo"], "base": s["base_ref"] or s["base"][:12], "head": s["head_ref"] or s["head"][:12],
        "stack_root": s["root"][:12], "verify_command": s["config"]["verify_command"],
        "original": {"files": len(s["files"]), "units": len(s["units"]), "budgeted_lines": total,
                     "added": sum(f["added"] for f in s["files"].values()),
                     "removed": sum(f["removed"] for f in s["files"].values())},
        "attention_map": {"annotated_units": len(ann), "mechanical_lines": mech_total,
                          "skimmable_pct": round(100 * mech_total / total, 1) if total else 0.0},
        "unmapped_units": _compress([u for u, a in ann.items()
                                     if a.get("requirement", "").strip().lower() in {"none", "unrelated", "-"}]),
        "timing": {"minutes_since_start": round((time.time() - s["created_at"]) / 60, 1),
                   "verify_runs": len(verifies),
                   "parallel_wall_seconds": round(sum(e.get("wall_seconds", 0) for e in verifies), 1),
                   "serial_equivalent_seconds": round(sum(e.get("serial_seconds", 0) for e in verifies), 1)},
        "plan_versions": (s["plan"] or {}).get("version", 0),
        "repairs": [e for e in events if e["event"] == "plan" and e.get("via") in ("move_units", "merge_slices")],
    }
    if s["plan"]:
        mat = {o["slice"]: o for o in (s["materialized"] or {}).get("slices", [])}
        rows = []
        for k, sl in enumerate(s["plan"]["slices"], 1):
            o = mat.get(k)
            behavioral = [u for u in sl["units"] if ann.get(u, {}).get("kind") == "behavioral"]
            rows.append({"slice": k, **_slice_stats(s, sl), "rationale": sl["rationale"], "requirement": sl["requirement"],
                         "review_closely": _compress(behavioral),
                         "branch": o["branch"] if o else None,
                         "worktree": o.get("worktree") if o else None,
                         "verify": _current_status(s, o["tree"]) if o else "not built"})
        out["slices"] = rows
        out["largest_slice_lines"] = max(r["lines"] for r in rows)
        if s["materialized"]:
            out["drift_ok"] = s["materialized"]["slices"][-1]["tree"] == s["head_tree"]
    if s.get("requirements") or s.get("annotations"):
        out["requirements"] = _requirement_coverage(s)
    cert = _sdir(root) / "certificate.json"
    if cert.exists():
        out["certificate"] = json.loads(cert.read_text(encoding="utf-8"))["statement"]
    return out


@server.tool()
def report(
    summary: Annotated[str, Field(description="2-3 sentences: what the PR does and how the stack is organised")] = "",
    slice_notes: Annotated[dict[str, str] | None, Field(description="One line per slice number on what reviewers "
                                                                    "should focus on, e.g. {'3': 'Check how suggestions are ranked'}")] = None,
    title: Annotated[str, Field(description="Page title; empty = 'Diffract stack map: <head> into <base>'")] = "",
    repo_path: RepoPath = None,
) -> dict[str, Any]:
    """Write the stack map, .diffract/report.html: one self-contained page with the prism view of the split,
    requirement coverage and gaps, slice sizes (behavioural vs mechanical), test status, the repair timeline
    and the zero-drift proof. Every number comes from git; only summary and slice_notes are free text.
    Also writes .diffract/stack.json, the same run as data, which the Diffract website can open.
    Call after drift_check."""
    root = _root(repo_path)
    s = _load(root)
    view = _report_view(root, s, title, summary, {str(k): v for k, v in (slice_notes or {}).items()})
    path = _sdir(root) / "report.html"
    path.write_text(render_report(view), encoding="utf-8")
    run_file = _sdir(root) / "stack.json"
    run_file.write_text(json.dumps(_stack_document(view), indent=1), encoding="utf-8")
    largest = max(item["lines"] for item in view["slices"])
    return {"path": str(path), "url": path.as_uri(),
            "headline": f"{view['original']['lines']:,} lines -> {len(view['slices'])} slices, largest {largest:,}; "
                        f"zero drift: {view['proof']['zero_drift']}",
            "stack_json": str(run_file),
            "worth_a_look": view["gaps"] + [f"slice {i['number']} flaky" for i in view["slices"] if i["flaky"]]}


ASSERTION_RE = re.compile(
    r"^[+-][ \t]*(?:assert\b|self\.assert\w|pytest\.raises\b|expect\(|assertEquals\b|require\.\w|t\.Error\b|t\.Fatal\b)"
)
DEF_LINE_RE = re.compile(r"^[+-][ \t]*(?:def |class |export |function |public )")


@server.tool()
def facts(repo_path: RepoPath = None) -> dict[str, Any]:
    """Return verifiable facts about the current session for claim-checking.

    Use this to test whether an agent's or author's claims about a pull request
    are supported by evidence before accepting them.  No model calls, no network;
    every number comes from git and the session.

    Returns:
      areas          - for each top-level folder (or "." for root files): the number
                       of changed files and total budgeted lines.
      tests          - each changed test file with assertion lines added/removed
                       (lines starting with assert, self.assert*, pytest.raises,
                       expect(, assertEquals, require.*, t.Error, t.Fatal).
                       weakened_units: hunk unit ids where removed assertions > added.
      dependencies   - changed lockfiles (role == lockfile) and manifests (name in
                       MANIFESTS or matches a budget-exclude glob).
      api_surface    - for each non-test source file: added and removed top-level
                       definition lines (def , class , export , function , public ).
      totals         - files, units, budgeted lines; behavioural/mechanical lines
                       when annotate_units has been called.
    """
    root = _root(repo_path)
    s = _load(root)
    units = s["units"]
    files = s["files"]
    ann = s.get("annotations", {})

    # --- areas ---------------------------------------------------------------
    area_files: dict[str, int] = {}
    area_lines: dict[str, int] = {}
    for path, finfo in files.items():
        top = path.split("/")[0] if "/" in path else "."
        area_files[top] = area_files.get(top, 0) + 1
        area_lines[top] = area_lines.get(top, 0) + sum(units[u]["lines"] for u in finfo["units"])
    areas = [{"folder": k, "files": area_files[k], "lines": area_lines[k]}
             for k in sorted(area_files)]

    # --- tests ---------------------------------------------------------------
    test_entries = []
    weakened_units: list[str] = []
    for uid in s["unit_order"]:
        u = units[uid]
        if u["kind"] != "hunk":
            continue
        if not (TEST_PATH_RE.search(u["path"].lower()) or JVM_TEST_RE.search(u["path"])):
            continue
        added_a = sum(1 for line in u["diff"].splitlines() if line.startswith("+") and ASSERTION_RE.match(line))
        removed_a = sum(1 for line in u["diff"].splitlines() if line.startswith("-") and ASSERTION_RE.match(line))
        if added_a or removed_a:
            test_entries.append({"unit": uid, "path": u["path"], "assertions_added": added_a, "assertions_removed": removed_a})
        if removed_a > added_a:
            weakened_units.append(uid)

    # aggregate per file for the summary list
    file_assertions: dict[str, dict[str, int]] = {}
    for entry in test_entries:
        row = file_assertions.setdefault(entry["path"], {"assertions_added": 0, "assertions_removed": 0})
        row["assertions_added"] += entry["assertions_added"]
        row["assertions_removed"] += entry["assertions_removed"]
    tests_out = [{"path": p, **v} for p, v in sorted(file_assertions.items())]

    # --- dependencies --------------------------------------------------------
    dep_list = []
    budget_exclude = s["config"]["budget_exclude"]
    for path, finfo in sorted(files.items()):
        name = path.rsplit("/", 1)[-1].lower()
        is_lockfile = name in LOCKFILES
        is_manifest = name in {m.lower() for m in MANIFESTS}
        is_excluded = any(fnmatch.fnmatchcase(path, pat) for pat in budget_exclude)
        if is_lockfile or is_manifest or is_excluded:
            role = "lockfile" if is_lockfile else "manifest"
            dep_list.append({"path": path, "role": role,
                             "added": finfo["added"], "removed": finfo["removed"]})

    # --- api_surface ---------------------------------------------------------
    api_out = []
    for path, finfo in sorted(files.items()):
        if finfo["binary"]:
            continue
        if TEST_PATH_RE.search(path.lower()) or JVM_TEST_RE.search(path):
            continue
        if path.lower().endswith((".md", ".rst", ".txt", ".adoc")):
            continue
        defs_added = defs_removed = 0
        for uid in finfo["units"]:
            u = units[uid]
            if u["kind"] != "hunk":
                continue
            for line in u["diff"].splitlines():
                if DEF_LINE_RE.match(line):
                    if line.startswith("+"):
                        defs_added += 1
                    elif line.startswith("-"):
                        defs_removed += 1
        if defs_added or defs_removed:
            api_out.append({"path": path, "definitions_added": defs_added, "definitions_removed": defs_removed})

    # --- totals --------------------------------------------------------------
    total_lines = sum(u["lines"] for u in units.values())
    totals: dict[str, Any] = {"files": len(files), "units": len(units), "budgeted_lines": total_lines}
    if ann:
        totals["behavioural_lines"] = sum(units[u]["lines"] for u, a in ann.items() if a["kind"] == "behavioral")
        totals["mechanical_lines"] = sum(units[u]["lines"] for u, a in ann.items() if a["kind"] == "mechanical")

    return {
        "areas": areas,
        "tests": {"files": tests_out, "weakened_units": weakened_units},
        "dependencies": dep_list,
        "api_surface": api_out,
        "totals": totals,
    }


@server.tool()
def publish(
    dry_run: Annotated[bool, Field(description="True = only return the commands. Set False only after the user approves")] = True,
    bodies: Annotated[dict[str, str] | None, Field(description="PR description per slice number, e.g. {'1': '...'}")] = None,
    remote: str = "origin",
    base_branch: Annotated[str, Field(description="Remote branch the first PR targets; empty = session base")] = "",
    draft: bool = True,
    title_prefix: str = "",
    status_checks: Annotated[bool, Field(description="Post 'diffract/zero-drift' and 'diffract/tests' commit statuses "
                                                     "on every PR, so the proof shows in the forge's checks list")] = True,
    forge: Annotated[Literal["auto", "github", "gitlab"], Field(description="Where to open the stack. 'auto' reads "
                                                                            "the remote URL: GitLab if it mentions gitlab, else GitHub")] = "auto",
    repo_path: RepoPath = None,
) -> dict[str, Any]:
    """Push the slice branches and open stacked PRs (GitHub, with `gh`) or merge requests (GitLab, with
    `glab`): request k targets branch k-1, request 1 targets the base branch. Each description gets a stack
    table appended, and each head commit gets two statuses: diffract/zero-drift and diffract/tests.
    Refuses if drift is detected."""
    root = _root(repo_path)
    s = _load(root)
    if not s["materialized"] or s["materialized"]["plan_version"] != s["plan"]["version"]:
        raise DiffractError("Plan changed since the last build: run verify (or set_plan) first.")
    stack = s["materialized"]["slices"]
    if stack[-1]["tree"] != s["head_tree"]:
        raise DiffractError("Drift detected: the stack tip does not match the PR head. Not publishing.")
    base = base_branch or s["base_ref"]
    if not base:
        raise DiffractError("Unknown base branch: pass base_branch (e.g. 'main').")
    K = len(stack)
    bodies = {str(k): v for k, v in (bodies or {}).items()}
    push_cmd = ["git", "push", "--force", remote] + [f"{o['branch']}:{o['branch']}" for o in stack]
    if (forge if forge != "auto" else _detect_forge(root, remote)) == "gitlab":
        return _publish_gitlab(root, s, stack, base, bodies, push_cmd, dry_run, draft, title_prefix, status_checks)
    plan_cmds = [" ".join(push_cmd)]
    for o in stack:
        prev = base if o["slice"] == 1 else stack[o["slice"] - 2]["branch"]
        plan_cmds.append(f"gh pr create --base {prev} --head {o['branch']} "
                         f"--title \"{title_prefix}[{o['slice']}/{K}] {o['title']}\"{' --draft' if draft else ''}")
    statuses = _planned_statuses(s, stack)
    if status_checks:
        plan_cmds.append(f"gh api repos/<owner>/<repo>/statuses/<sha> for each PR: diffract/zero-drift and "
                         f"diffract/tests ({len(statuses) * 2} statuses)")
    not_green = [item["slice"] for item in statuses if item["tests"][0] != "success"]
    if dry_run:
        return {"dry_run": True, "commands": plan_cmds,
                **({"warning": f"slices {not_green} are not green; their diffract/tests status will say so"}
                   if not_green else {}),
                "note": "Show these to the user; call publish(dry_run=False, bodies=...) only after approval."}
    if not shutil.which("gh"):
        raise DiffractError("GitHub CLI `gh` not found on PATH (install it and run `gh auth login`).")
    proof = drift_check(str(root))
    if not proof.get("zero_drift", proof["statement"].startswith("ZERO DRIFT")):
        raise DiffractError("Drift detected: the stack tip does not match the PR head. Not publishing.")
    push = subprocess.run(push_cmd, cwd=root, capture_output=True, text=True)
    if push.returncode != 0:
        raise DiffractError(f"git push failed: {push.stderr.strip()}")
    urls: dict[int, str] = {}
    for o in stack:
        prev = base if o["slice"] == 1 else stack[o["slice"] - 2]["branch"]
        title = f"{title_prefix}[{o['slice']}/{K}] {o['title']}"
        body = bodies.get(str(o["slice"]), o["title"])
        view = subprocess.run(["gh", "pr", "view", o["branch"], "--json", "url", "-q", ".url"],
                              cwd=root, capture_output=True, text=True)
        if view.returncode == 0 and view.stdout.strip():
            urls[o["slice"]] = view.stdout.strip()
            retarget = subprocess.run(["gh", "pr", "edit", urls[o["slice"]], "--base", prev, "--title", title],
                                      cwd=root, capture_output=True, text=True)
            if retarget.returncode != 0:
                raise DiffractError(f"gh pr edit failed for slice {o['slice']}: could not point the existing PR "
                                    f"at {prev}: {(retarget.stderr or retarget.stdout).strip()}")
            continue
        args = ["gh", "pr", "create", "--base", prev, "--head", o["branch"], "--title", title, "--body", body]
        if draft:
            args.append("--draft")
        r = subprocess.run(args, cwd=root, capture_output=True, text=True)
        if r.returncode != 0:
            raise DiffractError(f"gh pr create failed for slice {o['slice']}: {r.stderr.strip()}")
        urls[o["slice"]] = r.stdout.strip().splitlines()[-1]
    cert = proof["statement"]
    description_problems: list[str] = []
    for o in stack:
        k = o["slice"]
        table = "\n".join(f"{j}. {'**' if j == k else ''}[{stack[j - 1]['title']}]({urls[j]}){'** ← this PR' if j == k else ''}"
                          for j in range(1, K + 1))
        full = (f"{bodies.get(str(k), o['title'])}\n\n---\n**Stack: review and merge in order.**\n{table}\n\n"
                f"_Split with Diffract. {cert}_")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as tf:
            tf.write(full)
        try:
            edit = subprocess.run(["gh", "pr", "edit", urls[k], "--body-file", tf.name], cwd=root, capture_output=True, text=True)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tf.name)
        if edit.returncode != 0:
            description_problems.append(f"slice {k}: description not updated: {(edit.stderr or edit.stdout).strip()[:200]}")
    posted, problems = [], []
    if status_checks:
        posted, problems = _post_statuses(root, statuses)
    s = _load(root)
    _event(s, "publish", urls=urls)
    _save(root, s)
    result: dict[str, Any] = {"dry_run": False, "prs": [{"slice": k, "url": u} for k, u in sorted(urls.items())]}
    if description_problems:
        result["description_problems"] = description_problems
    if status_checks:
        result["status_checks"] = posted
        if problems:
            result["status_check_problems"] = problems
    return result


@server.tool()
def cleanup(
    delete_branches: Annotated[bool, Field(description="Also delete local diffract/* branches for this stack")] = False,
    delete_session: Annotated[bool, Field(description="Also delete .diffract/ (logs, batches, certificate)")] = False,
    repo_path: RepoPath = None,
) -> dict[str, Any]:
    """Remove slice worktrees (and optionally branches and session files). Safe to run any time."""
    root = _root(repo_path)
    wroot = _wt_root(root)
    removed = []
    if wroot.exists():
        for child in sorted(wroot.iterdir()):
            if re.fullmatch(r"[sp]\d{2}", child.name):
                _remove_worktree(root, child)
                removed.append(child.name)
        shutil.rmtree(wroot, ignore_errors=True)
    _git(root, "worktree", "prune", check=False)
    deleted = []
    sfile = _sdir(root) / "session.json"
    if delete_branches and sfile.exists():
        s = json.loads(sfile.read_text(encoding="utf-8"))
        for ref in _gtext(root, "for-each-ref", "--format=%(refname)", f"refs/heads/diffract/{s['stack_name']}/").splitlines():
            _git(root, "update-ref", "-d", ref)
            deleted.append(ref.replace("refs/heads/", ""))
    if delete_session and _sdir(root).exists():
        shutil.rmtree(_sdir(root))
    return {"worktrees_removed": removed, "branches_deleted": deleted, "session_deleted": delete_session}


if __name__ == "__main__":
    server.run()
