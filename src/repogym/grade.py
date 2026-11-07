"""Grade an episode outside the agent's view (design §5, review D2/D3).

1. The agent's diff was extracted on the host (repogym.env.extract), never via its git.
2. Test files and test infrastructure the agent changed are reverted to base_commit, in a
   pristine host checkout: `git checkout <base_commit> -- <path>`, or deletion if the path
   didn't exist at base. The reverts cover test_patch paths, every test file, conftest.py, pytest
   configuration, sitecustomize/usercustomize/.pth, and new modules that would shadow an import
   from an import root. Every revert is recorded; reverts of non-test files are flagged.
3. The sanitised patch then goes through validation's own runner in a fresh container, with
   test_patch, the same per-test timeout and the same OOM handling.
4. Reward 1 only if every FAIL_TO_PASS passes and every PASS_TO_PASS still passes.
"""

from __future__ import annotations

import configparser
import json
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

from repogym.config import CACHE_DIR, RepoSpec, get_repo
from repogym.env.docker import DockerUnavailable, image_id, run_tests
from repogym.env.extract import _ENV, _SAFE, AgentWork, ExtractError
from repogym.mining.patches import FileKind, classify_path
from repogym.models import RunStage, TestRun, TestStatus
from repogym.release import ReleaseTask

IMPORT_ROOTS = ("", "src")
PYTEST_SECTIONS = {"pytest.ini": None, "tox.ini": ("pytest",), "setup.cfg": ("tool:pytest",)}
TEST_REVERT_KINDS = {"test_patch_path", "test_file", "conftest", "test_config"}


class Revert(BaseModel):
    path: str
    kind: (
        str  # test_patch_path | test_file | conftest | test_config | import_hook | shadowing_module
    )
    action: str  # checkout | delete


class Grade(BaseModel):
    status: str  # graded | error
    reward: int | None = None
    fail_to_pass_passed: int = 0
    fail_to_pass_total: int = 0
    pass_to_pass_failed: list[str] = Field(default_factory=list)
    pass_to_pass_total: int = 0
    fail_to_pass_failed: list[str] = Field(default_factory=list)
    stage: str | None = None
    reverts: list[Revert] = Field(default_factory=list)
    graded_patch_lines: int = 0
    error: str = ""
    peak_memory_bytes: int | None = None
    seconds: float = 0.0

    @property
    def fail_to_pass_fraction(self) -> float | None:
        return (
            self.fail_to_pass_passed / self.fail_to_pass_total if self.fail_to_pass_total else None
        )


def patch_paths(patch: str) -> list[str]:
    paths = []
    for line in patch.splitlines():
        if line.startswith(("--- a/", "+++ b/")):
            paths.append(line[6:])
    return sorted(set(paths))


def image_modules(spec: RepoSpec) -> set[str]:
    """Top-level names importable in the task image (stdlib, builtins and installed), cached
    per image digest. Used only to decide whether a new top-level module would shadow one."""
    digest = image_id(spec.image)
    cache = CACHE_DIR / "image_modules" / f"{digest.replace(':', '_')}.json"
    if cache.exists():
        return set(json.loads(cache.read_text()))
    code = (
        "import sys, json, pkgutil\n"
        "names = set(sys.stdlib_module_names) | set(sys.builtin_module_names)\n"
        "names |= {m.name for m in pkgutil.iter_modules()}\n"
        "print(json.dumps(sorted(names)))"
    )
    done = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", spec.image, "python3", "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if done.returncode != 0:
        raise DockerUnavailable(done.stderr[-500:])
    names = json.loads(done.stdout)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(names))
    return set(names)


def _pytest_config_changed(name: str, base: bytes | None, agent: bytes | None) -> bool:
    if name == "pyproject.toml":

        def table(data: bytes | None):
            try:
                return tomllib.loads(data.decode()).get("tool", {}).get("pytest") if data else None
            except (tomllib.TOMLDecodeError, UnicodeDecodeError):
                return "<unparseable>"

        return table(base) != table(agent)

    def sections(data: bytes | None):
        parser = configparser.RawConfigParser(strict=False, interpolation=None)
        try:
            parser.read_string(data.decode() if data else "")
        except (configparser.Error, UnicodeDecodeError):
            return "<unparseable>"
        wanted = PYTEST_SECTIONS[name]
        return {s: dict(parser.items(s)) for s in parser.sections() if s in wanted}

    return sections(base) != sections(agent)


def shadow_candidates(path: str) -> list[tuple[str, str]]:
    """(module name, top-level path) that ``path`` would define in each import root.

    Only files directly in an import root (the repo root, or ``src/``) can shadow an import:
    ``pytest.py`` or ``src/pytest/__init__.py`` can; ``scripts/json.py`` can't.
    """
    parts = PurePosixPath(path).parts
    found = []
    for root in IMPORT_ROOTS:
        if root and (len(parts) < 2 or parts[0] != root):
            continue
        inner = parts[1:] if root else parts
        prefix = f"{root}/" if root else ""
        if len(inner) == 1 and inner[0].endswith(".py"):
            found.append((inner[0][:-3], prefix + inner[0]))
        elif len(inner) > 1:
            found.append((inner[0], prefix + inner[0]))
    return found


