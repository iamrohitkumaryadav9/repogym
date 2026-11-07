"""Extract what the agent changed, without trusting anything the agent controls (design D2).

The working tree is copied out with ``docker cp`` (no agent code runs). The diff is computed in a
pristine host checkout of base_commit, with git hooks, filters and user config disabled. Ignore
rules come from base_commit's .gitignore files, so the agent can't hide files by editing them.
The agent's .git is never used by git; it's only parsed as data, to see whether the agent
committed or created refs.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

JUNK_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".hypothesis", ".tox", ".nox"}
JUNK_SUFFIXES = (".pyc", ".pyo")
TEST_TMP = "/tmp/pytest-of-agent"  # pytest's own tmp_path dirs, created by run_tests
# Docker bind-mounts these into every container; their ctime comes from the host and the agent
# (non-root, files owned by root) can't write them.
DOCKER_MANAGED = {"/etc/hostname", "/etc/hosts", "/etc/resolv.conf"}

_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "HOME": "/nonexistent",
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
}
_SAFE = [
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.attributesFile=/dev/null",
]
# External diff drivers are impossible here: user and system config are disabled (_ENV), the
# clone is our own, and every `git diff` passes --no-ext-diff and --no-textconv. (Setting
# diff.external to an empty value does NOT disable it; git tries to run an empty command.)


class ExtractError(RuntimeError):
    pass


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    done = subprocess.run(
        ["git", *_SAFE, *args], cwd=repo, env=_ENV, input=input_bytes, capture_output=True
    )
    if done.returncode != 0:
        raise ExtractError(f"git {args[0]} failed: {done.stderr.decode()[-600:]}")
    return done.stdout


@dataclass
class AgentWork:
    diff: str
    changed: dict[str, str]  # path -> git status letter (A, M, D, T)
    committed: bool
    refs: dict[str, str]  # agent's refs (parsed as data)
    head: str | None
    outside_repo: list[str] = field(default_factory=list)
    test_tmp: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    symlinks_outside: list[str] = field(default_factory=list)


def _skip(rel: str, excluded: set[str], gitlinks: set[str]) -> bool:
    pure = PurePosixPath(rel)
    if pure.parts[:1] == (".git",) or rel in excluded:
        return True
    if any(rel == g or rel.startswith(g + "/") for g in gitlinks):
        return True
    if any(part in JUNK_PARTS for part in pure.parts) or rel.endswith(JUNK_SUFFIXES):
        return True
    return any(rel.startswith(a.rstrip("/") + "/") for a in excluded if a.endswith("/"))


def parse_refs(members: dict[str, bytes]) -> tuple[str | None, dict[str, str]]:
    """HEAD and refs from a copied .git, read as plain files (no git, no config, no hooks)."""
    refs: dict[str, str] = {}
    for name, data in members.items():
        if name.startswith(".git/refs/"):
            refs[name[len(".git/") :]] = data.decode(errors="replace").strip()
    for line in members.get(".git/packed-refs", b"").decode(errors="replace").splitlines():
        if line and not line.startswith(("#", "^")) and " " in line:
            sha, ref = line.split(" ", 1)
            refs.setdefault(ref.strip(), sha)
    head = members.get(".git/HEAD", b"").decode(errors="replace").strip() or None
    if head and head.startswith("ref: "):
        head = refs.get(head[5:], head)
    return head, refs


def extract(
    tar_bytes: bytes, bare: Path, base_commit: str, setup_artifacts: list[str], gitlinks: set[str]
) -> AgentWork:
    """Diff of the copied /work tree against base_commit, computed in a pristine checkout."""
    excluded = set(setup_artifacts)
    git_files: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory(prefix="repogym-extract-") as tmp:
        repo = Path(tmp) / "repo"
        subprocess.run(
            ["git", *_SAFE, "clone", "--quiet", "--no-checkout", "--shared", str(bare), str(repo)],
            env=_ENV,
            check=True,
            capture_output=True,
        )
        _git(repo, "checkout", "--quiet", "--detach", base_commit)
        base_ignores = [
            p
            for p in _git(repo, "ls-files", "-z").decode().split("\0")
            if PurePosixPath(p).name == ".gitignore"
        ]
        for entry in repo.iterdir():
            if entry.name != ".git":
                shutil.rmtree(
                    entry
                ) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
        agent_ignores: dict[str, bytes] = {}
        symlinks_outside = []
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
            for member in tar:
                name = member.name
                if not name.startswith("work/"):
                    continue
                rel = name[len("work/") :]
                if rel.startswith(".git/") or rel == ".git":
                    if member.isfile() and (
                        rel == ".git/HEAD"
                        or rel == ".git/packed-refs"
                        or rel.startswith(".git/refs/")
                    ):
                        git_files[rel] = tar.extractfile(member).read()  # type: ignore[union-attr]
                    continue
                if not rel or ".." in PurePosixPath(rel).parts or _skip(rel, excluded, gitlinks):
                    continue
                dest = repo / rel
                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                elif member.issym():
                    target = member.linkname
                    resolved = os.path.normpath(os.path.join("/work", os.path.dirname(rel), target))
                    if not (resolved == "/work" or resolved.startswith("/work/")):
                        symlinks_outside.append(f"{rel} -> {target}")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(target, dest)
                elif member.isfile():
                    data = tar.extractfile(member).read()  # type: ignore[union-attr]
                    if PurePosixPath(rel).name == ".gitignore":
                        agent_ignores[rel] = data
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                    os.chmod(dest, 0o755 if member.mode & 0o111 else 0o644)
        # Select files with base_commit's ignore rules, then record the agent's .gitignore
        # edits as ordinary content.
        if base_ignores:
            _git(repo, "checkout", base_commit, "--", *base_ignores)
        # Submodule paths keep their base gitlink: the agent's copy of them isn't extracted.
        keep = [f":(exclude){g}" for g in sorted(gitlinks)]
        _git(repo, "add", "--all", "--", ".", *keep)
        for path in base_ignores:
            if path not in agent_ignores:
                (repo / path).unlink(missing_ok=True)
        for path, data in agent_ignores.items():
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_bytes(data)
        if base_ignores or agent_ignores:
            _git(
                repo,
                "add",
                "--all",
                "--force",
                "--",
                *sorted(set(base_ignores) | set(agent_ignores)),
            )
        diff = _git(
            repo,
            "diff",
            "--cached",
            "--binary",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            base_commit,
        ).decode("utf-8", errors="surrogateescape")
        status = (
            _git(repo, "diff", "--cached", "--name-status", "-z", "--no-renames", base_commit)
            .decode()
            .split("\0")
        )
    changed = {status[i + 1]: status[i][0] for i in range(0, len(status) - 1, 2) if status[i]}
    head, refs = parse_refs(git_files)
    committed = (
        head != base_commit
        or any(sha != base_commit for sha in refs.values())
        or (set(refs) - {"refs/heads/repogym"} != set())
    )
    return AgentWork(
        diff=diff,
        changed=changed,
        committed=committed,
        refs=refs,
        head=head,
        symlinks_outside=symlinks_outside,
    )


def classify_outside(paths: list[str]) -> tuple[list[str], list[str]]:
    """(files outside the repo, pytest's own tmp dirs). /work and kernel paths are excluded."""
    outside, test_tmp = [], []
    for path in sorted(set(paths)):
        if path == "/work" or path.startswith(("/work/", "/proc/", "/sys/", "/dev/")):
            continue
        if path in DOCKER_MANAGED:
            continue
        (test_tmp if path == TEST_TMP or path.startswith(TEST_TMP + "/") else outside).append(path)
    return outside, test_tmp
