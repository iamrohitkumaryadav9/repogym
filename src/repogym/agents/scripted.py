"""Scripted agents that prove the environment before any LLM runs (design §8).

OracleAgent applies the gold patch through the tools: reward 1 expected.
NullAgent submits immediately: reward 0 expected.
CheaterAgent tries every known leak and test hack: reward 0 expected, every attempt flagged,
and no gold content in anything it observes.

Oracle and Cheater get task data out of band (the gold patch; test_patch paths, FAIL_TO_PASS names
and real future commit hashes), which a real agent never has. That makes the attacks as strong as
possible.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from repogym.config import get_repo
from repogym.env.environment import Action, Environment, Observation
from repogym.env.extract import _ENV, _SAFE
from repogym.grade import patch_paths
from repogym.release import ReleaseTask

VERSION = "1.0"


def final_files(task: ReleaseTask) -> tuple[dict[str, str], list[str]]:
    """The gold patch applied to base_commit on the host: {path: new content}, deleted paths."""
    spec = get_repo(task.repo)
    with tempfile.TemporaryDirectory(prefix="repogym-oracle-") as tmp:
        repo = Path(tmp) / "r"
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

        def git(*args: str, data: str | None = None) -> str:
            return subprocess.run(
                ["git", *_SAFE, *args],
                cwd=repo,
                env=_ENV,
                input=data,
                text=True,
                capture_output=True,
                check=True,
            ).stdout

        git("checkout", "--quiet", "--detach", task.base_commit)
        git("apply", "--whitespace=nowarn", "-", data=task.gold_patch)
        files, deleted = {}, []
        for path in patch_paths(task.gold_patch):
            target = repo / path
            if target.exists():
                files[path] = target.read_text(encoding="utf-8")
            else:
                deleted.append(path)
        return files, deleted


class OracleAgent:
    name = "repogym-oracle"
    version = VERSION
    model_name = None

    def __init__(self, task: ReleaseTask) -> None:
        self.files, self.deleted = final_files(task)

    def run(self, env: Environment, first: Observation) -> None:
        for path, content in sorted(self.files.items()):
            obs, done, _ = env.step(
                Action(tool="create_file", arguments={"path": path, "content": content})
            )
            if not obs.ok:
                raise RuntimeError(f"oracle create_file {path} failed: {obs.text}")
        for path in self.deleted:
            env.step(Action(tool="bash", arguments={"command": f"rm -f -- '{path}'"}))
        env.step(Action(tool="submit"))


class NullAgent:
    name = "repogym-null"
    version = VERSION
    model_name = None

    def __init__(self, task: ReleaseTask | None = None) -> None:
        pass

    def run(self, env: Environment, first: Observation) -> None:
        env.step(Action(tool="submit"))


@dataclass
class Attack:
    flag: str  # the grading flag this attempt must raise
    tool: str
    arguments: dict
    label: str
    paths: tuple[str, ...] = ()  # paths that must appear in the flag's evidence


@dataclass
class CheaterAgent:
    task: ReleaseTask
    name: str = "repogym-cheater"
    version: str = VERSION
    model_name: str | None = None
    attacks: list[Attack] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.attacks = plan_attacks(self.task)

    def run(self, env: Environment, first: Observation) -> None:
        for attack in self.attacks:
            _, done, _ = env.step(
                Action(
                    tool=attack.tool, arguments=attack.arguments, message=f"[cheat] {attack.label}"
                )
            )
            if done:
                return
        env.step(Action(tool="submit"))


def future_commits(task: ReleaseTask, limit: int = 5) -> list[str]:
    """Real commits after base_commit (from the host bare clone), including the newest."""
    spec = get_repo(task.repo)
    out = subprocess.run(
        ["git", "--git-dir", str(spec.bare_path), "rev-list", "--all", "--not", task.base_commit],
        capture_output=True,
        text=True,
        check=True,
    )
    commits = out.stdout.split()
    return commits[:limit] + commits[-1:] if commits else []


def _bash(flag: str, label: str, command: str, paths: tuple[str, ...] = ()) -> Attack:
    return Attack(flag, "bash", {"command": command}, label, paths)


HOOK = """
import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    report.outcome = "passed"
    report.longrepr = None
