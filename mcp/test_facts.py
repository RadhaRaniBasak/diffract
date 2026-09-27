"""
Pytest tests for the `facts` MCP tool in diffract_mcp.py.

Sets DIFFRACT_HOME and DIFFRACT_WORKTREE_ROOT *before* importing diffract_mcp
so the module uses tmp_path-local state and does not touch the real cache.
"""

import subprocess
import sys
from pathlib import Path
import os


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _build_repo(tmp: Path) -> Path:
    """
    Creates a minimal git repo with:
      src/math.py        — source file (adds a `def add` function)
      tests/test_math.py — test file whose HEAD version weakens tests:
                           removes 2 assert lines, adds 1.

    base commit  ← main
    feature commit ← feature/weakened-tests
    """
    repo = tmp / "repo"
    repo.mkdir()

    # git setup
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "core.autocrlf", "false")

    # Padding between the two test functions so git produces separate hunks.
    PAD = "".join(f"    # pad {i}\n" for i in range(8))

    # base: a simple add function and a thorough test, with padding so the
    # hunk touching test_add is separate from any hunk adding test_mul.
    _write(repo, "src/math.py", "def add(a, b):\n    return a + b\n")
    _write(repo, "tests/test_math.py",
           "from src.math import add\n\n\n"
           "def test_add():\n"
           "    assert add(1, 2) == 3\n"
           "    assert add(0, 0) == 0\n"
           "    assert add(-1, 1) == 0\n"
           + PAD
           )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")

    # feature branch:
    #   - test_add loses 2 assert lines and gains 1  → weakened (removed > added)
    #   - test_mul is a brand-new function far enough away to be a separate hunk
    _git(repo, "checkout", "-b", "feature/weakened-tests")
    _write(repo, "src/math.py",
           "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    _write(repo, "tests/test_math.py",
           "from src.math import add, mul\n\n\n"
           "def test_add():\n"
           "    assert add(1, 2) == 3\n"     # kept
           # removed: assert add(0, 0) == 0
           # removed: assert add(-1, 1) == 0
           "    assert add(99, 1) == 100\n"  # 1 added — net: removed(2) > added(1)
           + PAD
           + "\n\ndef test_mul():\n"
           "    assert mul(3, 4) == 12\n"
           )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feature: add mul, weaken test_add assertions")

    return repo


# ---------------------------------------------------------------------------
# Fixture: isolated diffract_mcp module loaded once per test session
# ---------------------------------------------------------------------------



