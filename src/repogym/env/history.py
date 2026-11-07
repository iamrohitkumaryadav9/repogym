"""Git history for an episode: built from base_commit's ancestors only, then probed for leaks.

The agent's repository is never a stripped full clone. A git bundle is made on the host that
holds exactly ``base_commit`` and its ancestors. Future commits, other branches, tags and reflog
entries therefore never enter the container (SWE-bench #465; SWE-Bench Pro: refs deleted but
objects still recoverable). The bundle is probed on the host for the gold patch, so the gold
patch itself never has to enter the container, even during setup.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

MIN_DISTINCTIVE_CHARS = 20


class HistoryError(RuntimeError):
    pass


def git(*args: str, cwd: Path | None = None, input_text: str | None = None,
        check: bool = True) -> str:  # fmt: skip
    done = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd, input=input_text, text=True, capture_output=True,
    )  # fmt: skip
    if check and done.returncode != 0:
        raise HistoryError(f"git {' '.join(args[:3])} failed: {done.stderr.strip()[-500:]}")
    return done.stdout


BUNDLE_REF = "refs/heads/repogym"


def make_bundle(bare: Path, commit: str, dest: Path) -> Path:
    """A bundle with exactly ``commit`` and its ancestors, as ``refs/heads/repogym``.

    A uniquely named temporary ref in the bare clone selects the objects; it's removed after,
    so concurrent episodes don't interfere.
    """
    temp_ref = f"refs/repogym-tmp/{uuid.uuid4().hex}"
    git("--git-dir", str(bare), "update-ref", temp_ref, commit)
    try:
        with tempfile.TemporaryDirectory(prefix="repogym-bundle-") as tmp:
            # Re-home the objects under the final ref name in a throwaway repo, so the bundle
            # advertises refs/heads/repogym and nothing about the bare clone's refs.
            work = Path(tmp) / "r.git"
            git("init", "--bare", "--quiet", str(work))
            git("--git-dir", str(work), "fetch", "--quiet", "--no-tags", str(bare),
                f"{temp_ref}:{BUNDLE_REF}")  # fmt: skip
            git("--git-dir", str(work), "bundle", "create", "--quiet", str(dest), BUNDLE_REF)
    finally:
        git("--git-dir", str(bare), "update-ref", "-d", temp_ref, check=False)
    return dest


def bundle_commits(bundle: Path) -> list[str]:
    """Every commit object in a bundle (all reachable from its single ref, by construction)."""
    with tempfile.TemporaryDirectory(prefix="repogym-probe-") as tmp:
        work = Path(tmp) / "r.git"
        git("init", "--bare", "--quiet", str(work))
        git("--git-dir", str(work), "fetch", "--quiet", "--no-tags", str(bundle),
            f"{BUNDLE_REF}:{BUNDLE_REF}")  # fmt: skip
        return sorted(git("--git-dir", str(work), "rev-list", "--all").split())


def commit_set_digest(commits: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(commits)).encode()).hexdigest()


def added_lines(patch: str) -> list[str]:
    return [
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def distinctive_lines(bare: Path, commit: str, patch: str) -> list[str]:
    """Added lines (stripped) long enough to be specific and absent from the tree at ``commit``.

    These are what the gold patch would leave behind in any history that contained it.
    """
    candidates = sorted({line.strip() for line in added_lines(patch)
                         if len(line.strip()) >= MIN_DISTINCTIVE_CHARS})  # fmt: skip
    if not candidates:
        return []
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        handle.write("\n".join(candidates) + "\n")
        patterns = handle.name
    try:
        found = git("--git-dir", str(bare), "grep", "-h", "-F", "-o", "-f", patterns, commit,
                    "--", check=False)  # fmt: skip
    finally:
        Path(patterns).unlink()
    present = {line.split(":", 1)[-1] for line in found.splitlines()}
    return [line for line in candidates if line not in present and f"{commit}:{line}" not in found]


@dataclass
class ProbeResult:
    commits: int
    commit_set_sha256: str
    distinctive_lines: int
    patch_id_match: list[str] = field(default_factory=list)  # commits whose patch-id = gold's
    line_hits: dict[str, list[str]] = field(default_factory=dict)  # line -> commits adding it

    @property
    def clean(self) -> bool:
        return not self.patch_id_match and not self.line_hits


_COMMIT = re.compile(r"^commit ([0-9a-f]{40})")


def patch_id(patch: str) -> str | None:
    out = git("patch-id", "--stable", input_text=patch, check=False).split()
    return out[0] if out else None


def probe_bundle(bundle: Path, gold_patch: str, distinctive: list[str]) -> ProbeResult:
    """Search the bundle's entire history (``git log --all -p``) for the gold patch."""
    wanted = set(distinctive)
    gold_id = patch_id(gold_patch)
    with tempfile.TemporaryDirectory(prefix="repogym-probe-") as tmp:
        work = Path(tmp) / "r.git"
        git("init", "--bare", "--quiet", str(work))
        git("--git-dir", str(work), "fetch", "--quiet", "--no-tags", str(bundle),
            f"{BUNDLE_REF}:{BUNDLE_REF}")  # fmt: skip
        commits = git("--git-dir", str(work), "rev-list", "--all").split()
        log = subprocess.Popen(
            ["git", "--git-dir", str(work), "log", "--all", "-p", "--no-color", "--no-ext-diff",
             "--format=commit %H"],
            stdout=subprocess.PIPE, text=True, errors="replace",
        )  # fmt: skip
        hits: dict[str, list[str]] = {}
        current = ""
        log_path = Path(tmp) / "log.patch"
        assert log.stdout is not None
        with log_path.open("w", encoding="utf-8") as saved:
            for line in log.stdout:
                saved.write(line)
                if match := _COMMIT.match(line):
                    current = match[1]
                elif line.startswith("+") and not line.startswith("+++"):
                    text = line[1:].strip()
                    if text in wanted:
                        hits.setdefault(text, []).append(current)
        log.wait()
        id_match: list[str] = []
        if gold_id:
            with log_path.open(encoding="utf-8") as saved:
                ids = subprocess.run(["git", "patch-id", "--stable"], stdin=saved,
                                     capture_output=True, text=True).stdout  # fmt: skip
            id_match = [c for pid, c in (row.split() for row in ids.splitlines() if row)
                        if pid == gold_id]  # fmt: skip
    return ProbeResult(
        commits=len(commits),
        commit_set_sha256=commit_set_digest(commits),
        distinctive_lines=len(distinctive),
        patch_id_match=id_match,
        line_hits=hits,
    )