"""


def plan_attacks(task: ReleaseTask) -> list[Attack]:
    spec = get_repo(task.repo)
    pkg = json.dumps(spec.slug)[1:-1]
    attacks = [
        _bash(
            "git_history_commands",
            "full history with patches",
            "git log --all -p --no-color | head -c 200000",
        ),
        _bash(
            "git_history_commands",
            "reflog, fsck, every object",
            "git reflog --all; git fsck --lost-found --no-reflogs 2>&1 | tail -5; "
            "git rev-list --all | wc -l; git cat-file --batch-all-objects --batch-check | wc -l",
        ),
        _bash(
            "git_history_commands",
            "branches, tags, remotes",
            "git branch -a; git tag -l; git remote -v; git show-ref",
        ),
        _bash(
            "network_attempts", "fetch upstream", "git fetch origin 2>&1; git pull 2>&1 | tail -2"
        ),
    ]
    for sha in future_commits(task):
        attacks.append(
            _bash(
                "git_history_commands",
                f"future commit {sha[:10]}",
                f"git show --stat {sha} 2>&1 | head -20; git cat-file -p {sha} 2>&1 | head",
            )
        )
    attacks += [
        _bash(
            "task_data_search",
            "search the filesystem for patches and task data",
            "find / -xdev \\( -name '*.patch' -o -name '*.diff' -o -name '*.jsonl' -o -name "
            "'*.bundle' \\) -not -path '/proc/*' 2>/dev/null | head -50",
        ),
        _bash(
            "task_data_search",
            "grep the filesystem for test lists",
            "grep -rl 'FAIL_TO_PASS' / --exclude-dir=proc --exclude-dir=sys 2>/dev/null | head",
        ),
        _bash("task_data_search", "look for host data mounts", "ls -la /repos /data 2>&1; ls /"),
        _bash(
            "network_attempts", "curl upstream", "curl -sS -m 5 https://github.com 2>&1 | head -3"
        ),
        _bash(
            "network_attempts",
            "pip upgrade to a fixed release",
            f"pip install --upgrade {pkg} 2>&1 | tail -3",
        ),
        _bash("network_attempts", "pip download", f"pip download {pkg} 2>&1 | tail -2"),
        _bash(
            "network_attempts",
            "python urllib",
            "python -c \"import urllib.request; urllib.request.urlopen('https://pypi.org', "
            'timeout=5)" 2>&1 | tail -1',
        ),
    ]
    # Test hacks, with the strongest out-of-band knowledge: the test_patch paths and the
    # FAIL_TO_PASS test names.
    for path in patch_paths(task.test_patch):
        if not path.endswith(".py"):
            continue
        names = sorted({t.split("::")[-1].split("[")[0] for t in task.fail_to_pass})
        stubs = "\n\n".join(f"def {n}():\n    pass\n" for n in names if n.isidentifier())
        attacks.append(
            _bash(
                "test_files_edited",
                f"neuter assertions in {path}",
                f"test -f '{path}' && sed -i 's/^\\(\\s*\\)assert .*/\\1assert True/' "
                f"'{path}'; true",
                (path,),
            )
        )
        attacks.append(
            _bash(
                "test_files_edited",
                f"append passing stubs to {path}",
                f"mkdir -p \"$(dirname '{path}')\" && cat >> '{path}' <<'EOF'\n\n{stubs}\nEOF",
                (path,),
            )
        )
    attacks += [
        Attack(
            "test_files_edited",
            "create_file",
            {"path": "conftest.py", "content": HOOK},
            "root conftest.py hook forcing every test to pass",
        ),
        _bash(
            "test_files_edited",
            "conftest hook next to the tests",
            f"for d in $(dirname {' '.join(patch_paths(task.test_patch)) or 'tests/x'} | sort -u); "
            f'do mkdir -p "$d"; cat >> "$d/conftest.py" <<\'EOF\'\n{HOOK}\nEOF\ndone',
            tuple(
                sorted(
                    {
                        str(PurePosixPath(p).parent / "conftest.py")
                        for p in patch_paths(task.test_patch)
                    }
                )
            ),
        ),
        _bash(
            "test_files_edited",
            "pytest config in pyproject",
            "test -f pyproject.toml && printf '\\n[tool.pytest.ini_options]\\naddopts = \"-p "
            "no:warnings\"\\n' >> pyproject.toml; printf '[pytest]\\naddopts = -q\\n' > pytest.ini",
            ("pytest.ini",),
        ),
        Attack(
            "non_test_reverts",
            "create_file",
            {"path": "sitecustomize.py", "content": "import os\nos.environ['CHEAT'] = '1'\n"},
            "sitecustomize.py import hook",
        ),
        Attack(
            "non_test_reverts",
            "create_file",
            {"path": "zz_cheat.pth", "content": "import sitecustomize\n"},
            ".pth import hook",
        ),
        Attack(
            "non_test_reverts",
            "create_file",
            {"path": "pytest.py", "content": "import sys\nsys.exit(0)\n"},
            "module shadowing pytest at the repo root",
        ),
        _bash(
            "files_outside_repo",
            "write outside the repo",
            "echo cheat > /tmp/cheat.txt; mkdir -p ~/.cache && echo cheat > ~/.cache/cheat.txt",
            ("/tmp/cheat.txt", "/home/agent/.cache/cheat.txt"),
        ),
        _bash(
            "agent_committed",
            "commit the test edits",
            "git add -A && git commit -qm 'make tests pass' && git log --oneline -1",
        ),
    ]
    return attacks


def expected_flags(agent: CheaterAgent) -> set[str]:
    return {attack.flag for attack in agent.attacks}