def _load_module(tmp: Path):
    """
    Import diffract_mcp with DIFFRACT_HOME and DIFFRACT_WORKTREE_ROOT pointing
    inside tmp so the module never touches ~/.cache/diffract.
    """
    os.environ["DIFFRACT_HOME"] = str(tmp / "diffract-home")
    os.environ["DIFFRACT_WORKTREE_ROOT"] = str(tmp / "diffract-wt")

    # Force a fresh import so the module reads the env vars we just set.
    if "diffract_mcp" in sys.modules:
        del sys.modules["diffract_mcp"]

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import diffract_mcp as d
    return d


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFacts:

    def test_assertion_counts_and_weakened_unit(self, tmp_path):
        """The hunk that removes 2 asserts and adds 1 must appear in weakened_units."""
        d = _load_module(tmp_path)
        repo = _build_repo(tmp_path)

        d.start_session(str(repo), head="feature/weakened-tests", base="main")
        result = d.facts(str(repo))

        tests = result["tests"]
        files = {entry["path"]: entry for entry in tests["files"]}

        assert "tests/test_math.py" in files, (
            "test_math.py should appear in tests.files"
        )
        entry = files["tests/test_math.py"]
        assert entry["assertions_removed"] == 2, (
            f"expected 2 assertions removed, got {entry['assertions_removed']}"
        )
        assert entry["assertions_added"] >= 1, (
            f"expected at least 1 assertion added, got {entry['assertions_added']}"
        )

        assert len(tests["weakened_units"]) >= 1, (
            "the hunk that removes more assertions than it adds must be in weakened_units"
        )

    def test_weakened_unit_is_in_test_file(self, tmp_path):
        """Every id in weakened_units must belong to the test file, not the source file."""
        d = _load_module(tmp_path)
        repo = _build_repo(tmp_path)

        d.start_session(str(repo), head="feature/weakened-tests", base="main")
        result = d.facts(str(repo))
        s = d._load(repo)

        for uid in result["tests"]["weakened_units"]:
            path = s["units"][uid]["path"]
            assert "test" in path.lower(), (
                f"weakened unit {uid} points to {path!r}, expected a test file"
            )

    def test_areas_cover_both_top_level_folders(self, tmp_path):
        """Both src/ and tests/ must appear as areas with at least 1 file each."""
        d = _load_module(tmp_path)
        repo = _build_repo(tmp_path)

        d.start_session(str(repo), head="feature/weakened-tests", base="main")
        result = d.facts(str(repo))

        folders = {a["folder"]: a for a in result["areas"]}
        assert "src" in folders, f"expected 'src' in areas; got {list(folders)}"
        assert "tests" in folders, f"expected 'tests' in areas; got {list(folders)}"
        assert folders["src"]["files"] >= 1
        assert folders["tests"]["files"] >= 1

    def test_areas_lines_are_non_negative(self, tmp_path):
        """Every area must have non-negative line counts."""
        d = _load_module(tmp_path)
        repo = _build_repo(tmp_path)

        d.start_session(str(repo), head="feature/weakened-tests", base="main")
        result = d.facts(str(repo))

        for area in result["areas"]:
            assert area["lines"] >= 0, f"negative lines in area {area['folder']!r}"

    def test_api_surface_captures_new_def(self, tmp_path):
        """The new `def mul` in src/math.py must be counted as a definition added."""
        d = _load_module(tmp_path)
        repo = _build_repo(tmp_path)

        d.start_session(str(repo), head="feature/weakened-tests", base="main")
        result = d.facts(str(repo))

        api = {entry["path"]: entry for entry in result["api_surface"]}
        assert "src/math.py" in api, (
            f"src/math.py should appear in api_surface; got {list(api)}"
        )
        assert api["src/math.py"]["definitions_added"] >= 1, (
            "new `def mul` should be counted as a definition added"
        )

    def test_test_files_excluded_from_api_surface(self, tmp_path):
        """Test files must not appear in api_surface."""
        d = _load_module(tmp_path)
        repo = _build_repo(tmp_path)

        d.start_session(str(repo), head="feature/weakened-tests", base="main")
        result = d.facts(str(repo))

        api_paths = {entry["path"] for entry in result["api_surface"]}
        for path in api_paths:
            assert "test" not in path.lower(), (
                f"test file {path!r} must not appear in api_surface"
            )

    def test_totals_match_session(self, tmp_path):
        """totals.files and totals.units must match the session counts."""
        d = _load_module(tmp_path)
        repo = _build_repo(tmp_path)

        d.start_session(str(repo), head="feature/weakened-tests", base="main")
        result = d.facts(str(repo))
        s = d._load(repo)

        assert result["totals"]["files"] == len(s["files"])
        assert result["totals"]["units"] == len(s["units"])

    def test_totals_include_annotation_breakdown_when_annotated(self, tmp_path):
        """After annotate_units, totals must include behavioural_lines and mechanical_lines."""
        d = _load_module(tmp_path)
        repo = _build_repo(tmp_path)

        d.start_session(str(repo), head="feature/weakened-tests", base="main")
        s = d._load(repo)
        units_list = list(s["units"].keys())

        d.annotate_units([
            {"unit": units_list[0], "kind": "behavioral", "requirement": "none"},
        ])

        result = d.facts(str(repo))
        assert "behavioural_lines" in result["totals"], (
            "totals should include behavioural_lines after annotate_units"
        )
        assert "mechanical_lines" in result["totals"]

    def test_no_dependencies_in_minimal_repo(self, tmp_path):
        """A repo with no lockfiles or manifests should have an empty dependencies list."""
        d = _load_module(tmp_path)
        repo = _build_repo(tmp_path)

        d.start_session(str(repo), head="feature/weakened-tests", base="main")
        result = d.facts(str(repo))

        assert result["dependencies"] == [], (
            f"expected no dependencies, got {result['dependencies']}"
        )
