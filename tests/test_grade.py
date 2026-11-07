"""Extraction and grading without Docker: diff from a copied tree, reverts, sanitising, reward."""

import io
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from repogym.env.extract import classify_outside, extract, parse_refs
from repogym.grade import Revert, patch_paths, revert_plan, sanitise, score, shadow_candidates
from repogym.models import RunStage, TestRun, TestStatus
from tests.test_history import run

SHADOWABLE = {"pytest", "json", "os", "hypothesis", "_pytest"}


@dataclass
class FakeSpec:
    bare_path: Path
    source_roots: tuple = ("src/pkg/",)
    packages: tuple = ("pkg",)


@pytest.fixture
def repo(tmp_path):
    work = tmp_path / "up"
    work.mkdir()
    run(work, "init", "-q", "-b", "main")
    (work / "src" / "pkg").mkdir(parents=True)
    (work / "tests").mkdir()
    (work / "src" / "pkg" / "__init__.py").write_text("def answer():\n    return 41\n")
    (work / "tests" / "test_pkg.py").write_text(
        "from pkg import answer\n\ndef test_a():\n    assert answer() == 41\n"
    )
    (work / ".gitignore").write_text("*.log\nbuild/\n")
    (work / "pyproject.toml").write_text(
        '[project]\nname = "pkg"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n'
    )
    run(work, "add", "-A")
    run(work, "commit", "-q", "-m", "base")
    base = run(work, "rev-parse", "HEAD")
    bare = tmp_path / "bare.git"
    run(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return {"bare": bare, "base": base, "spec": FakeSpec(bare)}


def agent_tar(files: dict[str, bytes | str | None], git_files: dict[str, str]) -> bytes:
    """A tar shaped like `docker cp container:/work -`: members under work/."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in {**files, **{f".git/{k}": v for k, v in git_files.items()}}.items():
            info = tarfile.TarInfo(f"work/{name}")
            if data is None:
                continue
            raw = data.encode() if isinstance(data, str) else data
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return buffer.getvalue()


BASE_FILES = {
    "src/pkg/__init__.py": "def answer():\n    return 41\n",
    "tests/test_pkg.py": "from pkg import answer\n\ndef test_a():\n    assert answer() == 41\n",
    ".gitignore": "*.log\nbuild/\n",
    "pyproject.toml": '[project]\nname = "pkg"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n',
}


def heads(base, sha=None):
    return {"HEAD": "ref: refs/heads/repogym\n", "refs/heads/repogym": (sha or base) + "\n"}


def test_extract_captures_edits_new_files_and_deletions(repo):
    files = dict(BASE_FILES)
    files["src/pkg/__init__.py"] = "def answer():\n    return 42\n"
    files["src/pkg/new.py"] = "X = 1\n"
    del files["tests/test_pkg.py"]
    files["src/pkg/__pycache__/x.pyc"] = b"\x00junk"
    files["debug.log"] = "ignored by base rules\n"
    work = extract(agent_tar(files, heads(repo["base"])), repo["bare"], repo["base"], [], set())
    assert work.changed == {
        "src/pkg/__init__.py": "M",
        "src/pkg/new.py": "A",
        "tests/test_pkg.py": "D",
    }
    assert "return 42" in work.diff and not work.committed


def test_editing_gitignore_cannot_hide_files(repo):
    files = dict(BASE_FILES)
    files[".gitignore"] = "*.log\nbuild/\nsrc/pkg/sneaky.py\n"
    files["src/pkg/sneaky.py"] = "HIDDEN = True\n"
    work = extract(agent_tar(files, heads(repo["base"])), repo["bare"], repo["base"], [], set())
    assert work.changed["src/pkg/sneaky.py"] == "A" and work.changed[".gitignore"] == "M"


def test_setup_artifacts_and_gitlinks_are_excluded(repo):
    files = dict(BASE_FILES)
    files["src/pkg.egg-info/PKG-INFO"] = "x"
    files["vendor/sub/file.txt"] = "submodule content"
    work = extract(
        agent_tar(files, heads(repo["base"])),
        repo["bare"],
        repo["base"],
        ["src/pkg.egg-info/"],
        {"vendor/sub"},
    )
    assert work.changed == {}


def test_agent_commits_are_detected_and_still_extracted(repo):
    files = dict(BASE_FILES)
    files["tests/test_pkg.py"] = "def test_a():\n    assert True\n"
    work = extract(
        agent_tar(files, heads(repo["base"], "f" * 40)), repo["bare"], repo["base"], [], set()
    )
    assert work.committed and work.changed == {"tests/test_pkg.py": "M"}
    head, refs = parse_refs(
        {".git/HEAD": b"ref: refs/heads/x\n", ".git/packed-refs": b"# pack\nabc refs/heads/x\n"}
    )
    assert head == "abc" and refs == {"refs/heads/x": "abc"}


def plan_for(repo, files, shadowable=SHADOWABLE, test_patch=""):
    work = extract(
        agent_tar(files, heads(repo["base"], "e" * 40)), repo["bare"], repo["base"], [], set()
    )

    def base_blob(path):
        done = subprocess.run(
            ["git", "--git-dir", str(repo["bare"]), "cat-file", "blob", f"{repo['base']}:{path}"],
            capture_output=True,
        )
        return done.stdout if done.returncode == 0 else None

    def exists(path):
        return (
            subprocess.run(
                ["git", "--git-dir", str(repo["bare"]), "cat-file", "-e", f"{repo['base']}:{path}"]
            ).returncode
            == 0
        )

    class Task:
        pass

    task = Task()
    task.test_patch = test_patch
    plan = revert_plan(
        task,
        repo["spec"],
        work,
        exists,
        base_blob,
        lambda p: files.get(p, "").encode() if files.get(p) else None,
        shadowable,
    )
    return work, plan


def test_test_files_and_infrastructure_are_reverted_using_base_commit(repo):
    files = dict(BASE_FILES)
    files["src/pkg/__init__.py"] = "def answer():\n    return 42\n"  # the legitimate fix
    files["tests/test_pkg.py"] = "def test_a():\n    assert True\n"
    files["tests/conftest.py"] = "def pytest_runtest_makereport(): pass\n"
    files["pyproject.toml"] = BASE_FILES["pyproject.toml"].replace('"-q"', '"-p no:x"')
    files["sitecustomize.py"] = "import os\n"
    files["cheat.pth"] = "import os\n"
    files["pytest.py"] = "import sys\nsys.exit(0)\n"
    files["tests/new_test_helper.py"] = "x = 1\n"
    work, plan = plan_for(repo, files)
    kinds = {r.path: (r.kind, r.action) for r in plan}
    assert kinds == {
        "tests/test_pkg.py": ("test_file", "checkout"),
        "tests/conftest.py": ("conftest", "delete"),
        "pyproject.toml": ("test_config", "checkout"),
        "sitecustomize.py": ("import_hook", "delete"),
        "cheat.pth": ("import_hook", "delete"),
        "pytest.py": ("shadowing_module", "delete"),
        "tests/new_test_helper.py": ("test_file", "delete"),
    }
    # The agent committed; the revert still restores base_commit's content, and the fix stays.
    patch = sanitise(repo["spec"], repo["base"], work.diff, plan)
    assert patch_paths(patch) == ["src/pkg/__init__.py"] and "return 42" in patch


def test_shadowing_rule_only_reverts_files_in_an_import_root(repo):
    files = dict(BASE_FILES)
    files["scripts/json.py"] = "x = 1\n"  # not on the import path: must survive
    files["src/pkg/json.py"] = "x = 1\n"  # inside the package: must survive
    files["docs/pytest.py"] = "x = 1\n"  # not an import root
    files["src/hypothesis/__init__.py"] = "x = 1\n"  # src/ layout root: shadows
    files["json.py"] = "x = 1\n"  # repo root: shadows the stdlib
    files["newmodule.py"] = "x = 1\n"  # repo root but shadows nothing
    _, plan = plan_for(repo, files)
    assert {r.path for r in plan} == {"src/hypothesis/__init__.py", "json.py"}
    # A file inside the package defines only "src" (repo root) and "pkg" (src root), neither
    # of which shadows anything; json.py there isn't directly in an import root.
    assert shadow_candidates("src/pkg/json.py") == [("src", "src"), ("pkg", "src/pkg")]
    assert shadow_candidates("json.py") == [("json", "json.py")]
    assert shadow_candidates("src/pytest.py") == [("src", "src"), ("pytest", "src/pytest.py")]


def test_pyproject_change_outside_the_pytest_table_is_kept(repo):
    files = dict(BASE_FILES)
    files["pyproject.toml"] = BASE_FILES["pyproject.toml"].replace(
        'name = "pkg"', 'name = "pkg"\nversion = "2"'
    )
    _, plan = plan_for(repo, files)
    assert plan == []


def test_new_files_at_test_patch_paths_are_deleted(repo):
    files = dict(BASE_FILES)
    files["tests/test_new.py"] = "def test_x():\n    pass\n"
    test_patch = "--- /dev/null\n+++ b/tests/test_new.py\n@@ -0,0 +1 @@\n+x\n"
    _, plan = plan_for(repo, files, test_patch=test_patch)
    assert plan == [Revert(path="tests/test_new.py", kind="test_patch_path", action="delete")]


class T:
    fail_to_pass = ["m::new1", "m::new2"]
    pass_to_pass = ["m::old"]


def test_reward_needs_every_fail_to_pass_and_no_regression():
    P, F, S = TestStatus.PASSED, TestStatus.FAILED, TestStatus.SKIPPED
    done = RunStage.COMPLETED
    assert (
        score(T, TestRun(stage=done, tests={"m::new1": P, "m::new2": P, "m::old": P}))["reward"]
        == 1
    )
    partial = score(T, TestRun(stage=done, tests={"m::new1": P, "m::new2": F, "m::old": P}))
    assert partial["reward"] == 0 and partial["fail_to_pass_passed"] == 1
    regress = score(T, TestRun(stage=done, tests={"m::new1": P, "m::new2": P, "m::old": F}))
    assert regress["reward"] == 0 and regress["pass_to_pass_failed"] == ["m::old"]
    skipped = score(T, TestRun(stage=done, tests={"m::new1": P, "m::new2": S, "m::old": P}))
    assert skipped["reward"] == 0  # a skipped FAIL_TO_PASS test is not a pass
    missing = score(T, TestRun(stage=done, tests={"m::new1": P, "m::old": P}))
    assert missing["reward"] == 0  # a test that never ran counts as failed
    oom = score(T, TestRun(stage=RunStage.OOM, tests={"m::new1": P, "m::new2": P, "m::old": P}))
    assert oom["reward"] == 0


def test_outside_paths_ignore_docker_managed_files_and_pytest_tmp():
    outside, tmp = classify_outside(
        ["/etc/hosts", "/work/x", "/tmp/cheat", "/tmp/pytest-of-agent/p0/x", "/home/agent/.cache/c"]
    )
    assert outside == ["/home/agent/.cache/c", "/tmp/cheat"] and tmp == [
        "/tmp/pytest-of-agent/p0/x"
    ]