def revert_plan(
    task: ReleaseTask,
    spec: RepoSpec,
    work: AgentWork,
    exists_at_base: Callable[[str], bool],
    read_base: Callable[[str], bytes | None],
    read_agent: Callable[[str], bytes | None],
    shadowable: set[str],
) -> list[Revert]:
    """Which of the agent's changed paths are restored to base_commit before grading."""
    test_patch = set(patch_paths(task.test_patch))
    own = set(spec.packages)
    plan: list[Revert] = []
    for path, status in sorted(work.changed.items()):
        pure = PurePosixPath(path)
        kind = None
        if path in test_patch:
            kind = "test_patch_path"
        elif pure.name == "conftest.py":
            kind = "conftest"
        elif classify_path(path, spec.source_roots) is FileKind.TEST:
            kind = "test_file"
        elif pure.name == "pytest.ini":
            kind = "test_config"
        elif pure.name in PYTEST_SECTIONS or pure.name == "pyproject.toml":
            if _pytest_config_changed(pure.name, read_base(path), read_agent(path)):
                kind = "test_config"
        elif pure.name in ("sitecustomize.py", "usercustomize.py") or pure.suffix == ".pth":
            kind = "import_hook"
        elif status == "A":
            for module, top in shadow_candidates(path):
                if module in shadowable and module not in own and not exists_at_base(top):
                    kind = "shadowing_module"
                    break
        if kind:
            plan.append(
                Revert(
                    path=path, kind=kind, action="checkout" if exists_at_base(path) else "delete"
                )
            )
    return plan


def sanitise(spec: RepoSpec, base_commit: str, diff: str, plan: list[Revert]) -> str:
    """The agent's diff with the planned reverts applied, from a pristine host checkout."""
    if not diff.strip():
        return ""
    with tempfile.TemporaryDirectory(prefix="repogym-grade-") as tmp:
        repo = Path(tmp) / "repo"
        subprocess.run(
            [
                "git",
                *_SAFE,
                "clone",
                "--quiet",
                "--no-checkout",
                "--shared",
                str(spec.bare_path),
                str(repo),
            ],
            env=_ENV,
            check=True,
            capture_output=True,
        )

        def git(*args: str, data: bytes | None = None) -> bytes:
            done = subprocess.run(
                ["git", *_SAFE, *args], cwd=repo, env=_ENV, input=data, capture_output=True
            )
            if done.returncode != 0:
                raise ExtractError(f"git {args[0]}: {done.stderr.decode()[-500:]}")
            return done.stdout

        git("checkout", "--quiet", "--detach", base_commit)
        git(
            "apply",
            "--index",
            "--binary",
            "--whitespace=nowarn",
            "-",
            data=diff.encode("utf-8", errors="surrogateescape"),
        )
        for revert in plan:
            if revert.action == "checkout":
                git("checkout", base_commit, "--", revert.path)
            else:
                git("rm", "--quiet", "--force", "--ignore-unmatch", "--", revert.path)
        return git(
            "diff",
            "--cached",
            "--binary",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            base_commit,
        ).decode("utf-8", errors="surrogateescape")


def score(task: ReleaseTask, run: TestRun) -> dict:
    statuses = run.tests
    f2p_failed = [t for t in task.fail_to_pass if statuses.get(t) is not TestStatus.PASSED]
    p2p_failed = [t for t in task.pass_to_pass if statuses.get(t) is not TestStatus.PASSED]
    reward = int(run.stage is RunStage.COMPLETED and not f2p_failed and not p2p_failed)
    return {
        "reward": reward,
        "fail_to_pass_passed": len(task.fail_to_pass) - len(f2p_failed),
        "fail_to_pass_total": len(task.fail_to_pass),
        "fail_to_pass_failed": f2p_failed,
        "pass_to_pass_failed": p2p_failed,
        "pass_to_pass_total": len(task.pass_to_pass),
    }


def grade(
    task: ReleaseTask,
    work: AgentWork,
    test_files: list[str],
    read_agent: Callable[[str], bytes | None],
    runner=run_tests,
) -> Grade:
    start = time.monotonic()
    spec = get_repo(task.repo)

    def read_base(path: str) -> bytes | None:
        done = subprocess.run(
            [
                "git",
                "--git-dir",
                str(spec.bare_path),
                "cat-file",
                "blob",
                f"{task.base_commit}:{path}",
            ],
            capture_output=True,
            env=_ENV,
        )
        return done.stdout if done.returncode == 0 else None

    def exists_at_base(path: str) -> bool:
        done = subprocess.run(
            [
                "git",
                "--git-dir",
                str(spec.bare_path),
                "cat-file",
                "-e",
                f"{task.base_commit}:{path}",
            ],
            capture_output=True,
            env=_ENV,
        )
        return done.returncode == 0

    try:
        plan = revert_plan(
            task, spec, work, exists_at_base, read_base, read_agent, image_modules(spec)
        )
        patch = sanitise(spec, task.base_commit, work.diff, plan)
        run = runner(spec, task.base_commit, patch, task.test_patch, test_files)
    except (DockerUnavailable, ExtractError, subprocess.SubprocessError) as exc:
        return Grade(
            status="error",
            error=f"{type(exc).__name__}: {exc}"[:2000],
            seconds=round(time.monotonic() - start, 1),
        )
    if run.stage in (RunStage.CHECKOUT_FAILED, RunStage.INSTALL_FAILED, RunStage.SUBMODULE_FAILED):
        return Grade(
            status="error",
            stage=run.stage.value,
            error=run.detail[-1000:],
            reverts=plan,
            seconds=round(time.monotonic() - start, 1),
        )
    return Grade(
        status="graded",
        stage=run.stage.value,
        reverts=plan,
        graded_patch_lines=len(patch.splitlines()),
        peak_memory_bytes=run.peak_memory_bytes,
        error="" if run.stage is RunStage.COMPLETED else run.detail[-1000:],
        seconds=round(time.monotonic() - start, 1),
        **score(task, run),
    )
